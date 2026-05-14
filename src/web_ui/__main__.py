# src/web_ui/__main__.py
"""Start Web UI server.

Binds to 127.0.0.1 only. Public access happens via nginx reverse-proxy that
forwards ``/api/*`` to this service (see ``docs/deploy/nginx-m8.conf``).
``proxy_headers=False`` keeps uvicorn from rewriting ``request.client.host``
to the X-Forwarded-For value, so :class:`_LoopbackOnlyMiddleware` continues
to gate on the real TCP peer (nginx loopback). Real client IP is recovered
from the X-Real-IP header where genuinely needed (see ``routes/login.py``).
"""
import logging
import os
import sys

import uvicorn

from src.logging_config import configure_logging
from src.web_ui.app import create_app


def main() -> None:
    # Hard-code 127.0.0.1 — no Web UI auth in M5, must not be publicly accessible.
    # Admin on remote server: use SSH tunnel (ssh -L 8003:127.0.0.1:8003 server).
    configure_logging(level=logging.INFO)
    if not os.getenv("FERNET_KEY"):
        print(
            "WARNING: FERNET_KEY not set. SSH key storage will be disabled.",
            file=sys.stderr,
        )
        print(
            "  Generate: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"",
            file=sys.stderr,
        )
    host = "127.0.0.1"
    port = 8003
    app = create_app()
    # proxy_headers=False — see module docstring for rationale. Trusting
    # X-Forwarded-For from nginx would rewrite scope["client"] and trip
    # _LoopbackOnlyMiddleware with 403 for every external /api/* request.
    uvicorn.run(app, host=host, port=port, access_log=True, proxy_headers=False)


if __name__ == "__main__":
    sys.exit(main())
