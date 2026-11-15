import unittest

from scripts.station_identity_continuity import build_continuity_report


def station(identity, name, source_ids=(), country="GB", language="English"):
    return {
        "stationUuid": identity,
        "name": name,
        "normalizedName": " ".join(name.casefold().split()),
        "countryCode": country,
        "language": language,
        "streams": [
            {"source": source, "sourceStationIds": [source_id], "url": f"https://{source}.example/live"}
            for source, source_id in source_ids
        ],
    }


class StationIdentityContinuityTests(unittest.TestCase):
    def test_retained_ids_are_counted(self):
        report = build_continuity_report(
            [station("same", "Example Radio")],
            [station("same", "Example Radio")],
            1, 2,
        )
        self.assertEqual(1, report["retained"])
        self.assertEqual(0, report["added"])
        self.assertEqual(0, report["removed"])

    def test_provider_id_change_is_high_confidence_candidate(self):
        old = station("old-id", "Old Name", [("radio-browser", "stable-1")])
        new = station("new-id", "Renamed Station", [("radio-browser", "stable-1")])
        report = build_continuity_report([old], [new], 1, 2)
        self.assertEqual(1, report["changed"])
        self.assertEqual("high", report["changes"][0]["confidence"])
        self.assertTrue(report["changes"][0]["automaticMatch"])

    def test_metadata_only_candidate_is_low_confidence_and_observational(self):
        old = station("old-id", "Example Radio")
        new = station("new-id", "Example Radio")
        report = build_continuity_report([old], [new], 1, 2)
        self.assertEqual("low", report["changes"][0]["confidence"])
        self.assertFalse(report["changes"][0]["automaticMatch"])
        self.assertIn("no station IDs are rewritten", report["policy"])

    def test_ambiguous_matches_are_not_claimed_as_added_or_removed(self):
        old = station("old-id", "Example Radio")
        new_a = station("new-a", "Example Radio")
        new_b = station("new-b", "Example Radio")
        report = build_continuity_report([old], [new_a, new_b], 1, 2)
        self.assertEqual(1, report["ambiguousPrevious"])
        self.assertEqual(2, report["ambiguousCurrent"])
        self.assertEqual(0, report["added"])
        self.assertEqual(0, report["removed"])

    def test_incompatible_country_is_not_a_candidate(self):
        old = station("old-id", "Example Radio", country="GB")
        new = station("new-id", "Example Radio", country="US")
        report = build_continuity_report([old], [new], 1, 2)
        self.assertEqual(1, report["removed"])
        self.assertEqual(1, report["added"])
        self.assertEqual([], report["changes"])


if __name__ == "__main__":
    unittest.main()
