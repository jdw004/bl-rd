"""Synthetic probe safety checks, separate from measured sample accuracy."""

import difflib
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from probe_runner import Outcomes
from runtime_probe import ProbeBudget


class ProbeTests(unittest.TestCase):
    def execute(self, body, changed="return 2", timeout=4, extra=None):
        original = b"def value():\n    return 1\n"
        modified = ("def value():\n    " + changed + "\n").encode()
        patch = "".join(difflib.unified_diff(original.decode().splitlines(True),
            modified.decode().splitlines(True), fromfile="a/core.py", tofile="b/core.py")).encode()
        baseline = {"core.py": original, "tests/test_core.py": body.encode(), **(extra or {})}
        names = [line.split("(")[0][4:] for line in body.splitlines() if line.startswith("def test_")]
        tests = [{"number": i, "id": "tests/test_core.py::" + name, "seconds": 0.001}
                 for i, name in enumerate(names)]
        candidates = set(range(len(tests)))
        before = baseline.copy()
        keep, details = ProbeBudget(10, timeout).refine(baseline, patch, candidates, tests, 100)
        self.assertEqual(baseline, before)
        return keep, details

    def test_remove_only_passing_tests(self):
        keep, details = self.execute("from core import value\ndef test_pass(): assert value() == 2\ndef test_fail(): assert value() == 1\n")
        self.assertEqual(keep, {1}, details)
        self.assertEqual(details["status"], "complete")

    def test_fixture_teardown_failure_keeps_prior_passes(self):
        keep, details = self.execute("import pytest\n@pytest.fixture(scope='module', autouse=True)\ndef shared():\n    yield\n    assert False\ndef test_one(): pass\ndef test_two(): pass\n")
        self.assertEqual(keep, {0, 1}, details)

    def test_import_failure_retains_every_candidate(self):
        keep, details = self.execute("import missing_probe_dependency\ndef test_one(): pass\n")
        self.assertEqual(keep, {0}, details)

    def test_timeout_keeps_candidates(self):
        keep, details = self.execute("import time\ndef test_hang(): time.sleep(30)\n", timeout=0.4)
        self.assertEqual(keep, {0}, details)
        self.assertEqual(details.get("execution_failure"), "deadline_exceeded", details)

    def test_timed_out_probe_cleans_up_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            pidfile = Path(temporary) / "child.pid"
            heartbeat = Path(temporary) / "heartbeat"
            child_code = ("from pathlib import Path\nimport time\n"
                          f"p = Path({str(heartbeat)!r})\n"
                          "while True:\n p.write_text(str(time.monotonic_ns()))\n time.sleep(.01)\n")
            body = ("import subprocess, sys, time\nfrom pathlib import Path\n"
                    "def test_child():\n"
                    f"    child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                    f"    Path({str(pidfile)!r}).write_text(str(child.pid))\n"
                    "    time.sleep(30)\n")
            keep, details = self.execute(body, timeout=1)
            self.assertEqual(keep, {0}, details)
            self.assertTrue(pidfile.exists(), details)
            pid = int(pidfile.read_text())
            self.assertTrue(heartbeat.exists(), details)
            before = heartbeat.read_text()
            time.sleep(.1)
            after = heartbeat.read_text()
            if before != after:
                os.kill(pid, 9)
                self.fail('probe descendant continued executing after group cleanup')

    def test_real_subtest_failure_is_retained(self):
        keep, details = self.execute("def test_subtests(subtests):\n    with subtests.test(case='bad'):\n        assert False\n    with subtests.test(case='good'):\n        assert True\n")
        self.assertEqual(keep, {0}, details)
        self.assertEqual(details["status"], "complete")

    def test_parametrized_ids_remain_distinct(self):
        baseline = {"core.py": b"value = 1\n", "tests/test_params.py": b"import pytest\nfrom core import value\n@pytest.mark.parametrize('expected', [1, 2])\ndef test_value(expected): assert value == expected\n"}
        patch = b"--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
        tests = [{"number": n, "id": f"tests/test_params.py::test_value[{n+1}]", "seconds": .001} for n in range(2)]
        keep, details = ProbeBudget(10, 4).refine(baseline, patch, {0, 1}, tests, 100)
        self.assertEqual(keep, {0}, details)

    def test_child_interpreter_reads_same_patched_tree(self):
        keep, details = self.execute("import subprocess, sys, os\ndef test_child():\n    assert os.environ['SKIP_INTEGRATION'] == '0'\n    subprocess.run([sys.executable, '-c', 'from core import value; assert value() == 2'], check=True)\n")
        self.assertEqual(keep, set(), details)

    def test_fixture_file_patch_seen_by_reader(self):
        original, modified = b"old\n", b"new\n"
        patch = "".join(difflib.unified_diff(original.decode().splitlines(True), modified.decode().splitlines(True),
                        fromfile="a/data.txt", tofile="b/data.txt")).encode()
        baseline = {"data.txt": original, "tests/test_data.py": b"from pathlib import Path\ndef test_data(): assert Path('data.txt').read_text() == 'new\\n'\n"}
        tests = [{"number": 7, "id": "tests/test_data.py::test_data", "seconds": 0.001}]
        keep, details = ProbeBudget(10, 4).refine(baseline, patch, {7}, tests, 100)
        self.assertEqual(keep, set(), details)

    def test_subtest_failure_cannot_be_overwritten(self):
        result = Outcomes(["test"])
        result.collected = {"test"}
        for phase, outcome in [("setup", "passed"), ("call", "failed"), ("call", "passed"), ("teardown", "passed")]:
            result.pytest_runtest_logreport(SimpleNamespace(nodeid="test", when=phase, outcome=outcome))
        self.assertEqual(result.passed(1), [])


if __name__ == "__main__":
    unittest.main()
