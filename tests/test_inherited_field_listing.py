# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_inherited_field_listing.py
"""WI-3 — inherited field/method listing coverage (10 cases).

Business rules protected (ETHOS #11 - test BEHAVIOUR, not implementation):

  T1. model_inspect(fields) on a child model MUST list fields declared on its
      mixin parents ("inherited from <mixin>" provenance).
  T2. entity_lookup(field) on an inherited field MUST NOT return "not found" —
      it must return detail with "Inherited from:" provenance.
  T3. When child AND mixin both declare the same field name, the child's version
      (depth-0, different ttype) MUST shadow the mixin's.
  T4. A model with no INHERITS edge must return only its own fields — no
      regression, no phantom "inherited from" tokens.
  T5. A field reachable only through the inherited fallback and scoped to
      tenant A must be INVISIBLE to tenant B (ADR-0034 fail-closed). Admin
      must see it.
  T6. The list path must complete in bounded time on a K~30 same-name mesh plus
      one mixin (anti-explosion guard, ADR-0048 shape D2).
  T7. The field count in model summary ("Fields: N") must equal the total
      returned by _list_fields across pages — summary and list must not diverge
      after inherited fields are included.
  T8. method_listing and method_detail on a child model MUST include methods
      declared on its mixin parents — symmetric to fields (T1/T2).
  T9. The provenance token for DELEGATES_TO-derived fields must say
      "delegated via" not "inherited from" — the two edge kinds are distinct.
  T10. _list_fields on a dense graph (K~30 same-name extenders + 7 mixins,
       each ~50 fields) completes within a time tripwire — account.move-scale
       perf guard.

All tests use TEST_VERSION="99.0" with fresh synthetic graph data seeded
directly through the Neo4j driver (no Neo4jWriter needed — simpler + faster).
T5 additionally requires Postgres (tenant profile scoping).

These tests MUST BE RED on the current branch (WI-1 helper exists, but
WI-2 wiring into server.py _list_fields/_resolve_field/_list_methods/
_resolve_method has NOT been done yet). T4/T6/T10 are regression/perf guards
and may be green immediately.

Requires Neo4j (testcontainers — pytest mark neo4j).
"""
import os
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.neo4j

# ─── version / naming ────────────────────────────────────────────────────────
TEST_VERSION = "99.0"

# model names (collision-safe with "99.0" + clean_neo4j teardown)
_CHILD   = "t_inh.child"
_MIXIN   = "t_inh.mixin"
# modules
_CHILD_MOD  = "t_inh_child_mod"
_MIXIN_MOD  = "t_inh_mixin_mod"

# Field names
_ONLY_MIXIN_FIELD  = "res_ref_t"      # exists only on mixin — key regression field
_ONLY_CHILD_FIELD  = "child_own_f"    # exists only on child
_SHARED_FIELD      = "shared_name_f"  # declared on BOTH child (char) and mixin (integer)
_SHARED_CHILD_TYPE = "char"
_SHARED_MIXIN_TYPE = "integer"

# Method names
_ONLY_MIXIN_METHOD = "compute_res_ref_t"   # exists only on mixin
_ONLY_CHILD_METHOD = "child_own_method"    # exists only on child

# delegation test model names
_DELEGATE_PARENT = "t_inh.delegate_parent"
_DELEGATE_CHILD  = "t_inh.delegate_child"
_DELEGATE_FIELD  = "delegated_field_t"

# perf test constants
_PERF_BASE   = "t_inh.perf_child"
_PERF_MESH_PREFIX = "t_inh.perf_ext_"   # same-name extenders of _PERF_BASE
K_PERF   = 30   # number of same-name extenders (account.move-scale proxy)
N_MIXINS = 7    # mixin models attached to _PERF_BASE
FIELDS_PER_MIXIN = 50
PERF_TRIPWIRE_S = 5.0   # list must complete well inside this bound

# tenant-isolation prefix
_PFX = "winh_"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _seed_basic_world(drv):
    """Seed the minimal child→mixin world for T1-T4.

    Graph (all at TEST_VERSION):
        t_inh.child (is_definition, had_explicit_name)
            --INHERITS {order:0}--> t_inh.mixin (is_definition)

    Fields on mixin:
        res_ref_t   : reference
        shared_name_f : integer  (mixin version)

    Fields on child:
        child_own_f  : char
        shared_name_f: char      (child version — overrides mixin)

    Methods on mixin:
        compute_res_ref_t : plain
        shared_name_f (same name as field, different obj type — valid in Odoo)

    Methods on child:
        child_own_method : plain
    """
    with drv.session() as s:
        # Models
        s.run(
            "MERGE (c:Model {name:$n, module:$m, odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=_CHILD, m=_CHILD_MOD, v=TEST_VERSION,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:$m, odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=_MIXIN, m=_MIXIN_MOD, v=TEST_VERSION,
        )
        # INHERITS edge
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
            "MERGE (c)-[:INHERITS {order:0}]->(mx)",
            c=_CHILD, mx=_MIXIN, v=TEST_VERSION,
        )

        # Fields on mixin
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='reference', f.profile=[] "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            fn=_ONLY_MIXIN_FIELD, m=_MIXIN, mod=_MIXIN_MOD, v=TEST_VERSION,
        )
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype=$t, f.profile=[] "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            fn=_SHARED_FIELD, m=_MIXIN, mod=_MIXIN_MOD,
            t=_SHARED_MIXIN_TYPE, v=TEST_VERSION,
        )

        # Fields on child
        s.run(
            "MATCH (c:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='char', f.profile=[] "
            "MERGE (f)-[:BELONGS_TO]->(c)",
            fn=_ONLY_CHILD_FIELD, m=_CHILD, mod=_CHILD_MOD, v=TEST_VERSION,
        )
        s.run(
            "MATCH (c:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype=$t, f.profile=[] "
            "MERGE (f)-[:BELONGS_TO]->(c)",
            fn=_SHARED_FIELD, m=_CHILD, mod=_CHILD_MOD,
            t=_SHARED_CHILD_TYPE, v=TEST_VERSION,
        )

        # Methods on mixin
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (mth:Method {name:$mn, model:$m, module:$mod, odoo_version:$v}) "
            "SET mth.convention_kind='plain', mth.profile=[] "
            "MERGE (mth)-[:DEFINED_IN_METHOD]->(mx)",
            mn=_ONLY_MIXIN_METHOD, m=_MIXIN, mod=_MIXIN_MOD, v=TEST_VERSION,
        )

        # Methods on child
        s.run(
            "MATCH (c:Model {name:$m, odoo_version:$v}) "
            "MERGE (mth:Method {name:$mn, model:$m, module:$mod, odoo_version:$v}) "
            "SET mth.convention_kind='plain', mth.profile=[] "
            "MERGE (mth)-[:DEFINED_IN_METHOD]->(c)",
            mn=_ONLY_CHILD_METHOD, m=_CHILD, mod=_CHILD_MOD, v=TEST_VERSION,
        )

        # Module nodes (needed for DEFINED_IN edge used by _resolve_model)
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
            n=_CHILD_MOD, v=TEST_VERSION,
        )
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
            n=_MIXIN_MOD, v=TEST_VERSION,
        )
        # DEFINED_IN edges (required for _resolve_model to find the model)
        s.run(
            "MATCH (c:Model {name:$mn, odoo_version:$v}) "
            "MATCH (m:Module {name:$mod, odoo_version:$v}) "
            "MERGE (c)-[:DEFINED_IN]->(m)",
            mn=_CHILD, mod=_CHILD_MOD, v=TEST_VERSION,
        )
        s.run(
            "MATCH (c:Model {name:$mn, odoo_version:$v}) "
            "MATCH (m:Module {name:$mod, odoo_version:$v}) "
            "MERGE (c)-[:DEFINED_IN]->(m)",
            mn=_MIXIN, mod=_MIXIN_MOD, v=TEST_VERSION,
        )


def _seed_delegation_world(drv):
    """Seed a DELEGATES_TO parent-child pair for T9.

    t_inh.delegate_child -[:DELEGATES_TO]-> t_inh.delegate_parent
        delegated_field_t: char (on parent)
    """
    with drv.session() as s:
        s.run(
            "MERGE (p:Model {name:$n, module:'t_inh_del_p', odoo_version:$v}) "
            "SET p.is_definition=true, p.had_explicit_name=true",
            n=_DELEGATE_PARENT, v=TEST_VERSION,
        )
        s.run(
            "MERGE (c:Model {name:$n, module:'t_inh_del_c', odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=_DELEGATE_CHILD, v=TEST_VERSION,
        )
        # via_field mirrors how writer_neo4j.py seeds DELEGATES_TO edges:
        # MERGE (m)-[:DELEGATES_TO {via_field: $via_field}]->(d)
        # ADR-0023: label must read "delegated via <field> from <owner>".
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (p:Model {name:$p, odoo_version:$v}) "
            "MERGE (c)-[:DELEGATES_TO {via_field: 'delegate_link_id'}]->(p)",
            c=_DELEGATE_CHILD, p=_DELEGATE_PARENT, v=TEST_VERSION,
        )
        s.run(
            "MATCH (p:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:'t_inh_del_p', odoo_version:$v}) "
            "SET f.ttype='char', f.profile=[] "
            "MERGE (f)-[:BELONGS_TO]->(p)",
            fn=_DELEGATE_FIELD, m=_DELEGATE_PARENT, v=TEST_VERSION,
        )
        s.run(
            "MERGE (mod:Module {name:'t_inh_del_p', odoo_version:$v}) "
            "SET mod.edition='community'",
            v=TEST_VERSION,
        )
        s.run(
            "MERGE (mod:Module {name:'t_inh_del_c', odoo_version:$v}) "
            "SET mod.edition='community'",
            v=TEST_VERSION,
        )


def _seed_perf_world(drv):
    """Seed an account.move-scale graph for T10.

    _PERF_BASE (is_definition) --INHERITS--> N_MIXINS mixin models
        K_PERF same-name extender models (is_definition=false) EACH --INHERITS--> _PERF_BASE

    Each mixin carries FIELDS_PER_MIXIN char fields.
    _PERF_BASE has 5 own fields.
    """
    v = TEST_VERSION
    with drv.session() as s:
        # Base model
        s.run(
            "MERGE (c:Model {name:$n, module:'t_inh_perf_base', odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=_PERF_BASE, v=v,
        )
        s.run(
            "MERGE (mod:Module {name:'t_inh_perf_base', odoo_version:$v}) "
            "SET mod.edition='community'",
            v=v,
        )
        # Own fields on base
        for i in range(5):
            s.run(
                "MATCH (c:Model {name:$m, odoo_version:$v}) "
                "MERGE (f:Field {name:$fn, model:$m, module:'t_inh_perf_base', odoo_version:$v}) "
                "SET f.ttype='char', f.profile=[] MERGE (f)-[:BELONGS_TO]->(c)",
                fn=f"own_f_{i}", m=_PERF_BASE, v=v,
            )

        # N_MIXINS mixin models, each with FIELDS_PER_MIXIN fields
        for mi in range(N_MIXINS):
            mx_name = f"t_inh.perf_mixin_{mi}"
            mx_mod  = f"t_inh_perf_mixin_{mi}"
            s.run(
                "MERGE (mx:Model {name:$n, module:$mod, odoo_version:$v}) "
                "SET mx.is_definition=true, mx.had_explicit_name=true",
                n=mx_name, mod=mx_mod, v=v,
            )
            s.run(
                "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
                n=mx_mod, v=v,
            )
            # INHERITS edge from base to mixin
            s.run(
                "MATCH (c:Model {name:$c, odoo_version:$v}) "
                "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
                "MERGE (c)-[:INHERITS {order:$o}]->(mx)",
                c=_PERF_BASE, mx=mx_name, o=mi, v=v,
            )
            for fi in range(FIELDS_PER_MIXIN):
                s.run(
                    "MATCH (mx:Model {name:$m, odoo_version:$v}) "
                    "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
                    "SET f.ttype='char', f.profile=[] MERGE (f)-[:BELONGS_TO]->(mx)",
                    fn=f"mx{mi}_field_{fi}", m=mx_name, mod=mx_mod, v=v,
                )

        # K_PERF same-name extenders of _PERF_BASE
        for ki in range(K_PERF):
            ext_mod = f"t_inh_perf_ext_{ki}"
            s.run(
                "MERGE (e:Model {name:$n, module:$mod, odoo_version:$v}) "
                "SET e.is_definition=false",
                n=_PERF_BASE, mod=ext_mod, v=v,
            )
            s.run(
                "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
                n=ext_mod, v=v,
            )
            # Extender --INHERITS--> definition (K×D topology from ADR-0048)
            s.run(
                "MATCH (ext:Model {name:$n, module:$mod, odoo_version:$v}) "
                "MATCH (def:Model {name:$n2, module:'t_inh_perf_base', odoo_version:$v}) "
                "MERGE (ext)-[:INHERITS {order:0}]->(def)",
                n=_PERF_BASE, mod=ext_mod, n2=_PERF_BASE, v=v,
            )


# ─── server.py env wiring ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _wire_neo4j_env():
    """Point server.py helpers at the test Neo4j container."""
    os.environ["NEO4J_URI"]      = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"]     = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")


# ─── T1 — list includes mixin-inherited field ─────────────────────────────────

def test_list_fields_includes_mixin_inherited_field(clean_neo4j):
    """model_inspect(fields) on child MUST list fields from its mixin ancestors.

    Business rule: 'res_ref_t' is declared only on t_inh.mixin. When asking for
    fields on t_inh.child (which inherits t_inh.mixin), the field must appear
    with provenance 'inherited from t_inh.mixin'.
    RED on WI-1-only branch (wiring not done). GREEN after WI-2.
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    out = _list_fields(_CHILD, odoo_version=TEST_VERSION)

    assert _ONLY_MIXIN_FIELD in out, (
        f"Inherited field '{_ONLY_MIXIN_FIELD}' missing from list output.\n"
        f"output:\n{out}"
    )
    # Provenance token: "inherited from t_inh.mixin" (or substring)
    assert "inherited from" in out.lower(), (
        f"Expected 'inherited from' provenance token in output.\noutput:\n{out}"
    )


# ─── T2 — resolve inherited field — not "not found" ──────────────────────────

def test_resolve_field_resolves_mixin_field(clean_neo4j):
    """entity_lookup(field) on an inherited field must NOT return 'not found'.

    Business rule: 'res_ref_t' is inherited from t_inh.mixin via INHERITS edge.
    The detail view must resolve it with 'Inherited from:' provenance.
    RED on WI-1-only branch.
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _resolve_field

    out = _resolve_field(_CHILD, _ONLY_MIXIN_FIELD, odoo_version=TEST_VERSION)

    assert "not found" not in out.lower(), (
        f"_resolve_field returned 'not found' for an inherited field — "
        f"inherited fallback not wired.\noutput:\n{out}"
    )
    assert "Inherited from:" in out, (
        f"Expected 'Inherited from:' provenance line in field detail.\noutput:\n{out}"
    )


# ─── T3 — child override shadows mixin field (same name, different type) ──────

def test_child_override_shadows_mixin_field(clean_neo4j):
    """When child AND mixin both declare the same field name, child wins (depth-0).

    Business rule (ADR-0048 depth-first): 'shared_name_f' declared as 'char' on
    child and 'integer' on mixin — the list must include the mixin-only field
    'res_ref_t' (proving traversal reached the mixin) AND must show 'char' for
    'shared_name_f' (child wins), never 'integer' (mixin type suppressed by dedup).

    Two invariants:
      1. Traversal reached the mixin: res_ref_t (mixin-only) must appear.
      2. Depth-first dedup: no line for shared_name_f should carry 'integer'.

    Both invariants require WI-2 wiring — RED on WI-1-only branch because
    invariant 1 fails (res_ref_t missing from flat-MATCH output).
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    out = _list_fields(_CHILD, odoo_version=TEST_VERSION)

    # Invariant 1: traversal reached the mixin (prerequisite for dedup test)
    assert _ONLY_MIXIN_FIELD in out, (
        f"Mixin-only field '{_ONLY_MIXIN_FIELD}' missing — traversal not wired. "
        f"Cannot verify override dedup without mixin traversal.\noutput:\n{out}"
    )

    # Invariant 2: child's char version of shared_name_f must appear
    assert _SHARED_CHILD_TYPE in out, (
        f"Expected child type '{_SHARED_CHILD_TYPE}' for '{_SHARED_FIELD}'.\noutput:\n{out}"
    )

    # Invariant 3: no line for shared_name_f should carry 'integer' (mixin type)
    # (The magic 'id' field is also integer, so we check only shared_name_f lines.)
    lines_with_shared = [
        ln for ln in out.splitlines() if _SHARED_FIELD in ln
    ]
    for ln in lines_with_shared:
        assert _SHARED_MIXIN_TYPE not in ln, (
            f"Mixin type '{_SHARED_MIXIN_TYPE}' found for '{_SHARED_FIELD}' — "
            f"depth-first dedup broken. Offending line: {ln!r}\nfull output:\n{out}"
        )


# ─── T4 — no-inherit model unchanged (regression guard) ─────────────────────

def test_no_inherit_model_unchanged(clean_neo4j):
    """A model with no INHERITS edges must return only its own fields.

    Regression guard: the new traversal must not affect models with no mixin.
    'child_own_f' must appear; no 'inherited from' token must appear for a
    standalone model (t_inh.mixin has no parent, so listing its fields must
    return only its own fields without inherited tokens).
    Expected GREEN even on WI-1-only branch.
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    # t_inh.mixin has NO parent INHERITS edges — its own fields only
    out = _list_fields(_MIXIN, odoo_version=TEST_VERSION)

    assert _ONLY_MIXIN_FIELD in out, (
        f"Own field '{_ONLY_MIXIN_FIELD}' missing from mixin's own field list.\n"
        f"output:\n{out}"
    )
    # No inherited provenance for a leaf model
    assert "inherited from" not in out.lower(), (
        f"Unexpected 'inherited from' token on a leaf model (no INHERITS edge).\n"
        f"output:\n{out}"
    )


# ─── T5 — inherited field fail-closed for foreign tenant (ADR-0034) ──────────

@pytest.mark.postgres
def test_inherited_field_fail_closed_for_foreign_tenant(clean_pg_embeddings, clean_neo4j):
    """Inherited field scoped to tenant A must be invisible to tenant B.

    Business rule (ADR-0034): the INHERITS traversal must not bypass the tenant
    choke on the Field node. Field 'res_ref_t' scoped to acme_p must be
    invisible when the request tenant is globex. Admin (None tenant) must see it.
    RED on WI-1-only branch (traversal not wired into _list_fields).
    """
    from tests.conftest import PG_EMBED_VERSION as V

    pg  = clean_pg_embeddings
    drv = clean_neo4j

    # Clean any leftover PG rows from a previous interrupted run
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()

    # Provision two tenants + three profiles
    with pg.cursor() as cur:
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}acme') RETURNING id")
        acme_id = cur.fetchone()[0]
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}globex') RETURNING id")
        globex_id = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO profiles (name, odoo_version) VALUES ('{_PFX}base', %s)",
            (V,),
        )
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) "
            f"VALUES ('{_PFX}acme_p', %s, %s)", (V, acme_id),
        )
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) "
            f"VALUES ('{_PFX}globex_p', %s, %s)", (V, globex_id),
        )
    pg.commit()

    base_p = f"{_PFX}base"
    acme_p = f"{_PFX}acme_p"

    # Seed child → mixin with the mixin field scoped to [acme_p, base_p]
    child_n = f"{_PFX}child_t5"
    mixin_n = f"{_PFX}mixin_t5"
    with drv.session() as s:
        s.run(
            "MERGE (c:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=child_n, mod=f"{_PFX}child_mod_t5", v=V,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=mixin_n, mod=f"{_PFX}mixin_mod_t5", v=V,
        )
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
            "MERGE (c)-[:INHERITS {order:0}]->(mx)",
            c=child_n, mx=mixin_n, v=V,
        )
        # Scoped field: visible only to acme
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:'scoped_inh_f', model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='char', f.profile=$p "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            m=mixin_n, mod=f"{_PFX}mixin_mod_t5", v=V,
            p=[acme_p, base_p],
        )

    @contextmanager
    def _as_tenant(tenant_id):
        from src.mcp import session as mcp_session
        from src.mcp.server import _tenant_id_var
        mcp_session.invalidate_allowed_profiles()
        token = _tenant_id_var.set(tenant_id)
        try:
            yield
        finally:
            _tenant_id_var.reset(token)
            mcp_session.invalidate_allowed_profiles()

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    # Globex must NOT see the field
    with _as_tenant(globex_id):
        out_globex = _list_fields(child_n, odoo_version=V)
    assert "scoped_inh_f" not in out_globex, (
        f"CROSS-TENANT INHERITED LEAK: globex sees 'scoped_inh_f' via inheritance.\n"
        f"output:\n{out_globex}"
    )

    # Admin (None tenant) MUST see it
    with _as_tenant(None):
        out_admin = _list_fields(child_n, odoo_version=V)
    assert "scoped_inh_f" in out_admin, (
        f"Admin must see inherited scoped field, but it was hidden.\n"
        f"output:\n{out_admin}"
    )

    # Teardown PG rows
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()


# ─── T6 — K~30 same-name mesh + mixin — bounded, no explosion ────────────────

def test_list_fields_inherited_bounded_no_explosion(clean_neo4j):
    """_list_fields on a K=30 same-name mesh + mixin must complete within tripwire.

    Regression guard against issue #273 path-explosion: the per-hop name-dedup
    shape must prevent combinatorial blowup. The time bound is a tripwire, not a
    benchmark. Expected GREEN on WI-1-only branch (flat MATCH is fast; the guard
    protects against regression after WI-2 wiring).
    """
    drv = clean_neo4j
    K = 30
    base_n   = "t_inh.t6_base"
    mixin_n  = "t_inh.t6_mixin"
    base_mod = "t_inh_t6_base"
    mix_mod  = "t_inh_t6_mix"
    v = TEST_VERSION

    with drv.session() as s:
        s.run(
            "MERGE (m:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET m.is_definition=true, m.had_explicit_name=true",
            n=base_n, mod=base_mod, v=v,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=mixin_n, mod=mix_mod, v=v,
        )
        s.run(
            "MATCH (b:Model {name:$b, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
            "MERGE (b)-[:INHERITS {order:0}]->(mx)",
            b=base_n, mx=mixin_n, v=v,
        )
        # Mixin field
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:'mx_flag', model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='boolean', f.profile=[] MERGE (f)-[:BELONGS_TO]->(mx)",
            m=mixin_n, mod=mix_mod, v=v,
        )
        # K same-name extenders of base_n
        for ki in range(K):
            ext_mod = f"t_inh_t6_ext_{ki}"
            s.run(
                "MERGE (e:Model {name:$n, module:$mod, odoo_version:$v}) "
                "SET e.is_definition=false",
                n=base_n, mod=ext_mod, v=v,
            )
            # Extender -> definition (K×D, not K^2)
            s.run(
                "MATCH (ext:Model {name:$n, module:$mod, odoo_version:$v}) "
                "MATCH (def:Model {name:$n2, module:$base_mod, odoo_version:$v}) "
                "MERGE (ext)-[:INHERITS {order:0}]->(def)",
                n=base_n, mod=ext_mod, n2=base_n, base_mod=base_mod, v=v,
            )
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
            n=base_mod, v=v,
        )
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
            n=mix_mod, v=v,
        )

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    start = time.monotonic()
    out = _list_fields(base_n, odoo_version=v)
    elapsed = time.monotonic() - start

    assert elapsed < PERF_TRIPWIRE_S, (
        f"_list_fields on K={K} same-name mesh took {elapsed:.2f}s > "
        f"{PERF_TRIPWIRE_S}s tripwire — issue #273 explosion regression."
    )
    _ = out  # result consumed


# ─── T7 — field count consistency: summary == list total ─────────────────────

def test_field_count_matches_list_total(clean_neo4j):
    """Summary 'Fields: N' from _resolve_model must match paginated list total.

    Business rule: the two counts must be consistent. After WI-2 wiring, both
    must include inherited fields. Before WI-2, summary is flat-count (~2 own
    fields) and list is also flat — they're consistent at a LOWER number. After
    WI-2, both must reflect the inherited total. This test is RED before WI-2
    because after wiring the list grows but the summary counter stays flat,
    causing divergence — the test catches that mismatch.
    RED on WI-1-only branch (both counts consistent only if both use flat-match
    OR both use traversal; mismatched counts = red).
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import re
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields, _resolve_model

    # Get summary count from _resolve_model output ("Fields: N")
    summary_out = _resolve_model(_CHILD, odoo_version=TEST_VERSION)
    m = re.search(r"Fields:\s*(\d+)", summary_out)
    assert m, f"Could not find 'Fields: N' in _resolve_model output:\n{summary_out}"
    summary_count = int(m.group(1))

    # Paginate _list_fields to get the true total from the output's "of N" line
    # The output contains "Showing X-Y of N" — extract N; if absent, count rows.
    list_out = _list_fields(_CHILD, odoo_version=TEST_VERSION, limit=1000)
    total_m = re.search(r"of\s+(\d+)", list_out)
    if total_m:
        list_total = int(total_m.group(1))
    else:
        # Fallback: count lines that are INDEXED field rows (contain " : " AND
        # carry a "[ref=" ref-id prefix). Magic implicit fields in the <builtin>
        # group are injected at render time WITHOUT a [ref=] tag — they are not
        # indexed and summary "Fields: N" never counts them (ADR-0023 §3).
        # Counting only [ref=-prefixed lines keeps the measurement aligned with
        # the indexed-field set that both summary and list must agree on.
        indexed_field_lines = [
            ln for ln in list_out.splitlines()
            if " : " in ln and "[ref=" in ln
        ]
        list_total = len(indexed_field_lines)

    # The two counts MUST agree
    assert summary_count == list_total, (
        f"Summary count ({summary_count}) != list total ({list_total}). "
        f"Summary and list field counts are out of sync — "
        f"both must use the same traversal (own + inherited).\n"
        f"summary output:\n{summary_out}\n"
        f"list output:\n{list_out}"
    )


# ─── T8 — method listing symmetric to field listing ──────────────────────────

def test_method_listing_includes_inherited_method(clean_neo4j):
    """model_inspect(methods) on child MUST include methods from mixin ancestors.

    Business rule (symmetry with T1/T2): 'compute_res_ref_t' is declared only
    on t_inh.mixin. When listing methods on t_inh.child, it must appear with
    an 'inherited from' provenance token. Also, _resolve_method must not return
    'not found' for it.
    RED on WI-1-only branch (method wiring not done).
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_methods, _resolve_method

    # List: inherited method must appear
    list_out = _list_methods(_CHILD, odoo_version=TEST_VERSION)
    assert _ONLY_MIXIN_METHOD in list_out, (
        f"Inherited method '{_ONLY_MIXIN_METHOD}' missing from list output.\n"
        f"output:\n{list_out}"
    )
    assert "inherited from" in list_out.lower(), (
        f"Expected 'inherited from' provenance in method list.\noutput:\n{list_out}"
    )

    # Detail: must NOT be "not found"
    detail_out = _resolve_method(_CHILD, _ONLY_MIXIN_METHOD, odoo_version=TEST_VERSION)
    assert "not found" not in detail_out.lower(), (
        f"_resolve_method returned 'not found' for inherited method — "
        f"inherited fallback not wired.\noutput:\n{detail_out}"
    )
    assert "Inherited from:" in detail_out, (
        f"Expected 'Inherited from:' in method detail.\noutput:\n{detail_out}"
    )


# ─── T9 — DELEGATES_TO label distinct from INHERITS label ────────────────────

def test_delegates_label_distinct_from_inherits(clean_neo4j):
    """Fields reached via DELEGATES_TO must show 'delegated via' not 'inherited from'.

    Business rule (P4 Truc 3): the two edge types represent different Odoo
    semantics (_inherit mixin vs _inherits delegation). The provenance token
    must distinguish them so AI clients can reason about the difference.
    RED on WI-1-only branch (wiring not done; field not in output at all).
    """
    drv = clean_neo4j
    _seed_delegation_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    out = _list_fields(_DELEGATE_CHILD, odoo_version=TEST_VERSION)

    assert _DELEGATE_FIELD in out, (
        f"Delegated field '{_DELEGATE_FIELD}' missing from list output.\n"
        f"output:\n{out}"
    )
    # Must say "delegated via" (not "inherited from") for DELEGATES_TO edges
    assert "delegated via" in out.lower(), (
        f"Expected 'delegated via' provenance for DELEGATES_TO field, "
        f"got neither that token nor the field.\noutput:\n{out}"
    )
    # Must NOT incorrectly call it "inherited from"
    assert "inherited from" not in out.lower() or "delegated via" in out.lower(), (
        f"Delegated field incorrectly labelled as 'inherited from'.\noutput:\n{out}"
    )


# ─── T10 — perf gate: account.move-scale (K=30, 7 mixins × 50 fields) ────────

def test_list_fields_perf_account_move_scale(clean_neo4j):
    """_list_fields on a dense K=30 + 7-mixin graph completes within tripwire.

    account.move-scale regression gate: 7 mixin models each with 50 fields plus
    K=30 same-name extenders. The per-hop name-dedup shape (ADR-0048 D2) must
    keep list-gom well within the tripwire. Expected GREEN even on WI-1-only
    branch (flat MATCH does not traverse, so is fast); must stay GREEN after
    WI-2 wiring via bounded traversal.
    """
    drv = clean_neo4j
    _seed_perf_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields

    start = time.monotonic()
    out = _list_fields(_PERF_BASE, odoo_version=TEST_VERSION, limit=1000)
    elapsed = time.monotonic() - start

    assert elapsed < PERF_TRIPWIRE_S, (
        f"_list_fields on account.move-scale graph took {elapsed:.2f}s > "
        f"{PERF_TRIPWIRE_S}s tripwire — bounded traversal may have regressed.\n"
        f"output (first 500 chars):\n{out[:500]}"
    )
    _ = out  # consumed


# ─── T11 — edition-rank tiebreak: CE before EE for same-name field ────────────

def test_edition_rank_tiebreak_ce_before_ee(clean_neo4j):
    """Same-name field on CE + EE modules on the same owner model: CE must win.

    Business rule (V1 regression fix): when two Field nodes exist for the same
    field name on the same model - one from a community module and one from an
    enterprise module - the dedup ORDER BY must pick the community record
    (edition_rank=0) over the enterprise one (edition_rank=1). Pure alphabet order
    would pick the wrong module if the EE module name sorts before the CE module
    name lexicographically.

    Test FAILS if tiebreak is alphabetic only (no edition_rank) and CE module
    name sorts after EE module name. It PASSES only when edition_rank is honoured.
    """
    drv = clean_neo4j
    v = TEST_VERSION

    # CE module name sorts AFTER EE module name alphabetically so that a
    # pure-alpha ORDER BY would pick the EE record - this is the adversarial case.
    ce_mod = "zzz_sale_community"   # sorts last alphabetically
    ee_mod = "aaa_sale_enterprise"  # sorts first alphabetically

    child_model = "t_inh.t11_child"
    mixin_model = "t_inh.t11_mixin"
    field_name  = "t11_shared_field"

    with drv.session() as s:
        s.run(
            "MERGE (c:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=child_model, mod="t_inh_t11_child", v=v,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:$mod, odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=mixin_model, mod="t_inh_t11_mixin", v=v,
        )
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
            "MERGE (c)-[:INHERITS {order:0}]->(mx)",
            c=child_model, mx=mixin_model, v=v,
        )
        # Two Field nodes for the same field name on the SAME mixin model -
        # one from ce_mod, one from ee_mod.
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='char', f.profile=[], f.string='CE version' "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            fn=field_name, m=mixin_model, mod=ce_mod, v=v,
        )
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:$mod, odoo_version:$v}) "
            "SET f.ttype='integer', f.profile=[], f.string='EE version' "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            fn=field_name, m=mixin_model, mod=ee_mod, v=v,
        )
        # Module nodes with different editions
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='community'",
            n=ce_mod, v=v,
        )
        s.run(
            "MERGE (mod:Module {name:$n, odoo_version:$v}) SET mod.edition='enterprise'",
            n=ee_mod, v=v,
        )

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields, _resolve_field

    # List: the kept (deduped) record must come from the CE module
    list_out = _list_fields(child_model, odoo_version=v)
    assert field_name in list_out, (
        f"Field '{field_name}' missing from list output.\noutput:\n{list_out}"
    )
    # After dedup, only CE module should appear in any line containing the field.
    field_lines = [ln for ln in list_out.splitlines() if field_name in ln]
    for ln in field_lines:
        assert ee_mod not in ln, (
            f"EE module '{ee_mod}' appeared for '{field_name}' — edition_rank "
            f"tiebreak broken (CE module should win).\noffending line: {ln!r}"
        )

    # Detail: _resolve_field on the child must return the CE record (char, not integer)
    detail_out = _resolve_field(child_model, field_name, odoo_version=v)
    assert "not found" not in detail_out.lower(), (
        f"_resolve_field returned 'not found' for inherited field.\n"
        f"output:\n{detail_out}"
    )
    # CE record is ttype=char; EE record is ttype=integer
    assert "char" in detail_out, (
        f"Expected CE ttype 'char' in field detail — edition_rank tiebreak broken.\n"
        f"output:\n{detail_out}"
    )
    assert "integer" not in detail_out, (
        f"EE ttype 'integer' found in field detail — edition_rank tiebreak broken.\n"
        f"output:\n{detail_out}"
    )


# ─── T12 — from_module inherited fallback ─────────────────────────────────────

def test_from_module_inherited_fallback(clean_neo4j):
    """_resolve_field with from_module must HIT when the field is declared on a mixin
    that belongs to from_module, and must return 'not found' for a wrong module.

    Business rule (V2 fix): the inherited fallback must run even when from_module is
    set. It resolves the field on the nearest ancestor and then post-filters by the
    declaring module. This allows module-scoped lookup (used by inspect.py) to find
    fields inherited via mixin ownership without a flat match.

    Two invariants:
      1. from_module = <mixin's real module>  -> HIT (not "not found").
      2. from_module = <wrong module>         -> "not found" (post-filter drops it).
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _resolve_field

    # Invariant 1: from_module = mixin's real module -> must resolve
    out_hit = _resolve_field(
        _CHILD, _ONLY_MIXIN_FIELD, odoo_version=TEST_VERSION,
        from_module=_MIXIN_MOD,
    )
    assert "not found" not in out_hit.lower(), (
        f"_resolve_field with from_module='{_MIXIN_MOD}' returned 'not found' for "
        f"a field declared on the mixin in that module — inherited fallback not "
        f"post-filtering correctly.\noutput:\n{out_hit}"
    )
    assert "Inherited from:" in out_hit, (
        f"Expected 'Inherited from:' provenance in field detail.\noutput:\n{out_hit}"
    )

    # Invariant 2: from_module = wrong module -> must not find anything
    out_miss = _resolve_field(
        _CHILD, _ONLY_MIXIN_FIELD, odoo_version=TEST_VERSION,
        from_module="nonexistent_module_xyz",
    )
    assert "not found" in out_miss.lower(), (
        f"_resolve_field with wrong from_module should return 'not found', "
        f"but got:\n{out_miss}"
    )


# ─── T13 — inherited field detail contains Label and Help ─────────────────────

def test_inherited_field_detail_contains_label_and_help(clean_neo4j):
    """_resolve_field on an inherited field must include Label and Help lines.

    Business rule (V3 fix - ADR-0023 output completeness): when a mixin field has
    a string (label) and help text, the detail view for an inherited hit must render
    'Label:' and 'Help:' lines, matching own-field detail parity. Absence of these
    lines is a parity regression.
    """
    drv = clean_neo4j
    v = TEST_VERSION

    child_model = "t_inh.t13_child"
    mixin_model = "t_inh.t13_mixin"
    field_name  = "t13_field_with_meta"

    with drv.session() as s:
        s.run(
            "MERGE (c:Model {name:$n, module:'t_inh_t13_child', odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=child_model, v=v,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:'t_inh_t13_mixin', odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=mixin_model, v=v,
        )
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, odoo_version:$v}) "
            "MERGE (c)-[:INHERITS {order:0}]->(mx)",
            c=child_model, mx=mixin_model, v=v,
        )
        # Field with string (label) and help text on the mixin
        s.run(
            "MATCH (mx:Model {name:$m, odoo_version:$v}) "
            "MERGE (f:Field {name:$fn, model:$m, module:'t_inh_t13_mixin', "
            "       odoo_version:$v}) "
            "SET f.ttype='char', f.profile=[], "
            "    f.string='My Field Label', f.help='My field help text' "
            "MERGE (f)-[:BELONGS_TO]->(mx)",
            fn=field_name, m=mixin_model, v=v,
        )
        s.run(
            "MERGE (mod:Module {name:'t_inh_t13_mixin', odoo_version:$v}) "
            "SET mod.edition='community'",
            v=v,
        )

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _resolve_field

    out = _resolve_field(child_model, field_name, odoo_version=v)

    assert "not found" not in out.lower(), (
        f"_resolve_field returned 'not found' for inherited field with metadata.\n"
        f"output:\n{out}"
    )
    assert "Inherited from:" in out, (
        f"Expected 'Inherited from:' provenance in inherited field detail.\n"
        f"output:\n{out}"
    )
    assert "Label:" in out, (
        f"Expected 'Label:' line in inherited field detail — parity regression.\n"
        f"Inherited field detail must include Label just like own-field detail.\n"
        f"output:\n{out}"
    )
    assert "My Field Label" in out, (
        f"Expected label value 'My Field Label' in inherited field detail.\n"
        f"output:\n{out}"
    )
    assert "Help:" in out, (
        f"Expected 'Help:' line in inherited field detail — parity regression.\n"
        f"Inherited field detail must include Help just like own-field detail.\n"
        f"output:\n{out}"
    )
    assert "My field help text" in out, (
        f"Expected help value 'My field help text' in inherited field detail.\n"
        f"output:\n{out}"
    )


# ─── T15 — methods are NOT delegated via _inherits (GAP-1, negative) ──────────

# Method that exists on the DELEGATES_TO parent — must NOT leak to the child.
_DELEGATE_METHOD = "parent_only_method_t"


def _seed_delegation_world_with_method(drv):
    """Seed a DELEGATES_TO parent that has BOTH a method AND a field (GAP-1).

    t_inh.delegate_child -[:DELEGATES_TO {via_field}]-> t_inh.delegate_parent
        delegated_field_t   : char    (on parent — IS delegated → visible on child)
        parent_only_method_t: plain    (on parent — NOT delegated → hidden on child)

    Odoo ground-truth (v8→v19, unanimous): `_inherits` delegation gives the child
    the parent's FIELDS ONLY (related proxy, separate table). Methods are NEVER
    inherited through `_inherits`. So on the child:
      - the field MUST appear (delegated),
      - the method MUST NOT appear (not delegated).
    """
    _seed_delegation_world(drv)  # parent, child, DELEGATES_TO edge, delegated field
    with drv.session() as s:
        s.run(
            "MATCH (p:Model {name:$m, odoo_version:$v}) "
            "MERGE (mth:Method {name:$mn, model:$m, module:'t_inh_del_p', "
            "       odoo_version:$v}) "
            "SET mth.convention_kind='plain', mth.profile=[] "
            "MERGE (mth)-[:DEFINED_IN_METHOD]->(p)",
            mn=_DELEGATE_METHOD, m=_DELEGATE_PARENT, v=TEST_VERSION,
        )


def test_methods_not_delegated_via_inherits(clean_neo4j):
    """GAP-1: a method on a DELEGATES_TO parent must NOT surface on the child.

    Business rule (Odoo v8→v19): `_inherits` delegation carries FIELDS ONLY,
    never methods. The child must see the delegated FIELD but must NOT see the
    parent's method — neither in the method list nor via method detail.

    RED before the GAP-1 fix (method helpers traversed INHERITS|DELEGATES_TO and
    falsely pulled the parent's method into the child); GREEN after (methods
    traverse INHERITS only).
    """
    drv = clean_neo4j
    _seed_delegation_world_with_method(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields, _list_methods, _resolve_method

    # Methods: the parent's method must NOT appear on the child (not delegated).
    methods_out = _list_methods(_DELEGATE_CHILD, odoo_version=TEST_VERSION)
    assert _DELEGATE_METHOD not in methods_out, (
        f"GAP-1 LEAK: method '{_DELEGATE_METHOD}' from a DELEGATES_TO parent "
        f"surfaced on the child — methods are NOT delegated via _inherits.\n"
        f"output:\n{methods_out}"
    )

    # Method detail: must be 'not found' for the non-delegated method.
    detail_out = _resolve_method(
        _DELEGATE_CHILD, _DELEGATE_METHOD, odoo_version=TEST_VERSION
    )
    assert "not found" in detail_out.lower(), (
        f"GAP-1 LEAK: _resolve_method resolved a non-delegated parent method on "
        f"the child — methods are NOT delegated via _inherits.\n"
        f"output:\n{detail_out}"
    )

    # Counter-check: the FIELD on the same parent IS delegated → visible on child.
    fields_out = _list_fields(_DELEGATE_CHILD, odoo_version=TEST_VERSION)
    assert _DELEGATE_FIELD in fields_out, (
        f"Delegated field '{_DELEGATE_FIELD}' missing from child — fields ARE "
        f"delegated via _inherits (only methods are not).\noutput:\n{fields_out}"
    )


# ─── T16 — (*) override marker on an inherited method ─────────────────────────

_T16_CHILD  = "t_inh.t16_child"
_T16_MIXIN  = "t_inh.t16_mixin"
_T16_METHOD = "mixin_hook"


def _seed_inherited_overridden_method(drv):
    """Seed a mixin method overridden in >=2 modules ON THE MIXIN (GAP-2/GAP-3).

    t_inh.t16_child -[:INHERITS]-> t_inh.t16_mixin
        method `mixin_hook` declared on the mixin model by TWO modules:
          t_inh_t16_mix_a, t_inh_t16_mix_b  (override chain length 2 on the owner)
    The child has no own copy → the method is purely inherited, yet overridden
    >=2× on its owner. The (*) marker (GAP-2) and the real 2-entry override chain
    (GAP-3) must both reflect the owner's override multiplicity.
    """
    v = TEST_VERSION
    with drv.session() as s:
        s.run(
            "MERGE (c:Model {name:$n, module:'t_inh_t16_child', odoo_version:$v}) "
            "SET c.is_definition=true, c.had_explicit_name=true",
            n=_T16_CHILD, v=v,
        )
        s.run(
            "MERGE (mx:Model {name:$n, module:'t_inh_t16_mix_a', odoo_version:$v}) "
            "SET mx.is_definition=true, mx.had_explicit_name=true",
            n=_T16_MIXIN, v=v,
        )
        # Second module-node for the mixin model (extension that also declares the
        # method) — same model name, different declaring module.
        s.run(
            "MERGE (mx:Model {name:$n, module:'t_inh_t16_mix_b', odoo_version:$v}) "
            "SET mx.is_definition=false",
            n=_T16_MIXIN, v=v,
        )
        s.run(
            "MATCH (c:Model {name:$c, odoo_version:$v}) "
            "MATCH (mx:Model {name:$mx, module:'t_inh_t16_mix_a', odoo_version:$v}) "
            "MERGE (c)-[:INHERITS {order:0}]->(mx)",
            c=_T16_CHILD, mx=_T16_MIXIN, v=v,
        )
        # Same method name declared on the mixin model by TWO modules.
        for mod in ("t_inh_t16_mix_a", "t_inh_t16_mix_b"):
            s.run(
                "MATCH (mx:Model {name:$m, module:$mod, odoo_version:$v}) "
                "MERGE (mth:Method {name:$mn, model:$m, module:$mod, odoo_version:$v}) "
                "SET mth.convention_kind='plain', mth.profile=[], "
                "    mth.has_super_call=true, mth.decorators=[] "
                "MERGE (mth)-[:DEFINED_IN_METHOD]->(mx)",
                mn=_T16_METHOD, m=_T16_MIXIN, mod=mod, v=v,
            )
        for mod in ("t_inh_t16_child", "t_inh_t16_mix_a", "t_inh_t16_mix_b"):
            s.run(
                "MERGE (m:Module {name:$n, odoo_version:$v}) SET m.edition='community'",
                n=mod, v=v,
            )


def test_override_marker_on_inherited_method(clean_neo4j):
    """GAP-2: an inherited method overridden in >=2 modules on its owner gets (*).

    Business rule: the (*) override marker flags a method declared in >=2 modules
    on its OWNER model. For an inherited method the owner is the mixin — counting
    modules only on the child would never mark it. After GAP-2 the marker must
    appear in the child listing.

    RED before GAP-2 (override query keyed only on the child model → 0 rows for
    an inherited method → no marker); GREEN after.
    """
    drv = clean_neo4j
    _seed_inherited_overridden_method(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_methods

    out = _list_methods(_T16_CHILD, odoo_version=TEST_VERSION)

    # The inherited method must appear, marked with (*) (overridden 2× on owner).
    marker_lines = [
        ln for ln in out.splitlines()
        if _T16_METHOD in ln and "(*)" in ln
    ]
    assert marker_lines, (
        f"GAP-2: inherited method '{_T16_METHOD}' (overridden 2× on its owner) "
        f"is missing the (*) override marker.\noutput:\n{out}"
    )


# ─── T17 — inherited method detail shows the REAL override chain (not "(1)") ───

def test_inherited_method_detail_shows_real_override_chain(clean_neo4j):
    """GAP-3: inherited method detail shows the multi-module chain, not "(1)".

    Business rule: for a method inherited from a mixin and overridden by 2 modules
    on that mixin, the detail must render the REAL override chain (2 entries with
    both declaring modules) — not a hardcoded "Override chain (1)" naming only one.

    RED before GAP-3 (hardcoded "(1)" + single owner module); GREEN after.
    """
    drv = clean_neo4j
    _seed_inherited_overridden_method(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _resolve_method

    out = _resolve_method(_T16_CHILD, _T16_METHOD, odoo_version=TEST_VERSION)

    assert "not found" not in out.lower(), (
        f"_resolve_method returned 'not found' for an inherited method.\n"
        f"output:\n{out}"
    )
    assert "Inherited from:" in out, (
        f"Expected 'Inherited from:' provenance in inherited method detail.\n"
        f"output:\n{out}"
    )
    # The chain count must be the REAL 2 (both mixin modules), not the old "(1)".
    assert "Override chain (2)" in out, (
        f"GAP-3: inherited method detail must show the real 2-module override "
        f"chain 'Override chain (2)', not a hardcoded count.\noutput:\n{out}"
    )
    # Both declaring modules must be present in the chain.
    assert "t_inh_t16_mix_a" in out and "t_inh_t16_mix_b" in out, (
        f"GAP-3: both override-chain modules must appear in the inherited method "
        f"detail.\noutput:\n{out}"
    )


# ─── T18 — delegated field detail/row carries the "separate table" signal ─────

def test_delegated_field_signals_separate_table(clean_neo4j):
    """GAP-5: delegated field row + detail state 'separate table'/'fields-only'.

    Business rule: `_inherits` delegation is NOT ordinary inheritance — it gives
    the child the owner's FIELDS ONLY, stored in the owner's SEPARATE table via a
    FK. The provenance must signal this so an AI client does not treat a delegated
    field like an in-place inherited one.
    """
    drv = clean_neo4j
    _seed_delegation_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _list_fields, _resolve_field

    # List row carries the delegation signal.
    list_out = _list_fields(_DELEGATE_CHILD, odoo_version=TEST_VERSION)
    assert _DELEGATE_FIELD in list_out, (
        f"Delegated field '{_DELEGATE_FIELD}' missing from list.\noutput:\n{list_out}"
    )
    assert "separate table" in list_out.lower(), (
        f"GAP-5: delegated field row must signal 'separate table' "
        f"(delegation, fields-only).\noutput:\n{list_out}"
    )
    assert "fields-only" in list_out.lower(), (
        f"GAP-5: delegated field row must signal 'fields-only'.\noutput:\n{list_out}"
    )

    # Detail view also carries it.
    detail_out = _resolve_field(_DELEGATE_CHILD, _DELEGATE_FIELD, odoo_version=TEST_VERSION)
    assert "separate table" in detail_out.lower(), (
        f"GAP-5: delegated field detail must signal 'separate table'.\n"
        f"output:\n{detail_out}"
    )
    assert "fields-only" in detail_out.lower(), (
        f"GAP-5: delegated field detail must signal 'fields-only'.\n"
        f"output:\n{detail_out}"
    )


# ─── T19 — inherited-field detail next-step hint targets the OWNER model ───────

def test_inherited_field_hint_targets_owner_model(clean_neo4j):
    """FIX-3 (review #283): the impact_analysis next-step hint on an inherited
    field must reference the OWNER model (where the field is declared), not the
    child model.

    Business rule: the field NODE for 'res_ref_t' lives on t_inh.mixin (the
    owner), not on t_inh.child. impact_analysis flat-matches a field on its
    declaring model — a hint keyed by 't_inh.child.res_ref_t' resolves to an
    EMPTY blast radius, and the AI client wrongly concludes "no impact" and edits
    unsafely. The hint MUST be keyed by 't_inh.mixin.res_ref_t' so it resolves.

    RED before FIX-3 (hint used the child model_name); GREEN after.
    """
    drv = clean_neo4j
    _seed_basic_world(drv)

    import importlib
    import sys
    sys.modules.pop("src.mcp.server", None)
    importlib.invalidate_caches()
    from src.mcp.server import _resolve_field

    out = _resolve_field(_CHILD, _ONLY_MIXIN_FIELD, odoo_version=TEST_VERSION)

    assert "Inherited from:" in out, (
        f"Precondition: field must resolve as inherited.\noutput:\n{out}"
    )
    # The impact_analysis hint must reference the OWNER (mixin), not the child.
    assert f"entity_name='{_MIXIN}.{_ONLY_MIXIN_FIELD}'" in out, (
        f"FIX-3: impact_analysis hint must be keyed by the OWNER model "
        f"'{_MIXIN}.{_ONLY_MIXIN_FIELD}' (where the field is declared), so the "
        f"blast radius resolves.\noutput:\n{out}"
    )
    # And it must NOT be keyed by the child (which returns an empty blast radius).
    assert f"entity_name='{_CHILD}.{_ONLY_MIXIN_FIELD}'" not in out, (
        f"FIX-3: impact_analysis hint must NOT be keyed by the child model "
        f"'{_CHILD}.{_ONLY_MIXIN_FIELD}' — that flat-matches nothing and "
        f"misleads the agent into 'no impact'.\noutput:\n{out}"
    )


# ─── T20 — _method_override_chain maps tx-timeout → OrmQueryTimeout (bounded) ──

def test_method_override_chain_maps_tx_timeout_to_ormquerytimeout(monkeypatch):
    """FIX-1 (review #283): _method_override_chain must be bounded — a Neo4j
    transaction-timeout ClientError must surface as a clean OrmQueryTimeout, not
    a raw ClientError (which would 500) and not a hang.

    This is the same availability class as #273/#276: the COUNT/INHERITS-adjacent
    query is called up to 2× per inherited-method resolve. We simulate the driver
    timeout deterministically with a session whose .run() raises the driver's
    TransactionTimedOut* status code, then assert the helper translates it to a
    user-facing OrmQueryTimeout (no Cypher leaked). No real Neo4j needed.
    """
    from src.mcp.orm import OrmQueryTimeout
    from src.mcp.server import _method_override_chain
    from tests._timeout_harness import make_tx_timeout_error

    class _TimingOutSession:
        def run(self, *a, **k):
            # Driver-set per-query timeout status code (matches _is_tx_timeout).
            raise make_tx_timeout_error()

    with pytest.raises(OrmQueryTimeout) as ei:
        _method_override_chain(
            _TimingOutSession(), _CHILD, _ONLY_CHILD_METHOD, TEST_VERSION
        )
    msg = ei.value.user_message
    assert "timed out" in msg.lower(), f"Expected a timeout message, got: {msg!r}"
    # No Cypher / internal leaked to the client (ADR-0023 tone).
    assert "MATCH" not in msg and "Cypher" not in msg, f"Cypher leaked: {msg!r}"
    # Actionable English.
    assert "retry" in msg.lower() or "dense" in msg.lower(), (
        f"Expected an actionable English message, got: {msg!r}"
    )


def test_resolve_method_returns_clean_string_on_tx_timeout(monkeypatch):
    """FIX-1 caller path: _resolve_method must surface a bounded tx-timeout as a
    clean ADR-0023 STRING (the OrmQueryTimeout.user_message), never propagate a
    raw exception that model_inspect/entity_lookup's plain @offload would turn
    into a protocol-level 500.

    Simulated deterministically by forcing _method_override_chain to raise
    OrmQueryTimeout — the contract is that _resolve_method catches it and returns
    the user_message string rather than re-raising.
    """
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    sentinel = "Query timed out after 30s while resolving the override chain"

    def _boom(*a, **k):
        raise OrmQueryTimeout(sentinel + " (simulated).")

    # Stub the driver session + version resolution so we never touch Neo4j, then
    # force the override-chain query to time out.
    class _NoopSession:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _NoopDriver:
        def session(self):
            return _NoopSession()

    monkeypatch.setattr(srv, "_get_driver", lambda: _NoopDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TEST_VERSION)
    monkeypatch.setattr(srv, "_method_override_chain", _boom)

    out = srv._resolve_method(_CHILD, _ONLY_CHILD_METHOD, odoo_version=TEST_VERSION)

    assert isinstance(out, str), "must return a string, not raise"
    assert sentinel in out, (
        f"_resolve_method must return the OrmQueryTimeout.user_message string "
        f"(ADR-0023 clean), got:\n{out!r}"
    )


# ─── T21 — LIST path mirrors the detail-path tx-timeout contract (#284 follow-up) ──
#
# The detail resolvers (_resolve_field / _resolve_method) were hardened to map a
# per-query Neo4j tx-timeout (OrmQueryTimeout) to a clean degraded English STRING.
# The LIST path (_list_fields / _list_methods, method='fields'|'methods') began
# traversing INHERITS edges in the WI-2 wave but was MISSED — a 30s tx-timeout on
# a dense inheritance graph would ESCAPE _list_fields/_list_methods to FastMCP as
# a protocol-level `isError`, violating the ADR-0023 raw-text contract. These
# UNIT tests close that asymmetry. They monkeypatch the bounded helpers / driver
# session to FORCE the timeout deterministically — NO real Neo4j/Postgres needed
# (same no-Docker lane + mocking style as T20 above).

def test_list_fields_returns_clean_string_on_tx_timeout(monkeypatch):
    """_list_fields(method='fields') must surface a bounded tx-timeout as a clean
    ADR-0023 STRING (the OrmQueryTimeout.user_message), never propagate the raw
    exception that model_inspect/entity_lookup's plain @offload would turn into a
    protocol-level 500. Mirrors the detail-path contract (_resolve_field)."""
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    sentinel = "Query timed out after 30s while listing fields"

    def _boom(*a, **k):
        raise OrmQueryTimeout(sentinel + " (simulated).")

    class _NoopSession:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _NoopDriver:
        def session(self):
            return _NoopSession()

    import src.mcp.listings as listings

    monkeypatch.setattr(srv, "_get_driver", lambda: _NoopDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TEST_VERSION)
    # First bounded helper inside the session block raises the bounded timeout.
    # Moved to src/mcp/listings.py (Phase 7 / A1) where _list_fields imports it
    # from src.mcp.orm and calls it by bare name → patch on src.mcp.listings.
    monkeypatch.setattr(listings, "_list_fields_with_inherited", _boom)

    out = srv._list_fields(_CHILD, odoo_version=TEST_VERSION)

    assert isinstance(out, str), "must return a string, not raise"
    assert sentinel in out, (
        f"_list_fields must return the OrmQueryTimeout.user_message string "
        f"(ADR-0023 clean), got:\n{out!r}"
    )
    assert "MATCH" not in out and "Traceback" not in out, f"leak: {out!r}"


def test_list_methods_returns_clean_string_on_tx_timeout(monkeypatch):
    """_list_methods(method='methods') must surface a bounded tx-timeout from its
    list/count helpers as a clean ADR-0023 STRING, mirroring _resolve_method."""
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    sentinel = "Query timed out after 30s while listing methods"

    def _boom(*a, **k):
        raise OrmQueryTimeout(sentinel + " (simulated).")

    class _NoopSession:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _NoopDriver:
        def session(self):
            return _NoopSession()

    import src.mcp.listings as listings

    monkeypatch.setattr(srv, "_get_driver", lambda: _NoopDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TEST_VERSION)
    # Moved to src/mcp/listings.py (Phase 7 / A1); _list_methods calls it by bare
    # name there → patch on src.mcp.listings, not the server hub.
    monkeypatch.setattr(listings, "_list_methods_with_inherited", _boom)

    out = srv._list_methods(_CHILD, odoo_version=TEST_VERSION)

    assert isinstance(out, str), "must return a string, not raise"
    assert sentinel in out, (
        f"_list_methods must return the OrmQueryTimeout.user_message string "
        f"(ADR-0023 clean), got:\n{out!r}"
    )
    assert "MATCH" not in out and "Traceback" not in out, f"leak: {out!r}"


def test_list_methods_override_rec_raw_clienterror_converted(monkeypatch):
    """The override-marker query in _list_methods was a BARE
    `session.run(_bounded(...)).single()` that raised a RAW neo4j ClientError on
    timeout (NOT routed through orm.py's ClientError -> OrmQueryTimeout
    conversion). It is now routed through `_single_bounded`, so a tx-timeout on
    JUST that query must be converted and surfaced as the clean degraded STRING —
    never escape as a raw ClientError. We let the list/count helpers succeed and
    force ONLY the override_rec `session.run(...)` to raise the driver timeout
    code, exercising the REAL _single_bounded conversion (not a stubbed boom)."""
    import src.mcp.listings as listings
    import src.mcp.server as srv
    from tests._timeout_harness import make_tx_timeout_error

    # list/count helpers succeed (no inherited rows) so execution reaches the
    # override_rec query — which is the ONLY thing that times out here. These were
    # moved to src/mcp/listings.py (Phase 7 / A1) and are called by bare name in
    # _list_methods → patch them on src.mcp.listings. _resolve_version / _scope /
    # _get_driver stay hub helpers read through _list_methods' _srv. bind, so they
    # are still patched on src.mcp.server. The REAL _single_bounded conversion
    # (also a hub helper) is exercised unstubbed.
    monkeypatch.setattr(listings, "_list_methods_with_inherited", lambda *a, **k: [])
    monkeypatch.setattr(listings, "_count_methods_with_inherited", lambda *a, **k: 0)
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TEST_VERSION)
    # _scope must return the kwargs the bare query interpolates.
    monkeypatch.setattr(srv, "_scope", lambda p=None: {"own": None, "shared": []})

    class _TimingOutSession:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def run(self, *a, **k):
            # Driver-set per-query timeout status code (matches _is_tx_timeout),
            # the SAME code the detail-path T20 test uses.
            raise make_tx_timeout_error()

    class _NoopDriver:
        def session(self):
            return _TimingOutSession()

    monkeypatch.setattr(srv, "_get_driver", lambda: _NoopDriver())

    out = srv._list_methods(_CHILD, odoo_version=TEST_VERSION)

    assert isinstance(out, str), (
        "override_rec raw ClientError must be converted + surfaced as a string, "
        f"not raised; got: {out!r}"
    )
    assert "timed out" in out.lower(), (
        f"override_rec timeout must surface the degraded English string, got:\n{out!r}"
    )
    assert "MATCH" not in out and "Traceback" not in out, f"Cypher/trace leaked: {out!r}"
