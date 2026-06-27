# SPDX-License-Identifier: AGPL-3.0-or-later
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
    """Extension views often wrap arch inside <data> rather than directly using view type."""
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


# --- WI-A3: ViewInfo.line (sourceline) ---


def test_parse_view_line_is_set(tmp_path, sale_module):
    """parse_file must set ViewInfo.line to the 1-based source line of the <record> tag."""
    xml_file = tmp_path / "views.xml"
    xml_file.write_text(textwrap.dedent("""\
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">Sale Order Form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """))
    from src.indexer.parser_xml import parse_file as xml_parse_file
    views = xml_parse_file(str(xml_file), sale_module)
    assert len(views) == 1
    # The <record> tag is on line 3 in the dedented content
    assert views[0].line is not None
    assert views[0].line >= 1


# --- T1 (F-5): lxml comment node must not shadow real view-type element ---


def test_view_type_with_leading_comment_direct(tmp_path, sale_module):
    """F-5: <arch> with a leading XML comment → view_type must be 'list', not 'form'."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_list" model="ir.ui.view">
                <field name="name">sale.order.list</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><!-- leading comment -->
                    <list>
                        <field name="name"/>
                    </list>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].view_type == "list", (
        f"Expected 'list', got {result[0].view_type!r} — "
        "leading comment must not shadow the <list> element"
    )


def test_view_type_with_leading_comment_tree(tmp_path, sale_module):
    """F-5: <arch> with a leading XML comment → view_type must be 'tree', not 'form'."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_tree" model="ir.ui.view">
                <field name="name">sale.order.tree</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><!-- inherit from tree view -->
                    <tree>
                        <field name="name"/>
                    </tree>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].view_type == "tree", (
        f"Expected 'tree', got {result[0].view_type!r} — "
        "leading comment must not shadow the <tree> element"
    )


def test_view_type_with_comment_in_data_wrapper(tmp_path, sale_module):
    """F-5: <arch><data><!-- comment --><tree>...</tree></data></arch> → view_type='tree'."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_ext" model="ir.ui.view">
                <field name="name">sale.order.ext</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_sale_order_tree"/>
                <field name="arch" type="xml">
                    <data><!-- extends tree view -->
                        <tree>
                            <field name="amount_total"/>
                        </tree>
                    </data>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].view_type == "tree", (
        f"Expected 'tree', got {result[0].view_type!r} — "
        "comment inside <data> must not shadow the <tree> element"
    )


# --- T2: arch_snippet for base views ---


def test_arch_snippet_set_for_base_view(tmp_path, sale_module):
    """T2: base view (no inherit_id) must carry arch_snippet with view structure."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form>
                        <sheet>
                            <group name="partner_info">
                                <field name="partner_id"/>
                            </group>
                        </sheet>
                    </form>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    view = result[0]
    assert view.arch_snippet is not None, "base view must have arch_snippet"
    assert "<form>" in view.arch_snippet or "form" in view.arch_snippet


def test_arch_snippet_none_for_extension_view(tmp_path, sale_module):
    """T2: extension/inherit view (has inherit_id) must have arch_snippet=None."""
    f = write_xml(tmp_path, "views.xml", """
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
    assert result[0].arch_snippet is None, "extension view must have arch_snippet=None"


def test_arch_snippet_bounded_to_2000_chars(tmp_path, sale_module):
    """T2: arch_snippet must be capped at 2000 characters."""
    # Build a very long arch
    big_arch = "<form>" + ("<!-- padding -->\n" * 200) + "<field name='x'/></form>"
    f = write_xml(tmp_path, "views.xml", f"""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_big" model="ir.ui.view">
                <field name="name">sale.order.big</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">{big_arch}</field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    snippet = result[0].arch_snippet
    assert snippet is not None
    assert len(snippet) <= 2000


# --- GAP-1: conditional-visibility extraction (attrs/states v8-16, direct v17+) ---


def test_legacy_attrs_invisible_captured(tmp_path, sale_module):
    """v8-v16 form: attrs="{'invisible': [...]}" must surface as a captured condition."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form>
                        <field name="commitment_date"
                               attrs="{'invisible': [('state', '=', 'draft')]}"/>
                    </form>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    conds = result[0].conditions
    # exactly one condition, on the commitment_date field, attrs.invisible, legacy
    assert len(conds) == 1
    c = conds[0]
    assert c.element == "field"
    assert c.field == "commitment_date"
    assert c.attr == "attrs.invisible"
    assert c.legacy is True
    # raw domain preserved (no evaluation); the field+operator must be present
    assert "state" in c.expr and "draft" in c.expr


def test_legacy_states_captured(tmp_path, sale_module):
    """v8-v16 states="draft,sent" must surface as a captured condition."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form>
                        <button name="action_confirm" states="draft,sent"/>
                    </form>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    conds = result[0].conditions
    states = [c for c in conds if c.attr == "states"]
    assert len(states) == 1
    assert states[0].element == "button"
    assert states[0].expr == "draft,sent"
    assert states[0].legacy is True


def test_v17_direct_invisible_and_column_invisible_captured(tmp_path, sale_module):
    """v17+ direct invisible="expr" + column_invisible="1" captured with provenance."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_list" model="ir.ui.view">
                <field name="name">sale.order.list</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <list>
                        <field name="commitment_date" invisible="state == 'draft'"/>
                        <field name="company_id" column_invisible="1"/>
                    </list>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    conds = result[0].conditions
    by_attr = {(c.field, c.attr): c for c in conds}
    # direct invisible expression on commitment_date - non-legacy
    inv = by_attr[("commitment_date", "invisible")]
    assert inv.legacy is False
    assert inv.expr == "state == 'draft'"
    # column_invisible on company_id - captured, non-legacy
    col = by_attr[("company_id", "column_invisible")]
    assert col.legacy is False
    assert col.expr == "1"


def test_conditions_empty_when_no_conditional_attrs(tmp_path, sale_module):
    """A plain view with no attrs/states/direct-expr attributes -> conditions == []."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form><field name="partner_id"/></form>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result[0].conditions == []


def test_conditions_captured_in_xpath_inserted_field(tmp_path, sale_module):
    """Conditions on a field inserted via <xpath> in an extension view are captured."""
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_form_inherit" model="ir.ui.view">
                <field name="name">sale.order.form.inherit</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_sale_order_form"/>
                <field name="arch" type="xml">
                    <data>
                        <xpath expr="//field[@name='partner_id']" position="after">
                            <field name="x_extra" invisible="state == 'done'"/>
                        </xpath>
                    </data>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    conds = result[0].conditions
    assert any(
        c.field == "x_extra" and c.attr == "invisible" and not c.legacy
        for c in conds
    )


# --- GAP-9: EE view types must not silently default to "form" ---


def test_ee_view_type_map_captured(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_order_map" model="ir.ui.view">
                <field name="name">sale.order.map</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <map res_partner="partner_id"><field name="name"/></map>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result[0].view_type == "map"


def test_ee_view_type_hierarchy_captured(tmp_path, sale_module):
    f = write_xml(tmp_path, "views.xml", """
        <?xml version="1.0"?>
        <odoo>
            <record id="view_emp_hierarchy" model="ir.ui.view">
                <field name="name">hr.employee.hierarchy</field>
                <field name="model">hr.employee</field>
                <field name="arch" type="xml">
                    <hierarchy><field name="name"/></hierarchy>
                </field>
            </record>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result[0].view_type == "hierarchy", (
        f"Expected 'hierarchy', got {result[0].view_type!r} - "
        "EE hierarchy view must not default to 'form'"
    )
