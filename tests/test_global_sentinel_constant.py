# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift-guard: GLOBAL_PROFILE constant must stay in sync across all three files.

The '__global__' sentinel is the SSOT in src/constants.py (GLOBAL_PROFILE).
The migration SQL and any test files are the only other locations that may
contain the raw literal.  If they drift, suggest_pattern silently returns 0.

This test has NO pytest markers — it runs in make test-unit (no DB needed).
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# 1. Canonical constant value
# --------------------------------------------------------------------------


def test_global_profile_value():
    from src.constants import GLOBAL_PROFILE

    assert GLOBAL_PROFILE == "__global__", (
        f"GLOBAL_PROFILE must equal '__global__', got {GLOBAL_PROFILE!r}"
    )


# --------------------------------------------------------------------------
# 2. Migration SQL contains the sentinel literal
# --------------------------------------------------------------------------


def test_migration_contains_sentinel():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "0001_initial.sql"
    )
    text = migration.read_text(encoding="utf-8")
    assert "'__global__'" in text, (
        "migrations/0001_initial.sql must contain the literal '__global__' "
        "(used in backfill UPDATE, RLS policy, and sentinel CHECK). "
        "If you renamed the sentinel, update the squashed baseline migration too."
    )


# --------------------------------------------------------------------------
# 3. server.py no longer contains bare '__global__' literals outside comments
# --------------------------------------------------------------------------


def test_server_py_no_bare_global_literal():
    server_path = (
        Path(__file__).resolve().parents[1] / "src" / "mcp" / "server.py"
    )
    lines = server_path.read_text(encoding="utf-8").splitlines()
    violations = []
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        # Skip comment lines (start with '#' after stripping leading whitespace)
        if stripped.startswith("#"):
            continue
        if "'__global__'" in line:
            violations.append((i, line.rstrip()))
    assert violations == [], (
        "src/mcp/server.py must not contain bare '__global__' string literals "
        "(use the GLOBAL_PROFILE constant from src.constants instead). "
        f"Found at lines: {violations}"
    )
