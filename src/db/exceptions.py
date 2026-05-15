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
