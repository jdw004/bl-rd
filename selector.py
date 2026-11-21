"""Select tests from pinned dependency evidence, then minimize worker cost."""

import argparse
import json
import math
import sys
from pathlib import Path

from dependency_map import DependencyMap
from patch_analysis import PatchAnalyzer
from selection import TestSelector

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_COST_MILLISECONDS = 2_000


def duration_milliseconds(seconds: float) -> int:
    """Match score.py's half-up conversion without importing evaluation code."""
    return math.floor(seconds * 1000 + 0.5)


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """LPT in milliseconds; ties use test number, then lowest worker index."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test in sorted(
        tests,
        key=lambda test: (-duration_milliseconds(test["seconds"]), test["number"]),
    ):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += duration_milliseconds(test["seconds"])
    return workers


def allocate(tests: list[dict]) -> list[list[int]]:
    """Choose the cheapest LPT schedule, preferring fewer workers on cost ties.

    Input is a selected set with unique test numbers, as in tests.json.
    """
    if not tests:
        return []
    durations = {
        test["number"]: duration_milliseconds(test["seconds"]) for test in tests
    }
    candidates = []
    for worker_count in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers = [worker for worker in split(tests, worker_count) if worker]
        cost = max(sum(durations[number] for number in worker) for worker in workers)
        cost += WORKER_COST_MILLISECONDS * len(workers)
        candidates.append((cost, len(workers), workers))
    # Lists provide a final deterministic lexicographic tie-break if needed.
    return min(candidates)[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--dependency-map", type=Path,
                        default=Path(__file__).resolve().parent / "maps")
    parser.add_argument("--all-tests", action="store_true", help="run the full-suite comparison baseline")
    parser.add_argument("--strategy", choices=("all", "file", "function", "hybrid"), default="hybrid")
    options = parser.parse_args()
    strategy = "all" if options.all_tests else options.strategy
    tests = json.loads((options.data_dir / "tests.json").read_text())["tests"]
    analyzer = None
    selector = None
    if strategy == "all":
        print("full-suite baseline requested", file=sys.stderr)
    elif options.dependency_map.exists():
        try:
            dependencies = DependencyMap(options.dependency_map, tests)
            print(dependencies.summary(), file=sys.stderr)
            analyzer = PatchAnalyzer.from_data(dependencies.data)
            selector = TestSelector(dependencies, tests, strategy=strategy)
        except (OSError, ValueError, KeyError, IndexError, TypeError, EOFError) as error:
            print(f"dependency map unavailable: {error}; selecting all tests", file=sys.stderr)
    else:
        print("dependency map absent; selecting all tests", file=sys.stderr)
    all_numbers = {test["number"] for test in tests}
    schedules = {}
    selected_total = commit_count = 0
    for patch in sorted((options.data_dir / "commits").glob("*.patch")):
        selected = all_numbers
        decision = {"mode": "all", "reasons": ["baseline_requested" if strategy == "all" else "maps_unavailable"]}
        if analyzer is not None and selector is not None:
            change = analyzer.analyze(patch.read_bytes())
            selected, decision = selector.select(change)
        key = tuple(sorted(selected))
        if key not in schedules:
            schedules[key] = allocate([test for test in tests if test["number"] in selected])
        workers = schedules[key]
        print(json.dumps({"commit": patch.stem, "strategy": strategy,
                          "fallback": strategy != "all" and decision["mode"] == "all",
                          "uncertain_tests": len(dependencies.uncertain_numbers) if selector else 0,
                          "selected_tests": len(selected), **decision}), file=sys.stderr)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        selected_total += len(selected)
        commit_count += 1
    print(f"selected {selected_total} tests across {commit_count} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
