"""Version string for Odoo Semantic MCP.

Reads from package metadata (pyproject.toml ``version`` field) so that bumping
the version in one place is enough — no dual-maintenance.
Falls back to ``"unknown"`` for editable installs where package metadata has
not been generated yet (e.g. bare ``pip install -e .`` without build backend).
"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("odoo-semantic-mcp")
except PackageNotFoundError:
    __version__ = "unknown"  # editable install without metadata
