# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_orm_dense_inheritance.py
"""Issue #273 regression suite — dense-inheritance ORM behavior + anti-hang.

The 4 ORM-validation tools used to hang for ~19-24h on a model with a dense
extension mesh (K duplicate same-name Model nodes + a variable-length path that
enumerated 20-86M paths). WI-1 rewrote the read path to a per-hop name-dedup
BFS with a depth-first winner and a Neo4j query timeout; WI-2 added a bounded
semaphore; WI-3/WI-4 fixed the writer so same-name extenders link only to the
definition node (K edges, not K^2) plus a post-pass reconciliation.

These tests protect the BUSINESS BEHAVIOR those changes guarantee (ETHOS #11),
NOT the current code shape:

  - a dense fallback resolves correctly and does not hang (time bound detects a
    hang, it is NOT a benchmark — see debate Part 2-A1.6);
  - the depth-first winner: a field on a nearer ancestor shadows the same-named
    field on a farther ancestor;
  - validate_relation answers both the OK (subtype) and the MISMATCH
    (exhaustive-negative) case under bound;
  - the tenant choke point still fail-closes on an inherited field;
  - the Neo4j query timeout surfaces as a clean English OrmQueryTimeout;
  - the bounded semaphore caps concurrency, fast-rejects on overload, and
    releases its slot only after the worker thread settles;
  - the writer emits K same-name edges (not K^2) and the reconciliation post-pass
    backfills missing edges idempotently.

Requires Neo4j (testcontainers). The single cross-tenant test additionally
requires Postgres (tenant_id -> profile scope resolution).
"""
import asyncio
import os
import time
from contextlib import contextmanager

import pytest

from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Dedicated version for the dense graph — DELIBERATELY NOT TEST_VERSION ("99.0").
# The cross-tenant test below pulls in `clean_neo4j` (which DETACH DELETEs every
# odoo_version="99.0" node) and `clean_pg_embeddings` (PG_EMBED_VERSION="99.0").
# If the module-scoped dense graph also lived at "99.0", that per-test wipe would
# destroy it mid-session, making the writer-invariant tests order-dependent (a
# FIRST violation). A distinct version isolates the dense graph completely; its
# own fixture teardown cleans it. The cross-tenant test keeps PG_EMBED_VERSION.
DENSE_VERSION = "99.7"

# K = number of same-name extender modules of dense.model. Large enough that a
# reversion to the old all-K variable-length path would blow up, small enough to
# keep module-scoped seeding well under a few seconds (debate A1.6: the value is
# a regression tripwire, not a benchmark target).
K = 80

# A generous wall-clock ceiling: any correct query on this synthetic graph
# finishes in well under a second. A multi-second result means the hang has
# returned. We assert << the 30s Neo4j query timeout so a true regression is
# caught by THIS bound (a clean failure) rather than by the timeout path.
TIME_BOUND_S = 20.0

_DENSE = "dense.model"
_MIXIN = "dense.mixin"        # depth-1 ancestor that carries the target field
_FAR_MIXIN = "dense.far.mixin"  # depth-2 ancestor that carries a SAME-NAMED field
_TARGET_FIELD = "widget_ids"  # lives on BOTH mixins, different ttype (depth-first probe)
_VARIANT = "dense.variant"    # _inherits (delegation) child of dense.model

# Relation subtype chain for validate_relation.
_REL_TARGET = "dense.target"  # comodel base
_REL_SUB = "dense.sub"        # INHERITS dense.target  -> subtype OK
_REL_OTHER = "dense.other"    # unrelated             -> MISMATCH


def _writer() -> Neo4jWriter:
    return Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


@pytest.fixture(scope="module")
def seeded_dense_graph(neo4j_driver):
    """Seed a dense same-name extension graph through the REAL Neo4jWriter.

    Topology (all at DENSE_VERSION):

        dense.model (definition, is_definition=true) --INHERITS--> dense.mixin
        dense.mixin                                  --INHERITS--> dense.far.mixin
        K x dense.model (extender, is_definition=false, inherit=['dense.model'])
            -- each --INHERITS--> the definition node (writer WI-3 K-edge fix)

        dense.mixin.widget_ids      : one2many -> dense.widget        (depth 1)
        dense.far.mixin.widget_ids  : many2one -> dense.far.widget    (depth 2)
            (SAME field name, DIFFERENT ttype -> depth-first must pick depth 1)

        dense.variant  _inherits {dense.model: parent_id}  (DELEGATES_TO)

        dense.sub      --INHERITS--> dense.target    (subtype OK)
        dense.other    (no relation to dense.target) (MISMATCH)
        a relational field on dense.model whose comodel is dense.sub.

    Seeded once (module scope). Assertions never depend on edge counts in the
    main read tests — only the dedicated writer-invariant tests count edges.
    """
    writer = _writer()
    writer.setup_indexes()
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=DENSE_VERSION)

    def _mod(name, depends=None):
        return ModuleInfo(name, DENSE_VERSION, "odoo_test", "/tmp", depends or [], "")

    far_mod = _mod("dense_far")
    mixin_mod = _mod("dense_mixin", ["dense_far"])
    base_mod = _mod("dense_base", ["dense_mixin"])
    rel_mod = _mod("dense_rel", ["dense_base"])
    variant_mod = _mod("dense_variant", ["dense_base"])

    # --- the two mixins carrying a same-named field at different depths ---
    far_mixin = ModelInfo(
        name=_FAR_MIXIN, module="dense_far", odoo_version=DENSE_VERSION,
        had_explicit_name=True,
        fields=[FieldInfo(_TARGET_FIELD, "many2one", comodel_name="dense.far.widget")],
    )
    mixin = ModelInfo(
        name=_MIXIN, module="dense_mixin", odoo_version=DENSE_VERSION,
        had_explicit_name=True, inherit=[_FAR_MIXIN],
        fields=[FieldInfo(_TARGET_FIELD, "one2many", comodel_name="dense.widget")],
    )

    # --- the definition node: real model that INHERITS the mixin chain ---
    definition = ModelInfo(
        name=_DENSE, module="dense_base", odoo_version=DENSE_VERSION,
        had_explicit_name=True, inherit=[_MIXIN],
        fields=[FieldInfo("name", "char")],
    )

    # --- relation subtype chain ---
    rel_target = ModelInfo(
        name=_REL_TARGET, module="dense_rel", odoo_version=DENSE_VERSION,
        had_explicit_name=True, fields=[FieldInfo("code", "char")],
    )
    rel_sub = ModelInfo(
        name=_REL_SUB, module="dense_rel", odoo_version=DENSE_VERSION,
        had_explicit_name=True, inherit=[_REL_TARGET],
        fields=[FieldInfo("extra", "char")],
    )
    rel_other = ModelInfo(
        name=_REL_OTHER, module="dense_rel", odoo_version=DENSE_VERSION,
        had_explicit_name=True, fields=[FieldInfo("misc", "char")],
    )
    # dense.model carries two relational fields used by validate_relation.
    definition.fields.append(
        FieldInfo("sub_id", "many2one", comodel_name=_REL_SUB))
    definition.fields.append(
        FieldInfo("other_id", "many2one", comodel_name=_REL_OTHER))

    # --- _inherits delegation variant (terminal DELEGATES_TO hop) ---
    variant = ModelInfo(
        name=_VARIANT, module="dense_variant", odoo_version=DENSE_VERSION,
        had_explicit_name=True, inherits={_DENSE: "parent_id"},
        fields=[FieldInfo("parent_id", "many2one", comodel_name=_DENSE)],
    )

    writer.write_results([
        ParseResult(module=far_mod, models=[far_mixin]),
        ParseResult(module=mixin_mod, models=[mixin]),
        ParseResult(module=base_mod, models=[definition]),
        ParseResult(module=rel_mod, models=[rel_target, rel_sub, rel_other]),
        ParseResult(module=variant_mod, models=[variant]),
    ])

    # --- K extender modules, each adds a same-name dense.model extender ---
    ext_results = []
    for i in range(K):
        m = _mod(f"dense_ext_{i}", ["dense_base"])
        ext_model = ModelInfo(
            name=_DENSE, module=f"dense_ext_{i}", odoo_version=DENSE_VERSION,
            inherit=[_DENSE],  # self-name extension -> same-name INHERITS edge
            fields=[FieldInfo(f"ext_field_{i}", "char")],
        )
        ext_results.append(ParseResult(module=m, models=[ext_model]))
    writer.write_results(ext_results)
    writer.close()

    yield neo4j_driver
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=DENSE_VERSION)


@pytest.fixture
def orm_funcs(seeded_dense_graph):
    """Re-import the ORM helpers against the test Neo4j (underscore tools)."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.orm import (
        _lookup_field,
        _resolve_orm_chain,
        _validate_relation,
    )
    return _lookup_field, _resolve_orm_chain, _validate_relation


# ---------------------------------------------------------------------------
# 1. Dense fallback completes under bound + resolves correctly (no hang).
# ---------------------------------------------------------------------------

def test_dense_inherited_fallback_completes_and_resolves(orm_funcs):
    """resolve_orm_chain on a K-extender dense model resolves the inherited
    field WITHOUT hanging.

    Business rule: an inherited field reachable through a dense same-name mesh
    must resolve to its real type in bounded time. A multi-second wall clock
    means the issue #273 hang has returned.
    """
    _lookup_field, resolve_orm_chain, _ = orm_funcs
    start = time.monotonic()
    out = resolve_orm_chain(_DENSE, _TARGET_FIELD, DENSE_VERSION)
    elapsed = time.monotonic() - start
    assert elapsed < TIME_BOUND_S, (
        f"dense inherited fallback took {elapsed:.1f}s (>{TIME_BOUND_S}s) — "
        f"issue #273 hang regression"
    )
    # depth-1 mixin field is one2many -> dense.widget (NOT the depth-2 many2one).
    assert f"{_DENSE}.{_TARGET_FIELD} : one2many -> dense.widget (terminal)" in out, out
    assert "BROKEN" not in out


# ---------------------------------------------------------------------------
# 2. Depth-first: nearer ancestor's field shadows the farther one.
# ---------------------------------------------------------------------------

def test_depth_first_nearer_mixin_field_wins(orm_funcs):
    """When the same field name lives on a depth-1 AND a depth-2 ancestor, the
    NEARER (depth-1) definition wins (debate Decision #1, ADR depth-first).

    dense.mixin.widget_ids is one2many; dense.far.mixin.widget_ids is many2one.
    The resolved ttype must be the depth-1 one2many, proving depth-first order,
    not the alphabetical-over-the-whole-set order the old query used.
    """
    _lookup_field, _, _ = orm_funcs
    from src.mcp import server as srv
    with srv._get_driver().session() as session:
        info = _lookup_field(_DENSE, _TARGET_FIELD, DENSE_VERSION, session)
    assert info is not None, "inherited field must resolve"
    assert info["source"] == "inherited"
    assert info["ttype"] == "one2many", (
        f"depth-first must pick the depth-1 mixin's one2many, got {info['ttype']!r} "
        "(depth-2 far-mixin's many2one would mean depth ordering is broken)"
    )
    assert info["comodel"] == "dense.widget"


# ---------------------------------------------------------------------------
# 3. validate_relation OK (subtype) + MISMATCH, both under bound.
# ---------------------------------------------------------------------------

def test_validate_relation_subtype_ok_under_bound(orm_funcs):
    """A field whose comodel is a SUBTYPE of the expected target (comodel
    INHERITS target) is accepted — and the subtype walk does not hang."""
    _, _, validate_relation = orm_funcs
    start = time.monotonic()
    out = validate_relation(_DENSE, "sub_id", _REL_TARGET, DENSE_VERSION)
    elapsed = time.monotonic() - start
    assert elapsed < TIME_BOUND_S, f"subtype OK case took {elapsed:.1f}s — hang regression"
    assert "OK" in out, out
    assert "MISMATCH" not in out


def test_validate_relation_mismatch_under_bound(orm_funcs):
    """The MISMATCH case is the worst case: proving NO subtype edge exists
    requires an exhaustive negative search. It must still answer 'MISMATCH'
    in bounded time — never hang (issue #273 root case for validate_relation).
    """
    _, _, validate_relation = orm_funcs
    start = time.monotonic()
    out = validate_relation(_DENSE, "other_id", _REL_TARGET, DENSE_VERSION)
    elapsed = time.monotonic() - start
    assert elapsed < TIME_BOUND_S, (
        f"MISMATCH exhaustive-negative case took {elapsed:.1f}s — issue #273 hang regression"
    )
    assert "MISMATCH" in out, out
    assert _REL_OTHER in out  # reports the actual comodel


# ---------------------------------------------------------------------------
# 4. Issue repro shapes: (a) terminal delegated hop, (b) mixin INHERITS (case 1).
# ---------------------------------------------------------------------------

def test_repro_terminal_delegated_hop(orm_funcs):
    """A _inherits (DELEGATES_TO) child resolving an inherited field through the
    delegated parent — the 'categ_id-style' terminal-delegated repro shape.

    dense.variant _inherits dense.model; widget_ids lives on dense.model's mixin
    chain, so resolving it on dense.variant exercises a delegated terminal hop
    over the dense graph. Must complete and resolve, not hang.
    """
    _, resolve_orm_chain, _ = orm_funcs
    start = time.monotonic()
    out = resolve_orm_chain(_VARIANT, _TARGET_FIELD, DENSE_VERSION)
    elapsed = time.monotonic() - start
    assert elapsed < TIME_BOUND_S, f"delegated repro took {elapsed:.1f}s — hang regression"
    # Resolved through DELEGATES_TO -> dense.model -> INHERITS dense.mixin.
    assert f"{_VARIANT}.{_TARGET_FIELD} : one2many -> dense.widget (terminal)" in out, out
    assert "BROKEN" not in out


# ---------------------------------------------------------------------------
# 5. Cross-tenant inherited path: a scoped field on a mixin fail-closes.
#    Mirrors tests/test_cross_tenant_isolation.py (as_tenant + PG profile scope).
# ---------------------------------------------------------------------------

_PFX = "lt273_"  # prefix for collision-free PG cleanup


@contextmanager
def _as_tenant(tenant_id):
    """Pin the request tenant ContextVar (None = admin) for the block."""
    from src.mcp import session
    from src.mcp.server import _tenant_id_var

    session.invalidate_allowed_profiles()
    token = _tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id_var.reset(token)
        session.invalidate_allowed_profiles()


@pytest.fixture
def tenant_inherited_world(clean_pg_embeddings, clean_neo4j):
    """Two tenants sharing a base profile; a scoped inherited field on a mixin.

    The target field lives on a MIXIN node and is reachable only through the
    step-3 inherited fallback (no direct Field on the child model). Its Field
    node carries profile=[acme_p, base_p] so the ADR-0034 choke on the Field
    (the single tenant boundary of step 3) decides visibility.
    """
    from tests.conftest import PG_EMBED_VERSION as V

    pg = clean_pg_embeddings
    drv = clean_neo4j
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()

    with pg.cursor() as cur:
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}acme') RETURNING id")
        acme = cur.fetchone()[0]
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}globex') RETURNING id")
        globex = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO profiles (name, odoo_version) VALUES ('{_PFX}base', %s) RETURNING id",
            (V,),
        )
        cur.fetchone()
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) "
            f"VALUES ('{_PFX}acme_p', %s, %s)", (V, acme))
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) "
            f"VALUES ('{_PFX}globex_p', %s, %s)", (V, globex))
    pg.commit()

    base_p, acme_p = f"{_PFX}base", f"{_PFX}acme_p"

    # Child model + mixin (admin-visible, profile-less) but the inherited FIELD
    # carries the tenant scope so the step-3 Field choke decides visibility.
    with drv.session() as s:
        s.run("MERGE (c:Model {name:$c, module:$m, odoo_version:$v}) "
              "SET c.is_definition=true, c.had_explicit_name=true",
              c=f"{_PFX}child", m=f"{_PFX}child_mod", v=V)
        s.run("MERGE (mx:Model {name:$x, module:$m, odoo_version:$v}) "
              "SET mx.is_definition=true, mx.had_explicit_name=true",
              x=f"{_PFX}mixin", m=f"{_PFX}mixin_mod", v=V)
        s.run("MATCH (c:Model {name:$c, odoo_version:$v}) "
              "MATCH (mx:Model {name:$x, odoo_version:$v}) "
              "MERGE (c)-[:INHERITS {order:0}]->(mx)",
              c=f"{_PFX}child", x=f"{_PFX}mixin", v=V)
        # The scoped inherited field: profile=[acme_p, base_p].
        s.run("MATCH (mx:Model {name:$x, module:$m, odoo_version:$v}) "
              "MERGE (f:Field {name:$f, model:$x, module:$m, odoo_version:$v}) "
              "SET f.profile=$p, f.ttype='one2many', f.comodel_name=$co "
              "MERGE (f)-[:BELONGS_TO]->(mx)",
              x=f"{_PFX}mixin", m=f"{_PFX}mixin_mod", f="scoped_ids",
              co=f"{_PFX}line", v=V, p=[acme_p, base_p])

    yield {"acme": acme, "globex": globex, "child": f"{_PFX}child",
           "field": "scoped_ids", "v": V}

    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()


@pytest.mark.postgres
def test_inherited_field_fail_closed_for_foreign_tenant(tenant_inherited_world):
    """THE GATE: a field reachable only via the step-3 inherited fallback, whose
    Field node is scoped to acme, must be INVISIBLE to globex (fail-closed) yet
    VISIBLE to admin — proving the tenant choke on the inherited Field holds."""
    from src.mcp.orm import _lookup_field
    from src.mcp.server import _get_driver
    w = tenant_inherited_world

    with _as_tenant(w["globex"]), _get_driver().session() as session:
        denied = _lookup_field(w["child"], w["field"], w["v"], session)
    assert denied is None, f"CROSS-TENANT INHERITED LEAK: {denied!r}"

    with _as_tenant(None), _get_driver().session() as session:
        allowed = _lookup_field(w["child"], w["field"], w["v"], session)
    assert allowed is not None, "admin must resolve the inherited scoped field"
    assert allowed["source"] == "inherited"
    assert allowed["ttype"] == "one2many"


# ---------------------------------------------------------------------------
# 6. Timeout error path: a driver-set transaction timeout surfaces as a clean
#    English OrmQueryTimeout (no Cypher leaked).
# ---------------------------------------------------------------------------

def test_query_timeout_surfaces_as_ormquerytimeout(orm_funcs, monkeypatch):
    """When the Neo4j per-query timeout fires, the helper raises OrmQueryTimeout
    with a user-facing English message that does NOT leak Cypher.

    We simulate the timeout deterministically: monkeypatch session.run to raise
    the driver's TransactionTimedOutClientConfiguration ClientError, then assert
    the helper translates it to OrmQueryTimeout (NOT a raw ClientError, and not
    a hang). This protects the contract WI-2's wrapper depends on.
    """
    _lookup_field, _, _ = orm_funcs
    from neo4j.exceptions import ClientError

    from src.mcp.orm import OrmQueryTimeout

    class _TimingOutSession:
        """A session whose .run() always times out (driver-config status code)."""
        def run(self, *a, **k):
            raise ClientError(
                "The transaction has been terminated. "
                "Retry your operation in a new transaction.",
            )

    # Force the exact status code the driver returns on a per-query timeout.
    def _run(self, *a, **k):
        exc = ClientError("transaction timed out")
        exc.code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        raise exc

    monkeypatch.setattr(_TimingOutSession, "run", _run, raising=False)

    with pytest.raises(OrmQueryTimeout) as ei:
        _lookup_field(_DENSE, "no_such_field_here", DENSE_VERSION, _TimingOutSession())
    msg = ei.value.user_message
    assert "timed out" in msg.lower()
    assert "MATCH" not in msg and "Cypher" not in msg, f"Cypher leaked: {msg!r}"
    # English, actionable.
    assert "retry" in msg.lower() or "dense" in msg.lower()
    _ = _TimingOutSession  # keep referenced


# ---------------------------------------------------------------------------
# 7. Semaphore: cap holds, fast-reject on overload, slot released after worker.
#    Mirrors WI-2's standalone decorator test (tien-do.md WI-2 log).
# ---------------------------------------------------------------------------

def test_offload_bounded_caps_concurrency_and_releases():
    """offload_bounded must (a) never run more than the cap concurrently,
    (b) fast-reject the overflow as a 'busy' STRING (PR #275 review C2/MED
    isError: OrmOverloaded is now caught in the wrapper and returned as a plain
    str, uniform with the embed path + ADR-0023 — it no longer escapes as a
    protocol-level error), and (c) release every slot after the worker settles
    so the cap is fully reclaimable afterwards.

    Run with a tiny cap + short acquire timeout via env so the test is fast and
    deterministic. Uses asyncio.run (no reliance on the asyncio_mode fixture).
    """
    import importlib

    os.environ["ORM_QUERY_MAX_CONCURRENCY"] = "2"
    os.environ["ORM_SLOT_ACQUIRE_TIMEOUT"] = "0.2"
    # PR #275 review LOW SSOT: the ORM knobs moved to src.constants, so reload
    # constants FIRST for the env override to take, then server (re-imports them).
    import src.constants as consts
    importlib.reload(consts)
    import src.mcp.server as srv
    importlib.reload(srv)

    peak = 0
    current = 0
    lock = __import__("threading").Lock()

    @srv.offload_bounded
    def slow_tool(model, field, odoo_version="auto"):
        nonlocal peak, current
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.5)  # hold the slot so concurrent callers contend
        with lock:
            current -= 1
        return "done"

    async def _drive():
        # 5 concurrent calls, cap 2 -> 2 run, the rest fast-reject within 0.2s.
        tasks = [asyncio.create_task(slow_tool("m", "f", "99.0")) for _ in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    results = asyncio.run(_drive())

    served = [r for r in results if r == "done"]
    # PR #275 C2/MED isError: overload now returns a 'busy' STRING, not an
    # OrmOverloaded exception that escapes the wrapper.
    rejected = [
        r for r in results if isinstance(r, str) and "busy" in r and "retry" in r
    ]
    assert peak <= 2, f"concurrency cap breached: peak={peak} (max 2)"
    assert len(served) == 2, f"expected 2 served, got {len(served)}: {results}"
    assert len(rejected) == 3, f"expected 3 fast-rejected, got {len(rejected)}: {results}"

    # After everything settled, the full cap must be reacquirable (slots freed).
    # PR #275 C2: the semaphore is now a threading.BoundedSemaphore (slot tied to
    # the worker thread, not the coroutine), so acquire()/release() are the
    # blocking threading API, not awaitables.
    sem = srv._get_orm_semaphore()
    got = 0
    for _ in range(2):
        assert sem.acquire(timeout=1.0), "slot not released after worker completion"
        got += 1
    for _ in range(2):
        sem.release()
    assert got == 2, "slots not released after worker completion"

    # Reset env + modules so other tests see defaults (constants then server).
    os.environ.pop("ORM_QUERY_MAX_CONCURRENCY", None)
    os.environ.pop("ORM_SLOT_ACQUIRE_TIMEOUT", None)
    importlib.reload(consts)
    importlib.reload(srv)


# ---------------------------------------------------------------------------
# 8. Writer K-edge invariant: each extender has exactly ONE edge to the
#    definition (K total), NOT a K x (K-1) mesh.
# ---------------------------------------------------------------------------

def test_writer_emits_k_same_name_edges_not_mesh(seeded_dense_graph):
    """The K same-name extenders must produce K INHERITS edges to the single
    definition node (WI-3) — never the old K x (K-1) mesh that hung the tools.
    """
    drv = seeded_dense_graph
    with drv.session() as s:
        # Same-name INHERITS edges: source named dense.model, target named
        # dense.model, source is an extender (is_definition=false).
        same_name = s.run(
            """
            MATCH (ext:Model {name:$n, odoo_version:$v})-[:INHERITS]->
                  (d:Model {name:$n, odoo_version:$v})
            WHERE NOT coalesce(ext.is_definition, false)
              AND coalesce(d.is_definition, false) = true
            RETURN count(*) AS c
            """,
            n=_DENSE, v=DENSE_VERSION,
        ).single()["c"]
        # Each extender points to exactly one definition node.
        ext_count = s.run(
            "MATCH (m:Model {name:$n, odoo_version:$v}) "
            "WHERE NOT coalesce(m.is_definition, false) RETURN count(*) AS c",
            n=_DENSE, v=DENSE_VERSION,
        ).single()["c"]
    assert ext_count == K, f"expected {K} extenders, got {ext_count}"
    assert same_name == K, (
        f"expected exactly {K} same-name extender->definition edges (K-edge topology), "
        f"got {same_name} — a value near K*(K-1)={K * (K - 1)} means the K^2 mesh is back"
    )


# ---------------------------------------------------------------------------
# 9. Post-pass reconcile: deleting extender->definition edges then reconciling
#    restores them; a second run adds nothing (idempotent).
# ---------------------------------------------------------------------------

def test_reconcile_backfills_deleted_edges_idempotently(seeded_dense_graph):
    """Delete a handful of extender->definition edges, run the reconciliation
    post-pass: the edges return with a valid order, and a second run is a no-op.
    """
    drv = seeded_dense_graph

    def _same_name_edge_count(s):
        return s.run(
            """
            MATCH (ext:Model {name:$n, odoo_version:$v})-[:INHERITS]->
                  (d:Model {name:$n, odoo_version:$v})
            WHERE NOT coalesce(ext.is_definition, false)
              AND coalesce(d.is_definition, false) = true
            RETURN count(*) AS c
            """, n=_DENSE, v=DENSE_VERSION,
        ).single()["c"]

    with drv.session() as s:
        before = _same_name_edge_count(s)
        # Drop 5 extender->definition edges directly.
        s.run(
            """
            MATCH (ext:Model {name:$n, odoo_version:$v})-[r:INHERITS]->
                  (d:Model {name:$n, odoo_version:$v})
            WHERE NOT coalesce(ext.is_definition, false)
              AND coalesce(d.is_definition, false) = true
            WITH r LIMIT 5
            DELETE r
            """, n=_DENSE, v=DENSE_VERSION,
        )
        after_delete = _same_name_edge_count(s)
    assert after_delete == before - 5, "setup: 5 edges should have been deleted"

    writer = _writer()
    try:
        created = writer.reconcile_same_name_inherits(DENSE_VERSION)
        assert created == 5, f"reconcile should backfill exactly 5 edges, got {created}"
        # Second run is idempotent — no new edges.
        created_again = writer.reconcile_same_name_inherits(DENSE_VERSION)
        assert created_again == 0, f"second reconcile must be a no-op, got {created_again}"
    finally:
        writer.close()

    with drv.session() as s:
        restored = _same_name_edge_count(s)
        # All restored edges carry a non-null order (valid MRO position).
        null_orders = s.run(
            """
            MATCH (ext:Model {name:$n, odoo_version:$v})-[r:INHERITS]->
                  (d:Model {name:$n, odoo_version:$v})
            WHERE NOT coalesce(ext.is_definition, false)
              AND coalesce(d.is_definition, false) = true
              AND r.order IS NULL
            RETURN count(*) AS c
            """, n=_DENSE, v=DENSE_VERSION,
        ).single()["c"]
    assert restored == before, f"reconcile must restore to {before} edges, got {restored}"
    assert null_orders == 0, "every restored edge must carry a valid (non-null) order"


# ---------------------------------------------------------------------------
# 10. UN-CLEANED K^2 MESH (review r3 CRITICAL-1).
#
#     The fixtures above seed through the REAL writer, which emits the clean
#     K-edge topology (each extender -> the single definition). Production at
#     deploy time is DIFFERENT: it still carries the legacy K x (K-1) same-name
#     mesh that the cleanup script has not yet removed — exactly the "new code x
#     old data" cell of the ADR-0048 D8 rollout matrix where review r3 measured
#     12.6s..TIMEOUT against the FIRST per-hop rewrite.
#
#     Root cause of that regression: the first cut applied `pn <> $mn` only at
#     the final WHERE, so hop1 still collected $mn (same-name edges) and
#     hop2/hop3 re-expanded from ALL K mesh nodes; and the K-row anchor ran each
#     per-hop CALL once-per-row. These tests seed the mesh DIRECTLY via Cypher
#     (NOT the writer) so they reproduce that production shape, and assert the
#     fixed query (prune-during-expansion + single-row hop aggregation) both
#     returns the right answer AND finishes far under a tight bound — a bound
#     small enough to catch a re-introduction of the mesh re-expansion.
# ---------------------------------------------------------------------------

# Dedicated version so this mesh never collides with DENSE_VERSION/"99.0".
MESH_VERSION = "99.6"
# K large enough that re-expanding the full K x (K-1) mesh would blow far past
# MESH_TIME_BOUND_S, small enough to seed quickly.
MESH_K = 120
# A TIGHT ceiling: the fixed query traverses only cross-name edges (a handful)
# even on this mesh, so it finishes in well under a second. A multi-second
# result means the same-name mesh re-expansion has returned. Deliberately far
# tighter than TIME_BOUND_S (20s) so this test is the tripwire, not the 30s
# Neo4j query timeout.
MESH_TIME_BOUND_S = 5.0

_MESH_MODEL = "mesh.model"        # the K-duplicated same-name model
_MESH_MIXIN = "mesh.mixin"        # cross-name mixin carrying the inherited field
_MESH_FIELD = "mesh_widget_ids"   # field living ONLY on the mixin
_MESH_REL_TARGET = "mesh.target"  # validate_relation expected comodel
_MESH_REL_OTHER = "mesh.other"    # comodel with NO path to target -> MISMATCH


@pytest.fixture(scope="module")
def mesh_graph(neo4j_driver):
    """Seed an UN-CLEANED K^2 same-name mesh directly via Cypher.

    Topology (all at MESH_VERSION):

        mesh.model x MESH_K  (1 definition + (K-1) extenders, all same name)
            -- FULL K x (K-1) INHERITS mesh among them (the legacy un-cleaned
               shape: every same-name node points at every other) --
        definition node --INHERITS--> mesh.mixin   (the only cross-name edge)
        mesh.mixin.mesh_widget_ids : one2many -> mesh.line   (the inherited field)

        mesh.model (definition) field other_id : many2one -> mesh.other
        mesh.other  : no INHERITS path to mesh.target  -> validate_relation MISMATCH

    No profiles set -> admin-visible ($own IS NULL path), so _scope_pred passes.
    """
    v = MESH_VERSION
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)

        # K same-name mesh.model nodes; index 0 is the definition.
        s.run(
            """
            UNWIND range(0, $k - 1) AS i
            CREATE (m:Model {name:$n, module:'mesh_' + toString(i), odoo_version:$v,
                             is_definition: (i = 0), had_explicit_name: true})
            """,
            n=_MESH_MODEL, k=MESH_K, v=v,
        )
        # FULL K x (K-1) same-name INHERITS mesh (the un-cleaned legacy shape).
        s.run(
            """
            MATCH (a:Model {name:$n, odoo_version:$v})
            MATCH (b:Model {name:$n, odoo_version:$v})
            WHERE a.module <> b.module
            CREATE (a)-[:INHERITS {order:0}]->(b)
            """,
            n=_MESH_MODEL, v=v,
        )
        # The single cross-name edge: definition -> mixin carrying the field.
        s.run(
            """
            MERGE (mx:Model {name:$x, module:'mesh_mixin_mod', odoo_version:$v})
              ON CREATE SET mx.is_definition=true, mx.had_explicit_name=true
            WITH mx
            MATCH (d:Model {name:$n, odoo_version:$v}) WHERE d.is_definition = true
            MERGE (d)-[:INHERITS {order:0}]->(mx)
            """,
            n=_MESH_MODEL, x=_MESH_MIXIN, v=v,
        )
        # The inherited field lives ONLY on the mixin.
        s.run(
            """
            MATCH (mx:Model {name:$x, odoo_version:$v})
            MERGE (f:Field {name:$f, model:$x, module:'mesh_mixin_mod', odoo_version:$v})
              ON CREATE SET f.ttype='one2many', f.comodel_name='mesh.line'
            MERGE (f)-[:BELONGS_TO]->(mx)
            """,
            x=_MESH_MIXIN, f=_MESH_FIELD, v=v,
        )
        # Relation chain: a m2o field on the definition pointing at mesh.other,
        # which has NO INHERITS path to mesh.target -> the exhaustive-negative
        # MISMATCH case (review r3's res.users->res.partner shape).
        s.run(
            """
            MERGE (t:Model {name:$t, module:'mesh_rel', odoo_version:$v})
              ON CREATE SET t.is_definition=true, t.had_explicit_name=true
            MERGE (o:Model {name:$o, module:'mesh_rel', odoo_version:$v})
              ON CREATE SET o.is_definition=true, o.had_explicit_name=true
            WITH o
            MATCH (d:Model {name:$n, odoo_version:$v}) WHERE d.is_definition = true
            MERGE (rf:Field {name:'other_id', model:$n, module:'mesh_0', odoo_version:$v})
              ON CREATE SET rf.ttype='many2one', rf.comodel_name=$o
            MERGE (rf)-[:BELONGS_TO]->(d)
            """,
            n=_MESH_MODEL, t=_MESH_REL_TARGET, o=_MESH_REL_OTHER, v=v,
        )

    yield neo4j_driver
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)


@pytest.fixture
def mesh_funcs(mesh_graph):
    """ORM helpers bound to the test Neo4j, for the mesh tests."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.orm import _lookup_field, _validate_relation
    return _lookup_field, _validate_relation


def test_lookup_field_on_uncleaned_mesh_resolves_under_tight_bound(mesh_funcs, capsys):
    """_lookup_field must resolve the inherited field through the SINGLE
    cross-name edge even on the un-cleaned K^2 same-name mesh, far under a
    tight bound.

    Regression intent (review r3 C1): the first per-hop rewrite re-expanded the
    full K x (K-1) mesh (`pn <> $mn` only at the end; K-row anchor x per-row
    CALL) -> 12.6s..TIMEOUT on prod. With prune-during-expansion + single-row
    hop aggregation the BFS only crosses the one cross-name edge.
    """
    _lookup_field, _ = mesh_funcs
    from src.mcp import server as srv
    start = time.monotonic()
    with srv._get_driver().session() as session:
        info = _lookup_field(_MESH_MODEL, _MESH_FIELD, MESH_VERSION, session)
    elapsed = time.monotonic() - start
    with capsys.disabled():
        print(f"\n[mesh K={MESH_K}] _lookup_field inherited resolve: {elapsed * 1000:.0f} ms")
    assert info is not None, "inherited field must resolve through the cross-name edge"
    assert info["source"] == "inherited"
    assert info["ttype"] == "one2many"
    assert info["comodel"] == "mesh.line"
    assert elapsed < MESH_TIME_BOUND_S, (
        f"_lookup_field on un-cleaned K^2 mesh took {elapsed:.1f}s "
        f"(>{MESH_TIME_BOUND_S}s) — same-name mesh re-expansion regression (review r3 C1)"
    )


def test_validate_relation_mismatch_on_uncleaned_mesh_under_tight_bound(mesh_funcs, capsys):
    """validate_relation MISMATCH (exhaustive-negative, TEST GAP #7) on the
    un-cleaned K^2 mesh: proving NO subtype edge exists is the worst case
    because it walks all 5 hops. It must answer MISMATCH far under a tight
    bound, never re-expanding the same-name mesh.

    This is the exact interim deploy window review r3 measured as TIMEOUT for
    res.users -> res.partner before the cleanup script runs.
    """
    _, validate_relation = mesh_funcs
    start = time.monotonic()
    out = validate_relation(_MESH_MODEL, "other_id", _MESH_REL_TARGET, MESH_VERSION)
    elapsed = time.monotonic() - start
    with capsys.disabled():
        print(f"[mesh K={MESH_K}] validate_relation MISMATCH: {elapsed * 1000:.0f} ms")
    assert "MISMATCH" in out, out
    assert _MESH_REL_OTHER in out, out  # reports the actual comodel
    assert elapsed < MESH_TIME_BOUND_S, (
        f"validate_relation MISMATCH on un-cleaned K^2 mesh took {elapsed:.1f}s "
        f"(>{MESH_TIME_BOUND_S}s) — exhaustive-negative mesh re-expansion regression "
        f"(review r3 C1 + TEST GAP #7)"
    )
