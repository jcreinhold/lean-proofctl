# Makefile — kan-prover commands
#
# Run from this directory. The root Kan Makefile no longer hosts these
# targets; downstream acceptance and benchmarking live here.

SHELL := /bin/bash

.PHONY: accept bench paired-baseline bench-standard downstream help

# ─── Acceptance and benchmarks ───────────────────────────────────────

accept: ## Run the explicit downstream acceptance suite
	uv run pytest -o addopts='' tests/test_downstream_acceptance.py

bench: ## Run the downstream proof-workflow bench
	uv run kan-prover bench tests/acceptance/downstream_workflow_bench.yaml

paired-baseline: ## Run the paired plain-vs-tool benchmark harness
	uv run kan-prover bench tests/acceptance/paired_baseline_bench.yaml

bench-standard: ## Run the checked-in miniF2F and PutnamBench subset manifests
	uv run kan-prover bench benchmarks/manifests/minif2f-test-subset.yaml
	uv run kan-prover bench benchmarks/manifests/putnambench-subset.yaml

downstream: ## Run accept, bench, and paired-baseline in sequence
	$(MAKE) accept
	$(MAKE) bench
	$(MAKE) paired-baseline

# ─── Help ─────────────────────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-z][a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*## "} \
			{ names[NR] = $$1; descs[NR] = $$2; if (length($$1) > w) w = length($$1) } \
			END { for (i = 1; i <= NR; i++) printf "  \033[36m%-*s\033[0m  %s\n", w, names[i], descs[i] }'

.DEFAULT_GOAL := help
