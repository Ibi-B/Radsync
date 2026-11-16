import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_catalog import validate_catalog


class CatalogValidationTests(unittest.TestCase):
    def test_valid_catalogue_contract_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            version = 20261115220000000000
            version_dir = output / f"v{version}"
            for name in ("groups", "genres", "countries", "languages"):
                (version_dir / name).mkdir(parents=True, exist_ok=True)

            station = {
                "stationUuid": "radsync:example-radio:gb:english",
                "name": "Example Radio",
                "streams": [{
                    "source": "preferred",
                    "sources": ["preferred"],
                    "url": "https://radio.example/live",
                    "qualityScore": 90,
                }],
                "defaultStreamIndex": 0,
                "defaultSource": "preferred",
                "sourceGroups": [{"source": "preferred", "streamIndexes": [0]}],
            }
            (version_dir / "stations.json").write_text(
                json.dumps({"version": version, "stations": [station]}), encoding="utf-8"
            )
            master = {
                "version": version,
                "stationCount": 1,
                "streamCount": 1,
                "stationsUrl": "stations.json",
                "groups": {
                    key: {"url": f"groups/{key}.json"}
                    for key in ("genres", "countries", "languages")
                },
            }
            (version_dir / "master-index.json").write_text(json.dumps(master), encoding="utf-8")
            (version_dir / "source-report.json").write_text(json.dumps({
                "sources": [{"source": "preferred", "status": "ok", "stationCount": 1}]
            }), encoding="utf-8")
            for key in ("genres", "countries", "languages"):
                (version_dir / "groups" / f"{key}.json").write_text("{}", encoding="utf-8")
            current = output / "current"
            current.mkdir()
            (current / "master-index.json").write_text(json.dumps({
                "version": version,
                "masterIndex": f"v{version}/master-index.json",
            }), encoding="utf-8")

            self.assertEqual(0, validate_catalog(output))

    def test_duplicate_station_identity_fails_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            version = 20261115220000000001
            version_dir = output / f"v{version}"
            for name in ("groups", "genres", "countries", "languages"):
                (version_dir / name).mkdir(parents=True, exist_ok=True)
            station = {
                "stationUuid": "duplicate-id",
                "name": "Same Station",
                "streams": [{"source": "provider", "sources": ["provider"], "url": "https://radio.example/live"}],
                "defaultStreamIndex": 0,
                "defaultSource": "provider",
                "sourceGroups": [{"source": "provider", "streamIndexes": [0]}],
            }
            stations = [station, dict(station)]
            (version_dir / "stations.json").write_text(json.dumps({"version": version, "stations": stations}), encoding="utf-8")
            master = {
                "version": version, "stationCount": 2, "streamCount": 2, "stationsUrl": "stations.json",
                "groups": {key: {"url": f"groups/{key}.json"} for key in ("genres", "countries", "languages")},
            }
            (version_dir / "master-index.json").write_text(json.dumps(master), encoding="utf-8")
            (version_dir / "source-report.json").write_text(json.dumps({"sources": [{"source": "provider", "status": "ok", "stationCount": 2}]}), encoding="utf-8")
            for key in ("genres", "countries", "languages"):
                (version_dir / "groups" / f"{key}.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            current = output / "current"
            current.mkdir()
            (current / "master-index.json").write_text(json.dumps({"version": version, "masterIndex": f"v{version}/master-index.json"}), encoding="utf-8")
            self.assertEqual(1, validate_catalog(output))

    def test_group_index_duplicate_identity_fails_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            version = 20261115220000000002
            version_dir = output / f"v{version}"
            for name in ("groups", "genres", "countries", "languages"):
                (version_dir / name).mkdir(parents=True, exist_ok=True)
            station = {
                "stationUuid": "unique-id",
                "name": "Example Radio",
                "streams": [{"source": "provider", "sources": ["provider"], "url": "https://radio.example/live"}],
                "defaultStreamIndex": 0,
                "defaultSource": "provider",
                "sourceGroups": [{"source": "provider", "streamIndexes": [0]}],
            }
            (version_dir / "stations.json").write_text(json.dumps({"version": version, "stations": [station]}), encoding="utf-8")
            master = {
                "version": version, "stationCount": 1, "streamCount": 1, "stationsUrl": "stations.json",
                "groups": {key: {"url": f"groups/{key}.json"} for key in ("genres", "countries", "languages")},
            }
            (version_dir / "master-index.json").write_text(json.dumps(master), encoding="utf-8")
            (version_dir / "source-report.json").write_text(json.dumps({"sources": [{"source": "provider", "status": "ok", "stationCount": 1}]}), encoding="utf-8")
            (version_dir / "genres" / "example.json").write_text(json.dumps({"stations": [station, station]}), encoding="utf-8")
            (version_dir / "groups" / "genres.json").write_text(json.dumps({"items": [{"id": "example", "name": "Example", "url": "genres/example.json"}]}), encoding="utf-8")
            for key in ("countries", "languages"):
                (version_dir / "groups" / f"{key}.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            current = output / "current"
            current.mkdir()
            (current / "master-index.json").write_text(json.dumps({"version": version, "masterIndex": f"v{version}/master-index.json"}), encoding="utf-8")
            self.assertEqual(1, validate_catalog(output))

    def test_missing_current_pointer_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(1, validate_catalog(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
