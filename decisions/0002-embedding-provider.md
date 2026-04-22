---
status: accepted
scope: decisions/0002
date: 2026-04-21
accepted_date: 2026-04-22
deciders:
  - David Tran
  - Tran Truong Son
---

# ADR-0002: Default embedding provider

## Context

P3 adds semantic search. We need an embedding model. Options are an API (Voyage, OpenAI, Jina) or a self-hosted model. The dev machine has 12 GB VRAM, 32 GB RAM — enough to run many open-source code-embedding models locally. The Hosted tier needs a predictable-cost option.

## Drivers

- Cost ceiling for first 100 paying Hosted customers
- On-prem / offline customers need a self-host path from day one
- Quality matters most for Vietnamese variable names and domain-specific Viindoo code
- Minimise vendor lock-in

## Considered options

### Option A — Voyage `voyage-code-3` default, self-host available

- **Pros**: strong code quality per published benchmarks, simple setup, predictable pricing
- **Cons**: API dependency, data leaves the cluster (mitigated by scope), ongoing cost

### Option B — Self-host `bge-code-v1` default, Voyage available

- **Pros**: zero egress, no API cost, fits on dev machine's 12 GB VRAM
- **Cons**: slower on CPU-only deployments, weaker on non-English text per early reports, more ops

### Option C — OpenAI `text-embedding-3-large`

- **Pros**: industry standard, well-understood
- **Cons**: not code-specialised, higher cost, weaker on code benchmarks than Voyage

## Decision

**Option A** — Voyage `voyage-code-3` as default. `bge-code-v1` supported as first-class self-host alternative, selected via one env var.

Rationale: for the Hosted tier (our paid path), simplicity and predictable cost win. For on-prem customers, we need self-host anyway — we already owe them a supported path. Making it first-class forces us to keep the embedding-provider interface clean.

## Consequences

- **Positive**: onboarding for Hosted customers is one env var + an API key; on-prem customers get an official supported path
- **Negative**: two codepaths to test; embedding-quality benchmarks must cover both
- **Follow-ups**:
  - Before P3 ships, publish a benchmark comparing the two on a Vietnamese-heavy Viindoo codebase
  - Document how to swap providers in `architecture/vector-store.md`

## Kill criteria

Revisit if:

- Voyage price increases materially (>50%) within 12 months
- `bge-code-v1` benchmark within 5% of Voyage on our real corpus → switch default to self-host
- Any customer data incident traced to API egress → switch default to self-host immediately

## Revision 2026-04-22 — Self-host feasibility spike

Ran a bounded self-retrieval spike on `tests/fixtures/odoo_ce_subset` (258
docstring→body pairs) against three self-host candidates on the team's
RTX 3060 12 GB. Details + raw numbers in
[`research/embedding-self-host-spike.md`](../research/embedding-self-host-spike.md).

Headline:

| Model                                 | VRAM peak (batch=8, seq=2048) | P50 latency | Disk   |
| ------------------------------------- | ----------------------------- | ----------- | ------ |
| `BAAI/bge-code-v1`                    | 8.0 GB                        | 60.8 ms     | 5.9 GB |
| `BAAI/bge-m3`                         | 3.0 GB                        | 18.8 ms     | 2.2 GB |
| `jinaai/jina-embeddings-v2-base-code` | 5.7 GB                        | 8.7 ms      | 310 MB |

Recall@5 saturates at 100% for all three on this (easy) corpus, so the
spike cannot distinguish their quality — real quality comparison (incl.
vs Voyage API, incl. Vietnamese corpus) is deferred to P3 per
`research/embedding-benchmarks.md`.

**Decision impact**: no change. Option B (`bge-code-v1` self-host
first-class) is not disqualified by hardware — fits 12 GB with headroom,
latency within `find_examples` P50 < 200ms budget. Kill criteria stand
unchanged because spike did not compare against Voyage.

**Secondary finding**: `bge-m3` is a surprisingly strong candidate —
smaller VRAM, 3× faster than `bge-code-v1`, multilingual so may beat
both on the Vietnamese corpus in P3. Add to P3 benchmark candidate list
(did not previously plan to).

## References

- Architecture: `../architecture/vector-store.md`
- Spec: `../specs/find_examples.md`
- Research needed: `../research/embedding-benchmarks.md`
- Spike report: `../research/embedding-self-host-spike.md`
