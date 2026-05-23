# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for JS parser — era detection and chunking."""

from src.indexer.models import JSGraphResult, ModuleInfo
from src.indexer.parser_js import (
    _detect_era,
    _extract_era3_components,
    _extract_era3_patches,
    parse_file,
    parse_module_graph,
)


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


# --- bound_model heuristic (I2) ---

def test_parse_module_graph_era3_bound_model_orm_call(tmp_path):
    """era3: class with this.orm.read('sale.order', ...) → OWLCompInfo.bound_model='sale.order'."""
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n\n"
        "class SaleOrderList extends Component {\n"
        "    async loadData() {\n"
        "        const records = await this.orm.read('sale.order', [1, 2], ['name']);\n"
        "        return records;\n"
        "    }\n"
        "}\n"
    )
    _make_static_js(tmp_path, "sale_order_list.js", src)
    result = parse_module_graph(_module(tmp_path))
    assert result.components, "Expected at least one OWLCompInfo"
    comp = next((c for c in result.components if c.name == "SaleOrderList"), None)
    assert comp is not None, "Expected OWLCompInfo named 'SaleOrderList'"
    assert comp.bound_model == "sale.order", (
        f"Expected bound_model='sale.order', got {comp.bound_model!r}"
    )


def test_parse_module_graph_era3_bound_model_resmodel_kwarg(tmp_path):
    """era3: class with resModel: 'sale.order' kwarg → OWLCompInfo.bound_model == 'sale.order'."""
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n\n"
        "class SaleActionButton extends Component {\n"
        "    doAction() {\n"
        "        this.action.doAction({ resModel: 'sale.order', type: 'ir.actions.act_window' });\n"
        "    }\n"
        "}\n"
    )
    _make_static_js(tmp_path, "sale_action_button.js", src)
    result = parse_module_graph(_module(tmp_path))
    assert result.components, "Expected at least one OWLCompInfo"
    comp = next((c for c in result.components if c.name == "SaleActionButton"), None)
    assert comp is not None, "Expected OWLCompInfo named 'SaleActionButton'"
    assert comp.bound_model == "sale.order", (
        f"Expected bound_model='sale.order', got {comp.bound_model!r}"
    )


def test_parse_module_graph_era3_bound_model_none_when_dynamic(tmp_path):
    """era3: class with dynamic this.orm.read(this.props.model, ...) → bound_model == None."""
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n\n"
        "class DynamicModelList extends Component {\n"
        "    async loadData() {\n"
        "        const records = await this.orm.read(this.props.model, [1], ['name']);\n"
        "        return records;\n"
        "    }\n"
        "}\n"
    )
    _make_static_js(tmp_path, "dynamic_model_list.js", src)
    result = parse_module_graph(_module(tmp_path))
    assert result.components, "Expected at least one OWLCompInfo"
    comp = next((c for c in result.components if c.name == "DynamicModelList"), None)
    assert comp is not None, "Expected OWLCompInfo named 'DynamicModelList'"
    assert comp.bound_model is None, (
        f"Dynamic model reference should not be resolved, got bound_model={comp.bound_model!r}"
    )


# --- OWL era guard (v9 / pre-v14 protection) ---

def test_owl_component_skipped_for_v9(tmp_path):
    """_extract_era3_components must produce no OWLCompInfo for v9 modules.

    OWL framework only exists from v14+. Pre-v14 JS files using class syntax
    (rare but possible) must not generate OWLComp nodes.

    This is a pure unit test — does not require Neo4j.
    Moved from test_writer_neo4j_stub_profile.py so that file can carry a
    clean module-level pytestmark = pytest.mark.neo4j (CLAUDE.md convention).
    """
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser

    JS_LANGUAGE = Language(tsjs.language())
    parser = Parser(JS_LANGUAGE)

    source_code = b"""
/** @odoo-module **/
class MyComponent extends Component {
    static template = "my_module.MyComponent";
}
"""
    tree = parser.parse(source_code)

    v9_module = ModuleInfo(
        name="some_v9_module", odoo_version="9.0",
        repo="v9_repo", path=str(tmp_path), depends=[], version_raw="",
    )
    result = JSGraphResult(module=v9_module)

    _extract_era3_components(tree, source_code, v9_module, str(tmp_path / "test.js"), result)

    assert result.components == [], (
        "_extract_era3_components must return early for v9 — OWL does not exist pre-v14"
    )


# --- era3 patch() guard: OWL patch() only exists v14+ ---

def test_era3_patches_skipped_for_v13(tmp_path):
    """_extract_era3_patches must produce no JSPatchInfo for v13 modules.

    OWL patch() only exists from v14+. A v13 JS file using era3 syntax
    must not generate JSPatch nodes — they would be anachronistic stubs.

    This is a pure unit test — does not require Neo4j.
    """
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser

    JS_LANGUAGE = Language(tsjs.language())
    parser = Parser(JS_LANGUAGE)

    source_code = b"""
/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
patch(MyComp.prototype, "my_patch", { setup() {} });
"""
    tree = parser.parse(source_code)

    v13_module = ModuleInfo(
        name="some_v13_module", odoo_version="13.0",
        repo="v13_repo", path=str(tmp_path), depends=[], version_raw="",
    )
    result = JSGraphResult(module=v13_module)

    _extract_era3_patches(tree, source_code, v13_module, str(tmp_path / "test.js"), result)

    assert result.patches == [], (
        "_extract_era3_patches must return early for v13 — OWL patch() does not exist pre-v14"
    )


def test_era3_patches_skipped_for_v8(tmp_path):
    """_extract_era3_patches must produce no JSPatchInfo for v8 modules."""
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser

    JS_LANGUAGE = Language(tsjs.language())
    parser = Parser(JS_LANGUAGE)

    source_code = b"""
/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
patch(SomeWidget.prototype, "some_patch", {});
"""
    tree = parser.parse(source_code)

    v8_module = ModuleInfo(
        name="some_v8_module", odoo_version="8.0",
        repo="v8_repo", path=str(tmp_path), depends=[], version_raw="",
    )
    result = JSGraphResult(module=v8_module)

    _extract_era3_patches(tree, source_code, v8_module, str(tmp_path / "test.js"), result)

    assert result.patches == [], (
        "_extract_era3_patches must return early for v8 — OWL patch() does not exist pre-v14"
    )


def test_era3_patches_extracted_for_v14(tmp_path):
    """Regression: _extract_era3_patches must still extract patches for v14+.

    OWL patch() was introduced in v14 — guard must allow v14 and above.
    """
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser

    JS_LANGUAGE = Language(tsjs.language())
    parser = Parser(JS_LANGUAGE)

    source_code = b"""
/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
patch(MyComp.prototype, "my_patch", { setup() {} });
"""
    tree = parser.parse(source_code)

    v14_module = ModuleInfo(
        name="some_v14_module", odoo_version="14.0",
        repo="v14_repo", path=str(tmp_path), depends=[], version_raw="",
    )
    result = JSGraphResult(module=v14_module)

    _extract_era3_patches(tree, source_code, v14_module, str(tmp_path / "test.js"), result)

    assert len(result.patches) == 1, "v14 must produce JSPatchInfo nodes"
    assert result.patches[0].era == "patch"
    assert result.patches[0].target == "MyComp"
    assert result.patches[0].patch_name == "my_patch"


def test_era3_patches_extracted_for_v17(tmp_path):
    """Regression: _extract_era3_patches must still extract patches for v17."""
    _make_static_js(
        tmp_path, "patch17.js",
        '/** @odoo-module */\n'
        'import { patch } from "@web/core/utils/patch";\n'
        'patch(FormController.prototype, "form_ctrl_patch", { setup() {} });',
    )
    result = parse_module_graph(_module(tmp_path, version="17.0"))

    assert len(result.patches) == 1, "v17 must produce JSPatchInfo nodes"
    p = result.patches[0]
    assert p.era == "patch"
    assert p.target == "FormController"
    assert p.patch_name == "form_ctrl_patch"


# ---------------------------------------------------------------------------
# T1 — JS-G1: era2 files in v14+ produce OWLComp nodes (dual-dispatch)
# ---------------------------------------------------------------------------

def test_era2_owl_component_dual_dispatch_v14(tmp_path):
    """T1: era2 file with `class Foo extends Component` in v14 → OWLCompInfo.

    The file is classified era2 (odoo.define present, @odoo-module absent).
    The dual-dispatch in _extract_graph_from_file must also call
    _extract_era3_components, producing an OWLCompInfo node.
    """
    src = (
        "odoo.define('web.Popover', function(require) {\n"
        "    const { Component, hooks } = owl;\n"
        "    const patchMixin = require('web.patchMixin');\n"
        "    class Popover extends Component {\n"
        "        setup() {}\n"
        "    }\n"
        "    return patchMixin(Popover);\n"
        "});\n"
    )
    _make_static_js(tmp_path, "popover.js", src)
    result = parse_module_graph(_module(tmp_path, version="14.0"))
    comp = next((c for c in result.components if c.name == "Popover"), None)
    assert comp is not None, (
        "era2 file with class Popover extends Component (v14) must produce OWLCompInfo"
    )
    assert comp.extends == "Component"


def test_era2_owl_component_not_produced_for_v13(tmp_path):
    """T1 guard: era2 file with OWL component in v13 must NOT produce OWLCompInfo.

    OWL did not exist before v14. The dual-dispatch guard (_OWL_ENABLED_REGISTRY)
    must prevent extraction for v13 even if the file has `extends Component`.
    """
    src = (
        "odoo.define('old.Widget', function(require) {\n"
        "    class OldComp extends Component {}\n"
        "    return OldComp;\n"
        "});\n"
    )
    _make_static_js(tmp_path, "old_widget.js", src)
    result = parse_module_graph(_module(tmp_path, version="13.0"))
    assert result.components == [], (
        "era2 file with class extends Component in v13 must NOT produce OWLCompInfo"
    )


def test_era2_owl_no_double_count_era3_file(tmp_path):
    """T1 anti-regression: era3 file is NOT processed by era2 extractor (no double-count).

    era3 files go through _extract_era3_patches + _extract_era3_components only.
    The dual-dispatch code only runs for era2. A component in an era3 file must
    appear exactly once.
    """
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n"
        "export class FormView extends Component {\n"
        "    static template = 'web.FormView';\n"
        "}\n"
    )
    _make_static_js(tmp_path, "form_view.js", src)
    result = parse_module_graph(_module(tmp_path, version="16.0"))
    form_view_comps = [c for c in result.components if c.name == "FormView"]
    assert len(form_view_comps) == 1, (
        f"era3 file must produce exactly 1 OWLCompInfo for FormView, got {len(form_view_comps)}"
    )


# ---------------------------------------------------------------------------
# T2 — JS-G2: member-expr MyClass.patch("key", fn) in era2 files (v14+)
# ---------------------------------------------------------------------------

def test_era2_member_patch_v14_produces_jspatch(tmp_path):
    """T2: MyClass.patch('key', T => class extends T {...}) in era2 file v14 → JSPatchInfo.

    v14 OWL patchMixin pattern: after wrapping a class with patchMixin(), the class
    gains a .patch(name, fn) method. This is a member_expression call, not the era2
    .include() nor the era3 bare patch().
    """
    src = (
        "odoo.define('web.patch_test', function(require) {\n"
        "    const Popover = require('web.Popover');\n"
        "    Popover.patch('web.PopoverMixin', T => class extends T {\n"
        "        setup() { this._super(); }\n"
        "    });\n"
        "});\n"
    )
    _make_static_js(tmp_path, "patch_test.js", src)
    result = parse_module_graph(_module(tmp_path, version="14.0"))
    patch = next((p for p in result.patches if p.target == "Popover"), None)
    assert patch is not None, (
        "Popover.patch('key', fn) in era2 file v14 must produce JSPatchInfo"
    )
    assert patch.era == "patch"
    assert patch.patch_name == "web.PopoverMixin"


def test_era2_member_patch_not_produced_for_v13(tmp_path):
    """T2 guard: MyClass.patch() in era2 file v13 must NOT produce JSPatchInfo.

    OWL patchMixin only existed in v14+. Guard (_OWL_ENABLED_REGISTRY) must prevent
    this pattern from matching in v13.
    """
    src = (
        "odoo.define('old.patch', function(require) {\n"
        "    var Foo = require('web.Foo');\n"
        "    Foo.patch('old.key', function() {});\n"
        "});\n"
    )
    _make_static_js(tmp_path, "old_patch.js", src)
    result = parse_module_graph(_module(tmp_path, version="13.0"))
    member_patches = [p for p in result.patches if p.era == "patch"]
    assert member_patches == [], (
        "MyClass.patch() in era2 file v13 must NOT produce era=patch JSPatchInfo"
    )


def test_era2_include_still_works_alongside_member_patch(tmp_path):
    """T2 regression: .include() still produces era=include JSPatchInfo after T2 changes."""
    src = (
        "odoo.define('mymod.ext', function(require) {\n"
        "    var Foo = require('web.Foo');\n"
        "    Foo.include({ start: function() {} });\n"
        "});\n"
    )
    _make_static_js(tmp_path, "inc.js", src)
    result = parse_module_graph(_module(tmp_path, version="14.0"))
    include_patches = [p for p in result.patches if p.era == "include"]
    assert len(include_patches) == 1, (
        f"Foo.include() must still produce era=include JSPatchInfo, got {include_patches}"
    )
    assert include_patches[0].target == "Foo"


# ---------------------------------------------------------------------------
# T3 — V16-G1: OWLComp filter — non-Component classes must NOT become OWLComp
# ---------------------------------------------------------------------------

def test_era3_only_component_subclass_produces_owlcomp(tmp_path):
    """T3: era3 file with Component subclass + plain class → only Component subclass indexed.

    `class Domain {}` and `class Registry {}` are utility classes in era3 files.
    They must NOT produce OWLCompInfo. Only `class Foo extends Component` qualifies.
    """
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n"
        "class Foo extends Component {\n"
        "    static template = 'x.Foo';\n"
        "}\n"
        "class Domain {}\n"
        "class Registry {}\n"
        "class RPCError {}\n"
    )
    _make_static_js(tmp_path, "mixed.js", src)
    result = parse_module_graph(_module(tmp_path, version="16.0"))
    component_names = {c.name for c in result.components}
    assert "Foo" in component_names, "Foo extends Component must be indexed as OWLComp"
    assert "Domain" not in component_names, "Domain (plain class) must NOT be OWLComp"
    assert "Registry" not in component_names, "Registry (plain class) must NOT be OWLComp"
    assert "RPCError" not in component_names, "RPCError (plain class) must NOT be OWLComp"


def test_era3_non_component_class_excluded_v17(tmp_path):
    """T3 regression: non-OWL classes in era3 v17 file are excluded from OWLComp."""
    src = (
        "/** @odoo-module */\n"
        "import { Component } from '@odoo/owl';\n"
        "export class SaleWidget extends Component {}\n"
        "export class HelperUtil {}\n"
        "export class DataModel {}\n"
    )
    _make_static_js(tmp_path, "sale_widget.js", src)
    result = parse_module_graph(_module(tmp_path, version="17.0"))
    component_names = {c.name for c in result.components}
    assert "SaleWidget" in component_names
    assert "HelperUtil" not in component_names
    assert "DataModel" not in component_names
