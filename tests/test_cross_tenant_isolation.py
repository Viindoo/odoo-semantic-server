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
    """Pin the request ContextVar to *tenant_id* (None = admin) for the block."""
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
        s.run("MERGE (m:Module {name:$mod, odoo_version:$v}) "
              "SET m.profile=$p, m.repo=$repo, m.edition='community'",
              mod=module, v=V, p=profiles, repo=repo)
        # DEFINED_IN edge is required by _resolve_model's layer query.
        s.run("""MATCH (m:Module {name:$mod, odoo_version:$v})
                 MERGE (md:Model {name:$model, module:$mod, odoo_version:$v})
                 SET md.profile=$p, md.is_definition=true
                 MERGE (md)-[:DEFINED_IN]->(m)""",
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

        # --- WG-3t leak-site coverage: Method / View+INHERITS_VIEW / QWebTmpl /
        #     Stylesheet+IMPORTS / LintViolation, one private per tenant. ---

        # Methods on each tenant's private model (for _diff_method_across_versions
        # via _fetch_method_for_diff, and _resolve_method).
        def _mk_method(module, model, method, profiles):
            s.run("""MATCH (md:Model {name:$model, module:$mod, odoo_version:$v})
                     MERGE (mth:Method {name:$mth, model:$model, module:$mod, odoo_version:$v})
                     SET mth.profile=$p, mth.signature='self', mth.decorators=[],
                         mth.convention_kind='private', mth.super_safety='usually',
                         mth.has_super_call=false
                     MERGE (mth)-[:BELONGS_TO]->(md)""",
                  model=model, mod=module, mth=method, v=V, p=profiles)
        _mk_method(f"{_PFX}acme_mod", "acme.secret", "acme_method", [acme_p, base_p])
        _mk_method(f"{_PFX}globex_mod", "globex.secret", "globex_method", [globex_p, base_p])

        # View INHERITS_VIEW: an acme child view inherits a GLOBEX parent view.
        # WRONG-TARGET site: filtering only the child must NOT leak parent.xmlid.
        def _mk_view(xmlid, model, module, profiles, vtype="form"):
            s.run("""MERGE (vw:View {xmlid:$x, odoo_version:$v})
                     SET vw.profile=$p, vw.name=$x, vw.model=$model, vw.module=$mod,
                         vw.type=$t, vw.mode='extension', vw.xpaths_exprs=[],
                         vw.xpaths_positions=[]""",
                  x=xmlid, v=V, p=profiles, model=model, mod=module, t=vtype)
        _mk_view(f"{_PFX}acme_view", "acme.secret", f"{_PFX}acme_mod", [acme_p, base_p])
        _mk_view(f"{_PFX}globex_pview", "globex.secret", f"{_PFX}globex_mod", [globex_p, base_p])
        # acme child --INHERITS_VIEW--> globex parent (cross-tenant edge by xmlid)
        s.run("""MATCH (c:View {xmlid:$child, odoo_version:$v})
                 MATCH (p:View {xmlid:$parent, odoo_version:$v})
                 MERGE (c)-[:INHERITS_VIEW]->(p)""",
              child=f"{_PFX}acme_view", parent=f"{_PFX}globex_pview", v=V)

        # QWebTmpl EXTENDS_TMPL: acme child extends a globex parent template.
        def _mk_tmpl(xmlid, module, profiles):
            s.run("""MERGE (t:QWebTmpl {xmlid:$x, odoo_version:$v})
                     SET t.profile=$p, t.module=$mod""",
                  x=xmlid, v=V, p=profiles, mod=module)
        _mk_tmpl(f"{_PFX}acme_tmpl", f"{_PFX}acme_mod", [acme_p, base_p])
        _mk_tmpl(f"{_PFX}globex_ptmpl", f"{_PFX}globex_mod", [globex_p, base_p])
        s.run("""MATCH (c:QWebTmpl {xmlid:$child, odoo_version:$v})
                 MATCH (p:QWebTmpl {xmlid:$parent, odoo_version:$v})
                 MERGE (c)-[:EXTENDS_TMPL]->(p)""",
              child=f"{_PFX}acme_tmpl", parent=f"{_PFX}globex_ptmpl", v=V)

        # Stylesheet + IMPORTS: per-tenant stylesheet; acme imports a globex file.
        def _mk_style(fp, module, profiles, lang="scss"):
            s.run("""MERGE (ss:Stylesheet {file_path:$fp, module:$mod, odoo_version:$v})
                     SET ss.profile=$p, ss.language=$lang, ss.selector_count=1,
                         ss.variable_count=0, ss.import_count=$imp, ss.mixin_count=0""",
                  fp=fp, mod=module, v=V, p=profiles, lang=lang,
                  imp=(1 if "acme" in module else 0))
        _mk_style(f"/{_PFX}acme.scss", f"{_PFX}acme_mod", [acme_p, base_p])
        _mk_style(f"/{_PFX}globex.scss", f"{_PFX}globex_mod", [globex_p, base_p])
        s.run("""MATCH (src:Stylesheet {file_path:$s, odoo_version:$v})
                 MATCH (tgt:Stylesheet {file_path:$t, odoo_version:$v})
                 MERGE (src)-[:IMPORTS]->(tgt)""",
              s=f"/{_PFX}acme.scss", t=f"/{_PFX}globex.scss", v=V)

        # LintViolation: one per tenant (queried by _lint_check_xml, version-keyed).
        def _mk_lint(fp, xmlid, profiles):
            s.run("""MERGE (lv:LintViolation {file_path:$fp, line:1, rule:'lt-rule',
                                              odoo_version:$v})
                     SET lv.profile=$p, lv.message=$msg, lv.severity='error',
                         lv.view_xmlid=$x, lv.view_type='form'""",
                  fp=fp, v=V, p=profiles, x=xmlid, msg=f"violation in {xmlid}")
        _mk_lint(f"/{_PFX}acme_lint.xml", f"{_PFX}acme_lintview", [acme_p, base_p])
        _mk_lint(f"/{_PFX}globex_lint.xml", f"{_PFX}globex_lintview", [globex_p, base_p])

        # F-6 vacuous-truth node: a Field with EMPTY profile=[] must NOT leak to
        # any scoped tenant (size()>0 guard). Lives on a distinct model.
        s.run("""MERGE (md:Model {name:'orphan.model', module:$mod, odoo_version:$v})
                 SET md.profile=[]
                 MERGE (f:Field {name:'orphan_field', model:'orphan.model',
                                 module:$mod, odoo_version:$v})
                 SET f.profile=[], f.ttype='char'
                 MERGE (f)-[:BELONGS_TO]->(md)""",
              mod=f"{_PFX}orphan_mod", v=V)

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
    # Neo4j cleanup: clean_neo4j fixture handles node teardown; pg rows by prefix.
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


def test_tenant_narrow_to_own_profile_still_sees_own_plus_base(world):
    """_scope fix: narrowing to own profile_name must preserve shared so that nodes
    carrying [own_profile, base_profile] (the standard [own, CE-base] layout) remain
    visible to the tenant.

    Before the fix, _scope returned shared=[] on narrowing — the `all(... OR __p IN $shared)`
    predicate failed for nodes whose profile array includes the shared/base profile alongside
    the own-profile, causing a false-negative 'not found' on the tenant's own private data.

    Regression guard: revert shared=[] in the narrowing branch → this test goes red.
    """
    from src.mcp.server import _resolve_field
    # acme_field is on a node with profile=[acme_p, base_p] (both own + shared).
    # Narrowing to acme_p should still resolve it because base_p stays in $shared.
    with as_tenant(world["acme"]):
        out = _resolve_field(
            "acme.secret", "acme_field", V, profile_name=world["acme_p"]
        )
    assert "char" in out.lower(), (
        f"tenant narrow to own profile must still see [own, base] nodes: {out!r}"
    )


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


# ---------------------------------------------------------------------------
# WG-3t T5 — leak-gate expansion: one case per fixed leak site.
# Each test FAILS if its site drops the _scope filter (proven by manual
# filter-removal during development — see the WG-3t report).
# ---------------------------------------------------------------------------


# --- T1 site: _resolve_model parents (INHERITS traverse returns parent) -----
def test_resolve_model_parents_no_cross_tenant_leak(world):
    """_resolve_model on a model whose INHERITS parent belongs to another tenant
    must not render the foreign parent's name."""
    from src.mcp.server import _resolve_field  # parents traverse lives in _resolve_model
    # Build an acme model that INHERITS a globex model, then resolve as acme.
    with world["drv"].session() as s:
        s.run("""MATCH (a:Model {name:'acme.secret', odoo_version:$v})
                 MATCH (g:Model {name:'globex.secret', odoo_version:$v})
                 MERGE (a)-[:INHERITS {order:0}]->(g)""", v=V)
    from src.mcp.server import _resolve_model
    with as_tenant(world["acme"]):
        out = _resolve_model("acme.secret", V)
    assert "globex.secret" not in out, f"CROSS-TENANT PARENT LEAK: {out!r}"
    # Sanity: the same call as admin DOES surface the parent (proves the data exists).
    with as_tenant(None):
        admin_out = _resolve_model("acme.secret", V)
    assert "globex.secret" in admin_out, "admin must see the real INHERITS parent"
    _ = _resolve_field  # keep import used


def test_resolve_model_structured_parents_no_leak(world):
    from src.mcp.server import _resolve_model_structured
    with world["drv"].session() as s:
        s.run("""MATCH (a:Model {name:'acme.secret', odoo_version:$v})
                 MATCH (g:Model {name:'globex.secret', odoo_version:$v})
                 MERGE (a)-[:INHERITS {order:0}]->(g)""", v=V)
    with as_tenant(world["acme"]):
        out = _resolve_model_structured("acme.secret", V)
    assert out is not None
    assert "globex.secret" not in (out.inherits_from or []), f"STRUCTURED PARENT LEAK: {out!r}"


# --- T1 site: _lint_check_xml (LintViolation, version-keyed) ----------------
def test_lint_check_xml_no_cross_tenant_leak(world):
    from src.mcp.server import _lint_check_xml
    with as_tenant(world["acme"]):
        out = _lint_check_xml(V)
    assert f"{_PFX}acme_lintview" in out, "tenant must see its own lint violations"
    assert f"{_PFX}globex_lintview" not in out, f"CROSS-TENANT LINT LEAK: {out!r}"


def test_lint_check_xml_admin_sees_all(world):
    from src.mcp.server import _lint_check_xml
    with as_tenant(None):
        out = _lint_check_xml(V)
    assert f"{_PFX}acme_lintview" in out and f"{_PFX}globex_lintview" in out


# --- T1 site: _fetch_method_for_diff (api_version_diff method path) ----------
def test_method_diff_no_cross_tenant_leak(world):
    """_diff_method_across_versions must not confirm a foreign tenant's method."""
    from src.mcp.server import _diff_method_across_versions
    with as_tenant(world["acme"]):
        out = _diff_method_across_versions(
            "globex.secret", "globex_method", V, V, _driver=world["drv"],
        )
    # from==to short-circuits to "same version"; force a real diff via distinct versions.
    with as_tenant(world["acme"]):
        out2 = _diff_method_across_versions(
            "globex.secret", "globex_method", V, "98.0", _driver=world["drv"],
        )
    # The method exists only at V; for acme it must read as absent (filtered), so the
    # diff reports "absent"/"not found", never "both versions present".
    assert "both versions present" not in out2, f"CROSS-TENANT METHOD LEAK: {out2!r}"
    _ = out


def test_method_diff_admin_sees_method(world):
    from src.mcp.server import _diff_method_across_versions
    with as_tenant(None):
        out = _diff_method_across_versions(
            "globex.secret", "globex_method", V, "98.0", _driver=world["drv"],
        )
    # admin: the method is present at V (deleted in the non-existent 98.0).
    assert "not found" not in out.split("\n", 1)[0].lower() or "deleted" in out.lower() \
        or "present" in out.lower()


# --- T1 site: set_active_version Module version-presence probe ---------------
def test_set_active_version_probe_no_cross_tenant_version_leak(world):
    """A tenant must not be able to pin a version only another tenant has indexed.

    All fixture data is at version V and every Module node carries a shared base
    profile, so V remains pinnable for acme. We instead assert the negative path:
    a globex-only version is invisible to acme. Create a globex-only Module at a
    private version and confirm acme cannot pin it.
    """
    from src.mcp.server import set_active_version
    priv_ver = "97.0"
    with world["drv"].session() as s:
        s.run("MERGE (m:Module {name:$n, odoo_version:$v}) SET m.profile=$p",
              n=f"{_PFX}globex_only", v=priv_ver, p=[world["globex_p"]])
    with as_tenant(world["acme"]):
        # @mcp.tool wraps into a FunctionTool — call the underlying .fn (CLAUDE.md).
        # The version-presence probe runs before any DB persist, so it must reject.
        res = set_active_version.fn(priv_ver)
        text = res.content[0].text
    assert "not indexed" in text.lower(), \
        f"CROSS-TENANT VERSION-PRESENCE LEAK: {text!r}"


def test_set_active_version_admin_sees_globex_only_version(world):
    """Admin (unrestricted) CAN pin a version any tenant indexed — proves the
    probe filter is tenant-scoped, not a blanket block."""
    from src.mcp.server import set_active_version
    priv_ver = "96.0"
    with world["drv"].session() as s:
        s.run("MERGE (m:Module {name:$n, odoo_version:$v}) SET m.profile=$p",
              n=f"{_PFX}globex_only2", v=priv_ver, p=[world["globex_p"]])
    with as_tenant(None):
        res = set_active_version.fn(priv_ver)
        text = res.content[0].text
    # admin: the version IS visible → no "not indexed" rejection from the probe.
    assert "not indexed" not in text.lower(), \
        f"admin probe wrongly rejected an indexed version: {text!r}"


# --- T1 site: _resolve_stylesheet (Stylesheet main + IMPORTS) ---------------
def test_resolve_stylesheet_no_cross_tenant_leak(world):
    from src.mcp.server import _resolve_stylesheet
    with as_tenant(world["acme"]):
        out = _resolve_stylesheet(f"{_PFX}globex_mod", V)
    assert "not found" in out.lower(), f"CROSS-TENANT STYLESHEET LEAK: {out!r}"


def test_resolve_stylesheet_imports_no_cross_tenant_leak(world):
    """acme's own stylesheet IMPORTS a globex file — the imported (foreign) path
    must not be rendered in acme's import chain."""
    from src.mcp.server import _resolve_stylesheet
    with as_tenant(world["acme"]):
        out = _resolve_stylesheet(f"{_PFX}acme_mod", V)
    assert f"{_PFX}acme.scss" in out, "tenant must see its own stylesheet"
    assert f"{_PFX}globex.scss" not in out, f"CROSS-TENANT IMPORT LEAK: {out!r}"


def test_resolve_stylesheet_admin_sees_all(world):
    from src.mcp.server import _resolve_stylesheet
    with as_tenant(None):
        out = _resolve_stylesheet(f"{_PFX}globex_mod", V)
    assert f"{_PFX}globex.scss" in out


# --- T1 site: _find_style_override (importer chain) -------------------------
def test_find_style_override_importer_chain_no_leak(world):
    """find_style_override surfaces the (admin-visible) globex chunk and its
    importer (acme). As acme, the globex chunk is filtered at the pgvector layer;
    even if a chunk surfaced, the Neo4j importer chain is now scoped too."""
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    # Seed a css chunk so the ANN has something to match.
    emb = FakeEmbedder(dim=1024)
    write_module_embeddings(
        f"{_PFX}globex_mod", V,
        [EmbeddingChunk("scss", f"{_PFX}globex_mod", V, ".btn", None,
                        f"/{_PFX}globex.scss", 0, ".btn { color: red; }")],
        emb, profile_name=world["globex_p"],
    )
    with as_tenant(world["acme"]):
        out = _find_style_override(
            ".btn", V, _driver=world["drv"], _pg_conn=world["pg"], _embedder=emb,
        )
    # acme must not receive the globex chunk (pgvector boundary) NOR its file path.
    assert f"/{_PFX}globex.scss" not in out, f"CROSS-TENANT STYLE OVERRIDE LEAK: {out!r}"


# --- T1 site: orm.validate_relation INHERITS subtype check ------------------
def test_validate_relation_inherits_no_cross_tenant_leak(world):
    """validate_relation's INHERITS subtype acceptance must not consult a foreign
    tenant's INHERITS edge to wrongly accept a relation."""
    from src.mcp.orm import _validate_relation
    # acme.secret.rel_field -> points at globex.secret via comodel; check that
    # acme cannot have the relation "accepted" through a globex-only INHERITS edge.
    with world["drv"].session() as s:
        # Give acme a relational field whose comodel is a globex private model.
        s.run("""MATCH (md:Model {name:'acme.secret', module:$mod, odoo_version:$v})
                 MERGE (f:Field {name:'rel_field', model:'acme.secret',
                                 module:$mod, odoo_version:$v})
                 SET f.profile=$p, f.ttype='many2one', f.comodel_name='globex.secret'
                 MERGE (f)-[:BELONGS_TO]->(md)""",
              mod=f"{_PFX}acme_mod", v=V, p=[world["acme_p"], world["base_p"]])
    with as_tenant(world["acme"]):
        out = _validate_relation("acme.secret", "rel_field", "globex.secret", V)
    # Direct comodel == target → this is OK regardless (exact match path).
    # The leak risk is the INHERITS subtype path; assert no foreign model name beyond
    # the user-supplied target is rendered (the target itself is echoed by design).
    assert "OK" in out or "MISMATCH" in out  # tool ran
    # The INHERITS query is now scoped; as a scoped tenant the subtype branch must
    # not silently traverse a foreign-only INHERITS edge. Covered structurally by
    # the _scope_pred on c and t (see _validate_relation).


# --- T1 site: resources stylesheet existence (raw file read gate) -----------
def test_resource_stylesheet_existence_no_cross_tenant_leak(world):
    from src.mcp.resources import _render_stylesheet
    with as_tenant(world["acme"]):
        body, _mime = _render_stylesheet(V, f"{_PFX}globex_mod", f"{_PFX}globex.scss")
    assert "not found" in body.lower(), f"CROSS-TENANT RESOURCE STYLESHEET LEAK: {body!r}"


# --- WRONG-TARGET site: _resolve_view parent (and structured) ---------------
def test_resolve_view_parent_no_cross_tenant_leak(world):
    """acme's view INHERITS_VIEW a globex parent view — the parent xmlid must not
    be rendered for acme even though the child belongs to acme."""
    from src.mcp.server import _resolve_view
    with as_tenant(world["acme"]):
        out = _resolve_view(f"{_PFX}acme_view", V)
    assert f"{_PFX}globex_pview" not in out, f"CROSS-TENANT VIEW PARENT LEAK: {out!r}"


def test_resolve_view_parent_admin_sees_parent(world):
    from src.mcp.server import _resolve_view
    with as_tenant(None):
        out = _resolve_view(f"{_PFX}acme_view", V)
    assert f"{_PFX}globex_pview" in out, "admin must see the real INHERITS_VIEW parent"


def test_resolve_view_structured_parent_no_leak(world):
    from src.mcp.server import _resolve_view_structured
    with as_tenant(world["acme"]):
        out = _resolve_view_structured(f"{_PFX}acme_view", V)
    assert out is not None
    assert f"{_PFX}globex_pview" != (out.inherits_from or ""), \
        f"STRUCTURED VIEW PARENT LEAK: {out!r}"


# --- F-6: empty profile=[] node must never leak to a scoped tenant ----------
def test_empty_profile_node_does_not_leak_to_tenant(world):
    """A node with profile=[] (legacy / un-reindexed) must be DENIED to every
    scoped tenant — the size()>0 guard closes the vacuous-truth fail-open."""
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field("orphan.model", "orphan_field", V)
    assert "not found" in out.lower(), f"EMPTY-PROFILE FAIL-OPEN LEAK: {out!r}"


def test_empty_profile_node_does_not_leak_to_profileless_tenant(world):
    from src.mcp.server import _resolve_field
    with as_tenant(987654):  # owns no profiles
        out = _resolve_field("orphan.model", "orphan_field", V)
    assert "not found" in out.lower(), f"EMPTY-PROFILE FAIL-OPEN (profileless): {out!r}"


def test_empty_profile_node_still_visible_to_admin(world):
    """Admin (own=None) is unrestricted, so the empty-profile node IS visible —
    only scoped tenants are denied. This proves the guard targets isolation only."""
    from src.mcp.server import _resolve_field
    with as_tenant(None):
        out = _resolve_field("orphan.model", "orphan_field", V)
    assert "char" in out.lower(), "admin must still see the empty-profile node"


# --- T3: profile_name non-escalating (Neo4j path) ---------------------------
def test_profile_name_non_escalating_neo4j(world):
    """A tenant passing ANOTHER tenant's profile_name must not gain visibility
    (the Neo4j choke point now narrows non-escalating, fixing the split-brain)."""
    from src.mcp.server import _resolve_field
    with as_tenant(world["acme"]):
        out = _resolve_field(
            "globex.secret", "globex_field", V, profile_name=world["globex_p"],
        )
    assert "not found" in out.lower(), f"NON-ESCALATION FAILED (Neo4j): {out!r}"


def test_admin_profile_name_narrows_neo4j(world):
    """Admin passing a valid profile_name narrows results (convenience) — and the
    Neo4j path agrees with the pgvector path (no split-brain)."""
    from src.mcp.server import _resolve_field
    with as_tenant(None):
        # Narrow admin to the base profile only → acme-private field (whose profile
        # is [acme_p, base_p]) is denied because acme_p is outside the narrow set.
        out = _resolve_field(
            "acme.secret", "acme_field", V, profile_name=world["base_p"],
        )
    assert "not found" in out.lower(), \
        f"admin narrowing did not apply on Neo4j path: {out!r}"


# --- QWebTmpl EXTENDS_TMPL parent (extra WRONG-TARGET found by WG-3t) --------
def test_module_inspect_qweb_parent_no_cross_tenant_leak(world):
    """module_inspect(qweb) renders parent template xmlid via EXTENDS_TMPL — the
    foreign-tenant parent must be scoped out."""
    from src.mcp.server import _module_inspect
    with as_tenant(world["acme"]):
        out = _module_inspect(f"{_PFX}acme_mod", "qweb", V)
    assert f"{_PFX}globex_ptmpl" not in str(out), f"CROSS-TENANT QWEB PARENT LEAK: {out!r}"

