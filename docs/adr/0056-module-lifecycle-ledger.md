# ADR-0056: Module lifecycle ledger - every index run retires what git no longer ships

**Status:** Accepted

**Date:** 2026-09-24

**Author:** Viindoo Engineering (issues #373, #378)

**Supersedes:** ADR-0007 D4 (as "the cleanup button") and D5 (`--gc`).
**Amends:** ADR-0007, ADR-0016, ADR-0034, ADR-0037, ADR-0048, ADR-0051, ADR-0054,
ADR-0055 (each carries a dated amendment pointing here).

---

## Context

**#378.** `check_module_exists('test_pylint', '17.0')` kept answering "Yes" after
tvtmaaddons renamed the module to `test_viin_pylint` (commit `0240c6b77f`,
2026-09-11). It was not a one-off: at 17.0, `viin_ai_rag`, `viin_ai_agent`,
`viin_ai_skill`, `viin_ai_search` and `viin_ai_pulse_memory` were ghosts too, and
`impact_analysis` counted them as dependents. Three root causes:

1. Only `gc_stale_modules` ever deleted a module, and only under `--gc`. No timer,
   no Web UI default and no runbook step ran `--gc` nightly, so nothing retired.
2. The incremental path could not see a module that disappeared: it diffs the
   modules that changed, and a deleted module is not a changed module.
3. `gc_stale_modules` deleted the Module node only. Its Fields, Methods, Views,
   tests and embeddings stayed. An entity removed from a module that still exists
   (a field dropped in a refactor) was never removed by anything. No node recorded
   when it was last seen.

**#373.** `test_class_inspect` built its subclass list with
`collect(DISTINCT {name, module})` after an `OPTIONAL MATCH`, so a class with no
subclass got one map of nulls: a phantom `[?] ?` row and a count inflated by one.
The same child hops had no tenant predicate (H6): a scoped key saw the names of
other tenants' private subclasses.

Survey evidence (`04`/`05` of the design set) showed that the full Odoo v8-v19
range and the third-party repos exercise every lifecycle shape: delete, rename,
move between repos, CE to EE, merge N:1, split 1:N, `installable` flips (436 at
tvtmaaddons 19.0 in one commit), circular renames, name reuse, the same name in
two repos, the v10 dual manifest, v8/v9 overlays, and 553 untracked `.odoo-ai`
manifests in one local clone.

## Decision

### D1 - A lifecycle ledger per (repo, module name) is the source of truth

Postgres table `module_presence` (migration `0003_module_presence.sql`), one row
per `(repo_id, name)`:

- `state` is `present` (tracked manifest, indexable), `excluded` (tracked, not
  indexed: `exclusion_reason` = `installable_false | license_skip | unparseable`)
  or `retired` (`retire_reason` = `absent | repo_removed | orphan_sweep`).
  CHECK constraints tie each state to its reason.
- History is kept forever: `first_seen_*`, `last_seen_*`, `state_changed_*`,
  `removing_commit_sha/date/subject`, `successor_names` + `successor_source`
  (`git_rename | old_technical_name`), `resurrection_count`. A retired row that
  comes back is resurrected (count + 1, evidence cleared), never duplicated.
- The repo identity (`repo_url`, `repo_basename`, `repo_branch`) and
  `profile_name` are denormalized, and `repo_id` is `ON DELETE SET NULL`, so the
  history survives a repo or profile delete. A profile rename rewrites
  `profile_name` in the same transaction as `UPDATE profiles`.
- `shadowed_paths` keeps same-name losers inside one repo (the posbox
  `point_of_sale` stub beside the real module in odoo12-18); they are never rows.
- `version_raw` / `version_mismatch` keep the manifest version verdict (D3).
- `needs_rewrite` asks the owning repo's next run to re-parse that module, even
  at an unchanged HEAD.
- Complete-parse record of each copy: `last_full_parse_at`, `_sha`, `_run`,
  `_unobserved`, `_embedded_at` (D10).
- `repos` gains `presence_head_sha` (the HEAD the ledger reflects),
  `lifecycle_attention` and `lifecycle_attention_at` (the operator signal).
- RLS policy `module_presence_tenant`, same shape as `embeddings_tenant`
  (`app.allowed_profiles` GUC, `'*'` sentinel) minus the `'__global__'` branch;
  ENABLE, not FORCE; `SELECT` granted to `osm_reader`.

`ModulePresenceStore` (`src/db/module_presence.py`) is the only writer. Every
ledger-row write for a version takes the advisory lock `retire:<odoo_version>` on
the connection it writes with, waiting up to `RETIRE_LOCK_WAIT_SECONDS` (900 s),
then raises `LifecycleLockTimeout`.

### D2 - Scan truth is the set of git-tracked manifests

`registry.build_registry_scan` (`src/indexer/registry.py`) replaces the directory
walk as the truth of "which modules does this repo ship":

- The tracked manifests (`git ls-files --recurse-submodules`) are filtered through
  the same version dispatch as the manifest finder: legacy keeps `__openerp__.py`,
  modern keeps `__manifest__.py`, v10 keeps one per directory, preferring
  `__manifest__.py`.
- A manifest the walk sees but git does not track is ignored and counted as
  untracked (the tvtmaaddons13 `.odoo-ai/` copies). A tracked manifest missing on
  disk makes the scan incomplete. Completeness compares paths, not names.
- A directory without a manifest is not a module (v8/v9 overlays); a module
  without `__init__.py` is.
- One winner per name inside a repo: present beats excluded; among present
  copies a 3-part numeric `version` wins, then the shallower path, then path
  order. Losers go to `shadowed`.
- Git tracking unavailable (not a work tree, unborn HEAD, `safe.directory`
  refusal) -> the working tree is used, the scan is untrusted, nothing retires,
  and `lifecycle_attention` says so.

Scan trust (`git_utils.head_matches_remote_branch`): HEAD equals
`origin/<branch>`; with no such ref, the checked-out branch name must equal the
registered branch (attention: "no origin/<branch> ref").

Precondition (L10): full clones (ADR-0008/0035). Rename evidence
(`incremental.compute_manifest_changes`, `git log --diff-filter=D`) needs blobs;
on a partial clone it degrades to "no evidence" with a WARNING, never to a wrong
decision.

### D3 - Version rule: the standard branch name decides the module version

`registry.resolve_repo_version` / `resolve_odoo_version`:

1. A standard branch (`^\d+\.\d+$` after normalisation: `saas-17.1` -> `17.1`,
   `refs/heads/18.0` -> `18.0`) is the module `odoo_version`. If the profile
   version differs, the branch still wins and `lifecycle_attention` says so.
2. A non-standard branch falls back to the profile version, with attention.
3. Only with neither does a long-form manifest prefix (`17.0.1.0.0`) decide.
4. A long-form manifest version whose `X.Y` differs from the module version keeps
   the branch version and sets `version_mismatch = true`, keeping `version_raw`
   (ledger row and Module node). Short versions (`1.0.0`) never mismatch.

Minor versions (`17.1`) work in `VersionRegistry`, the era dispatch and every
version sort (numeric major then minor). Before this ADR a long-form prefix beat
the branch (`16.0.1.0.0` on a 17.0 branch was keyed 16.0).

### D4 - Two-phase retirement: repos observe, one reconcile per version deletes

- **Observe (per repo, `pipeline_repo._index_repo`).** Every scanned run commits
  the `present`/`excluded` rows of its scan (`commit_observed`) and flags the
  names its scan no longer contains `retire_pending` (reason `absent`) with the
  removing commit and the successor (git rename in the run's range, else the
  removing commit's own rename, else a live module declaring
  `old_technical_name`). It never deletes a module. `retire_blocked_by` records
  `gate:<ids>` or `no_retire` when the run was not allowed to retire.
- **Execute (per version, `reconcile.reconcile_version`).** Runs at the end of
  `index_profile`, or once per version after every profile worker joined in
  `index_all`, under `retire:<v>` for its whole duration. Per pending name (H5):
  - another repo still has it `present` -> `drop_module_owner` resets the Module
    and its whole subtree to exactly the surviving owners, the departing
    profiles' embeddings are deleted, the survivors get `needs_rewrite` (M5);
  - no present owner, but a repo that may still ship it is unsynced (D7) ->
    undecidable: nothing is deleted, the row stays pending, attention, exit 3;
  - nobody ships it -> dependents' `head_sha` reset, `retire_modules` (D8),
    embeddings of every owning profile deleted, and only then `commit_retired`.
- The ledger says `retired` only after the delete succeeded (C1). A gate trip,
  `--no-retire` or a crash leaves the row pending; the next run finishes it.
- `presence_head_sha` advances only when no gate tripped, the presence stamp
  matched every present name, and nothing is pending (H1); when only pending
  names hold it, the reconcile advances it once they are decided.

### D5 - Reconcile on every scan: skip, sync path, incremental, full

- **Unchanged skip** (zero cost, ADR-0007 kept) needs all of: HEAD equals
  `repos.head_sha`, `presence_head_sha` equals HEAD, no `needs_rewrite` row, and
  no degraded-parse record whose files changed on disk.
- **Sync path:** HEAD unchanged but one of the other conditions fails. The repo
  is scanned, the ledger committed and the nodes stamped; only the modules that
  must be written are re-parsed. The first run after the deploy is a sync run for
  every repo (`presence_head_sha` is NULL), which is what cleans the pre-ledger
  ghosts without `--full` (owner decision: no manual cleanup script).
- **Always written, in every mode but the skip:** `needs_rewrite` names (cleared
  after the write), self-heal names (`modules_needing_rewrite`: `no_node` - no
  node carries this repo's owning profile; `path_drift` - the node's path is not
  the registry winner, e.g. the posbox stub or an `.odoo-ai` copy; `repo_drift`),
  and the shared-module bootstrap batch (D10).
- `stamp_module_presence` stamps every present Module with `last_seen_sha`,
  `last_seen_at`, `repos` (the ledger's present owner basenames),
  `version_mismatch` and `version_raw`. A shortfall (fewer nodes than present
  names) holds `presence_head_sha` and sets attention.
- `gc_stale_test_nodes` runs on every run where retirement is on, G-A passes and
  at least one module is present (no flag).
- **Version-wide post-pass skip (E2E-D4).** The post-pass of `index_profile`
  (same-name INHERITS, OWL edges, framework TestHelpers, INHERITS_TEST,
  `is_helper`, COVERS_*) derives edges from the graph alone. A
  `(:PostPassState {odoo_version})` node records whether it is current: every
  repo run that is not the unchanged skip marks its version dirty BEFORE its
  first graph write, and so do `retire_modules`, `drop_module_owner`,
  `prune_module_children` and `index-core`; a clean record carries a digest of
  the deriving code (`pipeline._post_pass_token`), so a deploy re-runs it once.
  A version whose record is clean with this code is skipped; a writer that
  wrote the version in this process always runs it; a dirty mark that lands
  while a post-pass runs keeps the record dirty. Measured on odoo17 +
  tvtmaaddons17 (1154 modules): a no-change run 23.2 s -> 0.7 s, and a forced
  post-pass right after a skipped one changed no edge (INHERITS, INHERITS_TEST,
  COVERS_*, BOUND_TO counts and `is_helper` identical).
- **`--full`** re-parses every module of every repo. Retirement does not need it.
  It remains the way to backfill a new index-time property (ADR-0053 pattern)
  and, as a side effect, lets the entity prune (D9) and the complete-parse record
  (D10) cover every module at once.
- **`--gc`** is a deprecated no-op: still accepted (timers, scripts and the Web
  UI still pass it), logs a deprecation WARNING.

### D6 - Safety gates, attention and exit codes

| Gate | Where | Trips when | Effect |
|---|---|---|---|
| G-A `scan_incomplete` / `scan_untrusted` | per repo | a tracked manifest is missing or cannot be read (permission, I/O), or HEAD is not the registered branch tip | nothing of the repo retires or prunes; never bypassable |
| G-B `mass_retire` | per repo | soft drops > 50% of the previously present modules AND >= 20; while the repo was never synced, drops of the graph baseline (below) | pending rows blocked `gate:mass_retire`; presence not synced |
| G-B `total_wipe` | per repo | modules were present before, some drop, and NONE is present now | same; no floor |
| G-B `orphan_sweep:<gate>` | per version sweep | the same ratio over orphan + child-orphan names | nothing swept |
| soft signal `manifest_unparseable` | per repo | a tracked manifest of an indexed module (ledger `present` before, or a graph node) was read but does not parse | the module is kept as it is - never swept, never dropped as an excluding co-owner - until it parses; attention + exit 3; the repo's other retirements proceed |
| soft prune gate `entity_prune:<M>@<v>` | per module prune (D9, D10) | stale > 50% of the module's nodes or relationships AND >= 20 | prune held, graph and embeddings kept |

- Soft drops are present -> retired and present -> `excluded(unparseable)`.
  `installable_false` and `license_skip` never count (observed, deliberate).
- **Graph baseline (owner decision 2026-09-24).** While the ledger has never
  reflected a repo (`presence_head_sha` NULL: the first runs after the deploy, a
  new registration) its ledger rows cannot tell what it shipped, and the ghosts
  it left are removed by the orphan sweep, not by pending rows. G-B then counts
  against the `(odoo_version, name)` Module nodes the graph attributes to the
  repo (`Neo4jWriter.repo_module_baseline`: its `repo_id`, or its owning profile
  + basename in `repo` / `repos`): a pair the scan no longer indexes at that
  version (absent, unparseable, re-keyed to another version) is a drop. A trip
  keeps the repo unsynced, so the sweep keeps every orphan of its profile (D8)
  and the run exits 3; normal ghost cleanup of the other repos proceeds, and the
  dry-run audit predicts the same (`gates.baseline = "graph"`). An empty baseline
  never trips.
- `--allow-mass-retire` bypasses G-B and the soft prune gate, never G-A. It is a
  one-shot operator decision after reading the attention text: never put it in a
  timer or a drop-in.
- `--no-retire` scans and writes but deletes nothing (incident escape hatch);
  names stay pending, the modules it re-parsed are flagged `needs_rewrite` so the
  next plain run prunes them.
- `repos.lifecycle_attention` is replaced on every scanned run (a clean run
  clears it) and carries: version-rule messages, gate reasons, git-trust
  problems, stamp shortfall, held / deferred / degraded entity prunes, and the
  reconcile's undecidable and orphan-deferral lines. The unchanged skip rewrites
  it with what still stands (F38).
- `index-repo` exits **3** (`EXIT_LIFECYCLE_ATTENTION`) when a gate tripped, a
  name was undecidable or the reconcile failed: the index was written, but an
  operator must look, so systemd `OnFailure=` fires. Details go to stderr and to
  `repos.lifecycle_attention`.

### D7 - Who may still ship a module: decided per module (F48)

`reconcile.PotentialOwners` is the one rule for the pending-name reconcile, the
entity prune and the audit. An unsynced repo (`presence_head_sha` NULL or not its
`head_sha`) blocks module M only when it may actually ship M:

- it has ledger rows and its last observation had M (`had_name`);
- it was never observed but has a checkout, and git tracks a manifest of M there
  under the same version dispatch (`ships_name`), or git tracking of the checkout
  is unavailable (`checkout_unreadable`, fail-safe);
- a never-observed repo with no checkout on disk never blocks (it wrote nothing;
  when cloned, its modules are simply added);
- a repo whose version rule places it at another version ships nothing here.

The owner drop follows the same rule: a pending name another synced repo still
ships is re-owned (D8) only when no unsynced repo other than the survivors may
ship it; otherwise it is undecidable like a name with no survivor (a drop would
hide the node from the unsynced repo's profile until that repo ran again). An
excluding co-owner's drop waits likewise (`excluded_owner_waiting`).

In the entity prune a `ships_name`-only blocker skips the prune without retry
(`shared_unsynced`, recorded on the Module; once the sibling syncs, the reconcile
sends the owner back through `needs_rewrite` if the sibling did not take M).
The orphan sweep keeps its per-profile rule: every unsynced repo of a profile
holds that profile's orphans (an orphan has no ledger observation to judge by).

### D8 - One cascade, exact owner reset, orphan sweep

- `Neo4jWriter.retire_modules(v, names, run_started_at=)` is the only module
  delete. Order: LintViolation (through its View or a dangling `<module>.` xmlid)
  -> Method, Field, Model, View, QWebTmpl, Report, JSPatch, OWLComp, Stylesheet,
  JsTestSuite -> TestMethod, TestClass -> addon TestHelper -> Module
  (`MODULE_CHILD_LABELS`). Never touched: other versions, `@framework`
  TestHelpers, `__unresolved__` placeholders, AssetBundle (shared, reclaimed by
  `gc_orphan_asset_bundles`), spec labels. Each step is an auto-commit
  `CALL {} IN TRANSACTIONS OF NEO4J_DELETE_BATCH_ROWS ROWS`, retried on
  transient errors; idempotent.
- **Race guard (H2):** a name whose Module was stamped at or after
  `run_started_at` is `skipped_recent` - neither the Module nor any child is
  deleted, the row stays pending. `run_started_at` comes from the Neo4j clock
  (`server_now`). The guard holds through the cascade: every child delete keeps a
  child whose `written_at` is at or after `run_started_at` (a concurrent run of
  another profile, which does not hold `retire:<v>`, re-MERGEd the module after
  the first check), and a name whose Module survives the final (stale-only)
  Module delete is reported `skipped_recent` too, so its embeddings and ledger
  row are left alone.
- `drop_module_owner(v, name, owners)` sets `profile`, `repos`, `repo`, `path` of
  the Module AND the same `profile` on every child to exactly the survivors (M4),
  and deletes the departed repo's TestClass/TestMethod nodes.
- **Orphan sweep** (per version, after the pending names): Module nodes with a
  non-empty `profile` or a `repo_id` and no `present` ledger row anywhere (the
  pre-ledger ghosts, the old Module-only `--gc` debris), module-owned children
  whose Module is gone, and embedding groups no live owner accounts for. Swept
  only when the repos that could claim them are synced, under G-B, and only on
  versions a repo was scanned for this run, or under `--allow-mass-retire` (an
  all-skip night stays zero cost).
  A swept module whose departure git can prove gets a `retired(orphan_sweep)`
  history row with the removing commit and successor (only when its repo has no
  row for the name yet).
- **Exclusions leave through the sweep.** A module whose manifest flips to
  `installable: False` (or is license-skipped) stays in the scan as `excluded`,
  so it is never flagged pending; once no repo has it `present`, its node is an
  orphan and the sweep removes it. `installable_false` / `license_skip` names
  never count toward the sweep's G-B gate (436 such flips at tvtmaaddons 19.0 in
  one commit). The ledger row stays `excluded`, with its history. When another
  repo still ships the module, the reconcile drops the excluding repo from the
  node instead (`drop_excluded_owners`: `drop_module_owner` to the present
  owners, the excluding profile's embeddings and the excluding repo's tests go,
  survivors `needs_rewrite`; `lifecycle-audit` counts it under
  `would_drop_owner`).
- Then the version GCs: `gc_orphan_asset_bundles`, `gc_unresolved_placeholders`,
  `gc_null_repo_dep_stubs` (in `index_all`, or when every repo of the version is
  synced), and `reconcile_same_name_inherits` whenever anything was retired,
  re-owned or swept (L3).
- Embedding deletes run under a transaction-local `app.allowed_profiles = '*'`
  (`writer_pgvector._write_scope`), so an owner role without BYPASSRLS on a
  FORCEd table deletes what it means to delete.

### D9 - Entities and relationships a live module no longer defines leave the graph

- **Run token.** `Neo4jWriter.begin_run()` sets a token that every module-child
  writer stamps as `written_run` together with `written_at` (server
  `datetime()`), on nodes AND on the relationships in `MODULE_CHILD_REL_TYPES`.
  `index_profile` begins one run for all its repos.
- **Prune** (`pipeline_repo._prune_reparsed_modules`, after each repo's writes):
  for every module re-parsed in this run, delete its children and relationships
  that carry another token (or none) AND were written before the run started.
- **Instant comparison (F52).** Every guard that orders a stamp against a run
  start (`retire_modules`, the prune's `_stale`, the TestHelper liveness check)
  compares `epochMillis` (`writer_neo4j._instant`). Cypher orders two DateTimes
  of the same instant by their zone (`Z` vs `UTC`), so a stamp in the run's first
  millisecond used to compare as older and a concurrently protected module could
  be retired. Epoch milliseconds are zone-free and match the statement clock.
- **Skipped** (per module, reason in the run counters): `degraded` (a file could
  not be read or parsed - `parse_health`; transient read failures retried once
  per source state, content failures when the files change), `shared` (another
  repo has it present; D10 handles it), `shared_unsynced` / `undecidable_owner`
  (D7), `soft_gate` (D6), and blanket `no_retire` / `scan_untrusted` /
  `no_ledger` / `no_run`. Families a parse did not observe (LintViolation without
  RelaxNG schemas) are left out.
- **Embeddings follow the graph:** the parse upserts; rows whose chunk key the
  parse did not produce are deleted only when the module was pruned (or when the
  prune machinery is unavailable, `no_run` / `no_ledger`, which keeps the old
  replace behaviour), on the run's own connection (F51). A held or skipped prune
  keeps the embeddings with the nodes.
- Never pruned: the Module node, `@framework` / `__unresolved__` nodes, records a
  module writes under another module's xmlid, post-pass edges owned by other
  passes (`INHERITS_TEST`, `COVERS_*` are reconciled by `reconcile_test_surface`
  itself; same-name `INHERITS` by `reconcile_same_name_inherits`).
- First deploy: nodes without a token are treated as stale only in a module that
  is re-parsed completely; tokens spread module by module as modules are
  re-parsed, so the deploy itself never mass-deletes.

### D10 - Modules two repos ship, and a bounded bootstrap (F49)

The per-repo prune cannot judge a shared module (graph children carry no repo).
`reconcile._Reconciler.prune_shared` does, per module M with >= 2 live present
owners:

- every owner row must hold a complete-parse record (`last_full_parse_at`: not
  degraded, under a run token, trusted scan) and be neither `needs_rewrite` nor
  `retire_pending`;
- cutoff = the oldest record; children and relationships written before it by no
  owner's latest parse are pruned (`written_before` mode, same selection and
  exclusions as D9, same-name `INHERITS` kept), under the soft gate;
- an unsynced repo that may ship M holds it (`shared_prune_waiting`);
- embeddings of every chunk kind: a row in an owner profile goes when no owner's
  profile holds a row of the same chunk key stamped since that owner's
  `last_full_parse_embedded_at`; when an owner's latest parse did not embed
  completely the rows are left alone;
- a child stamped after the cutoff by no owner's latest parse sends the owners
  whose record predates it back to re-parse M (`needs_rewrite`,
  `shared_prune_rewrites`); a watermark on the Module (`shared_prune_state`)
  keeps an unchanged night at one ledger query plus one batched read.

**Bootstrap.** After the deploy no copy has a record. Each run re-parses at most
`SHARED_PARSE_BOOTSTRAP_PER_RUN` (env `OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN`,
default 60, 0 disables) of the repo's shared copies without a record, copies
whose other owners already recorded first. Measured on CE 17.0 (606 modules):
~1.2 s per module parse + graph write before embeddings, so ~75 s extra per repo
per run; a CE clone drains in about 11 daily runs, all clones in parallel.

### D11 - `lifecycle-audit`: a dry run of the next index run

`python -m src.indexer lifecycle-audit (--profile P | --all) [--version V]
[--json] [--fail-on-findings]` (`src/indexer/lifecycle_audit.py`) runs the same
decision code as an index run (`plan_repo_run`, `build_registry_scan`,
`observe_lifecycle`, `_commit_lifecycle`, `reconcile_version(dry_run=True)`)
against a session-private `pg_temp` copy of `module_presence` and `repos` in a
READ ONLY session, and a `ReadOnlyWriter` over Neo4j. It takes no lock, does no
`git fetch`, and bounds every graph read by `LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS`
(120 s).

JSON schema `osm.lifecycle-audit/3`. Findings (`FINDING_KEYS`): `would_retire`,
`would_drop_owner`, `undecidable`, `blocked`, `held_prunes`, `shared_prunes`,
`orphan_modules`, `child_orphans`, `embedding_orphans`,
`modules_without_profile` (F24), `would_rewrite`, `wrong_paths`,
`unapplied_changes`, `unparseable_kept`, `errors`. Informational, not findings: `prune_rewrites`,
`shared_prune_waiting`, `shared_prune_rewrites`, `shared_parse_backlog`.

Exit codes: 0 = report printed; **4** = `--fail-on-findings` and at least one
finding; 2 = unknown profile; 1 = crash. It needs migration 0003.

### D12 - Web UI removal goes through the ledger

`DELETE /api/repos/repos/{id}` and `DELETE /api/repos/profiles/{id}`
(`web_ui/routes/repos.py::_remove_repos_through_ledger`): take the profile lock
(try once), each repo's git lock and `retire:<v>` of every affected version (each
waited up to `WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS`, 20 s; otherwise HTTP 409 with
`Retry-After` and nothing changed), `mark_repo_removed`, then
`reconcile.reconcile_removed_repos` (same per-name decision as D4, plus the
removed repos' pre-ledger residue - a node with `Module.repo` = a removed repo's
basename AND owned by a removed repo's profile, or profile-less with its
`repo_id`; a basename alone collides across profiles - no version-wide sweep),
then the Postgres
delete while the locks are held. A module another repo still ships survives with
its ownership reset; history rows survive with `repo_id` NULL. Neo4j unavailable
-> rows stay pending and the next index run decides them. The old
`delete_modules_scoped` (delete by `Module.repo` basename) is removed: every CE
clone has basename `odoo`, so it retired the CE modules of every profile at that
version (F1).

### D13 - The read side says why a module is not there and how fresh it is

- Existence rule: a Module node with a non-empty `profile` that passes the
  ADR-0034 choke (`lifecycle_read.owned_pred`). Profile-less dependency stubs
  answer "No" to every caller.
- `check_module_exists` YES: `Last seen: HEAD <sha7> on <date> [<repo>]`, per
  repo from the ledger when several repos own it; `Version note` on a mismatch.
- `check_module_exists` / `describe_module` NO: the ledger block per visible row
  (state, pending reason, removing commit subject verbatim, `Renamed to`). The
  pending reason is the full `retire_blocked_by` only for an admin key; a scoped
  key gets its class (`gate: <ids>`, `no_retire`, `undecidable`, `error`,
  `skipped_recent`), because an undecidable reason names unsynced repos of any
  tenant and an error reason carries raw exception text. An `old_technical_name`
  successor (recorded or from the reverse lookup) is shown only when a module
  of that name is visible to the key;
  `Renamed from`, a reverse `old_technical_name` lookup, `Present at other
  versions` listed explicitly (numeric order, never a range), the dependency-stub
  line, EE lines; the ledger unreachable -> one `Lifecycle: unavailable` line,
  and the resource cache does not keep that degraded body.
- Ledger reads pass an explicit profile list AND `SET LOCAL app.allowed_profiles`
  (never None, F14). Rows of a deleted profile are visible to nobody through MCP.
- Tool / resource surface unchanged: 31 / 9.

### D14 - Ownership is a union on write and an exact reset on retire

Writers still union the owning profile into `profile[]` on MATCH (ADR-0034
single-owner provenance). Only the lifecycle resets it exactly: `drop_module_owner`
(owner drop, Web UI removal) and `retire_modules` (full removal). This is the
only place a profile leaves a node.

## Consequences

- A plain nightly `index-repo --all` is the whole mechanism: renamed, deleted,
  moved and merged modules leave the index on the run after the commit that
  removed them, with their subtree and embeddings, and `check_module_exists`
  names the removing commit and the successor.
- Retirement is fail-closed: an incomplete or untrusted scan, a mass drop, an
  unsynced possible owner or a concurrent write keeps the data and raises exit 3
  / `lifecycle_attention` instead of deleting.
- Every run now reads the ledger (a few queries per repo) and stamps present
  Modules. The unchanged skip stays zero cost.
- Deploys whose owner role lacks BYPASSRLS on a FORCEd `embeddings` table now
  delete embeddings correctly (they silently deleted 0 rows before).

## Rollout

Runbook: [`docs/deploy/runbooks/module-lifecycle-cleanup.md`](../deploy/runbooks/module-lifecycle-cleanup.md).
Backup (ADR-0018) -> read-only F24 check -> deploy 0.19.0 -> `src.db.migrate`
(0003) + regrant -> the normal index run (no `--full`, no script) syncs every
repo and retires the ghosts under the gates -> verify -> enable the weekly
`odoo-semantic-lifecycle-audit.timer`. Rollback: redeploy 0.18.x (the ledger and
the new columns are inert to it); a wrongly retired module comes back with
`index-repo --full` of its owner's profile.

## Known limits

- First deploy: shared-module residue drains over ~11 daily runs per CE clone
  (D10 budget); a repo whose parse of a shared copy stays untrusted keeps its
  module out of the shared prune.
- A never-cloned repo keeps its profile's pre-ledger orphans (attention, audit
  finding `orphan_modules`, not exit 3).
- Multi-owner Module node properties (`version_mismatch`, `version_raw`,
  `last_seen_sha`) are the last stamping repo's; the per-repo truth is the
  ledger row.
- A healthy `odoo://` resource body can outlive a reindex by up to the resource
  cache TTL (300 s); degraded bodies are never cached.
- A module that stays degraded by a content error keeps its stale children until
  its files change.
- `--no-retire` flags every module it re-parsed `needs_rewrite`: after
  `--full --no-retire` the next plain run re-parses everything.
- Two tenants shipping the same private module name still share one node
  (ADR-0034 A3).
- (Fixed, G2) A module one repo turns `excluded` (`installable: False`, license
  skip, unparseable) while another repo still has it `present` used to keep the
  excluding repo's profile in the node's `profile[]`. The reconcile now drops
  that owner exactly like a co-owner's retirement (see "Exclusions leave through
  the sweep" above).
- `scanner.is_odoo_version_branch` (legacy auto-discovery for
  `scripts/index_test.py`) does not accept `saas-17.1`; the profile-driven
  pipeline does not use it.
- Spec data (`cli_flags_*`, `lint_rules_*`, `tools_symbols_*`) is keyed by exact
  version: a `17.1` profile gets empty spec answers.

## References

- Code: `src/db/module_presence.py`, `migrations/0003_module_presence.sql`,
  `src/indexer/registry.py::build_registry_scan`, `src/indexer/lifecycle.py`,
  `src/indexer/pipeline_repo.py::_index_repo`, `src/indexer/reconcile.py`,
  `src/indexer/writer_neo4j.py::retire_modules` / `drop_module_owner` /
  `prune_module_children`, `src/indexer/lifecycle_audit.py`,
  `src/mcp/lifecycle_read.py`, `src/web_ui/routes/repos.py`.
- Env: `RETIRE_LOCK_WAIT_SECONDS`, `WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS`,
  `OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN`, `LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS`
  (`docs/operations/timeouts.md`).
- Units: `docs/deploy/odoo-semantic-reindex.{service,timer}`,
  `docs/deploy/odoo-semantic-lifecycle-audit.{service,timer}`.
