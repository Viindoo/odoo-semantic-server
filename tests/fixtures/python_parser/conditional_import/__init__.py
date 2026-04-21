"""models/__init__.py — conditional import guard for optional_mod."""
from . import base_mod as base_mod

try:
    from . import optional_mod as optional_mod
except ImportError:
    pass
