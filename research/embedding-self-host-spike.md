---
status: done
scope: research/embedding-self-host-spike
date: 2026-04-22
implications_for:
  - ../decisions/0002-embedding-provider.md
  - ../specs/find_examples.md
  - ./embedding-benchmarks.md
---

# Embedding self-host spike — 3-model self-retrieval on Odoo CE fixture

## Goal

Validate that the self-host path in [ADR-0002](../decisions/0002-embedding-provider.md)
(Option B — `bge-code-v1` as first-class alternative to Voyage API) is
technically viable on the hardware we already have:

- Dev machine: RTX 3060 12 GB, WSL2 Ubuntu 24.04, torch 2.6.0+cu124.

The spike is **not** a head-to-head quality comparison against the Voyage
default. Quality comparison on real Viindoo code (with Vietnamese variable
names) is P3 work; see `embedding-benchmarks.md` placeholder.

This spike answers a narrower question: does any of the three candidate
self-host models (`bge-code-v1`, `bge-m3`, `jina-v2-base-code`) fall apart
on 12 GB VRAM or produce patently bad rankings? If none do, ADR-0002
Option B stands as-is.

## Method

**Corpus** — `tests/fixtures/odoo_ce_subset` (46 Python files, 10 CE modules).
Extracted with `scripts/bench_corpus.py` (AST walk):

- Filter: `len(docstring) ≥ 20 chars` AND `≥ 3 non-trivial body statements`
- Yield: 258 `(docstring, body)` pairs
- Spread: mail 85, account 61, stock 39, base 30, sale 25, product 16,
  contacts 1, sale_management 1
- Body size p50 = 1238 chars, p95 = 6539, max = 17412
- Docstring size p50 = 230, p95 = 1349

**Task** — Self-retrieval. Query = docstring. Expected top-1 = the method
body from which the docstring was extracted, among the pool of 258 bodies.
Metric: Recall@{1, 5, 10}, MRR (mean reciprocal rank).

**Harness** — `scripts/bench_embed.py`:

- `sentence-transformers 5.4.1`, `transformers 4.57.6`, torch 2.6.0+cu124, CUDA
- `normalize_embeddings=True` → cosine = dot product
- `max_seq_length` capped uniformly at **2048 tokens** (see Caveats §2)
- `batch_size = 8` (native jina-v2 at seq=8192, batch=32 triggered a
  21 GiB attention tensor OOM — forced this knob down)
- Per-query latency: 20 warmup + 100 measurement iterations, warm cache,
  `torch.cuda.synchronize()` bracketed

## Results

### Headline table

| Model | Params | Recall@1 | Recall@5 | MRR | VRAM peak | Disk | Dim | P50 | P95 | Throughput |
|---|---|---|---|---|---|---|---|---|---|---|
| `jinaai/jina-embeddings-v2-base-code` | 137M | 97.7% | 100.0% | 0.988 | 5.7 GB | 310 MB | 768 | 8.7ms | 18.7ms | 28.9 items/s |
| `BAAI/bge-m3` | 568M | 97.3% | 100.0% | 0.986 | 3.0 GB | 2.2 GB | 1024 | 18.8ms | 36.1ms | 12.3 items/s |
| `BAAI/bge-code-v1` | 1.54B | **98.8%** | **100.0%** | **0.994** | 8.0 GB | 5.9 GB | 1536 | 60.8ms | 118.7ms | 4.8 items/s |

Truncation: 8 of 258 bodies exceed the 2048-token cap (3.1%), 0 of 258
docstrings. All three models see the same truncation mask → fair comparison.

Raw per-model JSON: [reports/embed-spike/](../reports/embed-spike/).

### What the numbers say

1. **Recall@5 saturates at 100% for all three** — self-retrieval on
   Odoo CE docstrings is too easy to separate models. This is the spike's
   biggest caveat (see Caveats §1).
2. **Recall@1 + MRR do discriminate slightly**: `bge-code-v1` (98.8 / .994)
   > `jina-v2` (97.7 / .988) > `bge-m3` (97.3 / .986). The gaps are
   < 2% and within noise for a 258-query set. Do not promote these gaps
   to product-level conclusions.
3. **Latency cost of quality is real**: `bge-code-v1` is **7× slower P50**
   than `jina-v2` and **3.2× slower** than `bge-m3`, while recall@1 differs
   by ~1 point. If latency matters, the jump from jina → bge-code-v1 is
   not automatically justified by these numbers.
4. **VRAM comfortably fits all three on 12 GB.** `bge-code-v1` peaks at
   8 GB with batch=8 + max_seq=2048 — 4 GB headroom for query encode +
   Postgres + other processes. Batch could likely grow to 16 safely.
5. **Ranking against `find_examples` P50 < 200ms acceptance target**:
   - `jina-v2`: 8.7ms embed → ~191ms for ANN + DB — huge budget.
   - `bge-m3`: 18.8ms → ~181ms — plenty.
   - `bge-code-v1`: 60.8ms → ~139ms — tight but passes.

## Conclusion

### For ADR-0002

**No change to the decision.** Voyage remains default for Hosted; `bge-code-v1`
remains the first-class self-host alternative. Spike validates Option B is
not disqualified by hardware constraints on a 12 GB consumer GPU. The kill
criteria in ADR-0002 are unchanged because this spike does not compare
against the Voyage API.

Revision added to ADR-0002 summarising measured VRAM / latency numbers so
reviewers do not have to re-derive feasibility later.

### For `specs/find_examples` (P3)

Three take-aways feed into the spec:

1. P50 < 200ms target is **reachable** with any of the three embedders
   on commodity GPU. Budget shape depends on which embedder ships first.
2. `bge-m3` is a surprisingly strong secondary self-host option — smaller
   VRAM, 3× faster than `bge-code-v1`, negligible recall delta on this
   (easy) corpus, AND multilingual so it may win on Vietnamese variable
   names in the P3 benchmark. Worth keeping on the candidate list for the
   P3 real-corpus shootout.
3. Recommend `embedding-benchmarks.md` (P3) use at least two adversarial
   axes this spike could not test:
   - hand-labelled NL-intent queries (not docstrings) to detect whether
     the model understands code intent, not just paraphrase matching
   - Vietnamese-heavy corpus (`tvtmaaddons/`) to measure cross-lingual
     degradation

### For hardware planning

12 GB VRAM is sufficient for all three candidate self-host models at
sane batch sizes. No upgrade needed before P3. An 8 GB GPU would still
run `jina-v2` and `bge-m3` comfortably but would push `bge-code-v1`
to batch=2-4 territory.

## Caveats

1. **Self-retrieval is a proxy, not the real task**. Docstrings for Odoo
   methods tend to paraphrase method names ("Compute the display name…"
   → `_compute_display_name`). Any decent code-aware embedder will excel.
   Real `find_examples` queries ("how do I compute delivery cost from
   order lines") have weaker lexical overlap with ground-truth bodies.
   Expect real recall@5 well below 100%. That is why ADR-0002 Revision
   does **not** promote the self-host default on the back of these numbers.
2. **max_seq_length capped at 2048** uniformly. Jina-v2 native is 8192
   but triggered a 21 GiB attention-tensor OOM on 12 GB VRAM at
   batch=32. Cap is applied to all three models for apples-to-apples
   — but it means bge-code-v1's 32 768-token native context is not
   exercised. Production find_examples embeds chunks, not full files,
   so this cap is realistic.
3. **Corpus is English-only.** No Vietnamese variable names, no
   Viindoo-specific domain terms. Vietnamese signal is the main
   open question for ADR-0002 and is deferred to the P3 benchmark on
   `tvtmaaddons/` per `research/embedding-benchmarks.md` §2.
4. **Recall@5 saturation** means this spike cannot discriminate the three
   models on quality. Recall@1 / MRR differences are within noise. The
   P3 benchmark needs queries harder than "docstring → body".
5. **Latency measured on a warm cache.** First-query cold start can be
   10-100× slower depending on tokeniser init. Not measured; both
   `find_examples` and indexer see only warm-path traffic.
6. **VRAM scaling with batch size** is model-specific. Jina's ALiBi
   attention used 5.7 GB peak on 137M params — more than bge-m3's 3 GB
   peak on 568M params, because attention intermediates scale with
   `batch × seq² × heads` regardless of weight size. If future P3 work
   needs larger batches, bge-m3 scales better than jina-v2.

## References

- Decision: [`decisions/0002-embedding-provider.md`](../decisions/0002-embedding-provider.md)
  — Revision 2026-04-22 references this file
- Placeholder: [`research/embedding-benchmarks.md`](embedding-benchmarks.md)
  — P3 full benchmark with Vietnamese + hand-labelled queries still owed
- Scripts: `scripts/bench_corpus.py`, `scripts/bench_embed.py`
- Raw results: [`reports/embed-spike/`](../reports/embed-spike/) (3 JSON files)
- Spec: [`specs/find_examples.md`](../specs/find_examples.md) — P50 target
  this spike de-risks
