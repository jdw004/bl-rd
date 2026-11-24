# Development and verification

Run commands from the repository root. Python 3.11+ and pytest 9.1+ are needed
for the local regression checks, including built-in subtest support. A virtual
environment keeps these dependencies separate from the system interpreter.
Container builds use the exact packages in `requirements.lock`.

## Local checks

```sh
make test
python3 -m unittest checks.test_selector checks.test_patch_analysis -v
make maps-check
make patches-check
make evaluate
make compare
```

`make test` runs synthetic checks and evaluation-harness checks. It does not run
the complete SQLGlot baseline or replace restricted-container verification.
Generated reports and scratch files are ignored by Git. `.local/` can hold
older local reports without including them in the archived source.

The selector uses `/data` unless `--data-dir` is supplied. Evaluation stages
only the test catalog and patches; sample answers remain in the evaluator.
The scoring API is unchanged. Simulated test cost and measured selector CPU
are reported separately.

## Pinned environment and artifacts

The baseline revision is `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.
The Dockerfiles retain the pinned `linux/amd64` Python base image. The manifest
records source hashes, environment information, instrumentation hashes, and
mapping gaps. Validation checks those hashes without rerunning coverage.

```sh
make maps-regenerate
make maps-reproduce
make maps-check
```

Regeneration requires Docker and downloads only the pinned source and package
dependencies. It runs baseline tests with `SKIP_INTEGRATION=0`, without access
to sample patches or answers, under the configured build resource budget.
`maps-reproduce` regenerates without cache and compares artifact bytes.
Changing an instrumentation module requires regenerating its maps; moving or
editing unrelated checks does not.

Normal builds copy and validate the committed maps. They do not regenerate
them. Unmapped tests, baseline failures, process-tracing gaps, unsupported
patches, and parser-version mismatches remain visible to fallback logic.
Unresolved dynamic relationships and native I/O limit dependency completeness;
passing samples does not establish correctness for unseen changes.

## Container evaluation

With Docker running:

```sh
make build
make container-evaluate
make container-compare
```

The harness performs three runs with no network, two CPUs, 4 GB memory, a
read-only root, a 64-MB `/tmp`, user `nobody`, `HOME=/tmp`, and a 256-process
limit. It checks JSONL validity, output size, determinism, misses, and cost.
Host measurements are not official whole-container CPU accounting. If Docker
is unavailable, these checks remain unverified; local unit tests are not a
substitute.

## Probing experiment

```sh
make probe-compare
```

This builds `Dockerfile.probe` and compares the same image with probing off and
on. Complete candidate tests run against independent temporary patched trees.
Only passing verdicts from completed pytest sessions can remove candidates.
Errors, incomplete sessions, skips, and fixture-cleanup failures retain tests.
Time budgets can cause selections to vary across machines or runs.

Probing is disabled in the normal image. Its performance benefit, native-library
behavior, temporary-storage use under full workloads, and complete container
verification remain experimental. CPU accounting can omit killed descendants.
