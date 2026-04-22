---
status: draft
scope: reports/phase-01-accept
phase: P1
date: 2026-04-22
reads-with:
  - ../tests/accept/questions.md
  - ../tasks/phase-01-plan.md
---

# Phase 1 accept-test results

Runner iterations per question: **100** (latency loop).
Live tenant schema: `osm_accept_95f25f27` (throwaway, dropped on teardown).
Token counter: `tiktoken` encoding `cl100k_base` (GPT-4 family).

## Per-question results

| QID | Tool | Status | Resp toks | Baseline toks | Reduction | P50 (ms) | P99 (ms) |
|-----|------|--------|-----------|---------------|-----------|---------|---------|
| Q1 | resolve_model | ok | 85 | 50118 | 99.8% | 0.05 | 0.68 |
| Q2 | resolve_model | ok | 121 | 11700 | 99.0% | 0.06 | 0.10 |
| Q3 | resolve_model | ok | 446 | 19845 | 97.8% | 0.10 | 0.15 |
| Q4 | resolve_field | ok | 200 | 17662 | 98.9% | 0.07 | 0.81 |
| Q5 | resolve_field | ok | 296 | 17831 | 98.3% | 0.08 | 0.15 |
| Q6 | resolve_method | ok | 130 | 17721 | 99.3% | 0.07 | 0.53 |
| Q7 | resolve_method | ok | 322 | 19458 | 98.3% | 0.08 | 0.17 |
| Q8 | resolve_model | ok | 124 | 46761 | 99.7% | 0.06 | 0.10 |
| Q9 | resolve_model | ok | 121 | 21697 | 99.4% | 0.06 | 0.21 |
| Q10 | resolve_model | expected_404 | 0 | 0 | — | 0.04 | 0.05 |

## Aggregate — token reduction by tool

| Tool | Questions | Mean reduction | Min reduction |
|------|-----------|----------------|---------------|
| resolve_field | 2 | 98.6% | 98.3% |
| resolve_method | 2 | 98.8% | 98.3% |
| resolve_model | 5 | 99.1% | 97.8% |

## Aggregate — latency

- Median P50 across successful questions: **0.07 ms**
- Max P99 across successful questions:   **0.81 ms**

## Notes

- Q10: NotFoundError raised as expected: model 'sale.fancyMadeUpModel' not in index
