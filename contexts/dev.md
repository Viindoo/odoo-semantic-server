---
status: confirmed
scope: mode/dev
reads-with:
  - product_brief.md
---

# Dev mode context

Load this when you (AI or human) are writing or modifying code.

## What to read first

1. `product_brief.md` — always
2. The spec for the tool or component you are changing (`specs/<tool>.md` or `architecture/<component>.md`)
3. The relevant data-model file (`data-model/<entity>.md`) if schema is involved

## What NOT to read

- Other tools' specs unless they are in `reads-with`
- All of `research/` — only read the specific research file a decision points to
- `security/` — only if the change touches data flow, auth, or external boundaries

## Writing rules

- Match the style in the relevant architecture file
- Every DB-touching change must update the `data-model/` file in the same commit
- Every user-visible API change must update the corresponding `specs/` file in the same commit
- Do not invent new MCP tools — they must first be speced in `specs/`
- Do not edit `product_brief.md` — propose via ADR

## When blocked

If the spec conflicts with the brief → stop, flag it in `tasks/todo.md`, do not guess.
If two specs disagree → stop, open an ADR to reconcile.
