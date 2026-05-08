# Minimal stub modelling Odoo's tools/safe_eval.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=function.
"""Safe expression evaluation utilities stub.

Provides safe_eval and related helpers that allow Odoo to evaluate
user-supplied Python expressions (domain filters, computed defaults,
report templates) in a restricted sandbox without access to the full
Python built-in namespace.
"""

# Restricted built-ins allowed inside safe_eval expressions.
_SAFE_BUILTINS = {
    "True": True,
    "False": False,
    "None": None,
    "int": int,
    "float": float,
    "str": str,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "bool": bool,
    "len": len,
    "range": range,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}


def safe_eval(expr, globals_dict=None, locals_dict=None, mode="eval", nocopy=False):
    """Evaluate *expr* in a restricted sandbox.

    Raises ValueError for expressions that attempt to access restricted
    built-ins or perform imports. Intended for evaluating domain filters
    and user-defined computed default expressions.

    Args:
        expr: Python expression string to evaluate.
        globals_dict: Additional names to expose in the evaluation namespace.
        locals_dict: Local variable bindings for the evaluation.
        mode: 'eval' for expressions, 'exec' for statements.
        nocopy: If True, do not copy globals_dict before merging safe built-ins.

    Returns:
        The result of evaluating *expr*, or None for 'exec' mode.

    Raises:
        ValueError: When *expr* contains unsafe constructs.
        SyntaxError: When *expr* is not valid Python syntax.
    """
    namespace = dict(_SAFE_BUILTINS)
    if globals_dict:
        namespace.update(globals_dict)
    if locals_dict:
        namespace.update(locals_dict)
    try:
        return eval(compile(expr, "<safe_eval>", mode), namespace)  # noqa: PGH001
    except Exception as exc:
        raise ValueError(f"safe_eval error: {exc}") from exc


def expr_eval(expr):
    """Evaluate a simple boolean expression string.

    Convenience wrapper around safe_eval for single boolean expressions
    commonly used in Odoo domain shorthand.

    Args:
        expr: Short Python expression string, e.g. "user.has_group('base.group_user')".

    Returns:
        Evaluated boolean result.
    """
    return safe_eval(expr, mode="eval")


def test_expr(expr, allowed_keys, message=""):
    """Validate that *expr* only references names in *allowed_keys*.

    Used to pre-validate user-supplied computed field expressions before
    executing them at runtime, catching typos and injection attempts early.

    Args:
        expr: Python expression string to validate.
        allowed_keys: Set of permitted variable names.
        message: Optional extra context included in the raised error.

    Raises:
        ValueError: When *expr* references a name not in *allowed_keys*.
    """
    import ast as _ast

    try:
        tree = _ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid expression syntax: {exc}") from exc

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and node.id not in allowed_keys:
            raise ValueError(
                f"Forbidden name {node.id!r} in expression {expr!r}. {message}"
            )
