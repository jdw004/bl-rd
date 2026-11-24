"""Optional isolated pytest execution; no evaluation labels are available here."""

import json
import os
from pathlib import Path
import sys


class Outcomes:
    def __init__(self, requested):
        self.requested = set(requested)
        self.collected = set()
        self.phases = {}
        self.collection_failed = False

    def pytest_collection_finish(self, session):
        self.collected = {item.nodeid for item in session.items}

    def pytest_collectreport(self, report):
        self.collection_failed |= report.failed

    def pytest_runtest_logreport(self, report):
        phases = self.phases.setdefault(report.nodeid, {})
        previous = phases.get(report.when)
        # A successful parent report must not erase a failing subtest.
        if previous not in {"failed", "skipped"}:
            phases[report.when] = report.outcome

    def passed(self, exit_code):
        if self.collection_failed or self.collected != self.requested or exit_code not in (0, 1):
            return []
        # Any setup/teardown failure can invalidate earlier tests sharing a
        # fixture, including tests whose own teardown already passed.
        if any(p.get(phase) == "failed" for p in self.phases.values()
               for phase in ("setup", "teardown")):
            return []
        return sorted(node for node in self.requested
                      if self.phases.get(node) == {"setup": "passed", "call": "passed", "teardown": "passed"})


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    destination = Path(sys.argv[2])
    os.chdir(request["root"])
    sys.path.insert(0, request["root"])
    sys.dont_write_bytecode = True
    import pytest
    outcomes = Outcomes(request["tests"])
    code = int(pytest.main(request["tests"] + ["-q", "--tb=no", "-o", "addopts=",
                          "-p", "no:cacheprovider"], plugins=[outcomes]))
    # Write only after pytest finishes session/fixture cleanup. Timeouts and
    # interpreter crashes cannot leave a partial set of passing verdicts.
    destination.write_text(json.dumps({"complete": True, "passed": outcomes.passed(code)}))


if __name__ == "__main__":
    main()
