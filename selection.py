"""Select tests from structural changes and baseline dependency evidence.

No answers, patch identities, or sample-specific rules are consulted. This is
dynamic-evidence selection, not a guarantee of independence for unseen changes.
"""

from collections import defaultdict


class TestSelector:
    __test__ = False  # This module is also importable by pytest discovery.

    def __init__(self, dependencies, tests, strategy="hybrid"):
        if strategy not in {"file", "function", "hybrid"}:
            raise ValueError("unknown dependency strategy")
        self.strategy = strategy
        self.dependencies = dependencies
        self.data = dependencies.data
        self.all_numbers = {test["number"] for test in tests}
        self.functions = defaultdict(dict)
        for path, definitions in self.data["functions"].items():
            for name, start, end, group in definitions:
                self.functions[path].setdefault(name, set()).update(dependencies.sets[group])
        self.by_test_file = defaultdict(set)
        for test in tests:
            self.by_test_file[test["file"]].add(test["number"])
        self.symbols = {module + "." + name: (path, name)
                        for module, path in self.data["modules"].items()
                        for name in self.functions.get(path, {})}
        self.qualified_symbols = defaultdict(set)
        for symbol, identity in self.symbols.items():
            self.qualified_symbols[identity].add(symbol)
        self.overrides = defaultdict(set)
        for classes in (self.data.get("structure", {}).get("classes", {}),
                        self.data.get("runtime_classes", {})):
            for owner, entry in classes.items():
                for method, parents in entry.get("overrides", {}).items():
                    for parent in ([parents] if isinstance(parents, str) else parents):
                        child = owner + "." + method
                        self.overrides[child].add(parent)
                        self.overrides[parent].add(child)

    def related_methods(self, path, name):
        pending = list(self.qualified_symbols.get((path, name), ()))
        seen = set(pending)
        while pending:
            for related in self.overrides[pending.pop()]:
                if related not in seen:
                    seen.add(related)
                    pending.append(related)
        return seen

    def file_consumers(self, path):
        consumers = self.dependencies.import_consumers(path, include_uncertain=False)
        consumers.update(self.by_test_file.get(path, set()))
        for users in self.functions.get(path, {}).values():
            consumers.update(users)
        for _, _, group in self.data["lines"].get(path, []):
            consumers.update(self.dependencies.sets[group])
        if path in self.data["reads"]:
            consumers.update(self.dependencies.read_consumers(path, include_uncertain=False))
        return consumers

    def select(self, change):
        reasons = []

        def all_tests(reason):
            return set(self.all_numbers), {"mode": "all", "reasons": reasons + [reason]}

        if self.dependencies.global_fallback_reasons:
            return all_tests(";".join(self.dependencies.global_fallback_reasons))
        if not change["format_supported"] or not change["reconstruction_complete"] or not change["analysis_complete"]:
            return all_tests("patch_analysis_failed")
        fatal = [reason["code"] for reason in change["uncertainty_reasons"]
                 if reason["code"] != "dependency_metadata_incomplete"]
        if fatal:
            return all_tests(";".join(fatal))
        selected = set(self.dependencies.uncertain_numbers)
        for file in change["files"]:
            path = file["old_path"] or file["new_path"]
            if file["uncertainty_reasons"]:
                return all_tests(path + ":" + ";".join(reason["code"] for reason in file["uncertainty_reasons"]))
            if path in self.dependencies.unresolved_by_path:
                return all_tests(path + ":unresolved_relationship")
            if self.strategy == "hybrid" and any(
                    affected in self.dependencies.unresolved_by_path
                    for affected in self.dependencies.affected_files(path)):
                return all_tests(path + ":unresolved_dependent_relationship")
            if file.get("requires_file_dependency_expansion", False):
                # New/deleted/renamed files and permission changes can affect
                # discovery and dynamic loading beyond baseline observations.
                return all_tests(path + ":file_structure_changed")
            if file["dependency_kind"] == "repository_data_file":
                if self.strategy == "function":
                    return all_tests(path + ":outside_function_coverage")
                if path not in self.data["reads"]:
                    return all_tests(path + ":no_read_mapping")
                readers = self.dependencies.read_consumers(path, include_uncertain=False)
                if not readers:
                    return all_tests(path + ":no_observed_readers")
                selected.update(readers)
                reasons.append(path + ":data_readers")
                continue
            python = file["python"]
            if self.strategy == "file" or python["requires_import_expansion"]:
                if self.strategy == "function":
                    return all_tests(path + ":outside_function_coverage")
                consumers = self.file_consumers(path)
                if not consumers:
                    return all_tests(path + ":no_initialization_mapping")
                selected.update(consumers)
                reasons.append(path + (":file_dependencies" if self.strategy == "file" else ":initialization_dependencies"))
                continue
            matched = False
            for symbol in python["symbol_changes"]:
                # Parent fingerprints also change when a nested body changes.
                # The leaf's function context identifies the actual consumers.
                if symbol["categories"] == ["nested_definitions"]:
                    continue
                if (symbol["change"] != "modified" or symbol["kind"] not in {"FunctionDef", "AsyncFunctionDef"}
                        or symbol["definition_time_change"]):
                    return all_tests(path + ":unsupported_symbol_expansion")
                name = symbol["identity"].split("::", 1)[1]
                users = self.functions.get(path, {}).get(name)
                if not users:
                    return all_tests(path + "::" + name + ":no_function_mapping")
                # Whole-function consumers cover newly inserted/unexecuted lines
                # in that function, rather than relying only on edited lines.
                selected.update(users)
                if self.strategy == "hybrid":
                    for related in sorted(self.related_methods(path, name)):
                        identity = self.symbols.get(related)
                        if identity is None or not self.functions[identity[0]].get(identity[1]):
                            return all_tests(path + ":unmapped_related_method")
                        selected.update(self.functions[identity[0]][identity[1]])
                matched = True
                reasons.append(path + "::" + name + ":function_consumers")
            if not matched:
                return all_tests(path + ":no_changed_function_evidence")
            selected.update(self.by_test_file.get(path, set()))
            # Reading source (e.g. inspect.getsource) can observe body edits
            # without calling the function. Function-only is an explicit ablation.
            if self.strategy == "hybrid" and path in self.data["reads"]:
                selected.update(self.dependencies.read_consumers(path))
                reasons.append(path + ":source_readers")
        return selected, {"mode": "dependencies", "reasons": reasons}
