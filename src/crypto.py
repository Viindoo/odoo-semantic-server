# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central FERNET key provider (WI-7 hardening, ADR-0020 update).

Single source of truth for FERNET_KEY resolution across all modules.

Resolution order (first wins):
  1. ``$CREDENTIALS_DIRECTORY/FERNET_KEY`` — systemd LoadCredential (preferred
     in production: never touches process env or cmdline).
  2. ``$FERNET_KEY`` environment variable — backward-compatible fallback.

Backward compatibility: existing deployments using ``EnvironmentFile=`` +
``FERNET_KEY=...`` continue to work without any change.

Two calling conventions:

* **Route handlers / feature code** — call ``get_fernet()`` (or
  ``get_fernet_key(require=True)``).  Both raise ``RuntimeError`` immediately
  when neither key source is configured.

* **Startup code** (``src/web_ui/__main__.py``) — calls
  ``get_fernet_key()`` (``require=False``) and checks the return value
  manually.  On ``None`` in production it calls ``raise SystemExit(1)`` itself;
  in dev mode it logs a warning and continues with SSH/TOTP features disabled.
  This avoids a bare ``RuntimeError`` leaking into the uvicorn startup log.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def get_fernet_key(*, require: bool = False) -> str | None:
    """Return the raw FERNET_KEY string (URL-safe base64, 44 chars).

    Resolution order:
      1. ``$CREDENTIALS_DIRECTORY/FERNET_KEY`` (systemd LoadCredential).
      2. ``$FERNET_KEY`` environment variable.

    Args:
        require: If True, raise RuntimeError when the key is absent instead
            of returning None.  Use this in startup assertions and in
            functions that unconditionally need the key.

    Returns:
        The key string, or None when absent and ``require=False``.

    Raises:
        RuntimeError: When absent and ``require=True``.
    """
    # 1. systemd LoadCredential path
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds_dir:
        cred_path = Path(creds_dir) / "FERNET_KEY"
        if cred_path.exists():
            key = cred_path.read_text().strip()
            if key:
                logger.debug("FERNET_KEY loaded from CREDENTIALS_DIRECTORY")
                return key
            logger.warning(
                "CREDENTIALS_DIRECTORY/FERNET_KEY exists but is empty — "
                "falling back to FERNET_KEY env var"
            )

    # 2. Environment variable fallback
    key = os.environ.get("FERNET_KEY")
    if key:
        return key

    if require:
        raise RuntimeError(
            "FERNET_KEY is not set. "
            "Either set the FERNET_KEY environment variable or configure "
            "LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY in the "
            "systemd unit. "
            "Generate a key: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return None


def get_fernet():
    """Return a ready-to-use ``cryptography.fernet.Fernet`` instance.

    Calls ``get_fernet_key(require=True)`` — raises ``RuntimeError`` if the
    key is absent.

    Returns:
        ``Fernet`` instance keyed with the resolved FERNET_KEY.
    """
    from cryptography.fernet import Fernet

    key = get_fernet_key(require=True)
    return Fernet(key.encode() if isinstance(key, str) else key)
