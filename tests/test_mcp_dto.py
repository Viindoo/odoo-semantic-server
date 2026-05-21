# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/mcp/dto.py — Pydantic v2 DTOs (M10.5 WI-B2).

Each test covers one *Output type:
  (a) Instantiate from a dict literal with realistic fixture data.
  (b) Assert model_json_schema() produces a valid JSON Schema with the
      ``next_step_hint`` field in ``properties`` and ``required``.

No database required — these are pure Pydantic model tests.
Runtime: <2s.
"""

import pytest

from src.mcp.dto import (
    CoreSymbolRef,
    DescribeModuleOutput,
    FieldRef,
    ListFieldsOutput,
    ListMethodsOutput,
    MethodRef,
    ModelRef,
    ModuleRef,
    PatternRef,
    ResolveFieldOutput,
    ResolveMethodOutput,
    ResolveModelOutput,
    ResolveViewOutput,
    ViewRef,
)

# ---------------------------------------------------------------------------
# Helper — validate JSON Schema shape (AC-B2-3)
# ---------------------------------------------------------------------------

def _assert_json_schema_valid(output_class) -> dict:
    """Return schema and assert it has required JSON Schema top-level keys."""
    schema = output_class.model_json_schema()
    assert isinstance(schema, dict), f"{output_class.__name__}.model_json_schema() must return dict"
    for key in ("title", "type", "properties"):
        assert key in schema, (
            f"{output_class.__name__} schema missing '{key}' key; got: {list(schema)}"
        )
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "next_step_hint" in props, (
        f"{output_class.__name__} schema missing 'next_step_hint' in properties"
    )
    required = schema.get("required", [])
    assert "next_step_hint" in required, (
        f"{output_class.__name__} schema 'next_step_hint' must be required field"
    )
    return schema


# ---------------------------------------------------------------------------
# *Ref types — smoke tests (7 classes)
# ---------------------------------------------------------------------------

class TestRefTypes:
    def test_model_ref(self):
        ref = ModelRef(name="sale.order", module="sale", odoo_version="17.0")
        assert ref.name == "sale.order"
        assert ref.module == "sale"
        assert ref.odoo_version == "17.0"

    def test_field_ref(self):
        ref = FieldRef(
            model="sale.order", name="amount_total", module="sale", odoo_version="17.0"
        )
        assert ref.model == "sale.order"
        assert ref.name == "amount_total"

    def test_method_ref(self):
        ref = MethodRef(
            model="sale.order", name="action_confirm", module="sale", odoo_version="17.0"
        )
        assert ref.name == "action_confirm"

    def test_view_ref_with_model(self):
        ref = ViewRef(xmlid="sale.view_order_form", model="sale.order", odoo_version="17.0")
        assert ref.model == "sale.order"

    def test_view_ref_qweb_no_model(self):
        ref = ViewRef(xmlid="point_of_sale.OrderWidget", model=None, odoo_version="17.0")
        assert ref.model is None

    def test_module_ref_with_profile(self):
        ref = ModuleRef(name="sale", odoo_version="17.0", profile=["community", "viindoo"])
        assert ref.profile == ["community", "viindoo"]

    def test_module_ref_no_profile(self):
        ref = ModuleRef(name="sale", odoo_version="17.0")
        assert ref.profile is None

    def test_pattern_ref(self):
        ref = PatternRef(pattern_id="compute-stored-field", odoo_version_range="v14-v17")
        assert ref.odoo_version_range == "v14-v17"

    def test_core_symbol_ref(self):
        ref = CoreSymbolRef(symbol="odoo.models.BaseModel", kind="class", odoo_version="17.0")
        assert ref.kind == "class"

    def test_ref_extra_field_forbidden(self):
        with pytest.raises(Exception):
            ModelRef(name="x", module="y", odoo_version="17.0", extra_field="bad")


# ---------------------------------------------------------------------------
# *Output types — 7 tests (one per Output class) covering AC-B2-4
# ---------------------------------------------------------------------------

class TestResolveModelOutput:
    """AC-B2-4 test 1/7."""

    def test_instantiate_and_schema(self):
        instance = ResolveModelOutput(
            ref=ModelRef(name="sale.order", module="sale", odoo_version="17.0"),
            is_definition=True,
            defined_in=ModuleRef(name="sale", odoo_version="17.0"),
            extended_by=[
                ModuleRef(name="sale_management", odoo_version="17.0"),
            ],
            inherits_from=["mail.thread", "mail.activity.mixin"],
            field_count=42,
            method_count=18,
            next_step_hint=(
                "└─ Next: list_fields(model='sale.order', odoo_version='17.0')"
                " for full field list | list_methods(model='sale.order',"
                " odoo_version='17.0') for behavior"
            ),
        )
        assert instance.ref.name == "sale.order"
        assert instance.is_definition is True
        assert instance.field_count == 42
        assert "list_fields" in instance.next_step_hint

        schema = _assert_json_schema_valid(ResolveModelOutput)
        assert "ref" in schema["properties"]
        assert "field_count" in schema["properties"]


class TestResolveFieldOutput:
    """AC-B2-4 test 2/7."""

    def test_instantiate_and_schema(self):
        instance = ResolveFieldOutput(
            ref=FieldRef(
                model="sale.order", name="amount_total",
                module="sale", odoo_version="17.0",
            ),
            ttype="monetary",
            computed=True,
            compute_method="_compute_amount",
            stored=True,
            required=False,
            related=None,
            declared_in=[
                FieldRef(
                    model="sale.order", name="amount_total",
                    module="sale", odoo_version="17.0",
                ),
            ],
            next_step_hint=(
                "└─ Next: find_examples(query='amount_total usage', odoo_version='17.0')"
                " for real-world patterns"
            ),
        )
        assert instance.ttype == "monetary"
        assert instance.computed is True
        assert instance.compute_method == "_compute_amount"
        assert len(instance.declared_in) == 1

        _assert_json_schema_valid(ResolveFieldOutput)


class TestResolveMethodOutput:
    """AC-B2-4 test 3/7."""

    def test_instantiate_and_schema(self):
        instance = ResolveMethodOutput(
            ref=MethodRef(
                model="sale.order", name="action_confirm",
                module="sale", odoo_version="17.0",
            ),
            override_chain=[
                MethodRef(
                    model="sale.order", name="action_confirm",
                    module="sale", odoo_version="17.0",
                ),
                MethodRef(
                    model="sale.order", name="action_confirm",
                    module="sale_ext", odoo_version="17.0",
                ),
            ],
            next_step_hint=(
                "└─ Next: find_override_point(model='sale.order', method='action_confirm',"
                " odoo_version='17.0') for safe extension spot"
            ),
        )
        assert instance.ref.name == "action_confirm"
        assert len(instance.override_chain) == 2
        assert "find_override_point" in instance.next_step_hint

        _assert_json_schema_valid(ResolveMethodOutput)


class TestResolveViewOutput:
    """AC-B2-4 test 4/7."""

    def test_instantiate_and_schema(self):
        instance = ResolveViewOutput(
            ref=ViewRef(xmlid="sale.view_order_form", model="sale.order", odoo_version="17.0"),
            view_type="form",
            module="sale",
            mode=None,
            inherits_from=None,
            xpath_count=0,
            extended_by=[
                ViewRef(
                    xmlid="sale_ext.view_order_form_ext",
                    model="sale.order", odoo_version="17.0",
                ),
            ],
            next_step_hint=(
                "└─ Next: list_views(model='sale.order', odoo_version='17.0') for sibling views"
                " | find_examples(query='sale.view_order_form xpath', odoo_version='17.0')"
                " for inheritance patterns"
            ),
        )
        assert instance.view_type == "form"
        assert instance.xpath_count == 0
        assert len(instance.extended_by) == 1

        _assert_json_schema_valid(ResolveViewOutput)

    def test_extension_view(self):
        instance = ResolveViewOutput(
            ref=ViewRef(
                xmlid="sale_ext.view_order_form_ext",
                model="sale.order", odoo_version="17.0",
            ),
            view_type="form",
            module="sale_ext",
            mode="extension",
            inherits_from="sale.view_order_form",
            xpath_count=3,
            extended_by=[],
            next_step_hint=(
                "└─ Next: list_views(model='sale.order', odoo_version='17.0')"
                " for sibling views"
            ),
        )
        assert instance.mode == "extension"
        assert instance.inherits_from == "sale.view_order_form"
        assert instance.xpath_count == 3


class TestDescribeModuleOutput:
    """AC-B2-4 test 5/7."""

    def test_instantiate_and_schema(self):
        instance = DescribeModuleOutput(
            ref=ModuleRef(name="sale", odoo_version="17.0"),
            edition="community",
            version_raw="17.0.1.0.0",
            depends=["base", "mail", "account"],
            defines_models=["sale.order", "sale.order.line"],
            extends_models=["res.partner"],
            view_total=15,
            js_patch_count=2,
            next_step_hint=(
                "└─ Next: list_fields(model='sale.order', module='sale',"
                " odoo_version='17.0') for declared fields"
            ),
        )
        assert instance.edition == "community"
        assert instance.version_raw == "17.0.1.0.0"
        assert len(instance.defines_models) == 2
        assert instance.view_total == 15

        schema = _assert_json_schema_valid(DescribeModuleOutput)
        assert "edition" in schema["properties"]
        assert "defines_models" in schema["properties"]


class TestListFieldsOutput:
    """AC-B2-4 test 6/7."""

    def test_instantiate_and_schema(self):
        fields = [
            FieldRef(model="sale.order", name=f"field_{i}", module="sale", odoo_version="17.0")
            for i in range(3)
        ]
        instance = ListFieldsOutput(
            model="sale.order",
            odoo_version="17.0",
            total=42,
            shown=3,
            fields=fields,
            next_step_hint=(
                "└─ Next: resolve_field(model='sale.order', field='field_0',"
                " odoo_version='17.0') for full chain | list_methods(model='sale.order',"
                " odoo_version='17.0') for behavior"
            ),
        )
        assert instance.total == 42
        assert instance.shown == 3
        assert len(instance.fields) == 3

        schema = _assert_json_schema_valid(ListFieldsOutput)
        assert "total" in schema["properties"]
        assert "shown" in schema["properties"]
        assert "fields" in schema["properties"]

    def test_empty_fields(self):
        instance = ListFieldsOutput(
            model="sale.order",
            odoo_version="17.0",
            total=0,
            shown=0,
            next_step_hint=(
                "└─ Next: list_methods(model='sale.order', odoo_version='17.0') for behavior"
            ),
        )
        assert instance.fields == []


class TestListMethodsOutput:
    """AC-B2-4 test 7/7."""

    def test_instantiate_and_schema(self):
        methods = [
            MethodRef(
                model="sale.order", name="action_confirm",
                module="sale", odoo_version="17.0",
            ),
            MethodRef(
                model="sale.order", name="action_confirm",
                module="sale_ext", odoo_version="17.0",
            ),
            MethodRef(
                model="sale.order", name="_compute_amount",
                module="sale", odoo_version="17.0",
            ),
        ]
        instance = ListMethodsOutput(
            model="sale.order",
            odoo_version="17.0",
            total=18,
            shown=3,
            methods=methods,
            override_names=["action_confirm"],
            next_step_hint=(
                "└─ Next: resolve_method(model='sale.order', method='action_confirm',"
                " odoo_version='17.0') for override chain"
            ),
        )
        assert instance.total == 18
        assert instance.shown == 3
        assert "action_confirm" in instance.override_names

        schema = _assert_json_schema_valid(ListMethodsOutput)
        assert "methods" in schema["properties"]
        assert "override_names" in schema["properties"]

    def test_empty_methods(self):
        instance = ListMethodsOutput(
            model="sale.order",
            odoo_version="17.0",
            total=0,
            shown=0,
            next_step_hint=(
                "└─ Next: list_fields(model='sale.order', odoo_version='17.0') for shape"
            ),
        )
        assert instance.methods == []
        assert instance.override_names == []
