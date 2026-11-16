import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

from sources import retention
from sources.state import save_snapshot


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.listed_prefixes = []

    def _missing(self, operation):
        return ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            operation,
        )

    def head_object(self, Bucket, Key):
        item = self.objects.get(Key)
        if item is None:
            raise self._missing("HeadObject")
        return {
            "ContentLength": len(item["body"]),
            "Metadata": dict(item["metadata"]),
            "LastModified": item["modified"],
        }

    def put_object(self, Bucket, Key, Body, ContentLength=None, Metadata=None, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        payload = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[Key] = {
            "body": payload,
            "metadata": dict(Metadata or {}),
            "modified": datetime.now(timezone.utc),
        }
        return {}

    def download_file(self, Bucket, Key, Destination):
        item = self.objects.get(Key)
        if item is None:
            raise self._missing("GetObject")
        Path(Destination).write_bytes(item["body"])

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self.listed_prefixes.append(Prefix)
        contents = [
            {"Key": key, "LastModified": value["modified"]}
            for key, value in self.objects.items()
            if key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


class RetentionLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "sources.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.client = FakeS3()
        self.context = patch(
            "sources.retention._s3_context",
            return_value=(self.client, "fixture-bucket", "radsync/state", "radsync/diagnostics"),
        )
        self.context.start()

    def tearDown(self):
        self.context.stop()
        self.temporary.cleanup()

    @staticmethod
    def _station(version):
        slug = version.lower().replace(" ", "-")
        return {
            "name": version,
            "normalizedName": slug,
            "streams": [{
                "source": "fixture",
                "url": f"https://{slug}.example/live",
                "codec": "AAC",
                "bitrate": 128,
            }],
        }

    def _save(self, version):
        save_snapshot(self.state_dir, "fixture", [self._station(version)])

    def _publish(self, version, run_id):
        self._save(version)
        return retention.publish_external_snapshots(
            self.state_dir,
            str(self.config_path),
            run_id=run_id,
        )

    def _manifest(self):
        return json.loads(
            (self.state_dir / "manifests" / "providers" / "fixture.json").read_text(encoding="utf-8")
        )

    def test_first_second_and_third_successful_promotions_rotate_versions(self):
        self._publish("Version One", "1")
        first = self._manifest()
        first_current = first["objectKey"]
        self.assertTrue(first_current.startswith("radsync/state/current/fixture/"))
        self.assertNotIn("previous", first)

        self._publish("Version Two", "2")
        second = self._manifest()
        second_current = second["objectKey"]
        second_previous = second["previous"]["objectKey"]
        self.assertTrue(second_current.startswith("radsync/state/current/fixture/"))
        self.assertTrue(second_previous.startswith("radsync/state/previous/fixture/"))
        self.assertIn(first_current, self.client.objects)
        self.assertIn(first_current, self.client.objects)
        self.assertIn(second_previous, self.client.objects)
        self.assertIn(first_current, [item["deleteKey"] for item in second["rotationCleanup"]])

        # The old current remains available until the manifest is known to be
        # pushed. Finalization then moves it out of current/ by deleting the
        # superseded source key after checking its previous/ replacement.
        finalized = retention.finalize_external_rotation(self.state_dir)
        self.assertGreaterEqual(finalized["finalized"], 1)
        self.assertNotIn(first_current, self.client.objects)
        self.assertIn(second_previous, self.client.objects)

        self._publish("Version Three", "3")
        third = self._manifest()
        third_current = third["objectKey"]
        third_previous = third["previous"]["objectKey"]
        archive_keys = [
            key for key in self.client.objects
            if key.startswith("radsync/state/archive/fixture/")
        ]
        self.assertEqual(1, len(archive_keys))
        old_previous = second_previous
        self.assertIn(old_previous, self.client.objects)
        self.assertIn(old_previous, [item["deleteKey"] for item in third["rotationCleanup"]])
        self.assertIn(third_previous, self.client.objects)
        self.assertIn(third_current, self.client.objects)

        retention.finalize_external_rotation(self.state_dir)
        self.assertNotIn(old_previous, self.client.objects)
        self.assertIn(archive_keys[0], self.client.objects)
        self.assertIn(third_current, self.client.objects)
        self.assertIn(third_previous, self.client.objects)
        self.assertNotIn("rotationCleanup", self._manifest())

    def test_failed_candidate_verification_does_not_change_current_or_previous(self):
        self._publish("Version One", "1")
        self._publish("Version Two", "2")
        manifest_before = (self.state_dir / "manifests/providers/fixture.json").read_text(encoding="utf-8")
        protected_before = {
            key: dict(value) for key, value in self.client.objects.items()
            if key.startswith("radsync/state/current/") or key.startswith("radsync/state/previous/")
        }

        self._save("Version Three")
        with patch.object(
            retention.base,
            "_stage_archive",
            side_effect=retention.base.ExternalStateError("forced candidate verification failure"),
        ):
            with self.assertRaisesRegex(retention.base.ExternalStateError, "forced candidate verification failure"):
                retention.publish_external_snapshots(self.state_dir, str(self.config_path), run_id="3")

        self.assertEqual(
            manifest_before,
            (self.state_dir / "manifests/providers/fixture.json").read_text(encoding="utf-8"),
        )
        protected_after = {
            key: dict(value) for key, value in self.client.objects.items()
            if key.startswith("radsync/state/current/") or key.startswith("radsync/state/previous/")
        }
        self.assertEqual(protected_before, protected_after)
        self.assertTrue((self.state_dir / "sources/fixture.json").exists())

    def test_corrupt_current_falls_back_to_verified_previous(self):
        self._publish("Version One", "1")
        self._publish("Version Two", "2")
        manifest = self._manifest()
        current_key = manifest["objectKey"]
        self.client.objects[current_key]["body"] = b"not the original compressed object"

        result = retention.restore_external_snapshots(self.state_dir, str(self.config_path))
        self.assertEqual(["fixture"], result["fallbackProviders"])
        restored = json.loads((self.state_dir / "sources/fixture.json").read_text(encoding="utf-8"))
        self.assertEqual("Version One", restored["stations"][0]["name"])

    def test_missing_current_falls_back_to_verified_previous(self):
        self._publish("Version One", "1")
        self._publish("Version Two", "2")
        current_key = self._manifest()["objectKey"]
        del self.client.objects[current_key]

        result = retention.restore_external_snapshots(self.state_dir, str(self.config_path))
        self.assertEqual(["fixture"], result["fallbackProviders"])
        restored = json.loads((self.state_dir / "sources/fixture.json").read_text(encoding="utf-8"))
        self.assertEqual("Version One", restored["stations"][0]["name"])

    def test_missing_current_and_previous_preserve_existing_local_last_good(self):
        self._publish("Version One", "1")
        self._publish("Version Two", "2")
        manifest = self._manifest()
        self.client.objects.pop(manifest["objectKey"], None)
        self.client.objects.pop(manifest["previous"]["objectKey"], None)
        self._save("Local Last Good")

        before = (self.state_dir / "sources/fixture.json").read_text(encoding="utf-8")
        with self.assertRaisesRegex(retention.base.ExternalStateError, "current and previous snapshots"):
            retention.restore_external_snapshots(self.state_dir, str(self.config_path))
        after = (self.state_dir / "sources/fixture.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_pruning_lists_and_deletes_archive_only(self):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        keys = {
            "radsync/state/current/fixture/active.jsonl.zst",
            "radsync/state/previous/fixture/previous.jsonl.zst",
            "radsync/state/archive/fixture/old.jsonl.zst",
        }
        for key in keys:
            self.client.objects[key] = {
                "body": b"data",
                "metadata": {"sha256": "abcd"},
                "modified": old,
            }

        preview = retention.prune_external_archive_objects(90, apply=False)
        self.assertEqual(["radsync/state/archive/fixture/old.jsonl.zst"], preview["eligible"])
        self.assertEqual(["radsync/state/archive/"], self.client.listed_prefixes)
        self.assertEqual(keys, set(self.client.objects))

        applied = retention.prune_external_archive_objects(90, apply=True)
        self.assertEqual(preview["eligible"], applied["deleted"])
        self.assertIn("radsync/state/current/fixture/active.jsonl.zst", self.client.objects)
        self.assertIn("radsync/state/previous/fixture/previous.jsonl.zst", self.client.objects)
        self.assertNotIn("radsync/state/archive/fixture/old.jsonl.zst", self.client.objects)

    def test_diagnostics_use_separate_prefix_and_report_is_retrievable(self):
        report = self.root / "source-report.json"
        ambiguity = self.root / "identity-ambiguity.json"
        report.write_text(json.dumps({"schemaVersion": 1, "buildStatus": "success", "sources": []}), encoding="utf-8")
        ambiguity.write_text(json.dumps({"schemaVersion": 1, "groups": []}), encoding="utf-8")

        self._save("Version One")
        result = retention.publish_external_snapshots(
            self.state_dir,
            str(self.config_path),
            report_path=report,
            run_id="42",
            ambiguity_path=ambiguity,
        )
        self.assertTrue(result["reportPublished"])
        self.assertTrue(result["ambiguityPublished"])

        report_manifest = json.loads(
            (self.state_dir / "manifests/reports/42.json").read_text(encoding="utf-8")
        )
        ambiguity_manifest = json.loads(
            (self.state_dir / "manifests/diagnostics/42.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report_manifest["objectKey"].startswith("radsync/diagnostics/"))
        self.assertTrue(ambiguity_manifest["objectKey"].startswith("radsync/diagnostics/"))
        self.assertFalse(report_manifest["objectKey"].startswith("radsync/state/"))

        output = self.root / "retrieved-report.json"
        fetched = retention.fetch_external_report(self.state_dir, "42", output)
        self.assertEqual("42", fetched["runId"])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), json.loads(output.read_text(encoding="utf-8")))

    def test_diagnostics_and_state_prefixes_must_not_overlap(self):
        with patch("sources.retention.base._s3_context", return_value=(
            self.client, "fixture-bucket", "radsync/state"
        )), patch.dict("os.environ", {"RADSYNC_DIAGNOSTICS_PREFIX": "radsync/state/diagnostics"}):
            with self.assertRaisesRegex(retention.base.ExternalStateError, "must be separate"):
                retention._s3_context()


if __name__ == "__main__":
    unittest.main()
