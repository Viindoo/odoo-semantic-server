import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_xml import parse_file, parse_module


@pytest.fixture
def sale_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=["base"], version_raw="17.0.1.0.0",
    )


def write_xml(directory: Path, filename: str, content: str) -> str:
    filepath = directory / filename
    filepath.write_text(textwrap.dedent(content).strip())
    return str(filepath)


def test_parse_primary_view(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form>
                        <field name="partner_id"/>
                    </form>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    view = result[0]
    assert view.xmlid == "sale.view_sale_order_form"
    assert view.model == "sale.order"
    assert view.view_type == "form"
    assert view.mode == "primary"
    assert view.inherit_xmlid is None
    assert view.xpaths == []


def test_parse_extension_view_with_xpaths(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form_inherit" model="ir.ui.view">
                <field name="name">viin sale order form inherit</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_sale_order_form"/>
                <field name="arch" type="xml">
                    <data>
                        <xpath expr="//field[@name='partner_id']" position="after">
                            <field name="x_approval_state"/>
                        </xpath>
                        <xpath expr="//button[@name='action_confirm']" position="attributes">
                            <attribute name="class">btn-primary</attribute>
                        </xpath>
                    </data>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    view = result[0]
    assert view.xmlid == "sale.view_sale_order_form_inherit"
    assert view.mode == "extension"
    assert view.inherit_xmlid == "sale.view_sale_order_form"
    assert len(view.xpaths) == 2
    assert view.xpaths[0].expr == "//field[@name='partner_id']"
    assert view.xpaths[0].position == "after"
    assert view.xpaths[1].expr == "//button[@name='action_confirm']"
    assert view.xpaths[1].position == "attributes"


def test_parse_view_type_from_arch(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_tree" model="ir.ui.view">
                <field name="name">sale.order.tree</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <tree>
                        <field name="name"/>
                    </tree>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result[0].view_type == "tree"


def test_parse_view_type_with_data_wrapper(tmp_path, sale_module):
    """Extension views thường bọc arch trong <data>, không phải trực tiếp view type."""
    f = write_xml(tmp_path, "ext_views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form_inherit" model="ir.ui.view">
                <field name="name">sale.order.form.inherit</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_sale_order_form"/>
                <field name="arch" type="xml">
                    <data>
                        <xpath expr="//field[@name='partner_id']" position="after">
                            <field name="x_field"/>
                        </xpath>
                    </data>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    view = result[0]
    # view_type should NOT be "data" — parser must look inside <data>
    assert view.view_type != "data"
    # xpaths inside <data> must still be captured
    assert len(view.xpaths) == 1
    assert view.xpaths[0].expr == "//field[@name='partner_id']"


def test_parse_skips_non_view_records(tmp_path, sale_module):
    f = write_xml(tmp_path, "data.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="sale_group" model="res.groups">
                <field name="name">Sales</field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result == []


def test_parse_skips_record_without_model_field(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="bad_view" model="ir.ui.view">
                <field name="name">no model set</field>
                <field name="arch" type="xml">
                    <form/>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result == []


def test_parse_skips_invalid_xml(tmp_path, sale_module):
    bad = tmp_path / "bad.xml"
    bad.write_text("<odoo><record id='unclosed'")
    result = parse_file(str(bad), sale_module)
    assert result == []


def test_parse_multiple_views_in_one_file(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
            <record id="view_sale_order_tree" model="ir.ui.view">
                <field name="name">sale.order.tree</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><tree/></field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 2
    xmlids = {v.xmlid for v in result}
    assert "sale.view_sale_order_form" in xmlids
    assert "sale.view_sale_order_tree" in xmlids


def test_parse_module_scans_all_xml_files(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="",
    )
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    (views_dir / "sale_views.xml").write_text(textwrap.dedent("""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """).strip())
    (views_dir / "sale_line_views.xml").write_text(textwrap.dedent("""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_line_form" model="ir.ui.view">
                <field name="name">sale.order.line.form</field>
                <field name="model">sale.order.line</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """).strip())
    result = parse_module(module)
    xmlids = {v.xmlid for v in result.views}
    assert "sale.view_sale_order_form" in xmlids
    assert "sale.view_sale_order_line_form" in xmlids


def test_parse_module_skips_static_dir(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="",
    )
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "view.xml").write_text(textwrap.dedent("""
        <?xml version="1.0"?>
        <odoo>
            <record id="should_be_skipped" model="ir.ui.view">
                <field name="name">static</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """).strip())
    result = parse_module(module)
    assert result.views == []
    assert result.qweb == []
