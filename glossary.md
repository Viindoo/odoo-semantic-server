---
status: draft
scope: project
reads-with:
  - docs/architecture/overview.md
---

# Glossary

Canonical terms used across this project. Link here instead of redefining.

## Domain (Odoo)

| Term | Meaning |
| ---- | ------- |
| **Model** | Odoo ORM class identified by `_name` (e.g. `sale.order`) |
| **Inheritance** | Extending a model. Three flavours: classical Python, `_inherit` (same model), `_inherits` (delegation) |
| **Override chain** | Ordered list of modules that modified a field/method, ending at the final definition. Order derives from manifest `depends` + load order |
| **View** | XML UI declaration (form, tree, kanban, search, ...) identified by `id` / `view_id` |
| **View inheritance** | A view declares `inherit_id` and patches its parent with XPath expressions |
| **XPath patch** | One patch spec element (`<xpath>`, `<field>`, `<button>`, ...) inside an extension view's `<arch>` that targets a parent-view node and modifies it |
| **`position`** | Attribute on a patch spec: `after`, `before`, `inside`, `replace`, `attributes`. Controls where patch content is inserted relative to the matched node |
| **`arch`** | The XML body of a view, held inside `<field name="arch" type="xml">`. Primary views carry a full view tree here; extension views carry a list of XPath patch specs |
| **`locate_node`** | Odoo's implicit XPath synthesis (`odoo/tools/template_inheritance.py`). `<field name="X">` → `//field[@name='X']`; other tag → `//<tag>[@a='v']...` |
| **Manifest** | `__manifest__.py` declaring module metadata, dependencies, data files |
| **Addon / module** | Folder containing `__manifest__.py`, Python models, XML data |
| **QWeb** | Odoo's XML templating engine for reports + some frontend widgets |
| **OWL** | Odoo Web Library — JS framework powering Odoo's 2020+ frontend |
| **Studio** | Odoo runtime customizer storing customizations in the DB. Out of scope |

## Technical (this project)

| Term | Meaning |
| ---- | ------- |
| **MCP** | Model Context Protocol — Anthropic spec for exposing tools/data to AI clients |
| **Tool (MCP)** | Callable function exposed by the MCP server, with input schema + structured return |
| **Graph** | Relational representation of models/fields/methods/views + inheritance edges, stored in PostgreSQL |
| **Vector** | Fixed-length embedding of a code chunk for semantic search |
| **Chunk** | Unit of code that gets embedded — typically one method body or one view definition |
| **Re-index** | Re-parse + re-embed after code changes; scoped to files that actually changed |
| **Index SHA** | Git commit the index was built against. Returned in every MCP response |
| **BYOC** | Bring Your Own Code — customer's private module repo indexed alongside CE |
| **Hosted tier** | Paid offering where Viindoo runs the indexer + MCP for the customer |
| **Self-hosted** | OSS distribution the customer runs via Docker Compose |

## Framework-specific

| Term | Meaning |
| ---- | ------- |
| **`libcst`** | Concrete Syntax Tree library; preserves whitespace/comments for byte-accurate snippets |
| **`lxml`** | C-backed XML library with native XPath — used for view resolution |
| **`pgvector`** | PostgreSQL extension for vector similarity search (HNSW, IVFFlat) |
| **FastMCP** | Python framework for building MCP servers |
| **Tailscale** | WireGuard mesh VPN — used for dev topology |

## Status tags (used in frontmatter)

| Tag | Meaning |
| --- | ------- |
| `draft` | In writing; not yet reviewed |
| `review` | Circulated for review |
| `confirmed` | Approved; safe to implement against |
| `implemented` | Code exists and matches this doc |
| `deprecated` | Superseded; see linked replacement |
