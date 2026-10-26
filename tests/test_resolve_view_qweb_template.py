# SPDX-License-Identifier: AGPL-3.0-or-later
"""entity_lookup(kind='view') resolves QWeb templates, record form included.

Every QWeb template is indexed as a :QWebTmpl node, never a :View - parser_qweb
owns both the ``<template>`` element and the ``<record model="ir.ui.view">``
type=qweb form, and parser_xml skips the latter. A record-form template is
keyed by its website ``key`` and keeps its record id as ``record_xmlid``. Before
this fix ``_resolve_view`` only matched :View, so
``entity_lookup(kind='view', xmlid='website_forum.default_faq')`` - and every
other QWeb template, ``website.layout`` included - answered "not found".

Fast lane (no Neo4j): the graph is built from the REAL parser output of a small
XML fixture, and the bounded read helpers are replaced by a stub that answers
each query the resolver sends from that graph. The Cypher itself (tenant scope,
unresolved placeholders) is pinned by the neo4j-marked
``tests/test_resolve_view_qweb_template_neo4j.py``.

Business rules under test:
  Q1  A record-form QWeb view resolves by its record xmlid, and the answer names
      the template key that t-call uses.
  Q2  The same template resolves by its website key, and the answer names the
      record xmlid that inherit_id ref= uses.
  Q3  A <template> QWeb template resolves by its xmlid, with its base, its
      extenders and the reports that render it.
  Q4  An ordinary view still resolves as a view; no QWeb query is sent.
  Q5  An xmlid that is neither a view nor a template keeps the exact
      not-found answer.
"""

from __future__ import annotations

import importlib
import textwrap

import pytest

from src.indexer import parser_qweb, parser_xml
from src.indexer.models import ModuleInfo

V = "99.0"

_FORUM_XML = """
<?xml version="1.0"?>
<odoo>
    <record id="default_faq" model="ir.ui.view">
        <field name="name">Faq Accordion</field>
        <field name="type">qweb</field>
        <field name="key">website_forum.faq_accordion</field>
        <field name="arch" type="xml"><t t-name="website_forum.faq_accordion"><div/></t></field>
    </record>
    <template id="faq_extra" inherit_id="website_forum.default_faq">
        <xpath expr="//div" position="inside"><p/></xpath>
    </template>
    <template id="layout"><div class="forum"/></template>
    <template id="forum_layout_ext" inherit_id="website_forum.layout">
        <xpath expr="//div" position="inside"><span/></xpath>
    </template>
    <record id="forum_view_form" model="ir.ui.view">
        <field name="name">forum.forum.form</field>
        <field name="model">forum.forum</field>
        <field name="arch" type="xml"><form><field name="name"/></form></field>
    </record>
</odoo>
"""


class _Sess:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Driver:
    def session(self, *a, **k):
        return _Sess()


@pytest.fixture
def graph(tmp_path):
    """In-memory graph built from the real parser output of the fixture."""
    module = ModuleInfo(
        name="website_forum", odoo_version=V, repo="odoo_99.0",
        path=str(tmp_path), depends=["website"], version_raw="99.0.1.0",
    )
    f = tmp_path / "forum_forum_template_faq.xml"
    f.write_text(textwrap.dedent(_FORUM_XML).lstrip())
    qwebs = parser_qweb.parse_file(str(f), module)
    views = parser_xml.parse_file(str(f), module)
    tmpls = {q.xmlid: q for q in qwebs}

    def _base_of(q):
        """The writer's EXTENDS_TMPL resolution: xmlid first, then record_xmlid."""
        if not q.inherit_xmlid:
            return None
        if q.inherit_xmlid in tmpls:
            return tmpls[q.inherit_xmlid]
        return next((b for b in qwebs if b.record_xmlid == q.inherit_xmlid), None)

    return {
        "views": {v.xmlid: v for v in views},
        "qwebs": qwebs,
        "base_of": _base_of,
        "reports": {"website_forum.report_faq": "website_forum.layout"},
    }


@pytest.fixture
def lookup(graph, monkeypatch):
    """Serve the resolver's queries from *graph*; record which kinds were asked."""
    # Resolve the server module at fixture time: other tests reload it, and the
    # QWeb fallback reads its helpers from the module currently in sys.modules.
    srv = importlib.import_module("src.mcp.server")
    asked: list[str] = []
    qwebs = graph["qwebs"]

    def _tmpl_rec(q):
        return {"t": {"xmlid": q.xmlid, "key": q.key, "mode": q.mode,
                      "record_xmlid": q.record_xmlid, "module": q.module},
                "module_name": q.module, "repo": "odoo_99.0"}

    def _single(session, text, label, **p):
        if "-[r:INHERITS_VIEW]->(parent:View" in text:
            return None  # no view parent in this fixture
        if "(v:View {xmlid: $xmlid" in text:
            asked.append("view")
            v = graph["views"].get(p["xmlid"])
            if v is None:
                return None
            return {"v": {"xmlid": v.xmlid, "name": v.name, "type": v.view_type,
                          "model": v.model, "mode": v.mode},
                    "module_name": v.module, "repo": "odoo_99.0"}
        if "QWebTmpl {record_xmlid: $xmlid" in text:
            asked.append("qweb_by_record_xmlid")
            hit = sorted((q for q in qwebs if q.record_xmlid == p["xmlid"]),
                         key=lambda q: q.xmlid)
            return _tmpl_rec(hit[0]) if hit else None
        if "(t:QWebTmpl {xmlid: $xmlid" in text:
            asked.append("qweb_by_xmlid")
            hit = [q for q in qwebs if q.xmlid == p["xmlid"]]
            return _tmpl_rec(hit[0]) if hit else None
        if "-[:EXTENDS_TMPL]->(base" in text:
            q = next(q for q in qwebs if q.xmlid == p["txmlid"])
            base = graph["base_of"](q)
            return {"parent_xmlid": base.xmlid} if base else None
        if "(ext:QWebTmpl" in text:
            exts = sorted((q for q in qwebs
                           if (b := graph["base_of"](q)) and b.xmlid == p["txmlid"]),
                          key=lambda q: q.xmlid)
            return {"total": len(exts),
                    "rows": [{"xmlid": q.xmlid, "module": q.module, "repo": "odoo_99.0"}
                             for q in exts][:p["cap"]]}
        if "USES_TEMPLATE" in text:
            reps = sorted(r for r, t in graph["reports"].items() if t == p["txmlid"])
            return {"total": len(reps), "rows": reps[:p["cap"]]}
        raise AssertionError(f"unexpected query: {text}")

    def _data(session, text, label, **p):
        return []  # View extension chain - empty in this fixture

    monkeypatch.setattr(srv, "_get_driver", lambda: _Driver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: V)
    monkeypatch.setattr(srv, "_single_bounded", _single)
    monkeypatch.setattr(srv, "_data_bounded", _data)
    monkeypatch.setattr(srv, "_scope", lambda p=None, **k: {"own": None, "shared": []})

    def _run(xmlid: str) -> str:
        asked.clear()
        return srv._resolve_view(xmlid, V)

    _run.asked = asked
    return _run


def test_record_form_qweb_view_resolves_by_its_record_xmlid(lookup):
    """Q1 - website_forum.default_faq is found and names its t-call key."""
    out = lookup("website_forum.default_faq")
    assert "not found" not in out, out
    assert out.splitlines()[0] == f"website_forum.default_faq (Odoo {V})"
    assert "Type:   qweb" in out
    assert "Template key: website_forum.faq_accordion" in out
    assert "Module: [odoo_99.0] website_forum" in out
    # The extender that names the record id is listed under the template.
    assert "Extended by (1):" in out
    assert "website_forum.faq_extra" in out


def test_record_form_qweb_view_resolves_by_its_website_key(lookup):
    """Q2 - the key finds the same template and names the record xmlid."""
    out = lookup("website_forum.faq_accordion")
    assert "not found" not in out, out
    assert "Record xmlid: website_forum.default_faq" in out
    assert "Template key:" not in out  # the requested name already is the key
    assert "website_forum.faq_extra" in out


def test_template_element_resolves_with_base_extenders_and_reports(lookup):
    """Q3 - a <template> is reachable as a view, with its whole neighbourhood."""
    out = lookup("website_forum.layout")
    assert "not found" not in out, out
    assert "Type:   qweb" in out
    assert "Record xmlid" not in out and "Template key" not in out
    assert "website_forum.forum_layout_ext" in out
    assert "Used by reports (1):" in out
    assert "website_forum.report_faq" in out

    ext = lookup("website_forum.forum_layout_ext")
    assert "Inherits from: website_forum.layout" in ext


def test_ordinary_view_still_resolves_as_a_view(lookup):
    """Q4 - a form view answers from :View; the QWeb fallback is never asked."""
    out = lookup("website_forum.forum_view_form")
    assert "Model:  forum.forum" in out
    assert "Type:   form" in out
    assert lookup.asked == ["view"]


def test_unknown_xmlid_keeps_the_not_found_answer(lookup):
    """Q5 - neither a view nor a template: the exact not-found line."""
    out = lookup("website_forum.nothing_here")
    assert out == f"View 'website_forum.nothing_here' not found in Odoo {V}."
    assert lookup.asked == ["view", "qweb_by_xmlid", "qweb_by_record_xmlid"]
