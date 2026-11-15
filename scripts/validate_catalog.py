#!/usr/bin/env python3
"""Validates the published Radsync catalogue and its BeastED consumption contract."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

OUTPUT = Path("output")


def validate_catalog(output=OUTPUT):
    pointer_path = output / "current" / "master-index.json"
    if not pointer_path.exists():
        print("No current catalog pointer found")
        return 1

    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        version = pointer["version"]
        master_relative = pointer["masterIndex"]
        version_dir = output / f"v{version}"
        master_path = output / master_relative
        if not master_path.exists():
            print(f"Missing master index for version {version}")
            return 1

        master = json.loads(master_path.read_text(encoding="utf-8"))
        if master.get("version") != version:
            print("Catalog version mismatch")
            return 1
        if master.get("stationCount", 0) <= 0 or master.get("streamCount", 0) <= 0:
            print("Catalog contains no stations or playable streams")
            return 1

        stations_path = version_dir / master["stationsUrl"]
        if not stations_path.exists():
            print("Missing station index")
            return 1
        station_payload = json.loads(stations_path.read_text(encoding="utf-8"))
        stations = station_payload.get("stations", [])
        if station_payload.get("version") != version or len(stations) != master["stationCount"]:
            print("Station index version/count mismatch")
            return 1

        station_ids = set()
        actual_stream_count = 0
        for station in stations:
            station_id = station.get("stationUuid")
            if not station_id or station_id in station_ids:
                print("Missing or duplicate station identity")
                return 1
            station_ids.add(station_id)
            streams = station.get("streams", [])
            if not streams:
                print(f"Station has no streams: {station.get('name', station_id)}")
                return 1
            actual_stream_count += len(streams)
            for stream in streams:
                parsed = urlsplit(str(stream.get("url", "")))
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    print(f"Invalid stream URL for station {station_id}")
                    return 1
            default_index = station.get("defaultStreamIndex")
            if not isinstance(default_index, int) or not 0 <= default_index < len(streams):
                print(f"Invalid default stream for station {station_id}")
                return 1
            if default_index != 0 or station.get("defaultSource") != streams[0].get("source"):
                print(f"Default stream is not the highest-ranked stream for station {station_id}")
                return 1
            grouped_indexes = set()
            for group in station.get("sourceGroups", []):
                source = group.get("source")
                for index in group.get("streamIndexes", []):
                    if not isinstance(index, int) or not 0 <= index < len(streams):
                        print(f"Invalid source-group stream index for station {station_id}")
                        return 1
                    if source not in streams[index].get("sources", [streams[index].get("source")]):
                        print(f"Source group does not match stream provenance for station {station_id}")
                        return 1
                    grouped_indexes.add(index)
            if grouped_indexes != set(range(len(streams))):
                print(f"Source groups do not cover all streams for station {station_id}")
                return 1

        if actual_stream_count != master["streamCount"]:
            print("Master stream count does not match station index")
            return 1

        report_path = version_dir / "source-report.json"
        if not report_path.exists():
            print("Missing source report")
            return 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        successful = [
            source for source in report.get("sources", [])
            if source.get("status") in ("ok", "stale") and source.get("stationCount", 0) > 0
            and (source.get("status") != "stale" or source.get("snapshot") == "last-good")
        ]
        if not successful:
            print("No source produced stations")
            return 1

        for group_type in ("genres", "countries", "languages"):
            group_file = version_dir / master["groups"][group_type]["url"]
            if not group_file.exists():
                print(f"Missing group index: {group_type}")
                return 1

    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Invalid catalogue structure: {error}")
        return 1

    print(f"Validated version {version}: {master['stationCount']} stations, {master['streamCount']} streams")
    for source in report["sources"]:
        print(f"{source['source']}: {source['status']} ({source['stationCount']} stations)")
    return 0


def main():
    return validate_catalog()


if __name__ == "__main__":
    sys.exit(main())
