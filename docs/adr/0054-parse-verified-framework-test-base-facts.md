# ADR-0054: Parse-verified framework test-base facts (per-version, branch-HEAD)

**Status:** Accepted

**Date:** 2026-07-21

**Author:** Viindoo Engineering (issue #362)

**Relates to:** ADR-0051 (test-surface index), ADR-0052 (per-feature version dispatch),
ADR-0032 (`VersionRegistry`), ADR-0023 (tool output completeness), ADR-0029 (implicit
session context / version resolution), ADR-0034 (multi-tenant fail-closed choke-point),
ADR-0007 (incremental indexer)

---

## Context

`test_base_classes` returned the identical eleven-entry framework base-class menu for
every indexed Odoo version, v8 through v19, and several of its per-class claims were
wrong. Root cause: `seed_framework_helpers(odoo_version)` (`src/indexer/parser_test.py`,
pre-fix) used its `odoo_version` argument only to stamp the written node - it wrote all
eleven entries of a flat, version-blind `_FRAMEWORK_BASES` dict identically under every
version. The Cypher query and the ADR-0029 version resolver were already correct: this
was a correct query over uniformly wrong data. A second, independent copy -
`_static_framework_bases` / `_static_framework_bases_str` in `src/mcp/tools/test_tools.py`,
the empty-graph in-process fallback - carried only 4-5 classes and disagreed with the
graph's eleven at nearly every version, so the tool's output depended on index state
rather than being a function of the version alone.

Re-deriving the facts from all twelve real checkouts (`odoo8`..`odoo19` on this
machine) showed the shipped data was wrong on both directional ends and on
classification, not merely stale in one place:

- `SavepointCase` - shipped as "(deprecated alias, v8-v15)" at every version, including
  v19. Real: present v8-v16 (backported onto the already-released 8.0 branch in commit
  `11ba4689b1d9`, 2015-06-23), deprecated from v15 via a genuine `__init_subclass__` +
  `warnings.warn(DeprecationWarning)` (not an alias), REMOVED at v17.
- `TreeCase` - shipped as "(v14+)". Real window is v11-v14; the shipped claim is
  inverted and points an agent at a class absent from v15-v19.
- `BaseCase` - shipped as "v10+". Real: present since v8.
- `HttpCaseCommon` - shipped as available at all twelve versions. Real: exists at v14
  ONLY.
- `Form` / `O2MForm` - shipped as "v14+". Real: v12+, relocating from
  `odoo/tests/common.py` to `odoo/tests/form.py` at v17.

Only 4 of the 11 shipped entries were correct at all twelve versions - a defect class,
not three bad strings. `docs/data/patterns.json`'s `test-savepointcase-v8-v15` gotcha
text carried the same folklore (SavepointCase "alias", "v16+ merged both") and was
fixed in the same effort (743b7d4) for the same reason: leaving identical folklore
alive in a sibling tool is the duplicate-source-of-truth failure this ADR exists to
end.

`CONTRIBUTING.md:521-539` requires an ADR for a new parser convention or storage
pattern. Parse-verified curation (a curated table whose structural claims are
mechanically checked against source, with a drift alarm) is a new convention; the
writer's new framework-helper prune is a new writer behaviour. Both categories apply
here.

---

## Decision

**1. One source of truth.** All facts about Odoo test-framework base classes live in
exactly one module, `src/indexer/framework_bases.py`. Its aggregate dispatcher
`framework_bases(odoo_version, odoo_source_root=None)` (and the single-class
`framework_base()`) is the ONLY place either the graph seeder or the MCP read path may
get this data from. The two former duplicates are DELETED: `_FRAMEWORK_BASES`
(`src/indexer/parser_test.py`) and `_static_framework_bases` /
`_static_framework_bases_str` (`src/mcp/tools/test_tools.py`). The existing
classification sets `TEST_BASE_CLASSES` and `TEST_TYPE_MAP` (`src/indexer/parser_test.py`)
remain and are explicitly NOT the menu: a name can be classifiable (used to type-tag an
addon test class's own base) without being offered as a base an agent should write
against (`TestCase` is classifiable but is bare `unittest.TestCase` - it is never
bound as `TestCase` inside `odoo/tests/common.py` at any surveyed version, per a
repo-wide grep across all twelve checkouts, so it is retained in the classifier and
kept out of the file-path-bearing menu rows).

On the read side, `framework_bases(v)` is the AUTHORITY for menu composition and for
the `name=` drill-down - never the graph. `_test_base_classes`
(`src/mcp/tools/test_tools.py`) calls `framework_bases(v)` / `framework_base(v, name)`
to decide which classes exist and what they say, then joins graph rows ONLY to
overlay `file_path`/`line` by name (`_enrich_with_graph_locations`,
`src/mcp/test_render.py`). This collapses the former split-brain by construction:
there is exactly one function, in one module, that answers "which classes exist at
version V" for both the writer and the reader, and the graph can never contribute or
withhold a class from the menu. It also means the fix is correct for every already
deployed server the moment the code is deployed, before any reindex runs - the menu
and the drill-down never depended on graph state to be right; only `file_path`/`line`
enrichment does.

**2. Version dispatch per ADR-0052.** One feature-owned
`VersionRegistry[Callable[[], list[FrameworkBaseFacts]]]`
(`_FRAMEWORK_BASE_REGISTRY`) with seven boundary entries - `(8,9)`, `(10,10)`,
`(11,11)`, `(12,13)`, `(14,14)`, `(15,16)`, `(17,None)` - fanning out to seven per-era
handlers (`_era_v8_v9` .. `_era_v17_plus`) behind the one aggregate dispatcher
`framework_bases()`. No inline `if major >= N` comparison anywhere in this feature.
Adding v20 as its own era is a one-line registry append plus one new handler; if v20
needs no menu change, the existing open-ended `(17, None, _era_v17_plus)` entry
already covers it with zero edits. A SEPARATE one-boundary registry
(`_REMOVED_REGISTRY`) drives `removed_at()`; it is independent BY CONSTRUCTION of
`_FRAMEWORK_BASE_REGISTRY` so that a future v20 era appended to the menu registry can
never silently change what `removed_at()` reports (the previous design internally
tested object identity against the v17+ era handler - appending a v20 handler would
have broken that identity check and made `removed_at("20.0")` wrongly answer `[]`,
reproducing the "SavepointCase still looks available" defect one boundary up the
stack). A third one-boundary registry (`_ERA_PREFIX_REGISTRY`) resolves the
`openerp/` vs `odoo/` source prefix for the parse oracle, reusing
`ODOO_NAMESPACE_LEGACY_MAX_MAJOR` rather than a second hardcoded `9`.

**3. Parse-verified curation - not a curated table again.** The failure mode that
caused issue #362 was not "curated data" per se; it was UNDETECTABLE curation drift -
nothing in the repo could notice that `_FRAMEWORK_BASES` had stopped matching
`odoo/tests/common.py` as new majors shipped. This ADR's curated era tables are
therefore paired with an independent oracle, `parse_framework_bases()`: an AST walk of
the real `<prefix>/tests/common.py` (plus `<prefix>/tests/form.py` from v17, when
present - a pure existence check, not a second `major >= 17` branch) that extracts
class name, base chain, `setUpClass` presence, and a deprecation marker
(`__init_subclass__` calling `warnings.warn(..., DeprecationWarning)`). `ast.parse` is
tried first for every era; only on `SyntaxError` does it fall back to a text-regex
class-header scan (`_scan_classes_text`), reusing `parser_python_era1.py`'s
`_RE_CLASS_HEAD` primitive - the SAME try-AST-then-text-regex-on-`SyntaxError` shape
`parser_python.parse_file` already established (`parser_python.py:993-1013`), not a
new convention. The fallback exists because the real v8/v9 `openerp/tests/common.py`
contains a genuine Python-2-only `except select.error, e:` clause inside
`HttpCase.phantom_poll` that is fatal to a whole-file `ast.parse` on this project's
runtime Python, even though it has no bearing on any framework base class's own shape.

`tests/test_framework_bases_parity.py` diffs the curated table against this oracle on
every CI run (against committed, AST-faithful fixture excerpts) and, when available,
against the real checkouts on the dev box - and is required to run, never to skip,
because a skip-when-absent parity test is exactly the "nothing notices drift" failure
this ADR exists to close. This is what distinguishes a parse-VERIFIED curated table
from the artifact whose silent rot caused #362: the curated table is a cached
projection of source with a drift alarm wired to it, not an independent, unverifiable
belief. A parse may only PROMOTE a curated `'available'` status to `'deprecated'`
(when the oracle finds the `__init_subclass__` marker); it may never DOWNGRADE a
curated `'deprecated'` back to `'available'` on a mere parse miss - a defensive
asymmetry so an incomplete parse can only ever make the answer more cautious, never
less.

On the ordinary tenant profile-reindex path (`reconcile_test_surface`,
`src/indexer/pipeline.py`) there is no Odoo source checkout available at all - a
customer profile need not contain the Odoo core repo - so `framework_bases(v)` is
called with no `odoo_source_root` and returns the curated table's structural facts
plus its human-authored guidance prose unenriched. The oracle only runs where a real
checkout is already open: the `index-core` walk (`parser_odoo_core.py`, has a
`source_root` argument), which enriches `file_path`/`line` and can promote deprecation
status, but writes with `profiles=[]` (a pre-existing, unrelated constraint - see
Consequences) so its enrichment is only visible to a tenant once a later profile pass
unions a profile onto the same node.

**4. Auto-extend was deliberately DROPPED.** An earlier iteration of this module, when
given `odoo_source_root`, appended any parsed class with no curated match to the
returned menu ("v20 auto-extend" - letting a real checkout's new class surface before
the curated table was updated for it). This directly contradicts the prune's
profile-invariance invariant (Decision §6): the name SET `framework_bases()` returns
must never become a function of `odoo_source_root`, or one profile's reindex (with a
source checkout reachable) could compute a different `live_names` set than another
profile's reindex (without one) - and a real class the curated table does not know
about yet is not a member of `KNOWN_FRAMEWORK_BASE_NAMES` either, so the prune could
never delete it, a permanent graph-hygiene hole. Auto-extend is gone:
`framework_bases(v, source_root)` only ever ENRICHES entries the curated table for `v`
ALREADY has; a parsed class with no curated match is silently ignored by the
enrichment step (never written, never returned).

Consequence, stated as designed behavior rather than an oversight: when a future Odoo
v20 ships, `framework_bases('20.0')` keeps returning the v17+ era menu (out-of-catalogue
fail-open, Decision §5) until a human appends one era line, and
`tests/test_framework_bases_parity.py`'s dev-box layer goes RED against a real v20
checkout the moment one exists on this machine. That red IS the alarm doing its job -
the same "curated table quietly stops matching source" failure mode issue #362 exists
to kill, relocated one level up the stack rather than eliminated by an auto-extend that
would have silently suppressed exactly that alarm.

**5. Branch-HEAD semantics.** Every version window this feature asserts describes what
a `git clone -b X.0 <odoo-repo>` working tree contains TODAY, at that branch's current
HEAD - never a GA-tag snapshot frozen at release time. Odoo backports fixes and even
new test-framework helpers onto already-released stable branches. Concrete evidence:
`SavepointCase` was introduced by commit `11ba4689b1d9` ("[IMP] running speed of some
tests & new testcase type", 2015-06-23) and backported onto the already-released 8.0
branch - it did not exist when Odoo 8.0 first shipped in 2014, yet it is a real,
load-bearing base class on that branch today: `odoo8/addons/mail/tests/common.py:25`
defines `class TestMail(common.SavepointCase):`. OSM therefore asserts `SavepointCase`
is available at 8.0, matching the branch a real 8.0 checkout gives an agent working
against it today, not a hypothetical frozen-at-GA menu that would wrongly declare it
absent.

**6. Removal must be executable.** Before this ADR, nothing in `src/` could ever
delete a `TestHelper` node: `write_framework_test_helpers` is pure `MERGE`+`SET`,
`gc_stale_test_nodes` DETACH DELETEs `TestClass`/`TestMethod` only, and
`delete_module_subtree`'s per-module cascade explicitly enumerates its labels
(`Model, Field, Method, View, QWebTmpl, Report, JSPatch, OWLComp`) and excludes
`TestHelper` (2026-09-24, ADR-0056: replaced by `retire_modules`, whose
`MODULE_CHILD_LABELS` cascade does delete ADDON TestHelpers but still never
`module='@framework'` ones - `prune_framework_test_helpers` stays their only
delete path); framework helpers also carry no `DEFINED_IN` edge, so no `Module`
delete can reach them by cascade either. A class removed from an era (SavepointCase
leaving the menu at v17+) would therefore survive on every already-indexed server
forever, and a code-only deploy could never fix an already-populated graph. The writer
gains `prune_framework_test_helpers(odoo_version, live_names)`
(`src/indexer/writer_neo4j.py`), wired into `reconcile_test_surface` immediately after
seeding, with two safety properties that MUST hold:

- **No ping-pong.** Deletion is additionally bounded by
  `th.name IN $known_universe` (`KNOWN_FRAMEWORK_BASE_NAMES`, the set of every name
  this feature has EVER emitted at any era) - a source-less profile run can therefore
  never delete a class a source-bearing `index-core` parse discovered and the curated
  table does not know about yet; without this bound the source-bearing and
  source-less paths would alternately create and delete the same node on every
  reindex.
- **Profile-invariant.** `live_names` is `{fact.name for fact in framework_bases(rv)}`
  - a pure function of the version alone, never of a source root or a profile - so
  one profile's prune can never delete a node another profile's reindex still needs.
  This is why, unlike `gc_stale_test_nodes`, this method needs no `repo`/profile
  scoping parameter at all.

**7. Out-of-catalogue policy.** The surveyed catalogue is majors 8..19. A resolved
major above it (including the `99.0` test-fixture sentinel), below it, or unparseable,
resolves to the newest known era (v17+) via `VersionRegistry`'s open-ended
`(17, None, ...)` entry AND the rendered menu carries an explicit provenance line
naming the substitution (`is_out_of_catalogue()`): `"Note: Odoo {v} is outside the
surveyed catalogue (v8-v19); showing the newest known menu (v17+)."` An empty menu is
never returned - fail-open-and-say-so, never fail-open-silently, and never
fail-closed. No test sentinel (`99.0` or otherwise) is special-cased anywhere in
production code; `is_out_of_catalogue()` is a pure range check with no literal
knowledge of `99.0`, deliberately diverging from the repo's own existing per-version
`spec_data/*_99.0.json` sentinel-file convention (`src/indexer/spec_data/`) because
that convention only works because those artifacts are version-keyed one-file-per-version
already, a shape this feature's in-process module is not.

**8. Absence is a first-class answer.** When `name=` names a class that does not exist
at the resolved version, `test_base_classes` renders an explicit `NOT AVAILABLE`
branch (`_format_base_class_not_available`, `src/mcp/test_render.py`) stating the real
window the class existed in, its deprecation point (when applicable), its removal
point, and its replacement (from `removed_at(v)`, the same single source of truth the
menu's own "Removed as of..." line uses) - or, for a name that was never a known
framework base at all, a distinct "not a known Odoo framework base class" line. It
NEVER falls back to rendering the full menu. Before this ADR the drill-down fell
through to `_static_framework_bases_str(v)` on any miss, printing the entire menu
instead of answering the one question actually asked - the single most useful answer
this tool could give (does class X exist here, and if not, what do I use instead) was
unreachable by construction.

**9. Schema.** Zero migration. The `TestHelper` MERGE key `(name, module, odoo_version)`
is unchanged. The non-key properties `file_path` and `line` change from
always-null (every framework helper was seeded with `file_path=None`, `line=None`
before this fix) to sometimes-populated, and are written with coalesce-ON-MATCH
(`th.file_path = coalesce($file_path, th.file_path)`, `th.line = coalesce($line, th.line)`
- the same pattern the 0.18.0 module identity-card work established for
`shortdesc`/`author`) so a source-less profile reseed can never wipe enrichment an
earlier `index-core` run wrote. Tool/resource surface unchanged at 31 tools / 9
resources.

---

## Consequences

**Positive:**

- Exactly one function answers "which framework test-base classes exist at Odoo
  version V" for both the write path and the read path; the class of bug issue #362
  reports (two disagreeing copies) cannot recur by construction, because there is no
  longer a second copy to disagree.
- The menu and the drill-down are correct on deploy, on every already-indexed server,
  before any reindex - they no longer read from the graph at all for correctness, only
  for optional `file_path`/`line` enrichment.
- A future curation error is DETECTABLE: `tests/test_framework_bases_parity.py` fails
  the moment the curated table disagrees with a real parse, on CI (committed fixtures)
  and on a dev box with real checkouts.
- Stale nodes are no longer permanent: `prune_framework_test_helpers` gives the writer
  a genuine removal path for a node class that had none before.
- v20 support is a one-line registry append plus one new era handler, matching
  ADR-0052's convention exactly.

**Negative:**

- Two independent one-boundary registries now exist purely for this feature
  (`_FRAMEWORK_BASE_REGISTRY`, `_REMOVED_REGISTRY`) plus a third for the source
  prefix (`_ERA_PREFIX_REGISTRY`) - by design (ADR-0052: boundaries are per-feature,
  not shared), but it means three small registries to keep straight when reading this
  one file, versus one.
- The AST/text-regex oracle only ever runs on the `index-core` path (a real checkout).
  The ordinary tenant profile-reindex path has no source root and is therefore
  100% curated, unverified-at-runtime data for that pass - correctness there rests on
  the parity test having been green at the last CI run and dev-box check, not on a
  live parse of that exact server's data.

**Risk:**

- Framework helper `file_path`/`line` enrichment is only ever written by `index-core`,
  and only reaches a tenant once a subsequent profile pass unions that tenant's
  profile onto the already-seeded node (`index-core` itself seeds with `profiles=[]`,
  a pre-existing, unrelated ADR-0034 constraint - see the reindex runbook's ordering
  note). An operator who runs `index-core` and never runs a profile pass afterward
  sees enrichment that is written but invisible to every scoped tenant.
- `KNOWN_FRAMEWORK_BASE_NAMES` is a second, manually-maintained list (distinct from
  the per-era curated tables) that bounds the prune. Forgetting to add a genuinely new
  framework base name to it when a future era gains one would make that name
  unprunable if it is later removed again - the same "no path to deletion" defect this
  ADR fixes, reintroduced for one future name. Mitigated by `test_framework_bases_parity.py`
  covering the full oracle-vs-curated diff, which would flag the new name as an
  uncurated parse result the moment a real checkout exists.

---

## Alternatives Considered

**Alt 1: A pure-parse design (no curated table at all).** Rejected: the profile-reindex
call site (`reconcile_test_surface`) has no `odoo_source_root` - a customer profile
need not contain an Odoo core checkout - so a pure-parse design cannot seed on the path
that must succeed for `INHERITS_TEST` edges from addon test classes to resolve against
framework bases. A curated table is required at minimum as the source-less fallback;
this ADR keeps it as the PRIMARY answer everywhere and layers the parse oracle on top
as a verification/enrichment mechanism instead, rather than trying to make parsing
load-bearing on a path where a checkout may not exist.

**Alt 2: Render the graph rows verbatim, as before, and rely solely on the new prune to
fix production.** Considered and rejected during design review. Under this approach the
tool's correctness on any already-indexed server would depend on an operator running a
reindex before the fix takes effect - so a code-only deploy would ship a fix that does
not fix anything until an out-of-band operational step runs. Making `framework_bases(v)`
the read-side authority (Decision §1) and using the graph only for optional enrichment
means the menu and the drill-down are correct immediately at deploy, and the prune
becomes a graph-hygiene concern (needed for `test_class_inspect` and for not leaving a
permanently wrong node lying around) rather than the mechanism the headline fix depends
on.

**Alt 3: Auto-extend the menu from a real checkout's parse when one is available
(v20 auto-extend).** Rejected (Decision §4): it makes the returned name set a function
of `odoo_source_root`, which breaks the prune's profile-invariance guarantee and can
create a permanently un-prunable node. A missing v20 era is meant to be loud (a RED
parity test) so a human appends one era line, not silently patched over by whichever
profile happens to have a source checkout on hand.

**Alt 4: Widen `patterns.json`'s `test-savepointcase-v8-v15` `odoo_version_max` to
`"16.0"`** because the class is technically still importable and subclassable at v16.
Rejected: subclassing it at v16 emits a real `DeprecationWarning`
(`odoo16/odoo/tests/common.py:826-833`), and the sibling pattern
`test-transaction-savepoint-v16plus` already covers v16 correctly with the
non-deprecated replacement. Widening would make `suggest_pattern(odoo_version='16.0')`
offer both the correct modern pattern and a deprecated one; only the gotcha TEXT was
corrected (743b7d4), the version window was left at `"15.0"`.

**Alt 5: Special-case the `99.0` test-fixture sentinel in production code**, mirroring
the repo's existing `spec_data/*_99.0.json` per-version data-file convention. Rejected:
that convention works specifically because those artifacts are already version-keyed
one file per version; this feature is a single in-process module, and the general
out-of-catalogue policy (Decision §7 - fail open to the newest era, labelled) already
covers `99.0` correctly as an ordinary case of "major outside the surveyed range",
with no test-only branch needed.

---

## Amendment 2026-07-21 (issue #364 - the pattern applied to three more families, and where it doesn't fit)

Issue #362 (this ADR) fixed exactly one curated artifact: `_FRAMEWORK_BASES`. Issue #364 asked
whether the same failure mode - a curated table nothing in the repo could notice drifting from
source - existed anywhere else. It did, in `cli_flags`, `lint_rules`, and `tools_symbols`
(`src/indexer/spec_data/*.json`), plus a fourth artifact outside `spec_data/` entirely
(`_DEPRECATED_API_SYMBOLS`, `src/indexer/parser_python.py`). This amendment records which of
those four actually adopted THIS ADR's pattern (a real AST/text oracle plus a parity test that
fails on a value mismatch, not merely a missing entry), where an oracle was found infeasible
by construction rather than merely unbuilt, and the version-boundary facts the audit surfaced
along the way. `docs/adr/0055-curated-data-minimum-bar.md` generalizes the bar this amendment's
findings forced into existence: an oracle is ONE of three acceptable answers, not the only one.

**Full oracle adoption (this ADR's exact shape).** `cli_flags` and `tools_symbols` both now have
a live AST-first / text-regex-on-`SyntaxError` oracle wired to a content-parity test that compares
field VALUES, not just presence - `parser_cli.py::parse_cli_flags` /
`tests/test_cli_flags_content_parity.py`, and the newly-built `parser_tools_symbols.py::
load_tools_symbols` / `tests/test_tools_symbols_content_parity.py`. Both reuse the identical
two-tier dispatch this ADR established for `framework_bases.py` and `parser_python.parse_file`
(`parser_python.py:993-1013`) - a third bespoke recovery mechanism was deliberately NOT invented
for either.

**Partial adoption, by design.** `lint_rules` has a live oracle (`parser_lint_rules.py`) for the
FALSIFIABLE slice of its content only - version-boundary claims about when a real pylint-odoo
checker/eslintrc/ruff.toml source construct appeared or changed. The remaining ~84% of the 603
curated `lint_rules_*.json` records are EDITORIAL conventions (naming/style recommendations with
no source construct to parse against, ever) - this is a PERMANENT property of that content, not a
gap this or a future PR closes. Building a parity test against nonexistent source would be a
decorative pseudo-oracle, exactly what ADR-0055's Alternative 1 rejects; the correct instrument
for the editorial majority is a classification guard (`test_lint_rules_content_parity.py::
test_an_editorial_rule_is_not_misclassified_as_falsifiable` and its sibling) plus ADR-0055's
"recorded reason" bar, not a fabricated diff against source.

**Deliberately lighter than a full oracle.** `_DEPRECATED_API_SYMBOLS` was restructured from a
trailing-comment claim (untestable by construction - this is precisely how it rotted) into a
`DeprecatedApiSymbol` dataclass with a `since_version` field, backed by
`tests/test_deprecated_api_symbols_parity.py`: a CI layer pinned against tiny hand-captured real-
source snippets for every one of the 25 entries, plus a dev-box layer that re-derives the same
facts from real checkouts when present. This is NOT a `framework_bases.py`-shaped production
oracle, and the module's own docstring states why: no production code reads `since_version` today
(the runtime check is membership-only; the real version-gating for the `USES_CORE_SYMBOL` edge
happens downstream against the matching `CoreSymbol` node's own `status`), so building a full
parser oracle for a fact nothing consumes at runtime would be effort spent for its own sake. A
pinned, falsifiable data structure satisfies ADR-0055's bar without over-building.

**Boundary fact 1 - the cli oracle's real Python-2 ceiling is v10->v11, one version past the
namespace split.** `parser_cli.py`'s AST walk had ALWAYS silently returned zero flags at v8, v9,
and v10 - a permanent `SyntaxError` on real `tools/config.py`'s `os.chmod(self.rcfile, 0600)`, a
Python-2-only leading-zero octal literal. Odoo itself did not fix this to `0o600` until the
v10->v11 boundary (`odoo10/odoo/tools/config.py:559` still reads `0600`;
`odoo11/odoo/tools/config.py:583` reads `0o600`) - ONE VERSION LATER than
`ODOO_NAMESPACE_LEGACY_MAX_MAJOR` (the `openerp`/`odoo` package-prefix split at v9->v10, which
this ADR's own `_ERA_PREFIX_REGISTRY` already tracks separately for framework test bases). The
Python-3-AST-parseability boundary and the namespace-rename boundary are different upstream facts
that happen to sit one version apart; `parser_cli.py`'s module docstring now states this
explicitly so the two are never conflated again. Recovery uses the same fallback tier this ADR
established, never a special case for the one known `0600` literal - the fallback degrades
per-call and is robust to Python-2 syntax anywhere else in the file.

**Boundary fact 2 - the lint gate's real v14 ceiling, and why v11-v13 stay excluded even though
real checker content exists there.** `LINT_RULES_MIN_MAJOR` moved from 17 to 14 (`src/constants.py`),
not to 13. Direct inspection of v11-v13 shows a real, actively-wired checker
(`_odoo_checkers.py`, rule E3110/`no-comma-exception`) with a genuine `msgs = {...}` dict - but its
filename does not match this parser's `_odoo_checker_*.py` glob (no separating underscore between
"checker" and the suffix). v13 additionally carries `_odoo_checker_sql_injection.py` (E8501),
which DOES match the glob. Gating v13 on alone would therefore recover E8501 while silently
dropping E3110 sitting right next to it in the same directory - a partial extraction that
UNDER-REPORTS is judged worse than an honest exclusion (the same "recorded reason" bar ADR-0055
codifies), so v11-v13 stay out of the live gate with this exact reasoning recorded inline in
`LINT_RULES_MIN_MAJOR`'s own comment, right alongside v8-v10 (no checker source at all). From v14
onward, checker filenames stabilize on the matching `_odoo_checker_<topic>.py` pattern and every
major v14-v19 yields at least one live-extracted rule with zero false gaps - the real ceiling this
ADR's oracle pattern can reach for `lint_rules` is v14, not the v17 the code previously assumed
nor the v13 an earlier audit pass guessed from an incomplete look at the glob alone.

See `docs/adr/0055-curated-data-minimum-bar.md` for the generalized minimum-bar decision this
amendment's findings fed into, and the CHANGELOG `[Unreleased]` entry for issue #364 for the full
list of corrected facts and record counts.
