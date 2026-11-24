.DEFAULT_GOAL := test
PYTHON ?= python3
IMAGE ?= blast-radius-entry
PROBE_IMAGE ?= selection-probe
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl
DATA_DIR ?= $(SAMPLE)
ANSWERS ?= $(DATA_DIR)/answers.json

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check evaluate compare container-evaluate container-compare maps-build maps-regenerate maps-reproduce maps-check patches-check test
.PHONY: probe-build probe-compare

test:
	$(PYTHON) -m unittest discover -s checks -t . -v

compare:
	$(PYTHON) compare_strategies.py --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

container-evaluate: build
	$(PYTHON) evaluate_container.py --image "$(IMAGE)" --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

container-compare: build
	@for strategy in all file function hybrid; do \
		$(PYTHON) evaluate_container.py --image "$(IMAGE)" --data-dir "$(DATA_DIR)" \
			--answers "$(ANSWERS)" --strategy "$$strategy" \
			--output "container-$$strategy-report.json" || exit $$?; \
	done

patches-check:
	$(PYTHON) analyze_patches.py --patches "$(DATA_DIR)/commits"

# Dedicated builder with a four-CPU, 8-GiB cap and ten-minute wall deadline.
maps-build: maps-regenerate

maps-regenerate:
	$(PYTHON) build_maps.py

maps-reproduce:
	$(PYTHON) build_maps.py --verify-reproducible

maps-check:
	$(PYTHON) validate_maps.py --repository . --catalog "$(DATA_DIR)/tests.json"

# Fast local evaluation, without Docker; official targets below are unchanged.
evaluate:
	$(PYTHON) evaluate.py --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

build:
	$(PYTHON) validate_maps.py --repository .
	docker build --network none --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Optional runtime-probing experiment; separate from the normal image.
probe-build:
	$(PYTHON) validate_maps.py --repository .
	docker build --platform linux/amd64 --file Dockerfile.probe --tag "$(PROBE_IMAGE)" .

# Paired measurements use the same image and the same restricted runtime.
probe-compare: probe-build
	$(PYTHON) evaluate_container.py --image "$(PROBE_IMAGE)" --data-dir "$(DATA_DIR)" \
		--answers "$(ANSWERS)" --output container-probe-off-report.json
	$(PYTHON) evaluate_container.py --image "$(PROBE_IMAGE)" --data-dir "$(DATA_DIR)" \
		--answers "$(ANSWERS)" --probe --output container-probe-on-report.json
