"""Unit tests for osm.indexer.python_parser against synthetic fixture files."""

from __future__ import annotations

import pathlib

import pytest

from osm.indexer.python_parser import FileParseResult, parse_file, scan_models_package

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "python_parser"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(name: str) -> FileParseResult:
    return parse_file(FIXTURES / name)


# ---------------------------------------------------------------------------
# pure_inherit.py
# ---------------------------------------------------------------------------


class TestPureInherit:
    def test_model_count(self) -> None:
        result = _parse("pure_inherit.py")
        assert len(result.models) == 1

    def test_model_name_is_none(self) -> None:
        m = _parse("pure_inherit.py").models[0]
        assert m.name is None

    def test_inherit_tuple(self) -> None:
        m = _parse("pure_inherit.py").models[0]
        assert m.inherit == ("sale.order",)

    def test_field_count(self) -> None:
        result = _parse("pure_inherit.py")
        assert len(result.fields) == 1
        assert result.fields[0].field_name == "custom_note"
        assert result.fields[0].field_type == "Char"
        assert result.fields[0].required is True

    def test_method_count(self) -> None:
        result = _parse("pure_inherit.py")
        assert len(result.methods) == 1
        assert result.methods[0].method_name == "action_custom"
        assert result.methods[0].calls_super is True

    def test_line_ranges_sane(self) -> None:
        m = _parse("pure_inherit.py").models[0]
        assert 0 < m.start_line <= m.end_line

    def test_content_hash_stable(self) -> None:
        r1 = _parse("pure_inherit.py")
        r2 = _parse("pure_inherit.py")
        assert r1.models[0].content_hash == r2.models[0].content_hash
        assert r1.fields[0].content_hash == r2.fields[0].content_hash
        assert r1.methods[0].content_hash == r2.methods[0].content_hash


# ---------------------------------------------------------------------------
# multi_inherit.py
# ---------------------------------------------------------------------------


class TestMultiInherit:
    def test_inherit_list(self) -> None:
        m = _parse("multi_inherit.py").models[0]
        assert "mail.thread" in m.inherit
        assert "mail.activity.mixin" in m.inherit
        assert len(m.inherit) == 2

    def test_model_name(self) -> None:
        m = _parse("multi_inherit.py").models[0]
        assert m.name == "product.template"

    def test_no_dynamic_inherit_flag(self) -> None:
        m = _parse("multi_inherit.py").models[0]
        assert not m.indexer_notes.get("dynamic_inherit")


# ---------------------------------------------------------------------------
# inherits_delegation.py
# ---------------------------------------------------------------------------


class TestInheritsDelegation:
    def test_inherits_map(self) -> None:
        m = _parse("inherits_delegation.py").models[0]
        assert m.inherits == {"product.template": "product_tmpl_id"}

    def test_inherit_and_inherits_coexist(self) -> None:
        m = _parse("inherits_delegation.py").models[0]
        assert "mail.thread" in m.inherit
        assert "product.template" in m.inherits

    def test_field_extraction(self) -> None:
        result = _parse("inherits_delegation.py")
        field_names = {f.field_name for f in result.fields}
        assert "default_code" in field_names

    def test_many2one_field(self) -> None:
        result = _parse("inherits_delegation.py")
        fk = next(f for f in result.fields if f.field_name == "product_tmpl_id")
        assert fk.field_type == "Many2one"
        assert fk.comodel_name == "product.template"


# ---------------------------------------------------------------------------
# conditional_import
# ---------------------------------------------------------------------------


class TestConditionalImport:
    def test_scan_returns_optional_mod(self) -> None:
        init = FIXTURES / "conditional_import" / "__init__.py"
        conditional = scan_models_package(init)
        assert "optional_mod" in conditional
        assert "base_mod" not in conditional

    def test_optional_mod_flagged(self) -> None:
        init = FIXTURES / "conditional_import" / "__init__.py"
        conditional = scan_models_package(init)
        result = parse_file(
            FIXTURES / "conditional_import" / "optional_mod.py",
            conditional_submodules=conditional,
        )
        assert len(result.models) == 1
        assert result.models[0].indexer_notes.get("conditional_import") is True

    def test_base_mod_not_flagged(self) -> None:
        init = FIXTURES / "conditional_import" / "__init__.py"
        conditional = scan_models_package(init)
        result = parse_file(
            FIXTURES / "conditional_import" / "base_mod.py",
            conditional_submodules=conditional,
        )
        assert len(result.models) == 1
        assert not result.models[0].indexer_notes.get("conditional_import")


# ---------------------------------------------------------------------------
# register_false.py
# ---------------------------------------------------------------------------


class TestRegisterFalse:
    def test_register_false_flag(self) -> None:
        m = _parse("register_false.py").models[0]
        assert m.register_false is True
        assert m.indexer_notes.get("register_false_chain") is True

    def test_abstract_model(self) -> None:
        m = _parse("register_false.py").models[0]
        assert m.abstract is True


# ---------------------------------------------------------------------------
# nested_classes.py
# ---------------------------------------------------------------------------


class TestNestedClasses:
    def test_only_one_model_emitted(self) -> None:
        result = _parse("nested_classes.py")
        assert len(result.models) == 1
        assert result.models[0].class_name == "SaleOrder"

    def test_inner_class_not_in_models(self) -> None:
        result = _parse("nested_classes.py")
        class_names = [m.class_name for m in result.models]
        assert "_StateHelper" not in class_names

    def test_method_from_outer_class(self) -> None:
        result = _parse("nested_classes.py")
        assert any(m.method_name == "action_confirm" for m in result.methods)


# ---------------------------------------------------------------------------
# depends_decorator.py
# ---------------------------------------------------------------------------


class TestDependsDecorator:
    def test_depends_linked_to_field(self) -> None:
        result = _parse("depends_decorator.py")
        f = next(f for f in result.fields if f.field_name == "price_subtotal")
        assert "price_unit" in f.depends
        assert "qty" in f.depends

    def test_compute_method_has_decorator(self) -> None:
        result = _parse("depends_decorator.py")
        m = next(m for m in result.methods if m.method_name == "_compute_price_subtotal")
        assert any("api.depends" in d for d in m.decorators)

    def test_non_compute_fields_have_empty_depends(self) -> None:
        result = _parse("depends_decorator.py")
        f = next(f for f in result.fields if f.field_name == "price_unit")
        assert f.depends == ()


# ---------------------------------------------------------------------------
# super_call.py
# ---------------------------------------------------------------------------


class TestSuperCall:
    def test_action_confirm_calls_super(self) -> None:
        result = _parse("super_call.py")
        m = next(m for m in result.methods if m.method_name == "action_confirm")
        assert m.calls_super is True

    def test_action_cancel_no_super(self) -> None:
        result = _parse("super_call.py")
        m = next(m for m in result.methods if m.method_name == "action_cancel")
        assert m.calls_super is False

    def test_line_ranges(self) -> None:
        result = _parse("super_call.py")
        for m in result.methods:
            assert m.start_line > 0
            assert m.start_line <= m.end_line


# ---------------------------------------------------------------------------
# dynamic_inherit.py
# ---------------------------------------------------------------------------


class TestDynamicInherit:
    def test_dynamic_inherit_flag(self) -> None:
        m = _parse("dynamic_inherit.py").models[0]
        assert m.indexer_notes.get("dynamic_inherit") is True

    def test_inherit_is_empty_when_dynamic(self) -> None:
        m = _parse("dynamic_inherit.py").models[0]
        assert m.inherit == ()

    def test_model_name_still_extracted(self) -> None:
        m = _parse("dynamic_inherit.py").models[0]
        assert m.name == "dynamic.model"


# ---------------------------------------------------------------------------
# broken_syntax.py
# ---------------------------------------------------------------------------


class TestBrokenSyntax:
    def test_returns_empty_no_crash(self) -> None:
        result = _parse("broken_syntax.py")
        assert result.models == []
        assert result.fields == []
        assert result.methods == []

    def test_error_in_notes(self) -> None:
        result = _parse("broken_syntax.py")
        assert "error" in result.notes


# ---------------------------------------------------------------------------
# Cross-cutting: content_hash regression (parse twice, compare)
# ---------------------------------------------------------------------------


class TestContentHashRegression:
    @pytest.mark.parametrize(
        "fixture",
        [
            "pure_inherit.py",
            "multi_inherit.py",
            "inherits_delegation.py",
            "register_false.py",
            "nested_classes.py",
            "depends_decorator.py",
            "super_call.py",
            "dynamic_inherit.py",
        ],
    )
    def test_hash_stable_across_parses(self, fixture: str) -> None:
        r1 = _parse(fixture)
        r2 = _parse(fixture)
        for m1, m2 in zip(r1.models, r2.models, strict=True):
            assert m1.content_hash == m2.content_hash
        for f1, f2 in zip(r1.fields, r2.fields, strict=True):
            assert f1.content_hash == f2.content_hash
        for m1, m2 in zip(r1.methods, r2.methods, strict=True):
            assert m1.content_hash == m2.content_hash
