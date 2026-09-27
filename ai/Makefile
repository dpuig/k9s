DAEMON := daemon
UV     := uv --project $(DAEMON)

default: help

install:        ## Install k9sai (Go client) and k9sai-daemon, then print plugin setup
	@go install ../cmd/k9sai
	@uv tool install --force ./$(DAEMON)
	@echo "Next: cp $(DAEMON)/config.example.yaml ~/.config/k9sai/config.yaml"
	@echo "      and merge plugins/k9sai.yaml into your k9s plugins.yaml"

test:           ## Python unit tests + Go client tests (no model, no cluster)
	@$(UV) run pytest -q $(DAEMON)/tests
	@cd .. && go test ./cmd/k9sai/...

lint:           ## ruff (lint + format check) and golangci-lint for the client
	@$(UV) run ruff check --config $(DAEMON)/pyproject.toml $(DAEMON)/src $(DAEMON)/tests eval
	@$(UV) run ruff format --config $(DAEMON)/pyproject.toml --check $(DAEMON)/src $(DAEMON)/tests eval
	@cd .. && golangci-lint run ./cmd/k9sai/...

check-config:   ## Validate ~/.config/k9sai/config.yaml and the SDK tool lockdown
	@$(UV) run k9sai-daemon check-config

eval:           ## Full eval: kind scenarios, tools on/off, real model; writes eval/reports/
	@$(UV) run python eval/run.py

eval-replay:    ## Grade committed fixtures against the real model (no cluster)
	@$(UV) run python eval/run.py --replay

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: default install test lint check-config eval eval-replay help
