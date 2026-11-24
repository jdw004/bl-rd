# Test selection utility

Archived test-selection project for independent changes against a pinned
SQLGlot source snapshot. It reconstructs patches, queries dependency maps,
and assigns selected tests to workers using simulated execution costs.

## Repository layout

| Location | Purpose |
| --- | --- |
| `selector.py`, `selection.py`, `dependency_map.py` | Selection and worker allocation |
| `unified_diff.py`, `patch_analysis.py` | Independent patch reconstruction and structural analysis |
| `build_dependencies.py`, `process_dependencies.py`, `dependency_structure.py` | Baseline dependency instrumentation |
| `fetch_source.py`, `build_maps.py`, `artifact_bundle.py`, `validate_maps.py` | Source provenance, regeneration, and artifact validation |
| `maps/` | Compressed dependency bundle and manifest |
| `evaluate.py`, `compare_strategies.py`, `evaluate_container.py` | Local evaluation tools |
| `score.py`, `SCORING.md`, `sample/` | Supplied scoring API and sample inputs |
| `checks/` | Synthetic and harness regression checks |
| `runtime_probe.py`, `probe_runner.py`, `Dockerfile.probe` | Optional probing experiment |
| `docs/development.md` | Setup, regeneration, and verification details |

## Usage

Run these commands from the repository directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'pytest>=9.1,<10'
make test
make evaluate
make compare
make maps-check
```

The local checks and evaluation tools support Python 3.11+. Structural
selection requires the same Python major/minor version as the generated maps;
another version conservatively selects all tests. Use the pinned container
for evaluation in the matching environment, with Docker running:

```sh
make build
make container-evaluate
```

`make` defaults to the local checks. Set `PYTHON=python3.13` to choose a local
interpreter. See [development notes](docs/development.md) for targeted checks
and the limits of local validation.

To use a different input directory:

```sh
make evaluate DATA_DIR=/path/to/data ANSWERS=/path/to/answers.json
```

The selector defaults to `/data`. Input consists of `tests.json` and
`commits/*.patch`. Each patch is independent. Output is one JSONL answer per
patch. Evaluation labels are used only by evaluation tools and are excluded
from the runtime image.

## Artifact maintenance

```sh
make maps-regenerate
make maps-reproduce
make maps-check
```

Normal image builds validate and copy existing artifacts. Regeneration uses
only the pinned baseline source and its tests. Missing or incomplete dependency
evidence remains visible to conservative fallback logic.

## Local reports

Evaluation and generation commands write local reports that are ignored by Git.
Keep measurements and performance results out of this document. Simulated cost
and measured CPU time are separate quantities; see `SCORING.md` for the API's
cost rules.

## Optional runtime checks

```sh
make probe-compare
```

This separate image installs `requirements.lock` and compares three restricted
runs with and without `--probe`. The experiment runs complete candidate tests
in independent patched source trees under `/tmp`. Errors, skips, unfinished
runs, and fixture cleanup failures retain candidates. A time and cost budget
limits probing; it is disabled in the normal image. The maps must first have a
complete baseline run. Probe CPU is charged in addition to simulated test cost.
Local CPU accounting may omit killed descendants. The experiment remains
opt-in; its end-to-end benefit and full restricted-runtime behavior have not
been established. The archive makes no performance-target guarantee.
