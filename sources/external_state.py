"""Immutable S3-compatible provider snapshots with verified SQLite staging.

Provider snapshots are stored as content-addressed JSONL/Zstandard objects. Small
manifests remain in the radsync-state Git branch. Objects are never overwritten:
their keys include the SHA-256 digest of the compressed bytes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import ijson
import zstandard
from botocore.config import Config
from botocore.exceptions import ClientError

from sources.state import STATE_SCHEMA_VERSION, snapshot_path


ARCHIVE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staged_stations (
    provider TEXT NOT NULL,
    station_key TEXT NOT NULL,
    station_index INTEGER NOT NULL,
    station_json TEXT NOT NULL,
    provenance_status TEXT NOT NULL,
    PRIMARY KEY (provider, station_key)
);
CREATE TABLE IF NOT EXISTS staged_streams (
    provider TEXT NOT NULL,
    station_key TEXT NOT NULL,
    stream_index INTEGER NOT NULL,
    record_index INTEGER NOT NULL,
    url_hash TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    codec TEXT,
    bitrate INTEGER,
    source_record_id TEXT,
    playlist_path TEXT,
    entry_index INTEGER,
    provenance_status TEXT NOT NULL,
    PRIMARY KEY (provider, station_key, stream_index, record_index)
);
"""


class ExternalStateError(RuntimeError):
    """Raised when external snapshot state cannot be verified safely."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _safe_source_id(value):
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "")).strip(".-")
    if not safe or safe != value:
        raise ExternalStateError(f"Unsafe provider identifier: {value!r}")
    return safe


def _read_snapshot_metadata(path):
    metadata = {}
    with Path(path).open("rb") as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix in {"schemaVersion", "source", "updatedAt", "stationCount"} and event in {
                "number", "string"
            }:
                metadata[prefix] = value
    return metadata


def _iter_snapshot_stations(path):
    with Path(path).open("rb") as handle:
        yield from ijson.items(handle, "stations.item", use_float=True)


def _valid_http_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def _normalized_url(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
    ).geturl()


def _count_snapshot(path, expected_source=None):
    metadata = _read_snapshot_metadata(path)
    if metadata.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise ExternalStateError(f"{path}: unsupported provider snapshot schema")
    if expected_source and metadata.get("source") != expected_source:
        raise ExternalStateError(f"{path}: provider identity mismatch")
    stations = 0
    streams = 0
    for station in _iter_snapshot_stations(path):
        if not isinstance(station, dict) or not station.get("name"):
            raise ExternalStateError(f"{path}: invalid station record")
        station_streams = station.get("streams")
        if not isinstance(station_streams, list) or not station_streams:
            raise ExternalStateError(f"{path}: station without streams")
        for stream in station_streams:
            if not isinstance(stream, dict) or not _valid_http_url(stream.get("url")):
                raise ExternalStateError(f"{path}: invalid stream URL")
        stations += 1
        streams += len(station_streams)
    if stations < 1 or metadata.get("stationCount") != stations:
        raise ExternalStateError(f"{path}: station count does not match snapshot header")
    metadata["stationCount"] = stations
    metadata["streamCount"] = streams
    return metadata


def _station_provenance_status(station, source_id):
    """Use stored status when present; otherwise classify only observed evidence."""
    status = (station.get("_provenanceStatusBySource") or {}).get(source_id)
    status = status or station.get("_provenanceStatus")
    if status in {"fresh", "restored-last-good", "legacy-incomplete-provenance"}:
        return status

    records = list(station.get("_identityEvidence") or [])
    records.extend(
        record
        for stream in station.get("streams", [])
        for record in (stream.get("sourceRecords") or [])
    )
    records = [
        record for record in records
        if isinstance(record, dict) and str(record.get("source") or "") == source_id
    ]
    if not records:
        return "legacy-incomplete-provenance"

    if source_id in {"iprd", "alon-drilich"}:
        required = (
            "source", "sourceUrl", "playlistRepository", "playlistPath",
            "rawExtinf", "originalTitle", "originalUrl", "streamUrl", "ingestedAt",
        )
        complete = all(
            all(record.get(field) not in (None, "") for field in required)
            and record.get("entryIndex") is not None
            for record in records
        )
    else:
        required = ("source", "streamUrl", "originalUrl", "ingestedAt")
        complete = all(
            all(record.get(field) not in (None, "") for field in required)
            for record in records
        )
    return "restored-last-good" if complete else "legacy-incomplete-provenance"


def _canonical_station_bytes(station):
    return json.dumps(
        station, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _create_archive(snapshot_file, archive_file, source, anomaly_bounds):
    metadata = _count_snapshot(snapshot_file, source)
    record_digest = hashlib.sha256()
    station_count = 0
    stream_count = 0
    header = {
        "recordType": "header",
        "archiveSchemaVersion": ARCHIVE_SCHEMA_VERSION,
        "snapshotSchemaVersion": STATE_SCHEMA_VERSION,
        "source": source,
        "snapshotUpdatedAt": metadata.get("updatedAt"),
        "createdAt": _utc_now(),
        "stationCount": metadata["stationCount"],
        "streamCount": metadata["streamCount"],
        "anomalyBounds": anomaly_bounds or {},
    }
    compressor = zstandard.ZstdCompressor(level=8)
    with Path(archive_file).open("wb") as raw_output:
        with compressor.stream_writer(raw_output, closefd=False) as writer:
            writer.write(json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
            for station in _iter_snapshot_stations(snapshot_file):
                encoded = _canonical_station_bytes(station)
                record_digest.update(encoded + b"\n")
                writer.write(
                    json.dumps(
                        {"recordType": "station", "station": station},
                        ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8") + b"\n"
                )
                station_count += 1
                stream_count += len(station.get("streams", []))
            footer = {
                "recordType": "footer",
                "stationCount": station_count,
                "streamCount": stream_count,
                "recordsSha256": record_digest.hexdigest(),
            }
            writer.write(json.dumps(footer, sort_keys=True).encode("utf-8") + b"\n")
    if station_count != metadata["stationCount"] or stream_count != metadata["streamCount"]:
        raise ExternalStateError(f"{source}: snapshot changed while archive was being created")
    return {
        "stationCount": station_count,
        "streamCount": stream_count,
        "snapshotUpdatedAt": metadata.get("updatedAt"),
    }


def _stage_archive(archive_file, database_file, expected_source, expected_digest=None,
                   expected_station_count=None, expected_stream_count=None):
    if expected_digest:
        actual = _sha256_file(archive_file)
        if actual != expected_digest:
            raise ExternalStateError(
                f"{expected_source}: compressed object SHA-256 mismatch"
            )

    connection = sqlite3.connect(database_file)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.executescript(STAGING_SCHEMA)
        connection.execute("DELETE FROM staged_streams WHERE provider = ?", (expected_source,))
        connection.execute("DELETE FROM staged_stations WHERE provider = ?", (expected_source,))

        record_digest = hashlib.sha256()
        station_count = 0
        stream_count = 0
        with Path(archive_file).open("rb") as compressed:
            reader = zstandard.ZstdDecompressor().stream_reader(compressed)
            text = io.TextIOWrapper(reader, encoding="utf-8")
            try:
                header_line = text.readline()
                if not header_line:
                    raise ExternalStateError(f"{expected_source}: empty compressed snapshot")
                header = json.loads(header_line)
                if (
                    header.get("recordType") != "header"
                    or header.get("archiveSchemaVersion") != ARCHIVE_SCHEMA_VERSION
                    or header.get("snapshotSchemaVersion") != STATE_SCHEMA_VERSION
                    or header.get("source") != expected_source
                ):
                    raise ExternalStateError(f"{expected_source}: invalid archive header")

                while True:
                    line = text.readline()
                    if not line:
                        raise ExternalStateError(f"{expected_source}: archive footer is missing")
                    item = json.loads(line)
                    if item.get("recordType") == "footer":
                        footer = item
                        break
                    if item.get("recordType") != "station":
                        raise ExternalStateError(f"{expected_source}: unexpected JSONL record")
                    station = item.get("station")
                    if not isinstance(station, dict) or not station.get("name"):
                        raise ExternalStateError(f"{expected_source}: invalid staged station")
                    streams = station.get("streams")
                    if not isinstance(streams, list) or not streams:
                        raise ExternalStateError(f"{expected_source}: station has no streams")

                    station_key = f"{station_count:012d}"
                    station_json = _canonical_station_bytes(station).decode("utf-8")
                    record_digest.update(station_json.encode("utf-8") + b"\n")
                    provenance_status = _station_provenance_status(station, expected_source)
                    connection.execute(
                        "INSERT INTO staged_stations VALUES (?, ?, ?, ?, ?)",
                        (expected_source, station_key, station_count, station_json, provenance_status),
                    )

                    for stream_index, stream in enumerate(streams):
                        if not isinstance(stream, dict) or not _valid_http_url(stream.get("url")):
                            raise ExternalStateError(f"{expected_source}: invalid staged stream URL")
                        normalized_url = _normalized_url(stream["url"])
                        url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
                        source_records = stream.get("sourceRecords") or [None]
                        for record_index, source_record in enumerate(source_records):
                            source_record = source_record if isinstance(source_record, dict) else {}
                            source_record_id = (
                                source_record.get("providerRecordId")
                                or source_record.get("providerStationUuid")
                                or source_record.get("id")
                            )
                            connection.execute(
                                """INSERT INTO staged_streams
                                (provider, station_key, stream_index, record_index, url_hash,
                                 normalized_url, codec, bitrate, source_record_id, playlist_path,
                                 entry_index, provenance_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    expected_source, station_key, stream_index, record_index,
                                    url_hash, normalized_url, stream.get("codec"),
                                    stream.get("bitrate"), str(source_record_id) if source_record_id else None,
                                    source_record.get("playlistPath"),
                                    source_record.get("entryIndex"), provenance_status,
                                ),
                            )
                        stream_count += 1
                    station_count += 1

                if text.readline().strip():
                    raise ExternalStateError(f"{expected_source}: trailing data after footer")
                if footer.get("stationCount") != station_count:
                    raise ExternalStateError(f"{expected_source}: footer station count mismatch")
                if footer.get("streamCount") != stream_count:
                    raise ExternalStateError(f"{expected_source}: footer stream count mismatch")
                if footer.get("recordsSha256") != record_digest.hexdigest():
                    raise ExternalStateError(f"{expected_source}: station-record checksum mismatch")
                if header.get("stationCount") != station_count or header.get("streamCount") != stream_count:
                    raise ExternalStateError(f"{expected_source}: header count mismatch")
                if expected_station_count is not None and station_count != expected_station_count:
                    raise ExternalStateError(f"{expected_source}: manifest station count mismatch")
                if expected_stream_count is not None and stream_count != expected_stream_count:
                    raise ExternalStateError(f"{expected_source}: manifest stream count mismatch")
            finally:
                text.close()
        connection.commit()
        return {
            "stationCount": station_count,
            "streamCount": stream_count,
            "snapshotUpdatedAt": header.get("snapshotUpdatedAt"),
            "anomalyBounds": header.get("anomalyBounds") or {},
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _s3_context():
    bucket = os.environ.get("RADSYNC_S3_BUCKET", "").strip()
    if not bucket:
        raise ExternalStateError("RADSYNC_S3_BUCKET is not configured")
    endpoint = os.environ.get("RADSYNC_S3_ENDPOINT", "").strip() or None
    region = os.environ.get("RADSYNC_S3_REGION", "").strip() or "us-east-1"
    access_key = os.environ.get("RADSYNC_S3_ACCESS_KEY_ID", "").strip() or None
    secret_key = os.environ.get("RADSYNC_S3_SECRET_ACCESS_KEY", "").strip() or None
    if bool(access_key) != bool(secret_key):
        raise ExternalStateError("Both S3 access key and secret key must be configured")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    configured_prefix = os.environ.get("RADSYNC_S3_PREFIX", "").strip("/ ")
    prefix = configured_prefix or "radsync/state"
    return client, bucket, prefix


def _object_exists(client, bucket, key, digest, size):
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return False
        raise
    metadata = response.get("Metadata") or {}
    if metadata.get("sha256") != digest or response.get("ContentLength") != size:
        raise ExternalStateError(f"Immutable object key already exists with different content: {key}")
    return True


def _upload_immutable(client, bucket, key, file_path, digest):
    file_path = Path(file_path)
    size = file_path.stat().st_size
    if _object_exists(client, bucket, key, digest, size):
        return size
    with file_path.open("rb") as body:
        client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentLength=size,
            Metadata={"sha256": digest},
        )
    if not _object_exists(client, bucket, key, digest, size):
        raise ExternalStateError(f"Uploaded object could not be verified: {key}")
    return size


def _download_verified(client, bucket, key, destination, expected_digest):
    client.download_file(bucket, key, str(destination))
    actual = _sha256_file(destination)
    if actual != expected_digest:
        Path(destination).unlink(missing_ok=True)
        raise ExternalStateError(f"Downloaded object checksum mismatch: {key}")
    return Path(destination).stat().st_size


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _config_by_source(config):
    mapping = {
        "radio-browser": "radio_browser",
        "airwavedb": "airwavedb",
        "icecast": "icecast",
        "iptv-org-radio": "iptv_org_radio",
        "iptv-org-streams": "iptv_org_streams",
        "pulham-hq": "pulham_hq",
        "iprd": "iprd",
        "alon-drilich": "alon_drilich",
        "somafm": "somafm",
        "bbc-radio": "bbc_radio",
    }
    return {
        source_id: config.get(config_key, {})
        for source_id, config_key in mapping.items()
    }


def _validate_counts(source, station_count, stream_count, bounds, baseline=None):
    bounds = bounds or {}
    min_stations = int(bounds.get("minStationCount", 1))
    min_streams = int(bounds.get("minStreamCount", 1))
    if station_count < min_stations:
        raise ExternalStateError(
            f"{source}: station count {station_count} is below minimum {min_stations}"
        )
    if stream_count < min_streams:
        raise ExternalStateError(
            f"{source}: stream count {stream_count} is below minimum {min_streams}"
        )
    if baseline:
        max_station_drop = bounds.get("maxStationDropRatio")
        max_stream_drop = bounds.get("maxStreamDropRatio")
        old_stations = int(baseline.get("stationCount") or 0)
        old_streams = int(baseline.get("streamCount") or 0)
        if old_stations and max_station_drop is not None:
            drop = max(0.0, (old_stations - station_count) / old_stations)
            if drop > float(max_station_drop):
                raise ExternalStateError(
                    f"{source}: station count dropped by {drop:.1%}, above {float(max_station_drop):.1%}"
                )
        if old_streams and max_stream_drop is not None:
            drop = max(0.0, (old_streams - stream_count) / old_streams)
            if drop > float(max_stream_drop):
                raise ExternalStateError(
                    f"{source}: stream count dropped by {drop:.1%}, above {float(max_stream_drop):.1%}"
                )


def _promote_staged_snapshot(database_file, state_dir, source, metadata):
    connection = sqlite3.connect(database_file)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM staged_stations WHERE provider = ?", (source,)
        ).fetchone()[0]
        stream_count = connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT provider, station_key, stream_index "
            "FROM staged_streams WHERE provider = ?)", (source,)
        ).fetchone()[0]
        if count != metadata["stationCount"] or stream_count != metadata["streamCount"]:
            raise ExternalStateError(f"{source}: staged SQLite counts changed before promotion")

        destination = snapshot_path(state_dir, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.external.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            output.write('{"schemaVersion":')
            output.write(str(STATE_SCHEMA_VERSION))
            output.write(',"source":')
            output.write(json.dumps(source))
            output.write(',"updatedAt":')
            output.write(json.dumps(metadata.get("snapshotUpdatedAt") or _utc_now()))
            output.write(',"stationCount":')
            output.write(str(count))
            output.write(',"stations":[')
            first = True
            cursor = connection.execute(
                "SELECT station_json FROM staged_stations WHERE provider = ? ORDER BY station_index",
                (source,),
            )
            for (station_json,) in cursor:
                if not first:
                    output.write(",")
                output.write(station_json)
                first = False
            output.write("]}")
        temporary.replace(destination)
    finally:
        connection.close()


def restore_external_snapshots(state_dir, config_path="config/sources.json"):
    state_dir = Path(state_dir)
    manifest_dir = state_dir / "manifests" / "providers"
    manifests = sorted(manifest_dir.glob("*.json")) if manifest_dir.exists() else []
    if not manifests:
        print("No external provider manifests found; existing last-good state is unchanged.")
        return {"restored": 0, "legacy": True}

    config = _load_json(config_path, {})
    provider_config = _config_by_source(config)
    client, bucket, prefix = _s3_context()

    with tempfile.TemporaryDirectory(prefix="radsync-restore-") as temporary:
        database_file = Path(temporary) / "staging.sqlite3"
        staged = []
        for manifest_path in manifests:
            manifest = _load_json(manifest_path)
            source = _safe_source_id(manifest.get("source"))
            if (
                manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION
                or manifest.get("format") != "radsync-jsonl-zstd-v1"
            ):
                raise ExternalStateError(f"{manifest_path}: unsupported external manifest")
            key = manifest.get("objectKey")
            digest = manifest.get("sha256")
            if not key or not digest:
                raise ExternalStateError(f"{manifest_path}: missing object key or checksum")
            if not key.startswith(prefix + "/"):
                raise ExternalStateError(f"{manifest_path}: object key is outside configured prefix")
            archive_file = Path(temporary) / f"{source}.jsonl.zst"
            _download_verified(client, bucket, key, archive_file, digest)
            metadata = _stage_archive(
                archive_file, database_file, source,
                expected_digest=digest,
                expected_station_count=int(manifest["stationCount"]),
                expected_stream_count=int(manifest["streamCount"]),
            )
            _validate_counts(
                source, metadata["stationCount"], metadata["streamCount"],
                provider_config.get(source, {}).get("anomalyBounds"),
            )
            staged.append((source, metadata))

        # No last-good file is promoted until every external object has passed
        # checksum, footer, schema, provenance staging, and count validation.
        for source, metadata in staged:
            _promote_staged_snapshot(database_file, state_dir, source, metadata)

    return {"restored": len(staged), "legacy": False}


def _compress_report(report_path, output_path):
    compressor = zstandard.ZstdCompressor(level=8)
    with Path(report_path).open("rb") as source, Path(output_path).open("wb") as destination:
        with compressor.stream_writer(destination, closefd=False) as writer:
            shutil.copyfileobj(source, writer, length=1024 * 1024)


def publish_external_snapshots(state_dir, config_path="config/sources.json",
                                report_path=None, run_id=None):
    state_dir = Path(state_dir)
    config = _load_json(config_path, {})
    provider_config = _config_by_source(config)
    client, bucket, prefix = _s3_context()
    manifest_dir = state_dir / "manifests" / "providers"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    previous_manifests = {
        path.stem: _load_json(path, {})
        for path in manifest_dir.glob("*.json")
    }
    snapshot_files = sorted((state_dir / "sources").glob("*.json"))
    if not snapshot_files:
        raise ExternalStateError("No local provider snapshots are available to publish")

    new_manifests = []
    with tempfile.TemporaryDirectory(prefix="radsync-publish-") as temporary:
        database_file = Path(temporary) / "staging.sqlite3"
        for snapshot_file in snapshot_files:
            source = _safe_source_id(snapshot_file.stem)
            bounds = provider_config.get(source, {}).get("anomalyBounds") or {}
            metadata = _count_snapshot(snapshot_file, source)
            previous = previous_manifests.get(source)
            _validate_counts(
                source, metadata["stationCount"], metadata["streamCount"], bounds, previous
            )

            archive_file = Path(temporary) / f"{source}.jsonl.zst"
            counts = _create_archive(snapshot_file, archive_file, source, bounds)
            digest = _sha256_file(archive_file)
            key = f"{prefix}/providers/{source}/sha256/{digest}.jsonl.zst"
            _upload_immutable(client, bucket, key, archive_file, digest)

            verified_file = Path(temporary) / f"{source}.verified.jsonl.zst"
            _download_verified(client, bucket, key, verified_file, digest)
            verified_counts = _stage_archive(
                verified_file, database_file, source, expected_digest=digest,
                expected_station_count=counts["stationCount"],
                expected_stream_count=counts["streamCount"],
            )
            _validate_counts(
                source, verified_counts["stationCount"], verified_counts["streamCount"],
                bounds, previous,
            )
            new_manifests.append({
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "format": "radsync-jsonl-zstd-v1",
                "source": source,
                "objectKey": key,
                "sha256": digest,
                "compressedBytes": archive_file.stat().st_size,
                "stationCount": counts["stationCount"],
                "streamCount": counts["streamCount"],
                "snapshotUpdatedAt": counts["snapshotUpdatedAt"],
                "anomalyBounds": bounds,
                "verifiedAt": _utc_now(),
            })

        # Optional large refresh reports are also stored as immutable objects;
        # the uncompressed report is left in the worktree for Actions artifact upload.
        report_manifest = None
        if report_path and Path(report_path).is_file():
            run_id = str(run_id or os.environ.get("GITHUB_RUN_ID") or "manual")
            compressed_report = Path(temporary) / "source-report.json.zst"
            _compress_report(report_path, compressed_report)
            report_digest = _sha256_file(compressed_report)
            report_key = f"{prefix}/reports/{run_id}/sha256/{report_digest}.json.zst"
            _upload_immutable(client, bucket, report_key, compressed_report, report_digest)
            verified_report = Path(temporary) / "source-report.verified.json.zst"
            _download_verified(client, bucket, report_key, verified_report, report_digest)
            report_manifest = {
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "format": "json-zstd",
                "runId": run_id,
                "objectKey": report_key,
                "sha256": report_digest,
                "compressedBytes": compressed_report.stat().st_size,
                "verifiedAt": _utc_now(),
            }

    # Publish pointers only after every provider object has been downloaded and
    # verified. Existing local JSON files remain intact if any object fails.
    for manifest in new_manifests:
        _write_json_atomic(
            manifest_dir / f"{manifest['source']}.json", manifest
        )
    if report_manifest:
        report_manifest_path = state_dir / "manifests" / "reports" / f"{report_manifest['runId']}.json"
        _write_json_atomic(report_manifest_path, report_manifest)

    for snapshot_file in snapshot_files:
        snapshot_file.unlink()

    return {
        "providersPublished": len(new_manifests),
        "providerStations": sum(item["stationCount"] for item in new_manifests),
        "providerStreams": sum(item["streamCount"] for item in new_manifests),
        "reportPublished": bool(report_manifest),
    }
