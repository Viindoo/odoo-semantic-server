# Contributing

This repository is both an engineering workspace and a source of truth for design decisions. Humans and AI sessions share the same documents, so a handful of conventions keep the repo navigable for both.

## Repository layout

Every folder has a `README.md` that is an **index**, not content. Individual files stay small so an AI session can load only what a task needs.

| Folder | Purpose | Read when |
| ------ | ------- | --------- |
| [`product_brief.md`](product_brief.md) | Product vision, tool list, phase roadmap — canonical | Always first |
| [`roadmap.md`](roadmap.md) | Timeline across five phases with exit criteria | Planning, sequencing, release framing |
| [`glossary.md`](glossary.md) | Term definitions | When language needs disambiguating |
| [`contexts/`](contexts/) | Role-based mini-prompts (`dev`, `review`, `research`) | Start of every working session |
| [`architecture/`](architecture/) | Components (indexer, graph store, vector store, MCP server, tenancy, deployment) | Before editing any component |
| [`data-model/`](data-model/) | One file per table | Writing a migration or changing schema |
| [`specs/`](specs/) | One file per MCP tool | Implementing or modifying a tool |
| [`decisions/`](decisions/) | ADRs — one decision per file | Understanding why something is the way it is |
| [`research/`](research/) | Evidence notes backing decisions | Before raising a new ADR |
| [`security/`](security/) | Threat model, access control, encryption, DPA template | Reviewing data flow, onboarding a BYOC tenant |
| [`tasks/`](tasks/) | `todo.md`, `lessons.md`, phase plans | Daily working state |

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

Open an ADR in [`decisions/`](decisions/) whenever a choice binds future work: technology selection, architectural boundary, commercial policy, naming. Draft the ADR before implementing the change; link from the affected `specs/` and `architecture/` files once it is accepted.

## Working-in-repo rules for AI sessions

AI assistants should follow these rules to keep sessions focused and cheap.

1. **Never load the whole repo.** Start with `product_brief.md` plus the single folder relevant to the current task.
2. **Use `contexts/<mode>.md` as a lens.** The mode file tells the session what to read and what to skip for that role (`dev`, `review`, `research`).
3. **Respect `reads-with`.** If a file declares siblings, load those too before editing.
4. **Do not infer folder structure from directory listing.** The layout table above is authoritative — even if an empty folder exists, its purpose comes from here.
5. **Flag `status: draft` before relying on a file.** Draft content may change without notice; `confirmed` content is safe to cite.

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
2. Open the `contexts/` file that matches your role.
3. Check `tasks/todo.md` for current blockers and in-flight decisions.
4. If the task is non-trivial (touches two or more components, introduces a new decision, or spans more than a day), draft a plan in `tasks/` before coding.
