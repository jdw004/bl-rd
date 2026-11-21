"""Bounded subprocess capture for local evaluation tools, never the selector."""

import os
import resource
import selectors
import signal
import subprocess
import time
from types import SimpleNamespace

STDOUT_LIMIT = 2_000_000  # Decimal MB is stricter than 2 MiB.


def run_bounded(command, *, cwd=None, timeout=180, output_limit=STDOUT_LIMIT):
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    failure = None
    with selectors.DefaultSelector() as poll:
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            poll.register(stream, selectors.EVENT_READ, name)
        try:
            while poll.get_map() or process.poll() is None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    failure = "deadline_exceeded"
                    break
                for key, _ in poll.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        poll.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    buffer.extend(chunk[:max(0, output_limit + 1 - len(buffer))])
                    if len(buffer) > output_limit:
                        failure = key.data + "_limit_exceeded"
                        break
                if failure:
                    break
        finally:
            # Kill the group even if the parent exited with pipe-owning children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return SimpleNamespace(stdout=bytes(buffers["stdout"]), stderr=bytes(buffers["stderr"]),
                           returncode=process.returncode, failure=failure,
                           wall_seconds=time.monotonic() - started,
                           cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)
