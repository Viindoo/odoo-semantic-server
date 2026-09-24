# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin repo page offers no GC option (G4, ADR-0056).

``--gc`` is a deprecated no-op: module retirement and the orphan sweep run on
every index run. A checkbox that does nothing invites operators to believe
cleanup needs it (and to leave it unticked, believing nothing is cleaned). The
site has no component-test harness (vitest covers ``src/lib`` only), so this
reads the component the page renders, like tests/test_site_ssr_fetch_routes_exist.py:
no checkbox bound to GC is rendered and the Index-all request body never
carries a ``gc`` field.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_TABLE = Path(__file__).resolve().parents[1] / "site" / "src" / "components" / "RepoTable.astro"


def test_repo_table_renders_no_gc_control_and_sends_no_gc_field():
    source = REPO_TABLE.read_text()
    inputs = re.findall(r"<input\b[^>]*>", source, re.S)
    assert inputs, "precondition: the component renders inputs"

    gc_inputs = [i for i in inputs if re.search(r"\bgc\b", i, re.I)]
    assert gc_inputs == [], gc_inputs
    assert not re.search(r"<span>\s*GC\s*</span>", source), "a GC label is still rendered"
    assert not re.search(r"\bbody\.gc\b|['\"]gc['\"]\s*:", source), (
        "the index request still sends a gc field"
    )
