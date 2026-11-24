#!/usr/bin/env python3
"""Run a local selector with unlabeled inputs and evaluate its stdout."""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from score import evaluate_run
from process_runner import run_bounded

ROOT = Path(__file__).resolve().parent


def load_inputs(data_dir, answers_path):
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads(answers_path.read_text("utf-8"))["commits"]
    patches = sorted((data_dir / "commits").glob("*.patch"))
    patch_commits = {patch.stem for patch in patches}
    if patch_commits != set(answers):
        raise ValueError(
            "answers must cover exactly the patch commits: "
            f"{len(patch_commits - set(answers))} patches have no answer; "
            f"{len(set(answers) - patch_commits)} answers have no patch"
        )
    return tests, answers, patches


def evaluate(data_dir: Path, answers_path: Path, selector: Path, all_tests: bool = False,
             strategy: str = "hybrid", probe: bool = False) -> tuple[dict, bytes]:
    tests, answers, patches = load_inputs(data_dir, answers_path)
    strategy = "all" if all_tests else strategy

    # Copy an allowlist, not the data directory: answers stay in this process.
    # This is input separation for local development, not an OS sandbox.
    with tempfile.TemporaryDirectory(prefix="blast-radius-eval-") as temporary:
        staged = Path(temporary)
        (staged / "commits").mkdir()
        shutil.copyfile(data_dir / "tests.json", staged / "tests.json")
        for patch in patches:
            shutil.copyfile(patch, staged / "commits" / patch.name)
        process = run_bounded(
            [sys.executable, "-B", str(selector.resolve()), "--data-dir", str(staged), "--strategy", strategy]
            + (["--probe"] if probe else []),
            cwd=staged,
        )
    report = summarize(process.stdout, process.stderr, tests, answers)
    report.update(strategy=strategy, probe=probe, selector_exit_code=process.returncode,
                  selector_cpu_seconds=process.cpu_seconds, wall_seconds=process.wall_seconds,
                  execution_failure=process.failure)
    if process.stderr:
        print(process.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
    return report, process.stdout


def summarize(stdout, stderr, tests, answers):
    """Score only in evaluation code; labels never enter selector arguments."""
    evaluation = evaluate_run(stdout, tests, answers)
    report = {
        "evaluation": evaluation,
        "available_tests": len(tests),
        "commit_count": len(answers),
        "stdout_bytes": len(stdout),
    }
    decisions = {}
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("commit") in answers and "fallback" in row:
            decisions[row["commit"]] = row
    report["decisions"] = decisions
    report["decision_records_complete"] = set(decisions) == set(answers)
    report["fallback_commits"] = sum(bool(row["fallback"]) for row in decisions.values()) if report["decision_records_complete"] else None
    report["fallback_frequency"] = report["fallback_commits"] / len(answers) if answers and report["decision_records_complete"] else None
    report["uncertain_inclusion_commits"] = (sum(row.get("uncertain_tests", 0) > 0 for row in decisions.values())
                                             if report["decision_records_complete"] else None)
    if evaluation["ok"]:
        # Parse only after the official API accepts the output. Empty workers
        # do not count, matching score.py's normalization.
        counts = {}
        lines = stdout.decode("utf-8").split("\n")
        if lines[-1] == "":
            lines.pop()
        for line in lines:
            selection = json.loads(line)
            occupied = [worker for worker in selection["workers"] if worker]
            selected = {int(number) for worker in occupied for number in worker}
            single = evaluate_run(line.encode("utf-8"), tests, {selection["commit"]: answers[selection["commit"]]})
            counts[selection["commit"]] = {
                "selected_tests": sum(len(worker) for worker in occupied),
                "workers": len(occupied),
                "simulated_milliseconds": single["simulated_milliseconds"],
                "missed": bool(single["missed_commits"]),
                "omitted_failing_tests": sorted(set(answers[selection["commit"]]["failing"]) - selected),
            }
        report["per_commit"] = dict(sorted(counts.items()))
        report["selected_tests_total"] = sum(c["selected_tests"] for c in counts.values())
        report["workers_total"] = sum(c["workers"] for c in counts.values())
        report["omitted_failing_tests_total"] = sum(len(c["omitted_failing_tests"]) for c in counts.values())
        report["miss_penalty_milliseconds"] = 300_000 * len(evaluation["missed_commits"])
    return report


def print_report(report: dict) -> None:
    evaluation = report["evaluation"]
    print(f"Output validity: {'valid' if evaluation['ok'] else 'invalid'}")
    print(f"Selector exit code: {report['selector_exit_code']}")
    print(f"Available tests: {report['available_tests']}; commits: {report['commit_count']}")
    if evaluation["ok"]:
        missed = evaluation["missed_commits"]
        print(f"Missed commits: {len(missed)} ({', '.join(missed) or 'none'})")
        print(f"Selected tests across commits: {report['selected_tests_total']}")
        print(f"Occupied workers across commits: {report['workers_total']}")
        print(f"Omitted failing tests: {report['omitted_failing_tests_total']}; fallback commits: {report['fallback_commits']}")
        print("Commit             Selected tests   Workers   Simulated s   Missed")
        for commit, counts in report["per_commit"].items():
            print(f"{commit:18} {counts['selected_tests']:14} {counts['workers']:9} "
                  f"{counts['simulated_milliseconds'] / 1000:13.3f}   {counts['missed']}")
        milliseconds = evaluation["simulated_milliseconds"]
        print(f"Simulated cost: {milliseconds // 1000:,}.{milliseconds % 1000:03d} s (includes miss penalties; excludes CPU)")
    else:
        location = "" if evaluation["line"] is None else f" on line {evaluation['line']}"
        print(f"Rejected: {evaluation['code']}{location}")
        print("Misses, selected tests, workers, and simulated cost: unavailable for invalid output")
    print(f"Measured selector CPU: {report['selector_cpu_seconds']:.6f} s (local user + system)")
    print(f"Wall time: {report['wall_seconds']:.6f} s; stdout: {report['stdout_bytes']} bytes; execution failure: {report['execution_failure']}")
    print("Local CPU excludes evaluation; this run is not the official three-run container score.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sample")
    parser.add_argument("--answers", type=Path, help="default: <data-dir>/answers.json")
    parser.add_argument("--output", type=Path, help="save the selector's raw stdout")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--all-tests", action="store_true", help="evaluate the full-suite comparison baseline")
    parser.add_argument("--strategy", choices=("all", "file", "function", "hybrid"), default="hybrid")
    parser.add_argument("--probe", action="store_true", help="evaluate optional runtime probing")
    options = parser.parse_args()
    try:
        report, stdout = evaluate(
            options.data_dir,
            options.answers or options.data_dir / "answers.json",
            ROOT / "selector.py",
            all_tests=options.all_tests,
            strategy=options.strategy,
            probe=options.probe,
        )
        if options.output:
            options.output.write_bytes(stdout)
    except (OSError, ValueError, KeyError) as error:
        print(f"Evaluation error: {error}", file=sys.stderr)
        return 2
    if options.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0 if report["evaluation"]["ok"] and report["selector_exit_code"] == 0 and not report["execution_failure"] else 1


if __name__ == "__main__":
    sys.exit(main())
