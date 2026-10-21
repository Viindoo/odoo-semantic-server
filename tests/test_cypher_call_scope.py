# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every Cypher ``CALL`` subquery must carry a variable scope clause.

Neo4j 5.23 deprecated a ``CALL { ... }`` subquery written without a scope
clause; the replacement is ``CALL () { ... }`` (imports nothing) or
``CALL (a, b) { ... }`` (imports ``a`` and ``b``). The server answers each
deprecated statement with a ``FeatureDeprecationWarning`` notification, and the
write path logs every one (``notifications_min_severity=WARNING``): the
``odoo_20`` ``--full`` re-index of 2026-10-22 logged 659 of them, one per
module, from the module LintViolation selector in ``writer_neo4j``, and
``find_deprecated_usage`` logged one per call. A later Neo4j release that drops
the old form would turn both into query errors.

The scan is static and needs no database: it reads every ``.py`` file under
``src/`` and ``ops/`` and every ``.cypher`` file under ``ops/``, and flags a line
that opens a subquery body with a bare ``CALL {`` (``CALL {{`` inside an
f-string). Prose such as ``CALL {} IN TRANSACTIONS`` or ``CALL { WITH }`` in a
comment or docstring does not open a body on that line, so it is not flagged.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A line whose last token opens a CALL body with no scope clause: ``CALL {`` in
# plain Cypher, ``CALL {{`` in a Python f-string.
_UNSCOPED_CALL = re.compile(r"\bCALL\s*\{\{?\s*$")


def _cypher_sources() -> list[pathlib.Path]:
    files = sorted((_REPO_ROOT / "src").rglob("*.py"))
    files += sorted((_REPO_ROOT / "ops").rglob("*.py"))
    files += sorted((_REPO_ROOT / "ops").rglob("*.cypher"))
    return files


def _unscoped_calls(text: str) -> list[int]:
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if _UNSCOPED_CALL.search(line)
    ]


def test_every_call_subquery_declares_a_scope_clause():
    offenders = []
    for path in _cypher_sources():
        for lineno in _unscoped_calls(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "CALL subquery without a variable scope clause (deprecated since Neo4j "
        "5.23); write `CALL () {` or `CALL (var, ...) {`:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "line",
    [
        "            CALL {{",
        "CALL {",
        "    CALL{",
    ],
)
def test_bare_call_body_is_flagged(line):
    assert _unscoped_calls(line) == [1]


@pytest.mark.parametrize(
    "line",
    [
        "            CALL () {{",
        "CALL (n) {",
        "CALL (ext, def, edge_order) {",
        "    For auto-commit statements only (CALL {} IN TRANSACTIONS cannot run in a",
        "#       the Neo4j 5.26 `CALL { WITH }` deprecation.",
        "//        CALL { ... } IN TRANSACTIONS OF 10000 ROWS, which runs each inner",
    ],
)
def test_scoped_call_and_prose_are_not_flagged(line):
    assert _unscoped_calls(line) == []
