# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable entrypoint: ``python -m src.mcp``.

Keeps ``src/mcp/server.py`` a pure importable module so it is loaded exactly once
(under its real name), avoiding the double-instance bug that arises when
``server.py`` itself is run as ``__main__`` (the tool wrapper modules re-import it,
registering the 31 tools onto a second FastMCP instance that the served app never
sees, so MCP ``tools/list`` returns 0 while ``/health`` still reports 31).
"""
from src.mcp.server import main

main()
