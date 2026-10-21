# ADR-0057: Source-oracle lifecycle prune - delete what git proves gone

**Status:** Accepted

**Date:** 2026-10-20

**Author:** Viindoo Engineering

**Amends:** ADR-0056 D6 (soft prune gate row, the `--allow-mass-retire` paragraph for the
entity prune), D9 (skip list, "Embeddings follow the graph"), D11 (`lifecycle-audit` schema
`/4`, `held_prunes` -> `parser_gaps`), Known limits.
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

## Rules R1-R9 (2026-10-20)

Cases where the oracle or the run still deleted live data, kept data for ever, or stayed
silent. Each rule below is protected by a behaviour test.

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
