"""Audit independent patches, keeping format support separate from reconstruction."""

import argparse
import json
from pathlib import Path

from patch_analysis import PatchAnalyzer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patches", type=Path, default=Path("sample/commits"))
    parser.add_argument("--maps", type=Path, default=Path("maps"))
    parser.add_argument("--output", type=Path, default=Path("patch-analysis-report.json"))
    options = parser.parse_args()
    baseline_error = None
    try:
        analyzer = PatchAnalyzer.from_bundle(options.maps)
    except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError) as error:
        baseline_error = str(error)
        analyzer = PatchAnalyzer({})
    paths = sorted(options.patches.glob("*.patch"))
    reports = {path.stem: analyzer.analyze(path.read_bytes()) for path in paths}
    summary = {
        "patches": len(paths), "format_supported": sum(r["format_supported"] for r in reports.values()),
        "files": sum(len(r["files"]) for r in reports.values()),
        "hunks": sum(file["hunks"] for r in reports.values() for file in r["files"]),
        "reconstructed": sum(r["reconstruction_complete"] for r in reports.values()),
        "analyzed": sum(r["analysis_complete"] for r in reports.values()),
        "fallback_required": sum(r["fallback_required"] for r in reports.values()),
        "unsupported": {name: report["uncertainty_reasons"] for name, report in reports.items() if not report["format_supported"]},
        "baseline_error": baseline_error,
    }
    options.output.write_text(json.dumps({"summary": summary, "commits": reports}, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if paths and summary["analyzed"] == len(paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())
