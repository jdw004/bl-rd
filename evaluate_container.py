"""Three bounded linux/amd64 runs; sample labels stay outside the container."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from evaluate import ROOT, load_inputs, summarize
from process_runner import run_bounded

# This validation wrapper measures only the selector child's user+system CPU.
# It is not official whole-container accounting and is not the image CMD.
MEASURE = """
import json, resource, subprocess, sys
result = subprocess.run([sys.executable, '-B', '/app/selector.py', '--strategy', *sys.argv[1:]])
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({'measurement': 'selector_child_cpu', 'seconds': usage.ru_utime + usage.ru_stime}), file=sys.stderr)
sys.exit(result.returncode)
"""

AUDIT = """
import json, os, platform, tempfile
from pathlib import Path
from artifact_bundle import read_bundle
assert os.getuid() == 65534 and os.getgid() == 65534
assert os.environ['HOME'] == '/tmp' and os.environ['SKIP_INTEGRATION'] == '0'
assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'
assert platform.machine() == 'x86_64' and platform.system() == 'Linux'
expected = {'selector.py', 'selection.py', 'dependency_map.py', 'artifact_bundle.py',
            'unified_diff.py', 'patch_analysis.py', 'maps'}
if os.environ.get('SELECTOR_PROBE_IMAGE') == '1':
    expected.update({'runtime_probe.py', 'probe_runner.py', 'process_runner.py'})
assert {p.name for p in Path('/app').iterdir()} == expected
assert {p.name for p in Path('/app/maps').iterdir()} == {'manifest.json', 'dependencies.json.gz'}
assert {p.name for p in Path('/data').iterdir()} == {'commits', 'tests.json'}
for p in Path('/app').rglob('*'):
    if p.is_file():
        assert os.access(p, os.R_OK), str(p)
        with p.open('rb') as stream:
            while stream.read(65536): pass
try:
    with open('/app/write-check', 'w') as stream: pass
except OSError: pass
else: raise AssertionError('root filesystem writable')
with tempfile.TemporaryFile(dir='/tmp') as stream: stream.write(b'ok')
data, manifest = read_bundle(Path('/app/maps'))
print(json.dumps({'readable': True, 'revision': data['metadata']['source']['commit'],
                  'artifact_sha256': manifest['artifact']['sha256']}))
"""


def docker_command(image, data_dir, name):
    return ["docker", "run", "--name", name, "--pull", "never", "--platform", "linux/amd64",
            "--network", "none", "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "SKIP_INTEGRATION=0",
            "--mount", f"type=bind,source={data_dir.resolve() / 'commits'},target=/data/commits,readonly",
            "--mount", f"type=bind,source={data_dir.resolve() / 'tests.json'},target=/data/tests.json,readonly",
            image]


def container_run(image, data_dir, arguments):
    name = "blast-radius-eval-" + uuid.uuid4().hex
    try:
        result = run_bounded(docker_command(image, data_dir, name) + arguments, timeout=180)
        # The host Docker CLI's CPU is intentionally discarded.
        return result
    finally:
        cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
        if cleanup.returncode and b"No such container" not in cleanup.stderr:
            raise RuntimeError("container cleanup failed: " + cleanup.stderr.decode(errors="replace"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sample")
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--image", default="blast-radius-entry")
    parser.add_argument("--strategy", choices=("all", "file", "function", "hybrid"), default="hybrid")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("container-report.json"))
    options = parser.parse_args()
    report = {"runs": [], "success": False, "deterministic": None, "strategy": options.strategy, "probe": options.probe,
              "limits": {"seconds": 180, "stdout_bytes": 2_000_000, "cpus": 2,
                         "memory_bytes": 4 * 1024**3, "tmp_bytes": 64 * 1024**2, "pids": 256},
              "cpu_scope": "selector child user+system; excludes wrapper/startup; not official container CPU"}
    if options.probe:
        report["cpu_scope"] += "; killed grandchildren may not be included"
    try:
        tests, answers, _ = load_inputs(options.data_dir, options.answers or options.data_dir / "answers.json")
        inspected = subprocess.run(["docker", "image", "inspect", options.image], capture_output=True,
                                   check=True, timeout=30)
        image = json.loads(inspected.stdout)[0]
        if image["Architecture"] != "amd64" or image["Os"] != "linux":
            raise ValueError("image must be linux/amd64")
        report["image_id"] = image["Id"]
        audit = container_run(image["Id"], options.data_dir,
                              ["python", "-B", "-c", AUDIT])
        if audit.returncode or audit.failure:
            raise ValueError("runtime audit failed: " + audit.stderr.decode(errors="replace"))
        report["artifact_audit"] = json.loads(audit.stdout)
        for _ in range(3):
            result = container_run(image["Id"], options.data_dir,
                                   ["python", "-B", "-c", MEASURE, options.strategy] + (["--probe"] if options.probe else []))
            row = summarize(result.stdout, result.stderr, tests, answers)
            row.update(wall_seconds=result.wall_seconds, selector_exit_code=result.returncode,
                       execution_failure=result.failure, selector_cpu_seconds=None,
                       stdout_sha256=hashlib.sha256(result.stdout).hexdigest())
            for line in result.stderr.decode(errors="replace").splitlines():
                try:
                    measurement = json.loads(line)
                except ValueError:
                    continue
                if isinstance(measurement, dict) and measurement.get("measurement") == "selector_child_cpu":
                    row["selector_cpu_seconds"] = measurement["seconds"]
            report["runs"].append(row)
        report["deterministic"] = len({r["stdout_sha256"] for r in report["runs"]}) == 1
        report["success"] = all(r["evaluation"]["ok"] and r["selector_exit_code"] == 0
                                and not r["execution_failure"] for r in report["runs"])
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            report["error_detail"] = error.stderr.decode(errors="replace")
    options.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["success"] and report["deterministic"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
