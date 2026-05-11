# odoo-semantic-mcp

A pre-computed graph + vector index of Odoo codebases, served over MCP. AI coding assistants answer Odoo questions in a single tool call — without reading source files into their context window.

## The problem we solve

Odoo resolves fields, methods, and views dynamically at load time through `_inherit`, `_inherits`, and XPath view patches. Static analysis cannot follow the chain, so AI assistants fall back to reading source. A single question — *"What fields does `sale.order` expose after `sale_margin` overrides it?"* — routinely consumes tens of thousands of tokens before the model can answer. On a large refactor, that context budget runs out.

This project moves the resolution off the AI's plate. The client calls an MCP tool; we return the answer, pre-computed from an index that already understands Odoo's dynamic inheritance the way the Odoo runtime does.

## What it does

Six MCP tools cover the questions that used to force source reads:

| Tool | Answers |
| ---- | ------- |
| `resolve_model` | Final field and method map for a model after the full override chain |
| `resolve_field` | Which module's version of a field wins, with the full extension history |
| `resolve_method` | Override chain for a method, including every `super()` link |
| `resolve_view` | Final rendered XML of a view after all inherited XPath patches |
| `find_examples` | Semantic search across indexed code by intent, not regex |
| `impact_analysis` | Every module, view, and test that depends on a given symbol |

Every response returns the git SHA the answer was computed against, so callers can detect staleness and trigger re-indexing.

## Why it is worth running

| Target | Value |
| ------ | ----- |
| Token reduction vs. raw-source baselines | ≥ 90% for model and field tools; ≥ 70% for method snippets |
| Query latency (P50) | under 50 ms on a 50k-LOC module |
| Query latency (P99) | under 500 ms |
| Override-chain correctness | ≥ 95% on the curated test set |

Correctness is the floor. Token reduction is what customers pay for.

## How it works

Two distinct lifecycles, sharing one Postgres instance.

```text
                   ┌──────────────────────────┐
   Odoo source ──► │      Indexer (offline)   │ ──► Postgres
   (git repos)     │  manifest → load order   │     (graph + cache + vectors)
                   │  → libcst (Python)       │
                   │  → lxml (XML views)      │
                   │  → resolver (override)   │
                   └──────────────────────────┘
                                                          ▲
                                                          │ recursive CTE
                                                          │ across (public + tenant)
   AI client ──────► MCP server (stdio/http) ─────────────┘
   (Claude Code,        │
    Cursor, …)          └─► returns {result, indexed_at_sha, warnings}
```

**Indexer.** Walks one or more addon paths, parses Python with libcst (preserves whitespace for byte-accurate snippets) and XML with lxml, computes content hashes, and upserts rows into `modules`, `models`, `fields`, `methods`, `views`, `view_patches`. Idempotent: re-running on an unchanged tree writes nothing outside `cache_metadata`. Run as a one-shot when commits land.

**Server.** Stateless FastMCP process. Each tool call validates input, runs a recursive CTE across `public.<table>` UNION ALL `<tenant>.<table>`, applies override semantics (last-loaded wins for fields, first-MRO for methods, XPath patches in `(priority, load_order)` order for views), and returns a structured envelope. No disk reads at query time.

**The overlay model.** Customer code never re-indexes Odoo Community Edition from scratch. CE lives in the shared `public` schema, refreshed centrally. Each customer has a private schema for their own addons. Queries union the two at runtime and resolve overrides with customer modules winning — the same ordering Odoo itself applies on boot.

## Quickstart

Six commands — prerequisites, full walkthrough, and troubleshooting in [`SETUP.md`](SETUP.md):

```bash
bash scripts/bootstrap.sh
cp .env.example .env                   # fill POSTGRES_PASSWORD
docker compose up -d db
make migrate
make test
uv run python -m osm.server            # FastMCP on stdio
```

Target production experience (self-host): `docker compose up -d`, point the indexer at your addons, connect your AI client over MCP (stdio or HTTP on `:8765`).

**Shared server, no Docker.** To run osm directly on a Linux box and let a team connect over SSH: `scripts/server-setup.sh` provisions it (Postgres + pgvector, the indexed Odoo CE databases, a restricted SSH user pinned to a forced command — no shell, no daemon), and each teammate runs `scripts/onboard.sh --host <host> --port <port> --ver 18` to add the `.mcp.json` entry. A client connection is just `ssh osm@<host> <ver>` running the stdio server for that Odoo version.

## Repository layout

```text
.
├── osm/                       # Application code
│   ├── indexer/               #   Source → Postgres pipeline
│   │   ├── manifest.py        #     read __manifest__.py
│   │   ├── load_order.py      #     simulate Odoo's module load order
│   │   ├── python_parser.py   #     libcst: models, fields, methods
│   │   ├── xml_parser.py      #     lxml: views + view_patches
│   │   ├── view_resolver.py   #     apply XPath patches → final XML
│   │   ├── resolver.py        #     compute override chains
│   │   └── driver.py          #     orchestrator (idempotent upsert)
│   └── server/                #   FastMCP server
│       ├── app.py             #     register tools, lifespan, transports
│       ├── tenancy.py         #     resolve tenant from env
│       ├── db.py              #     UNION ALL tenant + public, stale-SHA check
│       ├── errors.py          #     400 / 404 / 409 mapping
│       └── handlers/          #     one file per tool
├── migrations/                # SQL migrations (idempotent, schema-neutral)
├── scripts/                   # Operator/deploy CLIs: bootstrap, migrate, create_tenant, index,
│                              #   server-setup, osm-stdio, osm-authorize, onboard
├── tests/
│   ├── indexer/               #   Unit + integration for the indexer pipeline
│   ├── server/                #   Handler unit + golden tests
│   ├── migrations/            #   Schema-diff test (public vs tenant)
│   ├── accept/                #   End-to-end benchmark suite + live-Odoo dump
│   └── fixtures/              #   Frozen Odoo CE subset + hand-crafted edge cases
├── docker/                    # Dockerfile.server + Dockerfile.indexer
├── docker-compose.yml         # Dev topology: Postgres + MCP (the Docker path)
└── .github/workflows/         # CI
```

Single Python package (`osm/`), two subpackages (`indexer/`, `server/`). Schema lives in `migrations/`, not in code. Operator commands live in `scripts/`. Test maintenance scripts live alongside their tests in `tests/accept/`.

## Daily commands

All routine operations go through `make`. The Makefile is the source of truth — read it for the full set.

| Command | Does |
| --- | --- |
| `make dev` | `uv sync --extra dev` — install runtime + dev deps |
| `make lint` | `ruff check .` |
| `make typecheck` | `mypy osm` |
| `make test` | `pytest -q` — unit + integration (DB-gated tests skip without `DATABASE_URL`) |
| `make migrate [SCHEMA=<name>]` | Apply SQL migrations to a schema (default `public`) |
| `make index ADDONS="<paths>" TENANT=<name> GIT_SHA=<sha>` | Run the indexer pipeline |
| `make up` / `make down` | Start / stop the full Docker Compose stack |

Common workflows:

```bash
# First time setup
make dev && cp .env.example .env && docker compose up -d db && make migrate

# Index a fixture corpus to play with
make index ADDONS=./tests/fixtures/odoo_ce_subset TENANT=public GIT_SHA=fixture0

# Provision a customer tenant
uv run python scripts/create_tenant.py acme
make migrate SCHEMA=acme
make index ADDONS=/path/to/acme-addons TENANT=acme GIT_SHA=$(cd /path/to/acme-addons && git rev-parse HEAD)

# Run the server
uv run python -m osm.server                            # stdio (default for AI clients)
uv run python -m osm.server --http --port 8765        # HTTP (debugging)

# Run the benchmark suite (requires a live index + DATABASE_URL)
uv run python -m tests.accept.runner --tenant public --iterations 10
```

Re-labelling golden test fixtures (when handler output shape changes):

```bash
DATABASE_URL=postgresql:///osm_dev?user=osm \
    uv run python tests/accept/regenerate_golden.py
```

## Tiers

- **Viindoo internal** — free, indefinitely. First users, workflow validation, bug feedback.
- **Hosted BYOC** — **$10 per project per month**. Point the service at a private addon repository; we index it alongside the shared Odoo CE index. No code leaves your Git origin.
- **Self-host** — OSS core, Docker Compose, one command. For on-prem or compliance-bound deployments.
