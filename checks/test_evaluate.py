"""Integration checks for local input separation and scorer/report semantics."""

import json
import tempfile
import unittest
from pathlib import Path

from evaluate import ROOT, evaluate


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        (self.data / "commits").mkdir(parents=True)
        (self.data / "commits" / "change.patch").write_text("patch")
        (self.data / "tests.json").write_text(json.dumps({"tests": [
            {"number": 0, "seconds": 1.25},
            {"number": 1, "seconds": 0.5},
        ]}))
        self.answers = self.data / "answers.json"
        self.answers.write_text(json.dumps({"commits": {"change": {"failing": [1]}}}))
        self.selector = self.root / "selector.py"

    def run_output(self, output, exit_code=0):
        self.selector.write_text(
            "import sys\nfrom pathlib import Path\n"
            "data = Path(sys.argv[2])\n"
            "assert sys.argv[1] == '--data-dir'\n"
            "assert Path.cwd() == data.resolve()\n"
            "assert {p.name for p in data.iterdir()} == {'tests.json', 'commits'}\n"
            "assert {p.name for p in (data / 'commits').iterdir()} == {'change.patch'}\n"
            f"sys.stdout.buffer.write({output!r})\n"
            f"sys.exit({exit_code})\n"
        )
        return evaluate(self.data, self.answers, self.selector)

    def test_miss_and_empty_worker_counts(self):
        report, _ = self.run_output(b'{"commit":"change","workers":[[],[0.0],[]]}\n')
        self.assertEqual(report["selector_exit_code"], 0)
        self.assertEqual(report["evaluation"], {
            "ok": True, "simulated_milliseconds": 303250, "missed_commits": ["change"],
        })
        self.assertEqual(report["per_commit"]["change"], {"selected_tests": 1, "workers": 1,
                                                          "simulated_milliseconds": 303250, "missed": True,
                                                          "omitted_failing_tests": [1]})
        self.assertEqual(report["selected_tests_total"], 1)
        self.assertEqual(report["workers_total"], 1)
        self.assertGreaterEqual(report["selector_cpu_seconds"], 0)

    def test_multiple_omissions_charge_one_miss_penalty(self):
        self.answers.write_text(json.dumps({"commits": {"change": {"failing": [0, 1]}}}))
        report, _ = self.run_output(b'{"commit":"change","workers":[]}\n')
        self.assertEqual(report["omitted_failing_tests_total"], 2)
        self.assertEqual(report["miss_penalty_milliseconds"], 300000)
        self.assertEqual(report["evaluation"]["simulated_milliseconds"], 300000)

    def test_invalid_output_preserves_official_rejection(self):
        for output, code in [(b"not json\n", "not_json"), (b"\xff", "not_utf8"),
                             (b"", "missing_commit")]:
            with self.subTest(code=code):
                report, raw = self.run_output(output)
                self.assertEqual(raw, output)
                self.assertFalse(report["evaluation"]["ok"])
                self.assertEqual(report["evaluation"]["code"], code)
                self.assertNotIn("selected_tests_total", report)

    def test_nonzero_exit_is_separate_from_output_validity(self):
        report, _ = self.run_output(b'{"commit":"change","workers":[[0,1]]}\n', 7)
        self.assertTrue(report["evaluation"]["ok"])
        self.assertEqual(report["selector_exit_code"], 7)

    def test_answer_mismatch_fails_before_running_selector(self):
        self.answers.write_text('{"commits": {}}')
        with self.assertRaisesRegex(ValueError, "exactly the patch commits"):
            evaluate(self.data, self.answers, self.selector)

    def test_sample_cost_aware_allocation(self):
        report, _ = evaluate(ROOT / "sample", ROOT / "sample/answers.json", ROOT / "selector.py", all_tests=True)
        self.assertEqual(report["selector_exit_code"], 0)
        self.assertEqual(report["evaluation"], {
            "ok": True, "simulated_milliseconds": 1104510, "missed_commits": [],
        })
        self.assertEqual(report["available_tests"], 1620)
        self.assertEqual(report["commit_count"], 30)
        self.assertEqual(report["selected_tests_total"], 48600)
        self.assertEqual(report["workers_total"], 120)
        self.assertTrue(all(counts == {"selected_tests": 1620, "workers": 4,
                                      "simulated_milliseconds": 36817, "missed": False,
                                      "omitted_failing_tests": []}
                            for counts in report["per_commit"].values()))
        self.assertEqual(report["fallback_commits"], 0)


if __name__ == "__main__":
    unittest.main()
