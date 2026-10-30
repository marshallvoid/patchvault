PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_READY := $(VENV)/.ready

.DEFAULT_GOAL := help
.PHONY: help install run check test build clean

help: ## Show this help
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

$(VENV_READY): requirements-build.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --quiet --disable-pip-version-check -r requirements-build.txt
	@touch $(VENV_READY)

install: $(VENV_READY) ## Create the build environment

run: ## Open the app from source
	$(PYTHON) patchvault.py

check: ## Print available updates
	$(PYTHON) patchvault.py --check

test: ## Run the test suite
	$(PYTHON) -B -m unittest discover -s tests

build: $(VENV_READY) ## Build dist/PatchVault-<platform>-<arch>.zip
	$(VENV_PYTHON) -B scripts/build.py

clean: ## Remove build output, the build environment and caches
	rm -rf dist $(VENV)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
