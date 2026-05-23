# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cross_tenant_isolation.py
"""WI-4 RELEASE GATE — cross-tenant isolation leak test (ADR-0034).

Sets up two tenants (acme, globex) sharing a base profile, each with a private
module, plus a global spec symbol and per-profile embeddings. Then, with the
request thread-local pinned to one tenant, asserts that every user-data tool:

  * RETURNS the tenant's own + shared-base data,
  * NEVER returns the OTHER tenant's private data (with or without an explicit
    profile_name, and even when explicitly asked for the other tenant's profile),
  * still exposes the GLOBAL shared spec/pattern data (D3 exemption),
  * is fully UNRESTRICTED for the admin/global (None tenant) path.

The isolation guarantee here holds for distinctly-named private modules (the
operator namespacing convention, ADR-0034 Amendment A3). Identically-named
private modules across tenants are the documented residual (deferred REC-8).

Pinning the tenant requires a real pg (profiles with tenant_id, for
resolve_allowed_profiles) + a Neo4j with profile-tagged nodes + pgvector chunks.
"""
from contextlib import contextmanager

import pytest

from tests.conftest import PG_EMBED_VERSION as V  # "99.0"

pytestmark = pytest.mark.postgres

_PFX = "lt_"  # all rows created here use this prefix → easy, collision-free cleanup


@contextmanager
def as_tenant(tenant_id):
    """Pin the request thread-local to *tenant_id* (None = admin) for the block."""
    from src.mcp import session
    from src.mcp.server import _tenant_id_local

    session.invalidate_allowed_profiles()
    old = getattr(_tenant_id_local, "value", None)
    _tenant_id_local.value = tenant_id
    try:
        yield
    finally:
        _tenant_id_local.value = old
        session.invalidate_allowed_profiles()


def _cleanup(pg):
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()


@pytest.fixture
def world(clean_pg_embeddings, clean_neo4j):
    """Two tenants + shared base; private modules + global spec + embeddings."""
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    pg = clean_pg_embeddings
    drv = clean_neo4j
    _cleanup(pg)

    # --- pg: tenants + profiles (base = shared/NULL tenant; acme/globex scoped) ---
    with pg.cursor() as cur:
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}acme') RETURNING id")
        acme = cur.fetchone()[0]
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}globex') RETURNING id")
        globex = cur.fetchone()[0]
        cur.execute(
            f"INSERT INTO profiles (name, odoo_version) VALUES ('{_PFX}base', %s) RETURNING id",
            (V,),
        )
        base = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id, parent_profile_id) "
            f"VALUES ('{_PFX}acme_p', %s, %s, %s)",
            (V, acme, base),
        )
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id, parent_profile_id) "
            f"VALUES ('{_PFX}globex_p', %s, %s, %s)",
            (V, globex, base),
        )
    pg.commit()

    base_p, acme_p, globex_p = f"{_PFX}base", f"{_PFX}acme_p", f"{_PFX}globex_p"

    # --- Neo4j: shared base + per-tenant private modules/models/fields ---
    def _mk(s, module, model, field, profiles, repo):
        s.run("MERGE (m:Module {name:$mod, odoo_version:$v}) SET m.profile=$p, m.repo=$repo",
              mod=module, v=V, p=profiles, repo=repo)
        s.run("MERGE (md:Model {name:$model, module:$mod, odoo_version:$v}) SET md.profile=$p",
              model=model, mod=module, v=V, p=profiles)
        s.run("""MATCH (md:Model {name:$model, module:$mod, odoo_version:$v})
                 MERGE (f:Field {name:$fld, model:$model, module:$mod, odoo_version:$v})
                 SET f.profile=$p, f.ttype='char'
                 MERGE (f)-[:BELONGS_TO]->(md)""",
              model=model, mod=module, fld=field, v=V, p=profiles)

    with drv.session() as s:
        _mk(s, f"{_PFX}base_mod", "shared.model", "shared_field", [base_p], "base")
        _mk(s, f"{_PFX}acme_mod", "acme.secret", "acme_field", [acme_p, base_p], "acme")
        _mk(s, f"{_PFX}globex_mod", "globex.secret", "globex_field", [globex_p, base_p], "globex")
        # global spec symbol — has NO profile property → must stay visible to all (D3)
        s.run("MERGE (cs:CoreSymbol {qualified_name:'odoo.fields.Char', odoo_version:$v}) "
              "SET cs.kind='field_type', cs.status='stable'", v=V)

    # --- pgvector: one method chunk per profile ---
    emb = FakeEmbedder(dim=1024)

    def _emb(module, prof, text):
        chunk = EmbeddingChunk("method", module, V, f"{module}.do", None, f"/{module}.py", 0, text)
        write_module_embeddings(module, V, [chunk], emb, profile_name=prof)

    _emb(f"{_PFX}base_mod", base_p, "shared base helper method body")
    _emb(f"{_PFX}acme_mod", acme_p, "acme private secret method body")
    _emb(f"{_PFX}globex_mod", globex_p, "globex private secret method body")

    yield {"pg": pg, "drv": drv, "acme": acme, "globex": globex,
           "acme_p": acme_p, "globex_p": globex_p, "base_p": base_p}
    _cleanup(pg)


# ---------------------------------------------------------------------------
# Neo4j user-data isolation (resolve_field / resolve_model)
# ---------------------------------------------------------------------------


def test_tenant_sees_own_field(world):
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field("acme.secret", "acme_field", V)
    assert "char" in out.lower()  # field found + rendered


def test_tenant_cannot_see_other_tenant_field(world):
    """THE GATE: acme must NOT resolve globex's private field.

    The field EXISTS in Neo4j but is filtered out for acme, so resolve returns the
    "not found" message (which echoes the requested field name — hence we assert on
    the not-found marker + the ABSENCE of the rendered field detail, not the name).
    """
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field("globex.secret", "globex_field", V)
    assert "not found" in out.lower(), f"CROSS-TENANT LEAK: {out!r}"
    assert "Declared in" not in out, f"CROSS-TENANT LEAK (detail rendered): {out!r}"


def test_tenant_cannot_see_other_tenant_field_even_with_explicit_profile(world):
    """Explicitly asking for the other tenant's profile is denied (outside boundary)."""
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field("globex.secret", "globex_field", V, profile_name=world["globex_p"])
    assert "not found" in out.lower(), f"CROSS-TENANT LEAK via explicit profile: {out!r}"
    assert "Declared in" not in out, f"CROSS-TENANT LEAK (detail rendered): {out!r}"


def test_tenant_sees_shared_base(world):
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field("shared.model", "shared_field", V)
    assert "char" in out.lower(), "tenant must still see the shared base data"


def test_profileless_tenant_sees_only_shared_base(world):
    """A tenant that owns no profiles (own=[]) still sees the GLOBAL shared base
    (every profile on a base node is in `shared`), but NO tenant-private node."""
    from src.mcp.server import _resolve_field
    with as_tenant(999999):  # a tenant id that owns no profiles
        base = _resolve_field("shared.model", "shared_field", V)
        private = _resolve_field("acme.secret", "acme_field", V)
    assert "not found" not in base.lower(), f"profileless must still see shared base: {base!r}"
    assert "not found" in private.lower(), f"profileless must NOT see tenant-private: {private!r}"


def test_admin_sees_everything(world):
    """No tenant context (None) = admin/global = unrestricted."""
    from src.mcp.server import _resolve_field
    with as_tenant(None):
        a = _resolve_field("acme.secret", "acme_field", V)
        g = _resolve_field("globex.secret", "globex_field", V)
    assert "char" in a.lower() and "char" in g.lower()


# ---------------------------------------------------------------------------
# Global spec data exemption (D3) — visible to every tenant
# ---------------------------------------------------------------------------


def test_global_spec_visible_to_scoped_tenant(world):
    """CoreSymbol (no profile property) must remain visible — spec data is global."""
    from src.mcp.server import _lookup_core_api
    with as_tenant(world["acme"]):
        out = _lookup_core_api("Char", V)
    assert "odoo.fields.Char" in out, "global spec data must not be tenant-filtered"


# ---------------------------------------------------------------------------
# pgvector ANN isolation (find_examples) — C3
# ---------------------------------------------------------------------------


def test_find_examples_no_cross_tenant_leak(world):
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples
    with as_tenant(world["acme"]):
        out = _find_examples(
            "secret method", V,
            _driver=world["drv"], _pg_conn=world["pg"], _embedder=FakeEmbedder(dim=1024),
        )
    assert "globex" not in out, f"CROSS-TENANT EMBEDDING LEAK: {out!r}"


def test_find_examples_admin_sees_all(world):
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _find_examples
    with as_tenant(None):
        out = _find_examples(
            "secret method", V,
            _driver=world["drv"], _pg_conn=world["pg"], _embedder=FakeEmbedder(dim=1024),
        )
    # admin path: at least one of the private modules surfaces (unrestricted)
    assert (f"{_PFX}globex_mod" in out) or (f"{_PFX}acme_mod" in out)

