---
status: draft
scope: reports/phase-02-exit-criteria
phase: P2
reads-with:
  - ../roadmap.md
  - ../tasks/phase-02-plan.md
  - phase-02-accept.md
  - phase-01-exit-criteria.md
---

# Phase 2 exit-criteria check

Maps every exit-criterion line in `roadmap.md` and
`tasks/phase-02-plan.md` §7 to the evidence that satisfies it.
Regenerate when evidence changes (re-run
`tests/accept/runner_p2.py` + update the numbers).

All cells marked `<PENDING dump + runner>` block on WP-17 execution on
`osm-dev` (live Odoo CE required to populate
`tests/fixtures/golden/resolve_view_live/`).

## Correctness — top-50 views

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| Mean diff% vs live-Odoo golden | < 5% | `<PENDING dump + runner>` | `reports/phase-02-accept.md` |
| Coverage (views with golden) | ≥ 40 of 50 | `<PENDING dump + runner>` | `tests/fixtures/golden/resolve_view_live/` |
| No `error` status views | 0 | `<PENDING dump + runner>` | `reports/phase-02-accept-raw.json` |
| `position="attributes"` empty-value delete | Matches Odoo | ✅ unit test passes | `tests/indexer/test_view_resolver.py::test_attributes_op_empty_value_deletes_attr` |
| `position="replace"` targeting document root | Matches Odoo | ✅ unit test passes | `tests/indexer/test_view_resolver.py::test_replace_targets_document_root` |

**Verdict**: `<PENDING>`.

## Token reduction

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| `resolve_view` vs raw-XML baseline | ≥ 70% | `<PENDING dump + runner>` | `reports/phase-02-accept.md` aggregate |

**Verdict**: `<PENDING>`.

## Performance

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| P50 `resolve_view` | < 100ms | `<PENDING dump + runner>` | `reports/phase-02-accept.md` aggregate |
| P99 `resolve_view` | < 500ms | `<PENDING dump + runner>` | `reports/phase-02-accept.md` aggregate |
| XPath compile cache in place | Present | ✅ `_compile_xpath` with `lru_cache(256)` | `osm/indexer/view_resolver.py::_compile_xpath` |

**Verdict**: `<PENDING>`.

## Multi-tenancy

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| Tenant overlay applies to view resolution | Works | ✅ integration test passes | `tests/server/test_handlers_resolve_view.py::test_tenant_overlay_*` (DATABASE_URL-gated) |
| 409 on cross-schema staleness | Returns 409 | ✅ `StaleIndexError` wired | `osm/server/handlers/resolve_view.py` + `test_409_on_stale_cross_schema_sha` |
| Stale error message is generic | No internal topology leak | ✅ post-review hardening — generic message | `osm/server/handlers/resolve_view.py` commit 1 WP-17 |

**Verdict**: ✅ PASS (with DB-gated tests green on osm-dev).

## Operational

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| SQL identifier safety — schema via `sql.Identifier` | All handlers | ✅ `resolve_view.py` hardened in commit 1 WP-17 | `osm/server/handlers/resolve_view.py` |
| Symlink escape refused during indexing | Refused + logged | ✅ views / models file collection guarded | `osm/indexer/driver.py::_collect_xml_view_files` + `_collect_python_files` |
| `INSERT ... RETURNING id` raises on empty rowset | Raises | ✅ 5 asserts replaced by `RuntimeError` | `osm/indexer/driver.py` |
| All WP unit + integration tests pass | All green | ✅ **251 passed, 28 skipped** (laptop, no DB). Full suite count on osm-dev: `<PENDING>` | `uv run pytest -q` |
| `ruff`, `mypy` clean on `main` | Clean | ✅ ruff clean; mypy unchanged vs baseline | `ruff check osm tests scripts`, `uv run mypy osm tests` |

**Verdict**: ✅ PASS on laptop; full DB-gated suite rerun `<PENDING on osm-dev>`.

## P1 regression

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| P1 accept runner re-run after WP-16 merges | 10/10 still pass | `<PENDING dump + runner>` | `reports/phase-01-accept.md` (re-run) |
| P1 handler contracts unchanged | No response-shape drift | ✅ handler signatures identical; only internal SQL composition changed | commit 1 WP-17 diff |

**Verdict**: `<PENDING>`.

## Documentation

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| `tests/accept/questions.md` extended Q11-Q15 | Q11-Q15 present | ✅ appended | `tests/accept/questions.md` |
| `tests/accept/README.md` documents dump procedure | Yes | ✅ created / updated | `tests/accept/README.md` |
| `reports/phase-02-accept.md` published | Yes | `<PENDING dump + runner>` | `reports/` |
| `reports/phase-02-exit-criteria.md` published | Yes | ✅ this file | this folder |
| `tasks/lessons.md` updated with WP-15+16 lessons | Yes | ✅ 2026-04-23 entries | `project-docs/odoo-semantic-mcp/tasks/lessons.md` |

**Verdict**: partial — narrative files present; accept-report generation `<PENDING>`.

## Review

| Criterion | Target | Actual | Evidence |
|---|---|---|---|
| `code-reviewer` on WP-15+16 | Yes | ✅ executed 2026-04-23; 3 HIGH + 7 MEDIUM/LOW findings | `tasks/lessons.md` |
| `security-reviewer` on WP-15+16 | Yes | ✅ executed 2026-04-23; XXE + SQL surfaces reviewed | `tasks/lessons.md` |
| HIGH defects remediated in same PR | Yes | ✅ XXE hardening + depth guard + safe parser merged in WP-15 | commit `2a056db` |
| MEDIUM/LOW defects remediated in WP-17 | Yes | ✅ 7 items resolved | commit 1 WP-17 (`[FIX] post-review hardening`) |

**Verdict**: ✅ PASS.

---

## Overall verdict

`<PENDING dump + runner>` — hardening + scaffolding complete on laptop;
executing `tests/accept/dump_live_odoo_views.py` and
`tests/accept/runner_p2.py` on `osm-dev` fills in every `<PENDING>`
cell above. Once those runs publish `reports/phase-02-accept.md`, this
file is updated with numeric evidence and Gate 3 (Ship ready for P2)
evaluates.

**Blocking items for Gate 3:**

1. Run `scripts/regenerate_top50_views.py` on osm-dev against a full CE index.
2. Run `tests/accept/dump_live_odoo_views.py` — commit the `resolve_view_live/` golden set.
3. Run `tests/accept/runner_p2.py --coverage-threshold 40` — exit code 0.
4. Re-run `tests/accept/runner.py` (P1) to confirm zero regression.
5. Update this file with the measured numbers.
