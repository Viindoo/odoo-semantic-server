# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests (no DB) for Fix A — honest tri-state effective_readonly.

era1 (v8/v9) text-regex parsing makes no readonly determination, so the field
must carry ``effective_readonly is None`` (renderer omits the line) rather than
a false-green ``False``. era2 (v10+) still computes a concrete bool.
"""
import pytest

from src.indexer.models import FieldInfo, ModuleInfo
from src.indexer.parser_python import _compute_effective_readonly
from src.indexer.parser_python_era1 import _parse_era1_text


@pytest.fixture
def v8_module() -> ModuleInfo:
    return ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path="", depends=[], version_raw="8.0.1.0",
    )


def test_fieldinfo_default_effective_readonly_is_none():
    # Constructing without effective_readonly → tri-state None (no determination).
    f = FieldInfo(name="x", ttype="char")
    assert f.effective_readonly is None


def test_era1_fields_leave_effective_readonly_none(v8_module):
    src = (
        "print 'era1'\n\n"
        "class MyModel(osv.osv):\n"
        "    _name = 'my.model'\n"
        "    _columns = {\n"
        "        'name': fields.char('Name', size=64),\n"
        "        'total': fields.function(lambda *a: 0, type='float', string='Total'),\n"
        "    }\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert field_map  # sanity: fields extracted
    for f in field_map.values():
        assert f.effective_readonly is None, (
            f"era1 field {f.name} must leave effective_readonly None (false-green guard)"
        )


def test_era2_compute_without_inverse_is_true():
    # computed field, no inverse setter → effectively readonly
    assert _compute_effective_readonly(
        readonly=None, related=None, compute="_compute_total", inverse=None
    ) is True


def test_era2_compute_with_inverse_is_false():
    # computed field WITH an inverse setter → writable
    assert _compute_effective_readonly(
        readonly=None, related=None, compute="_compute_total", inverse="_inverse_total"
    ) is False
