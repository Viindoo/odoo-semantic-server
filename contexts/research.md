---
status: confirmed
scope: mode/research
reads-with:
  - product_brief.md
---

# Research mode context

Load this when gathering evidence that will inform a decision or spec. Research mode produces **notes**, not code.

## What to read first

1. `product_brief.md` — so findings stay on-mission
2. `research/README.md` — index of prior research to avoid duplicating
3. The ADR or open question driving this research (if any)

## What to produce

A single file in `research/` named after the question, e.g. `research/embedding-benchmarks.md`. Rules:

- One topic per file. Do not merge "competitors" and "embeddings" into one doc
- Every claim has a source link, or is flagged **assumption** in bold
- Include a date header — research rots
- End with an **implications** section: what this means for specs / ADRs

## What NOT to do

- Do not write code in research mode. If implementation is needed to evaluate, write a throwaway spike outside the repo and summarise results here.
- Do not update `product_brief.md` based on research findings — propose an ADR instead.
- Do not stack new research on top of stale findings silently. Mark superseded files with a `> Superseded by: ...` line at the top.

## When blocked

If the question is actually a decision in disguise → switch to ADR mode, write it in `decisions/`, reference the research file for evidence.
