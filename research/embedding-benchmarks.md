---
status: placeholder
scope: research/embedding-benchmarks
date: 2026-04-21
implications_for:
  - ../decisions/0002-embedding-provider.md
  - ../specs/find_examples.md
---

# Embedding benchmarks on real Viindoo corpus

**Status**: placeholder. Execute during early P3.

## Goal

Decide whether the default embedding provider in ADR-0002 still holds once tested on real Viindoo code (which contains Vietnamese variable names, domain-specific jargon like `thue_gtgt`, `bao_cao_thue`).

## Method

1. Assemble a query set of ~50 hand-labelled questions with expected hits
2. Index a representative slice of `tvtmaaddons/` + relevant CE modules
3. Embed with:
   - Voyage `voyage-code-3` (API)
   - `bge-code-v1` (self-hosted on dev machine's 12 GB VRAM GPU)
   - Optional: `jina-embeddings-v3` as a third data point
4. For each provider, compute Recall@1, Recall@5, Recall@10
5. Record cost per 1M tokens

## What we're looking for

- Recall@10 ≥ 80% (acceptance criterion for `find_examples`)
- Any provider that loses materially on Vietnamese content
- Cost difference that would shift ADR-0002

## Output

When filled, this file will have:

- Corpus statistics (size in LOC, token count, file count)
- Query set (as linked `.jsonl` or inline)
- Results table: provider × recall × cost
- Conclusion: does ADR-0002 stand, or do we propose ADR-0002a?

## Implications

- Direct input to `decisions/0002-embedding-provider.md` — may promote ADR to `accepted` or trigger a follow-up
- Feeds `specs/find_examples.md` acceptance test dataset
