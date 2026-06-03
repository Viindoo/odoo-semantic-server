# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_diff_engine_lifecycle_unit.py
"""Pure-logic unit tests extracted from test_diff_engine_lifecycle.py (WS-D / DD2 demote).

The ``TestComputeDiffDeprecatedBucket`` class exercises ``compute_diff()`` on
in-memory ``CoreSymbolInfo`` lists only — it never opens a Bolt session, never
requests the ``neo4j_driver`` / ``lifecycle_writer`` fixtures, and asserts purely
on the returned ``DiffResult``.  The parent file carries a module-level
``pytestmark = pytest.mark.neo4j`` (its other classes write/read real CoreSymbol
nodes), which a per-test override cannot subtract; so these pure tests live here
in an unmarked module and now run in the fast unit tier (``-m 'not neo4j'``).

DD2 evidence: confirmed no DB fixture dependency — ``compute_diff()`` is a pure
function over ``CoreSymbolInfo`` value objects.
"""
from src.indexer.diff_engine import DiffResult, compute_diff
from src.indexer.models import CoreSymbolInfo


def _sym(qname, version, kind="function", status="stable", **kwargs):
    return CoreSymbolInfo(
        qualified_name=qname, kind=kind, odoo_version=version,
        status=status, **kwargs,
    )


# ---------------------------------------------------------------------------
# Pure unit tests for compute_diff — deprecated bucket
# ---------------------------------------------------------------------------

class TestComputeDiffDeprecatedBucket:
    def test_diff_deprecated_detected_when_status_changes(self):
        """compute_diff produces deprecated bucket when status stable → deprecated."""
        old = [_sym("odoo.models.BaseModel.read_group", "17.0", status="stable")]
        new = [_sym("odoo.models.BaseModel.read_group", "18.0", status="deprecated")]
        diff = compute_diff(old, new)
        assert isinstance(diff, DiffResult)
        assert any(
            s.qualified_name == "odoo.models.BaseModel.read_group"
            for s in diff.deprecated
        ), f"Expected deprecated entry, got: {diff.deprecated}"

    def test_diff_no_deprecated_when_both_stable(self):
        """No deprecated entry when both old and new are stable."""
        old = [_sym("odoo.tools.safe_eval.safe_eval", "17.0", status="stable")]
        new = [_sym("odoo.tools.safe_eval.safe_eval", "18.0", status="stable")]
        diff = compute_diff(old, new)
        assert diff.deprecated == []

    def test_diff_deprecated_not_double_counted_in_removed(self):
        """Symbol that went stable→deprecated is in deprecated, NOT in removed."""
        old = [_sym("odoo.fields.Field.group_operator", "17.0", status="stable")]
        new = [_sym("odoo.fields.Field.group_operator", "18.0", status="deprecated")]
        diff = compute_diff(old, new)
        # present in both versions → not removed
        qnames_removed = [s.qualified_name for s in diff.removed]
        assert "odoo.fields.Field.group_operator" not in qnames_removed
