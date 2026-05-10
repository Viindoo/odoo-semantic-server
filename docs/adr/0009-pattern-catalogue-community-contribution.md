# ADR-0009 — Pattern Catalogue Community Contribution

**Date:** 2026-05-10  
**Status:** Accepted

## Context

Milestone 4.6 shipped ~54 hand-curated PatternExample entries in `src/data/patterns.json`. Milestone 6 Wave 2 added auto-reseed sentinel (`_SeedMeta` node with sha256 hash) to make catalogue updates cheap (skip re-embedding when unchanged).

The catalogue must scale via community contributions while maintaining quality and preventing regressions in `suggest_pattern` tool output. Without a formal contribution contract, drive-by PRs risk adding low-quality entries (single-line gotchas, Odoo Enterprise references, duplicate `pattern_id`, missing specific API names) that erode trust in recommendations.

## Decision

Community PRs to `src/data/patterns.json` MUST pass the following checks (enforced via CI + PR template) **before maintainer review:**

1. **JSON schema validation** against `src/data/patterns.schema.json` (landed M6 Wave 3 W3-2; forward reference).
2. **Unique `pattern_id` across catalogue** — CI dedup check fails on collision.
3. **`pattern_id` format** — kebab-case, regex `^[a-z][a-z0-9-]*$`.
4. **`language` enum** — MUST be one of `python`, `xml`, `js` (no `sql`, `bash`, etc.).
5. **Gotchas specificity** — ≥3 gotchas per entry. Specific = concrete API reference (e.g. `@api.depends()`), edge case (e.g. "trailing slash in XPath"), or version skew (e.g. "v17-only"). Generic boilerplate (e.g. "always test your code") rejected.
6. **NO Odoo Enterprise references** — `snippet_text` and `gotchas` MUST NOT mention:
   - Enterprise-only module paths (`enterprise/`, `account_accountant`, `web_studio`, `knowledge`, `pos_restaurant`, etc.)
   - EE license markers (`OEEL-1`, `LGPL-3-OCA`, etc.)
   - Enterprise-specific features (Studio, Database Cleaning, Valuation Methods, etc.)
   - Proprietary Viindoo addons (viin_* modules not in public CE ecosystem)
7. **`core_symbol_names` qualified resolution** — any `core_symbol_names` (e.g. `odoo.api.depends`, `odoo.fields.Char`) SHOULD resolve when Odoo CE is indexed. CI performs soft-fail check (warns if symbol not found, does not block PR).

**Maintainer override:** In rare cases where an entry cannot satisfy a rule (e.g., pattern has only 2 deeply-articulated gotchas, or must reference deprecated EE API for historical context), contributor MAY add explicit note `(reviewed: maintainer-override)` in PR description. Maintainer reviews note + re-evaluates rule.

## Consequences

**Positive:**
- PR template guides contributors to self-check before opening PR — reduces back-and-forth.
- CI fails fast on schema, dedup, regex, enum, and symbol violations — prevents low-signal entries from reaching review queue.
- Idempotent catalogue seed (`_SeedMeta` sentinel per ADR-0007) + formal review process → quality baseline preserved across M6+ releases.
- Specific-gotchas rule ensures patterns are actionable, not generic advice (differentiator from Stack Overflow).
- EE guard rule maintains "CE-first" positioning (all MCP tools work with CE + optional EE layer, not vice versa).

**Negative:**
- 7-rule checklist adds friction for first-time contributors. Mitigation: PR template + ADR linked in CONTRIBUTING.md reduce discovery friction.
- Soft-fail symbol check doesn't block PR if Odoo source not indexed locally (CI env may not have all versions). Maintainer must visually verify qualified names. Acceptable: names are in docstrings so typos are catchable at glance.
- Maintainer override text adds editorial work. Acceptable: expected 1-2 overrides per 50 entries (~2-5% of PRs); documenting override preserves decision history.

**Risk:**
- **Pattern rot over Odoo versions** — `snippet_text` may reference v17 API that changed v19+. Mitigation: each pattern includes `odoo_version_min`; M7 candidate feature: mark deprecated patterns with `status: archived` + auto-hide from `suggest_pattern` after version_min + 2 major releases.
- **Community contributor language barrier** — gotchas rules require English; gotchas specificity rule ambiguous across cultural contexts. Mitigation: provide 3-5 example patterns in PR template (visual references > verbal rules).
- **EE reference false negatives** — module `note` is sometimes CE, sometimes EE, depending on version. Rule "MUST NOT mention proprietary modules" is conservative; acceptable to defer edge cases to maintainer judgment via override.

## Alternatives Considered

1. **No formal rules — accept all PRs** — risks catalogue quality drift, requires more maintainer filtering, defeats "Ship Wow Product" principle (output must be trusted by AI client).

2. **Hard-coded author whitelist (maintainers-only edits)** — rejects community contribution entirely. Defeats goal of catalogue scale.

3. **Pattern PR pre-approval via Discussion** — contributor proposes entry in GitHub Discussion, maintainer pre-approves before PR. Adds async round-trip. Acceptable but not chosen — async review already happens on PR itself.

4. **`patterns.schema.json` as source of truth only; no separate rules** — schema cannot express "≥3 gotchas" or "NO EE references" (JSON Schema lacks semantic validation). Hybrid approach (schema + CI checks + human review) necessary.

5. **Auto-regenerate patterns.json from community votes** — patterns ranked by rating in Web UI (M5+), highest-rated auto-promote. Adds social feature engineering. Deferred to M7 ("lifecycle wow").

## References

- ADR-0003: PatternExample Storage (Neo4j node + embeddings table, module/method enrichment, language filter)
- ADR-0007: Incremental Indexer (auto-reseed sentinel, _SeedMeta label, idempotent seed)
- `src/data/patterns.json` — catalogue source (54 entries as of M6 Wave 2)
- `src/data/patterns.schema.json` — JSON schema (M6 Wave 3 W3-2)
- `.github/PULL_REQUEST_TEMPLATE/patterns.md` — PR template (M6 Wave 3 W3-1)
- M6 Wave 3 plan: `docs/superpowers/plans/2026-05-10-milestone-6-wave-3-catalogue-ecosystem.md`
