# Contributing

This repository is both an engineering workspace and a source of truth for design decisions. Humans and AI sessions share the same documents, so a handful of conventions keep the repo navigable for both.

## Repository layout

The repo is split into three layers: OSS landing files at the root, technical design under `docs/`, and code. Project tracking (todo, lessons, phase plans, roadmap, audits) lives **outside this repo** in the Viindoo internal workspace.

| Location | Purpose | Read when |
| -------- | ------- | --------- |
| [`product_brief.md`](product_brief.md) | Product vision, tool list, phase roadmap — canonical | Always first |
| [`glossary.md`](glossary.md) | Term definitions | When language needs disambiguating |
| [`BIRDS_EYE.md`](BIRDS_EYE.md) | Non-technical overview (VN) | Stakeholder briefing |
| [`docs/architecture/`](docs/architecture/) | Components (indexer, graph store, vector store, MCP server, tenancy, deployment) | Before editing any component |
| [`docs/data-model/`](docs/data-model/) | One file per table | Writing a migration or changing schema |
| [`docs/specs/`](docs/specs/) | One file per MCP tool | Implementing or modifying a tool |
| [`docs/decisions/`](docs/decisions/) | ADRs — one decision per file | Understanding why something is the way it is |
| [`docs/research/`](docs/research/) | Evidence notes backing decisions | Before raising a new ADR |
| [`docs/security/`](docs/security/) | Threat model, access control, encryption, DPA template | Reviewing data flow, onboarding a BYOC tenant |
| [`osm/`](osm/) | Python package (indexer + MCP server) | Implementing |
| [`tests/`](tests/) | Unit, integration, acceptance | Writing or running tests |
| [`scripts/`](scripts/) | Benchmarks, bootstrap, migration CLI | One-off tooling |
| [`migrations/`](migrations/) | SQL migrations | Schema changes |
| [`reports/`](reports/) | Acceptance / benchmark output (written by `tests/accept/runner.py` and `scripts/bench_embed.py`) | Inspecting CI artifacts |

Project-tracking artifacts (roadmap with live status, phase plans, `todo.md`, `lessons.md`, `contexts/` for AI sessions, manual audit reports) are kept in the Viindoo internal workspace at `project-docs/odoo-semantic-mcp/`. They are **not** shipped with the OSS repo by design.

## Document conventions

### Frontmatter

Every non-index Markdown file declares:

```yaml
---
status: draft | confirmed | superseded
scope: <folder>/<file-shortname>
reads-with:
  - path/to/sibling-1.md
  - path/to/sibling-2.md
---
```

- `status` — `draft` until reviewed; `confirmed` once it is authoritative for implementation; `superseded` when a newer file replaces it.
- `scope` — the document's identity, used to cross-reference without relying on folder paths.
- `reads-with` — sibling documents that must stay consistent. Update all of them together or not at all.

### When to open an ADR

Open an ADR in [`docs/decisions/`](docs/decisions/) whenever a choice binds future work: technology selection, architectural boundary, commercial policy, naming. Draft the ADR before implementing the change; link from the affected `docs/specs/` and `docs/architecture/` files once it is accepted.

## Working-in-repo rules for AI sessions

AI assistants should follow these rules to keep sessions focused and cheap.

1. **Never load the whole repo.** Start with `product_brief.md` plus the single folder relevant to the current task.
2. **Use the role context from `project-docs/odoo-semantic-mcp/contexts/<mode>.md` as a lens.** The mode file tells the session what to read and what to skip for that role (`dev`, `review`, `research`).
3. **Respect `reads-with`.** If a file declares siblings, load those too before editing.
4. **Flag `status: draft` before relying on a file.** Draft content may change without notice; `confirmed` content is safe to cite.

## Commit conventions

Commit messages follow Viindoo prefixes:

- `[ADD]` new feature, module, or file that did not exist before
- `[IMP]` improvement to existing behavior
- `[FIX]` bug fix
- `[REM]` remove
- `[REN]` rename
- `[MIG]` Odoo-version migration (not applicable here yet)
- `[UPG]` module-version upgrade
- `[I18N]` translation changes
- `[MISC]` anything that fits none of the above

Commit titles include the area (component or folder). Commit bodies are in English. Group related changes into a single commit per topic; keep unrelated changes in separate commits.

## Before starting work

1. Read `product_brief.md`.
2. Open the role context from `project-docs/odoo-semantic-mcp/contexts/` that matches your role.
3. Check `project-docs/odoo-semantic-mcp/tasks/todo.md` for current blockers and in-flight decisions.
4. If the task is non-trivial (touches two or more components, introduces a new decision, or spans more than a day), draft a plan in `project-docs/odoo-semantic-mcp/tasks/` before coding.
