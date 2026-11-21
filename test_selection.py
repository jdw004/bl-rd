"""Exercise selection against small, explicitly constructed dependency maps."""

import copy
import json
from pathlib import Path
import unittest

from dependency_map import DependencyMap
from patch_analysis import PatchAnalyzer
from score import evaluate_run
from selection import TestSelector
from selector import allocate
from test_patch_analysis import diff


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tests = [{"id": f"tests/test_{name}.py::test_{name}", "file": f"tests/test_{name}.py",
                       "number": index, "seconds": seconds}
                      for index, (name, seconds) in enumerate([("left", 1), ("right", 20), ("data", 2)])]
        self.source = b"def left():\n    return 1\ndef right():\n    return 2\n"
        self.data = {
            "tests": [test["id"] for test in self.tests], "test_sets": [[0], [1], [2]],
            "metadata": {"run_complete": True, "source_unchanged": True, "instrumentation_issues": [],
                "environment_mismatch": [], "incomplete_tests": [], "missing_mappings": [],
                "unresolved_dependencies": [], "collection_errors": [], "baseline_failures": []},
            "functions": {"mod.py": [["left", 1, 2, 0], ["right", 3, 4, 1]]},
            "lines": {}, "reads": {"fixtures/data.txt": 2}, "modules": {"mod": "mod.py"},
            "imports": {"tests/test_left.py": [["mod", 0]]}, "static_imports": {},
        }

    def engine(self, data=None, strategy="hybrid"):
        # Exercise the actual binder against this test-only in-memory bundle.
        from unittest.mock import patch
        with patch("dependency_map.read_bundle", return_value=(data or self.data, {})):
            bound = DependencyMap(Path("unused"), self.tests)
        return TestSelector(bound, self.tests, strategy=strategy)

    def change(self, new_source=None, data=False):
        if data:
            analyzer = PatchAnalyzer({"fixtures/data.txt": b"old\n"})
            result = analyzer.analyze(diff("old\n", "new\n", "fixtures/data.txt", "fixtures/data.txt"))
        else:
            analyzer = PatchAnalyzer({"mod.py": self.source})
            result = analyzer.analyze(diff(self.source.decode(), new_source or self.source.decode().replace("return 1", "return 3"), "mod.py", "mod.py"))
        # Source validation is covered by analyzer/bundle tests. Here we isolate
        # the selection policy from environment and provenance of synthetic data.
        result["uncertainty_reasons"] = []
        return result

    def test_function_and_data_consumers_reduce_cost_without_misses(self):
        selected, decision = self.engine().select(self.change())
        self.assertEqual(selected, {0})
        self.assertEqual(decision["mode"], "dependencies")
        selected_data, _ = self.engine().select(self.change(data=True))
        self.assertEqual(selected_data, {2})
        output = json.dumps({"commit": "change", "workers": allocate([self.tests[0]])}).encode()
        result = evaluate_run(output, self.tests, {"change": {"failing": [0]}})
        self.assertEqual(result, {"ok": True, "simulated_milliseconds": 3000, "missed_commits": []})
        baseline = evaluate_run(json.dumps({"commit": "change", "workers": allocate(self.tests)}).encode(),
                                self.tests, {"change": {"failing": [0]}})
        self.assertLess(result["simulated_milliseconds"], baseline["simulated_milliseconds"])

    def test_new_body_lines_expand_to_whole_function(self):
        changed = self.source.decode().replace("    return 1", "    extra = 3\n    return extra")
        selected, _ = self.engine().select(self.change(changed))
        self.assertEqual(selected, {0})

    def test_multifile_selection_unions_consumers(self):
        first, second = self.change(), self.change(data=True)
        first["files"].extend(second["files"])
        self.assertEqual(self.engine().select(first)[0], {0, 2})

    def test_initialization_expands_dependencies(self):
        changed = self.source.decode().replace("def left():", "@decorate\ndef left():")
        selected, decision = self.engine().select(self.change(changed))
        self.assertEqual(selected, {0, 1})
        self.assertIn("initialization_dependencies", decision["reasons"][0])

    def test_missing_or_uncertain_evidence_falls_back(self):
        for case in ("mapping", "reads", "baseline", "instrumentation", "parse", "comment"):
            with self.subTest(case=case):
                data = copy.deepcopy(self.data)
                change = self.change(data=case == "reads")
                if case == "mapping":
                    data["functions"] = {}
                elif case == "reads":
                    data["reads"] = {}
                elif case == "baseline":
                    data["metadata"]["run_complete"] = False
                elif case == "instrumentation":
                    data["metadata"]["instrumentation_issues"] = ["lost trace"]
                elif case == "parse":
                    change["analysis_complete"] = False
                elif case == "comment":
                    change = self.change("# comment\n" + self.source.decode())
                self.assertEqual(self.engine(data).select(change)[0], {0, 1, 2})

    def test_unresolved_relationships_are_scoped_not_silently_discarded(self):
        self.data["metadata"]["unresolved_dependencies"] = [{"file": "elsewhere.py", "kind": "inheritance"}]
        self.assertEqual(self.engine().select(self.change())[0], {0})
        self.data["metadata"]["unresolved_dependencies"][0]["file"] = "mod.py"
        self.assertEqual(self.engine().select(self.change())[0], {0, 1, 2})
        self.data["metadata"]["unresolved_dependencies"] = [{"test": self.tests[1]["id"], "kind": "fixture"}]
        self.assertEqual(self.engine().select(self.change())[0], {0, 1})

    def test_unmapped_catalog_test_is_always_included(self):
        self.data["tests"] = self.data["tests"][:2]
        self.data["test_sets"] = [[0], [1], []]
        self.assertEqual(self.engine().select(self.change())[0], {0, 2})

    def test_source_inspection_reader_does_not_need_to_call_function(self):
        self.data["reads"]["mod.py"] = 2
        self.assertEqual(self.engine().select(self.change())[0], {0, 2})
        self.assertEqual(self.engine(strategy="function").select(self.change())[0], {0})

    def test_file_and_function_ablations_have_distinct_scopes(self):
        self.assertEqual(self.engine(strategy="file").select(self.change())[0], {0, 1})
        self.assertEqual(self.engine(strategy="function").select(self.change())[0], {0})
        self.assertEqual(self.engine(strategy="function").select(self.change(data=True))[0], {0, 1, 2})

    def test_unresolved_importer_of_edited_source_falls_back(self):
        self.data["static_imports"] = {"dynamic.py": ["mod"]}
        self.data["metadata"]["unresolved_dependencies"] = [{"file": "dynamic.py", "kind": "dynamic_import"}]
        selected, reason = self.engine().select(self.change())
        self.assertEqual(selected, {0, 1, 2})
        self.assertIn("unresolved_dependent_relationship", reason["reasons"][0])

    def test_class_override_consumers_expand_transitively(self):
        self.source = b"class Base:\n    def render(self):\n        return 1\n"
        self.data["functions"] = {"mod.py": [["Base.render", 2, 3, 0]],
                                  "child.py": [["Child.render", 2, 3, 1]],
                                  "grandchild.py": [["Grandchild.render", 2, 3, 2]]}
        self.data["modules"].update(child="child.py", grandchild="grandchild.py")
        self.data["structure"] = {"classes": {
            "child.Child": {"overrides": {"render": ["mod.Base.render"]}}}}
        self.data["runtime_classes"] = {
            "grandchild.Grandchild": {"overrides": {"render": "child.Child.render"}}}
        selected, reason = self.engine().select(self.change())
        self.assertEqual(selected, {0, 1, 2})
        self.assertEqual(reason["mode"], "dependencies")
        self.assertEqual(self.engine(strategy="function").select(self.change())[0], {0})
        del self.data["functions"]["grandchild.py"]
        self.assertEqual(self.engine().select(self.change())[1]["mode"], "all")

    def test_baseline_failure_and_unknown_data_file_remain_visible(self):
        self.data["metadata"]["incomplete_tests"] = [self.tests[1]["id"]]
        self.assertEqual(self.engine().select(self.change())[0], {0, 1})
        self.data["reads"] = {}
        selected, reason = self.engine().select(self.change(data=True))
        self.assertEqual(selected, {0, 1, 2})
        self.assertEqual(reason["mode"], "all")

    def test_uncertain_test_is_not_a_substitute_for_missing_evidence(self):
        self.data["metadata"]["incomplete_tests"] = [self.tests[1]["id"]]
        self.data["test_sets"][2] = []
        self.assertEqual(self.engine().select(self.change(data=True))[1]["mode"], "all")
        self.data["imports"] = {}
        self.data["functions"] = {}
        changed = self.source.decode().replace("def left():", "@decorate\ndef left():")
        self.assertEqual(self.engine().select(self.change(changed))[1]["mode"], "all")


if __name__ == "__main__":
    unittest.main()
