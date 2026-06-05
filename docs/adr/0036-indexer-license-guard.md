# ADR-0036 — License Policy Engine (Config-Driven Soft Block)

**Date:** 2026-05-22
**Status:** Proposed — M13

> Complements [ADR-0034](0034-multi-tenant-pooled-isolation.md): that ADR guards
> **who can read** the graph; this one decides **what enters / is exposed** and
> under what license status. Builds on the OEEL / Partnership analysis in ADR-0034.

## Context

OSM ingests source from registered repos into Neo4j + pgvector. Today there is **no license handling** at all. The multi-tenant move (ADR-0034) + serving customers with **private repos** raises a copyright question: which modules may OSM legally index/host/derive/expose?

License survey of the on-disk corpus (2026-05-22): LGPL-3 ×4721 (CE), **OPL-1 ×3756 — 100% authored by Viindoo/TVTMA**, AGPL-3 ×76, OEEL-1 ×13 (Odoo S.A. Enterprise modules bundled into the public `odoo/odoo` repo), GPL-3 ×2.

Two design decisions shape this ADR:

1. **Only OEEL-1 implicates Viindoo's own obligations.** OEEL-1 copyright is always **Odoo S.A.**, and Viindoo is directly bound by the Partnership Agreement §3.2 (confidential within staff; no redistribution; test/dev only). A repo submitter **cannot waive Viindoo's contractual duty to Odoo S.A.** For every other license — including third-party OPL-1 — the question is the *submitter's* rights, which the submitter can represent. So: **OEEL-1 is restricted by policy; everything else is the submitter's responsibility** (via Terms of Service representation at registration).

2. **Soft, config-driven — not a hard-coded skip.** The handling of restricted content must be a **config policy + a visible marker**, not a hard `if license == 'OEEL-1': skip` buried in code. Rationale: AI clients and humans should *know* the license status (not see content silently vanish), and if Viindoo later obtains **written permission from Odoo S.A.** (Partnership §3.2(b) provides this path), enabling OEEL must be a **config change, not a code change**.

(An earlier draft of this ADR proposed a hard skip + author allowlist; this revision replaces it with the config-driven soft-block model.)

## Decision

### D1 — License detection is always on

The indexer records `license` + a derived `copyright_owner` on every `Module` node, regardless of policy. License strings are facts — recording them is never a legal issue, and it makes the policy layer purely declarative.

### D2 — Config-driven `license_policy` map (single source of truth)

A config map assigns each license class an action ∈ {`serve`, `ingest_flagged`, `skip`}:

| License class | Default action | Rationale |
|---|---|---|
| LGPL / AGPL / GPL | `serve` | copyleft |
| OPL-1 | `serve` | submitter responsibility (D5) |
| `Other proprietary` / unknown / missing | `serve` | submitter responsibility (D5) |
| **OEEL-1** | **`skip`** | Viindoo's own Odoo S.A. obligation (D4) |

Actions:
- `serve` — index + expose normally.
- `ingest_flagged` — index into graph/vector but tag `restricted=true` and withhold from normal results (return only the notice).
- `skip` — do not ingest at all.

The map lives in config/constants (one place), enforced at `registry.build_registry()` (single chokepoint, mirroring ADR-0034 D4).

### D3 — Soft block = visible, never silent

When a module's action is `skip` or `ingest_flagged`, OSM surfaces a structured **`license_notice`** to **both audiences**:
- **AI clients** — a marker field in tool output (e.g. `license_notice: "OEEL-1 (Odoo S.A. Enterprise) — restricted; not exposed pending a written agreement with Odoo S.A."`), so the model *knows why* the content is limited and can tell the user, rather than hallucinating around a silent gap.
- **Humans** — a badge/notice in the Web UI.

This is the "soft block so AI and human know" requirement.

### D4 — Future-proofing is the core goal (no code change later)

Because handling is a config map (D2), changing posture is config-only:
- Obtain Odoo S.A. written permission → flip `license_policy.OEEL-1`: `skip → serve` (then a one-time re-index of the now-permitted modules) or `ingest_flagged → serve` (no re-index needed).
- Change risk appetite → flip between `skip` / `ingest_flagged` / `serve`.

**No code change in any of these transitions** — the engine already supports all three actions for any license.

> Honest caveat: creating the graph/vector from OEEL source is *itself* the regulated derivative act. Therefore the **safe default is `skip`** (no derivation occurs until permitted). `ingest_flagged` (host-but-don't-serve) is supported by the engine but means OEEL-derived data exists before permission — choose it only with, or in clear anticipation of, written permission. Default stays `skip`.

### D5 — Submitter responsibility for everything except OEEL-1

Only OEEL-1 implicates Viindoo's own Partnership obligation. For all other licenses (including third-party OPL-1), the **submitter represents-and-warrants** at repo-registration time, via **Terms of Service**, that they own or are licensed to have OSM index/derive the code. OSM then indexes them and the submitter bears responsibility. This is what actually shifts liability — without the ToS representation, "submitter responsibility" is not legally operative. A simple **notice-and-takedown** path handles direct claims from a third party (who is not bound by the submitter's ToS).

### D6 — `EE_CONFUSION` name dict is unaffected

`src/data/ee_modules.py` stores only EE module **names** (public facts, no source). Not source ingestion; not subject to this policy.

## Consequences

**Positive:**
- Legally-safe default (OEEL not derived until permitted).
- **Zero code change** to enable OEEL later (or to change posture) — config flip only.
- Transparent: AI + human see *why* content is restricted (no silent gaps).
- Policy is one declarative config = single source of truth (data-driven).
- Viindoo's OPL-1 catalogue + CE stay fully served.

**Negative:**
- A config knob + a ToS clause to maintain.
- `ingest_flagged` mode, if ever made default, hosts OEEL-derived data before permission — flagged as carrying exposure, so default remains `skip`.
- ToS representation + notice-and-takedown are new product/legal surface (not code-heavy, but must exist for D5 to hold).

## References

- `src/indexer/registry.py` — `build_registry()` (policy chokepoint); config (`license_policy` map).
- `src/data/ee_modules.py` — EE name dict (out of scope).
- ADR-0034 — OEEL + Partnership Agreement analysis; multi-tenant read-side isolation.
- Terms of Service (to be drafted) — submitter representation-and-warranty (D5).
- License survey 2026-05-22 (4721 LGPL-3, 3756 OPL-1 all Viindoo/TVTMA, 76 AGPL-3, 13 OEEL-1, 2 GPL-3).

---

## Amendment 2026-06-05 (PR #266 — OPL-1 / OEEL-1 framing correction, #263)

**Scope:** Corrects the license-label semantics used in EE-confusion detection code and documentation. No policy change; the D2 table and D4/D5 decisions are unchanged.

### What changed

PR #166 (the regression that introduced #263) mislabeled third-party OPL-1 modules (authored by Viindoo/TVTMA) as Odoo Enterprise Edition modules. The root cause was a code comment that described OEEL-1 and OPL-1 interchangeably. PR #266 WI-8 corrects the code comments and gating logic in `src/mcp/server.py:_edition_label` and `_is_ee_by_edition`:

- **OEEL-1** = Odoo Enterprise Edition License. Copyright is always Odoo S.A. These modules carry `edition='enterprise'` after indexing. OSM's EE-confusion warning targets this class.
- **OPL-1** = Odoo Proprietary License. This is Odoo S.A.'s license for **third-party** / proprietary Odoo apps. Viindoo's `tvtmaaddons` catalogue is published under OPL-1. OPL-1 is **not** Odoo Enterprise — modules under OPL-1 carry `edition='viindoo'` (or `'custom'`) after indexing, not `edition='enterprise'`.

The `_edition_label` helper now respects `_FIRST_PARTY_EDITIONS = {"viindoo"}`: when `edition='viindoo'`, the label is derived from the edition enum directly **before** the license lookup, so a Viindoo OPL-1 module is labeled "Viindoo" (not "Odoo Enterprise"). The EE-confusion gate (`_is_ee_by_edition`) has always checked `edition == 'enterprise'` (correct); the regression was only in comment wording and `_edition_label` license-first ordering.

**Correction to ADR-0036 D2 rationale phrasing:** the existing text "OPL-1 ×3756 — 100% authored by Viindoo/TVTMA" is a survey observation (correct). The phrase "OPL-1 is intentionally OPL-1=Viindoo proprietary" found in earlier phase notes was imprecise — OPL-1 is Odoo S.A.'s license mechanism for third-party apps, not a Viindoo-specific license. The policy decision (D5: OPL-1 = submitter responsibility, serve by default) is unchanged and correct.
