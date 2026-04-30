"""Handler-level error types mapping to MCP status codes (400/404/409/500).

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
