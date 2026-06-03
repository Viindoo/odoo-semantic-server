# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests (no DB) for the xmlid-qualification helper + its two parser
call-sites (Fix C+D — external-id qualification).

Odoo's external-id rule: a value CONTAINING a '.' is already module-qualified
and must be left verbatim; a BARE value is prefixed with the current module.
"""
import textwrap
from pathlib import Path

from src.indexer._xmlid import qualify_xmlid
from src.indexer.models import ModuleInfo
from src.indexer.parser_qweb import parse_file as parse_qweb_file
from src.indexer.parser_xml import parse_file as parse_xml_file

# ---------------------------------------------------------------------------
# qualify_xmlid — pure helper
# ---------------------------------------------------------------------------

def test_qualify_bare_gets_module_prefix():
    assert qualify_xmlid("foo", "mymod") == "mymod.foo"


def test_qualify_already_dotted_is_verbatim():
    # already module.name-qualified → must NOT double-prefix
    assert qualify_xmlid("website_blog.blog_post_complete", "website_blog") == (
        "website_blog.blog_post_complete"
    )
    # cross-module reference stays untouched
    assert qualify_xmlid("other.x", "mymod") == "other.x"


def test_qualify_none_and_empty_return_none():
    assert qualify_xmlid(None, "mymod") is None
    assert qualify_xmlid("", "mymod") is None
    assert qualify_xmlid("   ", "mymod") is None


def test_qualify_strips_surrounding_whitespace():
    assert qualify_xmlid("  foo  ", "mymod") == "mymod.foo"
    assert qualify_xmlid("  a.b  ", "mymod") == "a.b"


# ---------------------------------------------------------------------------
# Parser call-sites
# ---------------------------------------------------------------------------

def _module(name: str, tmp_path: Path) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="17.0.1.0.0",
    )


def _write(directory: Path, filename: str, content: str) -> str:
    fp = directory / filename
    fp.write_text(textwrap.dedent(content).lstrip())
    return str(fp)


def test_qweb_own_id_already_qualified_not_double_prefixed(tmp_path):
    mod = _module("website_blog", tmp_path)
    f = _write(tmp_path, "t.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="website_blog.blog_post_complete"><div/></template>
        </odoo>
    """)
    q = parse_qweb_file(f, mod)
    assert len(q) == 1
    # MUST be the qualified id verbatim, NOT website_blog.website_blog.blog_post_complete
    assert q[0].xmlid == "website_blog.blog_post_complete"


def test_qweb_own_id_bare_gets_module_prefix(tmp_path):
    mod = _module("mod", tmp_path)
    f = _write(tmp_path, "t.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="foo"><div/></template>
        </odoo>
    """)
    q = parse_qweb_file(f, mod)
    assert len(q) == 1
    assert q[0].xmlid == "mod.foo"


def test_qweb_inherit_bare_ref_is_qualified(tmp_path):
    mod = _module("mod", tmp_path)
    f = _write(tmp_path, "t.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="child" inherit_id="bare_parent"><div/></template>
        </odoo>
    """)
    q = parse_qweb_file(f, mod)
    assert len(q) == 1
    assert q[0].inherit_xmlid == "mod.bare_parent"


def test_xml_view_bare_inherit_ref_is_qualified(tmp_path):
    mod = _module("mod", tmp_path)
    f = _write(tmp_path, "v.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="child_view" model="ir.ui.view">
                <field name="name">child</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="bare_x"/>
                <field name="arch" type="xml">
                    <field name="partner_id" position="after"><field name="x"/></field>
                </field>
            </record>
        </odoo>
    """)
    views = parse_xml_file(f, mod)
    assert len(views) == 1
    assert views[0].inherit_xmlid == "mod.bare_x"
    assert views[0].xmlid == "mod.child_view"


def test_xml_view_qualified_inherit_ref_stays_verbatim(tmp_path):
    mod = _module("mod", tmp_path)
    f = _write(tmp_path, "v.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="child_view" model="ir.ui.view">
                <field name="name">child</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="other.x"/>
                <field name="arch" type="xml">
                    <field name="partner_id" position="after"><field name="x"/></field>
                </field>
            </record>
        </odoo>
    """)
    views = parse_xml_file(f, mod)
    assert len(views) == 1
    assert views[0].inherit_xmlid == "other.x"


def test_xml_view_own_id_already_qualified_not_double_prefixed(tmp_path):
    mod = _module("mod", tmp_path)
    f = _write(tmp_path, "v.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="mod.existing_view" model="ir.ui.view">
                <field name="name">v</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """)
    views = parse_xml_file(f, mod)
    assert len(views) == 1
    assert views[0].xmlid == "mod.existing_view"
