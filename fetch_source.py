"""Fetch only the competition's pinned SQLGlot repository archive."""

import hashlib
import json
import tarfile
import tempfile
import urllib.request
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"


def fetch(destination: Path) -> None:
    if destination.exists():
        raise ValueError(f"destination already exists: {destination}")
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "source.tar.gz"
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive) as source:
            prefix = f"sqlglot-{COMMIT}"
            if any(member.name.split("/")[0] != prefix for member in source.getmembers()):
                raise ValueError("unexpected archive root")
            source.extractall(temporary, filter="data")
        (Path(temporary) / prefix).rename(destination)
    # Provenance sits outside the source tree so instrumentation can check it
    # remained unchanged. This identifies the fetched archive, not a signature.
    destination.with_suffix(".source.json").write_text(json.dumps({
        "commit": COMMIT, "url": URL, "archive_sha256": digest,
    }, sort_keys=True) + "\n")


if __name__ == "__main__":
    import sys
    fetch(Path(sys.argv[1]))
