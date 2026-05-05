# Makefile — odoo-semantic-mcp dev shortcuts
# Yêu cầu: Docker (cho integration tests), Python venv tại .venv/

PYTEST  := .venv/bin/pytest
COMPOSE := docker compose
UV      := $(shell which uv 2>/dev/null || echo "uv")

.PHONY: help install test test-unit test-integration test-all \
        neo4j-up neo4j-down neo4j-logs lint

help:
	@echo "Targets:"
	@echo "  install           Cài dependencies vào .venv"
	@echo "  test              Chạy unit tests (không cần Docker)"
	@echo "  test-integration  Chạy integration tests (cần Docker)"
	@echo "  test-all          Chạy toàn bộ tests"
	@echo "  neo4j-up          Start Neo4j container"
	@echo "  neo4j-down        Stop Neo4j container"
	@echo "  neo4j-logs        Xem log Neo4j"
	@echo "  lint              Chạy ruff"

install:
	$(UV) pip install -e ".[dev]"

# --- Tests ---

test: test-unit

test-unit:
	$(PYTEST) tests/ -v -m "not neo4j" --tb=short

# testcontainers tự spin up Neo4j nếu Docker có sẵn.
# Nếu muốn dùng Neo4j đang chạy sẵn thay vì testcontainers:
#   make neo4j-up && make _test-neo4j && make neo4j-down
test-integration:
	$(PYTEST) tests/ -v -m "neo4j" --tb=short

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

# --- Lint ---

lint:
	.venv/bin/ruff check src/ tests/
