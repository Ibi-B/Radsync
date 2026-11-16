import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts import build_radio_catalog
from scripts.validate_catalog import validate_catalog


class CatalogueBuildIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_build_publishes_grouped_sources_and_ranked_default(self):
        config = {
            "radio_browser": {"enabled": True, "url": "https://fixture.invalid/stations", "priority": 90},
            "airwavedb": {"enabled": True, "stationsUrl": "https://fixture.invalid/stations", "streamsUrl": "https://fixture.invalid/streams", "priority": 85},
            "icecast": {"enabled": False},
            "iptv_org_radio": {"enabled": False},
            "iptv_org_streams": {"enabled": False},
            "pulham_hq": {"enabled": False},
            "iprd": {"enabled": False},
            "alon_drilich": {"enabled": False},
            "somafm": {"enabled": False},
            "bbc_radio": {"enabled": False},
        }
        radio_rows = [{
            "name": "Example Radio",
            "country": "United Kingdom",
            "countrycode": "GB",
            "language": "English",
            "tags": "news",
            "stationuuid": "rb-1",
            "url_resolved": "https://radio-browser.example/live",
            "codec": "MP3",
            "bitrate": 128,
        }]
        airwave_stations = [{
            "id": "airwave-1",
            "name": "Example Radio",
            "country": "United Kingdom",
            "countryCode": "GB",
            "language": "English",
            "tags": ["News"],
        }]
        airwave_streams = [{
            "stationId": "airwave-1",
            "url": "https://airwave.example/live",
            "codec": "AAC",
            "bitrate": 256,
        }]

        with tempfile.TemporaryDirectory() as temporary:
            previous = os.getcwd()
            try:
                os.chdir(temporary)
                Path("config").mkdir()
                Path("config/sources.json").write_text(json.dumps(config), encoding="utf-8")
                with patch.object(build_radio_catalog, "fetch_stations", new=AsyncMock(return_value=radio_rows)), \
                     patch.object(build_radio_catalog, "fetch_catalog", new=AsyncMock(return_value=(airwave_stations, airwave_streams))):
                    await build_radio_catalog.main()

                self.assertEqual(0, validate_catalog(Path("output")))
                pointer = json.loads(Path("output/current/master-index.json").read_text(encoding="utf-8"))
                master = json.loads((Path("output") / pointer["masterIndex"]).read_text(encoding="utf-8"))
                payload = json.loads((Path("output") / f"v{pointer['version']}" / master["stationsUrl"]).read_text(encoding="utf-8"))
                self.assertEqual(1, len(payload["stations"]))
                station = payload["stations"][0]
                self.assertEqual(2, len(station["streams"]))
                self.assertEqual("airwavedb", station["defaultSource"])
                self.assertEqual({"airwavedb", "radio-browser"}, set(station["sources"]))
                self.assertEqual({"airwavedb", "radio-browser"}, {group["source"] for group in station["sourceGroups"]})

                # Reporting-only provenance markers must stay out of the app-facing contract.
                for field in (
                    "_provenanceStatus",
                    "_provenanceStatusBySource",
                ):
                    self.assertNotIn(field, station)
                public_source_report = json.loads(
                    (Path("output") / f"v{pointer['version']}" / "source-report.json")
                    .read_text(encoding="utf-8")
                )
                self.assertTrue(all(
                    "stationProvenance" not in source
                    for source in public_source_report["sources"]
                ))
                private_refresh_report = json.loads(
                    Path("state/diagnostics/source-report.json").read_text(encoding="utf-8")
                )
                self.assertTrue(any(
                    source["stationProvenance"]
                    for source in private_refresh_report["sources"]
                    if source["source"] in {"airwavedb", "radio-browser"}
                ))
            finally:
                os.chdir(previous)


    async def test_refresh_report_survives_canonical_identity_failure(self):
        disabled_sources = {
            "radio_browser": {"enabled": False},
            "airwavedb": {"enabled": False},
            "icecast": {"enabled": False},
            "iptv_org_radio": {"enabled": False},
            "iptv_org_streams": {"enabled": False},
            "pulham_hq": {"enabled": False},
            "iprd": {"enabled": False},
            "alon_drilich": {"enabled": False},
            "somafm": {"enabled": False},
            "bbc_radio": {"enabled": False},
        }

        with tempfile.TemporaryDirectory() as temporary:
            previous = os.getcwd()
            try:
                os.chdir(temporary)
                Path("config").mkdir()
                Path("config/sources.json").write_text(
                    json.dumps(disabled_sources), encoding="utf-8"
                )
                with patch.object(
                    build_radio_catalog,
                    "assign_canonical_station_ids",
                    side_effect=ValueError("identity collision fixture"),
                ):
                    with self.assertRaisesRegex(ValueError, "identity collision fixture"):
                        await build_radio_catalog.main(state_dir=Path("state"))

                report_path = Path("state/diagnostics/source-report.json")
                self.assertTrue(report_path.exists())
                self.assertFalse(report_path.with_name("source-report.json.tmp").exists())
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual("failed", report["buildStatus"])
                self.assertEqual("canonical-identity-assignment", report["failedStage"])
                self.assertEqual("ValueError", report["error"]["type"])
                self.assertEqual("identity collision fixture", report["error"]["message"])
                self.assertEqual(10, len(report["sources"]))
                self.assertTrue(all("ingestionStartedAt" in item for item in report["sources"]))
                self.assertTrue(all("ingestionCompletedAt" in item for item in report["sources"]))
                self.assertTrue(all("stationProvenance" in item for item in report["sources"]))
                self.assertFalse(Path("output/current/master-index.json").exists())
            finally:
                os.chdir(previous)



if __name__ == "__main__":
    unittest.main()
