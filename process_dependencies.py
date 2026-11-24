"""Generation-only tracing of spawned Python processes and process-pool tasks."""

import atexit
import faulthandler
from contextlib import contextmanager
import gzip
import inspect
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

ROOT_KEY = 'SELECTOR_TRACE_ROOT'
DIR_KEY = 'SELECTOR_TRACE_DIRECTORY'
CONTEXT_KEY = 'SELECTOR_TRACE_CONTEXT'
_worker = None
_task_contexts = set()
_completed = set()
_boot_context = None
_diagnostic_stream = None


def startup():
    """Keep child startup/fatal errors outside pytest's captured subprocess I/O."""
    global _diagnostic_stream
    directory = os.environ.get(DIR_KEY)
    if not directory:
        return
    _diagnostic_stream = open(Path(directory) / (str(os.getpid()) + '.stderr.log'), 'a')
    faulthandler.enable(file=_diagnostic_stream)
    original_hook = sys.excepthook

    def exception_hook(kind, error, tb):
        traceback.print_exception(kind, error, tb, file=_diagnostic_stream)
        _diagnostic_stream.flush()
        original_hook(kind, error, tb)

    sys.excepthook = exception_hook
    try:
        bootstrap()
    except BaseException:
        traceback.print_exc(file=_diagnostic_stream)
        _diagnostic_stream.flush()
        raise


def snapshot(recorder):
    contexts = set(recorder.lines) | set(recorder.functions) | set(recorder.reads) | set(recorder.imports) | set(recorder.loader_reads) | recorder.source_observers
    return {'observations': [
        {'context': list(context),
         'source_observer': context in recorder.source_observers,
         'lines': {p: sorted(v) for p, v in recorder.lines[context].items()},
         'functions': {p: sorted(v) for p, v in recorder.functions[context].items()},
         'reads': sorted(recorder.reads[context]),
         'loader_reads': sorted(recorder.loader_reads[context]),
         'imports': sorted(recorder.imports[context])}
        for context in sorted(contexts)],
        'issues': sorted(recorder.issues), 'scoped_issues': recorder.scoped_issues,
        'unresolved': recorder.unresolved, 'module_files': recorder.module_files,
        'classes': recorder.classes, 'expected_children': recorder.expected_children,
        'expected_tasks': recorder.expected_tasks}


def flush(finished=False):
    if _worker is None:
        return
    # Serialization must not record its own I/O or recursively enter flush.
    was_active = _worker.active
    _worker.active = False
    try:
        payload = snapshot(_worker)
        payload.update(owner=_boot_context, consumers=sorted(_task_contexts),
                       completed=sorted(_completed), finished=finished)
        destination = Path(os.environ[DIR_KEY]) / (str(os.getpid()) + '.json.gz')
        temporary = destination.with_suffix('.pending')
        temporary.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode(), compresslevel=1, mtime=0))
        temporary.replace(destination)
    finally:
        _worker.active = was_active


def bootstrap():
    global _worker, _boot_context
    if _worker is not None or not os.environ.get(ROOT_KEY):
        return
    from build_dependencies import Recorder
    _boot_context = tuple(json.loads(os.environ.get(CONTEXT_KEY, '["collection",""]')))
    _worker = Recorder(Path(os.environ[ROOT_KEY]))
    _worker.context = ('child_startup', '')
    _worker.start()
    atexit.register(flush, True)
    original_exit = os._exit

    def exiting(code):
        flush(True)
        original_exit(code)

    os._exit = exiting
    flush()


def run_task(context, token, function, args, kwargs):
    bootstrap()
    if _worker is None:
        return function(*args, **kwargs)
    context = tuple(context)
    _task_contexts.add(context)
    try:
        with _worker.scope(*context):
            return function(*args, **kwargs)
    finally:
        _completed.add(token)
        flush()


@contextmanager
def spawn_context(recorder):
    previous = os.environ.get(CONTEXT_KEY)
    os.environ[CONTEXT_KEY] = json.dumps(recorder.current())
    depth = getattr(recorder.local, 'spawn_depth', 0)
    recorder.local.spawn_depth = depth + 1
    try:
        yield
    finally:
        recorder.local.spawn_depth = depth
        if previous is None:
            os.environ.pop(CONTEXT_KEY, None)
        else:
            os.environ[CONTEXT_KEY] = previous


def install(recorder):
    import concurrent.futures
    import multiprocessing.process
    import subprocess
    old_submit = concurrent.futures.ProcessPoolExecutor.submit
    old_start = multiprocessing.process.BaseProcess.start
    old_popen = subprocess.Popen.__init__
    popen_signature = inspect.signature(old_popen)

    def submit(pool, function, /, *args, **kwargs):
        context = recorder.current()
        token = uuid.uuid4().hex
        future = old_submit(pool, run_task, context, token, function, args, kwargs)
        recorder.expected_tasks.append({'token': token, 'context': list(context)})
        return future

    def start(process, *args, **kwargs):
        context = recorder.current()
        with spawn_context(recorder):
            result = old_start(process, *args, **kwargs)
        recorder.expected_children.append({'pid': process.pid, 'context': list(context)})
        return result

    def popen(process, *args, **kwargs):
        context = recorder.current()
        bound = popen_signature.bind(process, *args, **kwargs)
        env = bound.arguments.get('env')
        with spawn_context(recorder):
            if env is not None:
                env = dict(env)
                for key in (ROOT_KEY, DIR_KEY, CONTEXT_KEY):
                    env[key] = os.environ[key]
                paths = os.environ['PYTHONPATH'].split(os.pathsep)[:2]
                env['PYTHONPATH'] = os.pathsep.join(paths + [env.get('PYTHONPATH', '')])
                bound.arguments['env'] = env
            result = old_popen(*bound.args, **bound.kwargs)
        recorder.expected_children.append({'pid': process.pid, 'context': list(context)})
        return result

    concurrent.futures.ProcessPoolExecutor.submit = submit
    multiprocessing.process.BaseProcess.start = start
    subprocess.Popen.__init__ = popen

    def restore():
        concurrent.futures.ProcessPoolExecutor.submit = old_submit
        multiprocessing.process.BaseProcess.start = old_start
        subprocess.Popen.__init__ = old_popen

    return restore


def merge_children(recorder, directory):
    payloads = {}
    for path in sorted(Path(directory).glob('*.json.gz')):
        try:
            payloads[int(path.name.split('.')[0])] = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError, EOFError):
            recorder.issues.add('unreadable child dependency report')
    expected_children = list(recorder.expected_children)
    expected_tasks = list(recorder.expected_tasks)
    completed = set()
    managed_pids = {row['pid'] for row in expected_children}
    managed_pids.update(row['pid'] for payload in payloads.values() for row in payload['expected_children'])
    for pid, payload in payloads.items():
        owner = tuple(payload['owner'])
        consumers = {tuple(c) for c in payload['consumers']} or {owner}
        for row in payload['observations']:
            context = tuple(row['context'])
            targets = consumers if context[0] == 'child_startup' else {context}
            for target in targets:
                if row.get('source_observer'):
                    recorder.source_observers.add(target)
                for path, values in row['lines'].items():
                    recorder.lines[target][path].update(values)
                for path, values in row['functions'].items():
                    recorder.functions[target][path].update(tuple(v) for v in values)
                recorder.reads[target].update(row['reads'])
                recorder.loader_reads[target].update(row['loader_reads'])
                recorder.imports[target].update(tuple(v) for v in row['imports'])
        recorder.module_files.update(payload['module_files'])
        recorder.classes.update(payload['classes'])
        recorder.unresolved.extend(payload['unresolved'])
        recorder.scoped_issues.extend(payload['scoped_issues'])
        completed.update(payload['completed'])
        expected_children.extend(payload['expected_children'])
        expected_tasks.extend(payload['expected_tasks'])
        for consumer in consumers:
            for reason in payload['issues']:
                recorder.gap(reason, consumer)
            repository_observed = bool(payload['module_files']) or any(
                row['lines'] or row['reads'] or row['loader_reads'] for row in payload['observations'])
            # Interpreter helpers (e.g. resource tracking) may outlive pytest.
            # Managed children and every process with repository evidence must
            # report shutdown; an empty unmanaged helper contributes no evidence.
            if not payload['finished'] and (pid in managed_pids or repository_observed or payload['completed']):
                recorder.gap('child did not report clean shutdown', consumer)
    for row in expected_children:
        if row['pid'] not in payloads:
            recorder.gap('child process has no coverage report', tuple(row['context']))
    for row in expected_tasks:
        if row['token'] not in completed:
            recorder.gap('process-pool task has incomplete coverage', tuple(row['context']))
