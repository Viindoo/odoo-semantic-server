# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal stub modelling Odoo's api.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=function.
"""Odoo API decorators stub.

Provides the decorator signatures that the Odoo ORM uses to mark methods
with lifecycle, dependency, and constraint metadata.
"""


def depends(*args):
    """Decorate a compute method declaring the fields it depends on.

    Args:
        *args: Field names (strings) that trigger recomputation.

    Returns:
        Decorator function wrapping the compute method.
    """
    def decorator(fn):
        fn._depends = args
        return fn
    return decorator


def constrains(*args):
    """Decorate a constraint method declaring the fields it validates.

    Args:
        *args: Field names that trigger the constraint check.

    Returns:
        Decorator function wrapping the constraint method.
    """
    def decorator(fn):
        fn._constrains = args
        return fn
    return decorator


def model(fn):
    """Mark a method as a model-level method (no record set required).

    Returns:
        The decorated function with _model flag set.
    """
    fn._model = True
    return fn


def model_create_multi(fn):
    """Mark a method to receive a list of dicts on create (Odoo 13+ pattern).

    Returns:
        The decorated function with _model_create_multi flag set.
    """
    fn._model_create_multi = True
    return fn


def onchange(*args):
    """Decorate an onchange handler declaring the trigger fields.

    Args:
        *args: Field names that trigger the onchange call.

    Returns:
        Decorator function wrapping the onchange handler.
    """
    def decorator(fn):
        fn._onchange = args
        return fn
    return decorator


def returns(model_name, downgrade=None, upgrade=None):
    """Annotate a method with its return type for API compatibility.

    Args:
        model_name: The Odoo model name the method returns records of.
        downgrade: Optional downgrade function for v7-API compat.
        upgrade: Optional upgrade function for v7-API compat.

    Returns:
        Decorator function.
    """
    def decorator(fn):
        fn._returns = (model_name, downgrade, upgrade)
        return fn
    return decorator
