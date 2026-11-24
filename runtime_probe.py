"""Opt-in, cost-limited checks of complete tests on independent patched trees.

This experiment uses ordinary pytest execution. It does not reduce test bodies,
replace functions in live modules, replay answers, or read evaluation labels.
"""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from process_runner import run_bounded
from unified_diff import parse_patch, reconstruct


class ProbeBudget:
    def __init__(self, wall_seconds=15.0, per_patch_seconds=3.0):
        self.remaining = max(0.0, wall_seconds)
        self.per_patch = max(0.0, per_patch_seconds)

    def refine(self, baseline, patch, candidates, tests, simulated_seconds):
        keep = set(candidates)
        details = {"status": "not_attempted", "removed": 0}
        chosen = [test for test in tests if test["number"] in keep]
        # Spend CPU only where there is a plausible saving. The catalog is an
        # estimate, not a guarantee of runtime on the current machine.
        estimate = 0.25 + sum(test["seconds"] for test in chosen)
        budget = min(self.remaining, self.per_patch, simulated_seconds / 4)
        if not chosen or budget < 0.25 or estimate > budget:
            details["status"] = "cost_budget"
            return keep, details
        if importlib.util.find_spec("pytest") is None:
            details["status"] = "pytest_unavailable"
            return keep, details
        # Reserve most of the 64-MB tmpfs for pytest/native test temporary data.
        if sum(map(len, baseline.values())) > 24_000_000:
            details["status"] = "source_too_large"
            return keep, details
        started = time.monotonic()
        try:
            changed = {}
            for record in parse_patch(patch):
                if record.action != "modify" or record.old_path not in baseline:
                    details["status"] = "unsupported_file_action"
                    return keep, details
                changed[record.old_path] = reconstruct(record, baseline[record.old_path])[0]
            with tempfile.TemporaryDirectory(prefix="selector-probe-") as temporary:
                work = Path(temporary)
                root = work / "source"
                root.mkdir()
                for name, original in baseline.items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("unsafe source path")
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(changed.get(name, original))
                    path.chmod(0o444)
                # Match the read-only source tree used by the scorer. Cleanup
                # restores only directory permissions after all children stop.
                directories = [root] + [p for p in root.rglob("*") if p.is_dir()]
                for directory in directories:
                    directory.chmod(0o555)
                try:
                    request, response = work / "request.json", work / "response.json"
                    request.write_text(json.dumps({"root": str(root), "tests": [t["id"] for t in chosen]}))
                    environment = dict(os.environ, SKIP_INTEGRATION="0", HOME="/tmp",
                        PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                        PYTHONPATH=str(root), TMPDIR=str(work), PYTHON_CPU_COUNT="2",
                        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
                    for key in list(environment):
                        if key.startswith("SELECTOR_TRACE_"):
                            del environment[key]
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        details["status"] = "staging_deadline"
                        return keep, details
                    result = run_bounded([sys.executable, "-B", str(Path(__file__).with_name("probe_runner.py")),
                                          str(request), str(response)], cwd=root, env=environment,
                                         timeout=remaining, output_limit=262144)
                    details.update(cpu_seconds=result.cpu_seconds, execution_failure=result.failure)
                    if result.failure or result.returncode or not response.exists() or response.stat().st_size > 262144:
                        details["status"] = "incomplete"
                        return keep, details
                    verdicts = json.loads(response.read_text())
                    allowed = {test["id"] for test in chosen}
                    passed = verdicts.get("passed")
                    if verdicts.get("complete") is not True or not isinstance(passed, list) or any(
                            not isinstance(node, str) or node not in allowed for node in passed):
                        raise ValueError("invalid probe verdicts")
                    passed = set(passed)
                    keep -= {test["number"] for test in chosen if test["id"] in passed}
                    details.update(status="complete", removed=len(candidates) - len(keep))
                    return keep, details
                finally:
                    for directory in directories:
                        directory.chmod(0o755)
        except (OSError, ValueError, KeyError, TypeError) as error:
            details.update(status="error", error=type(error).__name__)
            return set(candidates), details
        finally:
            elapsed = time.monotonic() - started
            self.remaining = max(0.0, self.remaining - elapsed)
            details["wall_seconds"] = elapsed
