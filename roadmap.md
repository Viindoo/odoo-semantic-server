---
status: draft
scope: project
reads-with:
  - product_brief.md
---

# Roadmap

Timeline across the 5 phases defined in [`product_brief.md`](product_brief.md). Target: 12–16 weeks for P1–P4. P5 (distribution) runs in parallel from the end of P3.

```text
Week:   0    2    4    6    8    10   12   14   16
        |----|----|----|----|----|----|----|----|
P1 ████████████                                       Python Model Graph
P2            ████████████                            XML View Resolver
P3                        ████████                    Hybrid Retrieval
P4                                ████████████████    Full Stack + Impact
P5                            ████████████████████    Public Distribution
```

## Phase gating

Each phase has an **exit criterion** and an **accept test**. Do not advance to the next phase until both pass. Criteria live in the individual spec files — see the `specs/` folder.

Every tool-shipping phase must pass **both** a correctness floor **and** a token-reduction target. Correctness alone is not enough — this product is sold on context savings.

| Phase | Tools shipped | Correctness floor | Token-reduction target | Spec files |
| ----- | ------------- | ----------------- | ---------------------- | ---------- |
| P1 | `resolve_model`, `resolve_field`, `resolve_method` | 95% override-chain accuracy on curated test set | ≥90% vs raw-source baseline on 10-model fixture | `specs/resolve_*.md` |
| P2 | `resolve_view` | Final XML diff <5% vs live Odoo on top-50 most-inherited views | ≥70% vs raw-source baseline | `specs/resolve_view.md` |
| P3 | `find_examples` | Recall@10 >80%; cost <$2 / 100k-LOC full index | ≥95% vs raw-grep baseline | `specs/find_examples.md` |
| P4 | `impact_analysis` | Covers >80% of affected files on 5 historical refactor tickets | ≥95% vs raw-grep-and-read baseline | `specs/impact_analysis.md` |
| P5 | Docker / CLI / doc site | One-command setup works for 10 external users | N/A (distribution phase) | n/a |

## Ordering rationale

- **P1 first** — model graph is the minimum useful product; every other tool depends on it
- **P2 before P3** — view resolution is deterministic (graph only); we want the graph story solid before adding vectors
- **P3 turns on BYOC** — semantic search is what makes indexing customers' private code valuable
- **P4 is amplifier** — impact analysis needs all prior layers
- **P5 parallel** — distribution work starts at P3 end because that's when the product becomes useful to external users

## Value vs effort (80/20)

```text
Value
  ^
  |   [P1 + P3]                    <- bulk of the value
  |
  |   [P2]                         <- correctness amplifier
  |
  |   [P4]                         <- review-time amplifier
  |
  |   [P5]                         <- commercialization
  +----------------------> Effort
```

## Revision

Update this file when a phase boundary changes or an exit criterion shifts. Material changes warrant an ADR (see `decisions/`).
