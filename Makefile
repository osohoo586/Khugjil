.PHONY: services services-down lint typecheck security test coverage check cashier-check e2e

COMPOSE_TEST = docker compose -f deploy/docker-compose.test.yml

services:            ## start throwaway Postgres + Redis for tests
	$(COMPOSE_TEST) up -d --wait

services-down:
	$(COMPOSE_TEST) down -v

lint:
	cd server && uv run ruff check . && uv run ruff format --check .

typecheck:
	cd server && uv run mypy

security:
	cd server && uv run bandit -q -c pyproject.toml -r src && uv run pip-audit --progress-spinner off --skip-editable

test:
	cd server && uv run pytest

coverage:
	cd server && uv run coverage run -m pytest -q && uv run coverage json -q -o coverage.json && uv run python ../scripts/coverage_gate.py coverage.json

cashier-check:
	cd cashier && npm run lint && npm run typecheck && npm test && npm audit --audit-level=moderate

e2e:
	cd e2e && npx playwright test

check: lint typecheck security coverage
