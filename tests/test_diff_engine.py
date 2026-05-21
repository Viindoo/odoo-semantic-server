# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_diff_engine.py
"""Diff engine tests (M4.5 WI2.1).

compute_diff is a pure function — no DB, no IO. Cross-version CoreSymbol diff
produces 4 buckets: added, removed, stable, replaced.
"""
from src.indexer.diff_engine import DiffResult, compute_diff
from src.indexer.models import CoreSymbolInfo


def _sym(qname, kind="function", version="17.0", **kwargs):
    return CoreSymbolInfo(
        qualified_name=qname, kind=kind, odoo_version=version, **kwargs,
    )


def test_diff_symbol_added_only_in_new():
    """Symbol present in v18 but not v17 → added."""
    old = []
    new = [_sym("odoo.tools.zip_dir", version="18.0")]
    diff = compute_diff(old, new)
    assert isinstance(diff, DiffResult)
    assert len(diff.added) == 1
    assert diff.added[0].qualified_name == "odoo.tools.zip_dir"
    assert diff.removed == []
    assert diff.stable == []
    assert diff.replaced == []


def test_diff_symbol_removed_only_in_old():
    """Symbol present in v17 but not v18 → removed."""
    old = [_sym("odoo.models.BaseModel.name_get", kind="orm_method", version="17.0")]
    new = []
    diff = compute_diff(old, new)
    assert len(diff.removed) == 1
    assert diff.removed[0].qualified_name == "odoo.models.BaseModel.name_get"
    assert diff.added == []


def test_diff_symbol_replaced_via_replacement_qname():
    """Old symbol with replacement_qname pointing to a new symbol → replaced edge."""
    old = [_sym(
        "odoo.fields.Field.group_operator",
        kind="field_type", version="17.0",
        replacement_qname="odoo.fields.Field.aggregator",
    )]
    new = [_sym(
        "odoo.fields.Field.aggregator",
        kind="field_type", version="18.0",
    )]
    diff = compute_diff(old, new)
    assert len(diff.replaced) == 1
    assert diff.replaced[0] == (
        "odoo.fields.Field.group_operator",
        "odoo.fields.Field.aggregator",
    )


def test_diff_symbol_stable_in_both_versions():
    """Symbol present in both versions, no replacement → stable bucket only."""
    old = [_sym("odoo.tools.safe_eval", version="17.0")]
    new = [_sym("odoo.tools.safe_eval", version="18.0")]
    diff = compute_diff(old, new)
    assert diff.added == []
    assert diff.removed == []
    assert diff.replaced == []
    assert len(diff.stable) == 1
    assert diff.stable[0][0].qualified_name == "odoo.tools.safe_eval"
    assert diff.stable[0][1].qualified_name == "odoo.tools.safe_eval"


def test_diff_replaced_skipped_when_target_missing():
    """Old symbol claims a replacement that's not in new — fall back to removed (no edge)."""
    old = [_sym(
        "odoo.x.gone",
        version="17.0",
        replacement_qname="odoo.x.never_existed",
    )]
    new = []  # replacement doesn't exist in new index
    diff = compute_diff(old, new)
    # Without a real target node, REPLACED_BY edge cannot be MERGED — counted as removed.
    assert diff.replaced == []
    assert len(diff.removed) == 1
