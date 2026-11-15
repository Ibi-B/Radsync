import unittest

from scripts.build_radio_catalog import (
    find_station_match,
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
        self.assertEqual(["source-a"], station["sources"])

    def test_source_group_indexes_match_sorted_streams(self):
        station = self.station("source-a", "https://a.example/live", 50)
        merge_station(station, self.station("source-b", "https://b.example/live", 70))
        by_index = {i: stream["source"] for i, stream in enumerate(station["streams"])}
        for group in station["sourceGroups"]:
            self.assertTrue(all(by_index[i] == group["source"] for i in group["streamIndexes"]))


if __name__ == "__main__":
    unittest.main()
