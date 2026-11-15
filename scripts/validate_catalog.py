#!/usr/bin/env python3
"""Validates the latest generated multi-source radio catalog."""
import json
import sys
from pathlib import Path

OUTPUT = Path("output")


def main():
    pointer_path = OUTPUT / "current" / "master-index.json"
    if not pointer_path.exists():
        print("No current catalog pointer found")
        return 1

    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    version = pointer.get("version")
    version_dir = OUTPUT / f"v{version}"
    master_path = version_dir / "master-index.json"

    if not master_path.exists():
        print(f"Missing master index for version {version}")
        return 1

    master = json.loads(master_path.read_text(encoding="utf-8"))
    if master.get("version") != version:
        print("Catalog version mismatch")
        return 1
    if master.get("stationCount", 0) <= 0:
        print("Catalog contains no stations")
        return 1
    if master.get("streamCount", 0) <= 0:
        print("Catalog contains no playable streams")
        return 1

    report_path = version_dir / "source-report.json"
    if not report_path.exists():
        print("Missing source report")
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    successful = [source for source in report["sources"] if source["status"] == "ok"]
    if not successful:
        print("No source produced stations")
        return 1

    for group_type in ("genres", "countries", "languages"):
        group_file = version_dir / master["groups"][group_type]["url"]
        if not group_file.exists():
            print(f"Missing group index: {group_type}")
            return 1

    print(f"Validated version {version}: {master['stationCount']} stations, {master['streamCount']} streams")
    for source in report["sources"]:
        print(f"{source['source']}: {source['status']} ({source['stationCount']} stations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
