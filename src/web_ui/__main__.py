# src/web_ui/__main__.py
"""Start Web UI server. Binds to 127.0.0.1 only (no auth M5 — not safe to expose)."""
import sys

import uvicorn

from src.web_ui.app import create_app


def main() -> None:
    # Hard-code 127.0.0.1 — no Web UI auth in M5, must not be publicly accessible.
    # Admin on remote server: use SSH tunnel (ssh -L 8003:127.0.0.1:8003 server).
    host = "127.0.0.1"
    port = 8003
    app = create_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    sys.exit(main())
