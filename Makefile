.PHONY: dev lint typecheck test up down index migrate

# Install runtime + dev dependencies via uv. Requires `uv` on PATH.
dev:
	uv sync --extra dev

lint:
	uv run ruff check .

typecheck:
	uv run mypy osm

test:
	uv run pytest -q

up:
	docker compose up -d

down:
	docker compose down

# Run the indexer against one or more addon roots.
# Usage: make index ADDONS="./tests/fixtures/odoo_ce_subset ./tests/fixtures/custom_addons" TENANT=public GIT_SHA=fixture0
ADDONS ?=
TENANT ?= public
GIT_SHA ?= unknown
index:
	@if [ -z "$(ADDONS)" ]; then echo "error: ADDONS=<paths> required"; exit 2; fi
	uv run python scripts/index.py $(foreach p,$(ADDONS),--addons $(p)) --tenant $(TENANT) --git-sha $(GIT_SHA)

# Apply SQL migrations to the public schema. Override schema via:
#   make migrate SCHEMA=<tenant>
SCHEMA ?= public
migrate:
	uv run python scripts/migrate.py --schema $(SCHEMA)
