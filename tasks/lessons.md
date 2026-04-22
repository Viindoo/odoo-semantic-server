---
status: active
scope: project
---

# Lessons Learned

Record mistakes and non-obvious discoveries so we do not repeat them. Newest on top.

## Format

```
## YYYY-MM-DD — Short title

**What happened**: one or two lines.
**Root cause**: what actually caused it, not the surface symptom.
**Lesson**: what we will do differently.
**Where it applies**: which parts of the project this rule kicks in.
```

---

## 2026-04-22 — Self-retrieval (docstring→body) saturates recall@5 on Odoo CE and cannot discriminate embedders

**What happened**: WP-13 spike ran 3 embedding models (bge-code-v1, bge-m3, jina-v2-base-code) against 258 docstring/body pairs from `tests/fixtures/odoo_ce_subset`. All three got Recall@5 = 100%. Recall@1 and MRR differed by < 2% — inside noise for a 258-item set.

**Root cause**: Odoo method docstrings paraphrase method names very closely ("Compute the display name…" → `_compute_display_name`). Any code-aware encoder hits this with trivial semantic match. Self-retrieval is a *paraphrase* task, not a real *intent→code* retrieval task.

**Lesson**: Self-retrieval is fine as a smoke signal ("does the embedder crash? does it fit VRAM?") but useless as a quality comparator. Real quality benchmarks need (a) NL-intent queries that do NOT paraphrase the method name, (b) negative distractors, (c) Vietnamese or domain-specific vocabulary. P3 `embedding-benchmarks.md` must NOT copy WP-13's harness — it needs hand-labelled queries.

**Where it applies**: P3 `find_examples` benchmark design. Any future "let me just use self-retrieval to pick a model" shortcut.

---

## 2026-04-22 — Attention tensors scale with `batch × seq²`, not param count — native max_seq of 8192 OOMs a 137M-param model on 12GB

**What happened**: First run of bench_embed against `jinaai/jina-embeddings-v2-base-code` (137M params, native max_seq=8192) with batch=32 failed with CUDA OOM — allocation of 21.23 GiB on a 12 GB GPU.

**Root cause**: `max_seq_length` defaults to the model's native — 8192 for jina-v2 (ALiBi). Attention intermediate tensors are `batch × heads × seq × seq × 4 bytes`. At batch=32, seq=8192: `32 × 12 × 8192² × 4 ≈ 100 GiB`. Param count (137M → weights 300MB) is negligible compared to the attention buffer.

**Lesson**: When benchmarking transformer embedders, always cap `max_seq_length` explicitly and lower batch_size below transformer defaults. Fair cross-model comparison requires the same cap. For Odoo method bodies the 95th percentile is ~6500 chars (~1600 tokens), so capping at 2048 loses < 5% of content while bringing VRAM within 12 GB even for 1.5B-param bge-code-v1 at batch=8.

**Where it applies**: `scripts/bench_embed.py` (now has `--max-seq-length` default 2048, `--batch-size` default 8). Any future embedding benchmark — P3 `embedding-benchmarks.md` on the larger tvtmaaddons corpus will need the same knobs.

---

## 2026-04-22 — Multiple classes in one module extending the same model collapse under UNIQUE(model_id, name)

**What happened**: WP-6 driver's first implementation of override_of write-back produced self-looping rows (e.g. `res.groups.write.override_of = <res.groups.write's own id>`) and flip-flopped values across successive runs. Second-run idempotence broke — 9 rows got wiped to NULL every other run.

**Root cause**: Odoo base module's `res_users.py` has 3 classes extending `res.groups`, each defining `write`. Each ParsedMethod hits `UNIQUE(model_id, method_name)` → DB has ONE row representing all three. The resolver still emits 3 distinct FieldOverrideLink / MethodOverrideLink. The apply step was keyed by `(model_name, module_name, name, start_line)` — 3 keys mapped to the same DB row id. The first apply walk wrote prev_id=self; `_populate_id_maps_from_db` on rerun only knew the last class's start_line, so earlier links got `None` and wiped the column.

**Lesson**: Resolver produces logical chain at ParsedX granularity but DB collapses at `UNIQUE(model_id, name)` — these two shapes diverge. The apply step must group by `(model_name, name)`, dedup by `my_id` via a `seen` set, and carry `prev_row` across the whole group (not across every link).

**Where it applies**: `osm/indexer/driver.py::_apply_override_links`. Same issue will come up for `resolve_view` in P2 when multiple `<record>` blocks patch the same view in one module.

---

## 2026-04-22 — Golden fixture labelling must come AFTER the handler ships

**What happened**: Hand-labelled `resolve_model.json` / `resolve_field.json` / `resolve_method.json` were written in WP-7 before WP-8 handlers existed. 3 handler golden-file tests failed on the first run — the handler returned MORE chain entries than the human labeler had recorded (e.g. missed that `sale_management/sale_order.py` also overrides `action_confirm`).

**Root cause**: Hand-labelling a multi-module extension chain from reading source is error-prone. Humans miss one file among 8. The handler (driven by indexer + resolver) is the authoritative oracle once it passes the 10/50/20 curated correctness threshold.

**Lesson**: For future P2+ tools, scaffold the golden with TODO skeletons containing only `{model_name, entity_name}`, then run the handler once the indexer is green and commit the output as golden. Human review spot-checks a few entries rather than labelling all.

**Where it applies**: WP-7 / fixture workflows for any new MCP tool. Use `scripts/regenerate_golden.py` as the template.

---

## 2026-04-22 — Postgres 16 vs 18 serialise sequence-default differently

**What happened**: `tests/test_schema_diff.py` passed on Postgres 16 but failed on Postgres 18 with `assert [('cache_metadata','id','bigint','NO',"nextval('cache_metadata_id_seq'::regclass)",...] == [('cache_metadata','id','bigint','NO',"nextval('_SCHEMA_.cache_metadata_id_seq'::regclass)",...]`.

**Root cause**: Pg18 inlines the schema prefix into `pg_get_expr()` output for sequence defaults even when the sequence lives in `public`; Pg16 does not. The original `_strip_schema` regex only stripped the schema name, leaving an orphan `.` in the Pg18 case.

**Lesson**: When normalising DB introspection output for cross-schema diff, also strip the separator (`.`) that follows the schema token. Assume future Postgres versions will keep shifting the serialisation.

**Where it applies**: `tests/test_schema_diff.py`. Any future test that diffs DDL across schemas via `information_schema` / `pg_catalog` / `pg_get_expr()`.

---

## 2026-04-22 — `SET LOCAL search_path` f-string injection is structural risk across sessions

**What happened**: Security review flagged that `_set_search_path(cur, tenant)` uses `f'SET LOCAL search_path TO "{tenant}", public'` without a local validation guard. Today safe because the only caller (`index()`) pre-validates via `tenancy.validate_tenant`, but a future caller in P5 could skip that step.

**Root cause**: Trust chain depends on "every caller remembers to validate" — fragile.

**Lesson**: Functions that inject an identifier into DDL/config commands via f-string must `validate_tenant()` themselves as a fail-fast guard. Defence in depth, not just defence at the boundary.

**Where it applies**: `osm/indexer/driver.py::_set_search_path`, `scripts/migrate.py` (also injects `--schema` arg into DDL without local validation), any future helper that takes a schema name.

---

## 2026-04-22 — Benchmark numerics stay honest when the harness bypasses the transport

**What happened**: WP-9 accept-test runner invokes handlers in-process via psycopg cursor instead of driving FastMCP over stdio. P50 came in at 0.07ms — orders of magnitude below the 20ms target.

**Root cause**: Measuring handler logic, not end-to-end MCP transport. FastMCP stdio adds JSON serialise + framing on top; real-world P50 will be 1–5ms higher. Chosen deliberately because (a) Claude Code MCP client driving needs external infrastructure, (b) transport overhead is a thin constant, (c) numeric comparison to exit targets is still sound with huge margin.

**Lesson**: Scope the benchmark to what it actually measures and say so in the report. `reports/phase-01-accept.md` Caveat section documents the in-process bypass. Do not let vanity numbers paper over measurement gaps.

**Where it applies**: Any future benchmark touching MCP handlers. Full E2E harness with a real MCP client lands when Phase 5 pilot customer validates.
