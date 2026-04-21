"""Acceptance tests: parse real Odoo CE product models."""

from __future__ import annotations

import pathlib

import pytest

from osm.indexer.python_parser import parse_file

_PRODUCT_DIR = pathlib.Path("/home/soncrits/git/17.0/odoo/addons/product/models")
_PRODUCT_PRODUCT = _PRODUCT_DIR / "product_product.py"
_PRODUCT_TEMPLATE = _PRODUCT_DIR / "product_template.py"


def _require_file(path: pathlib.Path) -> None:
    if not path.exists():
        pytest.skip(f"Odoo source not found: {path}")


class TestProductProduct:
    def setup_method(self) -> None:
        _require_file(_PRODUCT_PRODUCT)

    def test_at_least_one_model(self) -> None:
        result = parse_file(_PRODUCT_PRODUCT)
        assert len(result.models) >= 1

    def test_inherits_delegation(self) -> None:
        result = parse_file(_PRODUCT_PRODUCT)
        pp = next(
            (m for m in result.models if m.class_name == "ProductProduct"),
            None,
        )
        assert pp is not None, "ProductProduct class not found"
        assert pp.inherits.get("product.template") == "product_tmpl_id"

    def test_inherit_contains_mail_mixins(self) -> None:
        result = parse_file(_PRODUCT_PRODUCT)
        pp = next(m for m in result.models if m.class_name == "ProductProduct")
        assert "mail.thread" in pp.inherit
        assert "mail.activity.mixin" in pp.inherit

    def test_field_count_positive(self) -> None:
        result = parse_file(_PRODUCT_PRODUCT)
        pp_fields = [
            f for f in result.fields if f.model_class_name == "ProductProduct"
        ]
        assert len(pp_fields) > 0

    def test_no_crash(self) -> None:
        result = parse_file(_PRODUCT_PRODUCT)
        assert "error" not in result.notes


class TestProductTemplate:
    def setup_method(self) -> None:
        _require_file(_PRODUCT_TEMPLATE)

    def test_at_least_one_model(self) -> None:
        result = parse_file(_PRODUCT_TEMPLATE)
        assert len(result.models) >= 1

    def test_model_name(self) -> None:
        result = parse_file(_PRODUCT_TEMPLATE)
        tmpl = next(
            (m for m in result.models if m.name == "product.template"),
            None,
        )
        assert tmpl is not None

    def test_field_count_positive(self) -> None:
        result = parse_file(_PRODUCT_TEMPLATE)
        tmpl_fields = [
            f
            for f in result.fields
            if f.model_class_name == "ProductTemplate"
        ]
        assert len(tmpl_fields) > 0

    def test_no_crash(self) -> None:
        result = parse_file(_PRODUCT_TEMPLATE)
        assert "error" not in result.notes


class TestBothModels:
    def setup_method(self) -> None:
        _require_file(_PRODUCT_PRODUCT)
        _require_file(_PRODUCT_TEMPLATE)

    def test_two_models_combined(self) -> None:
        r1 = parse_file(_PRODUCT_PRODUCT)
        r2 = parse_file(_PRODUCT_TEMPLATE)
        total_models = r1.models + r2.models
        assert len(total_models) >= 2
