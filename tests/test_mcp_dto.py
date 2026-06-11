# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/mcp/dto.py — Pydantic v2 ``*Ref`` DTOs.

The retired ``*Output`` response DTOs (the dual-channel structured subsystem)
were physically removed when ADR-0028 made every tool text-only; only the
``*Ref`` composite-key identifiers remain in dto.py, so only they are tested
here.

No database required — these are pure Pydantic model tests.
Runtime: <2s.
"""

import pytest

from src.mcp.dto import (
    CoreSymbolRef,
    FieldRef,
    MethodRef,
    ModelRef,
    ModuleRef,
    PatternRef,
    ViewRef,
)

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

