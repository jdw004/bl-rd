# Test selection utility

Select tests for independent changes against a pinned source snapshot.
Python 3.11+ is required for local tools; the container uses the pinned image.

## Usage

Run these commands from the repository directory:

```sh
make test
make evaluate
make compare
make maps-check
make build
make container-evaluate
```

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
