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
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
