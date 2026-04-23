---
status: draft
scope: specs/find_examples
phase: P3
reads-with:
  - ../product_brief.md
  - ../architecture/vector-store.md
  - ../decisions/0002-embedding-provider.md
---

# Spec: `find_examples`

## 1. Purpose

Semantic search across indexed code. Given a natural-language query or a code snippet, return ranked examples (methods, views) across CE + custom modules. This is the first tool that needs vectors — graph alone cannot answer "show me code that computes X".

## 2. Input schema

```json
{
  "query": "compute delivery cost from order lines",
  "limit": 10,
  "scope": {
    "modules": ["sale", "delivery", "viin_freight_*"],
    "kinds": ["method", "view"]
  }
}
```

- `query` (string, required) — NL or code
- `limit` (int, default `10`, max `50`)
- `scope.modules` (string[], optional) — glob list; default = all indexed
- `scope.kinds` (string[], optional) — subset of `method | view | qweb | js`; default = `method`

## 3. Output schema

```json
{
  "result": {
    "query": "compute delivery cost from order lines",
    "hits": [
      {
        "kind": "method",
        "model": "sale.order",
        "name": "_compute_amount_delivery",
        "module": "delivery",
        "file": "addons/delivery/models/sale_order.py",
        "line_range": [120, 145],
        "score": 0.87,
        "snippet": "def _compute_amount_delivery(self): ..."
      }
    ]
  },
  "indexed_at_sha": "abc1234",
  "warnings": []
}
```

## 4. Algorithm

1. Validate inputs, normalise scope
2. Embed query via configured provider (see `decisions/0002-embedding-provider.md`)
3. ANN search on vectors (HNSW) with `limit * 2` candidates
4. Filter candidates by scope (modules, kinds)
5. Graph-validate each hit: row still exists, still in-scope → drop stale hits
6. Return top `limit`

## 5. Data accessed

- Vector store (embeddings + chunk metadata)
- `models`, `methods`, `views` (for graph validation)
- Filesystem for snippets

## 6. Out of scope

- Reranking with a separate model (possible post-MVP enhancement)
- Semantic search over comments/docstrings alone (chunks are whole bodies; we embed them, not separate strings)
- Cross-language search (searching Python from a JS query, etc.) — scope per call chooses `kinds`

## 7. Acceptance criteria

### Correctness (necessary condition)

- [ ] Recall@10 > 80% on a hand-labelled query set of 50 questions
- [ ] Results respect tenancy: shared + current tenant only, never leaks across tenants
- [ ] Graph validation drops stale hits (chunk no longer exists in current SHA)

### Token / context reduction (primary value)

- [ ] For "find 3 examples of computing a delivery cost", response ≤3k tokens vs ≥50k if AI had to grep and read candidates
- [ ] Target: ≥95% token reduction vs raw-grep baseline

### Cost

- [ ] Cost <$2 per 100k-LOC full index
- [ ] Cost <$0.05 per incremental commit on a 100k-LOC project

### Performance

- [ ] P50 <200ms including embed call

## 8. Open questions

- Reranker on / off as default? Leaning: off initially; add if recall drops in real use
- How to handle non-English queries (Vietnamese variable names etc.)? Research needed — see `research/embedding-benchmarks.md`

## 9. References

- Architecture: `../architecture/vector-store.md`
- Decision: `../decisions/0002-embedding-provider.md`
