"""Handler-level error types that map to the MCP error codes listed in
`architecture/mcp-server.md`:

    400 invalid input (schema validation)
    404 entity not in index
    409 index stale for this path
    500 handler bug

Handlers raise these; the FastMCP wrapper translates them at the server
boundary so callers see clean status-code semantics.
"""

from __future__ import annotations


class HandlerError(Exception):
    """Base class."""

    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidInputError(HandlerError):
    status_code = 400


class NotFoundError(HandlerError):
    status_code = 404


class StaleIndexError(HandlerError):
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(f"index stale: {reason}")
        self.reason = reason
