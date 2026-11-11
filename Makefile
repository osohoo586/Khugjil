.PHONY: services services-down lint typecheck security test coverage check cashier-check e2e sync-assets agent-check cf-worker-test

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
	cd e2e && npm test

agent-check:          ## desktop agent: capability guard, rustfmt, clippy, Rust tests (Phase 2)
	node agent/scripts/check-capabilities.mjs
	cd cashier && npm run build:agent
	cd agent/src-tauri && cargo fmt --check && cargo clippy --locked --all-targets -- -D warnings && cargo test --locked

sync-assets:          ## copy shared design tokens + fonts into the admin static folder
	mkdir -p server/src/khugjil/admin/static/shared/fonts
	cp shared/tokens.css server/src/khugjil/admin/static/shared/
	cp shared/fonts/* server/src/khugjil/admin/static/shared/fonts/

cf-worker-test:       ## Cloudflare Email Worker (port-25 fallback) tests, Node only
	cd cf-worker && npm test

check: lint typecheck security coverage
