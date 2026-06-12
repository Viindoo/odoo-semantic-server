# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural guard — every MCP tool that performs a Neo4j read must NOT let an
``OrmQueryTimeout`` escape as a FastMCP protocol ``isError`` (ADR-0023 / #287).

Background
----------
A tool body that touches Neo4j can raise ``OrmQueryTimeout`` (a tx-timeout
converted by ``_data_bounded`` / ``_single_bounded``, or surfaced by Tier-3
``_resolve_version`` -> ``_latest_version``). Two mechanisms keep that timeout
from escaping the tool as a protocol error:

  * **Decorator backstop** — ``@offload_neo4j`` / ``@offload_bounded`` /
    ``@offload_bounded_nonorm`` each wrap the WHOLE sync body in an
    ``except OrmQueryTimeout`` (they convert it to the clean string + metric).
    A bare ``@offload`` (the mutating/session pin decorator) does NOT — it only
    offloads to a thread, it has no timeout catch.
  * **Inline catch** — a tool with no backstop (an ``async def`` body, or a sync
    body under bare ``@offload``) must catch ``OrmQueryTimeout`` itself.

The original timeout-hardening harness never exercised the version-resolution
path (it always passed an EXPLICIT version, short-circuiting Tier-1, and also
stubbed ``_resolve_version``), so the bug-class "a Neo4j read inside the
resolution path escapes as a protocol isError" hid in a blind spot (the #287
review fix wrapped the 3 EMBED tools). This guard makes the WHOLE class
regression-proof for FUTURE tools: it AST-parses every ``@mcp.tool`` and asserts
that any tool which reads Neo4j and is NOT covered by a backstop decorator wraps
EVERY Neo4j read in a timeout-catching ``try`` within its own scope (the public
def body + the same-file ``_<toolname>`` implementation it dispatches to). The
check is PER-READ, not presence-of-any-catch: the original #287 bug had a catch
for the main query but left the version-resolution read outside it — a
presence-only guard would have passed that, so this guard counts uncovered reads.

This is a static (AST) test in the spirit of
``tests/test_pipeline_import_discipline.py`` — no database, no Docker.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TOOLS_DIR = _REPO_ROOT / "src" / "mcp" / "tools"

# Decorators whose body-level catch backstops OrmQueryTimeout for the WHOLE sync
# body. A plain ``@offload`` is intentionally NOT here — it offloads to a thread
# but adds no timeout catch (so a Neo4j-reading @offload tool must catch inline).
_BACKSTOP_DECORATORS = frozenset(
    {"offload_neo4j", "offload_bounded", "offload_bounded_nonorm"}
)

# Names whose call means "this scope reads Neo4j" (and so can raise
# OrmQueryTimeout). _resolve_version is included because its Tier-3 fallback runs
# a bounded Neo4j read (_latest_version) — the exact path #287 closed.
_NEO4J_READ_MARKERS = frozenset(
    {"_data_bounded", "_single_bounded", "_resolve_version"}
)

_ORM_TIMEOUT_EXC = "OrmQueryTimeout"


# ---------------------------------------------------------------------------
# AST helpers (module-level so the self-test can exercise them on a snippet).
# ---------------------------------------------------------------------------


def _decorator_names(func: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """Return the bare attribute/name of each decorator on *func*.

    ``@mcp.tool(...)`` -> ``tool``; ``@offload_neo4j`` -> ``offload_neo4j``;
    ``@offload`` -> ``offload``. Handles both the call form (``@x.y(...)``) and
    the bare form (``@x``).
    """
    names: set[str] = set()
    for dec in func.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _is_mcp_tool(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """True iff *func* is decorated with ``@mcp.tool`` (the registration hook)."""
    for dec in func.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Attribute) and node.attr == "tool":
            return True
    return False


def _has_backstop(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """True iff a body-level OrmQueryTimeout backstop decorator wraps *func*."""
    return bool(_decorator_names(func) & _BACKSTOP_DECORATORS)


def _is_marker_call(node: ast.AST) -> bool:
    """True iff *node* is a call to a Neo4j-read marker (Name or Attribute form).

    Captures both ``_data_bounded(...)`` (Name) and ``_srv._data_bounded(...)``
    (Attribute, the server-hub form) so a read reached via the hub is detected.
    """
    if not isinstance(node, ast.Call):
        return False
    target = node.func
    if isinstance(target, ast.Attribute):
        return target.attr in _NEO4J_READ_MARKERS
    if isinstance(target, ast.Name):
        return target.id in _NEO4J_READ_MARKERS
    return False


def _marker_call_ids(node: ast.AST) -> set[int]:
    """``id()`` of every Neo4j-read marker call anywhere under *node*."""
    return {id(n) for n in ast.walk(node) if _is_marker_call(n)}


def _try_catches_orm_timeout(try_node: ast.Try) -> bool:
    """True iff *try_node* has an ``except`` handler naming OrmQueryTimeout.

    Matches a bare ``except OrmQueryTimeout`` (Name / Attribute) and the tuple
    form ``except (OrmQueryTimeout, ...)``.
    """
    for handler in try_node.handlers:
        if handler.type is None:
            continue
        types = (
            handler.type.elts
            if isinstance(handler.type, ast.Tuple)
            else [handler.type]
        )
        for t in types:
            if isinstance(t, ast.Name) and t.id == _ORM_TIMEOUT_EXC:
                return True
            if isinstance(t, ast.Attribute) and t.attr == _ORM_TIMEOUT_EXC:
                return True
    return False


def _covered_marker_ids(func: ast.AST) -> set[int]:
    """``id()`` of marker calls lexically inside a timeout-catching ``try`` body.

    Only the ``try``/``else`` blocks count as protected — a read in the
    ``except``/``finally`` of a timeout-catching try is not itself guarded.

    This is PER-READ, not presence-of-any-catch: a tool that catches one read
    but leaves a SECOND read uncaught — the exact #287 split-read bug, where the
    version-resolution read escaped while the main query was caught — is still
    reported, because that second read's id is not in the covered set.
    """
    covered: set[int] = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Try) and _try_catches_orm_timeout(sub):
            for stmt in [*sub.body, *sub.orelse]:
                covered |= _marker_call_ids(stmt)
    return covered


def _uncovered_marker_count(scope: list[ast.AST]) -> int:
    """Count Neo4j-read marker calls NOT inside a timeout-catching ``try``.

    Computed per scope function: a read is protected only by a ``try`` in its
    OWN function body — the inline-catch convention these tools follow (the
    async wrapper merely ``to_thread``-s the impl; the catch lives with the
    read). A future tool that instead wraps ``to_thread`` in an outer try would
    be flagged here — a deliberate, loud false-positive preferred over a silent
    miss of the bug-class.
    """
    total = 0
    for func in scope:
        total += len(_marker_call_ids(func) - _covered_marker_ids(func))
    return total


def _reads_neo4j_scope(scope: list[ast.AST]) -> bool:
    """True iff any function in *scope* performs a Neo4j-read marker call."""
    return any(_marker_call_ids(f) for f in scope)


def _impl_for(tool_name: str, module: ast.Module) -> ast.AST | None:
    """Return the same-file ``_<tool_name>`` implementation def, if any.

    The async EMBED tools register a thin ``@mcp.tool`` wrapper that dispatches
    to a sibling ``_<name>`` impl carrying the actual Neo4j read + inline catch;
    a tool's protective scope is the union of both.
    """
    target = f"_{tool_name}"
    for node in module.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == target
        ):
            return node
    return None


# ---------------------------------------------------------------------------
# The core guard rule, applied to one parsed module.
# ---------------------------------------------------------------------------


def _violations_in_module(module: ast.Module) -> list[str]:
    """Return tool names in *module* that read Neo4j with an UNGUARDED read.

    A tool is a VIOLATION when, across its protective scope (public def + the
    same-file ``_<name>`` impl), it:
      * is NOT wrapped by a backstop decorator, AND
      * DOES read Neo4j (a marker call), AND
      * has at least one marker call NOT lexically inside a timeout-catching
        ``try`` (per-read, so a split-read like #287 is caught — not merely the
        "no catch anywhere" case).
    """
    violations: list[str] = []
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_mcp_tool(node):
            continue
        if _has_backstop(node):
            continue  # decorator catches the whole body — safe by construction.

        scope: list[ast.AST] = [node]
        impl = _impl_for(node.name, module)
        if impl is not None:
            scope.append(impl)

        if not _reads_neo4j_scope(scope):
            continue  # no Neo4j read in scope -> no OrmQueryTimeout to leak.

        if _uncovered_marker_count(scope) > 0:
            violations.append(node.name)
    return violations


def _guarded_neo4j_tools_in_module(module: ast.Module) -> list[str]:
    """Return tool names that read Neo4j, have no backstop, AND do catch.

    These are the "needs-guard-and-has-it" tools (the 3 EMBED + set_active_version)
    — the population the sanity test asserts the detector actually finds, so the
    guard cannot silently pass by failing to recognise any tool.
    """
    found: list[str] = []
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_mcp_tool(node) or _has_backstop(node):
            continue
        scope: list[ast.AST] = [node]
        impl = _impl_for(node.name, module)
        if impl is not None:
            scope.append(impl)
        if _reads_neo4j_scope(scope) and _uncovered_marker_count(scope) == 0:
            found.append(node.name)
    return found


def _tool_modules() -> list[pathlib.Path]:
    files = sorted(_TOOLS_DIR.glob("*.py"))
    files = [f for f in files if f.name != "__init__.py"]
    assert files, f"no tool modules found under {_TOOLS_DIR}"
    return files


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_neo4j_reading_mcp_tool_is_timeout_guarded():
    """No @mcp.tool may read Neo4j without a backstop decorator OR an inline catch.

    This is the regression guard for the #287 bug-class: a future async tool (or
    a sync tool stripped of its @offload_neo4j) that reads Neo4j but forgets the
    inline ``except OrmQueryTimeout`` would let a tx-timeout escape as a FastMCP
    protocol isError (ADR-0023 violation). Such a tool turns this test red, named.
    """
    offenders: dict[str, list[str]] = {}
    for path in _tool_modules():
        viol = _violations_in_module(_parse(path))
        if viol:
            offenders[str(path.relative_to(_REPO_ROOT))] = viol

    assert not offenders, (
        "These @mcp.tool handlers read Neo4j but neither carry a backstop "
        "decorator (@offload_neo4j/@offload_bounded/@offload_bounded_nonorm) nor "
        "catch OrmQueryTimeout inline — a tx-timeout would escape as a protocol "
        f"isError (ADR-0023 / #287). Offenders: {offenders}"
    )


def test_guard_actually_recognises_the_known_inline_caught_tools():
    """Sanity: the detector must FIND the >=4 known needs-guard-and-has-it tools.

    If the AST detection silently broke (e.g. a refactor renamed the markers or
    the decorators), ``_violations_in_module`` would return an empty list for the
    wrong reason and the guard above would pass vacuously. We pin the floor at
    the 4 tools that today read Neo4j with NO backstop but DO catch inline:
    the 3 EMBED tools (find_examples / suggest_pattern / find_style_override) and
    the mutating set_active_version. A shortfall means detection is broken.
    """
    guarded: set[str] = set()
    for path in _tool_modules():
        guarded.update(_guarded_neo4j_tools_in_module(_parse(path)))

    expected = {
        "find_examples",
        "suggest_pattern",
        "find_style_override",
        "set_active_version",
    }
    missing = expected - guarded
    assert not missing, (
        "AST detection failed to recognise known inline-caught Neo4j tools "
        f"{sorted(missing)} — the guard would pass vacuously. Found: {sorted(guarded)}"
    )
    assert len(guarded) >= 4, (
        f"expected >=4 needs-guard-and-has-catch tools, found {len(guarded)}: "
        f"{sorted(guarded)} — AST detection is under-counting."
    )


# A deliberately-broken async tool: registered via @mcp.tool, reads Neo4j via
# _resolve_version, NO backstop decorator, and NO except OrmQueryTimeout. The
# guard MUST flag it — proving the check is not a no-op (it can produce RED).
_VIOLATING_SNIPPET = '''
@mcp.tool(**READONLY_TOOL_KWARGS)
async def leaky_tool(odoo_version: RequiredOdooVersion) -> str:
    with _srv._get_driver().session() as session:
        v = _srv._resolve_version(odoo_version, session)
    return f"resolved {v}"
'''

# A compliant async counterpart: same Neo4j read, but WITH the inline catch.
_COMPLIANT_SNIPPET = '''
@mcp.tool(**READONLY_TOOL_KWARGS)
async def safe_tool(odoo_version: RequiredOdooVersion) -> str:
    from src.mcp.orm import OrmQueryTimeout
    try:
        with _srv._get_driver().session() as session:
            v = _srv._resolve_version(odoo_version, session)
    except OrmQueryTimeout as exc:
        return exc.user_message
    return f"resolved {v}"
'''


def test_guard_flags_a_violating_snippet():
    """Self-test: the guard reports a violation for an unguarded async Neo4j tool.

    Proves the rule is falsifiable (red-able) — a no-op guard that always finds
    zero violations would fail this test.
    """
    module = ast.parse(_VIOLATING_SNIPPET)
    assert _violations_in_module(module) == ["leaky_tool"], (
        "guard must flag an async @mcp.tool that reads Neo4j (via _resolve_version) "
        "with no backstop and no inline except OrmQueryTimeout"
    )


def test_guard_passes_a_compliant_snippet():
    """Self-test: the guard does NOT flag the same tool once it catches inline.

    Pairs with the violating snippet to prove the GREEN side — the rule passes
    exactly when the inline catch is present (not a tautological always-pass /
    always-fail).
    """
    module = ast.parse(_COMPLIANT_SNIPPET)
    assert _violations_in_module(module) == [], (
        "guard must NOT flag an async @mcp.tool that catches OrmQueryTimeout inline"
    )


# The EXACT #287 review bug shape: a tool that DOES catch OrmQueryTimeout for one
# Neo4j read (the data_bounded query) but leaves a SECOND read (the version
# resolution) OUTSIDE the try. A presence-of-any-catch guard would wrongly pass
# this; the per-read guard must flag it — this is the regression that actually
# shipped and was fixed in commit 497cb11.
_SPLIT_READ_SNIPPET = '''
@mcp.tool(**READONLY_TOOL_KWARGS)
async def split_tool(odoo_version: RequiredOdooVersion) -> str:
    from src.mcp.orm import OrmQueryTimeout
    with _srv._get_driver().session() as session:
        v = _srv._resolve_version(odoo_version, session)   # UNCAUGHT read (#287 bug)
    try:
        with _srv._get_driver().session() as session:
            rows = _srv._data_bounded(session, "...", label="x")
    except OrmQueryTimeout as exc:
        return exc.user_message
    return f"{v} {rows}"
'''


def test_guard_flags_a_split_read_even_when_one_read_is_caught():
    """Self-test: the guard flags the #287 split-read (one read caught, one not).

    This is the decisive case — a presence-of-any-catch check passes this tool
    (it HAS an except OrmQueryTimeout), but the per-read rule must report it
    because the version-resolution read sits outside the try. Proves the guard is
    strong enough to catch the exact bug that shipped, not just a no-catch tool.
    """
    module = ast.parse(_SPLIT_READ_SNIPPET)
    assert _violations_in_module(module) == ["split_tool"], (
        "guard must flag a tool whose version-resolution read is outside the "
        "OrmQueryTimeout try even though another read is caught (the #287 bug)"
    )
