# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Site marketing tool/resource count and version must match the real MCP surface
and pyproject.toml.

Guard against drift when the MCP tool surface changes but the marketing
constants in site/src/lib/constants.ts are not updated, or when pyproject.toml
is bumped but SITE_VERSION in constants.ts is forgotten.

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
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"


def _parse_ts_constant(name: str) -> int:
    """Extract the integer value of ``export const NAME = <int>;`` from constants.ts."""
    content = CONSTANTS_FILE.read_text()
    match = re.search(rf"export\s+const\s+{re.escape(name)}\s*=\s*(\d+)", content)
    assert match, (
        f"{name} not found in {CONSTANTS_FILE}. "
        "Add 'export const {name} = <int>;' to site/src/lib/constants.ts."
    )
    return int(match.group(1))


def _parse_ts_string_constant(name: str) -> str:
    """Extract the string value of ``export const NAME = '...';`` from constants.ts."""
    content = CONSTANTS_FILE.read_text()
    match = re.search(rf"""export\s+const\s+{re.escape(name)}\s*=\s*['"]([^'"]+)['"]""", content)
    assert match, (
        f"{name} not found in {CONSTANTS_FILE}. "
        f"Add \"export const {name} = '<version>';\" to site/src/lib/constants.ts."
    )
    return match.group(1)


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


def test_site_version_matches_pyproject():
    """SITE_VERSION in constants.ts must equal [project].version in pyproject.toml.

    Business rule: every version bump in pyproject.toml must be reflected in the
    site footer constant — otherwise the landing page/footer shows a stale version.
    """
    pyproject_content = PYPROJECT_FILE.read_text()
    # Match 'version = "x.y.z"' inside [project] section (first occurrence is project version)
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_content, re.MULTILINE)
    assert match, (
        f"Could not find 'version = \"...\"' in {PYPROJECT_FILE}. "
        "Ensure pyproject.toml has a [project] version entry."
    )
    pyproject_version = match.group(1)

    ts_version = _parse_ts_string_constant("SITE_VERSION")
    assert ts_version == pyproject_version, (
        f"SITE_VERSION in constants.ts is {ts_version!r} but pyproject.toml has "
        f"{pyproject_version!r}. "
        f"Update site/src/lib/constants.ts: export const SITE_VERSION = '{pyproject_version}';"
    )
