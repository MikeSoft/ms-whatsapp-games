# Atajos para lo que se hace todos los días. `make` a secas los lista.
#
# Todo pasa por .venv para no depender de qué tenga activado la terminal.

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup lint format test test-serial check run up down logs clean

help: ## Lista estos atajos
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Crea el entorno e instala las dependencias de desarrollo
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements-dev.txt
	@test -f .env || cp .env.example .env
	@echo "listo: edita .env y pon MANAGER_NUMBER"

lint: ## Pasa ruff
	$(VENV)/bin/ruff check app tests

format: ## Arregla lo que ruff sabe arreglar solo
	$(VENV)/bin/ruff check app tests --fix

test: ## Corre la suite en paralelo (unos 20 s)
	$(PY) -m pytest

test-serial: ## La suite en serie, para depurar con pdb
	$(PY) -m pytest -n0

check: lint test ## Lo que tiene que pasar antes de un pull request

run: ## Levanta el servicio en local con recarga
	$(VENV)/bin/uvicorn app.main:app --reload

up: ## Levanta el stack con docker y espera a que esté sano
	docker compose up -d --build --wait

down: ## Para el stack
	docker compose down

logs: ## Sigue el log del servicio
	docker compose logs -f api

clean: ## Borra cachés de python y de las herramientas
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
