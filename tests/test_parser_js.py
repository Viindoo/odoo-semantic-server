"""Tests for JS parser — era detection and chunking."""

from src.indexer.models import ModuleInfo
from src.indexer.parser_js import _detect_era, parse_file, parse_module_graph


def _module(tmp_path, name="sale", version="17.0"):
    m = ModuleInfo(
        name=name, odoo_version=version, repo="test", path=str(tmp_path), depends=[]
    )
    return m


def _write_js(tmp_path, filename: str, content: str) -> str:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


# --- Era detection ---

def test_detect_era1_plain():
    assert _detect_era("var x = Widget.extend({});") == "era1"


def test_detect_era2_odoo_define():
    assert _detect_era("odoo.define('sale.MyWidget', function(require){});") == "era2"


def test_detect_era3_odoo_module_comment():
    assert _detect_era("/** @odoo-module */\nimport { Component } from '@odoo/owl';") == "era3"


def test_detect_era3_import_statement():
    assert _detect_era("import { xml } from '@odoo/owl';") == "era3"


# --- Era 1 parsing ---

def test_parse_era1_basic_extend(tmp_path):
    src = "var MyWidget = AbstractWidget.extend({ start: function() {} });"
    fp = _write_js(tmp_path, "widget.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert len(chunks) >= 1
    chunk = chunks[0]
    assert chunk.era == "era1"
    assert "extend" in chunk.content


def test_parse_era1_fallback_when_no_extend(tmp_path):
    src = "console.log('no extend here');"
    fp = _write_js(tmp_path, "plain.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert len(chunks) >= 1
    assert all(c.era == "era1" for c in chunks)


def test_parse_era1_entity_name_from_variable(tmp_path):
    src = "var SaleWidget = AbstractWidget.extend({ template: 'sale' });"
    fp = _write_js(tmp_path, "sale.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert any("SaleWidget" in c.entity_name or c.entity_name for c in chunks)


# --- Era 2 parsing ---

def test_parse_era2_odoo_define(tmp_path):
    src = """odoo.define('sale.ConfirmDialog', function(require) {
    var Widget = require('web.Widget');
    return Widget.extend({ start: function() { return this._super.apply(this, arguments); } });
});"""
    fp = _write_js(tmp_path, "dialog.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert len(chunks) >= 1
    assert all(c.era == "era2" for c in chunks)


def test_parse_era2_entity_name_from_define_arg(tmp_path):
    src = "odoo.define('sale.MyModule', function(require) { return {}; });"
    fp = _write_js(tmp_path, "m.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert chunks[0].entity_name == "MyModule"


# --- Era 3 parsing ---

def test_parse_era3_owl_class(tmp_path):
    src = (
        "/** @odoo-module */\n"
        "import { Component, xml } from '@odoo/owl';\n\n"
        "export class SaleOrderLine extends Component {\n"
        "    static template = xml`<div>hello</div>`;\n"
        "}\n"
    )
    fp = _write_js(tmp_path, "sale_order_line.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert len(chunks) >= 1
    assert any(c.era == "era3" for c in chunks)
    assert any("SaleOrderLine" in c.entity_name for c in chunks)


def test_parse_era3_patch(tmp_path):
    src = """/** @odoo-module */
import { patch } from '@web/core/utils/patch';
patch("SaleOrderWidget", { setup() { this._super(); } });
"""
    fp = _write_js(tmp_path, "patch.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert any(c.era == "era3" for c in chunks)
    assert any("SaleOrderWidget" in c.entity_name for c in chunks)


# --- Chunking ---

def test_sliding_window_for_large_file(tmp_path):
    # 5000 chars — should produce multiple chunks
    src = "/** @odoo-module */\n" + ("x " * 2500)
    fp = _write_js(tmp_path, "big.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    assert len(chunks) > 1


def test_chunk_idx_monotonic(tmp_path):
    src = "/** @odoo-module */\n" + ("x " * 2500)
    fp = _write_js(tmp_path, "big2.js", src)
    chunks = parse_file(fp, _module(tmp_path))
    indices = [c.chunk_idx for c in chunks]
    assert indices == list(range(len(chunks)))


def test_chunk_module_version_propagated(tmp_path):
    src = "odoo.define('test.M', function(require) { return {}; });"
    fp = _write_js(tmp_path, "m.js", src)
    m = _module(tmp_path, name="my_module", version="17.0")
    chunks = parse_file(fp, m)
    for c in chunks:
        assert c.module == "my_module"
        assert c.odoo_version == "17.0"


# --- parse_module skip logic ---

def test_parse_module_skips_lib_dir(tmp_path):
    """Files inside static/src/lib/ must not produce chunks."""
    from src.indexer.parser_js import parse_module

    static_src = tmp_path / "static" / "src"
    lib_dir = static_src / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "jquery.min.js").write_text(
        "/** @odoo-module */\nexport class JQuery {};", encoding="utf-8"
    )
    # Also put a valid file outside lib/
    app_dir = static_src / "components"
    app_dir.mkdir()
    (app_dir / "my_widget.js").write_text(
        "/** @odoo-module */\nexport class MyWidget {};", encoding="utf-8"
    )

    m = _module(tmp_path)
    chunks = parse_module(m)
    file_paths = {c.file_path for c in chunks}
    assert not any("jquery.min.js" in fp for fp in file_paths), "lib/ file must be excluded"
    assert any("my_widget.js" in fp for fp in file_paths), "non-lib file must be included"


def test_parse_module_skips_oversized_file(tmp_path):
    """Files over 200 KB must not produce chunks."""
    from src.indexer.parser_js import _MAX_JS_BYTES, parse_module

    static_src = tmp_path / "static" / "src"
    static_src.mkdir(parents=True)
    big_file = static_src / "huge.js"
    big_file.write_bytes(b"x" * (_MAX_JS_BYTES + 1))

    m = _module(tmp_path)
    chunks = parse_module(m)
    assert all("huge.js" not in c.file_path for c in chunks), "oversized file must be skipped"


# --- parse_module_graph ---

def _make_static_js(tmp_path, filename: str, content: str, subdir: str = "") -> None:
    """Write JS file into static/src/<subdir>/ of a fake module."""
    base = tmp_path / "static" / "src"
    if subdir:
        base = base / subdir
    base.mkdir(parents=True, exist_ok=True)
    (base / filename).write_text(content, encoding="utf-8")


def test_parse_module_graph_era1_widget_extend(tmp_path):
    """era1: var Foo = Widget.extend({}) → JSPatchInfo era=extend."""
    _make_static_js(tmp_path, "widget.js", "var Foo = Widget.extend({ start: function() {} });")
    result = parse_module_graph(_module(tmp_path))
    assert len(result.patches) == 1
    p = result.patches[0]
    assert p.era == "extend"
    assert p.target == "Widget"
    assert p.patch_name == "Foo"


def test_parse_module_graph_era2_define_only_no_include(tmp_path):
    """era2: odoo.define without .include → empty patches."""
    _make_static_js(
        tmp_path, "mod.js",
        "odoo.define('mymod.Foo', function(require) { return {}; });"
    )
    result = parse_module_graph(_module(tmp_path))
    assert result.patches == []


def test_parse_module_graph_era2_include(tmp_path):
    """era2: Foo.include({}) → JSPatchInfo era=include, target=Foo."""
    src = (
        "odoo.define('mymod.ext', function(require) {"
        " var Foo = require('web.Foo');"
        " Foo.include({ start: function() {} }); });"
    )
    _make_static_js(tmp_path, "inc.js", src)
    result = parse_module_graph(_module(tmp_path))
    assert len(result.patches) == 1
    p = result.patches[0]
    assert p.era == "include"
    assert p.target == "Foo"


def test_parse_module_graph_era3_patch(tmp_path):
    """era3: patch(MyComp.prototype, 'p', {}) → JSPatchInfo era=patch."""
    src = (
        "/** @odoo-module */\n"
        'import { patch } from "@web/core/utils/patch";\n'
        'patch(MyComp.prototype, "my_patch", { setup() {} });'
    )
    _make_static_js(tmp_path, "patch.js", src)
    result = parse_module_graph(_module(tmp_path))
    assert len(result.patches) == 1
    p = result.patches[0]
    assert p.era == "patch"
    assert p.target == "MyComp"
    assert p.patch_name == "my_patch"


def test_parse_module_graph_era3_class_component(tmp_path):
    """era3: class FormView extends Component { static template = 'x.y' } → OWLCompInfo."""
    _make_static_js(
        tmp_path, "form_view.js",
        '/** @odoo-module */\nclass FormView extends Component {\n    static template = "x.y";\n}\n'
    )
    result = parse_module_graph(_module(tmp_path))
    assert len(result.components) == 1
    c = result.components[0]
    assert c.name == "FormView"
    assert c.extends == "Component"
    assert c.template == "x.y"


def test_parse_module_graph_skip_lib_dir(tmp_path):
    """Files inside static/src/lib/ must be skipped."""
    _make_static_js(tmp_path, "jquery.min.js", "var $ = {};", subdir="lib")
    # Also valid file outside lib/
    _make_static_js(tmp_path, "app.js", "var App = Widget.extend({});")
    result = parse_module_graph(_module(tmp_path))
    paths = {p.file_path for p in result.patches}
    assert not any("jquery.min.js" in fp for fp in paths), "lib/ file must be excluded"
