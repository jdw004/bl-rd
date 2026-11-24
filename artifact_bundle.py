"""Deterministic, ordinary-file dependency bundles; no pytest dependency."""

import gzip
import base64
import hashlib
import io
import json
from pathlib import Path

SCHEMA = 3
INSTRUMENTATION_VERSION = "3.1"
PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
BASE_IMAGE = "python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
REPOSITORY_LIMIT = 20_000_000
MAX_UNCOMPRESSED = 256 * 1024 * 1024


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_bundle(directory: Path, data: dict) -> dict:
    """Timings belong in a separate report, never in reproducible artifacts."""
    data = json.loads(json.dumps(data))
    data["metadata"].pop("elapsed_seconds", None)
    data["metadata"].pop("artifact_bytes", None)
    data["metadata"].pop("platform", None)  # kernel/build host is not the image environment
    raw = canonical(data)
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0, compresslevel=9) as output:
        output.write(raw)
    compressed = stream.getvalue()
    manifest = {
        "schema": SCHEMA,
        "instrumentation_version": INSTRUMENTATION_VERSION,
        "generation_command": data["metadata"]["generation_command"],
        "regeneration_command": "make maps-regenerate",
        "metadata": data["metadata"],
        "collected_tests": data["tests"],
        "source_sha256": data["source_sha256"],
        "artifact": {"name": "dependencies.json.gz", "sha256": digest(compressed),
                     "bytes": len(compressed), "uncompressed_sha256": digest(raw),
                     "uncompressed_bytes": len(raw)},
    }
    encoded_manifest = canonical(manifest)
    if len(compressed) + len(encoded_manifest) > REPOSITORY_LIMIT:
        raise ValueError("map bundle alone exceeds the total 20-MB repository budget")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o755)
    for name, content in (("dependencies.json.gz", compressed), ("manifest.json", encoded_manifest)):
        target = directory / name
        target.write_bytes(content)
        target.chmod(0o644)
    return manifest


def read_bundle(directory: Path, require_pinned=True):
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if (manifest["schema"] != SCHEMA or manifest["instrumentation_version"] != INSTRUMENTATION_VERSION
            or manifest["artifact"]["name"] != "dependencies.json.gz"):
        raise ValueError("unsupported dependency bundle")
    compressed = (directory / "dependencies.json.gz").read_bytes()
    record = manifest["artifact"]
    if len(compressed) != record["bytes"] or digest(compressed) != record["sha256"]:
        raise ValueError("compressed dependency artifact hash/size mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(MAX_UNCOMPRESSED + 1)
    if len(raw) > MAX_UNCOMPRESSED or len(raw) != record["uncompressed_bytes"] or digest(raw) != record["uncompressed_sha256"]:
        raise ValueError("uncompressed dependency artifact hash/size mismatch")
    data = json.loads(raw)
    if (data["schema"] != SCHEMA or data["metadata"] != manifest["metadata"]
            or data["tests"] != manifest["collected_tests"]
            or data["source_sha256"] != manifest["source_sha256"]):
        raise ValueError("manifest does not match dependency artifact")
    if len(set(data["tests"])) != len(data["tests"]):
        raise ValueError("duplicate collected node IDs")
    if set(data["baseline_files"]) != set(data["source_sha256"]):
        raise ValueError("baseline snapshot inventory mismatch")
    for name, encoded in data["baseline_files"].items():
        if digest(base64.b64decode(encoded, validate=True)) != data["source_sha256"][name]:
            raise ValueError("baseline snapshot hash mismatch: " + name)
    if any(type(index) is not int or not 0 <= index < len(data["tests"])
           for group in data["test_sets"] for index in group):
        raise ValueError("invalid node-ID reference")
    if require_pinned:
        metadata = data["metadata"]
        if metadata["source"]["commit"] != PINNED_COMMIT:
            raise ValueError("dependency map is not from pinned SQLGlot")
        if metadata["environment_mismatch"]:
            raise ValueError("dependency map has an environment mismatch: " + "; ".join(metadata["environment_mismatch"]))
        if metadata["environment"]["base_image"] != BASE_IMAGE or not metadata["source_unchanged"]:
            raise ValueError("dependency source or pinned image does not match")
    return data, manifest
