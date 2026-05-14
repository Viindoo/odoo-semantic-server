# src/web_ui/_json.py
"""JSON-safe conversion for routes returning rows with datetime/Decimal.

Reason: starlette JSONResponse uses raw json.dumps which raises TypeError (500)
on datetime objects. Convert at the route boundary so every route can reuse
one helper instead of each defining its own _json_safe / _serialize_keys variant.

Also covers: date, Decimal, nested dicts, lists, tuples.
"""
import datetime as _dt
from decimal import Decimal
from typing import Any


def _json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-serialisable types to serialisable equivalents.

    - datetime  → ISO-8601 string
    - date      → ISO-8601 string
    - Decimal   → float
    - dict      → dict with values recursively converted
    - list      → list with items recursively converted
    - tuple     → tuple with items recursively converted
    - anything else → returned as-is (must be natively serialisable)
    """
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_json_safe(v) for v in value)
    return value
