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

## References

- Architecture: `../architecture/vector-store.md`
- Spec: `../specs/find_examples.md`
- Research needed: `../research/embedding-benchmarks.md`
