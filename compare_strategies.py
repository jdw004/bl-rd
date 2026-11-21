"""Compare selection policies using labels exclusively in the evaluator."""

import argparse
import json
from pathlib import Path

from evaluate import ROOT, evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sample")
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--output", type=Path, default=Path("comparison-report.json"))
    options = parser.parse_args()
    reports = {}
    print("Strategy   Valid  Simulated s  Misses  Omitted  Selections  Workers  CPU s    Fallbacks")
    for strategy in ("all", "file", "function", "hybrid"):
        report, _ = evaluate(options.data_dir, options.answers or options.data_dir / "answers.json",
                             ROOT / "selector.py", strategy=strategy)
        reports[strategy] = report
        score = report["evaluation"]
        valid = score["ok"] and report["selector_exit_code"] == 0 and not report["execution_failure"]
        simulated = f"{score['simulated_milliseconds'] / 1000:.3f}" if score["ok"] else "unavailable"
        misses = len(score["missed_commits"]) if score["ok"] else "-"
        print(f"{strategy:10} {str(valid):5} {simulated:>12} "
              f"{misses:6} {report.get('omitted_failing_tests_total', '-'):8} "
              f"{report.get('selected_tests_total', '-'):10} {report.get('workers_total', '-'):8} "
              f"{report['selector_cpu_seconds']:8.4f} {report['fallback_commits']}")
    options.output.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")
    print(f"Details: {options.output}. Local measurements, not leaderboard scores.")
    return int(any(not r["evaluation"]["ok"] or r["selector_exit_code"] != 0 or r["execution_failure"]
                   for r in reports.values()))


if __name__ == "__main__":
    raise SystemExit(main())
