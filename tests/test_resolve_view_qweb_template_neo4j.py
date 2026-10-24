# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-Cypher contract of the QWeb fallback of entity_lookup(kind='view').

The no-DB lane (``tests/test_resolve_view_qweb_template.py``) proves the routing
from real parser output; this file pins what only a live graph can: the Cypher
matches a :QWebTmpl by ``xmlid`` and by ``record_xmlid``, skips ``__unresolved__``
placeholders, and lists extenders and reports from the real edges.

Business rules under test:
  N1  A record-form QWeb view resolves by its record xmlid and by its key.
  N2  A template whose own xmlid is the requested name wins over another
      template that only carries it as record_xmlid.
  N3  An unresolved placeholder template is never rendered.
  N4  Extenders (EXTENDS_TMPL) and reports (USES_TEMPLATE) are listed.
  N5  A :View with the requested xmlid still answers as a view.
"""
import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

_MOD = "t_qwv_forum"


def _seed(driver):
    with driver.session() as s:
        s.run("MERGE (m:Module {name:$m, odoo_version:$v}) SET m.profile=['p'], m.repo='r'",
              m=_MOD, v=TEST_VERSION)
        for x, key, rec in (
            (f"{_MOD}.faq_accordion", f"{_MOD}.faq_accordion", f"{_MOD}.default_faq"),
            (f"{_MOD}.layout", None, None),
            (f"{_MOD}.faq_extra", None, None),
            # N2 decoy: carries an existing node's xmlid only as its record_xmlid.
            (f"{_MOD}.decoy_key", f"{_MOD}.decoy_key", f"{_MOD}.layout"),
        ):
            s.run("""MATCH (m:Module {name:$m, odoo_version:$v})
                     MERGE (t:QWebTmpl {xmlid:$x, odoo_version:$v})
                     SET t.profile=['p'], t.module=$m, t.key=$key, t.record_xmlid=$rec,
                         t.unresolved=false
                     MERGE (t)-[:DEFINED_IN]->(m)""",
                  m=_MOD, v=TEST_VERSION, x=x, key=key, rec=rec)
        s.run("""MATCH (c:QWebTmpl {xmlid:$c, odoo_version:$v})
                 MATCH (p:QWebTmpl {xmlid:$p, odoo_version:$v})
                 MERGE (c)-[:EXTENDS_TMPL]->(p)""",
              c=f"{_MOD}.faq_extra", p=f"{_MOD}.faq_accordion", v=TEST_VERSION)
        s.run("""MATCH (t:QWebTmpl {xmlid:$t, odoo_version:$v})
                 MERGE (rp:Report {xmlid:$r, odoo_version:$v})
                 SET rp.profile=['p'], rp.module=$m
                 MERGE (rp)-[:USES_TEMPLATE]->(t)""",
              t=f"{_MOD}.layout", r=f"{_MOD}.report_faq", m=_MOD, v=TEST_VERSION)
        # N3: a placeholder only (an extender named a template never indexed).
        s.run("""CREATE (:QWebTmpl {xmlid:$x, odoo_version:$v, module:'__unresolved__',
                                    unresolved:true, profile:[]})""",
              x=f"{_MOD}.ghost", v=TEST_VERSION)
        # N5: an ordinary view.
        s.run("""MERGE (vw:View {xmlid:$x, odoo_version:$v})
                 SET vw.profile=['p'], vw.module=$m, vw.model='forum.forum', vw.type='form',
                     vw.name='forum.forum.form'""",
              x=f"{_MOD}.forum_view_form", m=_MOD, v=TEST_VERSION)


@pytest.fixture
def resolve(clean_neo4j):
    _seed(clean_neo4j)
    from src.mcp.server import _resolve_view

    return lambda xmlid: _resolve_view(xmlid, TEST_VERSION)


def test_record_form_view_resolves_by_record_xmlid_and_by_key(resolve):
    """N1 + N4 (extenders)."""
    by_record = resolve(f"{_MOD}.default_faq")
    assert "not found" not in by_record, by_record
    assert f"Template key: {_MOD}.faq_accordion" in by_record
    assert f"{_MOD}.faq_extra" in by_record

    by_key = resolve(f"{_MOD}.faq_accordion")
    assert f"Record xmlid: {_MOD}.default_faq" in by_key
    assert f"{_MOD}.faq_extra" in by_key


def test_own_xmlid_wins_over_a_record_xmlid_alias(resolve):
    """N2 + N4 (reports): layout is its own node, not the decoy's alias."""
    out = resolve(f"{_MOD}.layout")
    assert "Template key" not in out, out
    assert f"{_MOD}.decoy_key" not in out
    assert f"{_MOD}.report_faq" in out


def test_unresolved_placeholder_template_is_not_rendered(resolve):
    """N3."""
    out = resolve(f"{_MOD}.ghost")
    assert out == f"View '{_MOD}.ghost' not found in Odoo {TEST_VERSION}."


def test_ordinary_view_still_answers_as_a_view(resolve):
    """N5."""
    out = resolve(f"{_MOD}.forum_view_form")
    assert "Model:  forum.forum" in out
    assert "qweb" not in out
