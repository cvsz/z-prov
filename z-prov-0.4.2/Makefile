SHELL := /usr/bin/env bash
.SHELLFLAGS := -Eeuo pipefail -c
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help env-init install build-wheel install-dry-run install-systemd update-check update \
	auto-update-dry-run auto-update test lint validate validate-shell sbom run build up down logs health clean

help: ## Show commands
	@awk 'BEGIN {FS = ":.*## "; print "Z-Prov"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env-init: ## Create local config and secrets without overwriting files
	@test ! -e .env || { echo ".env already exists"; exit 2; }
	@test ! -e config/providers.yaml || { echo "config/providers.yaml already exists"; exit 2; }
	@cp .env.example .env
	@sed -i "s/replace-with-generated-client-key/$$(openssl rand -hex 32)/" .env
	@cp config/providers.example.yaml config/providers.yaml
	@chmod 600 .env

install: ## Install development environment
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(PIP) install -e '.[dev]'

build-wheel: ## Build the standalone release wheel into dist/
	@rm -rf dist
	@$(PY) -m pip wheel --no-cache-dir --no-deps --wheel-dir dist .

install-dry-run: ## Preview standalone versioned installation
	@bash scripts/install.sh --dry-run

install-systemd: build-wheel ## Build the wheel, then install and enable user service with CONFIRM_INSTALL=yes
	@test "$(CONFIRM_INSTALL)" = "yes" || { echo "Set CONFIRM_INSTALL=yes"; exit 2; }
	@bash scripts/install.sh --apply --systemd-user

update-check: ## Check the configured HTTPS release manifest
	@bash scripts/update.sh --check

update: ## Apply checksum-verified update with CONFIRM_UPDATE=yes
	@CONFIRM_UPDATE="$(CONFIRM_UPDATE)" bash scripts/update.sh --apply

auto-update-dry-run: ## Preview daily verified auto-update timer
	@bash scripts/install-auto-update.sh --dry-run

auto-update: ## Enable daily verified updates with CONFIRM_AUTO_UPDATE=yes
	@CONFIRM_AUTO_UPDATE="$(CONFIRM_AUTO_UPDATE)" bash scripts/install-auto-update.sh --apply

test: ## Run tests
	@$(PY) -m pytest -q

lint: ## Run Ruff
	@$(PY) -m ruff check .

validate-shell: ## Parse-check installer and maintenance scripts
	@find scripts -type f -name '*.sh' -exec bash -n {} +

validate: ## Compile, lint, and test
	@$(MAKE) --no-print-directory validate-shell
	@$(PY) -m compileall -q src
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory test

sbom: ## Generate CycloneDX JSON SBOM
	@$(PY) scripts/generate-sbom.py dist/sbom.cdx.json

run: ## Start local gateway
	@Z_PROV_CONFIG=config/providers.yaml $(VENV)/bin/z-prov

build: ## Build hardened container
	@docker compose build

up: ## Start standalone gateway
	@docker compose up -d

down: ## Stop gateway and retain config
	@docker compose down

logs: ## Show recent logs
	@docker compose logs --tail 200 provider

health: ## Check readiness
	@curl -fsS http://127.0.0.1:$${Z_PROV_PORT:-8080}/health/ready

clean: ## Preview generated local files only
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -print
