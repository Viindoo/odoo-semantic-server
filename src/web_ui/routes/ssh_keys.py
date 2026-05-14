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


def parse_ed25519_private_pem(pem: bytes) -> tuple[str, str]:
    """Parse a user-supplied private key PEM. Validate Ed25519, derive public key,
    re-serialize to OpenSSH PEM and encrypt with Fernet.

    Accepts both OpenSSH PEM (BEGIN OPENSSH PRIVATE KEY) and traditional PEM
    (PKCS8 / PKCS1). Raises ValueError if the key is not Ed25519 or unparseable.
    Raises RuntimeError if FERNET_KEY not set.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_ssh_private_key,
    )

    private = None
    last_err: Exception | None = None
    for loader in (load_ssh_private_key, load_pem_private_key):
        try:
            private = loader(pem, password=None)
            break
        except Exception as e:
            last_err = e
            continue

    if private is None:
        raise ValueError(f"Could not parse private key PEM: {last_err}")

    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 keys are supported")

    pub = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    priv_pem = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    encrypted = _get_fernet().encrypt(priv_pem).decode()
    return pub, encrypted


@router.get("/ssh-keys", response_class=HTMLResponse)
async def ssh_keys_page(request: Request):
    """Render SSH keys management page."""
    templates = request.app.state.templates
    keys = []
    error = None
    fernet_missing = not os.getenv("FERNET_KEY")

    try:
        from src.db.pg import auth_store

        keys = auth_store().list_ssh_keys()
    except Exception as e:
        error = str(e)

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
        try:
            from src.db.pg import auth_store

            pub, encrypted = generate_ed25519_keypair()
            auth_store().save_ssh_key(name=name, public_key=pub, private_key_encrypted=encrypted)
            new_public_key = pub
            keys = auth_store().list_ssh_keys()
        except RuntimeError as e:
            error = str(e)
            fernet_missing = True
        except Exception as e:
            error = str(e)

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


@router.post("/ssh-keys/import", response_class=HTMLResponse)
async def import_ssh_key(
    request: Request,
    name: Annotated[str, Form()],
    private_key_pem: Annotated[str, Form()],
):
    """Import an existing Ed25519 private key (paste PEM). Server derives public key,
    validates Ed25519, encrypts with Fernet, stores in DB."""
    templates = request.app.state.templates
    keys = []
    error = None
    new_public_key = None
    fernet_missing = not os.getenv("FERNET_KEY")

    if fernet_missing:
        error = "FERNET_KEY is not set. Cannot store SSH keys securely."
    elif not name.strip() or not private_key_pem.strip():
        error = "Both name and private key PEM are required."
    else:
        try:
            from src.db.pg import auth_store

            pub, encrypted = parse_ed25519_private_pem(private_key_pem.encode())
            auth_store().save_ssh_key(name=name, public_key=pub, private_key_encrypted=encrypted)
            new_public_key = pub
        except ValueError as e:
            error = str(e)
        except RuntimeError as e:
            error = str(e)
            fernet_missing = True
        except Exception as e:
            error = str(e)

    try:
        from src.db.pg import auth_store

        keys = auth_store().list_ssh_keys()
    except Exception as e:
        if not error:
            error = str(e)

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
    try:
        from src.db.pg import auth_store

        auth_store().delete_ssh_key(key_id)
        _logger.info("SSH key %s deleted", key_id)
    except Exception as e:
        _logger.warning("Delete SSH key %s failed: %s", key_id, e)
    return RedirectResponse("/ssh-keys", status_code=303)
