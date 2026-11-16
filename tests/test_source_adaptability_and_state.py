import json
import aiohttp
import tempfile
import unittest
from unittest.mock import AsyncMock
from pathlib import Path

from sources.normalize import as_records, get_value, normalize_record, as_tags
from sources.state import load_snapshot, save_snapshot, validate_state
from sources.m3u import parse_m3u
from sources.github_playlists import fetch_repository_playlists
from scripts.build_radio_catalog import load_source
from sources.icecast import parse_directory


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

    def test_nested_station_and_stream_objects_are_supported(self):
        record = normalize_record({
            "station": {"title": "Nested Radio", "countryCode": "SE", "stationUuid": "nested-1"},
            "stream": {"streamUrl": "https://nested.example/live", "codec": "AAC", "bitrate": 192},
        })
        self.assertEqual("Nested Radio", record["name"])
        self.assertEqual("https://nested.example/live", record["url"])
        self.assertEqual("SE", record["countryCode"])
        self.assertEqual("nested-1", record["sourceStationId"])

    def test_tags_accept_strings_lists_and_objects(self):
        self.assertEqual(["Jazz", "News"], as_tags("Jazz; News, jazz"))
        self.assertEqual(["Rock", "Pop"], as_tags([{"title": "Rock"}, "Pop"]))


class PlaylistFormatTests(unittest.TestCase):
    def test_m3u_preserves_extended_metadata_and_handles_bom(self):
        payload = '\ufeff#EXTM3U\n#EXTINF:-1 tvg-id="station.one" tvg-country="GB" tvg-country-code="GB" tvg-language="English" group-title="Jazz",Example Radio\nhttps://radio.example/live\n'
        records = parse_m3u(payload)
        self.assertEqual(1, len(records))
        self.assertEqual("Example Radio", records[0]["name"])
        self.assertEqual("station.one", records[0]["station_id"])
        self.assertEqual(["Jazz"], records[0]["genres"])
        self.assertEqual("GB", records[0]["country"])
        self.assertEqual("GB", records[0]["country_code"])


    def test_icecast_parser_handles_xml_namespaces_and_field_variants(self):
        xml = '<root xmlns="urn:icecast"><station><name>Example</name><url>https://radio.example/live</url><genres>Jazz; News</genres><country_code>GB</country_code></station></root>'
        records = parse_directory(xml)
        self.assertEqual(1, len(records))
        self.assertEqual("Example", records[0]["name"])
        self.assertEqual("https://radio.example/live", records[0]["stream_url"])
        self.assertEqual("GB", records[0]["country_code"])

    def test_m3u_retains_playlist_entry_index_and_raw_metadata(self):
        payload = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="station.one" tvg-country="GB" group-title="Jazz",Example Radio\n'
            'https://radio.example/live\n'
            '#EXTINF:-1 tvg-id="station.two" group-title="News",Second Radio\n'
            'https://radio.example/second\n'
        )
        records = parse_m3u(payload)
        self.assertEqual([0, 1], [item["entry_index"] for item in records])
        self.assertEqual("Example Radio", records[0]["original_title"])
        self.assertEqual("station.one", records[0]["raw_metadata"]["tvg-id"])



class RepositoryPlaylistRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_reports_counts_and_tolerates_one_missing_file(self):
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="station.one",Example Radio\n'
            'https://radio.example/live\n'
        )
        tree = {"tree": [
            {"path": f"stations/{name}.m3u", "type": "blob"}
            for name in ("one", "two", "three", "four", "missing")
        ]}
        class FakeResponse:
            def __init__(self, *, payload=None, body="", status=200):
                self.payload, self.body, self.status = payload, body, status
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def raise_for_status(self):
                if self.status >= 400:
                    class FakeHttpError(aiohttp.ClientError):
                        def __init__(self, status):
                            self.status = status
                        def __str__(self):
                            return "fixture HTTP error"
                    raise FakeHttpError(self.status)
            async def json(self, content_type=None):
                return self.payload
            async def text(self):
                return self.body
        class FakeSession:
            def get(self, url, headers=None):
                if "api.github" in url:
                    return FakeResponse(payload=tree)
                if url.endswith("/stations/missing.m3u"):
                    return FakeResponse(status=404)
                return FakeResponse(body=playlist)

        report = {}
        records = await fetch_repository_playlists(
            FakeSession(), "https://api.github.example/tree",
            "https://raw.example/org/repo/main", report=report,
        )
        self.assertEqual(4, len(records))
        self.assertEqual(5, report["discovered"])
        self.assertEqual(5, report["processed"])
        self.assertEqual(4, report["retrieved"])
        self.assertEqual(4, report["succeeded"])
        self.assertEqual(1, report["failed"])
        self.assertEqual(0, report["skipped"])
        self.assertEqual(0.8, report["coverageRatio"])
        self.assertEqual("stations/missing.m3u", report["failedPlaylists"][0]["path"])
        self.assertIn("stations/one.m3u", {record["playlist_path"] for record in records})


class ProviderSnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip_and_validation(self):
        station = {"name": "Example", "streams": [{"source": "radio-browser", "url": "https://a.example/live"}]}
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
    async def test_empty_provider_response_restores_last_good_snapshot(self):
        station = {"name": "Saved Station", "streams": [{"source": "fixture", "url": "https://saved.example/live"}]}
        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "fixture", [station])
            stations, report = await load_source(
                None, "fixture", {"enabled": True, "priority": 50},
                AsyncMock(return_value=[]), temporary,
            )
            self.assertEqual("Saved Station", stations[0]["name"])
            self.assertEqual("https://saved.example/live", stations[0]["streams"][0]["url"])
            self.assertEqual("stale", report["status"])

    async def test_provider_failure_restores_its_own_last_good_snapshot(self):
        station = {"name": "Saved Station", "streams": [{"source": "fixture", "url": "https://saved.example/live", "sources": ["fixture"], "qualityScore": 50, "priority": 50, "bitrate": 0}]}
        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "fixture", [station])
            stations, report = await load_source(
                None, "fixture", {"enabled": True, "priority": 80},
                AsyncMock(side_effect=RuntimeError("upstream unavailable")), temporary,
            )
            self.assertEqual("Saved Station", stations[0]["name"])
            self.assertEqual("stale", report["status"])
            self.assertEqual(80, stations[0]["streams"][0]["priority"])
            self.assertEqual(80, stations[0]["streams"][0]["qualityScore"])
            self.assertEqual("last-good", report["snapshot"])


    async def test_partial_playlist_refresh_retains_last_good_records_for_failed_files(self):
        failed_old = {
            "name": "Old Failed Playlist Station",
            "streams": [{
                "source": "iprd", "url": "https://old.example/failed",
                "sourceRecords": [{"source": "iprd", "playlistPath": "failed.m3u"}],
            }],
            "_identityEvidence": [{"source": "iprd", "playlistPath": "failed.m3u"}],
        }
        successful_old = {
            "name": "Old Successful Playlist Station",
            "streams": [{
                "source": "iprd", "url": "https://old.example/success",
                "sourceRecords": [{"source": "iprd", "playlistPath": "success.m3u"}],
            }],
            "_identityEvidence": [{"source": "iprd", "playlistPath": "success.m3u"}],
        }
        fresh = {
            "name": "Fresh Playlist Station",
            "streams": [{"source": "iprd", "url": "https://fresh.example/live"}],
        }
        async def loader(session, config, priority):
            config["_playlistRefresh"] = {
                "discovered": 5, "processed": 5, "retrieved": 4,
                "succeeded": 4, "failed": 1, "skipped": 0,
                "coverageRatio": 0.8, "minimumCoverageRatio": 0.8,
                "failedPlaylists": [{"path": "failed.m3u", "error": "404", "status": 404}],
                "skippedPlaylists": [],
            }
            return [fresh]

        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "iprd", [failed_old, successful_old])
            stations, report = await load_source(
                None, "iprd", {"enabled": True, "priority": 80}, loader, temporary,
            )
        names = {station["name"] for station in stations}
        self.assertEqual(
            {"Fresh Playlist Station", "Old Failed Playlist Station"}, names
        )
        self.assertNotIn("Old Successful Playlist Station", names)
        self.assertEqual("partial", report["status"])
        self.assertEqual(5, report["playlistRefresh"]["discovered"])
        self.assertEqual(1, report["playlistRefresh"]["failed"])

    async def test_partial_playlist_refresh_without_matching_provenance_uses_full_last_good(self):
        old = {"name": "Saved Station", "streams": [{"source": "iprd", "url": "https://saved.example/live"}]}
        async def loader(session, config, priority):
            config["_playlistRefresh"] = {
                "discovered": 5, "processed": 5, "retrieved": 4,
                "succeeded": 4, "failed": 1, "skipped": 0,
                "coverageRatio": 0.8, "minimumCoverageRatio": 0.8,
                "failedPlaylists": [{"path": "missing-provenance.m3u"}],
                "skippedPlaylists": [],
            }
            return [{"name": "Fresh Station", "streams": [{"source": "iprd", "url": "https://fresh.example/live"}]}]

        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "iprd", [old])
            stations, report = await load_source(
                None, "iprd", {"enabled": True, "priority": 80}, loader, temporary,
            )
        self.assertEqual(["Saved Station"], [station["name"] for station in stations])
        self.assertEqual("stale", report["status"])
        self.assertIn("cannot map failed files", report["error"])

    async def test_playlist_coverage_collapse_restores_last_good_snapshot(self):
        old = {"name": "Saved Station", "streams": [{"source": "iprd", "url": "https://saved.example/live"}]}
        async def loader(session, config, priority):
            config["_playlistRefresh"] = {
                "discovered": 5, "processed": 5, "retrieved": 2,
                "succeeded": 2, "failed": 3, "skipped": 0,
                "coverageRatio": 0.4, "minimumCoverageRatio": 0.8,
                "failedPlaylists": [{"path": "a.m3u"}, {"path": "b.m3u"}, {"path": "c.m3u"}],
                "skippedPlaylists": [],
            }
            raise ValueError("GitHub playlist coverage collapsed")

        with tempfile.TemporaryDirectory() as temporary:
            save_snapshot(temporary, "iprd", [old])
            stations, report = await load_source(
                None, "iprd", {"enabled": True, "priority": 80}, loader, temporary,
            )
        self.assertEqual(["Saved Station"], [station["name"] for station in stations])
        self.assertEqual("stale", report["status"])
        self.assertEqual(0.4, report["playlistRefresh"]["coverageRatio"])


if __name__ == "__main__":
    unittest.main()
