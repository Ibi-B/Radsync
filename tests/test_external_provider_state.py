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
                reader = decompressor.stream_reader(source)
                payload = reader.read().decode("utf-8")
                reader.close()
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


class SnapshotStoreAdapterTests(unittest.TestCase):
    def _station(self):
        return {
            "name": "Adapter Radio",
            "streams": [{"source": "fixture", "url": "https://adapter.example/live"}],
        }

    def test_local_adapter_supports_save_load_verify_and_safe_prune(self):
        from sources.snapshot_store import LocalFileSnapshotStore, SnapshotStore

        with tempfile.TemporaryDirectory() as temporary:
            store = LocalFileSnapshotStore(temporary)
            self.assertIsInstance(store, SnapshotStore)
            saved = store.save_snapshot("fixture", [self._station()])
            self.assertEqual("fixture", saved["source"])
            self.assertEqual("Adapter Radio", store.load_snapshot("fixture")["stations"][0]["name"])
            verified = store.verify_snapshot("fixture")
            self.assertEqual({"stationCount": 1, "streamCount": 1}, {
                "stationCount": verified["stationCount"],
                "streamCount": verified["streamCount"],
            })

            temp_file = Path(temporary) / "sources" / "fixture.json.tmp"
            temp_file.write_text("partial", encoding="utf-8")
            preview = store.prune_snapshots("fixture")
            self.assertEqual([str(temp_file)], preview)
            self.assertTrue(temp_file.exists())
            self.assertEqual(preview, store.prune_snapshots("fixture", apply=True))
            self.assertFalse(temp_file.exists())
            self.assertIsNotNone(store.load_snapshot("fixture"))


class ExternalReportRetrievalTests(unittest.TestCase):
    def test_report_pointer_download_checks_hash_then_decompresses_json(self):
        import hashlib
        import shutil
        from unittest.mock import patch
        from sources.external_state import fetch_external_report

        class FakeS3:
            def __init__(self, object_path):
                self.object_path = object_path

            def download_file(self, bucket, key, destination):
                self.assert_pointer(bucket, key)
                shutil.copyfile(self.object_path, destination)

            @staticmethod
            def assert_pointer(bucket, key):
                if bucket != "fixture-bucket" or key != "radsync/state/reports/123/sha256/report.json.zst":
                    raise AssertionError("Unexpected external report object pointer")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            manifest_dir = state_dir / "manifests" / "reports"
            manifest_dir.mkdir(parents=True)
            payload = b'{"schemaVersion":1,"buildStatus":"failed","sources":[]}'
            archive = root / "report.json.zst"
            archive.write_bytes(zstandard.ZstdCompressor().compress(payload))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (manifest_dir / "123.json").write_text(json.dumps({
                "schemaVersion": 1,
                "format": "json-zstd",
                "runId": "123",
                "objectKey": "radsync/state/reports/123/sha256/report.json.zst",
                "sha256": digest,
                "compressedBytes": archive.stat().st_size,
            }), encoding="utf-8")
            fake = FakeS3(archive)
            output = root / "retrieved" / "source-report.json"

            with patch("sources.external_state._s3_context", return_value=(fake, "fixture-bucket", "radsync/state")):
                result = fetch_external_report(state_dir, "123", output)

            self.assertEqual("123", result["runId"])
            self.assertEqual(json.loads(payload), json.loads(output.read_text(encoding="utf-8")))

    def test_report_download_hash_mismatch_does_not_create_output(self):
        import shutil
        from unittest.mock import patch
        from sources.external_state import ExternalStateError, fetch_external_report

        class FakeS3:
            def __init__(self, object_path):
                self.object_path = object_path

            def download_file(self, bucket, key, destination):
                shutil.copyfile(self.object_path, destination)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            manifest_dir = state_dir / "manifests" / "reports"
            manifest_dir.mkdir(parents=True)
            archive = root / "report.json.zst"
            archive.write_bytes(zstandard.ZstdCompressor().compress(b'{"ok":true}'))
            (manifest_dir / "123.json").write_text(json.dumps({
                "schemaVersion": 1,
                "format": "json-zstd",
                "runId": "123",
                "objectKey": "radsync/state/reports/123/sha256/report.json.zst",
                "sha256": "0" * 64,
            }), encoding="utf-8")
            output = root / "retrieved" / "source-report.json"

            with patch("sources.external_state._s3_context", return_value=(FakeS3(archive), "fixture-bucket", "radsync/state")):
                with self.assertRaisesRegex(ExternalStateError, "checksum mismatch"):
                    fetch_external_report(state_dir, "123", output)

            self.assertFalse(output.exists())

class ImmutableObjectUploadTests(unittest.TestCase):
    def test_upload_uses_atomic_create_only_condition(self):
        from botocore.exceptions import ClientError
        from sources.external_state import _upload_immutable

        class FakeClient:
            def __init__(self):
                self.head_calls = 0
                self.put_args = None

            def head_object(self, Bucket, Key):
                self.head_calls += 1
                if self.head_calls == 1:
                    raise ClientError(
                        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                        "HeadObject",
                    )
                return {"ContentLength": 4, "Metadata": {"sha256": "abcd"}}

            def put_object(self, **kwargs):
                self.put_args = kwargs

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "object.zst"
            path.write_bytes(b"data")
            client = FakeClient()
            result = _upload_immutable(client, "bucket", "prefix/key", path, "abcd")

        self.assertEqual(4, result)
        self.assertEqual("*", client.put_args["IfNoneMatch"])
        self.assertEqual("abcd", client.put_args["Metadata"]["sha256"])

    def test_concurrent_create_only_conflict_accepts_only_matching_object(self):
        from botocore.exceptions import ClientError
        from sources.external_state import _upload_immutable

        class FakeClient:
            def __init__(self, metadata):
                self.head_calls = 0
                self.metadata = metadata

            def head_object(self, Bucket, Key):
                self.head_calls += 1
                if self.head_calls == 1:
                    raise ClientError(
                        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                        "HeadObject",
                    )
                return {"ContentLength": 4, "Metadata": self.metadata}

            def put_object(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "object.zst"
            path.write_bytes(b"data")
            self.assertEqual(
                4,
                _upload_immutable(FakeClient({"sha256": "abcd"}), "bucket", "prefix/key", path, "abcd"),
            )
            with self.assertRaisesRegex(ExternalStateError, "already exists with different content"):
                _upload_immutable(FakeClient({"sha256": "different"}), "bucket", "prefix/key", path, "abcd")


if __name__ == "__main__":
    unittest.main()
