import json
import tempfile
import unittest
from unittest.mock import AsyncMock
from pathlib import Path

from sources.normalize import as_records, get_value, normalize_record, as_tags
from sources.state import load_snapshot, save_snapshot, validate_state
from sources.m3u import parse_m3u
from scripts.build_radio_catalog import load_source


class SourceNormalizationTests(unittest.TestCase):
    def test_accepts_common_envelope_shapes(self):
        for payload in (
            [{"stationName": "Example", "streamUrl": "https://a.example/live"}],
            {"data": {"items": [{"title": "Example", "url": "https://a.example/live"}]}},
            {"results": [{"name": "Example", "url_resolved": "https://a.example/live"}]},
            {"abc": {"name": "Example", "url": "https://a.example/live"}},
        ):
            self.assertEqual(1, len(as_records(payload)))

    def test_aliases_and_nested_metadata_normalize(self):
        record = normalize_record({
            "stationName": "Example Radio",
            "streamUrl": "https://a.example/live",
            "countryCode": "GB",
            "languages": ["English", "Welsh"],
            "genres": [{"name": "Jazz"}, "News"],
            "bitRate": "192",
            "stationUuid": "source-123",
        })
        self.assertEqual("Example Radio", record["name"])
        self.assertEqual("GB", record["countryCode"])
        self.assertEqual(["Jazz", "News"], record["tags"])
        self.assertEqual("source-123", record["sourceStationId"])

    def test_tags_accept_strings_lists_and_objects(self):
        self.assertEqual(["Jazz", "News"], as_tags("Jazz; News, jazz"))
        self.assertEqual(["Rock", "Pop"], as_tags([{"title": "Rock"}, "Pop"]))


class PlaylistFormatTests(unittest.TestCase):
    def test_m3u_preserves_extended_metadata_and_handles_bom(self):
        payload = '\ufeff#EXTM3U\\n#EXTINF:-1 tvg-id="station.one" tvg-country="GB" tvg-language="English" group-title="Jazz",Example Radio\\nhttps://radio.example/live\\n'
        records = parse_m3u(payload)
        self.assertEqual(1, len(records))
        self.assertEqual("Example Radio", records[0]["name"])
        self.assertEqual("station.one", records[0]["station_id"])
        self.assertEqual(["Jazz"], records[0]["genres"])
        self.assertEqual("GB", records[0]["country"])


class ProviderSnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip_and_validation(self):
        station = {"name": "Example", "streams": [{"url": "https://a.example/live"}]}
        with tempfile.TemporaryDirectory() as temporary:
            saved = save_snapshot(temporary, "radio-browser", [station])
            restored = load_snapshot(temporary, "radio-browser")
            self.assertEqual(saved["stations"], restored["stations"])
            self.assertEqual("radio-browser", restored["source"])
            self.assertEqual((True, []), validate_state(temporary))

    def test_empty_snapshot_is_never_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                save_snapshot(temporary, "radio-browser", [])
            self.assertIsNone(load_snapshot(temporary, "radio-browser"))

    def test_corrupt_snapshot_is_not_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources" / "radio-browser.json"
            path.parent.mkdir()
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_snapshot(temporary, "radio-browser"))
            valid, issues = validate_state(temporary)
            self.assertFalse(valid)
            self.assertTrue(issues)



class SourceFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_restores_its_own_last_good_snapshot(self):
        station = {"name": "Saved Station", "streams": [{"source": "fixture", "url": "https://saved.example/live", "sources": ["fixture"], "qualityScore": 50, "priority": 50, "bitrate": 0}]}
        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "fixture", [station])
            stations, report = await load_source(
                None, "fixture", {"enabled": True, "priority": 50},
                AsyncMock(side_effect=RuntimeError("upstream unavailable")), temporary,
            )
            self.assertEqual([station], stations)
            self.assertEqual("stale", report["status"])
            self.assertEqual("last-good", report["snapshot"])


if __name__ == "__main__":
    unittest.main()
