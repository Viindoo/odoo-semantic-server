# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Site marketing tool/resource count must match the real MCP surface.

Guard against drift when the MCP tool surface changes but the marketing
constants in site/src/lib/constants.ts are not updated.

No Docker required: importing src.mcp.server registers all @mcp.tool()
decorators and @mcp.resource() templates at module-import time (before any
DB connection is needed). The private _tool_manager/_resource_manager APIs
are used here — they are the same paths exercised by test_health_endpoint.py
and the /health introspection fallback (src/mcp/health.py).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTANTS_FILE = REPO_ROOT / "site" / "src" / "lib" / "constants.ts"


def _parse_ts_constant(name: str) -> int:
    """Extract the integer value of ``export const NAME = <int>;`` from constants.ts."""
    content = CONSTANTS_FILE.read_text()
    match = re.search(rf"export\s+const\s+{re.escape(name)}\s*=\s*(\d+)", content)
    assert match, (
        f"{name} not found in {CONSTANTS_FILE}. "
        "Add 'export const {name} = <int>;' to site/src/lib/constants.ts."
    )
    return int(match.group(1))


def test_constants_file_exists():
    """site/src/lib/constants.ts must exist (SSOT for marketing counts)."""
    assert CONSTANTS_FILE.exists(), (
        f"{CONSTANTS_FILE} does not exist. "
        "Create it with TOOL_COUNT and RESOURCE_COUNT exports."
    )


def test_tool_count_matches_mcp_surface():
    """TOOL_COUNT in constants.ts must equal the number of registered MCP tools.

    Business rule: every tool added to or removed from src/mcp/server.py must
    be reflected in the site marketing constant — otherwise the landing page
    advertises stale numbers.
    """
    from src.mcp.server import mcp  # noqa: PLC0415

    # _tool_manager._tools is the same private path used by the /health fallback
    # (src/mcp/health.py _get_mcp_tool_count). No DB call at import time.
    real_count = len(mcp._tool_manager._tools)

    declared = _parse_ts_constant("TOOL_COUNT")
    assert declared == real_count, (
        f"TOOL_COUNT in constants.ts is {declared} but MCP server has {real_count} tools. "
        "Update site/src/lib/constants.ts: export const TOOL_COUNT = {real_count};"
    )


def test_resource_count_matches_mcp_surface():
    """RESOURCE_COUNT in constants.ts must equal the number of registered MCP resource templates.

    Business rule: every resource template added to or removed from
    src/mcp/resources.py must be reflected in the site marketing constant.
    The docstring in register_resources() explicitly documents the count as 7
    and notes side-effect on ``mcp._resource_manager._templates``.
    """
    from src.mcp.server import mcp  # noqa: PLC0415

    # _resource_manager._templates is documented in resources.py register_resources().
    real_count = len(mcp._resource_manager._templates)

    declared = _parse_ts_constant("RESOURCE_COUNT")
    assert declared == real_count, (
        f"RESOURCE_COUNT in constants.ts is {declared} but MCP server has {real_count} "
        "resource templates. "
        "Update site/src/lib/constants.ts: export const RESOURCE_COUNT = {real_count};"
    )
