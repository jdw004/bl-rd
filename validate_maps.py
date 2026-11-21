"""Validate committed artifacts without running tests or importing SQLGlot."""

import argparse
import hashlib
import json
from pathlib import Path

from artifact_bundle import REPOSITORY_LIMIT, read_bundle
from dependency_map import DependencyMap


def repository_bytes(root):
    excluded = {".git", ".venv", "__pycache__"}
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file()
               and not excluded.intersection(path.relative_to(root).parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("maps"))
    parser.add_argument("--requirements", type=Path, default=Path("requirements.lock"))
    parser.add_argument("--tools-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--catalog", type=Path, help="reconcile IDs only; never a generation input")
    options = parser.parse_args()
    try:
        data, manifest = read_bundle(options.artifacts)
        metadata = data["metadata"]
        if hashlib.sha256(options.requirements.read_bytes()).hexdigest() != metadata["requirements_sha256"]:
            raise ValueError("requirements.lock changed since map generation")
        for name, expected in metadata["instrumentation_sha256"].items():
            if hashlib.sha256((options.tools_root / name).read_bytes()).hexdigest() != expected:
                raise ValueError("instrumentation changed since map generation: " + name)
        report = {"artifact_bytes": manifest["artifact"]["bytes"],
                  "revision": metadata["source"]["commit"],
                  "artifact_sha256": manifest["artifact"]["sha256"],
                  "collected_tests": len(data["tests"]),
                  "missing_mappings": metadata["missing_mappings"],
                  "incomplete_tests": metadata["incomplete_tests"],
                  "source_lines_executed": metadata["source_lines_executed"],
                  "source_lines_possible": metadata["source_lines_possible"],
                  "source_line_coverage_percent": metadata["source_line_coverage_percent"],
                  "environment": metadata["environment"],
                  "manifest_bytes": (options.artifacts / "manifest.json").stat().st_size,
                  "run_complete": metadata["run_complete"],
                  "instrumentation_complete": metadata["instrumentation_complete"],
                  "baseline_failures": metadata["baseline_failures"],
                  "collection_errors": metadata["collection_errors"],
                  "unresolved_count": len(metadata["unresolved_dependencies"])}
        if options.repository:
            report["repository_bytes"] = repository_bytes(options.repository)
            if report["repository_bytes"] > REPOSITORY_LIMIT:
                raise ValueError("repository exceeds the total 20-MB budget")
        if options.catalog:
            catalog = json.loads(options.catalog.read_text())
            report["reconciliation"] = DependencyMap(options.artifacts, catalog["tests"]).reconciliation()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError) as error:
        print(f"Map validation failed: {error}. Regenerate with make maps-regenerate.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
