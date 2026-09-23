# Convenience targets. On Windows, run the underlying commands directly if you
# do not have make available.

.PHONY: install dev run demo test lint up down load logs clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

run:
	observability-starter serve --reload --port 8000

demo:
	observability-starter demo --requests 200 --seed 7

test:
	pytest

lint:
	ruff check app tests load

up:
	docker compose -f deploy/docker-compose.yml up --build -d

down:
	docker compose -f deploy/docker-compose.yml down -v

load:
	observability-starter load --duration 120 --concurrency 20

logs:
	docker compose -f deploy/docker-compose.yml logs -f app

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache build *.egg-info
