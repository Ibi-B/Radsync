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

    def test_duplicate_station_keeps_all_sources_and_ranks_default(self):
        preferred = self.station("high-priority", "https://high.example/live", 90)
        alternate = self.station("fallback", "https://fallback.example/live", 60)

        self.assertTrue(stations_compatible(preferred, alternate))
        merge_station(preferred, alternate)

        self.assertEqual(2, len(preferred["streams"]))
        self.assertEqual("high-priority", preferred["defaultSource"])
        self.assertEqual(0, preferred["defaultStreamIndex"])
        self.assertEqual({"fallback", "high-priority"}, set(preferred["sources"]))
        self.assertEqual(
            {0, 1},
            {index for group in preferred["sourceGroups"] for index in group["streamIndexes"]},
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

    def test_same_name_country_and_language_with_distinct_provider_ids_merge(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("radio-browser", "https://one.example/live", priority=80, source_station_id="rb-1")],
        )
        second = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("airwavedb", "https://two.example/live", priority=80, source_station_id="aw-2")],
        )
        self.assertIs(first, find_station_match([first], second))
        merge_station(first, second)
        assign_canonical_station_ids([first])
        self.assertEqual(2, len(first["streams"]))
        self.assertEqual({"rb-1", "aw-2"}, {
            source_id for stream in first["streams"] for source_id in stream["sourceStationIds"]
        })

    def test_genuine_duplicate_keeps_all_streams_under_one_identity(self):
        first = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("radio-browser", "https://one.example/live", priority=90, source_station_id="rb-1")],
        )
        second = make_station(
            "Example Radio", country="GB", country_code="GB", language="English",
            streams=[make_stream("airwavedb", "https://two.example/live", priority=80, source_station_id="aw-2")],
        )
        merge_station(first, second)
        assign_canonical_station_ids([first])
        self.assertEqual(2, len(first["streams"]))
        self.assertTrue(first["stationUuid"])
        self.assertEqual(0, first["defaultStreamIndex"])

    def test_indistinguishable_aggregates_fail_closed(self):
        first = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        second = make_station(
            "Same", country="GB", country_code="GB", language="English",
            streams=[make_stream("provider", "https://same.example/live", priority=50, source_station_id="same-id")],
        )
        with self.assertRaises(ValueError):
            assign_canonical_station_ids([first, second])

    def test_identity_ambiguity_writes_private_diagnostic_before_failing(self):
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
            with self.assertRaisesRegex(ValueError, "identical metadata and provider provenance"):
                assign_canonical_station_ids([first, second], path, catalog_version=123)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(123, report["catalogVersion"])
        self.assertEqual(1, len(report["ambiguousGroups"]))
        group = report["ambiguousGroups"][0]
        self.assertEqual("likely_duplicate", group["classification"])
        self.assertEqual(2, len(group["records"]))
        self.assertNotIn("token=secret", json.dumps(report))

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
            with self.assertRaises(ValueError):
                assign_canonical_station_ids(stations, path)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(2, len(report["ambiguousGroups"]))

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
        self.assertIsNone(find_station_match([first, second], incoming))


if __name__ == "__main__":
    unittest.main()
