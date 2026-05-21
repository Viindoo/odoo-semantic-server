# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_models.py
from src.indexer.models import (
    JSGraphResult,
    JSPatchInfo,
    ModelInfo,
    ModuleInfo,
    OWLCompInfo,
    ParseResult,
)


def test_module_info_creation():
    m = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/git/odoo_17.0/sale", depends=["base", "account"],
        version_raw="17.0.1.0.0",
    )
    assert m.name == "sale"
    assert m.odoo_version == "17.0"
    assert "base" in m.depends


def test_model_info_defaults():
    model = ModelInfo(name="sale.order", module="sale", odoo_version="17.0")
    assert model.is_abstract is False
    assert model.is_transient is False
    assert model.inherit == []
    assert model.inherits == {}
    assert model.fields == []
    assert model.methods == []


def test_parse_result_creation():
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/tmp", depends=[], version_raw="",
    )
    result = ParseResult(module=module)
    assert result.models == []


def test_xpath_info_creation():
    from src.indexer.models import XPathInfo
    x = XPathInfo(expr="//field[@name='partner_id']", position="after")
    assert x.expr == "//field[@name='partner_id']"
    assert x.position == "after"


def test_view_info_primary_defaults():
    from src.indexer.models import ViewInfo
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
    from src.indexer.models import ViewInfo, XPathInfo
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
    from src.indexer.models import QWebInfo
    q = QWebInfo(
        xmlid="sale.sale_order_portal",
        module="sale",
        odoo_version="17.0",
    )
    assert q.inherit_xmlid is None


def test_qweb_info_with_inherit():
    from src.indexer.models import QWebInfo
    q = QWebInfo(
        xmlid="viin_sale.sale_order_portal_inherit",
        module="viin_sale",
        odoo_version="17.0",
        inherit_xmlid="sale.sale_order_portal",
    )
    assert q.inherit_xmlid == "sale.sale_order_portal"


def test_view_parse_result_defaults():
    from src.indexer.models import ViewParseResult
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/tmp", depends=[], version_raw="",
    )
    result = ViewParseResult(module=module)
    assert result.views == []
    assert result.qweb == []


def test_js_patch_info_creation():
    """Test JSPatchInfo instantiation with all required fields."""
    patch = JSPatchInfo(
        target="MyWidget",
        patch_name="MyPatch",
        module="viin_sale",
        odoo_version="17.0",
        era="extend",
        file_path="/path/to/file.js",
    )
    assert patch.target == "MyWidget"
    assert patch.patch_name == "MyPatch"
    assert patch.module == "viin_sale"
    assert patch.odoo_version == "17.0"
    assert patch.era == "extend"
    assert patch.file_path == "/path/to/file.js"


def test_owl_comp_info_defaults():
    """Test OWLCompInfo with required fields and default optionals."""
    comp = OWLCompInfo(
        name="FormView",
        module="sale",
        odoo_version="17.0",
    )
    assert comp.name == "FormView"
    assert comp.module == "sale"
    assert comp.odoo_version == "17.0"
    assert comp.template is None
    assert comp.extends is None
    assert comp.bound_model is None
    assert comp.file_path == ""


def test_js_graph_result_empty_lists():
    """Test JSGraphResult with empty patches and components defaults."""
    module = ModuleInfo(
        name="viin_sale", odoo_version="17.0", repo="odoo_17.0",
        path="/tmp", depends=[], version_raw="",
    )
    result = JSGraphResult(module=module)
    assert result.module == module
    assert result.patches == []
    assert result.components == []


def test_pattern_example_dataclass_defaults():
    """PatternExample (M4.6 WI3) — required fields + core_symbol_names default empty."""
    from src.indexer.models import PatternExample
    pe = PatternExample(
        pattern_id="computed-field-cross-model",
        intent_keywords=["computed", "depends", "cross-model"],
        file_ref="addons/sale/models/sale_order.py:245",
        snippet_text="@api.depends(...)\ndef _compute(self): ...",
        gotchas=["Missing Many2one root in path"],
        odoo_version_min="17.0",
        language="python",
    )
    assert pe.pattern_id == "computed-field-cross-model"
    assert pe.core_symbol_names == []
    assert pe.language == "python"


def test_core_symbol_info_defaults():
    """CoreSymbolInfo (M4.5 WI2.1) — required fields + defaults."""
    from src.indexer.models import CoreSymbolInfo
    cs = CoreSymbolInfo(
        qualified_name="odoo.tools.safe_eval",
        kind="function",
        odoo_version="18.0",
    )
    assert cs.qualified_name == "odoo.tools.safe_eval"
    assert cs.kind == "function"
    assert cs.odoo_version == "18.0"
    assert cs.signature is None
    assert cs.file_path is None
    assert cs.line is None
    assert cs.status == "stable"
    assert cs.replacement_qname is None
