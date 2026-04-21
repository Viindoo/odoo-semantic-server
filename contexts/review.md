---
status: confirmed
scope: mode/review
reads-with:
  - product_brief.md
---

# Review mode context

Load this when reviewing a PR or a doc change.

## What to read first

1. `product_brief.md` — the invariants
2. The ADRs in `decisions/` that cover the area touched by the change
3. The spec(s) the change claims to implement

## Review checklist

**Scope**
- Does the change match the spec it claims to implement?
- If the change is not covered by any spec, why is it in a PR? Request a spec first.

**Consistency**
- Data-model file updated if schema changed?
- Spec file updated if tool behaviour changed?
- Architecture file updated if a component contract changed?

**Brief alignment**
- Does the change stay within the product vision and the 5-phase plan in `product_brief.md`?
- If it breaks a brief invariant, is there an ADR approving the break?

**Risk surface**
- If the change touches parsing, indexing, auth, or data flow across customer boundaries, escalate to the security review step (see `security/`)

## When reviewing an ADR

- Is every "considered option" actually considered, or are alternatives straw-manned?
- Is the decision reversible? If not, how will we know we were wrong?
- Is there a kill criterion or trigger to revisit?

## When blocked

If the change is too big to review in context → ask for a split, do not rubber-stamp.
