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

## The overlay model

Customer code never re-indexes Odoo Community Edition from scratch. CE lives in a shared index, refreshed centrally. Each customer has a private index for their own addons. Queries union the two at runtime and resolve overrides with customer modules winning — the same ordering Odoo itself applies on boot.

## Tiers

- **Viindoo internal** — free, indefinitely. First users, workflow validation, bug feedback.
- **Hosted BYOC** — **$10 per project per month**. Point the service at a private addon repository; we index it alongside the shared Odoo CE index. No code leaves your Git origin.
- **Self-host** — OSS core, Docker Compose, one command. For on-prem or compliance-bound deployments.

## Quickstart

Dev setup in six commands — prerequisites, full walkthrough, and troubleshooting in [`SETUP.md`](SETUP.md):

```bash
bash scripts/bootstrap.sh
cp .env.example .env                   # fill POSTGRES_PASSWORD
docker compose up -d db
make migrate
make test
uv run python -m osm.server            # FastMCP on stdio
```

Target production experience (self-host): `docker compose up -d`, point the indexer at your addons, connect your AI client over MCP (stdio or HTTP on `:8765`). See [`docs/architecture/deployment.md`](docs/architecture/deployment.md) for customer and hosted topologies.

## Roadmap

Twelve-to-sixteen-week MVP across four capability phases:

1. **Phase 1 — Model graph** (`resolve_model`, `resolve_field`, `resolve_method`) — ✅ shipped
2. **Phase 2 — View resolver** (`resolve_view` with inherited XPath patches) — in implementation
3. **Phase 3 — Hybrid retrieval** (`find_examples` semantic + keyword)
4. **Phase 4 — Full stack** (`impact_analysis`, hosted BYOC onboarding)

Detailed sequencing, exit criteria, and live work-package status are tracked internally at `project-docs/odoo-semantic-mcp/roadmap.md`.

## Project status

Design confirmed 2026-04-22 for Phase 1 and Phase 2. Phase 1 is effectively complete — three P1 resolver tools (`resolve_model`, `resolve_field`, `resolve_method`) pass every exit criterion with wide margins; only the Docker Compose topology work is outstanding, blocked on host tooling. Phase 2 (XML view resolver) is in implementation — parser and fixture corpus shipped, DOM resolver next. Brand name and hosted-tier go-live date are not yet finalized.

---

**Contributing or editing docs?** Start with [`CONTRIBUTING.md`](CONTRIBUTING.md).
