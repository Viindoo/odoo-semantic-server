# SPDX-License-Identifier: AGPL-3.0-or-later
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_qweb import parse_file, parse_module


@pytest.fixture
def sale_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="sale",
        odoo_version="17.0",
        repo="odoo_17.0",
        path=str(tmp_path),
        depends=["base"],
        version_raw="17.0.1.0.0",
    )


def write_xml(directory: Path, filename: str, content: str) -> str:
    filepath = directory / filename
    filepath.write_text(textwrap.dedent(content).lstrip())
    return str(filepath)


def test_parse_primary_template(tmp_path, sale_module):
    f = write_xml(
        tmp_path,
        "templates.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template id="sale_order_portal">
                <t t-name="sale.order.portal"/>
            </template>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    assert q.xmlid == "sale.sale_order_portal"
    assert q.module == "sale"
    assert q.odoo_version == "17.0"
    assert q.inherit_xmlid is None


def test_parse_extension_template(tmp_path, sale_module):
    f = write_xml(
        tmp_path,
        "templates.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template id="sale_order_portal_inherit"
                      inherit_id="sale.sale_order_portal">
                <xpath expr="//span[@t-field='o.amount_total']" position="replace">
                    <span t-field="o.amount_total_with_discount"/>
                </xpath>
            </template>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    assert q.xmlid == "sale.sale_order_portal_inherit"
    assert q.inherit_xmlid == "sale.sale_order_portal"


def test_parse_skips_template_without_id(tmp_path, sale_module):
    f = write_xml(
        tmp_path,
        "templates.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template>
                <t t-name="no_id"/>
            </template>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert result == []


def test_parse_skips_invalid_xml(tmp_path, sale_module):
    bad = tmp_path / "bad.xml"
    bad.write_text("<odoo><template id='unclosed'")
    result = parse_file(str(bad), sale_module)
    assert result == []


def test_parse_multiple_templates_in_one_file(tmp_path, sale_module):
    f = write_xml(
        tmp_path,
        "templates.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template id="tmpl_a">
                <div>A</div>
            </template>
            <template id="tmpl_b">
                <div>B</div>
            </template>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 2
    xmlids = {q.xmlid for q in result}
    assert "sale.tmpl_a" in xmlids
    assert "sale.tmpl_b" in xmlids


def test_parse_module_scans_xml_files(tmp_path):
    module = ModuleInfo(
        name="sale",
        odoo_version="17.0",
        repo="odoo_17.0",
        path=str(tmp_path),
        depends=[],
        version_raw="",
    )
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    write_xml(
        views_dir,
        "portal.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template id="portal_tmpl"><div/></template>
        </odoo>
    """,
    )
    result = parse_module(module)
    assert any(q.xmlid == "sale.portal_tmpl" for q in result.qweb)


def test_parse_qweb_view_record_odoo_root(tmp_path, sale_module):
    """A1: <record model="ir.ui.view" type=qweb> with a `key` xmlid + arch body
    must be indexed as a QWebTmpl (keyed on `key`), not dropped.

    This is the v8-v14 website / test_website declaration form that has no
    <field name="model"> child, so parser_xml drops it; parser_qweb must catch it.
    """
    f = write_xml(
        tmp_path,
        "website_data.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <record id="aboutus" model="ir.ui.view">
                <field name="name">About us</field>
                <field name="type">qweb</field>
                <field name="key">website.aboutus</field>
                <field name="arch" type="xml">
                    <t name="About us" t-name="website.aboutus"><div>About</div></t>
                </field>
            </record>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    # Keyed on the `key` field (the public xmlid extenders inherit by), not the id.
    assert q.xmlid == "website.aboutus"
    assert q.module == "sale"
    assert q.odoo_version == "17.0"
    assert q.inherit_xmlid is None
    assert q.content is not None  # arch body captured for embedding


def test_parse_qweb_view_record_openerp_root(tmp_path, sale_module):
    """A1: same as above but under the legacy <openerp> root (v8-v9)."""
    f = write_xml(
        tmp_path,
        "website_data.xml",
        """
        <?xml version="1.0"?>
        <openerp>
            <data>
                <record id="contactus" model="ir.ui.view">
                    <field name="name">Contact us</field>
                    <field name="type">qweb</field>
                    <field name="key">website.contactus</field>
                    <field name="arch" type="xml">
                        <t t-name="website.contactus"><div>Contact</div></t>
                    </field>
                </record>
            </data>
        </openerp>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].xmlid == "website.contactus"


def test_parse_qweb_view_record_with_inherit(tmp_path, sale_module):
    """A1: a qweb-type ir.ui.view record carrying inherit_id resolves its parent
    via inherit_xmlid (the same shape <template inherit_id=...> produces)."""
    f = write_xml(
        tmp_path,
        "data.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <record id="test_view" model="ir.ui.view">
                <field name="type">qweb</field>
                <field name="key">test_website.test_view</field>
                <field name="inherit_id" ref="website.aboutus"/>
                <field name="arch" type="xml">
                    <xpath expr="//div" position="inside"><span/></xpath>
                </field>
            </record>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    assert q.xmlid == "test_website.test_view"
    assert q.inherit_xmlid == "website.aboutus"


def test_parse_qweb_view_record_falls_back_to_id(tmp_path, sale_module):
    """A1: when the qweb record has no `key`, fall back to the record id as xmlid."""
    f = write_xml(
        tmp_path,
        "data.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <record id="snippet_x" model="ir.ui.view">
                <field name="type">qweb</field>
                <field name="arch" type="xml"><t t-name="x"/></field>
            </record>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].xmlid == "sale.snippet_x"


def test_parse_non_qweb_view_record_ignored_by_qweb_parser(tmp_path, sale_module):
    """A1 guard: a standard form-view record (type!=qweb) must NOT be emitted as a
    QWebTmpl here — parser_xml owns those. Prevents double-indexing."""
    f = write_xml(
        tmp_path,
        "views.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_form" model="ir.ui.view">
                <field name="name">form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """,
    )
    result = parse_file(f, sale_module)
    assert result == []


def test_parse_module_skips_static_dir(tmp_path):
    module = ModuleInfo(
        name="sale",
        odoo_version="17.0",
        repo="odoo_17.0",
        path=str(tmp_path),
        depends=[],
        version_raw="",
    )
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    write_xml(
        static_dir,
        "tmpl.xml",
        """
        <?xml version="1.0"?>
        <odoo>
            <template id="should_skip"><div/></template>
        </odoo>
    """,
    )
    result = parse_module(module)
    assert result.qweb == []
