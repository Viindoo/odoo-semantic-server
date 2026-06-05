// correct_node_owning_profile.cypher
//
// PURPOSE
// -------
// Repair :Module (and child) nodes whose `profile[]` array was polluted by the
// pre-fix ADR-0016 Option-Y behaviour of stamping the FULL ancestor chain at
// WRITE time. When the same physical repo (e.g. the Odoo core repo) is indexed
// under a descendant profile, the old writer UNIONED the descendant's tenant-
// private ancestor chain onto shared-core nodes:
//
//     base.profile = ["viindoo_internal_17", "standard_viindoo_17", "odoo_17"]
//
// The ADR-0034 `all()` tenant choke then CORRECTLY denies `base` to any caller
// not allowed on every one of those three names — hiding shared Odoo core
// modules (base, sale, …) from callers scoped to the shared base profile.
//
// The DATA is wrong, not the predicate. This script rewrites each node's
// `profile[]` to the SINGLE owning profile of the repo that physically produced
// the node — restoring ADR-0034 D2's "index the shared base once" invariant and
// making Neo4j's array predicate structurally equivalent to pgvector's already-
// secure single-scalar `profile_name` membership (no split-brain). The WI-1
// writer fix (src/indexer/pipeline.py `_owning_profiles`) makes future writes
// correct; this script corrects the existing snapshot in place — no source
// re-parse, no downtime.
//
// ┌─────────────────────────────────────────────────────────────────────────┐
// │ ★ COLLISION-UNSAFETY — READ BEFORE RUNNING (F1)                          │
// └─────────────────────────────────────────────────────────────────────────┘
// This in-place backfill collapses a Module's `profile[]` to the single owner of
// its (single, first-writer-wins) `repo_id`. That is CORRECT for the prod
// situation it targets: shared public-core nodes (owner `odoo_*`) whose
// `profile[]` was polluted by *depender* tenant names within ONE inheritance
// hierarchy (collapsing them is safe — inheritance re-grants visibility via the
// `$shared` array at read time).
//
// It is NOT safe in the GENERAL case. For a genuine same-name cross-tenant
// COLLISION — two INDEPENDENT tenants (NOT in an ancestor relationship) each
// defining a module of the same `(name, version)` — the MERGE key (no `tenant_id`
// per ADR-0034 D2) converges them onto ONE node that legitimately carries a UNION
// of ≥2 owners, and the writer's `all()` choke correctly fail-closes it to both.
// A single `repo_id` (first-writer-wins) CANNOT reconstruct that multi-definer
// ownership, so this backfill would re-grant the node to ONE arbitrary tenant —
// re-opening the very leak `all()` closes. Run the in-place script ONLY when no
// same-name cross-tenant collision exists (e.g. a single tenant hierarchy where
// all sharing is via inheritance). Use Phase 0 below to check first.
//
// ► AUTHORITATIVE, COLLISION-SAFE CORRECTION: a `--full --no-embed` reindex under
//   the WI-1 writer fix. It regenerates `profile[]` from DEFINERS only (union-
//   correct for collisions — a colliding node keeps BOTH owners and stays
//   fail-closed). pgvector was never polluted by this bug (it always stamped the
//   leaf `profile_name`), so `--no-embed` is sufficient and avoids embedder load.
//   Prefer the reindex whenever Phase 0 flags any cross-tenant collision risk.
//
// SOURCE OF TRUTH
// ---------------
// The owning profile of a node is the profile that owns the `repos` row the node
// came from, authoritatively in Postgres. Export `tenant_id` too so Phase 0 can
// detect cross-tenant collisions:
//
//     SELECT r.id AS repo_id, p.name AS profile_name, p.tenant_id AS tenant_id
//     FROM repos r JOIN profiles p ON p.id = r.profile_id;
//
// Export that to a `$map` param: a list of {repo_id, profile_name, tenant_id}
// objects (tenant_id may be null for globally-shared `odoo_*` profiles). Pass it
// to this script (cypher-shell --param, Neo4j Browser :param, or the driver).
//
// SCOPE / SAFETY
// --------------
//   - Touches ONLY the `profile[]` property. No edges, no MERGE keys, no labels,
//     no node create/delete.
//   - Idempotent + re-runnable: the `WHERE coalesce(…profile,[]) <> [row.profile_name]`
//     guard makes a second run a no-op (0 nodes changed).
//   - Reversible: a `--full` reindex under the WI-1 writer regenerates `profile[]`
//     from scratch, so this is not a one-way door.
//   - Only rewrites nodes whose `repo_id` is in $map. A node with no `repo_id`
//     (legacy / placeholder) is left untouched by Phase 1 and only inherits via
//     Phase 2 if it has a DEFINED_IN/BELONGS_TO Module parent that WAS corrected.
//   - NULL-safe (F3): every guard + verify compares `coalesce(x.profile, [])`, so
//     a null-`profile` node is neither silently mutated nor reported as a
//     FALSE-CLEAN by the verify queries. Null-profile nodes are correctly
//     fail-closed for scoped tenants (admin `$own IS NULL` still sees them) and
//     are left untouched by the SET guards — that is acceptable; the point of the
//     coalesce is that VERIFY cannot MASK them.
//
// ORDERING (operator runbook)
// ---------------------------
//   0. Export $map (query above), then run PHASE 0 (read-only ADVISORY). If it
//      flags ANY Module whose profile[] spans ≥2 distinct non-null tenants, do
//      NOT run Phase 1 in place — run the `--full --no-embed` reindex instead
//      (collision-safe). Depender-pollution within ONE tenant hierarchy is safe
//      to collapse and may appear here as benign (verify the tenants are an
//      ancestor/descendant pair before proceeding).
//   1. Deploy the WI-1 writer fix (so future index runs stamp the owning profile).
//   2. (Registration hygiene) Ensure the shared Odoo core repo is registered ONCE
//      under the shared base profile (tenant_id IS NULL), with tenant profiles
//      inheriting via parent_profile_id. If the same repo is registered under
//      several profiles, a future index of each will RE-stamp its own owner — run
//      this script after consolidating, or it will need re-running.
//   3. Export $map from Postgres (query above) and run Phase 1 + Phase 2 below.
//   4. Run the VERIFY queries (Phase 3 + Phase 4); expect 0 residual rows.
//
// Run with (example):
//   cypher-shell -u neo4j -p <pw> \
//       --param 'map => [{repo_id:1,profile_name:"odoo_17",tenant_id:null}, …]' \
//       -f ops/correct_node_owning_profile.cypher
// or set `:param map => [...]` in Neo4j Browser then paste the statements.
// Each statement is terminated by ';' so they execute as a sequence.

// ---------------------------------------------------------------------------
// PHASE 0 — ADVISORY (read-only): flag cross-tenant collision risk BEFORE any
// write. Surfaces Modules whose existing `profile[]` maps (via $map
// profile_name → tenant_id) to >1 DISTINCT non-null tenant_id — i.e. a node a
// genuine collision would legitimately co-own. If this returns ANY row, do NOT
// run Phase 1 in place; use the `--full --no-embed` reindex (collision-safe).
// Depender-pollution confined to ONE tenant hierarchy (an ancestor/descendant
// pair) is benign and safe to collapse; eyeball the `tenants` column to confirm.
// Expected on the targeted prod snapshot: 0 rows (all sharing is via inheritance).
// ---------------------------------------------------------------------------
// Pure Cypher (no APOC dependency). `$map` rows carry {profile_name, tenant_id};
// for each Module profile name we look up its tenant_id by scanning the (small)
// $map list, keep the non-null ones, then de-duplicate via a reduce() into a
// distinct-tenant accumulator.
MATCH (m:Module)
WHERE size(coalesce(m.profile, [])) > 1
WITH m,
     [pn IN m.profile |
        head([row IN $map WHERE row.profile_name = pn | row.tenant_id])
     ] AS raw_tenants
WITH m, [t IN raw_tenants WHERE t IS NOT NULL] AS tenants
WITH m, reduce(acc = [], t IN tenants |
                 CASE WHEN t IN acc THEN acc ELSE acc + t END) AS distinct_tenants
WHERE size(distinct_tenants) > 1
RETURN m.name AS module, m.odoo_version AS version,
       m.profile AS profile, distinct_tenants AS tenants
ORDER BY module, version
LIMIT 50;

// ---------------------------------------------------------------------------
// PHASE 1 — Module nodes: rewrite profile[] to the single owning profile name.
// Batched for the prod-scale graph; the WHERE guard makes it idempotent.
// ---------------------------------------------------------------------------
CALL {
  UNWIND $map AS row
  MATCH (m:Module {repo_id: row.repo_id})
  WHERE coalesce(m.profile, []) <> [row.profile_name]
  SET m.profile = [row.profile_name]
} IN TRANSACTIONS OF 10000 ROWS;

// ---------------------------------------------------------------------------
// PHASE 2 — DIRECT children of Module inherit the corrected owning profile from
// their owning Module via the DEFINED_IN / BELONGS_TO edge. This catches the
// node kinds that attach directly to :Module — Model, View, QWebTmpl, OWLComp,
// JSPatch, Stylesheet. Only rewrites when out of sync, so it is idempotent.
// Children with no Module parent are left untouched (handled by Phase 2b or
// out-of-scope, see below).
//
// NOTE: Field and Method DON'T attach to Module — they attach to :Model via
// BELONGS_TO. They are handled by Phase 2b below, AFTER Models are corrected
// here, so they propagate from the already-fixed Model profile.
// ---------------------------------------------------------------------------
CALL {
  MATCH (child)-[:DEFINED_IN|BELONGS_TO]->(m:Module)
  WHERE coalesce(child.profile, []) <> coalesce(m.profile, [])
  SET child.profile = m.profile
} IN TRANSACTIONS OF 10000 ROWS;

// ---------------------------------------------------------------------------
// PHASE 2b — Field (~173k) + Method (~190k) inherit the corrected owning profile
// from their owning :Model via BELONGS_TO. These are the LARGEST polluted
// populations and are NOT reached by Phase 2 (they hang off Model, not Module),
// so omitting this phase leaves them choke-filtered with the stale 3-element
// profile[] → the reported bug persists for every field/method read of a shared
// core model. MUST run AFTER Phase 2 so :Model.profile is already corrected.
// Idempotent: the `<>` guard makes a re-run a no-op. Restricted to Field|Method
// labels so a future :Model→:Model BELONGS_TO (none today) can't be mis-touched.
// ---------------------------------------------------------------------------
CALL {
  MATCH (child)-[:BELONGS_TO]->(m:Model)
  WHERE (child:Field OR child:Method)
    AND coalesce(child.profile, []) <> coalesce(m.profile, [])
  SET child.profile = m.profile
} IN TRANSACTIONS OF 10000 ROWS;

// ---------------------------------------------------------------------------
// OUT OF SCOPE — :LintViolation (~20 nodes). LintViolation reaches NEITHER
// Module (no DEFINED_IN) NOR Model (no BELONGS_TO) via these edges, so it is not
// touched by Phase 1/2/2b. Live data confirms it is already single-profile
// (`[odoo_NN]`) and was never polluted by the descendant-chain / dependency-MERGE
// bug, so no correction is required. Documented here explicitly rather than
// silently ignored. If a future change makes LintViolation profile-bearing via a
// shared node, extend this script with a dedicated phase keyed to its owning
// Module/Model.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// PHASE 3 — VERIFY (read-only): Module nodes still mismatching $map.
// Expected: 0. Any row means the node's repo_id is absent from $map (export it)
// or the same repo is still registered under multiple profiles (consolidate).
// ---------------------------------------------------------------------------
UNWIND $map AS row
MATCH (m:Module {repo_id: row.repo_id})
WHERE coalesce(m.profile, []) <> [row.profile_name]
RETURN row.repo_id AS repo_id, row.profile_name AS expected,
       m.name AS module, m.profile AS actual
ORDER BY repo_id, module
LIMIT 50;

// ---------------------------------------------------------------------------
// PHASE 4 — VERIFY (read-only): DIRECT Module-children still out of sync with
// their Module. Expected: 0.
// ---------------------------------------------------------------------------
MATCH (child)-[:DEFINED_IN|BELONGS_TO]->(m:Module)
WHERE coalesce(child.profile, []) <> coalesce(m.profile, [])
RETURN labels(child) AS kind, count(*) AS mismatched_children;

// ---------------------------------------------------------------------------
// PHASE 4b — VERIFY (read-only): Field/Method still out of sync with their
// owning Model after Phase 2b. Expected: 0. A nonzero count here means Phase 2b
// did not run, or a Model was corrected AFTER this verify (re-run Phase 2b then
// re-verify). This is the population the original backfill MISSED.
// ---------------------------------------------------------------------------
MATCH (child)-[:BELONGS_TO]->(m:Model)
WHERE (child:Field OR child:Method)
  AND coalesce(child.profile, []) <> coalesce(m.profile, [])
RETURN labels(child) AS kind, count(*) AS mismatched_field_method;
