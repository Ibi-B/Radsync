from pathlib import Path
import unittest


class RefreshWorkflowReportingTests(unittest.TestCase):
    def test_prior_state_report_is_removed_before_setup_and_regression_tests(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")

        checkout = workflow.index("id: state_checkout")
        clear = workflow.index("id: clear_previous_refresh_report")
        setup = workflow.index("uses: actions/setup-python@v5")
        regression_tests = workflow.index("name: Run regression tests")

        self.assertLess(checkout, clear)
        self.assertLess(clear, setup)
        self.assertLess(clear, regression_tests)

        clear_step = workflow[clear:setup]
        self.assertIn("run: mkdir -p state/diagnostics && rm -f state/diagnostics/source-report.json", clear_step)

        persist_start = workflow.index("- name: Persist externally verified provider snapshots")
        upload_start = workflow.index("- name: Upload refresh report", persist_start)
        persist_step = workflow[persist_start:upload_start]
        self.assertIn("steps.build_catalogue.outcome != 'skipped'", persist_step)
        self.assertIn("if [ ! -f state/diagnostics/source-report.json ]; then", persist_step)
        self.assertIn("preserving the previous provider state", persist_step)

    def test_initial_external_migration_skips_manifest_staging_when_directory_is_absent(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")
        start = workflow.index("name: Restore externally stored provider snapshots")
        end = workflow.index("name: Validate restored provider state", start)
        restore_step = workflow[start:end]

        self.assertIn("if [ -d state/manifests/providers ]; then", restore_step)
        self.assertIn("git -C state add --all -- manifests/providers", restore_step)
        self.assertLess(
            restore_step.index("fi\n          python -m sources.state_cli restore-external"),
            len(restore_step),
        )

    def test_external_refresh_report_is_fetched_back_through_cli_after_publish(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")
        persist = workflow.index("- name: Persist externally verified provider snapshots")
        verify = workflow.index("- name: Verify external refresh report CLI retrieval")
        upload = workflow.index("- name: Upload identity ambiguity diagnostics")
        persist_step = workflow[persist:verify]
        verify_step = workflow[verify:upload]

        self.assertIn("id: persist_external", persist_step)
        self.assertLess(
            persist_step.index("git -C state push origin HEAD:refs/heads/radsync-state"),
            verify,
        )
        self.assertIn("steps.persist_external.outcome == 'success'", verify_step)
        self.assertIn("fetch-external-report", verify_step)
        self.assertIn("cmp --silent state/diagnostics/source-report.json verification/source-report.json", verify_step)
        self.assertIn("RADSYNC_S3_BUCKET: ${{ vars.RADSYNC_S3_BUCKET }}", verify_step)
        self.assertIn("RADSYNC_S3_ACCESS_KEY_ID: ${{ secrets.RADSYNC_S3_ACCESS_KEY_ID }}", verify_step)
        self.assertIn("RADSYNC_S3_SECRET_ACCESS_KEY: ${{ secrets.RADSYNC_S3_SECRET_ACCESS_KEY }}", verify_step)

    def test_refresh_report_upload_requires_build_attempt_and_runs_after_build_failure(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")
        start = workflow.index("- name: Upload refresh report")
        end = workflow.index("- name: Upload Pages artifact", start)
        upload_step = workflow[start:end]

        self.assertIn("always()", upload_step)
        self.assertIn("steps.state_checkout.outcome == 'success'", upload_step)
        self.assertIn("steps.build_catalogue.outcome != 'skipped'", upload_step)
        self.assertIn("if-no-files-found: warn", upload_step)
        self.assertIn("retention-days: 30", upload_step)

    def test_external_snapshot_restore_precedes_validation_and_publish_precedes_git_commit(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")

        install = workflow.index("name: Install dependencies")
        restore = workflow.index("name: Restore externally stored provider snapshots")
        validate = workflow.index("name: Validate restored provider state")
        build = workflow.index("name: Build catalogue using fresh or last-good provider data")
        persist = workflow.index("name: Persist externally verified provider snapshots")
        artifact = workflow.index("name: Upload refresh report")

        self.assertLess(install, restore)
        self.assertLess(restore, validate)
        self.assertLess(validate, build)
        self.assertLess(build, persist)
        self.assertLess(persist, artifact)

        persist_step = workflow[persist:artifact]
        self.assertIn("publish-external", persist_step)
        self.assertIn("git -C state push origin HEAD:refs/heads/radsync-state", persist_step)
        self.assertIn(":(exclude)diagnostics/source-report.json", persist_step)
        self.assertIn(":(exclude)diagnostics/identity-ambiguity.json", persist_step)
        self.assertIn("diagnostics/source-report.json diagnostics/identity-ambiguity.json", persist_step)
        self.assertIn("provider state was not advanced", persist_step)
        self.assertIn("--ambiguity state/diagnostics/identity-ambiguity.json", persist_step)
        self.assertIn("finalize-external --state-dir state --manifest-pushed", persist_step)
        self.assertLess(
            persist_step.index("git -C state push origin HEAD:refs/heads/radsync-state"),
            persist_step.index("finalize-external --state-dir state"),
        )

        restore_step = workflow[restore:validate]
        self.assertIn("finalize-external --state-dir state --manifest-pushed", restore_step)
        self.assertLess(
            restore_step.index("finalize-external --state-dir state"),
            restore_step.index("restore-external --state-dir state"),
        )

    def test_retention_documentation_separates_protected_and_expiring_prefixes(self):
        documentation = Path("docs/EXTERNAL_PROVIDER_STATE.md").read_text(encoding="utf-8")
        self.assertIn("radsync/state/current/", documentation)
        self.assertIn("radsync/state/previous/", documentation)
        self.assertIn("radsync/state/archive/", documentation)
        self.assertIn("radsync/diagnostics/", documentation)
        self.assertIn("Expire after 30 days", documentation)
        self.assertIn("Expire after 30 days", documentation)
        self.assertIn("Bucket Lock: Off", documentation)
        self.assertNotIn("indefinite retention rule", documentation.lower())
        self.assertIn("do not create a bucket-wide", documentation.lower())

    def test_external_storage_credentials_are_environment_only(self):
        workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")
        self.assertIn("RADSYNC_S3_BUCKET: ${{ vars.RADSYNC_S3_BUCKET }}", workflow)
        self.assertIn("RADSYNC_S3_ACCESS_KEY_ID: ${{ secrets.RADSYNC_S3_ACCESS_KEY_ID }}", workflow)
        self.assertIn("RADSYNC_S3_SECRET_ACCESS_KEY: ${{ secrets.RADSYNC_S3_SECRET_ACCESS_KEY }}", workflow)
        self.assertIn("RADSYNC_DIAGNOSTICS_PREFIX: ${{ vars.RADSYNC_DIAGNOSTICS_PREFIX }}", workflow)


if __name__ == "__main__":
    unittest.main()
