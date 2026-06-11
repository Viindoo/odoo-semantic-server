# MCP tool wrapper modules split out of src/mcp/server.py (god-file refactor).
# Each module imports `mcp` (+ the kwargs constants / offload decorators it needs)
# from src.mcp.server and registers tools via the @mcp.tool import-time side
# effect.  server.py imports these modules at the end of the file so the
# decorators run; tool bodies reach hub helpers via a late
# `from src.mcp import server as srv` (or import impls directly from their
# submodule).  See docs refactor-solution §2.2 / §3.
