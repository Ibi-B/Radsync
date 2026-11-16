"""Durable per-provider last-good snapshots for scheduled catalogue runs."""
import json
import re

import ijson
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


STATE_SCHEMA_VERSION = 1


def _safe_source_id(source_id):
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(source_id or "")).strip(".-")
    if not value or value != source_id:
        raise ValueError(f"Unsafe source identifier: {source_id!r}")
    return value


def snapshot_path(state_dir, source_id):
    return Path(state_dir) / "sources" / f"{_safe_source_id(source_id)}.json"


def _valid_snapshot_payload(payload, expected_source=None):
    if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
        return False
    if expected_source is not None and payload.get("source") != expected_source:
        return False
    stations = payload.get("stations")
    if not isinstance(stations, list) or not stations:
        return False
    if payload.get("stationCount") != len(stations):
        return False
    for station in stations:
        if not isinstance(station, dict) or not station.get("name"):
            return False
        streams = station.get("streams")
        if not isinstance(streams, list) or not streams:
            return False
        for stream in streams:
            if not isinstance(stream, dict):
                return False
            try:
                parsed = urlsplit(str(stream.get("url") or "").strip())
            except ValueError:
                return False
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return False
            if not stream.get("source"):
                return False
    return True


def load_snapshot(state_dir, source_id):
    path = snapshot_path(state_dir, source_id)
    if not path.exists():
        return None
    try:
        metadata = {}
        with path.open("rb") as handle:
            for prefix, event, value in ijson.parse(handle):
                if prefix in {"schemaVersion", "source", "updatedAt", "stationCount"} and event in {"number", "string"}:
                    metadata[prefix] = value
        with path.open("rb") as handle:
            stations = list(ijson.items(handle, "stations.item", use_float=True))
        payload = {**metadata, "stations": stations}
        return payload if _valid_snapshot_payload(payload, source_id) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ijson.common.JSONError):
        return None


def save_snapshot(state_dir, source_id, stations):
    if not stations:
        raise ValueError("Refusing to replace a last-good snapshot with an empty result")
    path = snapshot_path(state_dir, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "source": source_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "stationCount": len(stations),
        "stations": stations,
    }
    if not _valid_snapshot_payload(payload, source_id):
        raise ValueError("Refusing to persist an invalid provider snapshot")
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)
    return payload


def snapshot_counts(state_dir, source_id):
    """Read station and stream counts without materializing a large JSON snapshot."""
    path = snapshot_path(state_dir, source_id)
    if not path.exists():
        return None
    metadata = {}
    with path.open("rb") as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix in {"schemaVersion", "source", "stationCount"} and event in {"number", "string"}:
                metadata[prefix] = value
    if metadata.get("schemaVersion") != STATE_SCHEMA_VERSION or metadata.get("source") != source_id:
        raise ValueError(f"{path.name}: unsupported schema or provider identity mismatch")
    stations = 0
    streams = 0
    with path.open("rb") as handle:
        for station in ijson.items(handle, "stations.item", use_float=True):
            if not isinstance(station, dict) or not station.get("name"):
                raise ValueError(f"{path.name}: invalid station record")
            station_streams = station.get("streams")
            if not isinstance(station_streams, list) or not station_streams:
                raise ValueError(f"{path.name}: station without streams")
            for stream in station_streams:
                if not isinstance(stream, dict):
                    raise ValueError(f"{path.name}: invalid stream record")
                try:
                    parsed = urlsplit(str(stream.get("url") or "").strip())
                except ValueError:
                    raise ValueError(f"{path.name}: invalid stream URL")
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise ValueError(f"{path.name}: invalid stream URL")
            stations += 1
            streams += len(station_streams)
    if metadata.get("stationCount") != stations:
        raise ValueError(f"{path.name}: station count does not match snapshot")
    return {"stationCount": stations, "streamCount": streams}


def validate_provider_anomaly_bounds(source_id, stations, bounds=None,
                                     previous_counts=None, playlist_refresh=None):
    """Reject unexpected station, stream, or playlist-production collapse."""
    bounds = bounds or {}
    station_count = len(stations)
    stream_count = sum(len(station.get("streams", [])) for station in stations)
    minimum_stations = int(bounds.get("minStationCount", 1))
    minimum_streams = int(bounds.get("minStreamCount", 1))
    if station_count < minimum_stations:
        raise ValueError(
            f"{source_id}: station count {station_count} is below minimum {minimum_stations}"
        )
    if stream_count < minimum_streams:
        raise ValueError(
            f"{source_id}: stream count {stream_count} is below minimum {minimum_streams}"
        )
    if previous_counts:
        old_stations = int(previous_counts.get("stationCount") or 0)
        old_streams = int(previous_counts.get("streamCount") or 0)
        max_station_drop = bounds.get("maxStationDropRatio")
        max_stream_drop = bounds.get("maxStreamDropRatio")
        if old_stations and max_station_drop is not None:
            drop = max(0.0, (old_stations - station_count) / old_stations)
            if drop > float(max_station_drop):
                raise ValueError(
                    f"{source_id}: station count dropped by {drop:.1%}, "
                    f"above {float(max_station_drop):.1%}"
                )
        if old_streams and max_stream_drop is not None:
            drop = max(0.0, (old_streams - stream_count) / old_streams)
            if drop > float(max_stream_drop):
                raise ValueError(
                    f"{source_id}: stream count dropped by {drop:.1%}, "
                    f"above {float(max_stream_drop):.1%}"
                )
    minimum_production = bounds.get("minRecordProductionCoverage")
    if minimum_production is not None and playlist_refresh is not None:
        coverage = playlist_refresh.get("recordProductionCoverageRatio")
        if coverage is None or float(coverage) < float(minimum_production):
            value = "missing" if coverage is None else f"{float(coverage):.1%}"
            raise ValueError(
                f"{source_id}: record-production coverage {value} is below "
                f"minimum {float(minimum_production):.1%}"
            )
    return {"stationCount": station_count, "streamCount": stream_count}


def _validate_snapshot_file(path, expected_source):
    metadata = {}
    with path.open("rb") as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix in {"schemaVersion", "source", "stationCount"} and event in {"number", "string"}:
                metadata[prefix] = value
    if metadata.get("schemaVersion") != STATE_SCHEMA_VERSION or metadata.get("source") != expected_source:
        return False
    count = 0
    with path.open("rb") as handle:
        for station in ijson.items(handle, "stations.item"):
            if not isinstance(station, dict) or not station.get("name"):
                return False
            streams = station.get("streams")
            if not isinstance(streams, list) or not streams:
                return False
            for stream in streams:
                if not isinstance(stream, dict):
                    return False
                try:
                    parsed = urlsplit(str(stream.get("url") or "").strip())
                except ValueError:
                    return False
                if parsed.scheme not in ("http", "https") or not parsed.netloc or not stream.get("source"):
                    return False
            count += 1
    return count > 0 and metadata.get("stationCount") == count


def validate_state(state_dir):
    root = Path(state_dir)
    if not root.exists():
        return True, []
    issues = []
    sources_dir = root / "sources"
    for path in sources_dir.glob("*.json") if sources_dir.exists() else []:
        try:
            if not _validate_snapshot_file(path, path.stem):
                issues.append(f"{path.name}: unsupported schema or invalid provider snapshot")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            issues.append(f"{path.name}: unreadable JSON")
    return not issues, issues
