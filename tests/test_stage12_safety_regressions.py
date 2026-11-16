import tempfile
import unittest

from scripts.build_radio_catalog import (
    _source_ids_for_station,
    find_station_match,
    load_source,
    make_station,
    make_stream,
    merge_station,
)
from sources.github_playlists import fetch_repository_playlists
from sources.state import save_snapshot


class ProviderIdentityAttributionTests(unittest.TestCase):
    def test_station_source_ids_include_provider_attributed_source_record_ids(self):
        evidence = {
            "source": "provider-a",
            "providerRecordId": "record-100",
            "providerStationUuid": "station-200",
            "streamUrl": "https://evidence.example/live",
        }
        station = make_station(
            "Evidence Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", "https://evidence.example/live")],
            identity_evidence=[evidence],
        )

        self.assertEqual(
            ["record-100", "station-200"],
            station["sourceIds"]["provider-a"],
        )
        self.assertEqual(
            {"provider-a": ["record-100", "station-200"]},
            station["streams"][0]["providerSources"],
        )

    def test_shared_endpoint_keeps_each_id_under_its_original_provider(self):
        shared_url = "https://shared.example/live"
        provider_a = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", shared_url, priority=50, source_station_id="A-100")],
        )
        provider_b = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", shared_url, priority=90, source_station_id="B-200")],
        )

        merge_station(provider_a, provider_b)

        self.assertEqual(
            {"provider-a": ["A-100"], "provider-b": ["B-200"]},
            _source_ids_for_station(provider_a),
        )
        shared_stream = provider_a["streams"][0]
        self.assertEqual("provider-b", shared_stream["source"])
        self.assertEqual(
            {"provider-a": ["A-100"], "provider-b": ["B-200"]},
            shared_stream["providerSources"],
        )
        self.assertEqual({"A-100", "B-200"}, set(shared_stream["sourceStationIds"]))

    def test_same_id_string_from_different_provider_does_not_create_false_match(self):
        shared_url = "https://shared.example/live"
        merged = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", shared_url, priority=50, source_station_id="A-100")],
        )
        merge_station(merged, make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", shared_url, priority=90, source_station_id="B-200")],
        ))

        correct_provider_b_station = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", "https://other.example/live", priority=80, source_station_id="A-100")],
        )
        incoming = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", "https://new.example/live", priority=80, source_station_id="A-100")],
        )

        self.assertIs(
            correct_provider_b_station,
            find_station_match([merged, correct_provider_b_station], incoming),
        )


class PartialPlaylistPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_incomplete_paths_require_full_last_good_snapshot_if_one_is_unmapped(self):
        old_mapped = {
            "name": "Old Mapped Station",
            "streams": [{
                "source": "iprd", "url": "https://old.example/a",
                "sourceRecords": [{"source": "iprd", "playlistPath": "a.m3u"}],
            }],
            "_identityEvidence": [{"source": "iprd", "playlistPath": "a.m3u"}],
        }
        old_unmapped = {
            "name": "Old Unmapped Station",
            "streams": [{"source": "iprd", "url": "https://old.example/b"}],
        }
        async def loader(session, config, priority):
            config["_playlistRefresh"] = {
                "discovered": 5, "processed": 5, "retrieved": 3,
                "succeeded": 3, "failed": 2, "skipped": 0,
                "coverageRatio": 0.6, "minimumCoverageRatio": 0.5,
                "failedPlaylists": [{"path": "a.m3u"}, {"path": "b.m3u"}],
                "skippedPlaylists": [],
            }
            return [{
                "name": "Fresh Station",
                "streams": [{"source": "iprd", "url": "https://fresh.example/live"}],
            }]

        with tempfile.TemporaryDirectory() as directory:
            save_snapshot(directory, "iprd", [old_mapped, old_unmapped])
            stations, report = await load_source(
                None, "iprd", {"enabled": True, "priority": 80}, loader, directory,
            )

        self.assertEqual("stale", report["status"])
        self.assertEqual({"Old Mapped Station", "Old Unmapped Station"}, {
            station["name"] for station in stations
        })
        self.assertNotIn("Fresh Station", {station["name"] for station in stations})
        self.assertIn("b.m3u", report["error"])

    async def test_unparseable_skipped_playlist_retains_its_last_good_records(self):
        old = {
            "name": "Old Playlist Station",
            "streams": [{
                "source": "iprd", "url": "https://old.example/live",
                "sourceRecords": [{"source": "iprd", "playlistPath": "broken.m3u"}],
            }],
            "_identityEvidence": [{"source": "iprd", "playlistPath": "broken.m3u"}],
        }
        async def loader(session, config, priority):
            config["_playlistRefresh"] = {
                "discovered": 5, "processed": 5, "retrieved": 5,
                "succeeded": 4, "failed": 0, "skipped": 1,
                "coverageRatio": 1.0, "parseCoverageRatio": 0.8,
                "minimumCoverageRatio": 0.8, "minimumParseCoverageRatio": 0.8,
                "skippedPlaylists": [{
                    "path": "broken.m3u",
                    "classification": "unparseable",
                    "reason": "zero records",
                }],
                "failedPlaylists": [],
            }
            return [{
                "name": "Fresh Station",
                "streams": [{"source": "iprd", "url": "https://fresh.example/live"}],
            }]

        with tempfile.TemporaryDirectory() as directory:
            save_snapshot(directory, "iprd", [old])
            stations, report = await load_source(
                None, "iprd", {"enabled": True, "priority": 80}, loader, directory,
            )

        self.assertEqual("partial", report["status"])
        self.assertEqual({"Old Playlist Station", "Fresh Station"}, {
            station["name"] for station in stations
        })

    async def test_header_only_m3u_is_not_proof_of_intentional_empty(self):
        tree = {"tree": [{"path": "empty.m3u", "type": "blob"}]}

        class FakeResponse:
            def __init__(self, payload=None, body=""):
                self.payload = payload
                self.body = body
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def raise_for_status(self):
                return None
            async def json(self, content_type=None):
                return self.payload
            async def text(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None):
                if "api.github" in url:
                    return FakeResponse(payload=tree)
                return FakeResponse(body="#EXTM3U\n")

        report = {}
        records = await fetch_repository_playlists(
            FakeSession(), "https://api.github.example/tree",
            "https://raw.example/org/repo/main", report=report,
        )

        self.assertEqual([], records)
        self.assertEqual(1, report["retrieved"])
        self.assertEqual(1, report["parsedSuccessfully"])
        self.assertEqual(1, report["parsedWithoutUsableRecords"])
        self.assertEqual(0, report["validEmpty"])
        self.assertEqual(0, report["unparseable"])
        self.assertEqual(1.0, report["coverageRatio"])
        self.assertEqual(1.0, report["parseCoverageRatio"])
        self.assertEqual(0.0, report["recordProductionCoverageRatio"])
        self.assertEqual("no-usable-records", report["skippedPlaylists"][0]["classification"])

    async def test_playlist_with_malformed_entry_is_not_classified_valid_empty(self):
        tree = {"tree": [{"path": "bad.m3u", "type": "blob"}]}

        class FakeResponse:
            def __init__(self, payload=None, body=""):
                self.payload = payload
                self.body = body
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def raise_for_status(self):
                return None
            async def json(self, content_type=None):
                return self.payload
            async def text(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None):
                if "api.github" in url:
                    return FakeResponse(payload=tree)
                return FakeResponse(body='#EXTM3U\n#EXTINF:-1 tvg-id="one",Station\n')

        report = {}
        with self.assertRaisesRegex(ValueError, "parse coverage collapsed"):
            await fetch_repository_playlists(
                FakeSession(), "https://api.github.example/tree",
                "https://raw.example/org/repo/main", report=report,
            )

        self.assertEqual(1, report["parseFailed"])
        self.assertEqual(1, report["unparseable"])
        self.assertEqual(0, report["validEmpty"])
        self.assertEqual(1.0, report["coverageRatio"])
        self.assertEqual(0.0, report["parseCoverageRatio"])
        self.assertEqual(0.0, report["recordProductionCoverageRatio"])
        self.assertEqual("parse-failed", report["skippedPlaylists"][0]["classification"])
