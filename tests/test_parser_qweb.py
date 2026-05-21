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
