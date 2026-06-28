# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_report_scope_classification.py
"""Pure-function unit tests for _report_model_in_own_module (issue #347).

No Neo4j, no Docker, no I/O. Tests the helper's cross-version truth table
(CLASS A/B/C + own-module + edge cases) to prove version-uniform correctness
without spinning a container.

These are fast, deterministic tests suitable for `make test` (unit suite).
"""
import pytest

from src.indexer.writer_neo4j_ui import _report_model_in_own_module  # noqa: E402

# ---------------------------------------------------------------------------
# Truth table parametrize (F1-corrected per implementation-plan.md section 4)
# ---------------------------------------------------------------------------
# Each row: (model_name, report_module, expected, description)
# expected=True  -> own-module gap -> WARN-eligible
# expected=False -> cross-module or missing -> DEBUG
_CASES = [
    # Own-module gap (WARN side) - prefix matches declaring module
    ("sale.order", "sale", True, "own-module gap: sale.order in sale"),
    ("account.move", "account", True, "own-module gap: account.move in account"),
    (
        "base.automation", "base_automation", False,
        # F1-corrected: prefix('base.automation')='base' != 'base_automation'
        # -> False (residual false-negative; the module name uses underscore separator
        # so the dotted prefix cannot match it - accepted per implementation-plan.md)
        "CLASS-B-named module own gap: base.automation in base_automation -> False (F1)"
    ),
    # CLASS B canonical - prefix coincides with a DIFFERENT indexed module
    (
        "base.automation", "my_addon", False,
        "CLASS B canonical: base.automation in my_addon (owner base_automation)"
    ),
    # CLASS B reachable - live trigger (hr_timesheet/mrp_account on account.analytic.line)
    (
        "account.analytic.line", "hr_timesheet", False,
        "CLASS B reachable: account.analytic.line in hr_timesheet (owner analytic)"
    ),
    (
        "account.analytic.line", "mrp_account", False,
        "CLASS B reachable: account.analytic.line in mrp_account"
    ),
    # CLASS C - version-rename (base.action.rule v8-v10 -> base.automation v11+)
    (
        "base.action.rule", "base_action_rule", False,
        "CLASS C v10: base.action.rule in base_action_rule - prefix base != module"
    ),
    (
        "base.action.rule", "my_addon", False,
        "CLASS C v10 cross-module: base.action.rule in my_addon"
    ),
    # CLASS A - prefix not a real module (ir.*/res.*/report.* owned by base)
    (
        "ir.attachment", "my_addon", False,
        "CLASS A: ir.attachment in my_addon (owner base - pre-existing false-neg)"
    ),
    # Cross-module out-of-scope (prefix indexed but not declaring module)
    (
        "account.move", "my_addon", False,
        "out-of-scope cross-module: account.move in my_addon"
    ),
    # Stable own-module example (account.analytic.account v8-v19 in analytic)
    (
        "account.analytic.account", "hr_timesheet", False,
        "CLASS C stable: account.analytic.account in hr_timesheet"
    ),
    # Fail-safe edge cases - no crash, no false WARNING
    ("", "sale", False, "fail-safe: empty model_name"),
    (None, "sale", False, "fail-safe: None model_name"),
    ("sale.order", "", False, "fail-safe: empty report_module"),
    ("sale.order", None, False, "fail-safe: None report_module"),
]


@pytest.mark.parametrize("model_name,report_module,expected,description", _CASES)
def test_report_model_in_own_module_truth_table(
    model_name, report_module, expected, description
):
    """A report's cross-module target is not an own-module gap.

    Parametrized over the CLASS A/B/C survey rows plus own-module and edge cases
    to prove version-uniform correctness without a Neo4j container.
    Each row names its business rule in 'description'.
    """
    result = _report_model_in_own_module(model_name, report_module)
    assert result == expected, (
        f"_report_model_in_own_module({model_name!r}, {report_module!r}) = {result}, "
        f"expected {expected}. Case: {description}"
    )
