# SPDX-License-Identifier: AGPL-3.0-or-later
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

log = logging.getLogger(__name__)


def check_env_file_perms(path: str) -> None:
    """Check that an environment file is not world/group-readable.

    If ``WEBUI_ENV_FILE`` is set, call this helper at startup to abort if the
    file has group or other read permissions (mode bits ``& 0o077 != 0``).

    Args:
        path: Filesystem path to the env file (e.g. ``/etc/odoo-semantic/webui.env``).

    Raises:
        SystemExit: If the file has insecure permissions.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        log.warning("WEBUI_ENV_FILE %s not accessible: %s", path, exc)
        return
    if mode & 0o077:
        log.error(
            "WEBUI_ENV_FILE %s has insecure permissions %s — "
            "must be mode 0600 (owner-read-only). Aborting.",
            path,
            oct(mode & 0o777),
        )
        raise SystemExit(1)


def main() -> None:
    # Hard-code 127.0.0.1 — no Web UI auth in M5, must not be publicly accessible.
    # Admin on remote server: use SSH tunnel (ssh -L 8003:127.0.0.1:8003 server).
    configure_logging(level=logging.INFO)

    # Optional: check env file permissions if WEBUI_ENV_FILE is provided.
    env_file = os.getenv("WEBUI_ENV_FILE")
    if env_file:
        check_env_file_perms(env_file)

    if not os.getenv("FERNET_KEY"):
        if os.getenv("ENVIRONMENT", "").lower() == "production":
            log.error(
                "FERNET_KEY required in production. "
                "Generate: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
            raise SystemExit(1)
        else:
            log.warning(
                "FERNET_KEY unset — SSH key features disabled (dev mode). "
                "Generate: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
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
