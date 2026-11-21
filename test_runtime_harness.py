"""Synthetic limit/cleanup checks; these are not restricted Docker runs."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from evaluate_container import container_run, docker_command
from process_runner import run_bounded


class BoundedProcessTests(unittest.TestCase):
    def test_output_and_error_limits(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                result = run_bounded([sys.executable, "-c",
                    f"import sys; sys.{stream}.write('x' * 10000)"], output_limit=100)
                self.assertEqual(result.failure, stream + "_limit_exceeded")
                self.assertEqual(len(getattr(result, stream)), 101)

    def test_timeout_including_child_retaining_pipes(self):
        for code in ("import time; time.sleep(60)",
                     "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"):
            with self.subTest(code=code):
                result = run_bounded([sys.executable, "-c", code], timeout=0.2)
                self.assertEqual(result.failure, "deadline_exceeded")
                self.assertLess(result.wall_seconds, 5)

    def test_exit_code_and_cpu_are_reported_separately(self):
        result = run_bounded([sys.executable, "-c", "print('ok'); raise SystemExit(7)"])
        self.assertEqual(result.stdout, b"ok\n")
        self.assertEqual(result.returncode, 7)
        self.assertIsNone(result.failure)
        self.assertGreater(result.cpu_seconds, 0)


class ContainerCommandTests(unittest.TestCase):
    def test_restrictions_and_no_answer_mount(self):
        command = docker_command("entry", Path("/inputs"), "test-run")
        for flag, value in {"--platform": "linux/amd64", "--network": "none", "--cpus": "2",
                            "--memory": "4096m", "--memory-swap": "4096m", "--pids-limit": "256",
                            "--tmpfs": "/tmp:rw,size=64m,mode=1777", "--user": "65534:65534"}.items():
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--read-only", command)
        self.assertIn("HOME=/tmp", command)
        self.assertIn("SKIP_INTEGRATION=0", command)
        mounts = [command[i+1] for i, value in enumerate(command) if value == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all("readonly" in mount and "answers" not in mount for mount in mounts))

    def test_force_cleanup_on_timeout_or_capture_error(self):
        for error in (TimeoutError("timeout"), RuntimeError("capture failed")):
            with patch("evaluate_container.run_bounded", side_effect=error), \
                    patch("evaluate_container.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as cleanup:
                with self.assertRaises(type(error)):
                    container_run("entry", Path("/inputs"), [])
                self.assertEqual(cleanup.call_args.args[0][:3], ["docker", "rm", "--force"])


if __name__ == "__main__":
    unittest.main()
