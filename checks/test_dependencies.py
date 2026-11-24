"""Exercise instrumentation in a clean process against synthetic source only."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from dependency_map import DependencyMap, PINNED_COMMIT
from artifact_bundle import read_bundle, write_bundle

ROOT = Path(__file__).resolve().parents[1]


class SourceIndexTests(unittest.TestCase):
    def test_import_without_calling_python_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'native_import_target.py').write_text('value = 7\n')
            program = '''
import importlib, sys
from pathlib import Path
from build_dependencies import Recorder
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
recorder = Recorder(root)
recorder.start()
original = sys._getframe
def missing_frame(*args):
    raise ValueError('call stack is not deep enough')
try:
    sys._getframe = missing_frame
    module = __import__('native_import_target')
    assert module.value == 7
    assert importlib.import_module('native_import_target') is module
finally:
    sys._getframe = original
    recorder.stop()
assert ('<external>', 'native_import_target') in recorder.imports[('collection', '')]
assert recorder.module_files['native_import_target'] == 'native_import_target.py'
'''
            result = subprocess.run([sys.executable, '-B', '-c', program, str(root)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unmapped_bytecode_lines_in_module_and_nested_functions(self):
        import dis
        from build_dependencies import source_index, source_manifest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "example.py").write_text(
                "def outer():\n"
                "    def inner():\n"
                "        try:\n"
                "            return 1\n"
                "        finally:\n"
                "            pass\n"
                "    return inner()\n"
            )
            manifest = source_manifest(root)
            expected = source_index(root, manifest)
            original = dis.findlinestarts
            visited = set()

            def with_unmapped_lines(code):
                visited.add(code.co_name)
                yield 0, None
                yield 0, 0
                yield from original(code)
                yield len(code.co_code), None

            with patch("build_dependencies.dis.findlinestarts", side_effect=with_unmapped_lines):
                actual = source_index(root, manifest)
            self.assertEqual(actual, expected)
            self.assertEqual(visited, {"<module>", "outer", "inner"})
            self.assertIn(4, actual[0]["example.py"])


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="dependency-check-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        source = cls.root / "source"
        files = {
            "sqlglot/__init__.py": "",
            "sqlglot/leaf.py": "def helper(value):\n    return value + 1\n",
            "sqlglot/bridge.py": "from .leaf import helper\n",
            "sqlglot/base.py": "class Base:\n    def render(self):\n        return 'base'\n",
            "sqlglot/public.py": "from .base import Base as Exported\n__all__ = ['Exported']\n",
            "sqlglot/child.py": "from .public import Exported\nclass Child(Exported):\n    def render(self):\n        return 'child'\n",
            "tests/conftest.py": '''
                from pathlib import Path
                import pytest
                ROOT = Path(__file__).parents[1]
                COLLECTION = (ROOT / "conftest_collection.txt").read_text()
                @pytest.fixture(scope="session", autouse=True)
                def global_data():
                    value = (ROOT / "autouse.txt").read_text()
                    yield value
                    (ROOT / "autouse_teardown.txt").read_text()
                @pytest.fixture(scope="session")
                def seed():
                    return (ROOT / "seed.txt").read_text()
                @pytest.fixture(scope="session")
                def transitive(seed):
                    return seed
            ''',
            "tests/nested/conftest.py": '''
                from pathlib import Path
                import pytest
                ROOT = Path(__file__).parents[2]
                @pytest.fixture(scope="session")
                def seed():
                    return (ROOT / "nested_seed.txt").read_text()
                @pytest.fixture(scope="module", autouse=True)
                def scoped(transitive):
                    return transitive
            ''',
            "tests/nested/test_scoped.py": "def test_scoped():\n    assert True\n",
            "sqlglot/core.py": '''
                from pathlib import Path
                from .bridge import helper
                ROOT = Path(__file__).parents[1]
                def compute(value):
                    return helper(value)
                def prepare():
                    return (ROOT / "setup.txt").read_text()
                def cleanup():
                    return (ROOT / "cleanup.txt").read_text()
                def unused():
                    return "never called"
            ''',
            "tests/test_examples.py": '''
                import importlib
                import os
                from pathlib import Path
                import unittest
                import pytest
                from sqlglot import core, child
                ROOT = Path(__file__).parents[1]
                COLLECTION = (ROOT / "collection.txt").read_text()

                @pytest.fixture(scope="session")
                def shared(transitive):
                    value = (ROOT / "shared.txt").read_text()
                    yield value
                    (ROOT / "shared_teardown.txt").read_text()

                @pytest.fixture
                def prepared(request):
                    value = core.prepare()
                    request.addfinalizer(core.cleanup)
                    return value

                @pytest.mark.parametrize("value", [1, 2], ids=["one", "two"])
                def test_parameter(value, shared, prepared):
                    assert child.Child().render() == "child"
                    from sqlglot.leaf import helper
                    assert core.compute(value) == helper(value)
                    assert importlib.import_module("sqlglot.bridge").helper(value) == value + 1
                    with open(ROOT / "data.txt") as stream:
                        assert stream.read() == "data"
                    fd = os.open(ROOT / "raw.txt", os.O_RDONLY)
                    os.close(fd)

                class TestSubtests(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        (ROOT / "class_setup.txt").read_text()
                    @classmethod
                    def tearDownClass(cls):
                        (ROOT / "class_cleanup.txt").read_text()
                    def setUp(self):
                        (ROOT / "unittest_setup.txt").read_text()
                    def tearDown(self):
                        (ROOT / "unittest_cleanup.txt").read_text()
                    def test_parent(self):
                        for value in [3, 4]:
                            with self.subTest(value=value):
                                self.assertEqual(core.compute(value), value + 1)
            ''',
            "tests/test_other.py": '''
                def test_other():
                    assert True
            ''',
        }
        for name in ("setup.txt", "cleanup.txt", "collection.txt", "shared.txt",
                     "shared_teardown.txt", "data.txt", "raw.txt", "class_setup.txt",
                     "class_cleanup.txt", "unittest_setup.txt", "unittest_cleanup.txt"):
            files[name] = "data"
        for name in ("autouse.txt", "autouse_teardown.txt", "conftest_collection.txt", "seed.txt", "nested_seed.txt"):
            files[name] = "data"
        for name, content in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content).lstrip())
        cls.source = source
        cls.output = cls.root / "maps"
        cls.command = [sys.executable, str(ROOT / "build_dependencies.py"), "--source", str(source),
                       "--output", str(cls.output), "--timeout", "30"]
        result = subprocess.run(cls.command, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.data, cls.manifest = read_bundle(cls.output, require_pinned=False)

    def consumers(self, group):
        return {self.data["tests"][index] for index in self.data["test_sets"][group]}

    def function_users(self, path, name):
        for function, start, end, group in self.data["functions"][path]:
            if function == name:
                return self.consumers(group)
        return set()

    def test_parametrization_subtests_and_functions(self):
        self.assertEqual(len(self.data["tests"]), 5)
        self.assertIn("tests/test_examples.py::test_parameter[one]", self.data["tests"])
        self.assertIn("tests/test_examples.py::TestSubtests::test_parent", self.data["tests"])
        self.assertFalse(any("value=" in node for node in self.data["tests"]))
        users = self.function_users("sqlglot/core.py", "compute")
        self.assertEqual(len(users), 3)
        self.assertNotIn("tests/test_other.py::test_other", users)
        self.assertEqual(self.function_users("sqlglot/core.py", "unused"), set())
        line_users = set()
        for first, last, group in self.data["lines"]["sqlglot/core.py"]:
            if first <= 5 <= last:  # compute's body
                line_users.update(self.consumers(group))
        self.assertEqual(line_users, users)

    def test_collection_imports_cached_imports_and_reads(self):
        self.assertEqual(self.consumers(self.data["reads"]["collection.txt"]), set(self.data["tests"]))
        for name in ("data.txt", "raw.txt", "setup.txt", "cleanup.txt"):
            self.assertEqual(self.consumers(self.data["reads"][name]), {
                "tests/test_examples.py::test_parameter[one]",
                "tests/test_examples.py::test_parameter[two]",
            })
        imports = dict(self.data["imports"]["tests/test_examples.py"])
        self.assertEqual(len(self.consumers(imports["sqlglot.leaf"])), 2)
        self.assertIn("sqlglot.bridge", imports)
        self.assertIn("sqlglot.leaf", self.data["static_imports"]["sqlglot/bridge.py"])

    def test_shared_fixture_setup_and_finalizer_reach_all_consumers(self):
        for name in ("shared.txt", "shared_teardown.txt"):
            self.assertEqual(self.consumers(self.data["reads"][name]), {
                "tests/test_examples.py::test_parameter[one]", "tests/test_examples.py::test_parameter[two]"})
        self.assertEqual(len(self.function_users("sqlglot/core.py", "prepare")), 2)
        self.assertEqual(len(self.function_users("sqlglot/core.py", "cleanup")), 2)
        for name in ("class_setup.txt", "class_cleanup.txt", "unittest_setup.txt", "unittest_cleanup.txt"):
            self.assertEqual(self.consumers(self.data["reads"][name]), {
                "tests/test_examples.py::TestSubtests::test_parent",
            })

    def test_fixture_graph_autouse_transitive_and_conftest_scope(self):
        for name in ("autouse.txt", "autouse_teardown.txt", "conftest_collection.txt"):
            self.assertEqual(self.consumers(self.data["reads"][name]), set(self.data["tests"]))
        parameters = {"tests/test_examples.py::test_parameter[one]", "tests/test_examples.py::test_parameter[two]"}
        self.assertEqual(self.consumers(self.data["reads"]["seed.txt"]), parameters)
        self.assertEqual(self.consumers(self.data["reads"]["nested_seed.txt"]), {"tests/nested/test_scoped.py::test_scoped"})
        for node in self.data["tests"]:
            self.assertIn("global_data", self.data["test_fixtures"][node]["autouse"])
        nested = self.data["test_fixtures"]["tests/nested/test_scoped.py::test_scoped"]
        ordinary = self.data["test_fixtures"]["tests/test_examples.py::test_parameter[one]"]
        self.assertNotEqual(nested["used"]["seed"], ordinary["used"]["seed"])
        self.assertIn([ordinary["used"]["shared"], ordinary["used"]["transitive"]], self.data["fixture_edges"])
        self.assertIn([ordinary["used"]["transitive"], ordinary["used"]["seed"]], self.data["fixture_edges"])

    def test_inheritance_overrides_relative_imports_and_reexports(self):
        structure = self.data["structure"]
        child = structure["classes"]["sqlglot.child.Child"]
        self.assertEqual(child["bases"], ["sqlglot.base.Base"])
        self.assertEqual(child["overrides"]["render"], ["sqlglot.base.Base.render"])
        self.assertEqual(structure["reexports"]["sqlglot.public"]["Exported"], "sqlglot.base.Base")
        self.assertEqual(structure["explicit_exports"]["sqlglot.public"], ["Exported"])
        actual = self.data["runtime_classes"]["sqlglot.child.Child"]
        self.assertEqual(actual["mro"], ["sqlglot.child.Child", "sqlglot.base.Base", "builtins.object"])
        self.assertEqual(actual["overrides"]["render"], "sqlglot.base.Base.render")

    def test_compressed_artifacts_are_deterministic_and_tampering_is_rejected(self):
        duplicate_source = self.root / "different-location"
        shutil.copytree(self.source, duplicate_source)
        output = self.root / "repeat"
        command = self.command.copy()
        command[command.index("--source") + 1] = str(duplicate_source)
        command[command.index("--output") + 1] = str(output)
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("dependencies.json.gz", "manifest.json"):
            self.assertEqual((self.output / name).read_bytes(), (output / name).read_bytes(), name)
        archive = output / "dependencies.json.gz"
        archive.write_bytes(archive.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
            read_bundle(output, require_pinned=False)

    def test_unresolved_dynamic_relationships_and_collection_failures_are_visible(self):
        dormant = self.source / "sqlglot/dormant.py"
        dormant.write_text('''from importlib import import_module as dynamic_load
def load(name): return dynamic_load(name)
def fixture(request, name): return request.getfixturevalue(name)
def factory(base):
    class Derived(base): pass
    return Derived
''')
        failure = self.source / "tests/test_collection_failure.py"
        failure.write_text('raise RuntimeError("intentional collection failure")\n')
        try:
            output = self.root / "collection-failure"
            command = self.command.copy()
            command[command.index("--output") + 1] = str(output)
            result = subprocess.run(command, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            data = read_bundle(output, require_pinned=False)[0]
            metadata = data["metadata"]
            self.assertTrue(metadata["collection_errors"])
            self.assertFalse(metadata["run_complete"])
            self.assertTrue({"dynamic_import", "inheritance", "fixture"} <= {row["kind"] for row in metadata["unresolved_dependencies"]})
            catalog = [{"id": node, "number": index} for index, node in enumerate(self.data["tests"])]
            bound = DependencyMap(output, catalog, require_pinned=False)
            self.assertEqual(bound.line_consumers("unknown.py", 1, 1), set(range(len(catalog))))
            self.assertTrue(bound.reconciliation()["collection_errors"])
        finally:
            dormant.unlink()
            failure.unlink()

    def test_metadata_and_permissions(self):
        metadata = self.data["metadata"]
        self.assertTrue(metadata["run_complete"])
        self.assertTrue(metadata["instrumentation_complete"])
        self.assertFalse(any(row["reason"] == "base is external or unresolved statically"
                             for row in metadata["unresolved_dependencies"]))
        self.assertTrue(metadata["environment_mismatch"])
        self.assertTrue(metadata["source_unchanged"])
        self.assertEqual(metadata["skip_integration"], "0")
        self.assertGreater(metadata["source_lines_executed"], 0)
        self.assertLess(metadata["source_lines_executed"], metadata["source_lines_possible"])
        self.assertEqual((self.output / "dependencies.json.gz").stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)

    def test_runtime_translation(self):
        # A fixture can test binding without pretending to be pinned-source evidence.
        data = json.loads(json.dumps(self.data))
        path = self.root / "binding-fixture"
        write_bundle(path, data)
        catalog = [{"id": node, "number": 100 + i}
                   for i, node in enumerate(reversed(data["tests"]))]
        catalog.append({"id": "new_test", "number": 999})
        bound = DependencyMap(path, catalog, require_pinned=False)
        self.assertEqual(bound.unmapped_ids, ["new_test"])
        expected = {test["number"] for test in catalog if "test_parameter[" in test["id"]}
        self.assertEqual(set(bound.sets[data["reads"]["data.txt"]]), expected)
        self.assertEqual(bound.read_consumers("data.txt"), {t["number"] for t in catalog})
        self.assertEqual(bound.import_consumers("sqlglot/leaf.py"), {t["number"] for t in catalog})
        with self.assertRaisesRegex(ValueError, "pinned SQLGlot"):
            DependencyMap(self.output, catalog)

    def test_failure_and_skip_report_incomplete(self):
        path = self.source / "tests/test_incomplete.py"
        path.write_text('''import pytest
def test_failure(subtests):
    with subtests.test(case=1):
        assert False
    with subtests.test(case=2):
        assert True
@pytest.mark.skip(reason="intentional check")
def test_skipped():
    pass
''')
        try:
            output = self.root / "partial"
            command = self.command.copy()
            command[command.index("--output") + 1] = str(output)
            result = subprocess.run(command, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            partial = read_bundle(output, require_pinned=False)[0]["metadata"]
            self.assertFalse(partial["run_complete"])
            self.assertIn("tests/test_incomplete.py::test_failure", partial["incomplete_tests"])
            self.assertIn("tests/test_incomplete.py::test_skipped", partial["incomplete_tests"])
            self.assertEqual(partial["outcomes"]["tests/test_incomplete.py::test_failure"]["call"], "failed")
        finally:
            path.unlink()

    def test_skips_child_processes_and_deadline_are_explicit(self):
        cases = [
            ("skip", 'import pytest\n@pytest.mark.skip(reason="example")\ndef test_extra(): pass\n', 0),
            ("child", 'import subprocess, sys\ndef test_extra():\n    subprocess.run([sys.executable, "-c", "pass"], check=True)\n', 0),
            ("thread", 'import threading\ndef test_extra():\n    worker = threading.Thread(target=lambda: None)\n    worker.start()\n    worker.join()\n', 0),
            ("deadline", 'import time\ndef test_extra(): time.sleep(20)\n', 1),
        ]
        for name, source, expected in cases:
            with self.subTest(case=name):
                path = self.source / "tests/test_extra.py"
                path.write_text(source)
                try:
                    output = self.root / name
                    command = self.command.copy()
                    command[command.index("--output") + 1] = str(output)
                    if name == "deadline":
                        command[command.index("--timeout") + 1] = "2"
                    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    partial = read_bundle(output, require_pinned=False)[0]["metadata"]
                    if name == "skip":
                        self.assertFalse(partial["instrumentation_complete"])
                        self.assertTrue(partial["run_complete"])
                        self.assertIn("tests/test_extra.py::test_extra", partial["incomplete_tests"])
                    elif name in {"child", "thread"}:
                        self.assertTrue(partial["instrumentation_complete"])
                        self.assertEqual(partial["instrumentation_issues"], [])
                    else:
                        self.assertFalse(partial["instrumentation_complete"])
                        self.assertFalse(partial["run_complete"])
                        self.assertTrue(any("deadline" in issue for issue in partial["instrumentation_issues"]))
                finally:
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
