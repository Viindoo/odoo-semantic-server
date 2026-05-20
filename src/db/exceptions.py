# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/exceptions.py
"""Typed exceptions for profile validation errors.

These exceptions carry HTTP-semantic meaning so that route handlers can
distinguish 404 (not found) from 422 (unprocessable entity) without
string-matching generic ValueError messages.
"""


class ProfileNotFoundError(KeyError):
    """Raised when a referenced profile ID does not exist in the database."""


class ProfileCycleError(ValueError):
    """Raised when setting a parent would create a cycle in the profile hierarchy."""


class ProfileVersionMismatchError(ValueError):
    """Raised when child and parent profiles have different odoo_version values."""


class ProfileNameConflictError(ValueError):
    """Raised when a profile name already exists (UNIQUE constraint violation)."""


class RepoNotFoundError(KeyError):
    """Raised when a referenced repo ID does not exist in the database."""


class RepoConflictError(ValueError):
    """Raised when a repo update would violate the UNIQUE(url, branch) constraint."""


class ProfileIndexedError(ValueError):
    """Raised when a profile has indexed repos and the requested field change would
    invalidate their Neo4j data (name or version change requires full re-index).

    HTTP mapping: 409 Conflict.
    """


class PoolNotInitializedError(RuntimeError):
    """Raised by `src.db.pg.get_pool()` when the module-level pool singleton
    has not been initialised yet (e.g. lifespan handler hasn't run, or PG was
    unreachable at startup and the background retry hasn't recovered).

    Inherits from RuntimeError so existing `except RuntimeError` paths keep
    working — but allows downstream callers (notably AuthMiddleware) to
    narrow their except to THIS specific failure instead of swallowing every
    unrelated RuntimeError (config errors, version checks, etc.).

    HTTP mapping: 503 Service Unavailable + Retry-After header.
    """
