import json
import sqlite3
import tempfile
import unittest
import zstandard
from pathlib import Path

from sources.external_state import (
    ExternalStateError,
    _create_archive,
    _sha256_file,
    _stage_archive,
)
from sources.state import (
    save_snapshot,
    snapshot_counts,
    validate_provider_anomaly_bounds,
    validate_state,
)


class ExternalSnapshotArchiveTests(unittest.TestCase):
    def _sample_station(self):
        evidence = {
            "source": "iprd",
            "providerRecordId": "provider-record-1",
            "providerStationUuid": "",
            "playlistPath": "stations/se/example.m3u",
            "entryIndex": 0,
            "rawExtinf": '#EXTINF:-1 tvg-id="example",Example Radio',
            "originalTitle": "Example Radio",
            "originalUrl": "https://radio.example/live",
            "streamUrl": "https://radio.example/live",
            "ingestedAt": "2026-11-16T10:00:00+00:00",
        }
        return {
            "name": "Example Radio",
            "normalizedName": "example radio",
            "_provenanceStatus": "fresh",
            "_provenanceStatusBySource": {"iprd": "fresh"},
            "_identityEvidence": [evidence],
            "streams": [{
                "source": "iprd",
                "url": "https://radio.example/live",
                "codec": "AAC",
                "bitrate": 128,
                "sourceRecords": [evidence],
            }],
        }

    def test_jsonl_zstd_archive_stages_station_stream_and_playlist_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            save_snapshot(state_dir, "iprd", [self._sample_station()])
            snapshot = state_dir / "sources" / "iprd.json"
            archive = root / "iprd.jsonl.zst"
            database = root / "staging.sqlite3"

            counts = _create_archive(
                snapshot, archive, "iprd",
                {
                    "minStationCount": 1,
                    "maxStationDropRatio": 0.10,
                    "minStreamCount": 1,
                    "maxStreamDropRatio": 0.15,
                },
            )
            digest = _sha256_file(archive)
            staged = _stage_archive(
                archive, database, "iprd", expected_digest=digest,
                expected_station_count=1, expected_stream_count=1,
            )

            self.assertEqual(1, counts["stationCount"])
            self.assertEqual(1, counts["streamCount"])
            self.assertEqual(counts["stationCount"], staged["stationCount"])
            self.assertEqual(counts["streamCount"], staged["streamCount"])
            connection = sqlite3.connect(database)
            try:
                station = connection.execute(
                    "SELECT provenance_status FROM staged_stations WHERE provider='iprd'"
                ).fetchone()
                stream = connection.execute(
                    """SELECT codec, bitrate, source_record_id, playlist_path,
                       entry_index, provenance_status FROM staged_streams WHERE provider='iprd'"""
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(("fresh",), station)
            self.assertEqual(
                ("AAC", 128, "provider-record-1", "stations/se/example.m3u", 0, "fresh"),
                stream,
            )

    def test_truncated_archive_fails_footer_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            save_snapshot(state_dir, "iprd", [self._sample_station()])
            archive = root / "valid.jsonl.zst"
            broken = root / "truncated.jsonl.zst"
            _create_archive(state_dir / "sources" / "iprd.json", archive, "iprd", {})
            decompressor = zstandard.ZstdDecompressor()
            with archive.open("rb") as source:
                payload = decompressor.decompress(source.read()).decode("utf-8")
            lines = payload.splitlines()
            self.assertEqual("footer", json.loads(lines[-1])["recordType"])
            compressor = zstandard.ZstdCompressor()
            broken.write_bytes(compressor.compress(("\n".join(lines[:-1]) + "\n").encode("utf-8")))
            with self.assertRaisesRegex(ExternalStateError, "footer is missing"):
                _stage_archive(broken, root / "stage.sqlite3", "iprd")

    def test_compressed_checksum_is_verified_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            save_snapshot(state_dir, "iprd", [self._sample_station()])
            archive = root / "snapshot.jsonl.zst"
            _create_archive(state_dir / "sources" / "iprd.json", archive, "iprd", {})
            with self.assertRaisesRegex(ExternalStateError, "SHA-256 mismatch"):
                _stage_archive(
                    archive, root / "stage.sqlite3", "iprd",
                    expected_digest="0" * 64,
                )

    def test_snapshot_counts_and_state_validation_stream_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            save_snapshot(state_dir, "iprd", [self._sample_station()])
            self.assertEqual(
                {"stationCount": 1, "streamCount": 1},
                snapshot_counts(state_dir, "iprd"),
            )
            self.assertEqual((True, []), validate_state(state_dir))

    def test_complete_legacy_playlist_evidence_is_classified_restored_not_fabricated(self):
        station = self._sample_station()
        station.pop("_provenanceStatus", None)
        station.pop("_provenanceStatusBySource", None)
        evidence = station["_identityEvidence"][0]
        evidence.update({
            "sourceUrl": "https://raw.example/IPRD/stations/se/example.m3u",
            "playlistRepository": "iprd-org/IPRD",
        })

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            save_snapshot(state_dir, "iprd", [station])
            archive = root / "iprd.jsonl.zst"
            database = root / "staging.sqlite3"
            _create_archive(state_dir / "sources" / "iprd.json", archive, "iprd", {})
            _stage_archive(archive, database, "iprd")

            connection = sqlite3.connect(database)
            try:
                station_status = connection.execute(
                    "SELECT provenance_status FROM staged_stations WHERE provider='iprd'"
                ).fetchone()[0]
                stream_status = connection.execute(
                    "SELECT provenance_status FROM staged_streams WHERE provider='iprd'"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual("restored-last-good", station_status)
        self.assertEqual("restored-last-good", stream_status)


class ProviderAnomalyBoundTests(unittest.TestCase):
    def _stations(self, station_count, streams_per_station=1):
        return [
            {
                "name": f"Station {index}",
                "streams": [
                    {"url": f"https://station-{index}-{stream}.example/live"}
                    for stream in range(streams_per_station)
                ],
            }
            for index in range(station_count)
        ]

    def test_provider_specific_station_and_stream_bounds_pass(self):
        result = validate_provider_anomaly_bounds(
            "fixture",
            self._stations(90, 2),
            {
                "minStationCount": 80,
                "maxStationDropRatio": 0.10,
                "minStreamCount": 170,
                "maxStreamDropRatio": 0.15,
                "minRecordProductionCoverage": 0.80,
            },
            {"stationCount": 100, "streamCount": 200},
            {"recordProductionCoverageRatio": 0.90},
        )
        self.assertEqual({"stationCount": 90, "streamCount": 180}, result)

    def test_station_drop_is_rejected_independently(self):
        with self.assertRaisesRegex(ValueError, "station count dropped"):
            validate_provider_anomaly_bounds(
                "fixture", self._stations(80),
                {"maxStationDropRatio": 0.10},
                {"stationCount": 100, "streamCount": 80},
            )

    def test_stream_drop_is_rejected_independently(self):
        with self.assertRaisesRegex(ValueError, "stream count dropped"):
            validate_provider_anomaly_bounds(
                "fixture", self._stations(100, 1),
                {"maxStreamDropRatio": 0.15},
                {"stationCount": 100, "streamCount": 200},
            )

    def test_playlist_record_production_coverage_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "record-production coverage"):
            validate_provider_anomaly_bounds(
                "iprd", self._stations(100),
                {"minRecordProductionCoverage": 0.80},
                playlist_refresh={"recordProductionCoverageRatio": 0.79},
            )


if __name__ == "__main__":
    unittest.main()
