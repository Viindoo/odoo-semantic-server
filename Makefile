# Makefile — odoo-semantic-mcp dev shortcuts
# Yêu cầu: Docker (cho integration tests), uv >= 0.4
# Venv nằm ngoài repo để tránh ô nhiễm source tree:
VENV    := $(HOME)/.venv/odoo-semantic-mcp
PYTEST  := $(VENV)/bin/pytest
COMPOSE := docker compose
UV      := $(shell which uv 2>/dev/null || echo "uv")

.PHONY: help install test test-unit test-integration test-browser test-all \
        neo4j-up neo4j-down neo4j-logs lint lint-py lint-shell validate-plugin \
        recreate-db

help:
	@echo "Targets:"
	@echo "  install           Cài dependencies vào ~/.venv/odoo-semantic-mcp"
	@echo "  test              Chạy unit tests (không cần Docker)"
	@echo "  test-integration  Chạy integration tests (cần Docker)"
	@echo "  test-browser      Chạy browser E2E tests (cần Docker + PostgreSQL)"
	@echo "  test-all          Chạy toàn bộ tests"
	@echo "  neo4j-up          Start Neo4j container"
	@echo "  neo4j-down        Stop Neo4j container"
	@echo "  neo4j-logs        Xem log Neo4j"
	@echo "  recreate-db       down → up postgres → wait-healthy (use sau khi compose đổi)"
	@echo "  lint              Chạy ruff + shell lint (strict)"

install:
	$(UV) venv $(VENV)
	$(UV) pip install --python $(VENV)/bin/python -e ".[dev]"
	@[ -f .env ] || (cp .env.example .env && \
		echo "✓ .env created")
	@[ -f odoo-semantic.conf ] || (cp odoo-semantic.conf.example odoo-semantic.conf && \
		echo "✓ odoo-semantic.conf created")
	@echo ""
	@echo "✓ Setup complete. Next steps:"
	@echo "  1. Edit .env — điền NEO4J_PASSWORD + PG_PASSWORD (replace <PASSWORD> trong PG_DSN)"
	@echo "  2. docker compose up -d        # khởi động Neo4j + PostgreSQL"
	@echo "  3. $(VENV)/bin/python -m src.db.migrate    # bootstrap schema"
	@echo "  4. Xem README §Local E2E Quickstart để index repo + start MCP server."
	@echo "  5. (optional) $(VENV)/bin/playwright install chromium    # cần cho 'make test-browser'"
	@echo ""

# --- Tests ---

test: test-unit

test-unit:
	$(PYTEST) tests/ -v -m "not neo4j and not postgres" --tb=short

# testcontainers tự spin up Neo4j nếu Docker có sẵn.
# Nếu muốn dùng Neo4j đang chạy sẵn thay vì testcontainers:
#   make neo4j-up && make _test-neo4j && make neo4j-down
test-integration:
	$(PYTEST) tests/ -v -m "(neo4j or postgres) and not browser" --tb=short -rs

test-browser:
	@$(COMPOSE) up -d postgres > /dev/null 2>&1
	@echo "Đợi PostgreSQL sẵn sàng..."
	@until $(COMPOSE) exec -T postgres pg_isready -U odoo_semantic > /dev/null 2>&1; do \
		printf "."; sleep 2; \
	done
	@echo " PostgreSQL sẵn sàng"
	$(VENV)/bin/playwright install chromium
	$(PYTEST) tests/test_web_ui_browser.py -v -m "browser and postgres" --tb=short

test-all: test-unit test-integration

# --- Neo4j thủ công (khi không muốn dùng testcontainers) ---

neo4j-up:
	$(COMPOSE) up -d neo4j
	@echo "Đợi Neo4j sẵn sàng..."
	@until $(COMPOSE) exec -T neo4j cypher-shell -u neo4j -p password 'RETURN 1' \
	       > /dev/null 2>&1; do \
		printf "."; sleep 2; \
	done
	@echo " Neo4j sẵn sàng tại bolt://localhost:7687"

neo4j-down:
	$(COMPOSE) stop neo4j

neo4j-logs:
	$(COMPOSE) logs -f neo4j

# --- DB tier lifecycle ---

# Atomically recreate the DB tier. Run this AFTER any change to docker-compose.yml
# (bind-mount paths, image versions, volume mappings) — a bare `docker compose up -d`
# is not enough because existing containers remember the OLD bind-mount metadata
# (resolved from the cwd they were first created in) and the `up` command does
# NOT re-resolve unless the container is recreated. See incident 2026-05-19
# (docs/deploy/db-tier-operations.md).
recreate-db:
	$(COMPOSE) down
	$(COMPOSE) up -d postgres
	@echo "Đợi PostgreSQL healthy..."
	@bash scripts/wait-pg-healthy.sh

# --- Lint ---

lint: lint-py lint-shell

lint-py:
	$(VENV)/bin/ruff check src/ tests/

# Strict mode — any new JSONResponse(dict) without _json_safe wrap fails the build,
# any fetch() without Content-Type fails the build. M9 legacy violations cleared
# in task #22 (2026-05-16). See CONTRIBUTING.md Common Pitfalls.
lint-shell:
	@bash scripts/lint_json_response.sh
	@bash scripts/lint_fetch_content_type.sh

validate-plugin:  ## Validate Claude Code plugin schema (requires claude CLI)
	claude plugin validate dist/odoo-semantic-plugin/
