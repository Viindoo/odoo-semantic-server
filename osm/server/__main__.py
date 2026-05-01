"""Entrypoint so `python -m osm.server` boots the FastMCP server."""

from __future__ import annotations

import sys

from osm.server.app import main

if __name__ == "__main__":
    sys.exit(main())
