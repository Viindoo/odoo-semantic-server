# Milestone 2 — "View Wow" Implementation Plan

> **Status:** ✓ DONE — M2 shipped 2026-05-06

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake:** Không bỏ qua edge cases (invalid XML, view không có model, missing inherit_id). Làm đúng ngay từ đầu.
> - **Ship Wow Product:** `resolve_view` output phải đẹp, có cấu trúc cây — AI đọc được ngay, không cần parse thêm.

**Goal:** `resolve_view("sale.view_sale_order_form", "17.0")` trả về đúng view inheritance chain, tất cả XPath modifications từ mọi extension module, và model target — kết nối được từ VS Code / Claude Code.

**Architecture:** `parser_xml.py` parse `<record model="ir.ui.view">` từ XML files → `ViewInfo`; `parser_qweb.py` parse `<template>` → `QWebInfo`; `writer_neo4j.py` ghi View/QWebTmpl nodes + INHERITS_VIEW/EXTENDS_TMPL/TARGETS_MODEL edges; `server.py` expose `resolve_view` tool qua FastMCP.

**Tech Stack:** Python 3.12+, stdlib `xml.etree.ElementTree` (no new deps), neo4j Python driver, fastmcp, pytest.

---

## Cấu Trúc File

```
src/indexer/
├── models.py             -- MODIFY: thêm XPathInfo, ViewInfo, QWebInfo, ViewParseResult
├── parser_xml.py         -- CREATE: parse <record model="ir.ui.view">
├── parser_qweb.py        -- CREATE: parse <template> (QWeb)
└── writer_neo4j.py       -- MODIFY: thêm write_view_results() + indexes

src/mcp/
└── server.py             -- MODIFY: thêm _resolve_view() + @mcp.tool() resolve_view

tests/
├── test_models.py        -- MODIFY: thêm tests cho ViewInfo, QWebInfo, ViewParseResult
├── test_parser_xml.py    -- CREATE: unit tests cho parser_xml
├── test_parser_qweb.py   -- CREATE: unit tests cho parser_qweb
├── test_writer_neo4j.py  -- MODIFY: thêm view/qweb writer tests (integration, neo4j)
├── test_mcp_server.py    -- MODIFY: thêm resolve_view tests (integration, neo4j)
├── test_doc_sync.py      -- CREATE: guard tests (TASKS.md file existence + stale markers)
└── test_output_snapshots.py -- CREATE: MCP output schema contract tests (unit + neo4j)

TASKS.md                  -- MODIFY: cập nhật M2 task status
```

---

## Task 1: Thêm ViewInfo, XPathInfo, QWebInfo, ViewParseResult vào models.py

**Files:**
- Modify: `src/indexer/models.py`
- Modify: `tests/test_models.py`

- [ ] **Bước 1: Viết failing tests**

Thêm vào cuối `tests/test_models.py`:

```python
from src.indexer.models import (
    ModelInfo, ModuleInfo, ParseResult,
    XPathInfo, ViewInfo, QWebInfo, ViewParseResult,  # new imports
)


def test_xpath_info_creation():
    x = XPathInfo(expr="//field[@name='partner_id']", position="after")
    assert x.expr == "//field[@name='partner_id']"
    assert x.position == "after"


def test_view_info_primary_defaults():
    v = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version="17.0",
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    assert v.mode == "primary"
    assert v.inherit_xmlid is None
    assert v.xpaths == []


def test_view_info_extension_with_xpaths():
    xpaths = [
        XPathInfo(expr="//field[@name='partner_id']", position="after"),
        XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
    ]
    v = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin sale order form",
        model="sale.order",
        module="viin_sale",
        odoo_version="17.0",
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=xpaths,
    )
    assert v.mode == "extension"
    assert v.inherit_xmlid == "sale.view_sale_order_form"
    assert len(v.xpaths) == 2
    assert v.xpaths[0].position == "after"


def test_qweb_info_defaults():
    q = QWebInfo(
        xmlid="sale.sale_order_portal",
        module="sale",
        odoo_version="17.0",
    )
    assert q.inherit_xmlid is None


def test_qweb_info_with_inherit():
    q = QWebInfo(
        xmlid="viin_sale.sale_order_portal_inherit",
        module="viin_sale",
        odoo_version="17.0",
        inherit_xmlid="sale.sale_order_portal",
    )
    assert q.inherit_xmlid == "sale.sale_order_portal"


def test_view_parse_result_defaults():
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/tmp", depends=[], version_raw="",
    )
    result = ViewParseResult(module=module)
    assert result.views == []
    assert result.qweb == []
```

- [ ] **Bước 2: Chạy test để xác nhận fail**

```bash
cd /home/tuan/git/odoo-semantic-mcp
source ~/.venv/odoo-semantic-mcp/bin/activate
pytest tests/test_models.py -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'XPathInfo' from 'src.indexer.models'`

- [ ] **Bước 3: Implement — thêm vào cuối `src/indexer/models.py`**

```python
@dataclass
class XPathInfo:
    """XPath modification trong một extension view."""
    expr: str
    position: str  # before | after | inside | replace | attributes


@dataclass
class ViewInfo:
    """Thông tin về một Odoo ir.ui.view record."""
    xmlid: str           # "module.xml_id", e.g., "sale.view_sale_order_form"
    name: str
    model: str           # target Odoo model, e.g., "sale.order"
    module: str
    odoo_version: str
    view_type: str       # form | tree | list | kanban | search | pivot | graph | ...
    mode: str            # "primary" | "extension"
    inherit_xmlid: str | None
    xpaths: list[XPathInfo] = field(default_factory=list)


@dataclass
class QWebInfo:
    """Thông tin về một QWeb template."""
    xmlid: str           # "module.template_id"
    module: str
    odoo_version: str
    inherit_xmlid: str | None = None


@dataclass
class ViewParseResult:
    """Kết quả parse XML files trong một module."""
    module: ModuleInfo
    views: list[ViewInfo] = field(default_factory=list)
    qweb: list[QWebInfo] = field(default_factory=list)
```

- [ ] **Bước 4: Chạy test để xác nhận pass**

```bash
pytest tests/test_models.py -v 2>&1 | tail -20
```

Expected: tất cả tests PASS

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/models.py tests/test_models.py
git commit -m "feat(models): add ViewInfo, XPathInfo, QWebInfo, ViewParseResult for M2"
```

---

## Task 2: parser_xml.py — parse ir.ui.view records

**Files:**
- Create: `src/indexer/parser_xml.py`
- Create: `tests/test_parser_xml.py`

- [ ] **Bước 1: Viết failing tests**

Tạo file `tests/test_parser_xml.py`:

```python
# tests/test_parser_xml.py
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
    filepath.write_text(textwrap.dedent(content))
    return str(filepath)


# --- parse_file tests ---

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


# --- parse_module tests ---

def test_parse_module_scans_all_xml_files(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="",
    )
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    (views_dir / "sale_views.xml").write_text("""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """)
    (views_dir / "sale_line_views.xml").write_text("""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_sale_order_line_form" model="ir.ui.view">
                <field name="name">sale.order.line.form</field>
                <field name="model">sale.order.line</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """)
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
    (static_dir / "view.xml").write_text("""
        <?xml version="1.0"?>
        <odoo>
            <record id="should_be_skipped" model="ir.ui.view">
                <field name="name">static</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml"><form/></field>
            </record>
        </odoo>
    """)
    result = parse_module(module)
    assert result.views == []
    assert result.qweb == []
```

- [ ] **Bước 2: Chạy test để xác nhận fail**

```bash
pytest tests/test_parser_xml.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.parser_xml'`

- [ ] **Bước 3: Implement `src/indexer/parser_xml.py`**

```python
# src/indexer/parser_xml.py
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import ModuleInfo, ViewInfo, ViewParseResult, XPathInfo

_VIEW_TYPES = {
    "form", "tree", "list", "kanban", "search",
    "pivot", "graph", "calendar", "gantt", "activity", "map",
}


def _parse_record(record: ET.Element, module: ModuleInfo) -> ViewInfo | None:
    if record.get("model") != "ir.ui.view":
        return None

    xml_id = record.get("id", "").strip()
    if not xml_id:
        return None

    name = ""
    model = ""
    inherit_xmlid = None
    view_type = "form"
    mode = "primary"
    xpaths: list[XPathInfo] = []

    for child in record:
        if child.tag != "field":
            continue
        fname = child.get("name", "")
        if fname == "name":
            name = (child.text or "").strip()
        elif fname == "model":
            model = (child.text or "").strip()
        elif fname == "inherit_id":
            ref = child.get("ref", "").strip()
            if ref:
                inherit_xmlid = ref
                mode = "extension"
        elif fname == "arch":
            arch_children = list(child)
            if arch_children:
                first = arch_children[0]
                # Unwrap <data> container used by many extension views
                if first.tag == "data":
                    data_children = list(first)
                    if data_children and data_children[0].tag in _VIEW_TYPES:
                        view_type = data_children[0].tag
                elif first.tag in _VIEW_TYPES:
                    view_type = first.tag
            for xpath_el in child.iter("xpath"):
                expr = xpath_el.get("expr", "").strip()
                position = xpath_el.get("position", "inside").strip()
                if expr:
                    xpaths.append(XPathInfo(expr=expr, position=position))

    if not model:
        return None

    return ViewInfo(
        xmlid=f"{module.name}.{xml_id}",
        name=name,
        model=model,
        module=module.name,
        odoo_version=module.odoo_version,
        view_type=view_type,
        mode=mode,
        inherit_xmlid=inherit_xmlid,
        xpaths=xpaths,
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[ViewInfo]:
    """Parse một file XML, trả về list ViewInfo tìm được."""
    try:
        tree = ET.parse(filepath)
    except ET.ParseError:
        return []
    root = tree.getroot()
    views = []
    for record in root.iter("record"):
        view = _parse_record(record, module)
        if view:
            views.append(view)
    return views


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse toàn bộ file XML trong một module directory."""
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}
    for xml_file in sorted(module_path.rglob("*.xml")):
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.views.extend(parse_file(str(xml_file), module_info))
    return result
```

- [ ] **Bước 4: Chạy test để xác nhận pass**

```bash
pytest tests/test_parser_xml.py -v 2>&1 | tail -20
```

Expected: tất cả 10 tests PASS

- [ ] **Bước 5: Chạy lint**

```bash
ruff check src/indexer/parser_xml.py tests/test_parser_xml.py
```

Expected: no errors

- [ ] **Bước 6: Commit**

```bash
git add src/indexer/parser_xml.py tests/test_parser_xml.py
git commit -m "feat(parser): add parser_xml for ir.ui.view records with xpath extraction"
```

---

## Task 3: parser_qweb.py — parse QWeb templates

**Files:**
- Create: `src/indexer/parser_qweb.py`
- Create: `tests/test_parser_qweb.py`

- [ ] **Bước 1: Viết failing tests**

Tạo file `tests/test_parser_qweb.py`:

```python
# tests/test_parser_qweb.py
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_qweb import parse_file, parse_module


@pytest.fixture
def sale_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=["base"], version_raw="17.0.1.0.0",
    )


def write_xml(directory: Path, filename: str, content: str) -> str:
    filepath = directory / filename
    filepath.write_text(textwrap.dedent(content))
    return str(filepath)


def test_parse_primary_template(tmp_path, sale_module):
    f = write_xml(tmp_path, "templates.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="sale_order_portal">
                <t t-name="sale.order.portal"/>
            </template>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    assert q.xmlid == "sale.sale_order_portal"
    assert q.module == "sale"
    assert q.odoo_version == "17.0"
    assert q.inherit_xmlid is None


def test_parse_extension_template(tmp_path, sale_module):
    f = write_xml(tmp_path, "templates.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="sale_order_portal_inherit"
                      inherit_id="sale.sale_order_portal">
                <xpath expr="//span[@t-field='o.amount_total']" position="replace">
                    <span t-field="o.amount_total_with_discount"/>
                </xpath>
            </template>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    q = result[0]
    assert q.xmlid == "sale.sale_order_portal_inherit"
    assert q.inherit_xmlid == "sale.sale_order_portal"


def test_parse_skips_template_without_id(tmp_path, sale_module):
    f = write_xml(tmp_path, "templates.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template>
                <t t-name="no_id"/>
            </template>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert result == []


def test_parse_skips_invalid_xml(tmp_path, sale_module):
    bad = tmp_path / "bad.xml"
    bad.write_text("<odoo><template id='unclosed'")
    result = parse_file(str(bad), sale_module)
    assert result == []


def test_parse_multiple_templates_in_one_file(tmp_path, sale_module):
    f = write_xml(tmp_path, "templates.xml", """
        <?xml version="1.0"?>
        <odoo>
            <template id="tmpl_a">
                <div>A</div>
            </template>
            <template id="tmpl_b">
                <div>B</div>
            </template>
        </odoo>
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 2
    xmlids = {q.xmlid for q in result}
    assert "sale.tmpl_a" in xmlids
    assert "sale.tmpl_b" in xmlids


def test_parse_module_scans_xml_files(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="",
    )
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    (views_dir / "portal.xml").write_text("""
        <?xml version="1.0"?>
        <odoo>
            <template id="portal_tmpl"><div/></template>
        </odoo>
    """)
    result = parse_module(module)
    assert any(q.xmlid == "sale.portal_tmpl" for q in result.qweb)


def test_parse_module_skips_static_dir(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="",
    )
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "tmpl.xml").write_text("""
        <?xml version="1.0"?>
        <odoo>
            <template id="should_skip"><div/></template>
        </odoo>
    """)
    result = parse_module(module)
    assert result.qweb == []
```

- [ ] **Bước 2: Chạy test để xác nhận fail**

```bash
pytest tests/test_parser_qweb.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.parser_qweb'`

- [ ] **Bước 3: Implement `src/indexer/parser_qweb.py`**

```python
# src/indexer/parser_qweb.py
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import ModuleInfo, QWebInfo, ViewParseResult


def _parse_template(elem: ET.Element, module: ModuleInfo) -> QWebInfo | None:
    template_id = elem.get("id", "").strip()
    if not template_id:
        return None
    inherit_xmlid = elem.get("inherit_id", "").strip() or None
    return QWebInfo(
        xmlid=f"{module.name}.{template_id}",
        module=module.name,
        odoo_version=module.odoo_version,
        inherit_xmlid=inherit_xmlid,
    )


def parse_file(filepath: str, module: ModuleInfo) -> list[QWebInfo]:
    """Parse một file XML, trả về list QWebInfo tìm được."""
    try:
        tree = ET.parse(filepath)
    except ET.ParseError:
        return []
    root = tree.getroot()
    qweb = []
    for tmpl in root.iter("template"):
        q = _parse_template(tmpl, module)
        if q:
            qweb.append(q)
    return qweb


def parse_module(module_info: ModuleInfo) -> ViewParseResult:
    """Parse toàn bộ file XML trong một module directory."""
    result = ViewParseResult(module=module_info)
    module_path = Path(module_info.path)
    SKIP_DIRS = {".git", "static", "tests", "__pycache__"}
    for xml_file in sorted(module_path.rglob("*.xml")):
        if SKIP_DIRS & set(xml_file.parts):
            continue
        result.qweb.extend(parse_file(str(xml_file), module_info))
    return result
```

- [ ] **Bước 4: Chạy test để xác nhận pass**

```bash
pytest tests/test_parser_qweb.py -v 2>&1 | tail -20
```

Expected: tất cả 7 tests PASS

- [ ] **Bước 5: Chạy toàn bộ unit tests (không cần Docker)**

```bash
pytest tests/ -m "not neo4j" -v 2>&1 | tail -20
```

Expected: tất cả PASS

- [ ] **Bước 6: Commit**

```bash
git add src/indexer/parser_qweb.py tests/test_parser_qweb.py
git commit -m "feat(parser): add parser_qweb for QWeb template inheritance chain"
```

---

## Task 4: writer_neo4j.py — ghi View/QWebTmpl nodes + edges

**Files:**
- Modify: `src/indexer/writer_neo4j.py`
- Modify: `tests/test_writer_neo4j.py`

**Schema Neo4j:**
- Node `View`: MERGE key = `(xmlid, odoo_version)`, SET: name, model, module, type, mode, xpaths_exprs, xpaths_positions
- Node `QWebTmpl`: MERGE key = `(xmlid, odoo_version)`, SET: module
- Edges: `DEFINED_IN`, `TARGETS_MODEL`, `INHERITS_VIEW`, `EXTENDS_TMPL`
- Indexes: `FOR (n:View) ON (n.xmlid, n.odoo_version)`, `FOR (n:QWebTmpl) ON (n.xmlid, n.odoo_version)`

**Unresolved pattern:** Nếu parent view không tìm được, tạo placeholder `View {xmlid: ..., module: '__unresolved__'}` + edge với `unresolved: true`. Pattern nhất quán với M1 (INHERITS/DELEGATES_TO).

- [ ] **Bước 1: Viết failing tests (Neo4j integration)**

Thêm vào cuối `tests/test_writer_neo4j.py`:

```python
# --- View/QWeb writer tests (thêm vào cuối file) ---
from src.indexer.models import (
    FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult,
    ViewInfo, QWebInfo, ViewParseResult, XPathInfo,  # new imports
)


def make_view_parse_result(
    module_name: str,
    views: list | None = None,
    qweb: list | None = None,
) -> ViewParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    return ViewParseResult(module=module, views=views or [], qweb=qweb or [])


def test_write_view_node(writer, neo4j_driver):
    view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    result = make_view_parse_result("sale", views=[view])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (v:View {xmlid: $x, odoo_version: $v}) RETURN v",
            x="sale.view_sale_order_form", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["v"]["type"] == "form"
    assert rec["v"]["mode"] == "primary"
    assert rec["v"]["model"] == "sale.order"


def test_write_view_xpaths_stored(writer, neo4j_driver):
    view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin inherit",
        model="sale.order",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[
            XPathInfo(expr="//field[@name='partner_id']", position="after"),
            XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
        ],
    )
    result = make_view_parse_result("viin_sale", views=[view])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (v:View {xmlid: $x, odoo_version: $v}) RETURN v",
            x="viin_sale.view_sale_order_form_inherit", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert list(rec["v"]["xpaths_exprs"]) == [
        "//field[@name='partner_id']",
        "//button[@name='action_confirm']",
    ]
    assert list(rec["v"]["xpaths_positions"]) == ["after", "attributes"]


def test_write_inherits_view_edge(writer, neo4j_driver):
    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="base", model="sale.order", module="sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="primary", inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="ext", model="sale.order", module="viin_sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="extension", inherit_xmlid="sale.view_sale_order_form",
    )
    writer.write_view_results([
        make_view_parse_result("sale", views=[base_view]),
        make_view_parse_result("viin_sale", views=[ext_view]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:View {xmlid: $ext_xmlid, odoo_version: $v})
                  -[:INHERITS_VIEW]->
                  (base:View {xmlid: $base_xmlid, odoo_version: $v})
            RETURN count(*) AS cnt
        """, ext_xmlid="viin_sale.view_sale_order_form_inherit",
             base_xmlid="sale.view_sale_order_form", v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_write_inherits_view_unresolved(writer, neo4j_driver, caplog):
    import logging
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="ext", model="sale.order", module="viin_sale",
        odoo_version=TEST_VERSION, view_type="form",
        mode="extension", inherit_xmlid="sale.view_sale_order_form",  # NOT seeded
    )
    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_neo4j"):
        writer.write_view_results([make_view_parse_result("viin_sale", views=[ext_view])])

    assert "unresolved INHERITS_VIEW" in caplog.text
    assert "viin_sale.view_sale_order_form_inherit" in caplog.text

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:View {xmlid: $ext_xmlid, odoo_version: $v})
                  -[r:INHERITS_VIEW]->(:View {xmlid: $base_xmlid, module: '__unresolved__'})
            RETURN r.unresolved AS unresolved
        """, ext_xmlid="viin_sale.view_sale_order_form_inherit",
             base_xmlid="sale.view_sale_order_form", v=TEST_VERSION).single()
    assert rec is not None
    assert rec["unresolved"] is True


def test_write_qweb_node(writer, neo4j_driver):
    q = QWebInfo(
        xmlid="sale.sale_order_portal",
        module="sale",
        odoo_version=TEST_VERSION,
    )
    result = make_view_parse_result("sale", qweb=[q])
    writer.write_view_results([result])

    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (t:QWebTmpl {xmlid: $x, odoo_version: $v}) RETURN t",
            x="sale.sale_order_portal", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["t"]["module"] == "sale"


def test_write_extends_tmpl_edge(writer, neo4j_driver):
    base_q = QWebInfo(xmlid="sale.portal_tmpl", module="sale", odoo_version=TEST_VERSION)
    ext_q = QWebInfo(
        xmlid="viin_sale.portal_tmpl_inherit", module="viin_sale",
        odoo_version=TEST_VERSION, inherit_xmlid="sale.portal_tmpl",
    )
    writer.write_view_results([
        make_view_parse_result("sale", qweb=[base_q]),
        make_view_parse_result("viin_sale", qweb=[ext_q]),
    ])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (ext:QWebTmpl {xmlid: $ext, odoo_version: $v})
                  -[:EXTENDS_TMPL]->
                  (base:QWebTmpl {xmlid: $base, odoo_version: $v})
            RETURN count(*) AS cnt
        """, ext="viin_sale.portal_tmpl_inherit",
             base="sale.portal_tmpl", v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_view_xpaths_arrays_length_invariant(writer, neo4j_driver):
    """xpaths_exprs và xpaths_positions phải luôn cùng độ dài (parallel array invariant)."""
    view = ViewInfo(
        xmlid="sale.view_xpaths_invariant_test",
        name="invariant test", model="sale.order", module="sale",
        odoo_version=TEST_VERSION, view_type="form", mode="extension",
        inherit_xmlid="sale.base_view",
        xpaths=[
            XPathInfo(expr="//field[@name='a']", position="after"),
            XPathInfo(expr="//field[@name='b']", position="inside"),
            XPathInfo(expr="//button[@name='c']", position="attributes"),
        ],
    )
    writer.write_view_results([make_view_parse_result("sale", views=[view])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (v:View {xmlid: $x, odoo_version: $ver})
            RETURN size(v.xpaths_exprs) AS exprs_count,
                   size(v.xpaths_positions) AS pos_count
        """, x="sale.view_xpaths_invariant_test", ver=TEST_VERSION).single()
    assert rec["exprs_count"] == rec["pos_count"] == 3


def test_write_view_indexes_created(writer, neo4j_driver):
    """Verify indexes for View and QWebTmpl exist after setup_indexes()."""
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES YIELD labelsOrTypes, properties").data()
    view_index = any(
        "View" in (r.get("labelsOrTypes") or [])
        for r in indexes
    )
    qweb_index = any(
        "QWebTmpl" in (r.get("labelsOrTypes") or [])
        for r in indexes
    )
    assert view_index, "Missing index on :View"
    assert qweb_index, "Missing index on :QWebTmpl"
```

- [ ] **Bước 2: Chạy tests để xác nhận fail**

```bash
pytest tests/test_writer_neo4j.py -v -m neo4j 2>&1 | tail -20
```

Expected: `AttributeError: 'Neo4jWriter' object has no attribute 'write_view_results'`

- [ ] **Bước 3: Implement — sửa `src/indexer/writer_neo4j.py`**

Thêm import ở đầu file (sau `from .models import ParseResult`):
```python
from .models import ParseResult, ViewParseResult
```

Thêm hàm private sau `_write_parse_result`:

```python
def _write_view_parse_result(tx, result: ViewParseResult) -> None:
    module = result.module

    for view in result.views:
        tx.run("""
            MERGE (v:View {xmlid: $xmlid, odoo_version: $ver})
            SET v.name = $name, v.model = $model, v.module = $module,
                v.type = $view_type, v.mode = $mode,
                v.xpaths_exprs = $xpaths_exprs,
                v.xpaths_positions = $xpaths_positions
        """, xmlid=view.xmlid, ver=view.odoo_version,
             name=view.name, model=view.model, module=view.module,
             view_type=view.view_type, mode=view.mode,
             xpaths_exprs=[x.expr for x in view.xpaths],
             xpaths_positions=[x.position for x in view.xpaths])

        tx.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            MERGE (mod:Module {name: $module, odoo_version: $ver})
            MERGE (v)-[:DEFINED_IN]->(mod)
        """, xmlid=view.xmlid, ver=view.odoo_version, module=view.module)

        if view.inherit_xmlid:
            rec = tx.run("""
                MATCH (ext:View {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base:View {xmlid: $inherit_xmlid, odoo_version: $ver})
                WHERE NOT coalesce(base.unresolved, false)
                MERGE (ext)-[:INHERITS_VIEW]->(base)
                RETURN 1 AS ok
            """, xmlid=view.xmlid, ver=view.odoo_version,
                 inherit_xmlid=view.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved INHERITS_VIEW: %s → %s (version %s) — parent view not indexed",
                    view.xmlid, view.inherit_xmlid, view.odoo_version,
                )
                tx.run("""
                    MATCH (ext:View {xmlid: $xmlid, odoo_version: $ver})
                    MERGE (placeholder:View {xmlid: $inherit_xmlid,
                                             module: '__unresolved__', odoo_version: $ver})
                    ON CREATE SET placeholder.unresolved = true
                    MERGE (ext)-[:INHERITS_VIEW {unresolved: true}]->(placeholder)
                """, xmlid=view.xmlid, ver=view.odoo_version,
                     inherit_xmlid=view.inherit_xmlid)

    for qweb in result.qweb:
        tx.run("""
            MERGE (t:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
            SET t.module = $module
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module)

        tx.run("""
            MATCH (t:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
            MERGE (mod:Module {name: $module, odoo_version: $ver})
            MERGE (t)-[:DEFINED_IN]->(mod)
        """, xmlid=qweb.xmlid, ver=qweb.odoo_version, module=qweb.module)

        if qweb.inherit_xmlid:
            rec = tx.run("""
                MATCH (ext:QWebTmpl {xmlid: $xmlid, odoo_version: $ver})
                MATCH (base:QWebTmpl {xmlid: $inherit_xmlid, odoo_version: $ver})
                MERGE (ext)-[:EXTENDS_TMPL]->(base)
                RETURN 1 AS ok
            """, xmlid=qweb.xmlid, ver=qweb.odoo_version,
                 inherit_xmlid=qweb.inherit_xmlid).single()
            if rec is None:
                _logger.warning(
                    "unresolved EXTENDS_TMPL: %s → %s (version %s) — base template not indexed",
                    qweb.xmlid, qweb.inherit_xmlid, qweb.odoo_version,
                )
```

Thêm vào `setup_indexes()` trong class `Neo4jWriter` (trong for loop của các `session.run`):

```python
"CREATE INDEX IF NOT EXISTS FOR (n:View) ON (n.xmlid, n.odoo_version)",
"CREATE INDEX IF NOT EXISTS FOR (n:QWebTmpl) ON (n.xmlid, n.odoo_version)",
```

Thêm method mới vào class `Neo4jWriter` sau `write_results`:

```python
def write_view_results(self, results: list[ViewParseResult]) -> None:
    with self.driver.session() as session:
        for result in results:
            session.execute_write(_write_view_parse_result, result)
```

- [ ] **Bước 4: Chạy tests để xác nhận pass**

```bash
pytest tests/test_writer_neo4j.py -v -m neo4j 2>&1 | tail -30
```

Expected: tất cả tests PASS (bao gồm cả M1 tests cũ)

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/writer_neo4j.py tests/test_writer_neo4j.py
git commit -m "feat(writer): write View/QWebTmpl nodes, INHERITS_VIEW/EXTENDS_TMPL edges to Neo4j"
```

---

## Task 5: server.py — thêm resolve_view tool

**Files:**
- Modify: `src/mcp/server.py`
- Modify: `tests/test_mcp_server.py`

**Output format của `resolve_view`:**

```
sale.view_sale_order_form (Odoo 17.0)
├─ Type:   form
├─ Model:  sale.order
├─ Module: [odoo] sale
└─ Mở rộng bởi (2 modules):
    ├─ viin_sale.view_sale_order_form_inherit  →  [tvtma] viin_sale
    │   ├─ xpath: //field[@name='partner_id'] [after]
    │   └─ xpath: //button[@name='action_confirm'] [attributes]
    └─ to_sale.view_sale_form_ext  →  [to_sale_repo] to_sale
```

Và ngược lại, query một extension view:

```
viin_sale.view_sale_order_form_inherit (Odoo 17.0)
├─ Type:   form
├─ Model:  sale.order
├─ Module: [tvtma] viin_sale  (extension)
├─ Kế thừa từ: sale.view_sale_order_form
├─ XPath modifications (2):
│   ├─ //field[@name='partner_id'] [after]
│   └─ //button[@name='action_confirm'] [attributes]
└─ Không có extension thêm
```

- [ ] **Bước 1: Viết failing tests**

Thêm vào cuối `tests/test_mcp_server.py`:

```python
# --- resolve_view tests (thêm vào cuối file) ---
from src.indexer.models import (
    FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult,
    ViewInfo, QWebInfo, ViewParseResult, XPathInfo,
)


@pytest.fixture(scope="module")
def seeded_views(neo4j_driver):
    """Seed Neo4j với view data cho resolve_view tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    VIEW_VERSION = "97.0"  # version riêng để tránh conflict với seeded_neo4j (99.0, 98.0)

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)

    base_mod = ModuleInfo("sale", VIEW_VERSION, "odoo_test", "/tmp", [], "")
    ext_mod = ModuleInfo("viin_sale", VIEW_VERSION, "tvtma_test", "/tmp", ["sale"], "")

    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin sale form inherit",
        model="sale.order",
        module="viin_sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[
            XPathInfo(expr="//field[@name='partner_id']", position="after"),
            XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
        ],
    )

    writer.write_view_results([
        ViewParseResult(module=base_mod, views=[base_view]),
        ViewParseResult(module=ext_mod, views=[ext_view]),
    ])
    writer.close()

    yield VIEW_VERSION  # yield version so tests can use it

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)


@pytest.fixture
def view_tools(seeded_views):
    """Import _resolve_view sau khi đã seed data."""
    view_version = seeded_views
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_view
    return _resolve_view, view_version


def test_resolve_view_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.view_sale_order_form" in result
    assert version in result
    assert "form" in result


def test_resolve_view_shows_model(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.order" in result


def test_resolve_view_shows_extensions(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "viin_sale.view_sale_order_form_inherit" in result


def test_resolve_view_shows_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "//field[@name='partner_id']" in result
    assert "after" in result


def test_resolve_view_extension_shows_parent(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "sale.view_sale_order_form" in result
    assert "Kế thừa từ" in result


def test_resolve_view_extension_shows_own_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "//field[@name='partner_id']" in result
    assert "//button[@name='action_confirm']" in result


def test_resolve_view_not_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("nonexistent.view", version)
    assert "Không tìm thấy" in result
```

- [ ] **Bước 2: Chạy tests để xác nhận fail**

```bash
pytest tests/test_mcp_server.py -v -m neo4j -k "view" 2>&1 | tail -15
```

Expected: `ImportError: cannot import name '_resolve_view' from 'src.mcp.server'`

- [ ] **Bước 3: Implement — thêm vào `src/mcp/server.py`**

Thêm hàm `_resolve_view` trước dòng `@mcp.tool()` đầu tiên:

```python
def _resolve_view(xmlid: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        view_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            OPTIONAL MATCH (v)-[:DEFINED_IN]->(mod:Module)
            RETURN v, mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version).single()

        if not view_rec:
            return f"Không tìm thấy view '{xmlid}' trong Odoo {odoo_version}."

        parent_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                  -[r:INHERITS_VIEW]->(parent:View {odoo_version: $ver})
            WHERE NOT coalesce(r.unresolved, false)
            RETURN parent.xmlid AS parent_xmlid
        """, xmlid=xmlid, ver=odoo_version).single()

        extensions = session.run("""
            MATCH (ext:View {odoo_version: $ver})-[:INHERITS_VIEW]->
                  (v:View {xmlid: $xmlid, odoo_version: $ver})
            WHERE NOT coalesce(ext.unresolved, false)
            OPTIONAL MATCH (ext)-[:DEFINED_IN]->(mod:Module)
            RETURN ext.xmlid AS ext_xmlid,
                   ext.xpaths_exprs AS xpaths_exprs,
                   ext.xpaths_positions AS xpaths_positions,
                   mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version).data()

    v_props = view_rec["v"]
    repo_str = f"[{view_rec['repo']}] " if view_rec.get("repo") else ""
    mode_label = " (extension)" if v_props.get("mode") == "extension" else ""

    lines = [f"{xmlid} (Odoo {odoo_version})"]
    lines.append(f"├─ Type:   {v_props.get('type', '?')}")
    lines.append(f"├─ Model:  {v_props.get('model', '?')}")
    lines.append(f"├─ Module: {repo_str}{view_rec.get('module_name', '?')}{mode_label}")

    if parent_rec:
        lines.append(f"├─ Kế thừa từ: {parent_rec['parent_xmlid']}")
        own_exprs = list(v_props.get("xpaths_exprs") or [])
        own_positions = list(v_props.get("xpaths_positions") or [])
        if own_exprs:
            lines.append(f"├─ XPath modifications ({len(own_exprs)}):")
            for expr, pos in zip(own_exprs, own_positions):
                lines.append(f"│   ├─ {expr} [{pos}]")

    if extensions:
        lines.append(f"└─ Mở rộng bởi ({len(extensions)} modules):")
        for i, ext in enumerate(extensions):
            ext_repo = f"[{ext['repo']}] " if ext.get("repo") else ""
            connector = "    └─" if i == len(extensions) - 1 else "    ├─"
            lines.append(f"{connector} {ext['ext_xmlid']}  →  {ext_repo}{ext.get('module_name', '?')}")
            exprs = list(ext.get("xpaths_exprs") or [])
            positions = list(ext.get("xpaths_positions") or [])
            for expr, pos in zip(exprs, positions):
                lines.append(f"    │   └─ xpath: {expr} [{pos}]")
    else:
        # Always show — covers both primary views with no extensions AND extension views with no children
        lines.append("└─ Không có extension")

    return "\n".join(lines)
```

Thêm `@mcp.tool()` decorator sau các tools hiện có:

```python
@mcp.tool()
def resolve_view(xmlid: str, odoo_version: str = "auto") -> str:
    """Trả về view inheritance chain, XPath modifications từ mọi extension module."""
    return _resolve_view(xmlid, odoo_version)
```

- [ ] **Bước 4: Chạy tests để xác nhận pass**

```bash
pytest tests/test_mcp_server.py -v -m neo4j 2>&1 | tail -30
```

Expected: tất cả tests PASS (bao gồm cả M1 tests cũ)

- [ ] **Bước 5: Chạy toàn bộ integration tests**

```bash
pytest tests/ -v -m neo4j 2>&1 | tail -20
```

Expected: tất cả PASS

- [ ] **Bước 6: Commit**

```bash
git add src/mcp/server.py tests/test_mcp_server.py
git commit -m "feat(mcp): add resolve_view tool — view inheritance chain + xpath modifications"
```

---

## Task 7: Doc Sync Guard — chống drift giữa code và tài liệu

> **Background:** M1 có 21 drift points qua 4 commits (`04c7271`, `53644e7`, `3d22724`, `dcdc4b0`).
> Root cause: không có gì enforce agent update docs sau khi implement.
> Task này implement 2 guard mechanisms được research agent đề xuất (phương án 1 + 2).

**Files:**
- Create: `tests/test_doc_sync.py` (unit tests, no Neo4j — chạy trong CI `unit-tests` job)
- Create: `tests/test_output_snapshots.py` (neo4j tests — chạy trong CI `integration-tests` job)

**Catches:**
- `test_doc_sync.py`: TASKS.md `[x]` file không tồn tại trên disk; stale `[~]` markers
- `test_output_snapshots.py`: output format drift giữa `_resolve_*` functions và architecture doc

- [ ] **Bước 1: Tạo `tests/test_doc_sync.py`**

```python
# tests/test_doc_sync.py
"""
Doc sync guard — unit tests (no Neo4j needed).

Catches two drift categories from M1 (21 drift points across 4 commits):
  1. TASKS.md marks [x] but file doesn't exist on disk
  2. Stale [~] in-progress markers left by incomplete agent work
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _completed_file_refs() -> list[str]:
    """Extract file paths from [x] tasks in TASKS.md."""
    content = (REPO_ROOT / "TASKS.md").read_text()
    files = []
    for line in content.splitlines():
        m = re.match(r"\s*-\s*\[x\]\s*`([^`]+)`", line)
        if not m:
            continue
        raw = m.group(1).split(":")[0].strip()
        if "/" in raw or raw.endswith((".py", ".yml", ".toml", ".md", ".sh")):
            files.append(raw)
    return files


def test_tasks_md_completed_files_exist():
    """Every [x] file in TASKS.md must exist on disk.

    Failing scenario: agent adds '- [x] `src/new_file.py`: description' to TASKS.md
    without creating the file → test fails:
    "TASKS.md marks these [x] but files don't exist: src/new_file.py"
    """
    completed = _completed_file_refs()
    assert len(completed) >= 1, "Regex parsed zero [x] file refs — check TASKS.md format"
    missing = [f for f in completed if not (REPO_ROOT / f).exists()]
    assert not missing, (
        "TASKS.md marks these [x] but files don't exist on disk:\n"
        + "\n".join(f"  {f}" for f in missing)
        + "\nFix: create the file, or revert [x] → [ ] in TASKS.md."
    )


def test_no_stale_in_progress_markers():
    """[~] markers must not remain when a milestone has no open [ ] tasks left.

    During active work: [~] + [x] + [ ] coexist — this is NORMAL, test passes.
    Stale case: [~] + [x] but NO [ ] remaining — milestone looks complete
    but someone forgot to flip [~] → [x].

    Failing scenario: agent marks ALL tasks [x] but leaves one [~] behind:
    "Milestone 2 looks complete but has stale [~]: update [~] → [x] in TASKS.md."
    """
    content = (REPO_ROOT / "TASKS.md").read_text()
    milestone_blocks = re.split(r"(?=## Milestone \d+)", content)
    for block in milestone_blocks:
        if "## Milestone" not in block:
            continue
        header = re.match(r"## Milestone (\d+)", block)
        num = header.group(1) if header else "?"
        has_wip = bool(re.search(r"- \[~\]", block))
        has_done = bool(re.search(r"- \[x\]", block))
        has_pending = bool(re.search(r"- \[ \]", block))
        # Only stale if: has [~] AND has [x] AND no [ ] left (milestone appears complete)
        if has_wip and has_done and not has_pending:
            assert False, (
                f"Milestone {num} looks complete (all done [x], no pending [ ]) "
                f"but has stale [~]. Update [~] → [x] in TASKS.md."
            )
```

- [ ] **Bước 2: Chạy test để xác nhận pass (guard test — luôn pass khi invariant đúng)**

```bash
pytest tests/test_doc_sync.py -v 2>&1 | tail -10
```

Expected: 2 tests PASS — xác nhận current state hợp lệ.

- [ ] **Bước 3: Demo drift detection (không commit)**

Để verify guard hoạt động, thêm tạm dòng fake vào TASKS.md, chạy lại test, rồi revert:

```bash
# Thêm fake entry vào TASKS.md
echo '- [x] `src/indexer/fake_module.py`: fake task' >> TASKS.md
pytest tests/test_doc_sync.py::test_tasks_md_completed_files_exist -v 2>&1 | tail -5
# Expected: FAIL với "TASKS.md marks these [x] but files don't exist: src/indexer/fake_module.py"

# Revert
git checkout TASKS.md
```

- [ ] **Bước 4: Tạo `tests/test_output_snapshots.py`**

```python
# tests/test_output_snapshots.py
"""
MCP output schema guard — integration tests (require Neo4j).

Catches API drift: when _resolve_* functions change output format without
updating docs/thiet-ke-kien-truc.md §MCP Tools Interface.

Run: pytest tests/test_output_snapshots.py -m neo4j
When intentionally changing output format: update assertions here AND architecture doc.
"""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_SNAP_VERSION = "96.0"  # dedicated version — avoids conflict with 99.0 / 98.0 / 97.0 fixtures


@pytest.fixture(scope="module")
def snapshot_db(neo4j_driver):
    """Seed minimal account.move data + yield; teardown after module."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)

    mod = ModuleInfo("account", _SNAP_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="account.move", module="account", odoo_version=_SNAP_VERSION,
        fields=[FieldInfo("name", "char", required=True)],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)


def test_resolve_model_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move (Odoo 96.0)
        ├─ Định nghĩa tại:   [odoo_test] account
        ├─ Tổng số field:  1
        └─ Tổng số method: 1

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_model
    result = _resolve_model("account.move", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move (Odoo {_SNAP_VERSION})", \
        "Line 0 must be '<model> (Odoo <version>)' — see architecture doc §6"
    assert any("Định nghĩa tại" in ln for ln in lines), "Missing 'Định nghĩa tại' (definition source)"
    assert any("Tổng số field" in ln for ln in lines), "Missing field count"
    assert any("Tổng số method" in ln for ln in lines), "Missing method count"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), \
        "Missing tree connectors (Ship Wow Product requirement)"


def test_resolve_field_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.name (Odoo 96.0)
        ├─ Loại:     char
        ├─ Computed: Không
        ...
        └─ Khai báo trong: ...

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_field
    result = _resolve_field("account.move", "name", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.name (Odoo {_SNAP_VERSION})"
    assert any("Loại" in ln for ln in lines), "Missing field type line"
    assert any("Computed" in ln for ln in lines), "Missing computed indicator"
    assert any("Khai báo trong" in ln for ln in lines), "Missing declaration source"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines)


def test_resolve_method_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.action_post() (Odoo 96.0)
        Override chain:
          [odoo_test] account — ✓ gọi super() — decorators: —

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_method
    result = _resolve_method("account.move", "action_post", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.action_post() (Odoo {_SNAP_VERSION})"
    assert any("Override chain" in ln for ln in lines), "Missing 'Override chain' header"
    assert any("super()" in ln for ln in lines), "Missing super() call indicator"
    assert any("decorators" in ln for ln in lines), "Missing decorators field"


def test_resolve_view_not_found_contract(snapshot_db):
    """
    resolve_view NOT_FOUND output contract — added in M2.
    Happy path contract is in test_mcp_server.py::test_resolve_view_found.

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_view
    result = _resolve_view("nonexistent.view.xmlid", _SNAP_VERSION)
    assert "Không tìm thấy" in result, "NOT_FOUND response must contain 'Không tìm thấy'"
    assert "nonexistent.view.xmlid" in result, "NOT_FOUND response must echo the queried xmlid"
```

- [ ] **Bước 5: Chạy test_doc_sync.py để confirm no regression**

```bash
pytest tests/test_doc_sync.py tests/test_output_snapshots.py -v -m "not neo4j" 2>&1 | tail -10
```

Expected: `test_doc_sync.py` 2 PASS; `test_output_snapshots.py` 0 collected (skipped — no neo4j marker match)

- [ ] **Bước 6: Chạy test_output_snapshots.py với Neo4j**

```bash
pytest tests/test_output_snapshots.py -v -m neo4j 2>&1 | tail -15
```

Expected: 4 tests PASS (3 M1 tool contracts + 1 M2 not-found contract)

- [ ] **Bước 7: Chạy full test suite để xác nhận không có regression**

```bash
pytest tests/ -m "not neo4j" -v 2>&1 | tail -5
pytest tests/ -m neo4j -v 2>&1 | tail -10
```

Expected: tất cả PASS

- [ ] **Bước 8: Commit**

```bash
git add tests/test_doc_sync.py tests/test_output_snapshots.py
git commit -m "test(guard): add doc-sync + output-contract guards to catch drift (anti-drift M2)"
```

---

## Task 6: Cập nhật TASKS.md + chạy full test suite

**Files:**
- Modify: `TASKS.md`

- [ ] **Bước 1: Chạy full test suite để xác nhận không có regression**

```bash
pytest tests/ -m "not neo4j" -v 2>&1 | tail -10  # unit tests
pytest tests/ -m neo4j -v 2>&1 | tail -20         # integration tests
```

Expected: tất cả PASS

- [ ] **Bước 2: Chạy lint trên toàn bộ code mới**

```bash
ruff check src/ tests/
```

Expected: no errors

- [ ] **Bước 3: Cập nhật TASKS.md**

Trong `TASKS.md`, sửa section Milestone 2 — mark tất cả tasks `[x]` trừ E2E manual:

```markdown
## Milestone 2 — "View Wow"
**Intent:** Mở rộng semantic awareness sang UI layer + thiết lập anti-drift guard.
**Outcome:** `resolve_view("sale.view_sale_order_form", "17.0")` trả về đúng XPath overrides + view chain.

- [x] `src/indexer/models.py`: thêm XPathInfo, ViewInfo, QWebInfo, ViewParseResult
- [x] `src/indexer/parser_xml.py`: views, inherit_id, xpath targets
- [x] `src/indexer/parser_qweb.py`: template inheritance chain
- [x] `src/indexer/writer_neo4j.py`: View/QWebTmpl nodes + INHERITS_VIEW/EXTENDS_TMPL edges + indexes
- [x] `src/mcp/server.py`: `resolve_view` + view chain reconstruction
- [x] `tests/test_doc_sync.py`: TASKS.md file guard + stale `[~]` marker guard (anti-drift)
- [x] `tests/test_output_snapshots.py`: MCP output schema contract tests (anti-drift)
- [ ] E2E test: kết nối VS Code + Claude Code, verify `resolve_view` kết quả
```

- [ ] **Bước 4: Commit**

```bash
git add TASKS.md
git commit -m "docs: mark M2 tasks complete in TASKS.md"
```

---

## Self-Review

### Spec coverage

| Requirement từ TASKS.md | Task thực hiện |
|-------------------------|---------------|
| `parser_xml.py`: views, inherit_id, xpath targets | Task 2 |
| `parser_qweb.py`: template inheritance chain | Task 3 |
| `server.py`: `resolve_view` + merged_structure | Task 5 |

### Kiểm tra type consistency

| Định nghĩa | Dùng ở |
|------------|--------|
| `XPathInfo(expr, position)` | Task 1 (models), Task 2 (parser_xml), Task 4 (writer), Task 5 (server) |
| `ViewInfo.xpaths: list[XPathInfo]` | Task 2 (parser_xml), Task 4 (writer sets xpaths_exprs/positions) |
| `ViewParseResult.views: list[ViewInfo]` | Task 2, 3 (parser), Task 4 (writer), Task 5 (test seeding) |
| `writer.write_view_results(list[ViewParseResult])` | Task 4 (writer), Task 5 (test fixtures) |
| `_resolve_view(xmlid, odoo_version)` | Task 5 (server + tests) |

### Placeholder scan — không có placeholder trong plan này.

---

## Neo4j Schema Summary (sau M2)

```
(:View     {xmlid, odoo_version, name, model, module, type, mode,
            xpaths_exprs: [str], xpaths_positions: [str]})
            -- KEY = (xmlid, odoo_version)

(:QWebTmpl {xmlid, odoo_version, module})
            -- KEY = (xmlid, odoo_version)

(:View)-[:DEFINED_IN]->(:Module)
(:View)-[:INHERITS_VIEW {unresolved?}]->(:View)
(:QWebTmpl)-[:DEFINED_IN]->(:Module)
(:QWebTmpl)-[:EXTENDS_TMPL]->(:QWebTmpl)
```

**Deferred item — TARGETS_MODEL edge (View → Model):** Được assign sang **M4 (Impact Wow)** vì:
1. `resolve_view` (M2 deliverable) không cần edge này để hoạt động
2. Edge này là prerequisite của M4's `impact_analysis` — khi đổi field, phải tìm được views target model đó
3. M4 sẽ implement cùng với JS parser để có đủ data cho full impact graph

Xem TASKS.md §M4 và `docs/thiet-ke-kien-truc.md` §Relationships để biết thêm chi tiết.
