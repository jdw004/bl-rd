IMAGE ?= blast-radius-entry
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

test:
	python3 -m unittest discover -v

compare:
	python3 compare_strategies.py --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

container-evaluate: build
	python3 evaluate_container.py --image "$(IMAGE)" --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

container-compare: build
	@for strategy in all file function hybrid; do \
		python3 evaluate_container.py --image "$(IMAGE)" --data-dir "$(DATA_DIR)" \
			--answers "$(ANSWERS)" --strategy "$$strategy" \
			--output "container-$$strategy-report.json" || exit $$?; \
	done

patches-check:
	python3 analyze_patches.py --patches "$(DATA_DIR)/commits"

# Dedicated builder with a four-CPU, 8-GiB cap and ten-minute wall deadline.
maps-build: maps-regenerate

maps-regenerate:
	python3 build_maps.py

maps-reproduce:
	python3 build_maps.py --verify-reproducible

maps-check:
	python3 validate_maps.py --repository . --catalog "$(DATA_DIR)/tests.json"

# Fast local evaluation, without Docker; official targets below are unchanged.
evaluate:
	python3 evaluate.py --data-dir "$(DATA_DIR)" --answers "$(ANSWERS)"

build:
	python3 validate_maps.py --repository .
	docker build --network none --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"
