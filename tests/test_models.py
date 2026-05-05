# tests/test_models.py
from src.indexer.models import ModelInfo, ModuleInfo, ParseResult


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
