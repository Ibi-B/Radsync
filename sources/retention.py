"""Retention-aware immutable object layout for Radsync provider state.

The Git state branch remains the authoritative pointer set. Object versions use
content-addressed keys beneath current/, previous/, and archive/. Promotion is
prepared and verified before the small Git manifests are advanced. Superseded
objects are removed only after the manifest commit has been pushed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ijson

from sources import external_state as base


LAYOUT_VERSION = 2


def _s3_context():
    client, bucket, prefix = base._s3_context()
    diagnostics = os.environ.get("RADSYNC_DIAGNOSTICS_PREFIX", "").strip("/ ")
    diagnostics_prefix = diagnostics or "radsync/diagnostics"
    if (
        diagnostics_prefix == prefix
        or diagnostics_prefix.startswith(prefix + "/")
        or prefix.startswith(diagnostics_prefix + "/")
    ):
        raise base.ExternalStateError("State and diagnostics prefixes must be separate")
    return client, bucket, prefix, diagnostics_prefix


def _descriptor(manifest, previous=False):
    if previous:
        value = manifest.get("previous")
        return value if isinstance(value, dict) else None
    if not isinstance(manifest, dict) or not manifest.get("objectKey"):
        return None
    return {
        "objectKey": manifest.get("objectKey"),
        "sha256": manifest.get("sha256"),
        "compressedBytes": manifest.get("compressedBytes"),
        "stationCount": manifest.get("stationCount"),
        "streamCount": manifest.get("streamCount"),
        "snapshotUpdatedAt": manifest.get("snapshotUpdatedAt"),
        "anomalyBounds": manifest.get("anomalyBounds") or {},
    }


def _validate_descriptor_key(key, prefix, source, role):
    if not isinstance(key, str):
        raise base.ExternalStateError(f"{source}: snapshot manifest has no object key")
    allowed = [f"{prefix}/{role}/{source}/"]
    if role == "current":
        # One-way compatibility for manifests written by PR #12.
        allowed.append(f"{prefix}/providers/{source}/")
    if not any(key.startswith(item) for item in allowed):
        raise base.ExternalStateError(
            f"{source}: {role} snapshot key is outside its permitted prefix"
        )


def _download_and_stage(client, bucket, prefix, source, descriptor, role, temp_dir):
    if not isinstance(descriptor, dict):
        raise base.ExternalStateError(f"{source}: missing {role} snapshot descriptor")
    key = descriptor.get("objectKey")
    digest = descriptor.get("sha256")
    _validate_descriptor_key(key, prefix, source, role)
    if not isinstance(digest, str) or len(digest) != 64:
        raise base.ExternalStateError(f"{source}: invalid {role} snapshot checksum")
    archive = Path(temp_dir) / f"{source}-{role}-{hashlib.sha256(key.encode()).hexdigest()[:8]}.jsonl.zst"
    database = Path(temp_dir) / f"{source}-{role}-{hashlib.sha256((key + digest).encode()).hexdigest()[:8]}.sqlite3"
    base._download_verified(client, bucket, key, archive, digest)
    metadata = base._stage_archive(
        archive,
        database,
        source,
        expected_digest=digest,
        expected_station_count=int(descriptor["stationCount"]),
        expected_stream_count=int(descriptor["streamCount"]),
    )
    return {
        "descriptor": dict(descriptor),
        "archive": archive,
        "database": database,
        "metadata": metadata,
        "role": role,
    }


def _verify_json(path):
    with Path(path).open("rb") as source:
        events = ijson.parse(source)
        first = next(events, None)
        if not first or first[0] != "" or first[1] not in {"start_map", "start_array"}:
            raise base.ExternalStateError(f"Diagnostic file is not valid JSON: {path}")
        for _ in events:
            pass


def _upload_verified_archive(client, bucket, key, archive, digest, source,
                             expected_station_count, expected_stream_count,
                             temp_dir, created_objects):
    size = Path(archive).stat().st_size
    exists = base._object_exists(client, bucket, key, digest, size)
    base._upload_immutable(client, bucket, key, archive, digest)
    if not exists:
        created_objects.append(key)
    verified = Path(temp_dir) / f"verify-{hashlib.sha256(key.encode()).hexdigest()[:12]}.jsonl.zst"
    base._download_verified(client, bucket, key, verified, digest)
    database = Path(temp_dir) / f"verify-{hashlib.sha256((key + digest).encode()).hexdigest()[:12]}.sqlite3"
    metadata = base._stage_archive(
        verified,
        database,
        source,
        expected_digest=digest,
        expected_station_count=expected_station_count,
        expected_stream_count=expected_stream_count,
    )
    return size, metadata


def _cleanup_uncommitted_objects(client, bucket, keys):
    for key in reversed(list(dict.fromkeys(keys))):
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception:
            # The object is not referenced by a committed manifest. If cleanup
            # is unavailable, leaving an unreferenced immutable object is safe.
            pass


def _archive_key(prefix, source, descriptor, timestamp, publication_id):
    digest = descriptor["sha256"]
    return f"{prefix}/archive/{source}/{timestamp}-{publication_id}-{digest}.jsonl.zst"


def _previous_key(prefix, source, descriptor, publication_id):
    digest = descriptor["sha256"]
    return f"{prefix}/previous/{source}/{publication_id}/sha256/{digest}/previous.jsonl.zst"


def _current_key(prefix, source, digest, publication_id):
    return f"{prefix}/current/{source}/{publication_id}/sha256/{digest}/active.jsonl.zst"


def _copy_descriptor_to_prefix(client, bucket, prefix, source, descriptor, role,
                                target_key, temp_dir, created_objects):
    downloaded = Path(descriptor["_localArchive"])
    digest = descriptor["sha256"]
    size, metadata = _upload_verified_archive(
        client, bucket, target_key, downloaded, digest, source,
        int(descriptor["stationCount"]), int(descriptor["streamCount"]),
        temp_dir, created_objects,
    )
    result = dict(descriptor)
    result["objectKey"] = target_key
    result["compressedBytes"] = size
    result["stationCount"] = metadata["stationCount"]
    result["streamCount"] = metadata["streamCount"]
    result["snapshotUpdatedAt"] = metadata.get("snapshotUpdatedAt") or descriptor.get("snapshotUpdatedAt")
    result["anomalyBounds"] = metadata.get("anomalyBounds") or descriptor.get("anomalyBounds") or {}
    return result


def _manifest_from_snapshot(source, key, digest, size, counts, bounds):
    return {
        "schemaVersion": base.MANIFEST_SCHEMA_VERSION,
        "layoutVersion": LAYOUT_VERSION,
        "format": "radsync-jsonl-zstd-v1",
        "source": source,
        "objectKey": key,
        "sha256": digest,
        "compressedBytes": size,
        "stationCount": counts["stationCount"],
        "streamCount": counts["streamCount"],
        "snapshotUpdatedAt": counts.get("snapshotUpdatedAt"),
        "anomalyBounds": bounds,
        "verifiedAt": base._utc_now(),
    }


def publish_external_snapshots(state_dir, config_path="config/sources.json",
                               report_path=None, run_id=None, ambiguity_path=None):
    state_dir = Path(state_dir)
    config = base._load_json(config_path, {})
    provider_config = base._config_by_source(config)
    client, bucket, prefix, diagnostics_prefix = _s3_context()
    manifest_dir = state_dir / "manifests" / "providers"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    previous_manifests = {
        path.stem: base._load_json(path, {})
        for path in manifest_dir.glob("*.json")
    }
    snapshot_files = sorted((state_dir / "sources").glob("*.json"))
    if not snapshot_files:
        raise base.ExternalStateError("No local provider snapshots are available to publish")

    run_id = base._safe_source_id(str(run_id or os.environ.get("GITHUB_RUN_ID") or "manual"))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    # Unique publication namespace prevents a failed concurrent run from
    # deleting a content-identical object already referenced by another run.
    publication_id = f"{run_id}-{uuid.uuid4().hex[:12]}"
    created_objects = []
    new_manifests = []
    report_manifest = None
    ambiguity_manifest = None

    try:
        with tempfile.TemporaryDirectory(prefix="radsync-retention-publish-") as temporary:
            for snapshot_file in snapshot_files:
                source = base._safe_source_id(snapshot_file.stem)
                bounds = provider_config.get(source, {}).get("anomalyBounds") or {}
                counts = base._count_snapshot(snapshot_file, source)
                prior = previous_manifests.get(source)
                old_current = _descriptor(prior) if prior else None
                old_previous = _descriptor(prior, previous=True) if prior else None

                candidate_archive = Path(temporary) / f"{source}-candidate.jsonl.zst"
                candidate_counts = base._create_archive(snapshot_file, candidate_archive, source, bounds)
                candidate_digest = base._sha256_file(candidate_archive)
                current_key = _current_key(prefix, source, candidate_digest, publication_id)
                candidate_db = Path(temporary) / f"{source}-candidate.sqlite3"
                candidate_verified = base._stage_archive(
                    candidate_archive, candidate_db, source,
                    expected_digest=candidate_digest,
                    expected_station_count=candidate_counts["stationCount"],
                    expected_stream_count=candidate_counts["streamCount"],
                )
                if candidate_verified["stationCount"] != counts["stationCount"] or candidate_verified["streamCount"] != counts["streamCount"]:
                    raise base.ExternalStateError(f"{source}: local candidate counts changed during staging")

                current_verified = None
                previous_verified = None
                current_error = None
                previous_error = None
                if old_current:
                    try:
                        current_verified = _download_and_stage(
                            client, bucket, prefix, source, old_current, "current", temporary
                        )
                    except Exception as error:
                        current_error = error
                if old_previous:
                    try:
                        previous_verified = _download_and_stage(
                            client, bucket, prefix, source, old_previous, "previous", temporary
                        )
                    except Exception as error:
                        previous_error = error

                if prior and not current_verified and not previous_verified:
                    raise base.ExternalStateError(
                        f"{source}: neither current nor previous external snapshot verified; "
                        f"current={current_error!r}; previous={previous_error!r}"
                    )

                baseline = old_current if current_verified else old_previous if previous_verified else None
                base._validate_counts(
                    source, counts["stationCount"], counts["streamCount"], bounds, baseline
                )

                # Select the last-good object to retain as previous. If current
                # is corrupt or missing, fall back to the already verified previous.
                retained_source = current_verified or previous_verified
                new_previous = None
                if retained_source:
                    retained_descriptor = dict(retained_source["descriptor"])
                    retained_descriptor["_localArchive"] = str(retained_source["archive"])
                    if current_verified:
                        previous_key = _previous_key(prefix, source, retained_descriptor, publication_id)
                        new_previous = _copy_descriptor_to_prefix(
                            client, bucket, prefix, source, retained_descriptor,
                            "previous", previous_key, temporary, created_objects,
                        )
                    else:
                        # Existing previous is already under the protected prefix.
                        new_previous = dict(retained_source["descriptor"])

                # The older previous is archived only when the current snapshot
                # was independently verified and is taking its place.
                archived_previous_key = None
                if current_verified and previous_verified:
                    previous_descriptor = dict(previous_verified["descriptor"])
                    previous_descriptor["_localArchive"] = str(previous_verified["archive"])
                    archived_previous_key = _archive_key(
                        prefix, source, previous_descriptor, stamp, publication_id
                    )
                    _copy_descriptor_to_prefix(
                        client, bucket, prefix, source, previous_descriptor,
                        "archive", archived_previous_key, temporary, created_objects,
                    )

                current_size, current_metadata = _upload_verified_archive(
                    client, bucket, current_key, candidate_archive, candidate_digest,
                    source, candidate_counts["stationCount"], candidate_counts["streamCount"],
                    temporary, created_objects,
                )
                manifest = _manifest_from_snapshot(
                    source, current_key, candidate_digest, current_size,
                    current_metadata, bounds,
                )
                if new_previous:
                    manifest["previous"] = {
                        key: value for key, value in new_previous.items()
                        if not key.startswith("_")
                    }

                pending_cleanup = list((prior or {}).get("rotationCleanup") or [])
                cleanup_items = []
                if old_current:
                    replacement = new_previous.get("objectKey") if new_previous else current_key
                    if old_current.get("objectKey") not in {replacement, current_key}:
                        cleanup_items.append({
                            "deleteKey": old_current["objectKey"],
                            "replacementKey": replacement,
                            "replacementRole": "previous" if new_previous else "current",
                            "sha256": (new_previous or {}).get("sha256") if new_previous else candidate_digest,
                        })
                if old_previous and old_previous.get("objectKey") != (new_previous or {}).get("objectKey"):
                    replacement = archived_previous_key or (new_previous or {}).get("objectKey")
                    if replacement:
                        cleanup_items.append({
                            "deleteKey": old_previous["objectKey"],
                            "replacementKey": replacement,
                            "replacementRole": "archive" if archived_previous_key else "previous",
                            "sha256": (new_previous or {}).get("sha256") if not archived_previous_key else old_previous.get("sha256"),
                        })
                # Keep any unfinalized cleanup from earlier successful runs.
                combined = []
                seen_cleanup = set()
                for item in pending_cleanup + cleanup_items:
                    identity = (item.get("deleteKey"), item.get("replacementKey"))
                    if identity not in seen_cleanup:
                        combined.append(item)
                        seen_cleanup.add(identity)
                if combined:
                    manifest["rotationCleanup"] = combined
                new_manifests.append(manifest)

            # Diagnostics live outside state/, with their own lifecycle prefix.
            if report_path and Path(report_path).is_file():
                _verify_json(report_path)
                base._compress_report(report_path, Path(temporary) / "refresh-report.json.zst")
                compressed = Path(temporary) / "refresh-report.json.zst"
                digest = base._sha256_file(compressed)
                key = f"{diagnostics_prefix}/refresh-report-{publication_id}-{digest}.json.zst"
                size, _ = _upload_verified_diagnostic(client, bucket, key, compressed, digest, temporary, created_objects)
                report_manifest = {
                    "schemaVersion": base.MANIFEST_SCHEMA_VERSION,
                    "format": "json-zstd",
                    "runId": run_id,
                    "objectKey": key,
                    "sha256": digest,
                    "compressedBytes": size,
                    "verifiedAt": base._utc_now(),
                }

            if ambiguity_path and Path(ambiguity_path).is_file():
                _verify_json(ambiguity_path)
                base._compress_report(ambiguity_path, Path(temporary) / "identity-ambiguity.json.zst")
                compressed = Path(temporary) / "identity-ambiguity.json.zst"
                digest = base._sha256_file(compressed)
                key = f"{diagnostics_prefix}/identity-ambiguity-{publication_id}-{digest}.json.zst"
                size, _ = _upload_verified_diagnostic(client, bucket, key, compressed, digest, temporary, created_objects)
                ambiguity_manifest = {
                    "schemaVersion": base.MANIFEST_SCHEMA_VERSION,
                    "format": "json-zstd",
                    "runId": run_id,
                    "kind": "identity-ambiguity",
                    "objectKey": key,
                    "sha256": digest,
                    "compressedBytes": size,
                    "verifiedAt": base._utc_now(),
                }

        # Update local pointers only after every provider and diagnostic object
        # has been uploaded, downloaded, checksum checked, and semantically staged.
        for manifest in new_manifests:
            base._write_json_atomic(manifest_dir / f"{manifest['source']}.json", manifest)
        if report_manifest:
            base._write_json_atomic(
                state_dir / "manifests" / "reports" / f"{run_id}.json", report_manifest
            )
        if ambiguity_manifest:
            base._write_json_atomic(
                state_dir / "manifests" / "diagnostics" / f"{run_id}.json", ambiguity_manifest
            )
        for snapshot_file in snapshot_files:
            snapshot_file.unlink()

    except Exception:
        # New immutable objects not referenced by committed state are removed
        # best-effort. Existing active/previous keys are never part of this list.
        _cleanup_uncommitted_objects(client, bucket, created_objects)
        raise

    return {
        "providersPublished": len(new_manifests),
        "providerStations": sum(item["stationCount"] for item in new_manifests),
        "providerStreams": sum(item["streamCount"] for item in new_manifests),
        "reportPublished": bool(report_manifest),
        "ambiguityPublished": bool(ambiguity_manifest),
        "layoutVersion": LAYOUT_VERSION,
    }


def _upload_verified_diagnostic(client, bucket, key, compressed, digest, temp_dir, created_objects):
    size = Path(compressed).stat().st_size
    exists = base._object_exists(client, bucket, key, digest, size)
    base._upload_immutable(client, bucket, key, compressed, digest)
    if not exists:
        created_objects.append(key)
    verified = Path(temp_dir) / f"diagnostic-{hashlib.sha256(key.encode()).hexdigest()[:12]}.zst"
    base._download_verified(client, bucket, key, verified, digest)
    # A full Zstandard read and JSON parse catch truncated or malformed
    # diagnostics before the manifest pointer is stored.
    expanded = Path(temp_dir) / f"diagnostic-{hashlib.sha256(key.encode()).hexdigest()[:12]}.json"
    with verified.open("rb") as source, expanded.open("wb") as target:
        reader = base.zstandard.ZstdDecompressor().stream_reader(source)
        try:
            shutil.copyfileobj(reader, target, length=1024 * 1024)
        finally:
            reader.close()
    _verify_json(expanded)
    return size, verified


def restore_external_snapshots(state_dir, config_path="config/sources.json"):
    state_dir = Path(state_dir)
    manifest_dir = state_dir / "manifests" / "providers"
    manifests = sorted(manifest_dir.glob("*.json")) if manifest_dir.exists() else []
    if not manifests:
        print("No external provider manifests found; existing last-good state is unchanged.")
        return {"restored": 0, "legacy": True, "fallbackProviders": []}

    config = base._load_json(config_path, {})
    provider_config = base._config_by_source(config)
    client, bucket, prefix, _ = _s3_context()

    with tempfile.TemporaryDirectory(prefix="radsync-retention-restore-") as temporary:
        staged = []
        fallback_providers = []
        for manifest_path in manifests:
            manifest = base._load_json(manifest_path)
            source = base._safe_source_id(manifest.get("source"))
            if (
                manifest.get("schemaVersion") != base.MANIFEST_SCHEMA_VERSION
                or manifest.get("format") != "radsync-jsonl-zstd-v1"
            ):
                raise base.ExternalStateError(f"{manifest_path}: unsupported external manifest")

            candidates = [
                ("current", _descriptor(manifest)),
                ("previous", _descriptor(manifest, previous=True)),
            ]
            errors = []
            restored_candidate = None
            for role, descriptor in candidates:
                if not descriptor:
                    continue
                candidate_dir = Path(temporary) / f"{source}-{role}"
                candidate_dir.mkdir(parents=True, exist_ok=True)
                try:
                    candidate = _download_and_stage(
                        client, bucket, prefix, source, descriptor, role, candidate_dir
                    )
                    base._validate_counts(
                        source,
                        candidate["metadata"]["stationCount"],
                        candidate["metadata"]["streamCount"],
                        provider_config.get(source, {}).get("anomalyBounds"),
                    )
                    restored_candidate = candidate
                    break
                except Exception as error:
                    errors.append(f"{role}: {type(error).__name__}: {error}")

            if not restored_candidate:
                raise base.ExternalStateError(
                    f"{source}: current and previous snapshots are unavailable or invalid; "
                    + "; ".join(errors)
                )
            if restored_candidate["role"] == "previous":
                fallback_providers.append(source)
            staged.append((source, restored_candidate))

        # Promote only after every provider has a fully verified current or
        # previous version. A failed provider leaves all local last-good files intact.
        for source, candidate in staged:
            base._promote_staged_snapshot(
                candidate["database"], state_dir, source, candidate["metadata"]
            )

    return {
        "restored": len(staged),
        "legacy": False,
        "fallbackProviders": fallback_providers,
    }


def fetch_external_report(state_dir, run_id, output_path):
    run_id = base._safe_source_id(str(run_id))
    manifest_path = Path(state_dir) / "manifests" / "reports" / f"{run_id}.json"
    manifest = base._load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise base.ExternalStateError(f"External report manifest is missing for run {run_id}")
    if (
        manifest.get("schemaVersion") != base.MANIFEST_SCHEMA_VERSION
        or manifest.get("format") != "json-zstd"
        or str(manifest.get("runId")) != run_id
    ):
        raise base.ExternalStateError(f"External report manifest is invalid for run {run_id}")

    client, bucket, prefix, diagnostics_prefix = _s3_context()
    key = manifest.get("objectKey")
    digest = manifest.get("sha256")
    legacy_prefix = f"{prefix}/reports/"
    if not key or not digest or not (
        key.startswith(diagnostics_prefix + "/refresh-report-")
        or key.startswith(legacy_prefix)
    ):
        raise base.ExternalStateError("External report manifest has an invalid diagnostic pointer")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(output_path.name + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="radsync-report-") as temporary:
            compressed = Path(temporary) / "source-report.json.zst"
            base._download_verified(client, bucket, key, compressed, digest)
            with compressed.open("rb") as source, temporary_output.open("wb") as target:
                reader = base.zstandard.ZstdDecompressor().stream_reader(source)
                try:
                    shutil.copyfileobj(reader, target, length=1024 * 1024)
                finally:
                    reader.close()
        _verify_json(temporary_output)
        temporary_output.replace(output_path)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise

    return {
        "runId": run_id,
        "output": str(output_path),
        "sha256": digest,
        "compressedBytes": manifest.get("compressedBytes"),
    }


def finalize_external_rotation(state_dir, *, manifest_pushed=False):
    """Delete superseded objects only after the caller confirms pointers were pushed."""
    if not manifest_pushed:
        raise base.ExternalStateError(
            "Rotation finalization requires confirmation that the manifest commit was pushed"
        )
    state_dir = Path(state_dir)
    manifest_dir = state_dir / "manifests" / "providers"
    manifests = sorted(manifest_dir.glob("*.json")) if manifest_dir.exists() else []
    pending = []
    for path in manifests:
        manifest = base._load_json(path, {})
        for item in manifest.get("rotationCleanup") or []:
            if isinstance(item, dict) and item.get("deleteKey") and item.get("replacementKey"):
                pending.append((path, manifest, item))
    if not pending:
        return {"finalized": 0, "manifestsUpdated": 0, "pending": 0}

    client, bucket, prefix, _ = _s3_context()
    grouped = {}
    finalized = 0
    for path, manifest, item in pending:
        source = base._safe_source_id(manifest.get("source"))
        delete_key = item["deleteKey"]
        replacement_key = item["replacementKey"]
        replacement_role = item.get("replacementRole")
        digest = item.get("sha256")
        protected = {manifest.get("objectKey")}
        previous = manifest.get("previous") or {}
        protected.add(previous.get("objectKey"))
        if delete_key in protected:
            continue
        allowed_delete = (
            delete_key.startswith(f"{prefix}/current/{source}/")
            or delete_key.startswith(f"{prefix}/previous/{source}/")
            or delete_key.startswith(f"{prefix}/providers/{source}/")
        )
        if not allowed_delete:
            raise base.ExternalStateError(f"{source}: refusing to delete outside snapshot prefixes")
        if replacement_role == "previous" and replacement_key != previous.get("objectKey"):
            raise base.ExternalStateError(f"{source}: cleanup replacement is not the committed previous snapshot")
        if replacement_role == "current" and replacement_key != manifest.get("objectKey"):
            raise base.ExternalStateError(f"{source}: cleanup replacement is not the committed current snapshot")
        if replacement_role == "archive" and not replacement_key.startswith(f"{prefix}/archive/{source}/"):
            raise base.ExternalStateError(f"{source}: archive replacement is outside archive prefix")
        if replacement_role not in {"previous", "current", "archive"}:
            raise base.ExternalStateError(f"{source}: unknown rotation cleanup role")

        # Confirm the replacement still exists with the expected SHA before
        # deleting the superseded object. HEAD metadata is only a quick check:
        # re-download and hash the actual bytes so stale metadata cannot mask
        # object corruption between promotion and finalization.
        replacement_response = client.head_object(Bucket=bucket, Key=replacement_key)
        replacement_metadata = replacement_response.get("Metadata") or {}
        if digest and replacement_metadata.get("sha256") != digest:
            raise base.ExternalStateError(f"{source}: replacement checksum metadata mismatch")
        with tempfile.TemporaryDirectory(prefix="radsync-finalize-") as temporary:
            verification_file = Path(temporary) / "replacement.jsonl.zst"
            base._download_verified(
                client, bucket, replacement_key, verification_file, digest
            )
        try:
            client.delete_object(Bucket=bucket, Key=delete_key)
        except Exception as error:
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"404", "NoSuchKey", "NotFound"} and status != 404:
                raise
        finalized += 1
        grouped.setdefault(path, (manifest, []))[1].append(item)

    updated = 0
    for path, (manifest, completed_items) in grouped.items():
        completed_ids = {
            (item["deleteKey"], item["replacementKey"])
            for item in completed_items
        }
        remaining = [
            item for item in (manifest.get("rotationCleanup") or [])
            if (item.get("deleteKey"), item.get("replacementKey")) not in completed_ids
        ]
        if remaining:
            manifest["rotationCleanup"] = remaining
        else:
            manifest.pop("rotationCleanup", None)
        base._write_json_atomic(path, manifest)
        updated += 1

    return {"finalized": finalized, "manifestsUpdated": updated, "pending": len(pending) - finalized}


def prune_external_archive_objects(older_than_days=90, apply=False):
    """Prune old objects from archive/ only; current/ and previous/ are never listed."""
    if int(older_than_days) < 90:
        raise base.ExternalStateError("Archive pruning age cannot be less than 90 days")
    client, bucket, prefix, _ = _s3_context()
    archive_prefix = f"{prefix}/archive/"
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(older_than_days))
    eligible = []
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": archive_prefix}
        if token:
            args["ContinuationToken"] = token
        response = client.list_objects_v2(**args)
        for item in response.get("Contents") or []:
            key = item.get("Key")
            modified = item.get("LastModified")
            if key and key.startswith(archive_prefix) and modified and modified < cutoff:
                eligible.append(key)
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not token:
            raise base.ExternalStateError("Archive listing is truncated without a continuation token")

    if apply:
        for key in eligible:
            if not key.startswith(archive_prefix):
                raise base.ExternalStateError("Refusing to prune an object outside archive/")
            client.delete_object(Bucket=bucket, Key=key)
    return {"prefix": archive_prefix, "eligible": eligible, "deleted": eligible if apply else [], "apply": bool(apply)}
