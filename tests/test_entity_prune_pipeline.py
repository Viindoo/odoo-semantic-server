# SPDX-License-Identifier: AGPL-3.0-or-later
"""Intra-module entity prune (B14, review M13 / G4): the index keeps only what a
LIVE module's source still defines.

Real shape: the ``viin_ai_*`` consolidation wave split and merged modules while
the survivors kept their names. Before B14 a field, method or view removed from a
module that still exists stayed in Neo4j forever, so ``validate_domain`` kept
accepting a field the source no longer has - a correctness bug under the product
rule "never blur data for the AI". The rule now (08-implementation-plan B14,
07-solution-review section 5):

* a module re-parsed in run R loses every child (node AND relationship) the parse
  did not re-write in R, together with its embedding rows;
* (a) a module NOT re-parsed is never pruned;
* (b) a degraded parse (a file could not be read or parsed) prunes nothing and
  keeps the embeddings; a transient IO failure is retried once per failure
  fingerprint, a content (syntax) failure only when a commit touches the module;
* (c) a module another present repo still ships is skipped; an unsynced repo that
  may ship it defers the prune until it syncs; legacy multi-profile ``Module``
  arrays never block;
* (d) a mass drop (> 50 % and >= 20 of the module's nodes OR relationships) is
  held - graph and embeddings unchanged, operator attention, CLI exit 3 - until
  ``--allow-mass-retire``.

Every test drives the REAL ``index_profile`` (or the real CLI ``main``) over temp
git repos with a bare origin, against a real Neo4j and PostgreSQL + pgvector.
Expected values come from those rules, never from what the implementation returns.
"""
from __future__ import annotations

import shutil
import stat
import sys
import textwrap
from collections import Counter
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.indexer.embedder import FakeEmbedder
from tests import _retirement_fixture as rf
from tests._lifecycle_repo import (
    GitRepo,
    V,
    lc,
    ledger,
    module_node,
    needs_attention,
    register,
    repo_row,
    run,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

RAG = rf.RETIRED  # "viin_ai_rag" - here a LIVE module that loses entities
AI = rf.SURVIVOR  # "viin_ai"


# ---------------------------------------------------------------------------
# Fixture + repo builders
# ---------------------------------------------------------------------------

@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


def _with_rng(repo: GitRepo) -> None:
    """Ship the core RelaxNG schemas so views get linted (LintViolation exists)."""
    rng = repo.path / "odoo" / "addons" / "base" / "rng"
    rng.mkdir(parents=True, exist_ok=True)
    for f in rf.RNG_DIR.iterdir():
        shutil.copy(f, rng / f.name)


def _rag_repo(parent: Path, name: str = "viindoo_addons") -> GitRepo:
    """``viin_ai`` + ``viin_ai_rag`` (one artifact of every kind) + RNG, pushed."""
    repo = GitRepo(parent, name)
    rf.write_viin_ai(repo.path)
    rf.write_viin_ai_rag(repo.path)
    _with_rng(repo)
    repo.commit("add viin_ai and viin_ai_rag")
    return repo


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _rag_models(repo: GitRepo, text: str) -> None:
    _write(repo.path / RAG / "models" / "ai_rag_source.py", text)


# The rag models without ``rag_count`` and its compute method (the split residue).
_RAG_MODELS_NO_RAG_COUNT = rf._RAG_MODELS.split("    rag_count = fields.Integer(")[0]
assert "_compute_rag_count" not in _RAG_MODELS_NO_RAG_COUNT
assert "rag_source_ids" in _RAG_MODELS_NO_RAG_COUNT


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def _count(driver, label: str, **props) -> int:
    where = " AND ".join(f"n.{k} = ${k}" for k in props)
    with driver.session() as s:
        return s.run(
            f"MATCH (n:{label}) WHERE n.odoo_version = $v"
            + (f" AND {where}" if where else "")
            + " RETURN count(n) AS n",
            v=V, **props,
        ).single()["n"]


def _nodes(driver, module: str) -> Counter:
    """Multiset of the module's child nodes (label + identifying properties),
    LintViolations included (they carry no ``module``, only the file path)."""
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (n) WHERE n.odoo_version = $v AND NOT n:Module
              AND (n.module = $m
                   OR (n:LintViolation AND n.file_path STARTS WITH ($m + '/')))
            RETURN labels(n) AS l, n.xmlid AS x, n.model AS mo, n.name AS na,
                   n.file_path AS f, n.class_name AS c
            """,
            v=V, m=module,
        ).data()
    return Counter(
        (tuple(sorted(r["l"])), r["x"], r["mo"], r["na"], r["f"], r["c"]) for r in rows
    )


def _node_key(alias: str) -> str:
    return (
        f"[labels({alias})[0], coalesce({alias}.module, ''), coalesce({alias}.xmlid, ''),"
        f" coalesce({alias}.model, ''), coalesce({alias}.name, ''),"
        f" coalesce({alias}.file_path, '')]"
    )


# Relationships the index derives in version-wide post-passes (not by a module's
# parse): never pruned per module - each post-pass recomputes its own edges every
# run and deletes the ones it no longer derives (reconcile_test_surface for the
# test edges, reconcile_owl_edges for OWLComp EXTENDS / BOUND_TO). Exact
# before/after comparisons of a module's parse output leave them out unless asked.
_POST_PASS_REL_TYPES = {
    "INHERITS_TEST", "COVERS_MODEL", "COVERS_FIELD", "COVERS_METHOD", "EXTENDS", "BOUND_TO",
}


def _rels(driver, module: str, *, post_pass: bool = False) -> Counter:
    """Multiset of relationships starting at the module's Module node or at one of
    its child nodes: (start key, type, end key)."""
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (a)-[r]->(b)
            WHERE a.odoo_version = $v
              AND ((a:Module AND a.name = $m) OR (NOT a:Module AND a.module = $m))
              AND ($post_pass OR NOT type(r) IN $derived)
            RETURN {_node_key('a')} AS a, type(r) AS t, {_node_key('b')} AS b
            """,
            v=V, m=module, post_pass=post_pass, derived=sorted(_POST_PASS_REL_TYPES),
        ).data()
    return Counter((tuple(r["a"]), r["t"], tuple(r["b"])) for r in rows)


def _has_rel(driver, module: str, rel_type: str, *, end_name: str | None = None,
             end_xmlid: str | None = None, start_name: str | None = None) -> int:
    with driver.session() as s:
        return s.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a.odoo_version = $v AND type(r) = $t
              AND ((a:Module AND a.name = $m) OR (NOT a:Module AND a.module = $m))
              AND ($start IS NULL OR a.name = $start)
              AND ($end IS NULL OR b.name = $end)
              AND ($endx IS NULL OR b.xmlid = $endx)
            RETURN count(r) AS n
            """,
            v=V, m=module, t=rel_type, start=start_name, end=end_name, endx=end_xmlid,
        ).single()["n"]


def _emb(pg_conn, module: str, profile: str | None = None) -> set[tuple[str, str]]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_type, entity_name FROM embeddings WHERE odoo_version = %s "
            "AND module = %s AND (%s::text IS NULL OR profile_name = %s::text)",
            (V, module, profile, profile),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def _tools():
    """The ORM-validation / impact tools, bound to the test Neo4j."""
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _impact_analysis, _resolve_orm_chain, _validate_domain
    return _validate_domain, _resolve_orm_chain, _impact_analysis


def _attention(pg_conn, rid: int) -> str:
    return repo_row(pg_conn, rid).get("lifecycle_attention") or ""


def _strip_run_tokens(driver) -> None:
    """Simulate data written by pre-B14 code: no node or relationship carries a token."""
    with driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v REMOVE n.written_run", v=V).consume()
        s.run(
            "MATCH (n)-[r]->() WHERE n.odoo_version = $v REMOVE r.written_run", v=V,
        ).consume()


# Entities of viin_ai_rag the first commit ships and the second one removes.
_REMOVED_NODES = [
    ("Field", {"module": RAG, "model": "ai.assistant", "name": "rag_count"}),
    ("Method", {"module": RAG, "model": "ai.assistant", "name": "_compute_rag_count"}),
    ("Method", {"module": RAG, "model": "ai.rag.source", "name": "action_reindex"}),
    ("View", {"xmlid": f"{RAG}.ai_rag_source_view_list"}),
    ("QWebTmpl", {"xmlid": f"{RAG}.report_rag_source_document"}),
    ("Report", {"module": RAG}),
    ("JSPatch", {"module": RAG}),
    ("OWLComp", {"module": RAG, "name": "RagSourcePanel"}),
    ("Stylesheet", {"module": RAG}),
    ("JsTestSuite", {"module": RAG}),
    ("TestClass", {"module": RAG, "name": "TestRagSource"}),
    ("TestMethod", {"module": RAG, "name": "test_reindex_returns_true"}),
]
_REMOVED_EMBEDDINGS = {
    ("field", "ai.assistant.rag_count"),
    ("method", "ai.assistant._compute_rag_count"),
    ("method", "ai.rag.source.action_reindex"),
    ("view", f"{RAG}.ai_rag_source_view_list"),
    ("qweb", f"{RAG}.report_rag_source_document"),
    ("js_era3", "FormController"),
    ("js_era3", "RagSourcePanel"),
    ("js_test", f"{RAG}/static/tests/rag_source_panel.test.js"),
    ("scss", "selector:.o_rag_source_panel"),
    ("test_class", "TestRagSource.__class__"),
    ("test_method", "TestRagSource.test_reindex_returns_true"),
}
_SURVIVING_NODES = [
    ("Model", {"module": RAG, "name": "ai.rag.source"}),
    ("Model", {"module": RAG, "name": "ai.assistant"}),
    ("Field", {"module": RAG, "model": "ai.rag.source", "name": "name"}),
    ("Field", {"module": RAG, "model": "ai.rag.source", "name": "assistant_id"}),
    ("Field", {"module": RAG, "model": "ai.assistant", "name": "rag_source_ids"}),
    ("View", {"xmlid": f"{RAG}.ai_rag_source_view_form"}),
    ("View", {"xmlid": f"{RAG}.ai_assistant_view_form_rag"}),
    ("TestClass", {"module": RAG, "name": "RagCommon"}),
]


def _remove_rag_entities(repo: GitRepo) -> str:
    """The split residue: viin_ai_rag stays, but drops one of every artifact kind."""
    mod = repo.path / RAG
    _rag_models(
        repo,
        _RAG_MODELS_NO_RAG_COUNT.replace(
            "\n    def action_reindex(self):\n        return True\n", "\n",
        ),
    )
    views = rf._RAG_VIEWS
    start = views.index('    <record id="ai_rag_source_view_list"')
    end = views.index('    <record id="ai_assistant_view_form_rag"')
    _write(mod / "views" / "ai_rag_source_views.xml", views[:start] + views[end:])
    shutil.rmtree(mod / "report")
    shutil.rmtree(mod / "static" / "src" / "js")
    shutil.rmtree(mod / "static" / "src" / "components")
    shutil.rmtree(mod / "static" / "src" / "scss")
    shutil.rmtree(mod / "static" / "tests")
    (mod / "tests" / "test_rag_source.py").unlink()
    _write(mod / "tests" / "__init__.py", "")
    return repo.commit(f"[REM] {RAG}: move rag_count, the list view, report and assets out")


# ---------------------------------------------------------------------------
# Core rule: removed entities leave the graph, their embeddings, and the tools
# ---------------------------------------------------------------------------

def test_entities_a_live_module_no_longer_defines_leave_graph_embeddings_and_tools(
    pg, neo4j_driver, tmp_path,
):
    """A field, methods, a view with its lint violation, a report + template, a JS
    patch, an OWL component, a stylesheet, a JS test suite and a test class/method
    removed from the LIVE module viin_ai_rag are gone after ONE plain incremental
    run - nodes and embedding rows - and validate_domain / resolve_orm_chain now
    report the removed field as not found. Everything the module still defines,
    and the untouched module viin_ai, stay exactly as they were."""
    repo = _rag_repo(tmp_path)
    (rid,) = register("viindoo_99", repo)
    run(pg, "viindoo_99")

    # Positive control: every entity the test expects to vanish was indexed.
    for label, props in _REMOVED_NODES:
        assert _count(neo4j_driver, label, **props) >= 1, (label, props)
    assert rf.lint_violations_of(neo4j_driver, RAG) >= 1
    assert _REMOVED_EMBEDDINGS <= _emb(pg, RAG)
    validate_domain, resolve_orm_chain, _ = _tools()
    before_domain = validate_domain("ai.assistant", "[('rag_count', '>', 0)]", V)
    assert "ERROR" not in before_domain, before_domain
    assert "BROKEN" not in resolve_orm_chain("ai.assistant", "rag_count", V)
    ai_nodes, ai_rels, ai_emb = (
        _nodes(neo4j_driver, AI), _rels(neo4j_driver, AI), _emb(pg, AI),
    )

    _remove_rag_entities(repo)
    summary = run(pg, "viindoo_99")

    for label, props in _REMOVED_NODES:
        assert _count(neo4j_driver, label, **props) == 0, (
            f"{label} {props} is no longer in the source and must leave the graph"
        )
    assert rf.lint_violations_of(neo4j_driver, RAG) == 0
    left = _REMOVED_EMBEDDINGS & _emb(pg, RAG)
    assert not left, f"embedding rows of removed entities survived: {sorted(left)}"
    for label, props in _SURVIVING_NODES:
        assert _count(neo4j_driver, label, **props) == 1, (label, props)
    assert {("field", "ai.rag.source.name"), ("view", f"{RAG}.ai_rag_source_view_form")} <= (
        _emb(pg, RAG)
    )
    assert module_node(neo4j_driver, RAG) is not None
    assert _nodes(neo4j_driver, AI) == ai_nodes
    assert _rels(neo4j_driver, AI) == ai_rels
    assert _emb(pg, AI) == ai_emb
    assert not needs_attention(summary), lc(summary)

    validate_domain, resolve_orm_chain, _ = _tools()
    after_domain = validate_domain("ai.assistant", "[('rag_count', '>', 0)]", V)
    assert "ERROR" in after_domain and "field 'rag_count' not found" in after_domain, (
        after_domain
    )
    chain = resolve_orm_chain("ai.assistant", "rag_count", V)
    assert "BROKEN" in chain and "field 'rag_count' not found" in chain, chain
    # The field the module still defines keeps validating.
    kept = validate_domain("ai.assistant", "[('rag_source_ids', '!=', False)]", V)
    assert "ERROR" not in kept, kept


# ---------------------------------------------------------------------------
# Relationships a module no longer declares
# ---------------------------------------------------------------------------

def _rel_module(repo: GitRepo, name: str, *, depends: list[str], models_py: str,
                views_xml: str | None = None, assets: dict | None = None,
                js: str | None = None) -> None:
    mod = repo.path / name
    manifest = {"name": name, "version": f"{V}.1.0", "depends": depends,
                "installable": True, "license": "LGPL-3"}
    if assets:
        manifest["assets"] = assets
    _write(mod / "__manifest__.py", repr(manifest) + "\n")
    _write(mod / "__init__.py", "from . import models\n")
    _write(mod / "models" / "__init__.py", "from . import models\n")
    _write(mod / "models" / "models.py", textwrap.dedent(models_py))
    if views_xml:
        _write(mod / "views" / "views.xml", textwrap.dedent(views_xml))
    if js:
        _write(mod / "static" / "src" / "js" / "panel.js", js)


_AI_MODELS = """\
    from odoo import fields, models


    class AiMixin(models.AbstractModel):
        _name = "ai.mixin"
        _description = "AI Mixin"

        mixin_note = fields.Char()


    class AiAssistant(models.Model):
        _name = "ai.assistant"
        _description = "AI Assistant"

        name = fields.Char()
    """
_AI_VIEWS = """\
    <?xml version="1.0"?>
    <odoo>
        <record id="ai_assistant_view_form" model="ir.ui.view">
            <field name="name">ai.assistant.form</field>
            <field name="model">ai.assistant</field>
            <field name="arch" type="xml">
                <form><sheet><field name="name"/></sheet></form>
            </field>
        </record>
        <record id="ai_assistant_view_form_alt" model="ir.ui.view">
            <field name="name">ai.assistant.form.alt</field>
            <field name="model">ai.assistant</field>
            <field name="arch" type="xml">
                <form><group><field name="name"/></group></form>
            </field>
        </record>
    </odoo>
    """
_PULSE_MODELS = """\
    from odoo import fields, models


    class AiAssistant(models.Model):
        _inherit = "ai.assistant"

        pulse_score = fields.Integer()
    """
_RAG_REL_MODELS = """\
    from odoo import api, fields, models


    class AiRagSource(models.Model):
        _name = "ai.rag.source"
        {inherit}
        _description = "RAG Source"

        name = fields.Char()
        assistant_id = fields.Many2one("ai.assistant")
        label = fields.Char(compute="_compute_label")

        @api.depends({depends})
        def _compute_label(self):
            for rec in self:
                rec.label = rec.name
    """
_RAG_REL_VIEWS = """\
    <?xml version="1.0"?>
    <odoo>
        <record id="ai_assistant_view_form_rag" model="ir.ui.view">
            <field name="name">ai.assistant.form.rag</field>
            <field name="model">ai.assistant</field>
            <field name="inherit_id" ref="viin_ai.{parent}"/>
            <field name="arch" type="xml">
                <xpath expr="//field[@name='name']" position="after">
                    <field name="name"/>
                </xpath>
            </field>
        </record>
    </odoo>
    """
_JS = '/** @odoo-module */\nexport const panel = 1;\n'


def _rag_rel_source(*, inherit: bool, depends: str) -> str:
    return _RAG_REL_MODELS.format(
        inherit='_inherit = ["ai.mixin"]' if inherit else "", depends=depends,
    )


def test_relations_a_live_module_no_longer_declares_leave_the_graph(
    pg, neo4j_driver, tmp_path,
):
    """Removed ``_inherit`` entry -> its INHERITS edge goes; removed manifest
    ``depends`` entry -> its DEPENDS_ON edge goes and impact_analysis stops listing
    the module as a dependent; a narrowed ``@api.depends`` -> the DEPENDS_ON_FIELD
    edge goes; a view re-parented -> the old INHERITS_VIEW goes and the new one
    exists; a dropped ``assets`` entry -> CONTRIBUTES_TO goes. A re-parsed module
    whose source did not change (viin_ai) keeps every relationship, the same-name
    INHERITS edge the post-pass derives survives, and the shared AssetBundle keeps
    its other contributor."""
    repo = GitRepo(tmp_path, "viindoo_addons")
    _rel_module(repo, AI, depends=["base"], models_py=_AI_MODELS, views_xml=_AI_VIEWS,
                assets={"web.assets_backend": [f"{AI}/static/src/**/*"]}, js=_JS)
    _rel_module(repo, "viin_ai_pulse", depends=[AI], models_py=_PULSE_MODELS)
    _rel_module(
        repo, RAG, depends=[AI, "viin_ai_pulse"],
        models_py=_rag_rel_source(inherit=True, depends='"name", "assistant_id"'),
        views_xml=_RAG_REL_VIEWS.format(parent="ai_assistant_view_form"),
        assets={"web.assets_backend": [f"{RAG}/static/src/**/*"]}, js=_JS,
    )
    repo.commit("add the ai modules")
    register("viindoo_99", repo)
    run(pg, "viindoo_99")

    # Positive control: every relationship the change removes exists now.
    assert _has_rel(neo4j_driver, RAG, "INHERITS", start_name="ai.rag.source",
                    end_name="ai.mixin") == 1
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON", end_name="viin_ai_pulse") == 1
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON_FIELD", start_name="_compute_label",
                    end_name="assistant_id") == 1
    assert _has_rel(neo4j_driver, RAG, "INHERITS_VIEW",
                    end_xmlid=f"{AI}.ai_assistant_view_form") == 1
    assert _has_rel(neo4j_driver, RAG, "CONTRIBUTES_TO", end_name="web.assets_backend") == 1
    assert _has_rel(neo4j_driver, "viin_ai_pulse", "DEPENDS_ON", end_name=AI) == 1
    pulse_same_name = _has_rel(neo4j_driver, "viin_ai_pulse", "INHERITS",
                               start_name="ai.assistant", end_name="ai.assistant")
    assert pulse_same_name >= 1
    _, _, impact = _tools()
    before = impact("model", "ai.mixin", V)
    assert "viin_ai_pulse" in before and RAG in before, before
    ai_rels = _rels(neo4j_driver, AI)

    # viin_ai: a comment only (re-parsed, source unchanged); pulse drops viin_ai
    # from depends; rag drops _inherit, a depends entry, a depends field, its
    # view's parent and its assets.
    ai_models = repo.path / AI / "models" / "models.py"
    ai_models.write_text(ai_models.read_text() + "# reviewed\n")
    _rel_module(repo, "viin_ai_pulse", depends=["base"], models_py=_PULSE_MODELS)
    shutil.rmtree(repo.path / RAG)
    _rel_module(
        repo, RAG, depends=[AI],
        models_py=_rag_rel_source(inherit=False, depends='"name"'),
        views_xml=_RAG_REL_VIEWS.format(parent="ai_assistant_view_form_alt"),
    )
    repo.commit("[IMP] ai modules: narrow the dependencies")
    summary = run(pg, "viindoo_99")

    assert _has_rel(neo4j_driver, RAG, "INHERITS", start_name="ai.rag.source",
                    end_name="ai.mixin") == 0
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON", end_name="viin_ai_pulse") == 0
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON", end_name=AI) == 1
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON_FIELD", start_name="_compute_label",
                    end_name="assistant_id") == 0
    assert _has_rel(neo4j_driver, RAG, "DEPENDS_ON_FIELD", start_name="_compute_label",
                    end_name="name") == 1
    assert _has_rel(neo4j_driver, RAG, "INHERITS_VIEW",
                    end_xmlid=f"{AI}.ai_assistant_view_form") == 0
    assert _has_rel(neo4j_driver, RAG, "INHERITS_VIEW",
                    end_xmlid=f"{AI}.ai_assistant_view_form_alt") == 1
    assert _has_rel(neo4j_driver, RAG, "CONTRIBUTES_TO") == 0
    assert _has_rel(neo4j_driver, "viin_ai_pulse", "DEPENDS_ON", end_name=AI) == 0
    # Kept: what the sources still declare, and what other modules own.
    assert _rels(neo4j_driver, AI) == ai_rels
    assert _has_rel(neo4j_driver, "viin_ai_pulse", "INHERITS", start_name="ai.assistant",
                    end_name="ai.assistant") == pulse_same_name
    assert _has_rel(neo4j_driver, RAG, "DEFINED_IN", start_name="ai.rag.source") == 1
    assert _has_rel(neo4j_driver, RAG, "BELONGS_TO", start_name="assistant_id") == 1
    assert _count(neo4j_driver, "AssetBundle", name="web.assets_backend") == 1
    assert not needs_attention(summary), lc(summary)

    _, _, impact = _tools()
    after = impact("model", "ai.mixin", V)
    assert "viin_ai_pulse" not in after, (
        "viin_ai_pulse no longer depends on viin_ai; impact_analysis must stop "
        f"listing it:\n{after}"
    )
    assert RAG in after, after


# ---------------------------------------------------------------------------
# First run after deploy, and constraint (a)
# ---------------------------------------------------------------------------

def test_first_run_after_deploy_loses_nothing_the_source_still_defines(
    pg, neo4j_driver, tmp_path,
):
    """Data written before B14 carries no run token. The first run that re-parses
    viin_ai_rag adds what is new and deletes nothing its source still defines -
    nodes, relationships (including the post-pass test edges) and embeddings - and
    the module it did not re-parse keeps its token-less data. A later removal from
    the same module is pruned normally."""
    repo = _rag_repo(tmp_path)
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    _strip_run_tokens(neo4j_driver)
    rag_nodes, rag_rels, rag_emb = (
        _nodes(neo4j_driver, RAG), _rels(neo4j_driver, RAG, post_pass=True), _emb(pg, RAG),
    )
    ai_nodes, ai_rels = _nodes(neo4j_driver, AI), _rels(neo4j_driver, AI)

    _rag_models(repo, rf._RAG_MODELS.replace(
        "    name = fields.Char()\n", "    name = fields.Char()\n    rag_note = fields.Text()\n", 1,
    ))
    repo.commit(f"[IMP] {RAG}: add rag_note")
    summary = run(pg, "viindoo_99")

    after_nodes = _nodes(neo4j_driver, RAG)
    assert not (rag_nodes - after_nodes), f"lost nodes: {rag_nodes - after_nodes}"
    added = after_nodes - rag_nodes
    assert [k[0][0] + ":" + (k[3] or "") for k in added.elements()] == ["Field:rag_note"]
    after_rels = _rels(neo4j_driver, RAG, post_pass=True)
    assert not (rag_rels - after_rels), f"lost relationships: {rag_rels - after_rels}"
    assert rag_emb <= _emb(pg, RAG)
    assert _nodes(neo4j_driver, AI) == ai_nodes
    assert _rels(neo4j_driver, AI) == ai_rels
    assert not needs_attention(summary), lc(summary)

    _rag_models(repo, rf._RAG_MODELS)
    repo.commit(f"[REV] {RAG}: drop rag_note")
    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="rag_note") == 0
    assert ("field", "ai.rag.source.rag_note") not in _emb(pg, RAG)
    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 1


def test_a_module_not_reparsed_this_run_is_never_pruned(pg, neo4j_driver, tmp_path):
    """(a): a run that re-parses only viin_ai_rag prunes only viin_ai_rag. viin_ai
    carries the previous run's token and even a ghost left by pre-B14 code, yet it
    keeps every node, relationship and embedding - until viin_ai itself is
    re-parsed, which then removes the ghost."""
    repo = _rag_repo(tmp_path)
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (m:Model {name: 'ai.assistant', module: $m, odoo_version: $v}) "
            "CREATE (f:Field {name: 'ghost_score', model: 'ai.assistant', module: $m, "
            "odoo_version: $v, profile: ['viindoo_99']})-[:BELONGS_TO]->(m)",
            m=AI, v=V,
        ).consume()
    ai_nodes, ai_rels, ai_emb = (
        _nodes(neo4j_driver, AI), _rels(neo4j_driver, AI), _emb(pg, AI),
    )

    _rag_models(repo, _RAG_MODELS_NO_RAG_COUNT)
    repo.commit(f"[REM] {RAG}: drop rag_count")
    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 0
    assert _nodes(neo4j_driver, AI) == ai_nodes
    assert _rels(neo4j_driver, AI) == ai_rels
    assert _emb(pg, AI) == ai_emb

    models = repo.path / AI / "models" / "ai_assistant.py"
    models.write_text(models.read_text() + "# reviewed\n")
    repo.commit(f"[IMP] {AI}: touch")
    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=AI, name="ghost_score") == 0
    assert _count(neo4j_driver, "Field", module=AI, name="name") == 1


# ---------------------------------------------------------------------------
# (b) degraded parses
# ---------------------------------------------------------------------------

_VIEWS_REL = f"{RAG}/views/ai_rag_source_views.xml"


def test_unreadable_file_keeps_graph_and_embeddings_and_is_retried_once_per_state(
    pg, neo4j_driver, tmp_path,
):
    """A views file that cannot be read makes the parse incomplete: nothing of
    viin_ai_rag is pruned (not even the field removed in the same commit) and no
    embedding row goes; the operator is told which repo-relative file failed and
    the module is flagged for retry. The retry happens ONCE; while the file stays
    unreadable later runs do zero work. Fixing the permission changes the failure
    fingerprint: the next run re-parses, prunes and clears the attention."""
    repo = _rag_repo(tmp_path)
    (rid,) = register("viindoo_99", repo)
    run(pg, "viindoo_99", refresh=False)
    _rag_models(repo, _RAG_MODELS_NO_RAG_COUNT)
    repo.commit(f"[REM] {RAG}: drop rag_count")
    before_nodes, before_rels, before_emb = (
        _nodes(neo4j_driver, RAG), _rels(neo4j_driver, RAG), _emb(pg, RAG),
    )
    views = repo.path / _VIEWS_REL
    views.chmod(0)
    try:
        run(pg, "viindoo_99", refresh=False)

        assert _nodes(neo4j_driver, RAG) == before_nodes
        assert _rels(neo4j_driver, RAG) == before_rels
        assert _emb(pg, RAG) == before_emb
        attention = _attention(pg, rid)
        assert _VIEWS_REL in attention, attention
        assert str(repo.path) not in attention, "file names must be repo-relative"
        assert ledger(pg, rid, RAG)["needs_rewrite"] is True

        retry = FakeEmbedder(dim=1024)
        retried = run(pg, "viindoo_99", refresh=False, embedder=retry)
        assert retried["modules"] >= 1, "the transient failure is retried once"
        assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 1
        assert _emb(pg, RAG) == before_emb

        for _ in range(2):
            idle = FakeEmbedder(dim=1024)
            quiet = run(pg, "viindoo_99", refresh=False, embedder=idle)
            assert quiet["modules"] == 0, "same failure state: no further retry"
            assert idle.call_count == 0
            assert _VIEWS_REL in _attention(pg, rid)
    finally:
        views.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    fixed = run(pg, "viindoo_99", refresh=False)

    assert fixed["modules"] >= 1
    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 0
    assert ("field", "ai.assistant.rag_count") not in _emb(pg, RAG)
    assert _count(neo4j_driver, "View", xmlid=f"{RAG}.ai_rag_source_view_list") == 1
    assert _VIEWS_REL not in _attention(pg, rid)
    assert ledger(pg, rid, RAG)["needs_rewrite"] is False
    assert run(pg, "viindoo_99", refresh=False)["modules"] == 0


def test_syntax_error_keeps_everything_and_is_not_retried_until_a_commit_fixes_it(
    pg, neo4j_driver, tmp_path,
):
    """A Python syntax error (v11+ source) is a content failure: nothing is pruned,
    the file is named, and re-parsing without a change cannot help, so later runs
    do zero work. The commit that fixes the file re-parses and prunes."""
    repo = _rag_repo(tmp_path)
    (rid,) = register("viindoo_99", repo)
    run(pg, "viindoo_99")
    _rag_models(repo, _RAG_MODELS_NO_RAG_COUNT)
    _write(repo.path / RAG / "models" / "broken.py", "def broken(:\n    pass\n")
    _write(repo.path / RAG / "models" / "__init__.py",
           "from . import ai_rag_source\nfrom . import broken\n")
    before_nodes, before_emb = _nodes(neo4j_driver, RAG), _emb(pg, RAG)
    repo.commit(f"[WIP] {RAG}: half-done refactor")

    run(pg, "viindoo_99")

    assert _nodes(neo4j_driver, RAG) == before_nodes
    assert _emb(pg, RAG) == before_emb
    assert f"{RAG}/models/broken.py" in _attention(pg, rid)
    for _ in range(2):
        idle = FakeEmbedder(dim=1024)
        quiet = run(pg, "viindoo_99", embedder=idle)
        assert quiet["modules"] == 0 and idle.call_count == 0
        assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 1

    (repo.path / RAG / "models" / "broken.py").unlink()
    _write(repo.path / RAG / "models" / "__init__.py", "from . import ai_rag_source\n")
    repo.commit(f"[FIX] {RAG}: finish the refactor")
    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 0
    assert ("field", "ai.assistant.rag_count") not in _emb(pg, RAG)
    assert "broken.py" not in _attention(pg, rid)


# ---------------------------------------------------------------------------
# (c) ownership
# ---------------------------------------------------------------------------

# SAFETY (constraint c): a new-code invariant. The pre-B14 code never pruned, so
# this test cannot fail on the revert by construction; it fails when the prune
# ignores the second owner.
def test_module_shipped_by_another_present_repo_is_not_pruned_by_one_copy(
    pg, neo4j_driver, tmp_path,
):
    """viin_ai_rag ships from two repos (two profiles, same version). Entities are
    not repo-attributable, so when one copy drops rag_count, neither rag_count
    (still in the other copy) nor the other copy's own ``only_in_a`` field may be
    deleted by that copy's re-parse, and the other profile's embeddings stay."""
    a = _rag_repo(tmp_path / "a", "addons_a")
    _rag_models(a, rf._RAG_MODELS.replace(
        "    name = fields.Char()\n",
        "    name = fields.Char()\n    only_in_a = fields.Char()\n", 1,
    ))
    a.commit("only_in_a")
    b = _rag_repo(tmp_path / "b", "addons_b")
    register("pa_99", a)
    register("pb_99", b)
    run(pg, "pa_99")
    run(pg, "pb_99")
    assert _count(neo4j_driver, "Field", module=RAG, name="only_in_a") == 1
    a_emb = _emb(pg, RAG, "pa_99")
    assert ("field", "ai.rag.source.only_in_a") in a_emb

    _rag_models(b, _RAG_MODELS_NO_RAG_COUNT)
    b.commit(f"[REM] {RAG}: drop rag_count in copy b")
    run(pg, "pb_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="only_in_a") == 1
    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 1
    assert _emb(pg, RAG, "pa_99") == a_emb


def test_unsynced_repo_that_ships_the_module_defers_the_prune_until_it_syncs(
    pg, neo4j_driver, tmp_path,
):
    """A registered, cloned but never indexed fork tracks its own copy of
    viin_ai_rag (installable False), so it may still ship the module: the owner
    cannot prune rag_count yet. That is a wait, not a fault: no retry flag (the
    owner's next runs do zero work) and no operator message. Once the fork is
    indexed - its copy is excluded, so it does not own viin_ai_rag - the
    owner's copy is re-armed and its next run, with no new commit, prunes.

    Rewritten for F48 (owner decision: per MODULE, not per repo): the old test
    used a fork that did NOT ship viin_ai_rag and expected it to defer the
    prune with attention + retry. Such a fork no longer blocks anything
    (tests/test_module_owner_decision_pipeline.py); the deferral intent needs a
    fork that tracks the module, and its resolution is the R2-2 re-arm. The
    fail-safe attention path (a checkout git cannot read) is protected in
    test_unreadable_sibling_checkout_stays_a_potential_owner_with_attention."""
    repo = _rag_repo(tmp_path / "main")
    fork = GitRepo(tmp_path / "fork", "customer_fork")
    _rel_module(fork, "fork_only", depends=["base"], models_py=_PULSE_MODELS.replace(
        '_inherit = "ai.assistant"', '_name = "fork.record"',
    ))
    rf.write_viin_ai_rag(fork.path)
    manifest = fork.path / RAG / "__manifest__.py"
    manifest.write_text(manifest.read_text().replace(
        "'installable': True", "'installable': False",
    ))
    assert "'installable': False" in manifest.read_text()
    fork.commit("fork")
    (rid,) = register("viindoo_99", repo)
    (fork_id,) = register("fork_99", fork)
    run(pg, "viindoo_99")
    _rag_models(repo, _RAG_MODELS_NO_RAG_COUNT)
    repo.commit(f"[REM] {RAG}: drop rag_count")

    held = run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 1
    assert ("field", "ai.assistant.rag_count") in _emb(pg, RAG)
    assert not needs_attention(held), lc(held)
    assert "customer_fork" not in _attention(pg, rid)
    assert ledger(pg, rid, RAG)["needs_rewrite"] is False
    assert run(pg, "viindoo_99")["modules"] == 0

    run(pg, "fork_99")
    assert ledger(pg, fork_id, RAG)["state"] == "excluded"
    assert ledger(pg, rid, RAG)["needs_rewrite"] is True
    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Field", module=RAG, name="rag_count") == 0
    assert ("field", "ai.assistant.rag_count") not in _emb(pg, RAG)
    assert "deferred" not in _attention(pg, rid)
    assert ledger(pg, rid, RAG)["needs_rewrite"] is False


def test_legacy_multi_profile_module_array_does_not_block_the_prune(
    pg, neo4j_driver, tmp_path,
):
    """ADR-0016-era Module nodes can carry several profiles. Ownership is decided
    by the ledger (one present owner), so such an array never blocks the prune."""
    repo = _rag_repo(tmp_path)
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (m:Module {name: $m, odoo_version: $v}) "
            "SET m.profile = ['legacy_parent', 'viindoo_99']",
            m=RAG, v=V,
        ).consume()
    _rag_models(repo, rf._RAG_MODELS.replace(
        "\n    def action_reindex(self):\n        return True\n", "\n",
    ))
    repo.commit(f"[REM] {RAG}: drop action_reindex")

    run(pg, "viindoo_99")

    assert _count(neo4j_driver, "Method", module=RAG, name="action_reindex") == 0
    assert ("method", "ai.rag.source.action_reindex") not in _emb(pg, RAG)


# ---------------------------------------------------------------------------
# (d) mass drop is held
# ---------------------------------------------------------------------------

def _bulk_models(n_fields: int) -> str:
    fields_src = "\n".join(f"    f{i:02d} = fields.Char()" for i in range(n_fields))
    return (
        "from odoo import fields, models\n\n\n"
        "class AiBulk(models.Model):\n"
        '    _name = "ai.bulk"\n'
        '    _description = "Bulk"\n\n'
        f"{fields_src}\n"
    )


def test_mass_drop_is_held_with_exit_3_until_allow_mass_retire(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, capsys,
):
    """25 of the module's 31 nodes disappear from one parse (> 50 %, >= 20): that
    looks like a broken parse, so the real CLI keeps the graph AND the embeddings,
    names the module to the operator and exits 3. Re-running with
    --allow-mass-retire on the same HEAD prunes both; the run after is clean."""
    import src.indexer.__main__ as cli

    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    monkeypatch.setattr(cli, "_build_embedder", lambda: FakeEmbedder(dim=1024))
    repo = GitRepo(tmp_path, "bulk_addons")
    _rel_module(repo, "viin_ai_bulk", depends=["base"], models_py=_bulk_models(30))
    repo.commit("add bulk")
    (rid,) = register("bulk_99", repo)
    assert cli.main(["index-repo", "--profile", "bulk_99"]) == 0
    capsys.readouterr()
    nodes_before, emb_before = _nodes(neo4j_driver, "viin_ai_bulk"), _emb(pg, "viin_ai_bulk")
    assert ("field", "ai.bulk.f29") in emb_before
    _rel_module(repo, "viin_ai_bulk", depends=["base"], models_py=_bulk_models(5))
    repo.commit("[REF] bulk: drop 25 fields")

    held = cli.main(["index-repo", "--profile", "bulk_99"])

    assert held == 3
    err = capsys.readouterr().err
    assert "viin_ai_bulk" in err, err
    assert _nodes(neo4j_driver, "viin_ai_bulk") == nodes_before
    assert _emb(pg, "viin_ai_bulk") == emb_before
    attention = _attention(pg, rid)
    assert "viin_ai_bulk" in attention and "--allow-mass-retire" in attention, attention

    confirmed = cli.main(["index-repo", "--profile", "bulk_99", "--allow-mass-retire"])

    assert confirmed == 0
    assert _count(neo4j_driver, "Field", module="viin_ai_bulk") == 5
    assert ("field", "ai.bulk.f29") not in _emb(pg, "viin_ai_bulk")
    assert ("field", "ai.bulk.f04") in _emb(pg, "viin_ai_bulk")
    assert "--allow-mass-retire" not in _attention(pg, rid)
    assert cli.main(["index-repo", "--profile", "bulk_99"]) == 0


def test_mass_drop_of_relationships_alone_is_held_until_allow_mass_retire(
    pg, neo4j_driver, tmp_path,
):
    """No node disappears, but 25 of the hub module's ~33 relationships (its
    manifest dependencies) do: the same mass-drop gate holds them, then
    allow_mass_retire prunes exactly the dropped DEPENDS_ON edges."""
    repo = GitRepo(tmp_path, "hub_addons")
    deps = [f"viin_dep_{i:02d}" for i in range(30)]
    for d in deps:
        mod = repo.path / d
        _write(mod / "__manifest__.py", repr({
            "name": d, "version": f"{V}.1.0", "depends": ["base"],
            "installable": True, "license": "LGPL-3",
        }) + "\n")
        _write(mod / "__init__.py", "")
    _rel_module(repo, "viin_hub", depends=deps, models_py=_bulk_models(1).replace(
        "ai.bulk", "ai.hub"))
    repo.commit("add hub")
    register("hub_99", repo)
    run(pg, "hub_99")
    assert _has_rel(neo4j_driver, "viin_hub", "DEPENDS_ON") == 30

    _write(repo.path / "viin_hub" / "__manifest__.py", repr({
        "name": "viin_hub", "version": f"{V}.1.0", "depends": deps[:5],
        "installable": True, "license": "LGPL-3",
    }) + "\n")
    repo.commit("[REF] hub: drop 25 dependencies")
    held = run(pg, "hub_99")

    assert needs_attention(held)
    assert any("viin_hub" in g for g in lc(held)["gates_tripped"]), lc(held)
    assert _has_rel(neo4j_driver, "viin_hub", "DEPENDS_ON") == 30

    confirmed = run(pg, "hub_99", allow_mass_retire=True)

    assert not needs_attention(confirmed), lc(confirmed)
    assert _has_rel(neo4j_driver, "viin_hub", "DEPENDS_ON") == 5


# ---------------------------------------------------------------------------
# T24: every writer of a module child stamps the run token
# ---------------------------------------------------------------------------

EXTRA = "viin_ai_extra"
_EXTRA_MODELS = """\
from odoo import fields, models


class AiDelegate(models.Model):
    _name = "ai.delegate"
    _description = "AI Delegate"
    _inherits = {"ai.assistant": "assistant_id"}

    assistant_id = fields.Many2one("ai.assistant", required=True, ondelete="cascade")
    note = fields.Char()

    def action_note(self):
        for rec in self:
            rec.note = rec.note or "x"
        return True
"""
_EXTRA_DATA = """\
<?xml version="1.0"?>
<odoo>
    <template id="report_rag_source_document_extra"
              inherit_id="viin_ai_rag.report_rag_source_document">
        <xpath expr="//div[hasclass('page')]" position="inside"><span>extra</span></xpath>
    </template>
    <template id="assets_backend_extra" inherit_id="web.assets_backend">
        <xpath expr="." position="inside">
            <script type="text/javascript" src="/viin_ai_extra/static/src/js/extra.js"/>
        </xpath>
    </template>
</odoo>
"""
_EXTRA_JS = """\
/** @odoo-module */
import { patch } from "@web/core/utils/patch";
import { RagSourcePanel } from "@viin_ai_rag/components/rag_source_panel";

export class ExtraPanel extends RagSourcePanel {
    static template = "viin_ai_extra.ExtraPanel";
}

patch(RagSourcePanel.prototype, {
    setup() {
        super.setup();
    },
});
"""


def _write_extra(repo: GitRepo) -> None:
    """One more module exercising the relationship writers the rag fixture does
    not: _inherits (DELEGATES_TO), a method reading a field (USES_FIELD), template
    inheritance (EXTENDS_TMPL / EXTENDS_ASSET_BUNDLE), OWL extends + patch
    (EXTENDS / PATCHES) and an SCSS import (IMPORTS)."""
    mod = repo.path / EXTRA
    _write(mod / "__manifest__.py", repr({
        "name": EXTRA, "version": f"{V}.1.0", "depends": [RAG], "installable": True,
        "license": "LGPL-3", "data": ["views/templates.xml"],
        "assets": {"web.assets_backend": [f"{EXTRA}/static/src/**/*"]},
    }) + "\n")
    _write(mod / "__init__.py", "from . import models\n")
    _write(mod / "models" / "__init__.py", "from . import delegate\n")
    _write(mod / "models" / "delegate.py", _EXTRA_MODELS)
    _write(mod / "views" / "templates.xml", _EXTRA_DATA)
    _write(mod / "static" / "src" / "js" / "extra.js", _EXTRA_JS)
    _write(mod / "static" / "src" / "scss" / "_vars.scss", "$extra-gap: 4px;\n")
    _write(mod / "static" / "src" / "scss" / "extra.scss",
           '@import "vars";\n.o_extra { padding: $extra-gap; }\n')

def test_every_child_writer_and_relationship_writer_stamps_the_run_token(
    pg, neo4j_driver, tmp_path,
):
    """T24 extended (B14): after one run, every node and relationship the per-module
    writers produced for a module carries that run's single token, so a missing
    token really means "not produced by this parse". Derived by exercising the
    real writers over one artifact of every kind, never by copying the constants.
    Every relationship type observed from a module's own nodes is listed in
    MODULE_CHILD_REL_TYPES (else the prune could never remove it), except the
    documented post-pass / projection gaps."""
    from src.indexer import writer_neo4j

    child_labels = writer_neo4j.MODULE_CHILD_LABELS
    # getattr: a tree without the SSOT must fail on the behaviour below, not on import.
    rel_types_ssot = getattr(writer_neo4j, "MODULE_CHILD_REL_TYPES", {})

    repo = _rag_repo(tmp_path)
    _write_extra(repo)
    repo.commit("add viin_ai_extra")
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    modules = [AI, RAG, EXTRA]

    with neo4j_driver.session() as s:
        nodes = s.run(
            """
            MATCH (n) WHERE n.odoo_version = $v AND NOT n:Module
              AND (n.module IN $ms
                   OR (n:LintViolation
                       AND any(m IN $ms WHERE n.file_path STARTS WITH m + '/')))
            RETURN labels(n)[0] AS label, n.written_run AS token,
                   coalesce(n.xmlid, n.name, n.file_path) AS key
            """,
            v=V, ms=modules,
        ).data()
        rels = s.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a.odoo_version = $v
              AND ((a:Module AND a.name IN $ms) OR (NOT a:Module AND a.module IN $ms))
            RETURN CASE WHEN a:Module THEN 'Module' ELSE labels(a)[0] END AS label,
                   type(r) AS type, r.written_run AS token
            """,
            v=V, ms=modules,
        ).data()

    child_nodes = [n for n in nodes if n["label"] in child_labels]
    observed_labels = {n["label"] for n in child_nodes}
    # Documented exception: the addon TestHelper is a post-pass projection.
    stamped = [n for n in child_nodes if n["label"] != "TestHelper"]
    tokens = {n["token"] for n in stamped}
    assert len(tokens) == 1 and None not in tokens, (
        "every child node needs the one token of this run; unstamped: "
        f"{sorted({(n['label'], n['key']) for n in stamped if not n['token']})}"
    )
    (token,) = tokens
    assert observed_labels >= set(child_labels), (
        "fixture must exercise every child label; missing "
        f"{set(child_labels) - observed_labels}"
    )

    prunable = [
        r for r in rels
        if r["type"] not in _POST_PASS_REL_TYPES and r["label"] != "TestHelper"
    ]
    unstamped = sorted({(r["label"], r["type"]) for r in prunable if r["token"] != token})
    assert not unstamped, f"relationships not stamped with this run's token: {unstamped}"
    unlisted = sorted({
        (r["label"], r["type"]) for r in prunable
        if r["type"] not in rel_types_ssot.get(r["label"], ())
    })
    assert not unlisted, (
        f"relationship types written from module nodes but never pruned: {unlisted}"
    )
    observed_types = {(r["label"], r["type"]) for r in prunable}
    # Non-vacuity: the fixture really exercised these writers. USES_CORE_SYMBOL
    # and USES_FIELD need a core index / a same-module field read, and OWLComp
    # EXTENDS / BOUND_TO are post-pass edges: all four are covered by the F47
    # tests at the end of this file.
    assert {
        ("Module", "DEPENDS_ON"), ("Module", "CONTRIBUTES_TO"), ("Model", "INHERITS"),
        ("Model", "DELEGATES_TO"), ("Method", "DEPENDS_ON_FIELD"),
        ("View", "INHERITS_VIEW"), ("View", "HAS_VIOLATION"), ("View", "TARGETS_MODEL"),
        ("QWebTmpl", "EXTENDS_TMPL"), ("QWebTmpl", "EXTENDS_ASSET_BUNDLE"),
        ("Report", "REPORTS_ON"), ("Report", "USES_TEMPLATE"), ("JSPatch", "PATCHES"),
        ("Stylesheet", "IMPORTS"), ("TestMethod", "BELONGS_TO_TEST"),
    } <= observed_types, observed_types


def test_delegation_patch_and_import_relations_a_module_stops_declaring_leave_the_graph(
    pg, neo4j_driver, tmp_path,
):
    """The same rule for the other relationship families: dropping ``_inherits``
    removes DELEGATES_TO, dropping the JS patch removes its PATCHES edge, and
    dropping an SCSS ``@import`` removes IMPORTS - while the model and both
    stylesheets themselves stay."""
    repo = _rag_repo(tmp_path)
    _write_extra(repo)
    repo.commit("add viin_ai_extra")
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    assert _has_rel(neo4j_driver, EXTRA, "DELEGATES_TO", start_name="ai.delegate") == 1
    assert _has_rel(neo4j_driver, EXTRA, "PATCHES", end_name="RagSourcePanel") == 1
    assert _has_rel(neo4j_driver, EXTRA, "IMPORTS") == 1

    mod = repo.path / EXTRA
    _write(mod / "models" / "delegate.py", _EXTRA_MODELS.replace(
        '    _inherits = {"ai.assistant": "assistant_id"}\n', "",
    ))
    _write(mod / "static" / "src" / "js" / "extra.js", _EXTRA_JS.split("\npatch(")[0])
    _write(mod / "static" / "src" / "scss" / "extra.scss", ".o_extra { padding: 4px; }\n")
    repo.commit(f"[IMP] {EXTRA}: plain model, no patch, no import")
    run(pg, "viindoo_99")

    assert _has_rel(neo4j_driver, EXTRA, "DELEGATES_TO") == 0
    assert _has_rel(neo4j_driver, EXTRA, "PATCHES") == 0
    assert _has_rel(neo4j_driver, EXTRA, "IMPORTS") == 0
    assert _count(neo4j_driver, "Model", module=EXTRA, name="ai.delegate") == 1
    assert _count(neo4j_driver, "Stylesheet", module=EXTRA) == 2


# ---------------------------------------------------------------------------
# F47: the method-level and OWL relationship writers the fixtures above do not
# reach - USES_CORE_SYMBOL, USES_FIELD (per-module, run-token stamped, B14
# prune) and OWLComp EXTENDS / BOUND_TO (version-wide post-pass
# reconcile_owl_edges) - exist when the source declares them and leave the
# graph when it stops.
# ---------------------------------------------------------------------------

_X_BASE_MODELS = """\
from odoo import fields, models


class XThing(models.Model):
    _name = "x.thing"
    _description = "X Thing"

    name = fields.Char()
    note = fields.Char()

    def action_label(self):
        for rec in self:
            rec.note = self.name
        return self.name_get()
"""
_X_BASE_MODELS_NO_USES = _X_BASE_MODELS.replace(
    "        for rec in self:\n            rec.note = self.name\n"
    "        return self.name_get()\n",
    "        return True\n",
)
assert "name_get" not in _X_BASE_MODELS_NO_USES and "self.name" not in _X_BASE_MODELS_NO_USES
_X_EXT_MODELS = """\
from odoo import fields, models


class XThing(models.Model):
    _inherit = "x.thing"

    extra = fields.Char()
"""
# web/static/src/legacy/legacy_component.js shape (Odoo 16.0: a Component
# subclass other components extend).
_LEGACY_COMPONENT_JS = """\
/** @odoo-module */
import { Component } from "@odoo/owl";

export class LegacyComponent extends Component {}
"""
_THING_PANEL_JS = """\
/** @odoo-module */
import { LegacyComponent } from "@web/legacy/legacy_component";

export class ThingPanel extends LegacyComponent {
    setup() {
        this.orm.searchRead("x.thing", [], ["name"]);
    }
}
"""
_THING_PANEL_PLAIN_JS = """\
/** @odoo-module */
import { Component } from "@odoo/owl";

export class ThingPanel extends Component {}
"""
# The deprecated core symbol the x.thing method calls, as index_core writes it
# (odoo/models.py BaseModel.name_get, deprecated since 17.0).
_NAME_GET = "odoo.models.BaseModel.name_get"


def _manifest(mod: Path, name: str, depends: list[str]) -> None:
    _write(mod / "__manifest__.py", repr({
        "name": name, "version": f"{V}.1.0", "depends": depends,
        "installable": True, "license": "LGPL-3",
    }) + "\n")


def _x_base(repo: GitRepo, models_py: str = _X_BASE_MODELS) -> None:
    mod = repo.path / "x_base"
    _manifest(mod, "x_base", ["base"])
    _write(mod / "__init__.py", "from . import models\n")
    _write(mod / "models" / "__init__.py", "from . import thing\n")
    _write(mod / "models" / "thing.py", models_py)


def _write_core_symbol(status: str = "deprecated") -> None:
    import os

    from src.indexer.models import CoreSymbolInfo
    from src.indexer.writer_neo4j import Neo4jWriter
    w = Neo4jWriter(
        uri=os.environ["NEO4J_URI"], user=os.environ["NEO4J_USER"],
        password=os.environ["NEO4J_PASSWORD"],
    )
    try:
        w.write_core_symbols([CoreSymbolInfo(
            qualified_name=_NAME_GET, kind="orm_method", odoo_version=V,
            file_path="odoo/models.py", status=status,
        )])
    finally:
        w.close()


def _method_rels(driver, rel_type: str) -> list[dict]:
    with driver.session() as s:
        return s.run(
            f"""
            MATCH (m:Method {{name: 'action_label', module: 'x_base', odoo_version: $v}})
                  -[r:{rel_type}]->(x)
            RETURN coalesce(x.qualified_name, x.name) AS target,
                   r.written_run AS rel_token, m.written_run AS node_token
            """,
            v=V,
        ).data()


def _deprecated_usage(monkeypatch, driver) -> str:
    from src.mcp.tools import spec
    monkeypatch.setattr(spec._srv, "_driver", driver)
    return spec._find_deprecated_usage(odoo_version=V)


def test_method_uses_of_a_core_symbol_and_a_field_are_stamped_and_leave_with_the_source(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """F47 (USES_CORE_SYMBOL, USES_FIELD): a method calling the deprecated
    ``name_get`` and reading its own model's ``name`` gets both edges, stamped
    with the run token its Method node carries, and find_deprecated_usage
    reports the call. After the method stops doing both, ONE incremental run
    removes both edges (B14 run-token prune) while the Method, the Field and the
    CoreSymbol stay, and find_deprecated_usage no longer reports it."""
    # GUARD: pre-existing behaviour (B14 prune of these types; a0df7ed keeps the
    # deprecated-call result unchanged)
    _write_core_symbol()
    repo = GitRepo(tmp_path, "x_addons")
    _x_base(repo)
    repo.commit("add x_base")
    register("x_99", repo)
    run(pg, "x_99")

    core = _method_rels(neo4j_driver, "USES_CORE_SYMBOL")
    field = _method_rels(neo4j_driver, "USES_FIELD")
    assert [r["target"] for r in core] == [_NAME_GET], core
    assert [r["target"] for r in field] == ["name"], field
    for r in core + field:
        assert r["rel_token"] and r["rel_token"] == r["node_token"], r
    report = _deprecated_usage(monkeypatch, neo4j_driver)
    assert "action_label" in report and "name_get" in report, report

    _x_base(repo, _X_BASE_MODELS_NO_USES)
    repo.commit("[IMP] x_base: action_label no longer calls name_get nor reads name")
    summary = run(pg, "x_99")

    assert _method_rels(neo4j_driver, "USES_CORE_SYMBOL") == []
    assert _method_rels(neo4j_driver, "USES_FIELD") == []
    assert _count(neo4j_driver, "Method", module="x_base", name="action_label") == 1
    assert _count(neo4j_driver, "Field", module="x_base", name="name") == 1
    assert _count(neo4j_driver, "CoreSymbol", qualified_name=_NAME_GET) == 1
    assert not needs_attention(summary), lc(summary)
    report = _deprecated_usage(monkeypatch, neo4j_driver)
    assert "action_label" not in report, report


def _web_repo(parent: Path) -> GitRepo:
    """web (LegacyComponent) + zz_legacy, an unrelated module shipping its own
    same-named LegacyComponent (decoy: nothing depends on or imports it)."""
    repo = GitRepo(parent, "web_addons")
    _manifest(repo.path / "web", "web", [])
    _write(repo.path / "web" / "__init__.py", "")
    _write(repo.path / "web" / "static" / "src" / "legacy" / "legacy_component.js",
           _LEGACY_COMPONENT_JS)
    _manifest(repo.path / "zz_legacy", "zz_legacy", ["base"])
    _write(repo.path / "zz_legacy" / "__init__.py", "")
    _write(repo.path / "zz_legacy" / "static" / "src" / "legacy_component.js",
           _LEGACY_COMPONENT_JS)
    repo.commit("add web + zz_legacy")
    return repo


def _app_repo(parent: Path) -> GitRepo:
    """x_base defines x.thing, x_ext extends it (a second x.thing Model node),
    x_ui's ThingPanel extends web's LegacyComponent and reads x.thing."""
    repo = GitRepo(parent, "app_addons")
    _x_base(repo)
    mod = repo.path / "x_ext"
    _manifest(mod, "x_ext", ["x_base"])
    _write(mod / "__init__.py", "from . import models\n")
    _write(mod / "models" / "__init__.py", "from . import thing\n")
    _write(mod / "models" / "thing.py", _X_EXT_MODELS)
    _manifest(repo.path / "x_ui", "x_ui", ["web", "x_base"])
    _write(repo.path / "x_ui" / "__init__.py", "")
    _write(repo.path / "x_ui" / "static" / "src" / "thing_panel.js", _THING_PANEL_JS)
    repo.commit("add x_base, x_ext, x_ui")
    return repo


def _owl_edges(driver) -> tuple[set, set]:
    with driver.session() as s:
        extends = {(r["cm"], r["cn"], r["pm"], r["pn"]) for r in s.run(
            "MATCH (c:OWLComp {odoo_version: $v})-[:EXTENDS]->(p) "
            "RETURN c.module AS cm, c.name AS cn, p.module AS pm, p.name AS pn", v=V)}
        bound = {(r["cm"], r["cn"], r["mm"], r["mn"]) for r in s.run(
            "MATCH (c:OWLComp {odoo_version: $v})-[:BOUND_TO]->(m) "
            "RETURN c.module AS cm, c.name AS cn, m.module AS mm, m.name AS mn", v=V)}
    return extends, bound


_EXPECTED_EXTENDS = {("x_ui", "ThingPanel", "web", "LegacyComponent")}
_EXPECTED_BOUND = {("x_ui", "ThingPanel", "x_base", "x.thing")}


@pytest.mark.parametrize("app_first", [True, False], ids=["child-repo-first", "parent-repo-first"])
def test_owl_component_extends_its_imported_parent_and_binds_the_defining_model(
    pg, neo4j_driver, tmp_path, app_first,
):
    """F47a/F47b (FIX): ThingPanel extends exactly the LegacyComponent its file
    imports (``@web/...`` -> web), never zz_legacy's same-named class, whether
    the child's repo is written before or after the parent's; it is bound to
    exactly ONE x.thing Model node - the definition in x_base, not x_ext's
    extension node. A second run changes nothing."""
    web, app = _web_repo(tmp_path), _app_repo(tmp_path)
    register("owl_99", *((app, web) if app_first else (web, app)))
    run(pg, "owl_99")

    assert _owl_edges(neo4j_driver) == (_EXPECTED_EXTENDS, _EXPECTED_BOUND)
    assert _count(neo4j_driver, "Model", name="x.thing") == 2, (
        "positive control: two x.thing nodes exist, one per module"
    )

    run(pg, "owl_99")

    assert _owl_edges(neo4j_driver) == (_EXPECTED_EXTENDS, _EXPECTED_BOUND)


def _replace_thing_panel_with_a_plain_component(pg, driver, tmp_path) -> dict:
    """Run 1 with ThingPanel(LegacyComponent) reading x.thing, then ThingPanel
    re-declared on OWL's own Component with no model access, run 2."""
    web, app = _web_repo(tmp_path), _app_repo(tmp_path)
    register("owl_99", web, app)
    run(pg, "owl_99")
    assert _owl_edges(driver) == (_EXPECTED_EXTENDS, _EXPECTED_BOUND)

    _write(app.path / "x_ui" / "static" / "src" / "thing_panel.js", _THING_PANEL_PLAIN_JS)
    app.commit("[IMP] x_ui: ThingPanel is a plain Component")
    summary = run(pg, "owl_99")
    assert _count(driver, "OWLComp", module="x_ui", name="ThingPanel") == 1
    assert not needs_attention(summary), lc(summary)
    return summary


def test_owl_bound_to_leaves_the_graph_when_the_component_stops_reading_the_model(
    pg, neo4j_driver, tmp_path,
):
    """F47 (BOUND_TO): ThingPanel no longer reading x.thing loses its binding in
    ONE run, while the component itself stays."""
    _replace_thing_panel_with_a_plain_component(pg, neo4j_driver, tmp_path)

    assert _owl_edges(neo4j_driver)[1] == set()


def test_owl_extends_leaves_the_graph_when_the_component_changes_its_parent(
    pg, neo4j_driver, tmp_path,
):
    """F47 / T1 (FIX 3ec5fd5; was xfail(strict) finding T1): ThingPanel
    re-declared on OWL's Component (import origin '@odoo/owl' unknown, no
    OWLComp node by that name) loses its EXTENDS edge to LegacyComponent in ONE
    run - a parent the source no longer names is never kept."""
    _replace_thing_panel_with_a_plain_component(pg, neo4j_driver, tmp_path)

    assert _owl_edges(neo4j_driver)[0] == set()


def test_owl_extends_of_a_pre_change_graph_keeps_the_still_declared_parent(
    pg, neo4j_driver, tmp_path,
):
    """T1 keep rule: a graph written before import origins were recorded (no
    ``extends_module`` on the component) whose declared parent has no
    resolvable candidate keeps its recorded edge to THAT parent - it is not
    guessed away. Recreated by removing the parent's module link (the parent
    node lives in a module outside the child's closure, as a same-named
    component written by an older resolver would) and the origin."""
    # GUARD: pre-existing behaviour
    web, app = _web_repo(tmp_path), _app_repo(tmp_path)
    register("owl_99", web, app)
    run(pg, "owl_99")
    assert _owl_edges(neo4j_driver)[0] == _EXPECTED_EXTENDS
    with neo4j_driver.session() as s:
        # Pre-change graph: no recorded origin, and x_ui's manifest link to web
        # absent - the edge cannot be re-derived, only kept or dropped.
        s.run("MATCH (c:OWLComp {name: 'ThingPanel', odoo_version: $v}) "
              "REMOVE c.extends_module", v=V).consume()
        s.run("MATCH (:Module {name: 'x_ui', odoo_version: $v})-[r:DEPENDS_ON]->"
              "(:Module {name: 'web', odoo_version: $v}) DELETE r", v=V).consume()

    run(pg, "owl_99")  # no commit: the repos are skipped, the post-pass runs

    assert _owl_edges(neo4j_driver)[0] == _EXPECTED_EXTENDS
