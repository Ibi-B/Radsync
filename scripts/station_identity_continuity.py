"""Observability-only comparison of station identities between catalogue releases."""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def _load_stations(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    stations = payload.get("stations", [])
    if not isinstance(stations, list):
        raise ValueError(f"Invalid station index: {path}")
    return stations


def _provider_ids(station):
    values = set()
    for stream in station.get("streams", []):
        source = str(stream.get("source") or "")
        ids = stream.get("sourceStationIds") or ([stream.get("sourceStationId")] if stream.get("sourceStationId") else [])
        for value in ids:
            if value:
                values.add(f"{source}:{value}")
    return values


def _name_key(station):
    return " ".join(str(station.get("normalizedName") or station.get("name") or "").casefold().split())


def _metadata_compatible(left, right):
    for field in ("countryCode", "language"):
        a = str(left.get(field) or "").strip().casefold()
        b = str(right.get(field) or "").strip().casefold()
        if a and b and a != b:
            return False
    return True


def _candidate_reason(previous, current):
    shared_ids = _provider_ids(previous) & _provider_ids(current)
    if shared_ids:
        return "provider-id", "high", sorted(shared_ids)
    if _name_key(previous) and _name_key(previous) == _name_key(current) and _metadata_compatible(previous, current):
        return "metadata-match", "low", []
    return None


def build_continuity_report(previous_stations, current_stations, previous_version=None, current_version=None):
    previous_by_id = {item.get("stationUuid"): item for item in previous_stations if item.get("stationUuid")}
    current_by_id = {item.get("stationUuid"): item for item in current_stations if item.get("stationUuid")}
    retained_ids = sorted(set(previous_by_id) & set(current_by_id))
    previous_remaining = [previous_by_id[key] for key in sorted(set(previous_by_id) - set(retained_ids))]
    current_remaining = [current_by_id[key] for key in sorted(set(current_by_id) - set(retained_ids))]

    candidates = []
    for old in previous_remaining:
        for new in current_remaining:
            reason = _candidate_reason(old, new)
            if reason:
                candidates.append((old, new, reason))

    old_degree = defaultdict(int)
    new_degree = defaultdict(int)
    for old, new, _ in candidates:
        old_degree[old["stationUuid"]] += 1
        new_degree[new["stationUuid"]] += 1

    changes = []
    matched_old = set()
    matched_new = set()
    for old, new, (reason, confidence, shared) in candidates:
        old_id, new_id = old["stationUuid"], new["stationUuid"]
        if old_degree[old_id] == 1 and new_degree[new_id] == 1:
            changes.append({
                "previousStationUuid": old_id,
                "currentStationUuid": new_id,
                "previousName": old.get("name"),
                "currentName": new.get("name"),
                "reason": reason,
                "confidence": confidence,
                "sharedProviderIds": shared,
                "automaticMatch": reason == "provider-id" and confidence == "high",
            })
            matched_old.add(old_id)
            matched_new.add(new_id)
        else:
            changes.append({
                "previousStationUuid": old_id,
                "currentStationUuid": new_id,
                "previousName": old.get("name"),
                "currentName": new.get("name"),
                "reason": reason,
                "confidence": "ambiguous",
                "sharedProviderIds": shared,
                "automaticMatch": False,
            })

    previous_unmatched = set(previous_by_id) - set(retained_ids) - matched_old
    current_unmatched = set(current_by_id) - set(retained_ids) - matched_new
    ambiguous_old = {item["previousStationUuid"] for item in changes if item["confidence"] == "ambiguous"}
    ambiguous_new = {item["currentStationUuid"] for item in changes if item["confidence"] == "ambiguous"}
    changes.sort(key=lambda item: (item["previousStationUuid"], item["currentStationUuid"]))

    return {
        "schemaVersion": 1,
        "previousVersion": previous_version,
        "currentVersion": current_version,
        "previousStationCount": len(previous_by_id),
        "currentStationCount": len(current_by_id),
        "retained": len(retained_ids),
        "changed": len(matched_old),
        "removed": len(previous_unmatched - ambiguous_old),
        "added": len(current_unmatched - ambiguous_new),
        "ambiguousPrevious": len(ambiguous_old),
        "ambiguousCurrent": len(ambiguous_new),
        "retainedStationUuids": retained_ids,
        "changes": changes,
        "removedStationUuids": sorted(previous_unmatched - ambiguous_old),
        "addedStationUuids": sorted(current_unmatched - ambiguous_new),
        "policy": "observability-only; no station IDs are rewritten",
    }


def compare_catalogues(previous_path, current_path, output_path):
    previous = _load_stations(previous_path)
    current = _load_stations(current_path)
    report = build_continuity_report(previous, current)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="Compare station identity continuity across catalogue releases")
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    compare_catalogues(args.previous, args.current, args.output)


if __name__ == "__main__":
    main()
