# ADR-0055: Minimum bar for curated data (provenance, oracle, or a recorded reason)

**Status:** Accepted

**Date:** 2026-07-21

**Author:** Viindoo Engineering (issue #364)

**Relates to:** ADR-0002 (spec schema policy), ADR-0009 (pattern catalogue contribution),
ADR-0033 (tools_symbols curation), ADR-0054 (parse-verified framework test-base facts)

---

## Context

Issue #362 found a curated, version-keyed table (`_FRAMEWORK_BASES`) that had been silently
wrong for five Odoo majors with nothing in the repo able to notice - fixed by ADR-0054's
parse-verified oracle pattern. Issue #364 asked whether the SAME failure mode existed anywhere
else in the three `spec_data/` curated families (`cli_flags`, `lint_rules`, `tools_symbols`) and
in curated tables living outside `spec_data/`. It did, repeatedly, and in forms ADR-0054's oracle
pattern alone does not cover:

- **Rule-ID provenance (B4).** 19 of 69 distinct curated `pylint-odoo` rule_ids
  (`lint_rules_*.json`) collide with a real, currently-assigned OCA `pylint-odoo` code under a
  COMPLETELY DIFFERENT meaning - e.g. curated `W8110` = "`_columns` dict deprecated" while real
  installed `pylint-odoo` 10.0.7 `W8110` = "missing return after `super()`". An agent that looks
  the code up externally gets a different rule than the one this repo describes. This is not a
  drift-from-source problem an oracle fixes (there is no real source to diff a hand-invented ID
  against) - it is a MISSING FACT problem: nothing recorded whether an ID was ever verified real.
- **Duplicate reporting (B5).** `W8140` (OSM-local, static) and `E8501` (the real Odoo-vendored
  `_odoo_checker_sql_injection.py` code) carried an identical SQL-injection regex at v17.0-v19.0,
  so `lint_check` silently double-reported one defect. Two independently-added curated facts, no
  mechanism ever compared them to each other.
- **Dead field (C4).** `tools_symbol.schema.json`'s `note` property claimed "shown in tool
  output" for 204/204 curated entries; nothing in the load/write/MCP chain read it. The schema
  documented a contract the code never honoured, and nothing said so.
- **Unenforced schema (A7).** `cli_flag.schema.json` was loaded by a test helper
  (`_load_schema()`) that was then never called - a schema that cannot fail is decorative. 11
  real pre-existing data violations (`--verbose` default `0`, `--token-length` default `16` -
  integers where the schema declares `["string", "null"]`) shipped for as long as that gap
  existed, in `cli_flags_12.0.json` through `cli_flags_18.0.json`.

None of these four is "data drifted from a real, checkable source" (ADR-0054's exact failure
mode). Each is a different way a curated artifact can go wrong with nothing in the repo able to
notice: an unverified ID borrowed from a real tool's namespace, two independently-curated facts
that say the same thing twice, a documented contract nobody honours, and a schema that exists but
is never actually run. ADR-0054 fixed "verify against source." This ADR states the smaller,
general rule that would have caught all four without requiring a new oracle for each one.

---

## Decision

**The minimum bar.** Every curated artifact added to this repo from now on (a `spec_data/*.json`
record, `patterns.json` entry, or a hand-curated table anywhere else, e.g.
`_DEPRECATED_API_SYMBOLS`) MUST carry at least ONE of the following, and whichever ADR/PR
introduces the artifact states which:

1. **An oracle** - a mechanical parser/check that can re-derive or verify the fact against a real
   source (ADR-0054's pattern: `framework_bases()` vs `parse_framework_bases()`), wired into a
   test that runs on real data, not merely defined and left uncalled (A7's exact failure).
2. **An explicitly recorded reason it cannot have one** - stated in the schema description or a
   code comment, not left to be inferred. Example already in this repo: `LINT_RULES_MIN_MAJOR`'s
   comment in `src/constants.py` states precisely why v8-v13 have no live pylint-odoo oracle
   (checker source absent or under a non-matching filename) rather than silently gating the
   version with no explanation.
3. **Provenance metadata** - when a fact borrows an ID/name from a real external namespace
   (a linter's rule-code space, a CLI's own flag names), the record states whether that ID is
   VERIFIED to be the real tool's own assignment (`rule_id_source: "upstream"`) or an
   OSM-invented ID living in the same namespace with no verified match
   (`rule_id_source: "osm-local"`), and, when `osm-local` is now KNOWN to collide with a real,
   differently-meaning ID, that collision is stated (`rule_id_collision`) rather than left latent.
   This is the B4 fix, generalized: `lint_rule.schema.json`'s `rule_id_source`/`rule_id_collision`
   properties are the reference implementation - a future family adding a similarly-shaped ID
   (e.g. a new lint-tool integration) should reuse the same two-field shape rather than invent a
   third.

**A schema-declared field with no reader is a defect, not a placeholder.** If a schema property
claims behavior ("shown in tool output", "used to X"), either that behavior is real and tested,
or the property is removed, or - when the reader is blocked by a real, temporary constraint (a
concurrently-edited file, per this issue's own S5 slice) - the gap is PINNED by a test that
currently passes by asserting the CURRENT (unwired) behavior and that will fail the moment the
gap closes, forcing an update rather than permanent silent drift. `tools_symbol.schema.json`'s
`note` field (C4) is the reference case: `tests/test_parser_tools_symbols.py::
TestNoteFieldPendingLoaderWiring` is the pin.

**A schema that is loaded must be validated, with the real library.** Every per-record
`spec_data/*.schema.json` file has a corresponding test that runs the actual `jsonschema`
library (`jsonschema.validators.validator_for(schema)`, not a hand-transcribed subset of the
schema's rules) against every real record, and that test is itself proven capable of failing
(a deliberately-broken record must trip it - see each family's
`test_validator_actually_rejects_a_broken_record`). A schema file that exists on disk but is
never run against real data is equivalent to no schema.

**Duplicate facts must be checked against each other, not just against their own schema.** A
family whose records can express the same real-world fact under two different keys (two
different `rule_id`s with identical `code_pattern`, per B5) needs a same-family duplicate guard,
not only a per-record schema check - `TestNoDuplicateCodePatternReporting`
(`tests/test_spec_data_lint_rules_curated.py`) is the reference test.

**Proportionality.** This is a small team; a standard nobody can meet is worse than none. The bar
is ONE of the four things above, not all four, and "an explicitly recorded reason" is always a
valid, cheap way to satisfy it when a real oracle is not feasible (see `LINT_RULES_MIN_MAJOR`'s
v8-v13 exclusion, or B5's v8.0-v13.0 unconsolidated `W8140`, both left as-is with a stated reason
rather than forced into an oracle that does not exist).

---

## Consequences

**Positive:**
- The four issue #364 findings (B4/B5/C4/A7) are fixed as concrete instances of this rule, not
  as four unrelated one-off patches - a future fifth instance of any of these shapes has a named
  pattern to follow instead of being re-diagnosed from scratch.
- `lint_rule.schema.json` now requires `rule_id_source` on every record (all 603 entries across
  12 versions + the `99.0` smoke sentinel populated in this pass) - a curated rule with a
  borrowed-looking ID can no longer be added without a provenance decision being made and
  recorded at write time.
- All three `spec_data/*.schema.json` files are now validated by the real `jsonschema` library,
  each proven able to fail (not just able to pass) via a `test_validator_actually_rejects_a_
  broken_record` test.

**Negative:**
- Two new required-ish fields on every `lint_rules_*.json` record (`rule_id_source`,
  `rule_id_collision`) is more curation overhead per future rule addition. Mitigated: the
  provenance decision is usually mechanical (matches a real tool's registry, or does not) and the
  schema's own description documents the two-field contract inline.
- The `note` field pin (`TestNoteFieldPendingLoaderWiring`) is a deliberately temporary
  compromise, not a finished fix - the loader gap it tracks should close as a natural extension of
  whichever change next touches `parser_tools_symbols.py`'s static loader, at which point the
  pinned test must be rewritten (its own docstring says so), not deleted.

**Risk:**
- `rule_id_source`/`rule_id_collision` were populated in this pass against the pip-installed
  `pylint-odoo` 10.0.7 package specifically (2026-07-21) for the general 69-id family, and
  separately against Odoo's own vendored `_odoo_checker_sql_injection.py` for `E8501` - these are
  TWO DIFFERENT real registries with overlapping but non-identical numbering (see
  `lint_rule.schema.json`'s `rule_id_source` description). A future re-verification pass against
  a newer `pylint-odoo` release could find the collision set has shifted (confirmed even across
  three installed 10.0.x builds on the same dev box: 56/58/58 `ODOO_MSGS` entries) -
  `TestRuleIdProvenance::test_known_collision_count_within_expected_range` pins a RANGE (15-25),
  not an exact count, for exactly this reason.

---

## Follow-up: index-core prune-on-full-write for LintRule / CLICommand / CLIFlag (issue #364 F2)

Removing a curated fact is a distinct failure mode from *changing* one, and the minimum bar above
does not address it: when #364 DELETED lint rule `W8140` from the v14-v19 curated sets, the next
`index-core` for those versions left a stale `LintRule {rule_id:'W8140'}` node behind forever,
because `write_lint_rules` / `write_cli_commands` / `write_cli_flags` are MERGE-only, version-keyed,
and never delete. Same latent class for CLICommand/CLIFlag.

**Rule:** `pipeline.index_core` (the SOLE caller of those three writers, and always writing the
FULL set for a version) runs an UNCONDITIONAL version-scoped prune immediately after each write,
against the live id/name/key set it just wrote - mirroring `prune_framework_test_helpers`
(ADR-0054). No `prune=` flag is needed because there is no partial caller (the CLI exposes no
family filter). Two mandatory safety guards, both mirroring existing precedent:
- **Empty-guard:** an empty live set NEVER deletes (a transient/degraded parse must not wipe a
  version) - same shape as the `write_pattern_examples` empty-guard.
- **Soft-drop gate:** if a single run would delete more than a large fraction
  (`_PRUNE_SOFT_DROP_MAX_FRACTION`, 50%) of a version's existing nodes, the prune is SKIPPED with a
  WARNING - mirroring the skip-and-warn guard of `gc_stale_modules` (removed by ADR-0056; its
  successor is the G-B mass-retire gate, 50% AND >= 20) and this ADR's sibling ADR-0005
  (">20% CoreSymbol drop = suspect path refactor"). This protects against a checkout that silently
  lost its source (e.g. `odoo/addons/test_lint/tests/`) before it can delete the whole version.

CLIFlag is keyed on the composite `(flag_name, command_name, odoo_version)`; the prune compares a
joined `flag_name|command_name` string so the same `flag_name` under different commands stays
distinct. `command_name` is never NULL in the stored graph (Neo4j MERGE rejects a null key
property; `parse_cli_flags` defaults it to the command name, `"server"` for global config flags),
so the `coalesce(command_name,'')` is defensive belt-and-suspenders, not a live path.

**CoreSymbol is EXEMPT - never add a `prune_core_symbols`.** Its cross-version lifecycle
(`added_in`/`removed_in`/`deprecated_in` properties + `REPLACED_BY` edges) is precisely what
`api_version_diff` / `find_deprecated_usage` / `lookup_core_api` consume; deleting a stale-version
CoreSymbol node would destroy that history. A stale WRONG CoreSymbol from a same-version *correction*
(not a lifecycle event - e.g. #364's `odoo.tools.pycompat` @ v8-v10 and `odoo.tools.image_process`
@ v19) is cleaned by a one-time reviewed Cypher (`ops/cleanup_stale_tools_symbols_364.cypher`), not
by a standing prune. A regression test (`test_no_prune_core_symbols_method_exists`) guards that no
`prune_core_symbols` method is ever added to `Neo4jWriter`.

---

## Alternatives Considered

**Alt 1: Require an oracle for every curated fact, no exceptions.** Rejected. B4's finding shows
this is not always possible - there is no mechanical way to verify that an OSM-invented rule ID
was never real; the only honest response is provenance metadata, not a fabricated oracle. Making
"oracle only" the sole acceptable answer would either block legitimate editorial curation (B3's
finding that ~84% of `lint_rules` content is editorial convention, not a source-derivable fact)
or push people to build a decorative pseudo-oracle just to satisfy the letter of the rule.

**Alt 2: A single repo-wide curated-data linter that enforces this ADR mechanically.** Deferred,
not rejected outright. The four fixes in this pass are heterogeneous enough (a schema field, a
data consolidation, a render wire-up, a validator library call) that a single generic linter
would need per-family plugins anyway, at which point it is not meaningfully cheaper than the
per-family tests this ADR requires (`TestRuleIdProvenance`, `TestJsonschemaLibraryValidation`,
`TestNoDuplicateCodePatternReporting`, `TestNoteFieldPendingLoaderWiring`). Revisit if a FOURTH
family is added and the same four test shapes need re-deriving a third time.

**Alt 3: Delete the dead `note` field outright instead of pinning the loader gap.** Considered for
C4. Rejected: `note` is populated on 204/204 curated entries with genuine lifecycle/misuse
content (e.g. "Context manager to silence specific loggers. Use only in tests."), and the only
reason it is unwired is a real, temporary, explicitly-scoped constraint (this issue's own S5 slice
was asked not to touch `parser_tools_symbols.py`, reserved for concurrent oracle work). Deleting
real curated content to satisfy a schema-honesty rule, when the actual blocker is a coordination
window rather than a design defect, would be destroying data to save face rather than to fix
anything. The pin makes the gap loud and trackable instead.

**Alt 4: Silently renumber the 19 colliding `pylint-odoo` rule_ids to non-colliding values.**
Rejected per this issue's own instruction: check for references first. `rule_id` is used as a
Neo4j MERGE key component and is externally visible via `lint_check` tool output; renumbering
would break any external reference or saved output without warning, for a cosmetic ID change that
does not fix the underlying fact (the MESSAGE, not the ID, is what matters to a reader; the
provenance field makes the ID's unreliability visible instead of pretending a fresh ID would be
any more "real").
