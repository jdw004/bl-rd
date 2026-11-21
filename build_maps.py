"""Regenerate pinned-source bundles with four CPUs, 8 GiB and a 600s budget."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from artifact_bundle import read_bundle, REPOSITORY_LIMIT
from validate_maps import repository_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-reproducible", action="store_true",
                        help="regenerate without cache and require byte identity with existing maps")
    options = parser.parse_args()
    root = Path(__file__).resolve().parent
    builder = f"blast-radius-maps-{os.getpid()}"
    started = time.monotonic()
    report = {"budget_seconds": 600, "cpu_quota": 4, "memory_bytes": 8 * 1024**3,
              "platform": "linux/amd64", "success": False}
    created = False
    with tempfile.TemporaryDirectory(prefix="blast-radius-buildx-") as temporary:
        environment = dict(os.environ, BUILDX_CONFIG=str(Path(temporary) / "config"))
        exported = Path(temporary) / "export"
        try:
            original = None
            if options.verify_reproducible:
                read_bundle(root / "maps")
                original = {name: (root / "maps" / name).read_bytes()
                            for name in ("dependencies.json.gz", "manifest.json")}
            commands = [
                ["docker", "buildx", "create", "--name", builder, "--driver", "docker-container",
                 "--driver-opt", "cpu-period=100000,cpu-quota=400000,memory=8g,memory-swap=8g"],
                ["docker", "buildx", "build", "--builder", builder, "--platform", "linux/amd64",
                 "--progress", "plain", "--file", "Dockerfile.maps", "--target", "export",
                 "--output", f"type=local,dest={exported}", "."],
            ]
            if options.verify_reproducible:
                commands[1].insert(3, "--no-cache")
            for command in commands:
                report["last_command"] = command
                subprocess.run(command, check=True, env=environment, cwd=root,
                               timeout=max(0.01, 600 - (time.monotonic() - started)))
                created = True
            data, manifest = read_bundle(exported / "maps")
            generated = [exported / "maps" / name for name in ("dependencies.json.gz", "manifest.json")]
            if original is not None:
                report["reproducible"] = all(path.read_bytes() == original[path.name] for path in generated)
                if not report["reproducible"]:
                    raise ValueError("fresh artifacts differ from existing bundle; existing maps preserved")
            previous = sum((root / "maps" / path.name).stat().st_size for path in generated
                           if (root / "maps" / path.name).exists())
            projected = repository_bytes(root) - previous + sum(path.stat().st_size for path in generated)
            if projected > REPOSITORY_LIMIT:
                raise ValueError(f"projected repository is {projected} bytes; limit is {REPOSITORY_LIMIT}")
            report["generation"] = json.loads((exported / "generation-report.json").read_text())
            report["repository_bytes"] = projected
            for path in generated:
                shutil.copyfile(path, root / "maps" / path.name)
                (root / "maps" / path.name).chmod(0o644)
            (root / "maps").chmod(0o755)
            shutil.copyfile(exported / "generation-report.json", root / "generation-report.json")
            report["success"] = True
            report["mapping_complete"] = data["metadata"]["instrumentation_complete"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            report["error"] = str(error)
        finally:
            report["build_elapsed_seconds"] = round(time.monotonic() - started, 3)
            if created:
                try:
                    subprocess.run(["docker", "buildx", "rm", "--force", builder], check=True,
                                   env=environment, timeout=30)
                except (OSError, subprocess.SubprocessError) as error:
                    report["cleanup_error"] = str(error)
            (root / "build-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(json.dumps(report, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
