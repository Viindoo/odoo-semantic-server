---
status: draft
scope: specs/<tool>
phase: P?
reads-with:
  - ../product_brief.md
  - ../architecture/mcp-server.md
---

# Spec: `<tool_name>`

## 1. Purpose

One paragraph: who calls this and what problem it solves. Reference the phase this tool belongs to.

## 2. Input schema

```json
{
  "field": "value"
}
```

Rules: types, required vs optional, validation.

## 3. Output schema

```json
{
  "result": {},
  "indexed_at_sha": "string",
  "warnings": []
}
```

## 4. Algorithm

Bullet-point the query plan:

1. Validate input
2. Query `../data-model/<table>.md`
3. Apply resolver logic
4. Attach SHA
5. Return

## 5. Data accessed

- [`../data-model/<table>.md`](../data-model/<table>.md) — what for
- [`../data-model/<table>.md`](../data-model/<table>.md) — what for

## 6. Out of scope

- …
- …

## 7. Acceptance criteria

- [ ] …
- [ ] …

## 8. Open questions

- …

## 9. References

- ADR: …
- Research: …
