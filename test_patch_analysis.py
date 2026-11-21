"""Focused reconstruction and AST checks; no sample answer inputs."""

import base64
import difflib
import hashlib
import unittest

from artifact_bundle import PINNED_COMMIT
from patch_analysis import PatchAnalyzer
from unified_diff import PatchError, parse_patch, reconstruct


def diff(old, new, old_path="module.py", new_path="module.py", context=3):
    # difflib does not emit Git's missing-newline marker, so add it explicitly.
    before = "/dev/null" if old_path is None else "a/" + old_path
    after = "/dev/null" if new_path is None else "b/" + new_path
    output = []
    for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                     fromfile=before, tofile=after, n=context):
        output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(output).encode()


def analyze(old, new, **options):
    return PatchAnalyzer({"module.py": old.encode()}).analyze(diff(old, new, **options))


def symbol(report, name):
    return next(change for change in report["files"][0]["python"]["symbol_changes"]
                if change["identity"] == "module.py::" + name)


class ReconstructionTests(unittest.TestCase):
    def test_multiple_hunks_and_files_are_independent(self):
        original = "".join(f"v{i} = {i}\n" for i in range(20))
        modified = original.replace("v1 = 1\n", "v1 = 30\n").replace("v18 = 18\n", "v18 = 40\n")
        patch = diff(original, modified, context=1)
        self.assertEqual(len(parse_patch(patch)[0].hunks), 2)
        patch += diff("old\n", "new\n", "fixtures/data.txt", "fixtures/data.txt")
        baseline = {"module.py": original.encode(), "fixtures/data.txt": b"old\n"}
        analyzer = PatchAnalyzer(baseline)
        report = analyzer.analyze(patch)
        self.assertTrue(report["reconstruction_complete"])
        self.assertEqual(len(report["files"]), 2)
        self.assertEqual(report["files"][1]["dependency_kind"], "repository_data_file")
        self.assertEqual(analyzer.analyze(patch), report)
        second = analyzer.analyze(diff(original, original.replace("v1 = 1\n", "v1 = 99\n")))
        self.assertTrue(second["analysis_complete"])
        self.assertEqual(baseline["module.py"], original.encode())

    def test_add_remove_and_rename_with_edits(self):
        baseline = {"old.py": b"def f():\n    return 1\n", "remove.txt": b"gone\n"}
        patch = (b"diff --git a/old.py b/new.py\nsimilarity index 70%\nrename from old.py\nrename to new.py\n"
                 + diff(baseline["old.py"].decode(), "def f():\n    return 2\n", "old.py", "new.py")
                 + diff("", "new data\n", None, "added.txt")
                 + diff("gone\n", "", "remove.txt", None))
        report = PatchAnalyzer(baseline).analyze(patch)
        self.assertTrue(report["analysis_complete"])
        self.assertEqual([file["action"] for file in report["files"]], ["rename", "add", "remove"])
        change = report["files"][0]["python"]["symbol_changes"][0]
        self.assertEqual(change["old_identity"], "old.py::f")
        self.assertEqual(change["new_identity"], "new.py::f")

    def test_metadata_only_rename_empty_files_and_mode(self):
        patches = [
            (b"diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n", {"a.py": b"x = 1\n"}, "rename"),
            (b"diff --git a/empty b/empty\nnew file mode 100644\nindex 0000000..e69de29\n", {}, "add"),
            (b"diff --git a/empty b/empty\ndeleted file mode 100644\nindex e69de29..0000000\n", {"empty": b""}, "remove"),
            (b"diff --git a/a.py b/a.py\nold mode 100644\nnew mode 100755\n", {"a.py": b"x = 1\n"}, "modify"),
        ]
        for patch, baseline, action in patches:
            with self.subTest(action=action):
                result = PatchAnalyzer(baseline).analyze(patch)
                self.assertTrue(result["analysis_complete"], result)
                self.assertEqual(result["files"][0]["action"], action)

    def test_rename_swap_reads_each_original_preimage(self):
        baseline = {"a.txt": b"A\n", "b.txt": b"B\n"}
        patch = (b"diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"
                 b"diff --git a/b.txt b/a.txt\nsimilarity index 100%\nrename from b.txt\nrename to a.txt\n")
        result = PatchAnalyzer(baseline).analyze(patch)
        self.assertTrue(result["reconstruction_complete"])
        self.assertEqual(result["files"][0]["modified_sha256"], hashlib.sha256(b"A\n").hexdigest())
        self.assertEqual(result["files"][1]["modified_sha256"], hashlib.sha256(b"B\n").hexdigest())

    def test_byte_encoding_and_insertion_at_start_and_end(self):
        old = b"# coding: latin-1\nvalue = '\xe9'\n"
        patch = b"--- a/module.py\n+++ b/module.py\n@@ -2 +2 @@\n-value = '\xe9'\n+value = '\xe8'\n"
        result = PatchAnalyzer({"module.py": old}).analyze(patch)
        self.assertTrue(result["analysis_complete"])
        self.assertFalse(result["files"][0]["python"]["identical_ast"])
        for old, new in [("x = 1\n", "y = 2\nx = 1\n"), ("x = 1\n", "x = 1\ny = 2\n")]:
            patched, _, _ = reconstruct(parse_patch(diff(old, new, context=0))[0], old.encode())
            self.assertEqual(patched, new.encode())

    def test_no_newline_markers_and_crlf_bytes(self):
        for old, new in [("x = 1", "x = 2"), ("x = 1\n", "x = 1"), ("x = 1", "x = 1\n"),
                         ("x = 1\r\n", "x = 2\r\n")]:
            with self.subTest(old=old, new=new):
                result, _, _ = reconstruct(parse_patch(diff(old, new))[0], old.encode())
                self.assertEqual(result, new.encode())

    def test_normalized_quoted_paths_and_unsafe_paths(self):
        patch = (b'diff --git "a/dir/sp\\141ce name.py" "b/dir/space name.py"\n'
                 b'--- "a/dir/space name.py"\n+++ "b/dir/space name.py"\n@@ -1 +1 @@\n-x = 1\n+x = 2\n')
        result = PatchAnalyzer({"dir/space name.py": b"x = 1\n"}).analyze(patch)
        self.assertTrue(result["analysis_complete"])
        normalized = b"--- ./a/dir//module.py\n+++ ./b/dir/module.py\n@@ -1 +1 @@\n-x=1\n+x=2\n"
        self.assertEqual(parse_patch(normalized)[0].old_path, "dir/module.py")
        with self.assertRaises(PatchError):
            parse_patch(b"--- a/../secret\n+++ b/../secret\n@@ -1 +1 @@\n-a\n+b\n")

    def test_unsupported_malformed_and_failed_preimages(self):
        for patch, expected in [
            (b"diff --cc module.py\n", "unsupported_combined_diff"),
            (b"diff --git a/file b/file\nGIT binary patch\n", "unsupported_binary"),
            (b"diff --git a/a b/b\ncopy from a\ncopy to b\n", "unsupported_copy"),
            (b"--- a/m.py\n+++ b/m.py\n@@ -1,2 +1 @@\n-old\n+new\n", "hunk_count_mismatch"),
            (b"--- a/m.py\n+++ b/m.py\n@@ -0 +1 @@\n-a\n+b\n", "malformed_hunk"),
            (b"--- a/m.py\n", "malformed_file_header"),
        ]:
            with self.subTest(expected=expected):
                report = PatchAnalyzer({}).analyze(patch)
                self.assertFalse(report["format_supported"])
                self.assertEqual(report["uncertainty_reasons"][0]["code"], expected)
        report = PatchAnalyzer({"module.py": b"different\n"}).analyze(diff("before\n", "after\n"))
        self.assertTrue(report["format_supported"])
        self.assertFalse(report["reconstruction_complete"])
        self.assertEqual(report["files"][0]["uncertainty_reasons"][0]["code"], "preimage_mismatch")

    def test_new_positions_and_removal_must_match(self):
        patch = b"--- a/m.py\n+++ b/m.py\n@@ -1 +2 @@\n-x=1\n+x=2\n"
        report = PatchAnalyzer({"m.py": b"x=1\n"}).analyze(patch)
        self.assertEqual(report["files"][0]["uncertainty_reasons"][0]["code"], "inconsistent_new_position")
        patch = b"--- a/m.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x=1\n"
        report = PatchAnalyzer({"m.py": b"x=1\ny=2\n"}).analyze(patch)
        self.assertEqual(report["files"][0]["uncertainty_reasons"][0]["code"], "incomplete_file_removal")


class StructuralTests(unittest.TestCase):
    def test_insertion_boundaries_and_line_shifts(self):
        old = "def first():\n    return 1\n\ndef last():\n    return 2\n"
        new = old.replace("def last():", "def added():\n    return 3\n\ndef last():")
        result = analyze(old, new, context=0)
        change = symbol(result, "added")
        self.assertEqual(change["baseline_enclosing_scope"], "module.py::<module>")
        self.assertIsNone(change["baseline_range"])
        self.assertTrue(change["requires_scope_expansion"])
        python = result["files"][0]["python"]
        self.assertEqual(python["position_only_symbols"], ["last"])
        self.assertEqual(python["new_code"][0]["baseline_enclosing_scopes"], ["module.py::<module>"])
        self.assertIsNone(python["new_code"][0]["baseline_coverage"])
        self.assertEqual(result["files"][0]["edits"][0]["old"], [3, 3])

    def test_bases_class_attributes_and_initialization(self):
        old = "class Child(Base):\n    FLAG = 1\n    def method(self):\n        return self.FLAG\n"
        new = old.replace("Child(Base)", "Child(Other)").replace("FLAG = 1", "FLAG = 2")
        result = analyze(old, new)
        change = symbol(result, "Child")
        self.assertIn("bases", change["categories"])
        self.assertIn("class_attributes", change["categories"])
        self.assertEqual([binding["target"] for binding in change["binding_changes"]], ["FLAG"])
        self.assertTrue(change["requires_import_expansion"])
        self.assertFalse(any(item["identity"].endswith("::Child.method") for item in result["files"][0]["python"]["symbol_changes"]))

    def test_decorators_defaults_signatures_annotations_async(self):
        old = "@decorate(1)\ndef f(x: int = 1) -> int:\n    return x\n"
        new = "@decorate(2)\nasync def f(x: str = 2, *, y=None) -> str:\n    return x\n"
        change = symbol(analyze(old, new), "f")
        self.assertTrue({"decorators", "signature", "defaults", "annotations", "kind"} <= set(change["categories"]))
        self.assertTrue(change["definition_time_change"])
        self.assertTrue(change["requires_import_expansion"])
        self.assertEqual(change["baseline_range"], [1, 3])

    def test_nested_functions_and_body_only_methods(self):
        old = "class Outer:\n    def method(self):\n        def inner():\n            return 1\n        return inner()\n"
        result = analyze(old, old.replace("return 1", "return 2"))
        inner = symbol(result, "Outer.method.inner")
        self.assertEqual(inner["categories"], ["body"])
        self.assertFalse(inner["definition_time_change"])
        self.assertFalse(inner["requires_import_expansion"])
        self.assertEqual(symbol(result, "Outer.method")["categories"], ["nested_definitions"])
        self.assertFalse(result["files"][0]["python"]["requires_import_expansion"])

    def test_deleted_symbols_and_new_nested_scope(self):
        old = "def removed():\n    return 1\ndef kept():\n    return 2\n"
        result = analyze(old, "def kept():\n    return 2\n")
        removed = symbol(result, "removed")
        self.assertEqual(removed["change"], "removed")
        self.assertEqual(removed["baseline_range"], [1, 2])
        self.assertIsNone(removed["modified_range"])
        old = "def parent():\n    return 1\n"
        new = "def parent():\n    def child():\n        return 2\n    return child()\n"
        child = symbol(analyze(old, new), "parent.child")
        self.assertEqual(child["baseline_enclosing_scope"], "module.py::parent")
        self.assertFalse(child["requires_import_expansion"])

    def test_python_file_addition_and_removal(self):
        content = "async def added():\n    return 1\n"
        result = PatchAnalyzer({}).analyze(diff("", content, None, "module.py"))
        self.assertTrue(result["analysis_complete"])
        self.assertIsNone(symbol(result, "added")["baseline_range"])
        result = PatchAnalyzer({"module.py": content.encode()}).analyze(diff(content, "", "module.py", None))
        self.assertTrue(result["analysis_complete"])
        self.assertEqual(symbol(result, "added")["change"], "removed")

    def test_comment_like_strings_are_actual_code_changes(self):
        old = 'def value():\n    return """# comment\n--- a/path\n+++ b/path\n"""\n'
        result = analyze(old, old.replace("# comment", "# different"))
        self.assertFalse(result["files"][0]["python"]["identical_ast"])
        self.assertIn("body", symbol(result, "value")["categories"])

    def test_identical_ast_encoding_comments_and_positions_are_not_safe(self):
        old = "# coding: utf-8\ndef f():\n    return 1\n"
        new = "# coding: latin-1\n# a comment\ndef f():\n    return 1\n"
        result = analyze(old, new)
        self.assertTrue(result["files"][0]["python"]["identical_ast"])
        self.assertEqual(result["files"][0]["python"]["symbol_changes"], [])
        self.assertTrue(result["fallback_required"])
        self.assertFalse(result["safe_to_skip"])
        codes = {reason["code"] for reason in result["files"][0]["uncertainty_reasons"]}
        self.assertTrue({"identical_ast_not_safety_proof", "encoding_changed"} <= codes)

    def test_malformed_python_and_duplicate_symbols_fall_back(self):
        result = analyze("x = 1\n", "def broken(\n")
        self.assertTrue(result["reconstruction_complete"])
        self.assertFalse(result["analysis_complete"])
        self.assertEqual(result["files"][0]["uncertainty_reasons"][0]["code"], "python_parse_failed")
        result = analyze("x = 1\n", "return 1\n")
        self.assertFalse(result["analysis_complete"])
        old = "def f(): return 1\ndef f(): return 2\n"
        result = analyze(old, old.replace("return 2", "return 3"))
        self.assertIn("ambiguous_symbol_identity", [r["code"] for r in result["files"][0]["uncertainty_reasons"]])

    def test_module_imports_constants_and_initialization(self):
        old = "import first\nVALUE = 1\ninitialize()\n"
        result = analyze(old, "from second import thing\nVALUE = 2\ninitialize(thing)\n")
        self.assertTrue({"imports", "assignments", "body"} <= set(result["files"][0]["python"]["module_changes"]))
        self.assertTrue(result["files"][0]["python"]["requires_import_expansion"])
        self.assertEqual([binding["target"] for binding in result["files"][0]["python"]["module_binding_changes"]], ["VALUE"])

    def test_snapshot_hashes_and_metadata_references(self):
        original = b"def f():\n    return 1\n"
        data = {"metadata": {"source": {"commit": PINNED_COMMIT}, "instrumentation_complete": True,
                             "environment": {"python": "3.11.0"}},
                "baseline_files": {"module.py": base64.b64encode(original).decode()},
                "source_sha256": {"module.py": hashlib.sha256(original).hexdigest()},
                "lines": {"module.py": [[2, 2, 0]]}, "functions": {"module.py": [["f", 1, 2, 1]]},
                "reads": {"module.py": 2}, "fixtures": {}, "structure": {"classes": {}}}
        analyzer = PatchAnalyzer.from_data(data)
        report = analyzer.analyze(diff(original.decode(), "def f():\n    return 2\n"))
        references = report["files"][0]["dependency_references"]
        self.assertEqual(references["baseline_line_consumer_sets"], [0])
        self.assertEqual(references["baseline_function_consumer_sets"], [1])
        self.assertEqual(references["file_read_consumer_set"], 2)
        data["baseline_files"]["module.py"] = base64.b64encode(b"wrong\n").decode()
        with self.assertRaisesRegex(ValueError, "source hashes"):
            PatchAnalyzer.from_data(data)


if __name__ == "__main__":
    unittest.main()
