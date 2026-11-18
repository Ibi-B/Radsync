import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_radio_catalog import (
    assign_canonical_station_ids,
    find_station_match,
    group_slug,
    make_station,
    make_stream,
    merge_station,
    stations_compatible,
)


class CatalogAggregationTests(unittest.TestCase):
    def station(self, source, url, priority, country="GB", language="English", bitrate=128):
        return make_station(
            "Example Radio",
            country=country,
            country_code=country,
            language=language,
            streams=[make_stream(source, url, bitrate=bitrate, priority=priority)],
        )

    def test_same_provider_station_keeps_stream_variants_and_ranks_default(self):
        preferred = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream(
                "radio-browser", "https://high.example/live", codec="AAC",
                bitrate=192, priority=90, source_station_id="rb-1",
            )],
        )
        alternate = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream(
                "radio-browser", "https://fallback.example/live", codec="MP3",
                bitrate=128, priority=60, source_station_id="rb-1",
            )],
        )

        self.assertTrue(stations_compatible(preferred, alternate))
        self.assertIs(preferred, find_station_match([preferred], alternate))
        merge_station(preferred, alternate)

        self.assertEqual(2, len(preferred["streams"]))
        self.assertEqual("radio-browser", preferred["defaultSource"])
        self.assertEqual(0, preferred["defaultStreamIndex"])
        self.assertEqual({"radio-browser"}, set(preferred["sources"]))
        self.assertEqual(
            {0, 1},
            {index for group in preferred["sourceGroups"] for index in group["streamIndexes"]},
        )
        self.assertEqual(
            {("AAC", 192), ("MP3", 128)},
            {(stream["codec"], stream["bitrate"]) for stream in preferred["streams"]},
        )

    def test_same_name_in_different_countries_is_not_merged(self):
        gb = self.station("source-a", "https://gb.example/live", 80, country="GB")
        us = self.station("source-b", "https://us.example/live", 80, country="US")
        self.assertFalse(stations_compatible(gb, us))
        self.assertIsNone(find_station_match([gb], us))

    def test_missing_metadata_stations_get_distinct_deterministic_ids(self):
        gb = make_station(
            "Shared Name", country="United Kingdom", country_code="GB", language="English",
            streams=[make_stream("provider-a", "https://a.example/live", priority=80, source_station_id="a-1")],
        )
        us_missing = make_station(
            "Shared Name", language="English",
            streams=[make_stream("provider-b", "https://b.example/live", priority=80, source_station_id="b-1")],
        )
        stations = [gb, us_missing]
        assign_canonical_station_ids(stations)
        expected = {station["stationUuid"] for station in stations}
        self.assertEqual(2, len(expected))

        reversed_stations = [us_missing.copy(), gb.copy()]
        assign_canonical_station_ids(reversed_stations)
        self.assertEqual(expected, {station["stationUuid"] for station in reversed_stations})

    def test_same_name_and_metadata_with_distinct_provider_ids_are_not_merged(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("radio-browser", "https://one.example/live", priority=80, source_station_id="rb-1")],
        )
        second = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("airwavedb", "https://two.example/live", priority=80, source_station_id="aw-2")],
        )
        self.assertIsNone(find_station_match([first], second))
        assign_canonical_station_ids([first, second])
        self.assertNotEqual(first["stationUuid"], second["stationUuid"])
        self.assertEqual(1, len(first["streams"]))
        self.assertEqual(1, len(second["streams"]))

    def test_cross_provider_endpoint_equivalence_ignores_tokens_but_keeps_urls(self):
        url = "https://shared.example/live?provider-token=one"
        first = make_station(
            "Example Radio", country="United Kingdom", country_code="GB", language="English",
            streams=[make_stream("radio-browser", url, codec="AAC", bitrate=128, priority=80, source_station_id="rb-1")],
        )
        second = make_station(
            "Example Radio", country="UK", country_code="GB", language="English",
            streams=[make_stream("airwavedb", "https://shared.example/live?provider-token=two", codec="MP3", bitrate=64, priority=70, source_station_id="aw-2")],
        )
        self.assertIs(first, find_station_match([first], second))
        merge_station(first, second)
        # Endpoint identity ignores volatile query tokens, but both original
        # playback URLs are retained rather than silently rewriting either.
        self.assertEqual(2, len(first["streams"]))
        self.assertEqual(
            {url, "https://shared.example/live?provider-token=two"},
            {stream["url"] for stream in first["streams"]},
        )
        self.assertEqual(
            {"radio-browser", "airwavedb"},
            {source for stream in first["streams"] for source in stream["sources"]},
        )

    def test_cross_provider_same_call_sign_and_website_merge_different_streams(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            tags=["news", "talk"],
            streams=[make_stream(
                "radio-browser", "https://one.example/live", codec="MP3",
                bitrate=64, priority=90, source_station_id="rb-1",
            )],
            identity_evidence=[{
                "source": "radio-browser", "website": "https://example-radio.co.uk/",
                "callSign": "WEXM", "city": "London", "region": "England",
            }],
        )
        second = make_station(
            "Example Radio", country="United Kingdom", country_code="GB", language="English",
            tags=["news"],
            streams=[make_stream(
                "airwavedb", "https://two.example/live", codec="AAC",
                bitrate=128, priority=80, source_station_id="aw-2",
            )],
            identity_evidence=[{
                "source": "airwavedb", "website": "http://www.example-radio.co.uk",
                "callSign": "W-EXM", "city": "London", "region": "England",
            }],
        )
        self.assertIs(first, find_station_match([first], second))
        merge_station(first, second)
        assign_canonical_station_ids([first])
        self.assertEqual(2, len(first["streams"]))
        self.assertTrue(first["stationUuid"])
        self.assertEqual({"MP3", "AAC"}, {stream["codec"] for stream in first["streams"]})
        self.assertEqual({64, 128}, {stream["bitrate"] for stream in first["streams"]})
        self.assertEqual({"radio-browser", "airwavedb"}, set(first["sources"]))

    def test_same_name_country_language_and_genres_without_identity_anchor_stay_separate(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            tags=["news", "pop"],
            streams=[make_stream("provider-a", "https://one.example/live", source_station_id="a-1")],
        )
        second = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            tags=["news", "pop"],
            streams=[make_stream("provider-b", "https://two.example/live", source_station_id="b-1")],
        )
        self.assertIsNone(find_station_match([first], second))

    def test_shared_endpoint_with_conflicting_call_signs_stays_separate(self):
        url = "https://relay.example/live"
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", url, source_station_id="a-1")],
            identity_evidence=[{"source": "provider-a", "callSign": "WAAA"}],
        )
        second = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", url, source_station_id="b-1")],
            identity_evidence=[{"source": "provider-b", "callSign": "WBBB"}],
        )
        self.assertIsNone(find_station_match([first], second))

    def test_shared_website_and_country_language_can_match_different_stream_urls(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", "https://one.example/live", source_station_id="a-1")],
            identity_evidence=[{"source": "provider-a", "website": "https://example.org"}],
        )
        second = make_station(
            "Example Radio", country="United Kingdom", country_code="GB", language="English",
            streams=[make_stream("provider-b", "https://two.example/live", source_station_id="b-1")],
            identity_evidence=[{"source": "provider-b", "website": "http://www.example.org/"}],
        )
        self.assertIs(first, find_station_match([first], second))

    def test_indistinguishable_aggregates_are_preserved_with_distinct_ids(self):
        first = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        second = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        assign_canonical_station_ids([first, second])
        expected = {first["stationUuid"], second["stationUuid"]}
        self.assertEqual(2, len(expected))

        reversed_first = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        reversed_second = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        assign_canonical_station_ids([reversed_second, reversed_first])
        self.assertEqual(expected, {reversed_first["stationUuid"], reversed_second["stationUuid"]})

    def test_identity_ambiguity_writes_diagnostic_after_preserving_both_stations(self):
        first = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live?token=secret", priority=50, source_station_id="same-id")],
        )
        second = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live?token=secret", priority=50, source_station_id="same-id")],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostics" / "identity-ambiguity.json"
            assign_canonical_station_ids([first, second], path, catalog_version=123)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(123, report["catalogVersion"])
        self.assertEqual(1, len(report["ambiguousGroups"]))
        group = report["ambiguousGroups"][0]
        self.assertEqual("preserved_separately", group["classification"])
        self.assertEqual(2, len(group["records"]))
        self.assertEqual(2, len({r["assignedStationUuid"] for r in group["records"]}))
        self.assertNotIn("token=secret", json.dumps(report))

    def test_playlist_entry_provenance_disambiguates_same_name_and_endpoint(self):
        url = "https://same.example/live"
        first = make_station(
            "Same", streams=[make_stream("provider", url, source_station_id="same-id")],
            identity_evidence=[{
                "source": "provider", "providerRecordId": "same-id",
                "streamUrl": url, "playlistRepository": "org/repo",
                "playlistPath": "countries/gb.m3u", "entryIndex": 12,
            }],
        )
        second = make_station(
            "Same", streams=[make_stream("provider", url, source_station_id="same-id")],
            identity_evidence=[{
                "source": "provider", "providerRecordId": "same-id",
                "streamUrl": url, "playlistRepository": "org/repo",
                "playlistPath": "genres/news.m3u", "entryIndex": 7,
            }],
        )
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = Path(directory) / "identity-ambiguity.json"
            assign_canonical_station_ids([first, second], diagnostic)
            self.assertFalse(diagnostic.exists())
        self.assertNotEqual(first["stationUuid"], second["stationUuid"])

    def test_identity_diagnostic_captures_raw_provider_evidence(self):
        from scripts.build_radio_catalog import make_identity_evidence
        evidence = make_identity_evidence(
            "radio-browser",
            {"name": "Raw Station", "stationuuid": "uuid-1", "tags": "news,pop"},
            provider_record_id="record-1",
            source_url="https://provider.example/api?key=secret",
            stream_url="https://stream.example/live?token=secret",
            codec="AAC",
            bitrate=128,
        )
        self.assertEqual("uuid-1", evidence["providerStationUuid"])
        self.assertEqual("record-1", evidence["providerRecordId"])
        self.assertEqual("Raw Station", evidence["rawName"])
        self.assertEqual(["news", "pop"], evidence["tags"])
        self.assertNotIn("secret", json.dumps(evidence))

    def test_diagnostic_report_includes_every_indistinguishable_group(self):
        stations = []
        for name in ("Same A", "Same B"):
            stations.extend([
                make_station(name, country="GB", country_code="GB", language="English",
                             streams=[make_stream("provider", f"https://{name.replace(' ', '').lower()}.example/live", source_station_id="same-id")]),
                make_station(name, country="GB", country_code="GB", language="English",
                             streams=[make_stream("provider", f"https://{name.replace(' ', '').lower()}.example/live", source_station_id="same-id")]),
            ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity-ambiguity.json"
            assign_canonical_station_ids(stations, path)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(2, len(report["ambiguousGroups"]))
        self.assertEqual(4, len({station["stationUuid"] for station in stations}))

    def test_invalid_stream_urls_are_not_published(self):
        station = make_station(
            "Broken Station",
            streams=[
                make_stream("bad", "javascript:alert(1)", priority=100),
                make_stream("empty", "", priority=99),
            ],
        )
        self.assertEqual([], station["streams"])
        self.assertIsNone(station["defaultSource"])
        self.assertIsNone(station["defaultStreamIndex"])

    def test_duplicate_stream_url_is_not_duplicated(self):
        station = self.station("source-a", "https://same.example/live", 80)
        merge_station(station, self.station("source-b", "https://same.example/live", 70))
        self.assertEqual(1, len(station["streams"]))
        self.assertEqual(["source-a", "source-b"], station["sources"])
        self.assertEqual(["source-a", "source-b"], station["streams"][0]["sources"])
        grouped_sources = {group["source"] for group in station["sourceGroups"]}
        self.assertEqual({"source-a", "source-b"}, grouped_sources)
        self.assertTrue(all(group["streamIndexes"] == [0] for group in station["sourceGroups"]))

    def test_long_group_names_are_bounded_and_collision_resistant(self):
        prefix = "00s-hits-nostalgic-pop-" * 20
        first = group_slug(prefix + "version-a")
        second = group_slug(prefix + "version-b")

        self.assertLessEqual(len(first), 96)
        self.assertLessEqual(len(second), 96)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(__import__("hashlib").sha256((prefix + "version-a").encode("utf-8")).hexdigest()[:12]))

    def test_source_group_indexes_match_sorted_streams(self):
        station = self.station("source-a", "https://a.example/live", 50)
        merge_station(station, self.station("source-b", "https://b.example/live", 70))
        by_index = {i: stream["source"] for i, stream in enumerate(station["streams"])}
        for group in station["sourceGroups"]:
            self.assertTrue(all(by_index[i] == group["source"] for i in group["streamIndexes"]))

    def test_exact_provider_identity_wins_when_multiple_compatible_candidates_exist(self):
        first = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.aac", priority=90, source_station_id="rb-1")],
        )
        second = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.mp3", priority=90, source_station_id="rb-2")],
        )
        incoming = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.aac", priority=90, source_station_id="rb-1")],
        )
        self.assertIs(first, find_station_match([first, second], incoming))

    def test_unmatched_provider_identity_does_not_guess_between_candidates(self):
        first = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.aac", priority=90, source_station_id="rb-1")],
        )
        second = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.mp3", priority=90, source_station_id="rb-2")],
        )
        incoming = make_station(
            "0 N - 2000s on Radio", country="Germany", country_code="DE", language="german",
            streams=[make_stream("radio-browser", "https://0n.example/live.other", priority=90, source_station_id="rb-3")],
        )
        self.assertIsNone(find_station_match([first], incoming))
        self.assertIsNone(find_station_match([first, second], incoming))

    def test_same_name_endpoint_and_location_match_without_provider_ids(self):
        url = "https://same.example/live"
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", url)],
        )
        incoming = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", url)],
        )
        self.assertIs(first, find_station_match([first], incoming))

    def test_multiple_provider_ids_for_one_provider_are_never_overwritten(self):
        station = make_station(
            "Multi ID Radio", country="GB", country_code="GB", language="English",
            streams=[
                make_stream("radio-browser", "https://multi.example/aac", source_station_id="rb-a"),
                make_stream("radio-browser", "https://multi.example/mp3", source_station_id="rb-b"),
            ],
        )
        self.assertEqual(["rb-a", "rb-b"], station["sourceIds"]["radio-browser"])
        incoming = make_station(
            "Multi ID Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("radio-browser", "https://multi.example/third", source_station_id="rb-b")],
        )
        self.assertIs(station, find_station_match([station], incoming))
        merge_station(station, incoming)
        self.assertEqual(["rb-a", "rb-b"], station["sourceIds"]["radio-browser"])

    def test_duplicate_endpoint_keeps_distinct_source_record_provenance(self):
        first = make_station(
            "Same Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", "https://same.example/live")],
            identity_evidence=[{"source": "provider-a", "streamUrl": "https://same.example/live", "playlistPath": "a.m3u", "entryIndex": 2}],
        )
        second = make_station(
            "Same Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", "https://same.example/live")],
            identity_evidence=[{"source": "provider-b", "streamUrl": "https://same.example/live", "playlistPath": "b.m3u", "entryIndex": 9}],
        )
        merge_station(first, second)
        self.assertEqual(1, len(first["streams"]))
        self.assertEqual({"provider-a", "provider-b"}, {item["source"] for item in first["streams"][0]["sourceRecords"]})


    def test_original_twelve_ambiguity_groups_are_kept_as_separate_stations(self):
        fixture_path = Path(__file__).parent / "fixtures" / "identity_ambiguity_groups.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(12, len(fixture["groups"]))
        self.assertEqual(39, sum(len(group["records"]) for group in fixture["groups"]))

        for group in fixture["groups"]:
            observations = [
                (stream["source"], stream["streamUrl"])
                for record in group["records"]
                for stream in record.get("streams", [])
            ]
            counts = {}
            for observation in observations:
                counts[observation] = counts.get(observation, 0) + 1
            duplicate = next(
                (observation for observation, count in counts.items() if count > 1),
                None,
            )
            self.assertIsNotNone(duplicate, group["name"])
            source, url = duplicate
            first = make_station(
                group["name"], streams=[make_stream(source, url, priority=50)]
            )
            second = make_station(
                group["name"], streams=[make_stream(source, url, priority=50)]
            )
            assign_canonical_station_ids([first, second])
            self.assertNotEqual(first["stationUuid"], second["stationUuid"], group["name"])

    def test_complete_playlist_provenance_survives_shared_endpoint_merge(self):
        url = "https://shared.example/live"
        evidence_a = {
            "source": "provider-a", "providerRecordId": "a-1",
            "sourceUrl": "https://raw.example/provider-a.m3u",
            "playlistRepository": "org/provider-a",
            "playlistPath": "stations/a.m3u", "entryIndex": 4,
            "rawName": "Provider A title",
            "rawExtinf": '#EXTINF:-1 tvg-id="a-1",Provider A title',
            "originalTitle": "Provider A title", "originalUrl": url,
            "rawMetadata": {"tvg-id": "a-1"}, "ingestedAt": "2026-11-16T00:00:00Z",
            "streamUrl": url,
        }
        evidence_b = {
            "source": "provider-b", "providerRecordId": "b-9",
            "sourceUrl": "https://raw.example/provider-b.m3u",
            "playlistRepository": "org/provider-b",
            "playlistPath": "stations/b.m3u", "entryIndex": 8,
            "rawName": "Provider B title",
            "rawExtinf": '#EXTINF:-1 tvg-id="b-9",Provider B title',
            "originalTitle": "Provider B title", "originalUrl": url,
            "rawMetadata": {"tvg-id": "b-9"}, "ingestedAt": "2026-11-16T00:01:00Z",
            "streamUrl": url,
        }
        first = make_station(
            "Shared Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-a", url)],
            identity_evidence=[evidence_a],
        )
        second = make_station(
            "Shared Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider-b", url)],
            identity_evidence=[evidence_b],
        )
        merge_station(first, second)
        self.assertEqual(1, len(first["streams"]))
        records = first["streams"][0]["sourceRecords"]
        self.assertEqual({"provider-a", "provider-b"}, {r["source"] for r in records})
        self.assertEqual({"stations/a.m3u", "stations/b.m3u"}, {r["playlistPath"] for r in records})
        self.assertEqual({4, 8}, {r["entryIndex"] for r in records})
        self.assertEqual({evidence_a["rawExtinf"], evidence_b["rawExtinf"]}, {r["rawExtinf"] for r in records})
        self.assertEqual({"Provider A title", "Provider B title"}, {r["originalTitle"] for r in records})
        self.assertEqual({url}, {r["originalUrl"] for r in records})


if __name__ == "__main__":
    unittest.main()
