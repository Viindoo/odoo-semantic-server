# ADR-0053: Module identity card, edition reclassification, and profile coverage transparency

**Status:** Accepted

**Date:** 2026-06-28

**Author:** Viindoo Engineering (issue Viindoo/odoo-mcp-client#121 - server slice)

**Relates to:** ADR-0036 (license policy engine / `copyright_owner`), ADR-0013 (Model
ranking heuristic), ADR-0048 (same-name INHERITS topology + ORM read bounds),
ADR-0028 (discriminator consolidation), ADR-0023 (tool-output tree grammar,
English-only), ADR-0034 (multi-tenant fail-closed choke-point), ADR-0050
(read-side timeout hardening)

---

## Context

GitHub issue #121 is a field retrospective from a real Viindoo pre-sales session.
An OSM-grounded deliverable carried two systematic FACTUAL errors that only a human
caught:

- `l10n_vn_viin_accounting_vninvoice` was asserted to be "VNPT" when it is actually
  VNInvoice / VNIs.
- `l10n_vn_viin_accounting_meinvoice` (MISA meInvoice) was omitted entirely.

Root cause on the server side: the human-authored display name
(`manifest['name']` = `ir.module.module.shortdesc`, e.g. "E-Invoice - VNIs VN-Invoice
Integrator") and `manifest['author']` were **never indexed**, so
`check_module_exists` / `describe_module` could not surface them. The agent was
forced to infer the provider from the technical slug - and guessed wrong. This is a
server DATA gap, not an agent reasoning failure.

A second, related gap: `_detect_module_edition` only recognized `viindoo` via a
`viin_`/`to_` name prefix. Viindoo's `l10n_vn_viin_accounting_*` addons start with
`l10n_` and so were mislabeled `custom`, even though they are OPL-1 and authored by
Viindoo.

A third gap (Rec.4): a profile only ever shows what IS indexed; nothing surfaced what
might be MISSING from a profile, so "absence from the index" read as "absence from the
product".

## Decision

### 1. Index a module identity card (raw, provenance-tagged) - P2

Add two RAW manifest fields to the Module node, ALONGSIDE the existing (lossy,
normalized) `copyright_owner` (NOT replacing it - `copyright_owner` is load-bearing
for the ADR-0036 license policy):

- `shortdesc` = `manifest['name']` (the human display name; distinct from the
  technical `name` slug).
- `author` = `manifest['author']` RAW, coerced str|list -> str via the new shared
  `_normalize_author()` helper (DRY with `_derive_copyright_owner`).

Both default to `None` (NOT `''`) so "manifest did not declare" stays distinct from
"declared empty" across versions (`author` is absent in CE core v9-v17). They are
written with ON CREATE direct / ON MATCH `coalesce($v, m.v)` (same safety pattern as
`summary`/`repo_url`: a later re-index that lacks the key never erases prior data).
The composite MERGE key `(name, odoo_version)` is untouched.

`check_module_exists` renders an "Identity (from indexed manifest):" block (display
name / summary / author), and `describe_module` renders the display name as the top
header line plus author in the Manifest sub-tree. Both render only when the field is
non-NULL (graceful degrade before backfill). The server exposes RAW data + a single
provenance label and deliberately does NOT classify brand/provider - that inference
(and any live-verify) is the client's job. No curated brand-mapping table is added
(it would be a second source of truth that drifts from the manifest - ETHOS #11 SSOT).

**Extended manifest metadata (follow-up to the identity card).** Nine more raw
manifest keys are indexed on the Module node, same pattern (default `None`, ON CREATE
direct / ON MATCH `coalesce`, MERGE key untouched): `description` (FULL, not
truncated), `website`, `live_test_url`, `demo_video_url`, `support`, `sequence`
(coerced int), `old_technical_name`, `price` (coerced float), `currency`.
`installable` is intentionally NOT indexed - it is a gate-only key (a module with
`installable=False` is skipped at registry build, so every indexed node would carry
`installable=True`, pure noise). Render policy: `check_module_exists` and
`describe_module` surface `website`, `price` ("`<price> <currency>`", rendered even at
0.0 - a priced-but-free marketplace module), and `old_technical_name` when non-NULL;
`live_test_url` / `demo_video_url` / `support` / `sequence` are INDEX-ONLY (queryable,
not rendered). `description` is OPT-IN: `describe_module(include_description=True)`
appends the full text as a "Description (from indexed manifest):" block (and only then
is the long field SELECTed at all) - the default keeps the overview lean and is
backward compatible (no tool-count change; still 31 tools / 9 resources).

### 2. Edition reclassification: OPL-1 + Viindoo/TVTMA author -> viindoo - P5

`_detect_module_edition` gains a rule, placed AFTER the OEEL-1 check and BEFORE OCA:
when `license == "OPL-1"` AND the normalized author contains "Viindoo" or "TVTMA",
return `viindoo`. OPL-1 is Odoo S.A.'s THIRD-PARTY proprietary license - it is NOT
Odoo Enterprise (OEEL-1), so this maps to `viindoo`, never `enterprise` (the ADR-0036
invariant "OPL-1 != enterprise" is preserved because OEEL-1 wins first). Third-party
OPL-1 modules whose author is not Viindoo/TVTMA stay `custom` (no over-claim). On 743
real Viindoo modules this rule has 0 false-negatives; OPL-1 only ever appears in
Viindoo repos, so false-positives are ~0.

The 3-tier edition labels are sharpened to carry free / paid / not-resold semantics
(verbose ASCII, ETHOS #0): `community` -> "Community (CE) - free, bundled with Odoo
CE"; `viindoo` -> "Viindoo Commercial - paid Viindoo subscription app"; `enterprise`
/ OEEL-1 -> "Odoo Enterprise (EE) - Odoo S.A. licensed, not resold by Viindoo". The
`_edition_label` hub stays the single SSOT, so every tool updates in lock-step. The
#263 regression guard ("a `viindoo` module must never read 'Odoo Enterprise'") is
preserved - the relabel actually removes the prior "(EE)" confusion from the Viindoo
label.

**INTENDED blast-radius (H1):** `EDITION_PRIORITY` (SSOT in `src/constants.py`,
unchanged) ranks `custom`=ELSE=4 below `viindoo`=2. Reclassifying a module from
`custom` to `viindoo` therefore SHIFTS the edition tiebreak used by the ORM same-name
field/method dedup (`_edition_rank_cypher`, tier 4 in the ADR-0013 5-tier ranking, at
`_resolve_field` / `_resolve_method` / the model-resolution ranking). For a field or
method defined under the same name by both a reclassified Viindoo module and another
module - with all higher tiers (is_definition, field_count, dependents) equal - the
Viindoo module now ranks higher (canonical). This is the correct outcome (a Viindoo
addon should rank as Viindoo, not as anonymous custom) and is locked by an integration
test that asserts the ordering (red if the reclassify is reverted to `custom`).

### 3. Profile coverage transparency: `profile_inspect(method='coverage')` - P1/Rec.4

A NEW discriminator method (ADR-0028, no new tool - tool count stays 31) renders
indexed module coverage by category for a profile, with a data-driven superset-diff:
two flat aggregations (ADR-0048 no-VLP, bounded by `_data_bounded` per ADR-0050) -
(1) per-category count WITHIN the profile, (2) per-category count across the whole
in-scope index. Each category shows `in_profile` vs `indexed_elsewhere`
(= in-scope total minus in_profile); a non-zero `indexed_elsewhere` is a "may be
incomplete" signal derived purely from Neo4j, never from a curated table. The
choke-point mirrors `_profile_summary` exactly: `**_scope(None)` + `profile_name=name`
(NOT `_scope(name)`, which would narrow `own=[name]` and wrongly drop modules stamped
with the full ancestor chain), with profile membership applied separately via
`$profile_name IN m.profile` (ADR-0034 fail-closed). A caveat routes explicitly to
live-verify: "Absence from this list != absence from the product ... cross-check live
`ir.module.module` - the static index cannot prove product absence." (ASCII `!=`).

## Consequences

- **Re-index required (`--full`) for P2 + the edition reclassify.** `shortdesc`,
  `author`, and the recomputed `edition` are index-time properties; incremental
  re-index only touches modules with a git diff, so unchanged manifests stay
  NULL/old-edition until a full pass. Tools degrade gracefully meanwhile (no identity
  block, old edition label - both valid). The label-text change and the coverage
  method are read-time and live immediately after deploy (no re-index).
- No schema migration: Neo4j has no auto-migration; the new properties appear as
  nodes are re-written by `--full`. The `(name, odoo_version)` MERGE key is unchanged.
- `copyright_owner` is unchanged and still drives the ADR-0036 policy; `author` is a
  parallel raw field for identity only.

## Alternatives considered

- **Curated brand-mapping data file** (slug/shortdesc -> provider): rejected (D1) -
  it creates a second source of truth that drifts from the manifest and is itself a
  guessing layer wearing an "authoritative index" badge. Raw exposure is enough: the
  display name already names the provider.
- **Replace `copyright_owner` with raw `author`**: rejected (D2) - would break the
  ADR-0036 license policy, which needs the normalized owner.
- **Fold coverage into `method='summary'`**: rejected (D4) - the category breakdown +
  caveat is a separate concern; summary stays lean (ADR-0028 supports new methods).

## Test

Behavior-protecting, red-before-green: unit tests for `_normalize_author`
(str/list/tuple/None) and `_detect_module_edition` (OPL-1+Viindoo -> viindoo, OPL-1
third-party -> custom, OEEL-1 -> enterprise invariant, prefix wins) on the real
e-invoice manifests; the label tests are rewritten SEMANTIC (preserving the #263
fail-ability: a `viindoo` label must contain "Viindoo" and never "Odoo Enterprise").
Integration (Neo4j) tests cover the writer roundtrip + coalesce, identity-block
render/hide, the direct "VNIs" factual fix, the H1 edition-rank ordering, and the
coverage superset-diff + caveat + ADR-0034 tenant-leak guard.
