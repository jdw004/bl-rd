"""Synthetic child/task attribution; no sample labels or patches are used."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from artifact_bundle import read_bundle


class ProcessMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='selector-process-check-')
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / 'source'
        (cls.source / 'tests').mkdir(parents=True)
        (cls.source / 'data.txt').write_text('payload')
        (cls.source / 'worker.py').write_text('''from pathlib import Path
namespace = globals()

def left():
    return Path(__file__).with_name('data.txt').read_text()

def right():
    return 42

def background():
    return 99
''')
        (cls.source / 'tests/test_tasks.py').write_text('''import subprocess, sys, threading
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
import pytest
import worker

@pytest.fixture(scope='module')
def pool():
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn')) as executor:
        yield executor

def test_left(pool):
    assert pool.submit(worker.left).result() == 'payload'

def test_right(pool):
    assert pool.submit(worker.right).result() == 42

def test_python_child():
    subprocess.run([sys.executable, '-c', 'import worker; assert worker.left() == "payload"'], check=True)

def test_captured_child_exception():
    result = subprocess.run([sys.executable, '-c', 'raise RuntimeError("child diagnostic check")'], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'child diagnostic check' in result.stderr

def test_untraced_child():
    subprocess.run(['/bin/sh', '-c', 'exit 0'], check=True)

def test_source_reader():
    assert 'def left' in Path(worker.__file__).read_text()

def test_code_positions():
    assert worker.left.__code__.co_firstlineno > 0

def test_computed_code_lookup():
    assert 'left' in str(getattr(worker.left, ''.join(['__', 'code', '__'])))

def test_background():
    thread = threading.Thread(target=worker.background)
    thread.start()
    thread.join()
''')
        cls.output = cls.root / 'maps'
        cls.command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'build_dependencies.py'),
            '--source', str(cls.source), '--output', str(cls.output), '--timeout', '25', '--allow-incomplete']
        result = subprocess.run(cls.command, capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.data, _ = read_bundle(cls.output, require_pinned=False)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def names(self, group):
        return {self.data['tests'][n].rsplit('::',1)[-1] for n in self.data['test_sets'][group]}

    def function_users(self, name):
        return set().union(*(self.names(g) for n, _, _, g in self.data['functions']['worker.py'] if n == name))

    def test_shared_pool_task_ownership(self):
        self.assertEqual(self.function_users('left'), {'test_left', 'test_python_child'})
        self.assertEqual(self.function_users('right'), {'test_right'})
        self.assertEqual(self.names(self.data['reads']['data.txt']), {'test_left','test_python_child'})

    def test_loader_read_is_separate_from_source_inspection(self):
        self.assertEqual(self.names(self.data['reads']['worker.py']), {'test_source_reader'})
        self.assertIn('worker.py', self.data['loader_reads'])

    def test_namespace_lookup_is_not_source_inspection(self):
        self.assertEqual(self.names(self.data['reflection_tests']), {'test_code_positions', 'test_computed_code_lookup'})
        self.assertEqual(self.names(self.data['dynamic_attribute_tests']),
                         {node.rsplit('::', 1)[-1] for node in self.data['tests']})

    def test_untraced_child_gap_is_owned_by_one_test(self):
        self.assertEqual(self.data['metadata']['instrumentation_issues'], [])
        self.assertEqual(self.data['metadata']['gap_tests'], ['tests/test_tasks.py::test_untraced_child'])

    def test_thread_observations_remain_conservative(self):
        self.assertEqual(self.function_users('background'), {n.rsplit('::',1)[-1] for n in self.data['tests']})

    def test_captured_exception_saved_outside_reproducible_bundle(self):
        report = json.loads((self.root / 'generation-report.json').read_text())
        self.assertTrue(any('RuntimeError: child diagnostic check' in text
                            for text in report['child_diagnostics']))
        self.assertNotIn('child_diagnostics', self.data['metadata'])
        self.assertTrue(self.data['metadata']['run_complete'])

    def test_spawned_process_ids_do_not_change_bundle_bytes(self):
        command = self.command.copy()
        repeat = self.root / 'repeat'
        command[command.index('--output') + 1] = str(repeat)
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ('dependencies.json.gz', 'manifest.json'):
            self.assertEqual((self.output / name).read_bytes(), (repeat / name).read_bytes(), name)


if __name__ == '__main__':
    unittest.main()
