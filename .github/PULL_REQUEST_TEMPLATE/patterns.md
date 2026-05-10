## Pattern Catalogue Contribution

### Description

Brief summary of the pattern entry you're adding (1–2 sentences). What does this pattern teach?

### Pattern Entry Checklist

- [ ] **Unique `pattern_id`** — no duplicate ID in `src/data/patterns.json`
- [ ] **Format `pattern_id`** — kebab-case, matches regex `^[a-z][a-z0-9-]*$` (e.g., `computed-field-cross-model`, `xpath-avoid-replace`)
- [ ] **`language` enum** — exactly one of `python`, `xml`, `js`
- [ ] **≥3 specific gotchas** — each gotcha mentions concrete API (e.g., `@api.depends()`) or concrete edge case (e.g., "trailing slash in XPath expression")
- [ ] **NO Odoo Enterprise references**:
  - [ ] No enterprise-only module paths (`enterprise/`, `account_accountant`, `web_studio`, `knowledge`, `pos_restaurant`, etc.)
  - [ ] No EE license markers (`OEEL-1`, `LGPL-3-OCA`, etc.)
  - [ ] No proprietary Viindoo addons (viin_* not in public CE)
  - [ ] No EE-specific features (Studio, Database Cleaning, Valuation Methods, etc.)
- [ ] **`core_symbol_names` (if used)** — qualified names like `odoo.api.depends`, `odoo.fields.Char` (CI will warn if not found in CE index)
- [ ] **JSON schema valid** — entry passes `src/data/patterns.schema.json` (CI check)

### Reviewer Notes

Any edge cases or exceptions? If a rule above cannot be satisfied, add note here:

```
(reviewed: maintainer-override) — reason for exception
```

### Examples

Not sure how to write a pattern? See the existing catalogue in `src/data/patterns.json`:
- `computed-field-cross-model` (Python) — @api.depends across related model
- `xpath-avoid-replace` (XML) — XPath override pitfall with position
- `owl-patch-v17` (JavaScript) — OWL component patching convention

Each has ≥3 specific gotchas, a concrete `snippet_text`, and `intent_keywords`.

---

**Questions?** See [ADR-0009](docs/adr/0009-pattern-catalogue-community-contribution.md) for full policy, or [CONTRIBUTING.md](CONTRIBUTING.md#contributing-patterns) for quick reference.
