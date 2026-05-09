# src/web_ui/routes/ssh_keys.py
"""SSH key pair management — generate Ed25519 keypair, store Fernet-encrypted."""
import logging
import os
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


def _get_fernet():
    """Return Fernet instance. Raises RuntimeError if FERNET_KEY not set."""
    from cryptography.fernet import Fernet

    key = os.getenv("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "FERNET_KEY is not set. SSH key storage requires FERNET_KEY. "
            "Generate one: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def generate_ed25519_keypair() -> tuple[str, str]:
    """Generate Ed25519 keypair. Return (public_key_openssh, private_key_fernet_encrypted).

    Raises RuntimeError if FERNET_KEY not set.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    pub = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    priv_pem = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    encrypted = _get_fernet().encrypt(priv_pem).decode()
    return pub, encrypted


def decrypt_private_key(encrypted: str) -> bytes:
    """Decrypt a Fernet-encrypted private key. Returns PEM bytes."""
    return _get_fernet().decrypt(encrypted.encode())


def _get_conn():
    import psycopg2

    from src import config

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception:
        return None


@router.get("/ssh-keys", response_class=HTMLResponse)
async def ssh_keys_page(request: Request):
    """Render SSH keys management page."""
    templates = request.app.state.templates
    keys = []
    error = None
    fernet_missing = not os.getenv("FERNET_KEY")

    conn = _get_conn()
    if conn:
        try:
            from src.db.auth_registry import list_ssh_keys

            keys = list_ssh_keys(conn)
        except Exception as e:
            error = str(e)
        finally:
            conn.close()
    else:
        error = "Cannot connect to PostgreSQL."

    return templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "keys": keys,
            "error": error,
            "fernet_missing": fernet_missing,
            "new_public_key": None,
        },
    )


@router.post("/ssh-keys", response_class=HTMLResponse)
async def create_ssh_key(
    request: Request,
    name: Annotated[str, Form()],
):
    """Generate a new Ed25519 keypair, store encrypted, display public key once."""
    templates = request.app.state.templates
    keys = []
    error = None
    new_public_key = None
    fernet_missing = not os.getenv("FERNET_KEY")

    if fernet_missing:
        error = "FERNET_KEY is not set. Cannot store SSH keys securely."
    else:
        conn = _get_conn()
        if conn:
            try:
                pub, encrypted = generate_ed25519_keypair()
                from src.db.auth_registry import list_ssh_keys, save_ssh_key

                save_ssh_key(conn, name=name, public_key=pub, private_key_encrypted=encrypted)
                new_public_key = pub
                keys = list_ssh_keys(conn)
            except RuntimeError as e:
                error = str(e)
                fernet_missing = True
            except Exception as e:
                error = str(e)
            finally:
                conn.close()
        else:
            error = "Cannot connect to PostgreSQL."

    return templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "keys": keys,
            "error": error,
            "fernet_missing": fernet_missing,
            "new_public_key": new_public_key,
        },
    )


@router.post("/ssh-keys/{key_id}/delete", response_class=RedirectResponse)
async def delete_ssh_key(request: Request, key_id: int):
    """Delete an SSH key pair by id."""
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ssh_key_pairs WHERE id = %s", (key_id,))
            _logger.info("SSH key %s deleted", key_id)
        except Exception as e:
            _logger.warning("Delete SSH key %s failed: %s", key_id, e)
        finally:
            conn.close()
    return RedirectResponse("/ssh-keys", status_code=303)
