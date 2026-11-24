"""Collect dependencies from an unmodified checkout, without any sample inputs.

Run in a dedicated process: the Python audit hook cannot be unregistered.
Pytest runs serially; shared fixtures and collection use conservative consumers.
"""

import argparse
import _thread
import ast
import base64
import builtins
from collections import defaultdict
from contextlib import contextmanager
import dis
import functools
import hashlib
import importlib
import importlib.util
import importlib.metadata
import importlib.machinery
import json
import os
from pathlib import Path
import platform
import signal
import sys
import threading
import tempfile
import time
import types

import pytest

from artifact_bundle import BASE_IMAGE, SCHEMA, INSTRUMENTATION_VERSION, write_bundle
from dependency_structure import analyze


def source_manifest(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not any(part in {".git", "__pycache__", ".pytest_cache"}
                                      for part in path.relative_to(root).parts)
    }


def source_index(root, manifest):
    """Index executable line positions, enclosing functions, and static imports."""
    executable, functions, imports, modules = {}, {}, {}, {}
    for name in manifest:
        if name.endswith(".py"):
            module = name[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            modules[module] = name
    for module, name in modules.items():
        source = (root / name).read_bytes()
        tree = ast.parse(source, filename=name)
        lines = set()

        def code_lines(code):
            # Python 3.13 emits None for bytecode with no source-line mapping.
            lines.update(line for _, line in dis.findlinestarts(code)
                         if line is not None and line > 0)
            for constant in code.co_consts:
                if isinstance(constant, types.CodeType):
                    code_lines(constant)

        code_lines(compile(source, str(root / name), "exec"))
        executable[name] = lines
        definitions = []

        def visit(node, parents=()):
            scope = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            names = parents + (node.name,) if scope else parents
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                definitions.append([".".join(names), start, node.end_lineno])
            for child in ast.iter_child_nodes(node):
                visit(child, names)

        visit(tree)
        functions[name] = definitions
        dependencies = set()
        package = module if name.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                if node.level:
                    try:
                        base = importlib.util.resolve_name(base, package)
                    except (ImportError, ValueError):
                        continue
                dependencies.add(base)
                dependencies.update(f"{base}.{alias.name}" for alias in node.names
                                    if f"{base}.{alias.name}" in modules)
        imports[name] = sorted(dependencies)
    return executable, functions, imports, modules


class Recorder:
    def __init__(self, root):
        self.root = str(root) + os.sep
        self.active = False
        self.context = ("collection", "")
        self.lines = defaultdict(lambda: defaultdict(set))
        self.functions = defaultdict(lambda: defaultdict(set))
        self.reads = defaultdict(set)
        self.loader_reads = defaultdict(set)
        self.source_observers = set()
        self.imports = defaultdict(set)
        self.module_files = {}
        self.classes = {}
        self.unresolved = []
        self.paths = {}
        self.code_paths = {}
        self.issues = set()
        self.scoped_issues = []
        self.expected_children = []
        self.expected_tasks = []
        self.threads_seen = False
        self.local = threading.local()
        self.main_thread = threading.get_ident()
        self.monitoring = hasattr(sys, "monitoring")
        self.tool_id = 3

    def relative(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return None
        path = os.fsdecode(path)
        # Resolve relative opens against their current cwd, not a cached cwd.
        path = os.path.abspath(path)
        if path not in self.paths:
            resolved = os.path.realpath(path)
            name = resolved[len(self.root):] if resolved.startswith(self.root) else None
            if name and any(p in {".git", "__pycache__", ".pytest_cache"} for p in name.split("/")):
                name = None
            self.paths[path] = name
        return self.paths[path]

    def current(self):
        if threading.get_ident() != self.main_thread:
            return ("background", "")
        return self.context

    def gap(self, reason, context=None):
        phase, consumer = context or self.current()
        if consumer and phase not in {"collection", "background", "child_startup"}:
            self.scoped_issues.append({"phase": phase, "consumer": consumer, "reason": reason})
        else:
            self.issues.add(reason)

    @contextmanager
    def scope(self, phase, consumer):
        previous = self.context
        self.context = (phase, consumer)
        if self.monitoring:
            sys.monitoring.restart_events()
        try:
            yield
        finally:
            self.context = previous
            if self.monitoring:
                sys.monitoring.restart_events()

    def line(self, code, number):
        if not self.active:
            return
        name = self.code_path(code)
        if name:
            context = self.current()
            self.lines[context][name].add(number)
            if code.co_name != "<module>":
                qualified = getattr(code, "co_qualname", code.co_name).replace(".<locals>.", ".")
                self.functions[context][name].add((qualified, code.co_firstlineno))
        # Re-enable locations on every phase/fixture change. Each line only
        # needs to be recorded once per context on the serial pytest thread.
        if self.monitoring and (not name or threading.get_ident() == self.main_thread):
            return sys.monitoring.DISABLE

    def code_path(self, code):
        filename = code.co_filename
        if filename not in self.code_paths:
            self.code_paths[filename] = self.relative(filename)
        return self.code_paths[filename]

    def trace(self, frame, event, arg):
        if not self.active:
            return None
        if not self.code_path(frame.f_code):
            return None
        if event == "line":
            self.line(frame.f_code, frame.f_lineno)
        return self.trace

    def audit(self, event, args):
        if not self.active or getattr(self.local, "busy", False):
            return
        self.local.busy = True
        try:
            if event == "open":
                path, mode, flags = args
                # Includes builtins.open, io.open/Path.read_text and os.open.
                readable = ("r" in mode or "+" in mode) if isinstance(mode, str) else (
                    flags & os.O_ACCMODE != os.O_WRONLY)
                name = self.relative(path)
                if readable and name:
                    target = self.loader_reads if getattr(self.local, "loader_depth", 0) else self.reads
                    target[self.current()].add(name)
            elif event in {"subprocess.Popen", "os.fork", "os.posix_spawn"}:
                if not getattr(self.local, "spawn_depth", 0):
                    self.gap("child process observed without instrumented startup")
        finally:
            self.local.busy = False

    def record_import(self, caller, names):
        if not self.active:
            return
        origin = self.relative(caller) or "<external>"
        expanded = set(names)
        for name in names:
            parts = name.split(".")
            expanded.update(".".join(parts[:count]) for count in range(1, len(parts))
                            if ".".join(parts[:count]) in sys.modules)
        for name in expanded:
            if not name:
                continue
            self.imports[self.current()].add((origin, name))
            module = sys.modules.get(name)
            filename = self.relative(getattr(module, "__file__", None))
            if filename:
                self.module_files[name] = filename

    def start(self):
        from process_dependencies import install
        self.restore_process_hooks = install(self) if os.environ.get("SELECTOR_TRACE_DIRECTORY") else lambda: None
        self.original_get_data = importlib.machinery.SourceFileLoader.get_data
        self.original_sourceless_get_data = importlib.machinery.SourcelessFileLoader.get_data

        def loader_read(original):
            def get_data(loader, path):
                depth = getattr(self.local, "loader_depth", 0)
                self.local.loader_depth = depth + 1
                try:
                    return original(loader, path)
                finally:
                    self.local.loader_depth = depth
            return get_data

        importlib.machinery.SourceFileLoader.get_data = loader_read(self.original_get_data)
        importlib.machinery.SourcelessFileLoader.get_data = loader_read(self.original_sourceless_get_data)
        self.original_import = builtins.__import__
        self.original_getattr = builtins.getattr
        self.original_import_module = importlib.import_module
        self.original_thread_start = threading.Thread.start
        self.original_low_thread_start = _thread.start_new_thread
        self.original_build_class = builtins.__build_class__

        def attribute_lookup(obj, name, /, *default):
            result = self.original_getattr(obj, name, *default)
            if self.active and isinstance(name, str) and (
                    name.startswith(("co_", "tb_")) or name in {"__code__", "f_lineno", "f_code"}):
                try:
                    caller = sys._getframe(1).f_code.co_filename
                except ValueError:
                    caller = None
                if self.relative(caller):
                    self.source_observers.add(self.current())
            return result

        def build_class(function, name, *bases, **kwargs):
            result = self.original_build_class(function, name, *bases, **kwargs)
            path = self.code_path(function.__code__)
            if path and isinstance(result, type):
                def qualified(cls):
                    return cls.__module__ + "." + cls.__qualname__
                overrides = {}
                for method, value in vars(result).items():
                    if isinstance(value, (types.FunctionType, classmethod, staticmethod, property)):
                        for parent in result.__mro__[1:]:
                            if method in vars(parent):
                                overrides[method] = qualified(parent) + "." + method
                                break
                self.classes[qualified(result)] = {"file": path, "line": function.__code__.co_firstlineno,
                    "bases": [qualified(base) for base in result.__bases__],
                    "mro": [qualified(base) for base in result.__mro__], "overrides": overrides}
            return result

        def thread_start(thread, *args, **kwargs):
            self.threads_seen = True
            if self.monitoring:
                sys.monitoring.restart_events()
            return self.original_thread_start(thread, *args, **kwargs)

        def low_thread_start(*args, **kwargs):
            self.threads_seen = True
            function, arguments = args[:2]
            keywords = args[2] if len(args) > 2 else kwargs.get("kwargs", {})

            def traced():
                sys.settrace(self.trace)
                return function(*arguments, **keywords)

            return self.original_low_thread_start(traced, ())

        def importing(name, globals=None, locals=None, fromlist=(), level=0):
            # The interpreter can call __import__ directly from C during
            # startup. Such an import has no calling Python frame.
            try:
                caller = sys._getframe(1).f_code.co_filename
            except ValueError:
                caller = None
            result = self.original_import(name, globals, locals, fromlist, level)
            resolved = name
            if level:
                package = (globals or {}).get("__package__") or (globals or {}).get("__name__", "")
                resolved = importlib.util.resolve_name("." * level + name, package)
            names = {resolved, getattr(result, "__name__", resolved)}
            names.update(f"{resolved}.{item}" for item in (fromlist or ())
                         if f"{resolved}.{item}" in sys.modules)
            self.record_import(caller, names)
            return result

        def import_module(name, package=None):
            try:
                caller = sys._getframe(1).f_code.co_filename
            except ValueError:
                caller = None
            try:
                result = self.original_import_module(name, package)
            except ImportError:
                path = self.relative(caller)
                if path:
                    self.unresolved.append({"kind": "dynamic_import", "file": path,
                        "expression": name, "reason": "runtime import failed"})
                raise
            self.record_import(caller, [result.__name__])
            return result

        builtins.__import__ = importing
        builtins.getattr = attribute_lookup
        importlib.import_module = import_module
        threading.Thread.start = thread_start
        _thread.start_new_thread = low_thread_start
        builtins.__build_class__ = build_class
        sys.addaudithook(self.audit)
        self.active = True
        # Legacy tracing is thread-local and independent of the main thread's
        # sys.monitoring.DISABLE optimization. Keep every background line.
        self.old_thread_trace = threading.gettrace()
        threading.settrace(self.trace)
        if self.monitoring:
            sys.monitoring.use_tool_id(self.tool_id, "blast-radius-dependencies")
            sys.monitoring.register_callback(self.tool_id, sys.monitoring.events.LINE, self.line)
            sys.monitoring.set_events(self.tool_id, sys.monitoring.events.LINE)
            sys.monitoring.restart_events()
        else:
            self.old_trace = sys.gettrace()
            sys.settrace(self.trace)

    def stop(self):
        self.active = False
        if self.monitoring:
            if sys.monitoring.get_events(self.tool_id) != sys.monitoring.events.LINE:
                self.issues.add("monitoring events were changed during instrumentation")
            sys.monitoring.set_events(self.tool_id, 0)
            sys.monitoring.register_callback(self.tool_id, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(self.tool_id)
        else:
            if sys.gettrace() != self.trace:
                self.issues.add("Python trace function was replaced during instrumentation")
            sys.settrace(self.old_trace)
        threading.settrace(self.old_thread_trace)
        builtins.__import__ = self.original_import
        builtins.getattr = self.original_getattr
        importlib.import_module = self.original_import_module
        threading.Thread.start = self.original_thread_start
        _thread.start_new_thread = self.original_low_thread_start
        builtins.__build_class__ = self.original_build_class
        importlib.machinery.SourceFileLoader.get_data = self.original_get_data
        importlib.machinery.SourcelessFileLoader.get_data = self.original_sourceless_get_data
        self.restore_process_hooks()


class Plugin:
    def __init__(self, recorder):
        self.recorder = recorder
        self.nodes = []
        self.outcomes = defaultdict(dict)
        self.collection_errors = []
        self.failure_diagnostics = []
        self.subtest_failures = set()
        self.fixtures = {}
        self.fixture_users = defaultdict(set)
        self.fixture_edges = set()
        self.test_fixtures = {}
        self.unresolved = []

    def fixture_key(self, fixturedef):
        function = fixturedef.func
        code = getattr(function, "__code__", None)
        path = self.recorder.relative(code.co_filename) if code else None
        path = path or "<external>/" + getattr(function, "__module__", "unknown")
        line = code.co_firstlineno if code else 0
        name = getattr(function, "__qualname__", fixturedef.argname)
        key = f"{path}:{line}:{name}:{fixturedef.argname}:{fixturedef.scope}:{fixturedef.baseid}"
        self.fixtures[key] = {"name": fixturedef.argname, "file": path, "line": line,
            "qualname": name, "scope": fixturedef.scope, "baseid": fixturedef.baseid,
            "dependencies": list(fixturedef.argnames)}
        return key

    def record_fixtures(self, item, runtime=False):
        try:
            info = item._fixtureinfo
            available = info.name2fixturedefs
            definitions = {name: choices[-1] for name, choices in available.items() if choices}
            if runtime:
                definitions.update(item._request._fixture_defs)
            record = self.test_fixtures.setdefault(item.nodeid, {
                "explicit": sorted(info.argnames), "initial": sorted(info.initialnames),
                "autouse": sorted(item.session._fixturemanager._getautousenames(item)), "used": {},
            })

            def link(definition, visited):
                key = self.fixture_key(definition)
                self.fixture_users[key].add(item.nodeid)
                if key in visited:
                    return
                visited.add(key)
                for name in definition.argnames:
                    if name == "request":
                        continue
                    target = definitions.get(name)
                    if target is definition:
                        choices = list(available.get(name, []))
                        index = choices.index(definition) if definition in choices else 0
                        target = choices[index - 1] if index else None
                    if target is None:
                        self.unresolved.append({"kind": "fixture", "test": item.nodeid,
                            "expression": name, "reason": "unresolved transitive fixture of " + key})
                    else:
                        self.fixture_edges.add((key, self.fixture_key(target)))
                        link(target, visited)

            for name in sorted(set(info.names_closure) | (set(definitions) if runtime else set())):
                if name == "request":
                    continue
                definition = definitions.get(name)
                if definition is None:
                    self.unresolved.append({"kind": "fixture", "test": item.nodeid,
                        "expression": name, "reason": "fixture definition missing"})
                    continue
                key = self.fixture_key(definition)
                record["used"][name] = key
                link(definition, set())
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            self.unresolved.append({"kind": "fixture", "test": item.nodeid,
                "expression": "pytest fixture metadata", "reason": type(error).__name__})

    def pytest_collection_finish(self, session):
        self.nodes = sorted(item.nodeid for item in session.items)
        for item in session.items:
            self.record_fixtures(item)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)
            self.failure_diagnostics.append({"nodeid": report.nodeid, "phase": "collection",
                                             "traceback": report.longreprtext[:16000]})

    def pytest_runtest_logreport(self, report):
        # Subtests retain the parent's node ID. A later PASSED parent must
        # never erase an earlier subtest failure.
        old = self.outcomes[report.nodeid].get(report.when)
        if old != "failed":
            self.outcomes[report.nodeid][report.when] = report.outcome
        if report.failed and hasattr(report, "context"):
            self.subtest_failures.add(report.nodeid)
        if report.failed and len(self.failure_diagnostics) < 10:
            self.failure_diagnostics.append({"nodeid": report.nodeid, "phase": report.when,
                "traceback": report.longreprtext[:16000],
                "captured": [[name, text[:16000]] for name, text in report.sections]})

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        with self.recorder.scope("setup", item.nodeid):
            yield
            self.record_fixtures(item, runtime=True)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        with self.recorder.scope("call", item.nodeid):
            yield
            self.record_fixtures(item, runtime=True)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item):
        with self.recorder.scope("teardown", item.nodeid):
            yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        # The stable fixture identity gathers all consumers, including cache
        # hits, autouse and transitive use. Parameter instances share a union.
        prefix = self.fixture_key(fixturedef)
        self.fixture_users[prefix].add(request._pyfuncitem.nodeid)
        original_addfinalizer = request.addfinalizer

        def addfinalizer(callback):
            @functools.wraps(callback)
            def finalizer():
                with self.recorder.scope("fixture_teardown", prefix):
                    return callback()
            original_addfinalizer(finalizer)

        request.addfinalizer = addfinalizer
        with self.recorder.scope("fixture_setup", prefix):
            yield


def artifact(root, recorder, plugin, manifest, index, metadata):
    executable, functions, static_imports, modules = index
    source_modules = dict(modules)
    nodes = plugin.nodes
    line_tests = defaultdict(lambda: defaultdict(set))
    function_tests = defaultdict(lambda: defaultdict(set))
    file_tests = defaultdict(set)
    loader_tests = defaultdict(set)
    import_tests = defaultdict(lambda: defaultdict(set))
    phase_stats = {}
    def consumers_for(context):
        phase, prefix = context
        if phase.startswith("fixture_"):
            return {i for i, node in enumerate(nodes) if node in plugin.fixture_users[prefix]}
        elif phase in {"collection", "background"}:
            return set(range(len(nodes)))
        return {i for i, node in enumerate(nodes) if node == prefix}

    contexts = set(recorder.lines) | set(recorder.reads) | set(recorder.imports) | set(recorder.loader_reads)
    for context in sorted(contexts):
        phase, prefix = context
        consumers = consumers_for(context)
        phase_stats[phase] = phase_stats.get(phase, 0) + sum(len(v) for v in recorder.lines[context].values())
        for name, lines in recorder.lines[context].items():
            for line in lines:
                line_tests[name][line].update(consumers)
        for name, executed in recorder.functions[context].items():
            for function, start, end in functions.get(name, []):
                if any((qualified == function or qualified.startswith(function + "."))
                       and start <= first <= end for qualified, first in executed):
                    function_tests[name][(function, start, end)].update(consumers)
        for name in recorder.reads[context]:
            file_tests[name].update(consumers)
        for name in recorder.loader_reads[context]:
            loader_tests[name].update(consumers)
        for origin, module in recorder.imports[context]:
            import_tests[origin][module].update(consumers)

    # Consumer sets are interned; integers here index stable node IDs, never
    # the runtime test numbers. Equal adjacent lines share a compact range.
    sets, set_numbers = [], {}

    def intern(consumers):
        key = tuple(sorted(consumers))
        if key not in set_numbers:
            set_numbers[key] = len(sets)
            sets.append(list(key))
        return set_numbers[key]

    # Attribute/global identifiers are conservative aliases: matching names
    # across different classes are unioned, never assumed disjoint.
    attribute_tests = defaultdict(set)
    reflection_tests, dynamic_attribute_tests = set(), set()
    for context in recorder.source_observers:
        reflection_tests.update(consumers_for(context))
    reflection_names = {"getsource", "getsourcelines", "findsource", "getframeinfo", "currentframe",
                        "_getframe", "format_exc", "format_exception", "extract_tb", "format_stack", "stack"}
    for path in sorted(executable):
        tree = ast.parse((root / path).read_bytes())
        for node in ast.walk(tree):
            name = None
            reflective = dynamic = False
            if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
                name = node.attr if isinstance(node, ast.Attribute) else node.id
                reflective = name.startswith(("co_", "tb_")) or name in {"__code__", "f_lineno", "f_code"}
            elif isinstance(node, ast.Call):
                called = getattr(node.func, "attr", getattr(node.func, "id", ""))
                reflective = called in reflection_names
                if called in {"getattr", "hasattr"}:
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                        name = node.args[1].value
                    else:
                        dynamic = True
                if called in {"vars", "eval", "exec", "globals", "locals"}:
                    # Namespace/attribute discovery is relevant to binding
                    # changes. It does not itself inspect function bodies or
                    # source positions; subsequent __code__/frame/source reads
                    # are indexed separately. Otherwise an ordinary globals()
                    # in module initialization makes every test a source reader.
                    dynamic = True
            if name or reflective or dynamic:
                users = set()
                for line in range(node.lineno, node.end_lineno + 1):
                    users.update(line_tests[path].get(line, ()))
                if name:
                    attribute_tests[name].update(users)
                if reflective:
                    reflection_tests.update(users)
                if dynamic:
                    dynamic_attribute_tests.update(users)

    encoded_lines, encoded_functions = {}, {}
    for name, mapping in sorted(line_tests.items()):
        ranges = []
        for line, consumers in sorted(mapping.items()):
            group = intern(consumers)
            if ranges and ranges[-1][1] == line - 1 and ranges[-1][2] == group:
                ranges[-1][1] = line
            else:
                ranges.append([line, line, group])
        encoded_lines[name] = ranges
        definitions = []
        for (function, start, end), consumers in sorted(function_tests[name].items()):
            if consumers:
                definitions.append([function, start, end, intern(consumers)])
        if definitions:
            encoded_functions[name] = definitions
    modules.update(recorder.module_files)
    package_files = [name for name in executable if name.startswith("sqlglot/")]
    seen = sum(len(set(line_tests[name]) & executable[name]) for name in package_files)
    total = sum(len(executable[name]) for name in package_files)
    incomplete_nodes = [node for node in nodes
                        if any(plugin.outcomes[node].get(phase) != "passed"
                               for phase in ("setup", "call", "teardown"))]
    structure = analyze(root, source_modules)
    unresolved = structure.pop("unresolved") + recorder.unresolved + plugin.unresolved
    known_classes = {(row["file"], row["line"]) for row in recorder.classes.values()}
    unresolved = [issue for issue in unresolved if not (
        issue.get("kind") == "inheritance" and issue.get("reason") == "base is external or unresolved statically"
        and (issue.get("file"), issue.get("line")) in known_classes)]
    unresolved = [json.loads(row) for row in sorted({json.dumps(row, sort_keys=True) for row in unresolved})]
    missing_mappings = [node for node in nodes if not recorder.lines.get(("call", node))]
    gap_tests = set()
    scoped_gaps = []
    for issue in recorder.scoped_issues:
        affected = sorted(nodes[i] for i in consumers_for((issue["phase"], issue["consumer"])))
        if not affected:
            recorder.issues.add("unresolved owner of instrumentation gap: " + issue["reason"])
        gap_tests.update(affected)
        scoped_gaps.append({"reason": issue["reason"], "tests": affected})
    scoped_gaps = [json.loads(row) for row in sorted({json.dumps(row, sort_keys=True) for row in scoped_gaps})]
    metadata.update({
        "collected_tests": len(nodes), "incomplete_tests": incomplete_nodes,
        "outcomes": dict(sorted(plugin.outcomes.items())),
        "collection_errors": sorted(plugin.collection_errors),
        "subtest_failures": sorted(plugin.subtest_failures),
        "baseline_failures": sorted(node for node, phases in plugin.outcomes.items() if "failed" in phases.values()),
        "unresolved_dependencies": unresolved,
        "missing_mappings": missing_mappings,
        "instrumentation_issues": sorted(recorder.issues),
        "scoped_instrumentation_issues": scoped_gaps,
        "gap_tests": sorted(gap_tests),
        "read_provenance": "loader-separated-v1",
        "reflection_index": "executed-identifiers-v1",
        "run_complete": metadata["pytest_exit_code"] == 0 and not plugin.collection_errors
                        and all("setup" in plugin.outcomes[node] and "teardown" in plugin.outcomes[node]
                                for node in nodes),
        "instrumentation_complete": not recorder.issues and not gap_tests and not incomplete_nodes and not unresolved and not missing_mappings,
        "sound_for_unseen_changes": False,
        "source_lines_executed": seen, "source_lines_possible": total,
        "source_line_coverage_percent": round(100 * seen / total, 3) if total else None,
        "source_files_executed": sum(bool(line_tests[name]) for name in package_files),
        "source_files_total": len(package_files),
        "phase_line_observations": phase_stats,
        "limitations": [
            "Python children and pool tasks are traced; unreported children remain scoped gaps. Native I/O is not traced.",
            "Collection dependencies apply to all collected tests; fixture parameter instances share a union of their consumers.",
            "Fixture graphs use pytest private metadata; unavailable relationships are explicitly unresolved.",
            "Static imports include unexecuted branches; dynamic imports are observed only on exercised paths.",
            "Untaken branches, absent files, filesystem metadata checks and future changes are not covered by dynamic maps.",
            "Background threads use independent thread-local tracing; their observations conservatively apply to all tests.",
        ],
    })
    result = {
        "schema": SCHEMA, "metadata": metadata, "tests": nodes, "test_sets": sets,
        "lines": encoded_lines, "functions": encoded_functions,
        "reads": {name: intern(users) for name, users in sorted(file_tests.items())},
        "loader_reads": {name: intern(users) for name, users in sorted(loader_tests.items())},
        "attribute_reads": {name: intern(users) for name, users in sorted(attribute_tests.items())},
        "reflection_tests": intern(reflection_tests),
        "dynamic_attribute_tests": intern(dynamic_attribute_tests),
        "imports": {name: [[module, intern(users)] for module, users in sorted(deps.items())]
                    for name, deps in sorted(import_tests.items())},
        "static_imports": static_imports, "modules": dict(sorted(modules.items())),
        "source_sha256": manifest,
        "structure": structure, "runtime_classes": recorder.classes,
        "fixtures": plugin.fixtures,
        "fixture_edges": sorted([list(edge) for edge in plugin.fixture_edges]),
        "fixture_users": {key: sorted(users) for key, users in sorted(plugin.fixture_users.items())},
        "test_fixtures": plugin.test_fixtures,
    }
    return result


def environment_metadata(requirements):
    packages = {distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
                for distribution in importlib.metadata.distributions() if distribution.metadata["Name"]}
    environment = {"python": platform.python_version(), "implementation": platform.python_implementation(),
        "system": platform.system(), "machine": platform.machine(),
        "base_image": os.environ.get("BLAST_RADIUS_BASE_IMAGE"), "packages": dict(sorted(packages.items())),
        "skip_integration": os.environ["SKIP_INTEGRATION"], "pythonhashseed": os.environ.get("PYTHONHASHSEED")}
    mismatch = []
    if environment["system"] != "Linux" or environment["machine"] not in {"x86_64", "amd64"}:
        mismatch.append("generation did not run on linux/amd64")
    if sys.version_info[:2] != (3, 13) or environment["base_image"] != BASE_IMAGE:
        mismatch.append("generation did not identify the pinned Python 3.13 base image")
    if requirements:
        for line in requirements.read_text().splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.strip().split("==", 1)
                if packages.get(name.lower().replace("_", "-")) != version:
                    mismatch.append(f"locked requirement mismatch: {name}=={version}")
    else:
        mismatch.append("requirements.lock not verified")
    return environment, mismatch


def main():
    generation_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--report", type=Path, help="timing report outside the deterministic bundle")
    parser.add_argument("--allow-incomplete", action="store_true", help="export partial evidence with explicit runtime fallback flags")
    options = parser.parse_args()
    root, output = options.source.resolve(), options.output.resolve()
    if output == root or root in output.parents:
        parser.error("output must be outside the unmodified source tree")
    if options.timeout <= 0:
        parser.error("timeout must be positive")
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    manifest = source_manifest(root)
    baseline_files = {name: base64.b64encode((root / name).read_bytes()).decode("ascii") for name in manifest}
    index = source_index(root, manifest)
    provenance = json.loads(options.provenance.read_text()) if options.provenance else {"commit": None}
    requirements_sha256 = (hashlib.sha256(options.requirements.read_bytes()).hexdigest()
                           if options.requirements else None)
    environment, mismatch = environment_metadata(options.requirements)
    report_path = (options.report or output.parent / "generation-report.json").resolve()
    command = ["python", "build_dependencies.py", "--source", "<source>", "--output", "<output>",
               "--timeout", str(options.timeout)]
    if options.provenance:
        command += ["--provenance", "<provenance>"]
    if options.requirements:
        command += ["--requirements", "<requirements.lock>"]
    if options.allow_incomplete:
        command += ["--allow-incomplete"]
    os.chdir(root)
    sys.path.insert(0, str(root))
    recorder = Recorder(root)
    trace_directory = tempfile.TemporaryDirectory(prefix="selector-traces-")
    hook_directory = Path(trace_directory.name) / "hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text("from process_dependencies import startup\nstartup()\n")
    os.environ["SELECTOR_TRACE_ROOT"] = str(root)
    os.environ["SELECTOR_TRACE_DIRECTORY"] = trace_directory.name
    os.environ["PYTHONPATH"] = os.pathsep.join([str(hook_directory), str(Path(__file__).resolve().parent),
                                               str(root), os.environ.get("PYTHONPATH", "")])
    plugin = Plugin(recorder)
    started = time.monotonic()
    exit_code = 3

    def timed_out(signum, frame):
        recorder.issues.add("instrumentation deadline reached; partial artifact")
        raise KeyboardInterrupt("instrumentation deadline")

    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(options.timeout)
    recorder.start()
    try:
        exit_code = int(pytest.main(["tests", "-q", "-o", "addopts=", "-p", "no:cacheprovider"], plugins=[plugin]))
    finally:
        recorder.stop()
        signal.alarm(0)
        from process_dependencies import merge_children
        merge_children(recorder, trace_directory.name)
        child_diagnostics = []
        for path in sorted(Path(trace_directory.name).glob('*.stderr.log')):
            with path.open() as stream:
                diagnostic = stream.read(16000)
            if diagnostic:
                child_diagnostics.append(diagnostic)
            if len(child_diagnostics) >= 8:
                break
        trace_directory.cleanup()
        after = source_manifest(root)
        if after != manifest:
            recorder.issues.add("source tree changed during tests")
        metadata = {
            "source": provenance, "python": platform.python_version(), "pytest": pytest.__version__,
            "platform": platform.platform(), "skip_integration": "0",
            "engine": "sys.monitoring" if recorder.monitoring else "sys.settrace",
            "pytest_exit_code": exit_code, "elapsed_seconds": round(time.monotonic() - started, 3),
            "source_unchanged": after == manifest,
            "requirements_sha256": requirements_sha256,
            "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "instrumentation_version": INSTRUMENTATION_VERSION,
            "instrumentation_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ("build_dependencies.py", "dependency_structure.py", "artifact_bundle.py", "fetch_source.py", "process_dependencies.py")},
            "environment": environment, "environment_mismatch": mismatch,
            "generation_command": command,
        }
        result = artifact(root, recorder, plugin, manifest, index, metadata)
        result["baseline_files"] = baseline_files
        bundle = write_bundle(output, result)
        metadata["artifact_bytes"] = bundle["artifact"]["bytes"]
        metadata["manifest_bytes"] = (output / "manifest.json").stat().st_size
        metadata["uncompressed_bytes"] = bundle["artifact"]["uncompressed_bytes"]
        metadata["elapsed_seconds"] = round(time.monotonic() - generation_started, 3)
        # Host paths, PIDs and exception text are diagnostic only, never inputs
        # to selection or part of the deterministic checked-in bundle.
        metadata["child_diagnostics"] = child_diagnostics
        metadata["failure_diagnostics"] = plugin.failure_diagnostics
        report_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        report_path.chmod(0o644)
        print(json.dumps({key: metadata[key] for key in (
            "collected_tests", "run_complete", "instrumentation_complete", "source_lines_executed",
            "source_lines_possible", "source_line_coverage_percent", "artifact_bytes", "elapsed_seconds",
        )}, sort_keys=True))
    # Preserve partial evidence for inspection but never silently bless it.
    # Skips are legitimate in the upstream suite (and remain explicitly
    # incomplete). Runtime binding must flag any skipped ID in the live catalog.
    return 0 if options.allow_incomplete or (metadata["run_complete"] and not recorder.issues) else 1


if __name__ == "__main__":
    sys.exit(main())
