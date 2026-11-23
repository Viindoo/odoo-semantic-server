# SPDX-License-Identifier: AGPL-3.0-or-later
"""Presence of the version-wide spec catalogues that ``index-core`` writes.

``index-core`` writes, per Odoo version, the core API catalogue (``CoreSymbol``)
and the odoo-bin catalogue (``CLICommand`` / ``CLIFlag``). A version can be
fully indexed for its modules (profile runs) while ``index-core`` has not run
for it yet. On such a version a lookup miss means "nothing indexed", NOT "this
symbol / command / flag does not exist": an answer that reads "not found" or
"removed" makes a client drop a real flag or rewrite a valid call (issue #404).

The spec tools therefore check catalogue presence on a miss and answer with a
distinct ``Status: not_indexed`` line instead. ``cli_help`` also counts the
``SpecMetadata {kind: 'cli'}`` row index-core writes with the CLI catalogue:
when it exists index-core ran, and a miss keeps its "not found" answer (with
the "pending curation" banner when the version's curated data is pending).

Not a tool module: callers pass their bounded single-row query function
(``_srv._single_bounded``) so the check runs on the caller's session and stays
monkeypatch-able in tests.
"""
from __future__ import annotations

from collections.abc import Callable

NOT_INDEXED_STATUS = "not_indexed"

_PROBES: dict[str, tuple[str, str]] = {
    # kind: (presence query, catalogue name used in the answer)
    "core": (
        "MATCH (n:CoreSymbol {odoo_version: $v}) RETURN n.qualified_name AS k LIMIT 1",
        "core API catalogue",
    ),
    "cli": (
        "MATCH (n:CLICommand {odoo_version: $v}) RETURN n.name AS k LIMIT 1",
        "CLI catalogue",
    ),
}


def catalogue_indexed(
    single_bounded: Callable, session, kind: str, version: str,
) -> bool:
    """True when ``index-core`` wrote the *kind* catalogue (``core``/``cli``) for *version*."""
    query, name = _PROBES[kind]
    rec = single_bounded(
        session, query, label=f"{name} presence (Odoo {version})", v=version,
    )
    return rec is not None


def not_indexed_warning(kind: str, version: str, subject: str, check: str) -> str:
    """One-line warning for a miss on a version whose *kind* catalogue is absent.

    *subject* names what was looked up (``"flag '--x'"``); *check* is how the
    caller can confirm it meanwhile (``"`odoo-bin server --help`"``).
    """
    name = _PROBES[kind][1]
    return (
        f"⚠ the {name} is not indexed for Odoo {version} - this is NOT evidence "
        f"that {subject} is absent or removed. Confirm with {check} on a {version} "
        f"checkout; the server operator runs index-core for this version."
    )
