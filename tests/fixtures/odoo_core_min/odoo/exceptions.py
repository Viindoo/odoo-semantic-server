# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal stub modelling Odoo's exceptions.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=exception.
"""Odoo exception hierarchy stub.

Provides the base exception classes used throughout the Odoo framework
to signal user-visible errors, access control violations, and validation
failures distinct from Python built-in exceptions.
"""


class OdooException(Exception):
    """Base class for all Odoo framework exceptions.

    Subclass this when you need an exception that the Odoo framework
    will handle (e.g., display to the user) rather than propagate as
    an internal server error.
    """


class UserError(Exception):
    """Raise when a user action cannot be completed due to business rules.

    This triggers a user-visible error dialog in the web client.
    Use this instead of ValueError for business-logic constraint violations.

    Args:
        message: Human-readable explanation of the error condition.
    """


class ValidationError(Exception):
    """Raise inside @api.constrains methods when a constraint is violated.

    The framework catches ValidationError and shows the message in the form
    view inline (below the field) rather than in a dialog.

    Args:
        message: Human-readable description of which constraint failed.
    """


class AccessError(Exception):
    """Raise when a user lacks the required access rights for an operation.

    The framework shows this as a permission denied message.

    Args:
        message: Human-readable description of the missing permission.
    """


class MissingError(Exception):
    """Raise when a record expected to exist has been deleted or is missing.

    Args:
        message: Human-readable context about which record is missing.
    """


class RedirectWarning(Warning):
    """Raise to display a warning with a redirect action button.

    Args:
        message: Human-readable warning text.
        action: The action to trigger when user clicks the action button.
        button_text: Label for the redirect button shown to the user.
    """
