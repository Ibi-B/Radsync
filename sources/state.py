"""Durable per-provider last-good snapshots for scheduled catalogue runs."""
import json
import re
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
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if _valid_snapshot_payload(payload, source_id) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return payload


def validate_state(state_dir):
    root = Path(state_dir)
    if not root.exists():
        return True, []
    issues = []
    sources_dir = root / "sources"
    for path in sources_dir.glob("*.json") if sources_dir.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not _valid_snapshot_payload(payload, path.stem):
                issues.append(f"{path.name}: unsupported schema or invalid provider snapshot")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            issues.append(f"{path.name}: unreadable JSON")
    return not issues, issues
