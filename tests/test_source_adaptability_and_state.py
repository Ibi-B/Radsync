import json
import tempfile
import unittest
from pathlib import Path

from sources.normalize import as_records, get_value, normalize_record, as_tags
from sources.state import load_snapshot, save_snapshot, validate_state


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


if __name__ == "__main__":
    unittest.main()
