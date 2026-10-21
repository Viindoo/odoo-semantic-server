# ADR-0057: Source-oracle lifecycle prune - delete what git proves gone

**Status:** Accepted

**Date:** 2026-10-20

**Author:** Viindoo Engineering

**Amends:** ADR-0056 D6 (soft prune gate row, the `--allow-mass-retire` paragraph for the
entity prune), D9 (skip list, "Embeddings follow the graph"), D11 (`lifecycle-audit` schema
`/4`, `held_prunes` -> `parser_gaps`), Known limits.
**Phase 2 (2026-10-20, below):** also amends ADR-0056 D4 (pending names), D6 (every G-B row,
the `--allow-mass-retire` paragraph, exit 3), D8 (orphan sweep), D10 (shared-module prune),
D11 (schema `/5`) and Known limits (registered repo without a checkout).

---

## Context

Owner requirement: "Nobody approves these. If code is deleted, its graph AND embedding data
must be deleted too. OSM reflects the real state of the code."

Since 2026-10-17 the nightly `index-repo --all` exited 3 every night. ADR-0056 B14/D9 pruned
every child of a re-parsed module the parse no longer produced, but held the prune when more
than 50% (and at least 20) of the module's nodes or relationships would go (soft gate
`entity_prune:<M>@<v>`), until an operator re-ran with `--allow-mass-retire`. Three modules at
17.0 tripped it (`viin_freight_mixed_container_patch1`, `viin_ai`, `viin_ai_voice`); their
ghosts kept being served by the MCP tools, and every night re-parsed and held them again
(`soft_gate` was a `needs_rewrite` retry reason).

Investigation (read-only, prod, 2026-10-20) found:

- The parse is no evidence of a deletion: a parser gap drops entities the source still
  defines exactly like a commit that deletes them. `viin_ai@17.0` commit `8ee88bae6` turned
  `_name = 'viin.ai.ui.introspect'` into `_name = INTROSPECT_SERVICE`; the parser could not
  read the constant (fixed in the same release by `parser_python_consts`), so the model and
  76 methods vanished from the parse. The prescribed
  `--allow-mass-retire` would have deleted a live model.
- Everything else held was git-proven: 2134 of 2214 stale `viin_ai` nodes, all 120 of
  `viin_ai_voice`, 63 of 65 of freight patch1 are absent from the HEAD tree.
- The census ran before the ungated test-node GC (`_gc_stale_test_nodes`), so it counted the
  test nodes of deleted test files that the GC then deleted in the same run - inflating the
  ratio (`viin_ai` was 33% without them) - and the GC deleted graph nodes without their
  embedding rows (`viin_freight_mixed_container_patch1@17.0`: 101 ghost `test_class` /
  `test_method` rows served by `find_test_examples`, zero test nodes).

## Decision

### D1 - A source oracle judges every candidate (`src/indexer/prune_evidence.py`)

For each module the prune would touch, the stale candidates (`Neo4jWriter.module_stale_children`:
nodes and relationships the run did not re-stamp, same selection as the census) are judged
against the module's files **at the trusted HEAD tree, read from the git object database**
(`git ls-tree` + `git cat-file --batch`), never through the OSM parsers:

| Verdict | When | Effect |
|---|---|---|
| `gone` | the identifier no longer occurs in a form that can define it | deleted, with its relationships and embedding rows |
| `superseded` | still mentioned, and a live node of the same label and the same identity key minus module / file was written by this run in the same module (a test class moved to another file); or derived from a live parent the run re-derived (a lint violation of a re-validated view; a same-name `INHERITS` edge the post-pass re-adds) | deleted |
| `present` | the source still defines it, nothing replaced it: a **parser gap** | kept, with its embedding rows |

Defining forms (each broader than what the parsers accept; `.po`/`.pot` and binary blobs are
not read):

| Label | `present` when the module's HEAD text has |
|---|---|
| Model | the name as a whole dotted name anywhere (`'sale.order'` is not found in `'sale.order.line'`); covers constants, XML, comments. Also present while any class of the module has a `_name` / `_inherit` the oracle cannot evaluate (below) |
| Field | in a `.py`: `name` followed by `=` or `:` (assignment, annotation, parenthesized target, keyword, backslash / newline continuation), or `'name':` (era1 `_columns`) - AND, in a file Python 3 parses, declared in the body of a class that can define it on the node's model (below) |
| Method | in a `.py`: `def name` (also `async def`, continuation) - AND, in a file Python 3 parses, defined in the body of a class that can define it on the node's model |
| TestClass / TestHelper | in a `.py`: `class Name` |
| TestMethod | its class in a `.py` file that also has `def name` |
| View / QWebTmpl / Report | the xmlid's local part as a token anywhere (`id=`, `t-name=`, `<field name="key">`, qualified ids) |
| OWLComp | `class Name` anywhere |
| JSPatch | its file in the tree with an `extend(` / `include(` / `patch(` call and its target |
| Stylesheet / JsTestSuite | its file in the tree |
| LintViolation (derived) | its file in the tree and its view's xmlid local part as a token (superseded instead when the view was re-validated this run) |

**Class identity and class scope.** The oracle parses the module's Python files once with the
standard-library `ast` (still not the OSM parsers) and evaluates each class's `_name` /
`_inherit` statically: string literals, tuples / lists of them, and names bound to them in the
class body, at module level of the same file, or in another file of the same addon (relative
imports that stay inside the addon, `odoo.addons.<module>` / `openerp.addons.<module>`, star
imports; every binding of a name counts). Anything else - a constant imported from another
addon, a call, an f-string, an attribute, a concatenation - makes the class **unevaluable**:
it may define any model, so while one exists no Model node and no `INHERITS` / `DELEGATES_TO`
relationship of the module is proven gone. A class's models are its evaluated identity; a
class without `_name` / `_inherit` takes those of its local Python bases (Odoo reads `_name`
through the MRO), and a class's members also count for every local class deriving from it.
A class with no identity at all, or an unevaluable one in that family, can define a member on
any model. A file Python 3 cannot parse (v8-v10 Python 2 source) has no class scope: there the
text rule alone decides, and a non-literal `_name` / `_inherit` found by text makes the
module's models unprovable the same way.

A path under a previous (pre path-drift) module directory that git proves HEAD does not have
(no tree there, no gitlink above it - e.g. an untracked copy the module was once indexed
from) counts as "not in the tree"; so does any other path outside the module directory
at which git proves the registered repo's HEAD has nothing (no blob, no tree, no gitlink
above it - rule R8: a file of the copy in the repo the module moved from);
every other path outside the module directory is undecidable. A relationship into a node
the prune deletes goes with that node.

Relationships (stale ones of surviving children): `DEPENDS_ON` - the target name in the
manifest; `INHERITS` / `DELEGATES_TO` - the target model in a `.py` (`DELEGATES_TO` also needs
an `_inherits`), or an unevaluable class identity in the module; `USES_FIELD` / `DEPENDS_ON_FIELD` / `USES_CORE_SYMBOL` - the target token inside
the method's own definition and decorators; `IMPORTS` - the target stem in the importing file;
xmlid / bundle targets as whole dotted names (local part for the module's own namespace),
by the end node's xmlid or, for a record-form QWeb view, its `record_xmlid` (the id an
extender's `inherit_id` names); otherwise the target's last name segment as a token.

**One-directional:** the oracle may wrongly answer `present` (a ghost is kept and reported)
but must never wrongly answer `gone`. Every doubt is `present`: an unknown label or
relationship type, a path that cannot be mapped into the module and that HEAD may still
have (R8), a git error, a gitlink
(submodule) inside the module directory, a module directory whose HEAD tree lacks its
manifest, a class identity the oracle cannot evaluate. A module inside a submodule is read
from that submodule's own HEAD.

Superseded is deliberately narrow (owner decision): the twin must have the same label AND
the same identity key minus module / file AND live in the same module, written by this run.
A twin in another module is no evidence - every Odoo extension redefines fields and methods
with the same `(name, model)` key - so a twin elsewhere never makes a node `superseded`. A
field moved to another model is judged by the class scope instead: once no class of the
module that can define it on the node's model declares it, the node is `gone`
(`viin_freight_mixed_container_patch1@17.0` `package_type_id`); in a Python 2 file, where
only the text rule applies, it is kept (reported as a parser gap).

### D2 - Partial prune per module, no threshold, no approval

`pipeline_repo._prune_reparsed_modules` deletes exactly the `gone` + `superseded` ids
(`prune_module_children(only_ids=, only_rel_ids=)`; the selection and the stale predicate still
apply, so a concurrent re-write is never deleted) and keeps the rest. The 50% / 20 ratio is
an informational log line only. `PRUNE_SKIP_SOFT_GATE` and the `entity_prune:<M>@<v>` gate of
the per-repo prune are gone, `soft_gate` is no `needs_rewrite` retry reason, and
`--allow-mass-retire` no longer affects this prune (it still bypasses the repo-level gates
and the shared-module prune gate of ADR-0056 D10, which are phase 2).

### D3 - Parser gaps: recorded, reported, re-evaluated only on change

A kept set is recorded on the Module node (`parser_gap_*`: counts, a sample of up to 10
identities, the module directories and a fingerprint = module HEAD tree id(s) + a digest of
the parsers, writers and oracle (`prune_evidence.prune_code_token`)), logged once (WARNING),
and reported by `lifecycle-audit` as the `parser_gaps` finding (schema `osm.lifecycle-audit/4`,
renamed from `held_prunes`). It causes **no exit 3 and no `lifecycle_attention`**. The module is
re-parsed for the record only when the fingerprint changes (`pipeline_repo._parser_gap_state`,
checked before the unchanged skip): a commit to the module, or a deploy that changes the
indexer code (a parser fix). A prune that keeps nothing clears the record (and any legacy
`prune_held_*` record).

### D4 - Embeddings follow, in every case

- Pruned or partially pruned module: the owning profile's rows whose chunk key the parse did
  not produce are deleted except the rows of kept entities
  (`writer_pgvector.kept_entity_chunk_filters`, the SSOT next to the chunk builders;
  `delete_module_embeddings_except(also_keep=)`).
- The test-node GC no longer touches the file level of a module the prune judged, nor of a
  degraded module: the prune owns those test nodes (deleted with their rows when git proves
  them gone), so the census counts exactly what the prune decides. The GC keeps covering the
  modules whose prune is skipped (shared / unsynced owner / no Module node / machinery
  unavailable); their rows follow through the shared-module prune or the pre-B14 replace.

### D5 - Reusable for phase 2

`prune_evidence` imports only the standard library (it must stay independent of the parsers
it checks) and exposes `load_module_source`, `classify`, `module_tree_fingerprint` and
`prune_code_token`, so the repo-level gates (`mass_retire`, `total_wipe`, orphan and
embedding sweeps) and the shared-module prune can adopt the same rule later (PR-4).

## Consequences

- The nightly exits 0 for git-proven drops of any size; nobody approves an entity prune.
- First nightly after deploy (the three held modules carry `needs_rewrite`): freight patch1,
  `viin_ai`, `viin_ai_voice` @17.0 lose every git-proven ghost with its embeddings. Measured
  read-only on the 2026-10-20 stale set with the release's parser and oracle: the parser
  re-produces the `viin.ai.ui.introspect` model and its 76 methods (no longer stale), all 120
  of `viin_ai_voice` and all 65 of freight patch1 are `gone`, and one `viin_ai` test method
  stays, reported as `parser_gaps`.
- A Model whose name survives somewhere in the module text (comment, docstring, XML), and a
  field / method still declared in text of a Python 2 file, are kept and reported instead of
  deleted - the safe direction. In files Python 3 parses, a field / method left only in a
  comment or under another model is `gone`.
- No migration: everything new is a Neo4j Module property or code.

## Verification

- Default lane (no DB, seconds): `tests/test_prune_evidence.py` (every identifier form, the
  dangerous cases - cross-addon constant `_inherit`, call / f-string / attribute `_name`, a
  Python 2 constant `_name`, a field moved between models of the module, a record-form QWeb
  extension - and a property test that every entity the REAL parsers produce from
  unusual-but-legal fixtures - v17 and v8 Python 2 - is `present`; each rule proven by
  breaking it), `tests/test_entity_prune_oracle_unit.py` (pipeline glue: no gate, partial
  prune, kept rows, record / clear, re-evaluation trigger, GC scope).
- Opt-in: `pytest -m odoo_source tests/test_prune_evidence_odoo_source.py` samples real
  modules of the newest available checkouts.
- Real data: the nightly oracle judges every stale entity of every re-parsed module in
  production; what it keeps is the `parser_gaps` finding of the weekly audit - either a real
  parser gap or a pattern that is too broad, never a deletion.

## Known limits

- A model identity the oracle cannot evaluate (a `_name` constant imported from another
  module, a call, an f-string) proves nothing: the module's Model nodes and inheritance edges
  are all kept while such a class exists (ghosts among them are visible in `parser_gaps`).
  Other identifiers built at runtime (`'sale.' + suffix` outside `_name` / `_inherit`) or
  defined only outside the module's own files cannot be found: if the parser stops
  producing such an entity it is judged `gone`.
- Kept ghosts (a model name that survives only in a comment, a member still in the text of a
  Python 2 file) stay until the text goes; they are visible in `parser_gaps`.
- (Closed by phase 2.) The shared-module prune and the repo-level gates used the G-B ratio
  and `--allow-mass-retire`.

---

## Phase 2 - every remaining lifecycle deletion is decided by git (2026-10-20)

**Context.** After phase 1 the only lifecycle deletions still held for a human were the
repo-level `mass_retire` / `total_wipe` gates, the orphan sweep and the orphan-embedding sweep
(`orphan_sweep:<gate>`, `embedding_sweep:<gate>`) and the shared-module prune
(`entity_prune:<M>@<v>`), all behind the same 50% / 20 ratio and `--allow-mass-retire`. A
ratio is no evidence either way: a commit that deletes 22 of 25 modules was held, a dispatch
regression that hides 5 of 100 was not. Same owner rule: delete what git proves gone, keep
what git cannot prove; one-directional; keeping is not an alert.

### P1 - Repo-level drops (`lifecycle.apply_gates`, `pipeline_repo.observe_lifecycle`)

The evidence is the raw HEAD tree of the G-A-trusted checkout
(`git_utils.head_tree_module_names`): every directory holding a `__manifest__.py` or
`__openerp__.py` in `git ls-tree -r HEAD`, from the object database (never the working tree
or the index), with no version dispatch (it must not depend on what the scan accepts), a
submodule read at the commit its gitlink records. Git unable to list it (not a repository,
unborn HEAD, an unreadable submodule) proves nothing.

| Drop | Verdict | Effect |
|---|---|---|
| ledger row present -> absent, name not in the HEAD tree | proven | flagged `retire_pending` unblocked, whatever the count; the reconcile retires it (D4/H5 unchanged) |
| ledger row present -> absent, name still in the HEAD tree | unproven | not flagged: the row stays `present` (so neither the reconcile nor the sweep removes the node); logged; `lifecycle-audit` finding `unproven_drops`; no attention, no exit 3; presence may sync |
| present -> `excluded(unparseable)` | kept by the `manifest_unparseable` rule (unchanged, exit 3) | - |
| graph baseline (`presence_head_sha` NULL) pair not in the scan | proven when the name is not in the HEAD tree or the scan keys it at another version (re-key); unproven otherwise | the node becomes an orphan; the sweep judges it again (P3) |

**Total wipe** (modules were present, none is now): retired only when every drop is proven
AND the HEAD the ledger last reflected (`presence_head_sha`, else `repos.head_sha`) is an
ancestor of HEAD (`git merge-base --is-ancestor`): the wipe is a commit of the branch's own
history. Otherwise (a manifest still in the tree, the tree unreadable, no recorded base, a
non-ancestor base: force-push, branch swap, wrong clone) gate `total_wipe` trips: nothing of
the repo retires, presence is not synced, exit 3 - a checkout OSM cannot trust is exactly
"OSM cannot reflect this code". The `mass_retire` ratio (`mass_gate_trips`) is informational.

### P2 - Shared-module prune (`reconcile._Reconciler.prune_shared`)

The stale candidates (cutoff mode of `module_stale_children`) are judged by the D1 oracle
against EACH present owner's HEAD copy (`repos.local_path` + ledger `path`; a file path is
re-anchored into each owner's module directory first). A candidate is deleted only when every
owner's verdict is `gone` / `superseded`; the rest is kept with its embedding rows
(`shared_module_stale_embeddings(also_keep=)`) and recorded on the Module node as a parser gap
under the first owner (its run re-parses the module when that owner's tree or the indexer
code changes, which re-opens the evaluation). An owner whose checkout is missing proves
nothing. The evaluated state recorded on the Module is the owners' parse records plus
`prune_code_token()`, so a kept set is never re-judged on an unchanged night (the old held
module was re-evaluated every run and exited 3 every night).

### P3 - Orphan and embedding sweeps (`reconcile._Reconciler.sweep`, `_embedding_orphans`)

- Orphan Module: swept when no checkout of a profile that owns it (its `profile[]` and the
  repo of its `repo_id`) at the version ships a manifest of its name in the raw HEAD tree
  (`_head_shipped_names`, version rule applied: a repo placed at another version ships
  nothing here). A name the ledger has `excluded` (`installable_false` / `license_skip`) is
  a positive observation and is swept although its manifest is tracked (ADR-0056 D8,
  "exclusions leave through the sweep"; the embedding sweep likewise). Kept otherwise
  (`orphans_kept`, a missing / unreadable checkout keeps).
  Children of a module-less name: swept when no checkout at the version ships the name
  (`child_orphans_kept` otherwise). Deferral for unsynced repos is unchanged.
- Orphan embedding group `(module, profile)`: precondition - the graph is healthy (every
  module a synced profile has `present` keeps its Module node). Unhealthy -> every group is
  held, gate `embedding_sweep:graph_unhealthy`, exit 3 (OSM lost nodes of live modules and
  the unchanged skip does not re-write them; the attention names the profile whose `--full`
  run re-writes them). Healthy -> a group goes when no checkout of its profile ships the
  module (`embedding_orphans_kept` otherwise).

### P4 - What still exits 3, and the bypass

Exit 3 is kept for failures that stop OSM reflecting code and that it cannot self-heal:
G-A, `total_wipe` (P1), `manifest_unparseable`, `embedding_sweep:graph_unhealthy`,
`undecidable`, reconcile errors, and a registered repo without a checkout the run could not
clone (`unavailable_repos`, issue #383 item 2: the run now clones a repo whose
`clone_status` is `pending` / `error`, or `manual` at the clone directory OSM manages,
through the cloner's core, `src/clone_ops.py`, under the same per-repo lock and SSH key
handling - see rules R1-R9 below for `manual` rows registered by path). Rule R6 adds
three: the same repo's pre-scan fetch failing in two consecutive runs (`stale_repos`), the
fetch failing for every repo of the run (at least two, `outages: fetch_outage`) and the
embedder returning no vector for any chunk the run sent (`outages: embedder_down`). Keeping
unproven data (`unproven_drops`, kept orphans / groups, parser gaps) is never an exit 3; the
weekly `lifecycle-audit` lists it without failing (rule R6).

`--allow-mass-retire` remains only as an emergency bypass for a known-wrong oracle: it also
deletes what git cannot prove gone (unproven drops, kept orphans and groups, the kept part of
a shared prune) and lifts a held total wipe / embedding sweep. Never G-A, never the
single-owner entity prune, never in a timer.

### P5 - Audit schema `osm.lifecycle-audit/5`

New finding `unproven_drops` (per repo); version keys `orphan_modules[].kept_because`,
`child_orphans_kept`, `embedding_orphans_kept` (counted in `orphan_modules` /
`child_orphans` / `embedding_orphans`); `embedding_orphans_held` means an unhealthy graph;
`shared_prunes[].outcome` is `would_prune` or `partial`. Every /4 key stays. New top-level key
`informational_findings` (`["parser_gaps", "unproven_drops"]`): those counts stay in
`findings` but do not set `has_findings`, so `--fail-on-findings` never exits 4 on them
(rule R6).

### Consequences (phase 2)

- A commit that deletes any number of modules, a sweep of any number of ghosts and a mass
  orphan-embedding delete run on the next plain run; nobody approves.
- A scan regression that hides tracked modules no longer deletes them (the old ratio let a
  small one through) and no longer alerts nightly; it is a weekly audit finding.
- No migration (`repo_sync_state` reads two more existing columns; the shared-prune state
  string changes format, so every shared module is evaluated once after the deploy).

### Known limits (phase 2)

- An unproven ledger row stays `present` while its manifest is tracked; it is re-evaluated
  only when the repo is scanned again (HEAD moves, `needs_rewrite`, `--full`), not when the
  indexer code changes.
- A module that only exists inside an uninitialized submodule makes the whole repo's tree
  unreadable for the evidence: none of that repo's drops is proven until it is initialized.
- The orphan evidence is name-level per profile: a tracked manifest of the same name in any
  checkout of an owning profile keeps the orphan (the safe direction).
- Only a total wipe is checked against the branch history (`is_ancestor`); a drop of some
  modules is proven by the HEAD tree alone, also after a force-push. Decided on purpose:
  G-A already guarantees the checkout is the `origin/<branch>` tip, so a force-pushed remote
  IS the real code, and what its tree no longer ships is gone from the code OSM reflects.

---

## Rules R1-R9 (2026-10-20)

Cases where the oracle or the run still deleted live data, kept data for ever, or stayed
silent. Each rule below is protected by a behaviour test.

- **R1 - Test GC follows the kept modules.** The module-level test-node GC (graph-only, no
  embedding rows) keeps the tests of every module the lifecycle keeps although the scan lost
  it: the unparseable ones (as before) and the unproven drops (`GateResult.kept_names`;
  graph-baseline `name@version` entries at the versions the GC runs at)
  (`pipeline_repo._test_gc_live_names`). With `--allow-mass-retire` they retire and leave
  through the retirement cascade with their rows. The file-level exclusion of judged modules
  is per `(version, name)` (`_test_gc_judged`; the prune report is keyed that way).
- **R2 - A module at the repo root is judged.** Its directory prefix is empty; the oracle
  lists the whole tree instead of passing git an empty pathspec (which it rejects), in the
  per-repo prune and for a shared module's root-copy owner.
- **R3 - Test twins are file-scoped.** A stale TestClass / TestMethod with a live same-name
  twin in another file is `superseded` only when its OWN file no longer defines it (class,
  and for a method its `def` in that file); otherwise `present` (two same-name classes in two
  files of one module are legal).
- **R4 - Re-pointed relationships are superseded.** For the relationships whose writer picks
  one end node among several of the same identity by ranking (`REL_TWIN_TYPES`: INHERITS,
  DELEGATES_TO, PATCHES, REPORTS_ON, USES_CORE_SYMBOL), a stale one is `superseded` when this
  run wrote one of the same type from the same start node to ANOTHER node of the same
  identity (labels, name, model, xmlid, qualified name; DELEGATES_TO also `via_field`):
  `module_stale_children` returns `twin`. A USES_CORE_SYMBOL edge whose CoreSymbol is no
  longer `deprecated` (the only status it is written to) is `superseded` too.
- **R5 - Owner trust and scope.** The shared-module prune treats an owner whose checkout HEAD
  is not its `presence_head_sha` (the commit its last trusted scan reflected) as unavailable
  (keeps), and the owners' trust is part of the evaluated state, so the module is judged
  again once that checkout is trusted. A class-body name also bound outside the class counts
  both values. `prune_code_token` covers every file that changes the parse or the verdicts
  (also `version_registry.py`, `_xmlid.py`, `framework_bases.py`, `scanner.py`, the curated
  `tools_symbols_*.json` / `lint_rules_*.json`). Binary formats are not loaded; a text blob
  above 32 MiB makes the source unavailable (keep) instead of being skipped. The parser no
  longer names a model after the second parent when `_inherit[0]` is unresolvable and no
  `_name` is set: the class is not indexed (its stale nodes are kept by the oracle).
- **R6 - Alerts and reports.** A pre-scan fetch failure is returned by
  `refresh_before_scan` and written to `lifecycle_attention`; exit 3 when the same repo's
  fetch failed in two consecutive runs (`stale_repos`, marker read back from the previous
  run's attention) or every fetch of the run failed (at least two repos, `outages:
  fetch_outage`), and when the embedder returned no vector for any chunk the run sent
  (`outages: embedder_down`). One failure that heals is a WARNING. `lifecycle-audit` lists
  `parser_gaps` / `unproven_drops` (`informational_findings`) but they do not set
  `has_findings` and never exit 4: kept data is not an alert.
- **R7 - Clones never replace an operator's checkout.** The nightly retry clones a `manual`
  row only when its `local_path` is the clone directory OSM manages (a Web UI registration);
  a `manual` row registered by path whose directory is missing is reported unavailable. A
  second lock keyed by the resolved clone directory serializes repos whose URLs share a slug,
  and a clone directory holding another origin is never refreshed or indexed for a repo.

- **R8 - A moved module's old-copy files are judged, not kept for ever.** Every parser
  reads a module's files from under its own directory, so a file-keyed child (Stylesheet,
  JsTestSuite, JSPatch, LintViolation, the own-file check of a TestClass / TestMethod twin)
  whose path maps into no directory of the module was written from a copy the judged repo
  does not ship - the repo the module moved from when that repo nested it at another path
  (`addons/<m>` there, `<m>` here). When git proves the repo's HEAD has nothing at that
  path (and no gitlink above it) it is `gone` (`superseded` for a test twin); otherwise
  (the path exists here, a submodule could hold it, an absolute path outside the repo) it
  stays undecidable. Before, every such node was kept as a parser gap beside the new
  copy's nodes, with its embedding rows (a regression against ADR-0056 D9, which pruned
  them). The shared-module prune applies the same rule per owner to a path no owner's
  directory anchors. LintViolations are selected by file prefix, so the old copy's were
  never candidates at all: the prune selector also selects a violation of one of the
  module's own Views (`view_xmlid IN` the module's View xmlids, index seek) whose path lies
  under no prefix but has a `<m>/` directory segment - a foreign-xmlid override raised in
  another module's file keeps that module's directory and is never selected.
- **R9 - Shared AssetBundles follow their live contributors.** The bundle writer only
  unions `profile` and sets `module` on create, and the lifecycle never touched a shared
  label (ADR-0056 D8), so a bundle kept the profile of a contributor that moved to another
  profile and named a module that was retired or merged away. Right after
  `gc_orphan_asset_bundles` the reconcile runs `Neo4jWriter.reconcile_asset_bundle_owners`
  (one bounded statement per version, idempotent): `profile` = sorted union of the
  contributing Modules' profiles (unchanged when none carries one), `module` = kept while
  it still contributes, else the bundle's namespace module when it contributes, else the
  first contributor by name; a bundle with no inbound CONTRIBUTES_TO is left alone. This is
  D14's exact reset applied to the one shared label. No MCP tool or resource reads
  AssetBundle (nor `CONTRIBUTES_TO` / `INCLUDES_BUNDLE` / `EXTENDS_ASSET_BUNDLE`), so the
  stale profile was provenance drift, never a visible tenant leak. A contributor written
  by a concurrent run of another profile between the statement's read and its write can be
  dropped from `profile` until the next reconcile restores it (unread data).

Known limits of rules R1-R9: the fetch-failure memory is the previous run's
`lifecycle_attention` text (no migration); a run that crashes before writing attention keeps
the earlier note. A test node the file-level GC removes for a module whose prune is skipped
(shared / unsynced owner) still leaves without the oracle (ADR-0057 D4, unchanged).
