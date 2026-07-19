# Convenience targets. On Windows, run the underlying commands directly if you
# do not have make available.

.PHONY: install dev run test up down load logs clean

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest

up:
	docker compose -f deploy/docker-compose.yml up --build -d

down:
	docker compose -f deploy/docker-compose.yml down -v

load:
	python load/generate.py --duration 120 --concurrency 20

logs:
	docker compose -f deploy/docker-compose.yml logs -f app

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
