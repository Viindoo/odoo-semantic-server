# Setup — dev environment

One-page cheatsheet to get a working dev box. For customer / self-host topology, jump to [`docs/architecture/deployment.md`](docs/architecture/deployment.md).

## TL;DR

```bash
# Prereqs: Python 3.10+, PostgreSQL 14+, Docker, uv
bash scripts/bootstrap.sh                       # install deps into .venv via uv
cp .env.example .env                            # fill POSTGRES_PASSWORD
docker compose up -d db                         # start Postgres
make migrate                                    # create public schema
make test                                       # 237 pass / 22 skip expected
make index ADDONS=./tests/fixtures/odoo_ce_subset TENANT=public GIT_SHA=fixture0
uv run python -m osm.server                     # FastMCP on stdio
```

## Prerequisites

| Tool | Version | How |
| --- | --- | --- |
| Python | 3.10+ | system or `pyenv` |
| PostgreSQL | 14+ (16 recommended) | `apt install postgresql-16 postgresql-contrib` or use the Docker Compose `db` service |
| `pgvector` | ships with `pgvector/pgvector:pg16` image (already in compose) | — |
| Docker + Compose | any recent | [docs.docker.com/install](https://docs.docker.com/get-docker/) |
| `uv` (Python package manager) | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `lxml` system libs | — | `apt install libxml2-dev libxslt1-dev` (if wheel fails) |

## Step by step

### 1. Clone and install deps

```bash
git clone git@github.com:Viindoo/odoo-semantic-mcp.git
cd odoo-semantic-mcp
bash scripts/bootstrap.sh            # idempotent: uv sync --extra dev
```

The script creates `.venv/` and installs runtime + dev extras (pytest, mypy, ruff, lxml, psycopg, fastmcp, ...).

### 2. Configure secrets

```bash
cp .env.example .env
# edit .env → change POSTGRES_PASSWORD; leave TS_AUTHKEY empty for now
```

Keys that matter on a dev box:

- `DATABASE_URL` — the indexer + server read this
- `OSM_TENANT` — `public` for shared Odoo CE, any other schema for a customer tenant
- `TS_AUTHKEY` — Tailscale sidecar auth key (leave empty until you enable the sidecar in `docker-compose.yml`; see ADR-0005)

### 3. Start Postgres

```bash
docker compose up -d db              # only the db service; sidecar + app stay down
docker compose logs -f db            # verify ready to accept connections
```

### 4. Apply migrations

```bash
make migrate                         # schema = public by default
make migrate SCHEMA=acme             # create a tenant schema later
```

### 5. Run the test suite

```bash
make test                            # expect 237 pass / 22 skip on a fresh box
```

The 22 skipped tests need `ODOO_SOURCE_PATH` set to a local Odoo 17.0 checkout. To run them:

```bash
export ODOO_SOURCE_PATH=/path/to/odoo-17.0
make test
```

### 6. Index a fixture (smoke test)

```bash
make index \
    ADDONS=./tests/fixtures/odoo_ce_subset \
    TENANT=public \
    GIT_SHA=fixture0
```

This populates the `public` schema with the 10-module Odoo CE subset used by the accept tests.

### 7. Start the MCP server

Two transports:

```bash
# stdio (what AI clients use by default)
uv run python -m osm.server

# HTTP (dev debugging only; bind to loopback or Tailscale)
uv run python -m osm.server --http --host 127.0.0.1 --port 8765
```

From another shell, smoke-test:

```bash
curl -s http://127.0.0.1:8765/ | jq .   # should list the 3 P1 tools
```

## Common commands (`make`)

| Command | Does |
| --- | --- |
| `make dev` | `uv sync --extra dev` |
| `make lint` | `ruff check .` |
| `make typecheck` | `mypy osm` |
| `make test` | `pytest -q` |
| `make up` / `make down` | start / stop the full compose stack |
| `make migrate [SCHEMA=<name>]` | apply SQL migrations to a schema |
| `make index ADDONS=<paths> TENANT=<name> GIT_SHA=<sha>` | run the indexer |

## Acceptance test (Phase 1 benchmark)

Once indexing is done, run the P1 accept suite:

```bash
uv run python -m tests.accept.runner --tenant public --iterations 10
# writes to reports/ (output files not committed to repo)
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `FATAL: password authentication failed` | `.env` password ≠ what Postgres was initialised with. Blow away the volume: `docker compose down -v && docker compose up -d db && make migrate` |
| `lxml` wheel fails to build | `apt install libxml2-dev libxslt1-dev` then `uv sync --extra dev --reinstall` |
| 22 extra tests skipped | set `ODOO_SOURCE_PATH=<path to odoo-17.0 source>` and re-run |
| `relation "modules" does not exist` | forgot `make migrate` after fresh `docker compose up -d db` |
| MCP client gets 0 tools | indexer not yet run against the tenant — run `make index` first |
| pgvector error | using wrong Postgres image; compose pins `pgvector/pgvector:pg16` |
| Port 5432 already in use | another Postgres on host. Either stop host service or edit `docker-compose.yml` to bind `5433:5432` and update `DATABASE_URL` |
| Test `test_schema_diff` fails on OID | known local patch not yet committed — pull latest `main` |

## What next

- Live status + next WP → internal `project-docs/odoo-semantic-mcp/README.md`
- Architecture deep-dive → [`docs/architecture/overview.md`](docs/architecture/overview.md)
- Customer self-host topology → [`docs/architecture/deployment.md`](docs/architecture/deployment.md) §2
- Adding a new MCP tool → `project-docs/odoo-semantic-mcp/specs/_template.md` then [`CONTRIBUTING.md`](CONTRIBUTING.md)
