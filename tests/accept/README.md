# Benchmark suite

End-to-end benchmark for the MCP handlers — correctness, token reduction,
latency.

- `questions.md` — human-readable question list (Q1-Q15).
- `runner.py` — model-graph runner (10 questions, model/field/method tools).
- `runner_p2.py` — view-resolver runner (top-50 views via `resolve_view`).
- `top50_views.json` — list of 50 primary views with the most
  extensions. Regenerate with `regenerate_top50_views.py`.
- `regenerate_golden.py` / `regenerate_golden_views.py` —
  re-label golden fixtures from live handler output (one-shot).
- `dump_live_odoo_views.py` — one-shot dump of canonical view arch
  from a live Odoo CE 17.0 install into
  `tests/fixtures/golden/resolve_view_live/`.

## View-resolver benchmark on a live host

The view-resolver benchmark requires a live Odoo CE 17.0 environment to
produce the golden XML that `runner_p2.py` compares against. Run on a
host that has the full CE corpus indexed plus the Odoo 17 venv set up;
laptop fixture subsets cannot reproduce this.

### 1. Regenerate the top-50 list

```bash
# repo root, against a live osm DB:
DATABASE_URL=postgresql:///osm_live?user=osm \
  OSM_TENANT=public \
  uv run python tests/accept/regenerate_top50_views.py
```

Writes `tests/accept/top50_views.json` with `_regenerated_at` filled in.
Commit the file. Re-run only on an Odoo pin bump or addons-path change.

### 2. Dump live-Odoo golden XML

```bash
# under an Odoo 17 venv with the addons path covering all CE modules:
source /path/to/odoo17/venv/bin/activate
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
# repo root, project venv activated (uv handles this):
DATABASE_URL=postgresql:///osm_live?user=osm \
  OSM_TENANT=public \
  uv run python -m tests.accept.runner_p2 --coverage-threshold 40
```

Writes a Markdown + raw-JSON report under `reports/`. Exit code 0 only
when every target passes (coverage, diff%, reduction%, P50 latency).
Exit code 1 on any failure; 2 on missing env.

## Model-graph regression

`runner.py` runs the 10 model/field/method questions. Run after handler
changes to confirm no regression:

```bash
DATABASE_URL=postgresql:///osm_live?user=osm \
  uv run python -m tests.accept.runner
```

## Troubleshooting

- **Odoo boot fails**: make sure the addons path covers every CE
  module used by the top-50 list. Missing `mail` or `account` addons
  is the most common source of `env.ref` returning `None`.
- **Golden file missing for a view**: `runner_p2.py` records status
  `no_golden` and continues. Coverage below the threshold fails the run
  — regenerate the dump to restore coverage.
- **Tokenizer mismatch**: `runner_p2.py` uses `tiktoken cl100k_base`
  (GPT-4 family). If the counter disagrees with `runner.py`, re-check
  that both runners are using the same encoding name.
