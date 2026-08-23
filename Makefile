# Repo-level orchestration. `make demo` is the one-command path from a clean
# checkout to a running CronJob.

CLUSTER   ?= dawatch
IMAGE     ?= dawatch
TAG       ?= dev
NAMESPACE ?= dawatch

.PHONY: help test lint image kind-up kind-down load deploy secret trigger logs \
        grafana prometheus demo clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test: ## Run the Python test suite
	cd script && uv run pytest

lint: ## Lint and type-check
	cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy

image: ## Build the container image
	docker build -t $(IMAGE):$(TAG) script/

kind-up: ## Create the local cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER)

kind-down: ## Delete the local cluster
	kind delete cluster --name $(CLUSTER)

load: image ## Side-load the image into the cluster
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

secret: ## Apply k8s/secret.yaml (create it from secret.example.yaml first)
	@test -f k8s/secret.yaml || { \
		echo "k8s/secret.yaml is missing."; \
		echo "  cp k8s/secret.example.yaml k8s/secret.yaml && \$$EDITOR k8s/secret.yaml"; \
		exit 1; }
	kubectl apply -f k8s/secret.yaml

deploy: ## Apply the local overlay
	kubectl apply -k k8s/overlays/local

trigger: ## Run the CronJob immediately instead of waiting for the schedule
	kubectl -n $(NAMESPACE) delete job dawatch-manual --ignore-not-found
	kubectl -n $(NAMESPACE) create job dawatch-manual --from=cronjob/dawatch
	kubectl -n $(NAMESPACE) wait --for=condition=complete job/dawatch-manual --timeout=120s

logs: ## Show logs from the most recent run
	kubectl -n $(NAMESPACE) logs -l app.kubernetes.io/name=dawatch --tail=100

demo: kind-up load deploy secret trigger logs ## Clean checkout to a completed run

grafana: ## Port-forward Grafana to http://localhost:3000
	@echo "Grafana: http://localhost:3000 (anonymous viewer access)"
	kubectl -n $(NAMESPACE) port-forward svc/grafana 3000:3000

prometheus: ## Port-forward Prometheus to http://localhost:9090
	@echo "Prometheus: http://localhost:9090"
	kubectl -n $(NAMESPACE) port-forward svc/prometheus 9090:9090

clean: ## Remove local build and test artefacts
	rm -rf script/.pytest_cache script/.mypy_cache script/.ruff_cache script/data
	find script -name __pycache__ -type d -prune -exec rm -rf {} +
