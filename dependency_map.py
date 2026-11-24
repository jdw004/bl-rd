"""Read build-time maps and bind their stable node IDs to a runtime catalog."""

from collections import defaultdict
from pathlib import Path

from artifact_bundle import PINNED_COMMIT, read_bundle


class DependencyMap:
    def __init__(self, path: Path, tests: list[dict], require_pinned=True):
        self.data, self.manifest = read_bundle(path, require_pinned=require_pinned)
        by_id = {test["id"]: test["number"] for test in tests}
        if len(by_id) != len(tests):
            raise ValueError("duplicate test node IDs in runtime catalog")
        # No suffix stripping: [param] identifies a distinct catalog test;
        # subtests already share their parent's pytest node ID in the map.
        self.sets = [frozenset(by_id[node] for index in group
                              if (node := self.data["tests"][index]) in by_id)
                     for group in self.data["test_sets"]]
        self.unmapped_ids = sorted(set(by_id) - set(self.data["tests"]))
        self.unmapped_numbers = {by_id[node] for node in self.unmapped_ids}
        metadata = self.data["metadata"]
        self.incomplete_ids = sorted(set(by_id) & (set(metadata["incomplete_tests"]) | set(metadata["missing_mappings"])))
        self.uncertain_numbers = self.unmapped_numbers | {by_id[node] for node in self.incomplete_ids}
        self.uncertain_numbers.update(by_id[node] for node in metadata.get("gap_tests", []) if node in by_id)
        self.global_fallback_reasons = []
        if not metadata["run_complete"]:
            self.global_fallback_reasons.append("baseline_incomplete")
        if not metadata["source_unchanged"]:
            self.global_fallback_reasons.append("source_changed")
        if metadata["instrumentation_issues"]:
            self.global_fallback_reasons.append("instrumentation_gap")
        if metadata["environment_mismatch"]:
            self.global_fallback_reasons.append("environment_mismatch")
        # A static relationship in an unrelated file must not invalidate every
        # observed function/read dependency. Keep it for affected-path fallback.
        self.unresolved_by_path = {}
        for issue in metadata["unresolved_dependencies"]:
            if issue.get("test") in by_id:
                self.uncertain_numbers.add(by_id[issue["test"]])
            elif issue.get("file"):
                self.unresolved_by_path.setdefault(issue["file"], []).append(issue)
            else:
                self.global_fallback_reasons.append("unscoped_relationship")
        if self.global_fallback_reasons:
            self.uncertain_numbers = set(by_id.values())
        self.map_only_ids = sorted(set(self.data["tests"]) - set(by_id))
        self.metadata = self.data["metadata"]
        self._affected_cache = {}
        self._import_consumers_cache = {}
        self._dependents = defaultdict(set)
        modules = self.data["modules"]
        for origin in set(self.data["static_imports"]) | set(self.data["imports"]):
            names = set(self.data["static_imports"].get(origin, []))
            names.update(name for name, _ in self.data["imports"].get(origin, []))
            for name in names:
                if name in modules:
                    self._dependents[modules[name]].add(origin)

    def line_consumers(self, path: str, start: int, end: int) -> set[int]:
        result = set(self.uncertain_numbers)
        for first, last, group in self.data["lines"].get(path, []):
            if first <= end and last >= start:
                result.update(self.sets[group])
        return result

    def function_consumers(self, path: str, start: int, end: int) -> set[int]:
        result = set(self.uncertain_numbers)
        for name, first, last, group in self.data["functions"].get(path, []):
            if first <= end and last >= start:
                result.update(self.sets[group])
        return result

    def read_consumers(self, path: str, include_uncertain=True) -> set[int]:
        group = self.data["reads"].get(path)
        return (self.uncertain_numbers if include_uncertain else set()) | (set(self.sets[group]) if group is not None else set())

    def affected_files(self, path: str) -> frozenset[str]:
        """Reverse static/observed import closure, cached for the patch batch."""
        if path in self._affected_cache:
            return self._affected_cache[path]
        affected = {path}
        pending = [path]
        while pending:
            for origin in self._dependents.get(pending.pop(), ()):
                if origin not in affected:
                    affected.add(origin)
                    pending.append(origin)
        self._affected_cache[path] = frozenset(affected)
        return self._affected_cache[path]

    def import_consumers(self, path: str, include_uncertain=True) -> set[int]:
        """Include transitive and cached imports, with static edges as a superset."""
        if path not in self._import_consumers_cache:
            affected = self.affected_files(path)
            modules, observed = self.data["modules"], self.data["imports"]
            observed_users = set()
            for dependencies in observed.values():
                for name, group in dependencies:
                    if modules.get(name) in affected:
                        observed_users.update(self.sets[group])
            self._import_consumers_cache[path] = frozenset(observed_users)
        result = set(self._import_consumers_cache[path])
        if include_uncertain:
            result.update(self.uncertain_numbers)
        return result

    def summary(self) -> str:
        return (f"dependency map: {len(self.data['tests'])} stable node IDs; "
                f"{len(self.unmapped_ids)} catalog IDs unmapped; "
                f"{len(self.incomplete_ids)} catalog IDs incompletely exercised; "
                f"{len(self.map_only_ids)} map IDs outside catalog; "
                f"{len(self.uncertain_numbers)} fallback test numbers; "
                f"run_complete={self.metadata['run_complete']}; "
                f"instrumentation_complete={self.metadata['instrumentation_complete']}")

    def reconciliation(self):
        return {"unmapped_catalog_ids": self.unmapped_ids, "incomplete_catalog_ids": self.incomplete_ids,
                "map_only_ids": self.map_only_ids, "fallback_test_numbers": sorted(self.uncertain_numbers),
                "collection_errors": self.metadata["collection_errors"],
                "baseline_failures": self.metadata["baseline_failures"],
                "unresolved_dependencies": self.metadata["unresolved_dependencies"],
                "environment_mismatch": self.metadata["environment_mismatch"]}
