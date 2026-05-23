# ADR-0034 — Multi-Tenant Pooled Isolation + Deploy-Key Credentials

**Date:** 2026-05-22
**Status:** Accepted + Amended (2026-05-23) — DDL/plumbing (v0.9.1: tenants table, tenant_id FKs, ssh_key_pairs.key_type, repos uniqueness, embeddings.profile_name, deploy-key endpoint, git hardening) + read-side enforcement WI-3/WI-4 + cross-tenant leak gate shipped (v0.10.0 #163, refined #165); Postgres RLS (D6) still deferred to WI-7 hardening wave. See Amendment.

> Supersedes the "optional `profile_name` filter" posture of [ADR-0016](0016-profile-hierarchy-and-neo4j-isolation.md)
> D6 and the "profile is convenience, not authz" amendment of
> [ADR-0029](0029-implicit-session-context.md). Builds on the deploy-key
> machinery of [ADR-0008](0008-ssh-auto-clone.md) and [ADR-0020](0020-fernet-key-delivery.md).

## Context

OSM is moving from a single-organisation deployment (Viindoo indexes public
Odoo + Viindoo EE, then exposes them to internal users) to a **pooled
multi-tenant SaaS**: many customers, each with **private repositories**,
served from one shared Neo4j + one shared Postgres.

Three foundational gaps make the current code unsafe for that model:

1. **Data is not isolated between tenants.** Neo4j nodes are keyed by
   `(name, …, odoo_version)` with **no tenant in the key**; the `profile_name`
   filter is *optional* and defaults to `None` → a query returns every node
   of that version (`src/mcp/server.py:361`, ADR-0016 D6). The `embeddings`
   table has **no profile/tenant column at all** — ANN search returns chunks
   from every profile sharing an `odoo_version` (`src/db/migrate.py:95-118`,
   `src/mcp/server.py:805-821`).

2. **Profile is segmentation by convention, not authorization.** `api_keys`
   has no `tenant_id`; any key can read any profile by passing `profile_name`
   explicitly (ADR-0029 amendment). `list_available_profiles()` returns *all*
   profiles to every key (`src/mcp/server.py:4934`).

3. **Credentials are centralised with a large blast radius.** SSH private
   keys are stored under one global `FERNET_KEY`; `ssh_key_pairs` has no owner
   column (`migrations/0001_initial.sql:54-61`). The current flow assumes
   Viindoo holds keys for every repo.

### Why pooled (not silo / self-host)

The deploying org chose the **pooled** topology (one datastore, row-level
isolation) for cost and operational simplicity. Pooled is also the highest-risk
option for cross-tenant leakage — so the design below centres on closing
gap #1 with **mandatory, fail-closed** enforcement at a single choke point,
not 88 hand-edited query sites.

> **Site-count correction (2026-05-22, post-wave3 survey):** The "~27 sites" and "88 query sites" figures were pre-implementation estimates. Verified count: **61 user-data Cypher query sites** (57 in `src/mcp/server.py` + 4 in `src/mcp/orm.py`) PLUS 3 embeddings queries with no Neo4j filter (`find_examples`, `find_style_override`, `suggest_pattern`) that rely on pgvector RLS (WI-5/ADR-0034 D6) for isolation. The "88 query sites" figure in D4 referred to a broader naive-approach count; the actual enforcement surface for WI-4 is 61 + 3.

### Why shared-base + overlay (not `tenant_id` in every key)

The naive reading of "pooled" — add `tenant_id` to all 15 node MERGE keys and
all ~88 read queries — was **rejected**:

- It duplicates shared Odoo data. If 100 customers run Odoo CE 17, the node
  `sale.order` is cloned 100 times in one graph, destroying the entire benefit
  of a pooled store.
- 88 hand-edited filters = 88 chances to forget one = 88 leak paths.

The genuinely-private surface per customer is only their **custom modules**.
Odoo CE/EE, core symbols, lint rules, and curated patterns are **public,
shared** data. This maps exactly onto the existing profile-inheritance
mechanism (ADR-0016 Option Y): a tenant profile whose parent is the shared
public `odoo_N` profile. The isolation primitive (`profile: list[str]`)
already exists; what is missing is (a) turning the filter from *optional* to
*mandatory + fail-closed*, (b) binding API keys to tenants, (c) a profile
column on `embeddings`.

## Decision

### D1 — Tenant entity + foreign keys (control plane)

A new `tenants` table is the authorization boundary. Shared/global rows use
`tenant_id IS NULL`.

```sql
CREATE TABLE tenants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

ALTER TABLE api_keys      ADD COLUMN tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE profiles      ADD COLUMN tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE;  -- NULL = shared base
ALTER TABLE ssh_key_pairs ADD COLUMN tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE repos         ADD COLUMN tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE;
```

A `tenant_id IS NULL` profile (e.g. `odoo_17`) is the **shared base**; every
tenant profile sets `tenant_id` and parents onto a shared-base profile of the
same `odoo_version` (ADR-0016 version-match rule already enforces this).

### D2 — Data model: shared base + per-tenant overlay (NO MERGE-key change)

Neo4j node MERGE keys are **unchanged**. Isolation reuses the `profile: list[str]`
ancestor-chain property (ADR-0016 D5):

- **Shared base** is indexed once under a `tenant_id IS NULL` profile; nodes
  carry `profile=['odoo_17']`. Not duplicated per tenant.
- **Per-tenant overlay**: a tenant indexes only its custom-module repos under
  a child profile; those nodes carry `profile=['acme_17','odoo_17']`. The
  union/stub-ownership rules of ADR-0016 D5/D7 apply as-is.

This keeps `sale.order` single-instance in the graph while making custom nodes
tenant-scoped through the existing inheritance array.

### D3 — Spec data stays global shared

`CoreSymbol`, `LintRule`, `CLICommand`, `CLIFlag`, `PatternExample`, and
`SpecMetadata` are standard Odoo reference data, identical across tenants.
They keep their current keys, carry **no** `profile`/`tenant_id`, and their
read queries (`_lookup_core_api`, `_lint_check`, `_cli_help`, `_suggest_pattern`
Neo4j fetch, `api_version_diff`) are **exempt** from the tenant filter. This
bounds the enforcement surface to the 9 user-data labels only.

### D4 — Choke-point enforcement (mandatory, fail-closed)

Because Neo4j Community has **no per-label row security**, enforcement must
live in the application at a single resolver — not scattered across ~88 query
sites.

1. `verify_api_key()` (`src/db/auth_registry.py:70-120`) returns `tenant_id`
   in addition to `api_key_id`. The ASGI auth middleware
   (`src/mcp/middleware.py:152-216`) writes `request.state.tenant_id`, and the
   tool-context thread-local (`src/mcp/server.py:153-164`) exposes it.
2. A single helper `resolve_allowed_profiles(tenant_id) -> list[str]` returns
   the tenant's profiles plus their shared-base ancestors (Postgres CTE,
   reusing `get_ancestor_profile_names`). Result is cached per the existing
   60s session cache (ADR-0029).
3. Every user-data Cypher query takes a **required** `$allowed_profiles`
   parameter and filters `WHERE <node>.profile IS NOT NULL AND
   any(p IN <node>.profile WHERE p IN $allowed_profiles)`. The
   `$profile_name IS NULL OR …` (optional-bypass) form is **removed** for
   user-data tools.
4. **Fail-closed**: a request whose API key resolves to no tenant context
   (and is not an admin/global key) returns an empty result — never the full
   graph. `set_active_profile` / `list_available_profiles` validate the
   profile belongs to the caller's tenant (`src/mcp/session.py:231`,
   `src/mcp/server.py:4934`).

Admin/global keys (`tenant_id IS NULL` on the key) bypass the filter for
operational queries — this is the only unscoped path and is audit-logged.

### D5 — `_latest_version()` must be tenant-scoped

`_latest_version()` (`src/mcp/server.py:295`) currently scans all `Module`
nodes. Under multi-tenancy it must restrict to `$allowed_profiles`, otherwise
`set_active_version("auto")` for tenant A can resolve to a version only tenant
B has indexed.

### D6 — pgvector: `profile_name` column + Postgres RLS

The `embeddings` gap is hard — there is no profile dimension to filter on.

```sql
ALTER TABLE embeddings ADD COLUMN profile_name TEXT;          -- NULL = shared base
-- recreate UNIQUE to include profile_name; add it to idx_embeddings_filter
ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;
CREATE POLICY embeddings_tenant ON embeddings
    USING (profile_name IS NULL
           OR profile_name = ANY (string_to_array(current_setting('app.allowed_profiles', true), ',')));
```

The MCP DB layer sets `SET LOCAL app.allowed_profiles = '…'` per request from
the resolved allow-list. Writer changes: `EmbeddingChunk` gains `profile_name`;
INSERT and the `DELETE … WHERE module=%s AND odoo_version=%s` clause
(`src/indexer/writer_pgvector.py:271`) add `AND profile_name=%s`.
**Postgres RLS is the strongest available enforcement layer and is preferred
over relying on every SQL string being correct** — Neo4j cannot offer the
equivalent, which is exactly why D4's choke point matters there.

### D7 — Deploy-key credential model (customer adds Viindoo's public key)

Customers grant repo access by adding **Viindoo's public key** as a read-only
deploy key on their own repo — Viindoo's private key never leaves the server,
and the customer controls grant/revoke.

- **Per-tenant keypair** (not one shared key): `ssh_key_pairs.tenant_id`
  (D1) + a `key_type TEXT CHECK (key_type IN ('deploy_key','access_key'))`
  column. Per-tenant keys let a customer revoke independently and bound the
  blast radius in the pooled store.
- **Self-service public-key endpoint**: a tenant-scoped
  `GET /api/tenant/deploy-key` returns the (non-secret) public key plus
  add-as-deploy-key instructions, gated by tenant-level auth — not full admin.
  The tenant identity is derived from the `X-API-Key` (request.state.tenant_id),
  NOT a path/query parameter, so a cross-tenant fetch is structurally impossible.
  The generation/encryption/expose machinery already exists
  (`src/web_ui/routes/ssh_keys.py:33-49,107-168`; UI copy button at
  `site/src/pages/admin/ssh-keys.astro:141-149`).
- Clone flow (`src/git_utils.py:103-117`, ADR-0008 `GIT_SSH_COMMAND`) is
  unchanged.

### D8 — Fernet hardening (envelope encryption deferred)

Per-tenant envelope encryption (per-tenant DEK wrapped by a KMS master key)
is **deferred** — it needs KMS integration disproportionate to current scale.
Interim posture: keep the single `FERNET_KEY` + existing `rotate-fernet` CLI
(`src/cli.py:697-807`), but **move the key into a secrets manager** (out of the
plain env file). This is recorded as explicit hardening debt because pooled
storage raises the cost of a `FERNET_KEY` compromise.

## Migration phases

- **P1 — Control plane:** `tenants` table; `tenant_id` FKs (D1);
  `verify_api_key` returns `tenant_id` (D4.1). DB + auth only; indexer untouched.
- **P2 — Enforcement choke point:** `resolve_allowed_profiles` helper;
  mandatory fail-closed Neo4j filter (D4.3/D4.4); tenant-scoped
  `_latest_version` (D5); `list_available_profiles` tenant filter.
  **Ships with a mandatory cross-tenant leak test** (tenant A must never see a
  tenant B node, via any tool, with or without explicit `profile_name`).
- **P3 — pgvector:** `profile_name` column + RLS + writer/reader updates (D6).
- **P4 — Deploy-key self-service:** per-tenant keypair, `key_type`,
  tenant-scoped public-key endpoint (D7).
- **P5 — Hardening:** `FERNET_KEY` → secrets manager; audit unscoped
  admin-key paths; revisit envelope encryption (D8).

## Consequences

**Positive:**
- Shared Odoo data stays single-instance; pooled store keeps its cost benefit.
- Enforcement surface for Neo4j is one resolver, not 88 query sites.
- Postgres RLS gives DB-level isolation for embeddings independent of SQL
  correctness.
- Reuses ADR-0016 inheritance + ADR-0029 session cache + ADR-0008 deploy-key
  machinery rather than inventing new mechanisms.

**Negative:**
- **One-time full reindex** to backfill `profile`/`profile_name` for existing
  nodes/chunks (same operational note as ADR-0016).
- Removing the optional-bypass filter is a **breaking change** for any current
  caller relying on `profile_name=None` returning everything; admin/global
  keys remain the explicit unscoped path.
- Neo4j Community offers no per-label security, so a bug in the D4 resolver is
  still a leak path — this is why the cross-tenant leak test in P2 is a release
  gate, not optional.
- Deferring envelope encryption (D8) leaves a single-`FERNET_KEY` blast radius
  until P5.

## Amendment — enforcement wave (2026-05-23, `feat/osm-final-stretch`)

The read-side enforcement deferred above shipped here. Three points refine D2/D4/D6:

**A1 — WI-3 / WI-4 shipped (D4 realised).** `resolve_allowed_profiles(tenant_id)`
(`RepoStore` recursive CTE + a 60s `session.py` cache; `None` = admin/unrestricted,
`[]` = deny-all) is the single resolver. The fail-closed filter is applied at all
**61 Cypher sites** (57 `server.py` + 4 `orm.py`) as the uniform fragment
`($allowed IS NULL OR any(__ap IN <alias>.profile WHERE __ap IN $allowed))`, plus
`_latest_version()` (D5) and the previously-unfiltered `find_override_point`. The
optional `$profile_name IS NULL OR …` bypass form is **fully removed** (0 occurrences;
statically guarded). Behaviour is preserved for admin/no-tenant callers (`$allowed`
resolves to `None`), so the gate adds isolation without changing existing results.

**A2 — D6 RLS deferred; SQL filter is the embeddings read-guard.** The 3 pgvector
ANN queries are split: `find_examples` + `find_style_override` (user-data chunks)
now carry `AND profile_name = ANY(%s)` built from the same resolver; `suggest_pattern`
is **exempt** (pattern_example chunks are the global shared catalogue, like spec data,
D3). Postgres RLS (D6) is **not** enabled in this wave: correct RLS needs
`FORCE ROW LEVEL SECURITY` + a non-owner read role + a write-path policy (otherwise
the table-owner role bypasses the policy and the indexer writer is blocked) — a
deployment/role change. The SQL filter is the actual read-isolation guard (proven by
the WI-4 cross-tenant leak test); RLS-with-read-role is re-scoped to the WI-7 hardening
wave as defense-in-depth.

**A3 — Pooled MERGE-key collision: known limitation + mitigation (D2 refinement).**
Because Module/sub-node MERGE keys exclude `tenant_id` (D2), two **distinctly-tenant
but identically-named private** modules (e.g. tenant A and tenant B both ship a custom
module literally named `sale_custom` at the same version) MERGE into ONE node whose
`profile[]` is the union of both tenants' chains — the WI-4 filter would then match it
for both, leaking/contaminating that node. Shared Odoo CE/EE + correctly-distinct
private modules are unaffected. Mitigation (chosen 2026-05-23, "document + constraint"
over a breaking MERGE-key change): tenant-private module names MUST be namespaced
(operator convention — e.g. a tenant-specific technical-name prefix), documented here
and at repo registration; the cross-tenant leak test validates isolation **under this
convention** (distinct names) and explicitly treats the same-name case as the known
residual. The proper fix — a tenant/repo discriminator in the MERGE key for
non-shared nodes (REC-8) — is deferred to its own RFC pending demand, because it is a
full-graph-teardown breaking change.

## Amendment — tenant model clarification (2026-05-23, WG-6 docs)

This section consolidates the confirmed tenant model semantics, known limitations, and
cross-process cache behaviour. It supersedes the informal notes scattered across earlier
amendments and the CHANGELOG entry.

### T1 — Shared vs own profile definitions (confirmed)

- **Shared profiles** (`tenant_id IS NULL` on the `profiles` row, e.g. `odoo_17`):
  contain Odoo CE base data + shared ancestor overlays (Odoo EE, curated community).
  All tenants inherit these automatically via `resolve_tenant_scope`. An admin/global
  API key (`tenant_id IS NULL` on the `api_keys` row) accesses ALL profiles with no
  filter — the only unscoped path.
- **Own profiles** (`tenant_id = <N>`): the tenant's private modules indexed under
  a child profile whose `parent_profile_id` points to the matching shared base
  (ADR-0016 version-match rule enforced). Only the owning tenant's key resolves them.
- **Spec-data global exempt** (D3): `CoreSymbol`, `LintRule`, `CLICommand`, `CLIFlag`,
  `PatternExample`, `SpecMetadata` carry no `profile` and are exempt from the filter.
  `suggest_pattern` is also exempt (global catalogue). All other user-data labels
  (Module, Model, Field, Method, View, OWLComp, JSPatch, Stylesheet, LintViolation)
  are subject to the choke-point filter.

### T2 — Choke-point filter invariant (confirmed)

The uniform guard fragment applied at all 61 Cypher sites is (params: `$own`, `$shared`):

```cypher
($own IS NULL OR (size(<alias>.profile) > 0
                  AND all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared)))
```

- A node is **granted** iff `$own` is `NULL` (admin) OR every profile on the node
  is in the tenant's own-or-shared allow-list.
- **Fail-closed**: a node whose `profile = []` (vacuous truth bug F-6, ADR-0034 D4)
  is *denied* because `all(__p IN [] WHERE ...)` evaluates to `TRUE` in Cypher, but
  the additional `size(<alias>.profile) > 0` guard ensures empty-profile
  nodes are never granted. Pre-reindex precondition: **0 nodes must have `profile=[]`**
  (verify with 5.11 query below before multi-tenant traffic).
- **`profile_name` is NARROW + non-escalating**: when an API key passes
  `profile_name` explicitly (via `set_active_profile` or tool arg), `$own` is narrowed
  to `[profile_name]` while `$shared` is kept — so nodes carrying `[own_profile, base_profile]`
  remain visible. A key cannot use `profile_name` to name a profile outside its resolved
  scope — the session layer validates membership before injecting
  (ADR-0029 superseded: profile is now narrowing authz, not advisory convenience).
  **Both Neo4j (choke-point) and pgvector (`AND profile_name = ANY(%s)`) use the same
  narrowed scope** — eliminates the split-brain that existed when Neo4j respected
  `profile_name` but pgvector did not.

### T3 — Cross-process cache constraint (known limitation)

The 60s in-memory session cache (ADR-0029) lives per process. MCP (`:8002`) and
FastAPI (`:8003`) are **two separate Python processes** with independent caches:

- A tenant profile mutation (add/remove profile, change `tenant_id`) invalidates the
  FastAPI cache immediately but the MCP process continues serving the stale resolved
  allow-list for up to **60 seconds**.
- The invalidation endpoint (`invalidate_allowed_profiles()`, currently 0 callers in
  prod — see F-11 / ADR-0034 consequence) only flushes the in-process cache of
  whichever service receives the call.
- **Chosen mitigation:** 60s TTL is acceptable for the current deployment scale.
  Cross-process invalidation (e.g. a shared Redis key) is deferred as a follow-up
  if sub-60s invalidation becomes a requirement. Admin-side profile changes are
  infrequent enough that the TTL window is safe for now.

### T4 — Profile `[]` pre-reindex gate (operations precondition)

Before enabling multi-tenant traffic (routing real API keys to the choke-point filter):

```cypher
MATCH (n) WHERE size(n.profile) = 0 RETURN labels(n) AS lbl, count(n) AS cnt;
```

Expected: **0 rows** for all user-data labels. Any non-zero count means nodes were
written before the profile-array writer was enforced (ADR-0016). Run `index-repo --all
--full` for the affected profile to backfill; nodes without a matching module in the
active index will need a manual Cypher `SET n.profile = ['<profile_name>']` or
deletion. See runbook §5.11 for the automated check.

### T5 — USES_FIELD same-module-only constraint (known limitation F-13 partial fix)

The USES_FIELD MATCH key includes `module` (WG-3w F-13 fix): a `self.field_name`
reference in method M is matched only to a Field node in the **same module** as M.
This prevents the fan-out (one ref matching Field nodes of every module that shares
the same field name on the same model), at the cost of under-counting cross-module
usage (a field defined in `base` but referenced from `sale` will not yield a
USES_FIELD edge from the `sale` method). Accepted trade-off: precision over recall
for usage tracking. Cross-module USES_FIELD via `DEPENDS_ON_FIELD` (which traces
`@api.depends` dotted paths) is unaffected.

### T6 — Public-share semantics + future direction (confirmed 2026-05-23)

"Shared" visibility is determined **purely** by `profiles.tenant_id IS NULL` (T1) and
resolved at READ time by `resolve_tenant_scope` (`src/db/repo_registry.py`). Two
consequences:

- **Re-classification is read-side, no reindex.** Making a profile shared↔private is a
  single `UPDATE profiles SET tenant_id = <N | NULL>` — the node `profile[]` arrays
  written at index time do **not** change; only the read-side scope resolution does.
  (Contrast a hypothetical `tenant_id`-in-MERGE-key model, rejected in D2, which would
  duplicate every shared CE node per tenant AND break the cross-tenant dependency
  closure — a tenant's `DEPENDS_ON base` could not resolve onto the single shared
  `base` node.)
- **The binary model is sufficient for launch** — one shared CE/EE base
  (`tenant_id IS NULL`) + per-tenant private overlays. There is no `public_shared` /
  per-repo / per-tenant-namespaced visibility flag, and none is needed for the launch
  use case (each customer consumes shared base + their own private repos; no customer
  shares their repos with another customer).

**Future "public share"** (a tenant publishing its OWN repos to other specific tenants,
per-repo granularity, self-service) is **deferred as a product feature, not a launch
gate** (RFC pending demand). If/when built it MUST populate a dedicated, **controlled
shared profile** (a re-indexed / namespaced publish target) — it must **NOT** flip
`tenant_id` on a tenant's live private profile, because that would retroactively expose
any name-colliding nodes and trigger the A3 collision fail-close. The profile-level
`profile[]` array (not a MERGE-key discriminator) remains the visibility primitive, and
the dependency closure already rides on it correctly.

### T7 — `describe_module` depends-list intentionally unscoped (confirmed not-a-leak)

The manifest `depends` list in `describe_module` (both `_describe_module` and
`_describe_module_structured`) returns only `d.name` of `DEPENDS_ON` targets with **no**
`_scope_pred("d")`, in deliberate contrast to `_module_dep_closure` which DOES filter
`dep`. This is **by design, not a leak**: the caller already passed the scoped
`mod_rec` query (which early-returns if they cannot see module `$n`@`$v`), and the
depends list runs as a *separate* `session.run` that re-matches `m` by name+version
only (not scoped) — so entitlement rests on that prior gate, not on this query. `d.name`
is a dependency name from the caller's own manifest; and the list returns **no foreign
node content** — whereas the
closure returns `dep.repo` / `dep.repo_url` (foreign paths/URLs) and therefore must
filter. A name-only list is not a useful leak oracle either: a `DEPENDS_ON` declaration
MERGE-creates a stub target node regardless of whether another tenant owns a same-named
module, so the name surfaces from the caller's own declaration. Adding the filter would
only hide a dependency the tenant itself declared whenever the name collides with
another tenant's private module (A3) — UX loss, zero confidentiality gain. Documented
here so a future audit does not re-flag the asymmetry as a bug.

## References

- ADR-0008 — SSH auto-clone (`GIT_SSH_COMMAND`, deploy-key delivery).
- ADR-0016 — Profile hierarchy + Neo4j Option Y (`profile[]`, inheritance).
- ADR-0020 — FERNET key delivery + rotation.
- ADR-0026 — RBAC + key ownership (`is_admin` DB-sourced).
- ADR-0029 — Implicit session context (per-API-key sticky version/profile, 60s cache).
- `src/db/auth_registry.py:70-120` — `verify_api_key` (returns `tenant_id` after P1).
- `src/mcp/middleware.py:152-216`, `src/mcp/server.py:153-164` — request context plumbing.
- `src/mcp/session.py:231` — `set_active_profile_db` tenant validation.
- `src/indexer/writer_neo4j.py:41-563` — node MERGEs (unchanged keys; profile array).
- `src/db/migrate.py:95-118`, `src/indexer/writer_pgvector.py:15-271` — embeddings schema + writer.
- `src/web_ui/routes/ssh_keys.py:33-168`, `site/src/pages/admin/ssh-keys.astro:141-149` — keypair gen + public-key expose.
