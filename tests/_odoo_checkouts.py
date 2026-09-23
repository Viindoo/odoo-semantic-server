# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared real-Odoo-checkout discovery for live-parser tests (issue #364 D2).

WHY THIS FILE EXISTS
---------------------
Before this file, two rival conventions answered "where is the real Odoo
checkout for major version N" and only one of them ever worked.
`test_framework_bases_parity.py` (#363) auto-discovers via `OSM_ODOO_CHECKOUTS`
(default `~/git`) + `odoo<major>` and passes 12/12 on a dev box that has all
twelve checkouts. `test_parser_cli.py` / `test_parser_lint_rules.py` instead
each read their own per-version env vars (`ODOO8_SRC`, `ODOO9_SRC`,
`ODOO17_SRC`, ...) defaulting to `/nonexistent/odooN` - nothing in the repo
ever sets them, so their live-parser smoke tests have SKIPPED unconditionally
on every machine that has ever run them, including a dev box with all twelve
checkouts present at the conventional path. Per `pytest -rs` the skip reason
always reads as a plausible, occasional absence ("Real Odoo N ... not on
disk") - nothing distinguishes "missing today" from "structurally can never
be found." That is exactly the "guard that doesn't guard" failure issue #364
is about (see phase2-D-oracle-infra.md Q1/Q3): a test that LOOKS like a live
oracle but is inert everywhere it has ever been tried.

This module is the ONE place that answers the discovery question for every
live-parser test file, so a third rival convention never has to be invented
for the next one (ETHOS #9: SSOT - each fact declared exactly once).

Resolution order per major version N (first match wins):
  1. Legacy per-version override `ODOO<N>_SRC`, if set AND the path exists on
     disk - preserves anyone's existing manual setup; nobody's env breaks.
  2. Conventional layout `<OSM_ODOO_CHECKOUTS or ~/git>/odoo<N>` - the
     convention `test_framework_bases_parity.py` already established and
     proved working (12/12) on this dev box.
  3. Neither resolves -> None. Callers `pytest.skip()` per version; a missing
     checkout is expected and fine (not every machine has all 12 checkouts) -
     the point of this module is only to make "present" reliably detected,
     not to guarantee presence.
"""
from __future__ import annotations

import os
from pathlib import Path

# Every major this test suite's live-parser families survey (8.0 through
# 19.0 inclusive) - the one definition of "the full range" every live-parser
# test file imports.
SURVEYED_MAJORS: list[int] = list(range(8, 20))


def checkouts_parent() -> Path:
    """Parent directory holding ``<parent>/odoo<major>`` checkouts.

    Single override: ``OSM_ODOO_CHECKOUTS``. Defaults to ``~/git`` - never a
    hardcoded machine-specific absolute path (ETHOS #11: portable).
    """
    return Path(os.environ.get("OSM_ODOO_CHECKOUTS", str(Path.home() / "git")))


def checkout_root(major: int) -> Path | None:
    """Return the real Odoo checkout root for major version *major*, or None.

    See module docstring for the resolution order. Returns None (never a
    fabricated/nonexistent path) when nothing resolves, so callers can
    ``pytest.skip()`` per version without a second existence check.
    """
    legacy_value = os.environ.get(f"ODOO{major}_SRC")
    if legacy_value:
        legacy_path = Path(legacy_value)
        if legacy_path.is_dir():
            return legacy_path
    conventional = checkouts_parent() / f"odoo{major}"
    if conventional.is_dir():
        return conventional
    return None
