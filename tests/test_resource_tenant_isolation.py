# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_resource_tenant_isolation.py
"""R5 — cross-tenant isolation tests for 6 odoo:// resource kinds.

Verifies that the ``odoo://`` resource handlers (model, field, method, module,
view, stylesheet) do NOT leak private-tenant data across tenant boundaries, both
on the cache-MISS path (first read) AND the cache-HIT path (second read with
same key).

Why cache-HIT must be tested separately:
  Prior to the R1 fix, cache keys were global per-URI (no tenant dimension).
  An admin reading first would cache an unrestricted body; a subsequent tenant
  cache-HIT would return it without re-filtering — a cross-tenant leak.
  The fix adds ``::t{tenant_id}`` to the cache key so each tenant gets its own
  slot.  The tests below call each resource twice to ensure the second call
  (cache-HIT) is equally safe.

Test structure mirrors ``test_cross_tenant_isolation.py`` (same ``world``
fixture topology, same ``as_tenant`` helper).

Markers: ``neo4j`` (graph data) + ``postgres`` (tenant/profile resolution).
Both Docker services must be available; testcontainers spins them automatically.
"""

from contextlib import contextmanager

import pytest

from tests.conftest import PG_EMBED_VERSION as V  # "99.0"

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

_PFX = "rti_"  # prefix for all rows/nodes created here → easy cleanup


# ---------------------------------------------------------------------------
# Helpers — replicated from test_cross_tenant_isolation.py so this file is
# self-contained (avoids cross-file fixture coupling).
# ---------------------------------------------------------------------------


def _is_denied(body: str) -> bool:
    """Return True if the resource body represents a 'not found / access denied' response.

    Different resource kinds use different messages:
      - model / field / method / view: "not found"
      - module: "no module named"
    """
    lower = body.lower()
    return "not found" in lower or "no module named" in lower


@contextmanager
def as_tenant(tenant_id):
    """Pin the request tenant ContextVar to *tenant_id* (None = admin) for the block.

    Uses ``_tenant_id_var`` (ContextVar) per PR #197 — the old
    ``_tenant_id_local`` threading.local was removed for asyncio-safe
    per-coroutine isolation; ``reset(token)`` restores the prior value.
    """
    from src.mcp import session
    from src.mcp.server import _tenant_id_var

    session.invalidate_allowed_profiles()
    token = _tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id_var.reset(token)
        session.invalidate_allowed_profiles()


def _cleanup(pg):
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world(clean_pg_embeddings, clean_neo4j):
    """Two tenants (acme, globex) + shared base; each tenant has a private module
    with a Model, Field, Method, Module node, and a View.

    acme can see: lt_base_mod (shared) + lt_acme_mod (own).
    globex can see: lt_base_mod (shared) + lt_globex_mod (own).
    Neither can see the other's private module data.
    """
    pg = clean_pg_embeddings
    drv = clean_neo4j
    _cleanup(pg)

    # --- Postgres: tenants + profiles ---
    with pg.cursor() as cur:
        cur.execute(
            f"INSERT INTO tenants (name) VALUES ('{_PFX}acme') RETURNING id",
        )
        acme = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO tenants (name) VALUES ('{_PFX}globex') RETURNING id",
        )
        globex = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) "
            f"VALUES ('{_PFX}base', %s) RETURNING id",
            (V,),
        )
        base_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id, parent_profile_id) "
            f"VALUES ('{_PFX}acme_p', %s, %s, %s)",
            (V, acme, base_id),
        )
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id, parent_profile_id) "
            f"VALUES ('{_PFX}globex_p', %s, %s, %s)",
            (V, globex, base_id),
        )
    pg.commit()

    base_p = f"{_PFX}base"
    acme_p = f"{_PFX}acme_p"
    globex_p = f"{_PFX}globex_p"

    # --- Neo4j: shared base + per-tenant private data ---
    with drv.session() as s:
        # Shared base module (visible to both tenants).
        s.run(
            "MERGE (m:Module {name:$mod, odoo_version:$v}) "
            "SET m.profile=$p, m.edition='community'",
            mod=f"{_PFX}base_mod", v=V, p=[base_p],
        )

        def _mk_module(module, profiles):
            """Create a Module node with a Model, Field, Method, and View."""
            s.run(
                "MERGE (m:Module {name:$mod, odoo_version:$v}) "
                "SET m.profile=$p, m.edition='community'",
                mod=module, v=V, p=profiles,
            )
            model = f"{module}.model"
            s.run(
                """
                MATCH (m:Module {name:$mod, odoo_version:$v})
                MERGE (md:Model {name:$model, module:$mod, odoo_version:$v})
                SET md.profile=$p, md.is_definition=true
                MERGE (md)-[:DEFINED_IN]->(m)
                """,
                model=model, mod=module, v=V, p=profiles,
            )
            # Field
            s.run(
                """
                MATCH (md:Model {name:$model, module:$mod, odoo_version:$v})
                MERGE (f:Field {name:'secret_field', model:$model,
                                module:$mod, odoo_version:$v})
                SET f.profile=$p, f.ttype='char'
                MERGE (f)-[:BELONGS_TO]->(md)
                """,
                model=model, mod=module, v=V, p=profiles,
            )
            # Method
            s.run(
                """
                MATCH (md:Model {name:$model, module:$mod, odoo_version:$v})
                MERGE (mth:Method {name:'secret_method', model:$model,
                                   module:$mod, odoo_version:$v})
                SET mth.profile=$p, mth.signature='self',
                    mth.decorators=[], mth.convention_kind='private',
                    mth.super_safety='usually', mth.has_super_call=false
                MERGE (mth)-[:BELONGS_TO]->(md)
                """,
                model=model, mod=module, v=V, p=profiles,
            )
            # View
            xmlid = f"{module}.secret_view"
            s.run(
                """
                MERGE (vw:View {xmlid:$x, odoo_version:$v})
                SET vw.profile=$p, vw.name=$x, vw.model=$model,
                    vw.module=$mod, vw.type='form', vw.mode='primary',
                    vw.xpaths_exprs=[], vw.xpaths_positions=[]
                """,
                x=xmlid, v=V, p=profiles, model=model, mod=module,
            )
            # Stylesheet (carries profile[] → private-tenant stylesheet exists).
            ss_path = f"addons/{module}/static/src/scss/secret.scss"
            s.run(
                """
                MERGE (ss:Stylesheet {file_path:$fp, module:$mod, odoo_version:$v})
                SET ss.profile=$p, ss.language='scss'
                """,
                fp=ss_path, mod=module, v=V, p=profiles,
            )

        _mk_module(f"{_PFX}acme_mod", [acme_p, base_p])
        _mk_module(f"{_PFX}globex_mod", [globex_p, base_p])

    yield {
        "acme": acme,
        "globex": globex,
        "acme_p": acme_p,
        "globex_p": globex_p,
        "base_p": base_p,
    }
    _cleanup(pg)


# ---------------------------------------------------------------------------
# Helper: clear the resource cache between calls so we can control
# cache-MISS vs cache-HIT explicitly.
# ---------------------------------------------------------------------------


def _clear_resource_cache():
    """Clear the module-level resource cache singleton."""
    from src.mcp.resources import get_cache
    get_cache().clear()


# ---------------------------------------------------------------------------
# R5 — model resource isolation (odoo://version/model/...)
# ---------------------------------------------------------------------------


def test_resource_model_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private model via odoo://model/."""
    from src.mcp.resources import _render_model

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        body, _mime = _render_model(V, f"{_PFX}globex_mod.model")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT MODEL RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_model_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must NOT serve the cached admin/other-tenant body.

    This proves the tenant-scoped cache key (R1 fix) prevents contamination.
    The test calls _render_model twice — first populates the cache for acme's
    slot, second must still return 'not found' (not an admin-cached body).
    """
    from src.mcp.resources import _render_model

    _clear_resource_cache()
    # Call 1: cache-MISS → computes + stores under acme's key.
    with as_tenant(world["acme"]):
        body1, _ = _render_model(V, f"{_PFX}globex_mod.model")
    assert "not found" in body1.lower()

    # Call 2: should be cache-HIT under acme's key — same denial expected.
    with as_tenant(world["acme"]):
        body2, _ = _render_model(V, f"{_PFX}globex_mod.model")
    assert "not found" in body2.lower(), (
        f"CROSS-TENANT MODEL RESOURCE LEAK (cache-HIT): {body2!r}"
    )


def test_resource_model_admin_sees_all(world):
    """Admin (tenant_id=None) must still see globex's private model."""
    from src.mcp.resources import _render_model

    _clear_resource_cache()
    with as_tenant(None):
        body, _ = _render_model(V, f"{_PFX}globex_mod.model")
    assert not _is_denied(body), (
        f"Admin unexpectedly denied globex model: {body!r}"
    )


def test_resource_model_admin_and_tenant_cache_slots_independent(world):
    """Admin cache-slot MUST NOT bleed into tenant cache-slot.

    Before R1 fix: admin reads first → caches unrestricted body globally →
    tenant reads same URI → cache-HIT → receives admin's body (LEAK).
    After fix: each has its own ::t_admin / ::t{id} slot.
    """
    from src.mcp.resources import _render_model

    _clear_resource_cache()
    # Admin reads globex model first (unrestricted → body has content).
    with as_tenant(None):
        admin_body, _ = _render_model(V, f"{_PFX}globex_mod.model")
    assert not _is_denied(admin_body), (
        f"Admin unexpectedly denied globex model: {admin_body!r}"
    )

    # Acme reads same URI — must get its own (denied) result, NOT admin's cached body.
    with as_tenant(world["acme"]):
        acme_body, _ = _render_model(V, f"{_PFX}globex_mod.model")
    assert _is_denied(acme_body), (
        f"CACHE CONTAMINATION: acme received admin's cached model body: {acme_body!r}"
    )


# ---------------------------------------------------------------------------
# R5 — field resource isolation (odoo://version/field/...)
# ---------------------------------------------------------------------------


def test_resource_field_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private field via odoo://field/."""
    from src.mcp.resources import _render_field

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        body, _ = _render_field(V, f"{_PFX}globex_mod.model", "secret_field")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT FIELD RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_field_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must return the same scoped denial."""
    from src.mcp.resources import _render_field

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        _render_field(V, f"{_PFX}globex_mod.model", "secret_field")
        # Second call — cache-HIT under acme's slot.
        body, _ = _render_field(V, f"{_PFX}globex_mod.model", "secret_field")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT FIELD RESOURCE LEAK (cache-HIT): {body!r}"
    )


def test_resource_field_admin_and_tenant_cache_independent(world):
    """Admin cache-slot must not contaminate tenant cache-slot for field resource."""
    from src.mcp.resources import _render_field

    _clear_resource_cache()
    with as_tenant(None):
        admin_body, _ = _render_field(V, f"{_PFX}globex_mod.model", "secret_field")
    assert not _is_denied(admin_body), (
        f"Admin unexpectedly denied globex field: {admin_body!r}"
    )

    with as_tenant(world["acme"]):
        acme_body, _ = _render_field(V, f"{_PFX}globex_mod.model", "secret_field")
    assert _is_denied(acme_body), (
        f"CACHE CONTAMINATION (field): acme got admin body: {acme_body!r}"
    )


# ---------------------------------------------------------------------------
# R5 — method resource isolation (odoo://version/method/...)
# ---------------------------------------------------------------------------


def test_resource_method_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private method via odoo://method/."""
    from src.mcp.resources import _render_method

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        body, _ = _render_method(V, f"{_PFX}globex_mod.model", "secret_method")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT METHOD RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_method_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must return the same scoped denial."""
    from src.mcp.resources import _render_method

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        _render_method(V, f"{_PFX}globex_mod.model", "secret_method")
        body, _ = _render_method(V, f"{_PFX}globex_mod.model", "secret_method")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT METHOD RESOURCE LEAK (cache-HIT): {body!r}"
    )


def test_resource_method_admin_and_tenant_cache_independent(world):
    """Admin cache-slot must not contaminate tenant cache-slot for method resource."""
    from src.mcp.resources import _render_method

    _clear_resource_cache()
    with as_tenant(None):
        admin_body, _ = _render_method(V, f"{_PFX}globex_mod.model", "secret_method")
    assert not _is_denied(admin_body), (
        f"Admin unexpectedly denied globex method: {admin_body!r}"
    )

    with as_tenant(world["acme"]):
        acme_body, _ = _render_method(V, f"{_PFX}globex_mod.model", "secret_method")
    assert _is_denied(acme_body), (
        f"CACHE CONTAMINATION (method): acme got admin body: {acme_body!r}"
    )


# ---------------------------------------------------------------------------
# R5 — module resource isolation (odoo://version/module/...)
# ---------------------------------------------------------------------------


def test_resource_module_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private module via odoo://module/.

    Note: _describe_module returns "No module named '...' indexed for Odoo X.Y."
    when access is denied (not "not found") — both are accepted by _is_denied().
    """
    from src.mcp.resources import _render_module

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        body, _ = _render_module(V, f"{_PFX}globex_mod")
    assert _is_denied(body), (
        f"CROSS-TENANT MODULE RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_module_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must return the same scoped denial."""
    from src.mcp.resources import _render_module

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        _render_module(V, f"{_PFX}globex_mod")
        body, _ = _render_module(V, f"{_PFX}globex_mod")
    assert _is_denied(body), (
        f"CROSS-TENANT MODULE RESOURCE LEAK (cache-HIT): {body!r}"
    )


def test_resource_module_admin_and_tenant_cache_independent(world):
    """Admin cache-slot must not contaminate tenant cache-slot for module resource."""
    from src.mcp.resources import _render_module

    _clear_resource_cache()
    with as_tenant(None):
        admin_body, _ = _render_module(V, f"{_PFX}globex_mod")
    assert not _is_denied(admin_body), (
        f"Admin unexpectedly denied globex module: {admin_body!r}"
    )

    with as_tenant(world["acme"]):
        acme_body, _ = _render_module(V, f"{_PFX}globex_mod")
    assert _is_denied(acme_body), (
        f"CACHE CONTAMINATION (module): acme got admin body: {acme_body!r}"
    )


# ---------------------------------------------------------------------------
# R5 — view resource isolation (odoo://version/view/...)
# ---------------------------------------------------------------------------


def test_resource_view_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private view via odoo://view/."""
    from src.mcp.resources import _render_view

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        body, _ = _render_view(V, f"{_PFX}globex_mod.secret_view")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT VIEW RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_view_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must return the same scoped denial."""
    from src.mcp.resources import _render_view

    _clear_resource_cache()
    with as_tenant(world["acme"]):
        _render_view(V, f"{_PFX}globex_mod.secret_view")
        body, _ = _render_view(V, f"{_PFX}globex_mod.secret_view")
    assert "not found" in body.lower(), (
        f"CROSS-TENANT VIEW RESOURCE LEAK (cache-HIT): {body!r}"
    )


def test_resource_view_admin_and_tenant_cache_independent(world):
    """Admin cache-slot must not contaminate tenant cache-slot for view resource."""
    from src.mcp.resources import _render_view

    _clear_resource_cache()
    with as_tenant(None):
        admin_body, _ = _render_view(V, f"{_PFX}globex_mod.secret_view")
    assert not _is_denied(admin_body), (
        f"Admin unexpectedly denied globex view: {admin_body!r}"
    )

    with as_tenant(world["acme"]):
        acme_body, _ = _render_view(V, f"{_PFX}globex_mod.secret_view")
    assert _is_denied(acme_body), (
        f"CACHE CONTAMINATION (view): acme got admin body: {acme_body!r}"
    )


# ---------------------------------------------------------------------------
# FIX 5 — stylesheet resource isolation (odoo://version/stylesheet/...)
#
# Stylesheets carry a profile[] array, so a private-tenant stylesheet can exist.
# Before FIX 5 the stylesheet handler used a plain per-URI cache key (no tenant
# dimension), so an admin/other-tenant cache HIT could serve a foreign body
# without re-running _render_stylesheet's _scope_pred. These tests cover both
# the render-side scope (cache-MISS) and the cache-KEY tenant dimension.
# ---------------------------------------------------------------------------

_SS_FILE = "addons/{mod}/static/src/scss/secret.scss"


def test_resource_stylesheet_no_cross_tenant_leak_cache_miss(world):
    """Cache-MISS: acme cannot read globex's private stylesheet via odoo://stylesheet/."""
    from src.mcp.resources import _render_stylesheet

    _clear_resource_cache()
    fp = _SS_FILE.format(mod=f"{_PFX}globex_mod")
    with as_tenant(world["acme"]):
        body, _ = _render_stylesheet(V, f"{_PFX}globex_mod", fp)
    assert "not found" in body.lower(), (
        f"CROSS-TENANT STYLESHEET RESOURCE LEAK (cache-MISS): {body!r}"
    )


def test_resource_stylesheet_no_cross_tenant_leak_cache_hit(world):
    """Cache-HIT: second call must return the same scoped denial."""
    from src.mcp.resources import _render_stylesheet

    _clear_resource_cache()
    fp = _SS_FILE.format(mod=f"{_PFX}globex_mod")
    with as_tenant(world["acme"]):
        _render_stylesheet(V, f"{_PFX}globex_mod", fp)
        body, _ = _render_stylesheet(V, f"{_PFX}globex_mod", fp)
    assert "not found" in body.lower(), (
        f"CROSS-TENANT STYLESHEET RESOURCE LEAK (cache-HIT): {body!r}"
    )


def test_stylesheet_cache_key_is_tenant_scoped(world):
    """FIX 5 core: the stylesheet cache key MUST carry a tenant dimension so a
    cache HIT can never serve one tenant a body computed for another.

    Before the fix the stylesheet handler keyed solely on the URI (tenant-blind),
    so admin's cached body would be returned to a scoped tenant on HIT. This
    asserts admin and tenant resolve to DISTINCT cache slots — identical to the
    other 5 tenant-scoped kinds.
    """
    from src.mcp.resources import _tenant_cache_key

    fp = _SS_FILE.format(mod=f"{_PFX}globex_mod")
    entity = f"{_PFX}globex_mod/{fp}"
    with as_tenant(None):
        admin_key = _tenant_cache_key(V, "stylesheet", entity)
    with as_tenant(world["acme"]):
        acme_key = _tenant_cache_key(V, "stylesheet", entity)

    assert admin_key != acme_key, (
        "Stylesheet cache key is NOT tenant-scoped — admin and tenant share a "
        f"cache slot (latent cross-tenant leak). admin={admin_key!r} acme={acme_key!r}"
    )
    assert admin_key.endswith("::t_admin"), (
        f"admin stylesheet key missing ::t_admin dimension: {admin_key!r}"
    )
    assert "::t" in acme_key and not acme_key.endswith("::t_admin"), (
        f"tenant stylesheet key missing per-tenant dimension: {acme_key!r}"
    )


# ---------------------------------------------------------------------------
# R2 — resources_index scope filter
# ---------------------------------------------------------------------------


def test_resources_index_excludes_foreign_tenant_models(world):
    """R2: list_resources_index() must not include other tenants' private models
    in the discovery list when called in a scoped-tenant context.
    """
    from src.mcp.resources_index import list_resources_index

    # Globex's private model name — acme should NOT see it in the index.
    globex_model = f"{_PFX}globex_mod.model"

    with as_tenant(world["acme"]):
        entries = list_resources_index()

    model_names = {e["name"] for e in entries}
    assert globex_model not in model_names, (
        f"R2 OVER-INCLUSIVE DISCOVERY: acme's resources/list includes "
        f"globex's private model {globex_model!r}: {model_names!r}"
    )


# A second indexed version that ONLY globex has data for — lets us prove that
# version discovery (not just model discovery) is tenant-scoped (FIX 4).
_V_GLOBEX_ONLY = "98.0"


@pytest.fixture
def world_with_globex_only_version(world, clean_neo4j):
    """Augment `world` with a Module at version 98.0 carrying ONLY globex's
    private profile — no acme, no shared profile. So acme must never discover
    98.0, but admin must.
    """
    drv = clean_neo4j
    with drv.session() as s:
        s.run(
            "MERGE (m:Module {name:$mod, odoo_version:$v}) "
            "SET m.profile=$p, m.edition='community'",
            mod=f"{_PFX}globex_only_mod", v=_V_GLOBEX_ONLY,
            p=[world["globex_p"]],
        )
    yield world
    with drv.session() as s:
        s.run(
            "MATCH (m:Module {name:$mod, odoo_version:$v}) DETACH DELETE m",
            mod=f"{_PFX}globex_only_mod", v=_V_GLOBEX_ONLY,
        )


def test_fetch_indexed_versions_tenant_scoped(world_with_globex_only_version):
    """FIX 4: _fetch_indexed_versions must be tenant-scoped.

    Business rule: a scoped tenant only discovers Odoo versions it actually has
    (own or shared) data for. Version 98.0 exists ONLY behind globex's private
    profile, so acme must NOT see 98.0 in the discovery index, while admin
    (unrestricted) must.
    """
    from src.mcp.resources_index import _fetch_indexed_versions
    from src.mcp.server import _get_driver, _scope

    # Scoped tenant (acme) — must NOT see the globex-only version.
    with as_tenant(world_with_globex_only_version["acme"]):
        scope_acme = _scope()
        with _get_driver().session() as s:
            acme_versions = _fetch_indexed_versions(s, scope_acme)
    assert _V_GLOBEX_ONLY not in acme_versions, (
        f"FIX 4 VERSION LEAK: acme discovered globex-only version "
        f"{_V_GLOBEX_ONLY!r}: {acme_versions!r}"
    )

    # Admin (own=None) — must see every indexed version, including 98.0.
    with as_tenant(None):
        scope_admin = _scope()
        with _get_driver().session() as s:
            admin_versions = _fetch_indexed_versions(s, scope_admin)
    assert _V_GLOBEX_ONLY in admin_versions, (
        f"FIX 4 ADMIN BYPASS BROKEN: admin missing version {_V_GLOBEX_ONLY!r}: "
        f"{admin_versions!r}"
    )
