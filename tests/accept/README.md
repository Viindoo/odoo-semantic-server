# Accept-test harness

Accept tests drive the MCP handlers end-to-end and publish the numerical
evidence referenced by the phase exit-criteria reports.

- `questions.md` — human-readable source of truth (Q1-Q15).
- `runner.py` — Phase 1 runner (10 questions, model/field/method tools).
- `runner_p2.py` — Phase 2 runner (top-50 views via `resolve_view`).
- `top50_views.json` — list of 50 primary views with the most
  extensions. Regenerate with `scripts/regenerate_top50_views.py`.
- `dump_live_odoo_views.py` — one-shot dump of canonical view arch
  from a live Odoo CE 17.0 install into
  `tests/fixtures/golden/resolve_view_live/`.

## Phase 2 — resolve_view accept test on `osm-dev`

The P2 accept test requires a live Odoo CE 17.0 environment to produce
the golden XML that `runner_p2.py` compares against. Run on `osm-dev`
(the dev server that has the full CE corpus indexed + the Odoo 17 venv
already set up). The laptop fixture subset cannot reproduce this.

### 1. Regenerate the top-50 list

```bash
# On osm-dev, repo root:
DATABASE_URL=postgresql:///osm_live?user=osm \
  OSM_TENANT=public \
  uv run python scripts/regenerate_top50_views.py
```

Writes `tests/accept/top50_views.json` with `_regenerated_at` filled in.
Commit the file. Re-run only on an Odoo pin bump or addons-path change.

### 2. Dump live-Odoo golden XML

```bash
# On osm-dev, under the project's Odoo 17 venv:
source /home/odoo/venvs/odoo17/bin/activate
python tests/accept/dump_live_odoo_views.py \
    --config /etc/odoo/odoo.conf \
    --database odoo17_full \
    --views-json tests/accept/top50_views.json \
    --out-dir tests/fixtures/golden/resolve_view_live
```

Expected duration: 5-10 minutes for 50 views on first run (Odoo boot
dominates; a warm Postgres cache drops repeat runs to ~1 minute).

Studio-origin views are skipped automatically (no source equivalent).
Commit every `.xml` written to `resolve_view_live/` so CI and other
developers can run `runner_p2.py` without re-booting Odoo.

### 3. Run the top-50 benchmark

```bash
# On osm-dev, repo root, project venv activated (uv handles this):
DATABASE_URL=postgresql:///osm_live?user=osm \
  OSM_TENANT=public \
  uv run python -m tests.accept.runner_p2 --coverage-threshold 40
```

Writes `reports/phase-02-accept.md` + `reports/phase-02-accept-raw.json`.
Exit code 0 only when every exit criterion passes (coverage, diff%,
reduction%, P50 latency). Exit code 1 on any failure; 2 on missing env.

### 4. Update the exit-criteria report

Edit `reports/phase-02-exit-criteria.md` — replace each
`<PENDING dump + runner>` cell with the number from
`reports/phase-02-accept.md`.

## Phase 1 regression

`runner.py` runs the 10 P1 questions. After WP-16 merges, re-run it to
confirm no regression in the `resolve_model` / `resolve_field` /
`resolve_method` tools:

```bash
DATABASE_URL=postgresql:///osm_live?user=osm \
  uv run python -m tests.accept.runner
```

Confirm all 10 questions still pass; note the outcome under "P1
regression" in `reports/phase-02-exit-criteria.md`.

## Troubleshooting

- **Odoo boot fails on osm-dev**: make sure the addons path covers
  every CE module used by the top-50 list. Missing `mail` or
  `account` addons is the most common source of `env.ref` returning
  `None`.
- **Golden file missing for a view**: `runner_p2.py` records status
  `no_golden` and continues. Coverage below the threshold fails the run
  — regenerate the dump to restore coverage.
- **Tokenizer mismatch**: `runner_p2.py` uses `tiktoken cl100k_base`
  (GPT-4 family). If the counter disagrees with `runner.py`, re-check
  that both runners are using the same encoding name.
