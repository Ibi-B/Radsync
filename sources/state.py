"""Durable per-provider last-good snapshots for scheduled catalogue runs."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


STATE_SCHEMA_VERSION = 1


def _safe_source_id(source_id):
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(source_id or "")).strip(".-")
    if not value or value != source_id:
        raise ValueError(f"Unsafe source identifier: {source_id!r}")
    return value


def snapshot_path(state_dir, source_id):
    return Path(state_dir) / "sources" / f"{_safe_source_id(source_id)}.json"


def load_snapshot(state_dir, source_id):
    path = snapshot_path(state_dir, source_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
            return None
        stations = payload.get("stations")
        if not isinstance(stations, list) or not stations:
            return None
        if not all(isinstance(item, dict) and item.get("name") and item.get("streams") for item in stations):
            return None
        return payload
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
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return payload


def validate_state(state_dir):
    root = Path(state_dir)
    if not root.exists():
        return True, []
    issues = []
    for path in (root / "sources").glob("*.json") if (root / "sources").exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
                issues.append(f"{path.name}: unsupported schema version")
                continue
            if not isinstance(payload.get("stations"), list) or not payload["stations"]:
                issues.append(f"{path.name}: empty or invalid stations")
                continue
            if payload.get("stationCount") != len(payload["stations"]):
                issues.append(f"{path.name}: station count mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            issues.append(f"{path.name}: unreadable JSON")
    return not issues, issues
