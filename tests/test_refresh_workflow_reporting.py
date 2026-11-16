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
        self.assertIn("run: rm -f state/diagnostics/source-report.json", clear_step)

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


if __name__ == "__main__":
    unittest.main()
