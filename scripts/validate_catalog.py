#!/usr/bin/env python3
"""Validates the latest generated radio catalog before publication."""
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
    master_path = OUTPUT / f"v{version}" / "master-index.json"
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

    for group_type in ("genres", "countries", "languages"):
        group_file = OUTPUT / f"v{version}" / master["groups"][group_type]["url"]
        if not group_file.exists():
            print(f"Missing group index: {group_type}")
            return 1

    print(f"Validated catalog version {version} with {master['stationCount']} stations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
