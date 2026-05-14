# src/web_ui/routes/ssh_keys.py
"""SSH key pair management — generate Ed25519 keypair, store Fernet-encrypted (M8 W1 pure JSON)."""
import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.web_ui._json import _json_safe

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ssh-keys")


def _json_safe_keys(keys) -> list[dict]:
    """Convert datetime fields in SSH key dicts to ISO strings.

    Delegates to the shared _json_safe helper from src.web_ui._json.
    Kept as a thin wrapper for call-site compatibility (callers pass a list
    and expect a list[dict] back).

    Why: ``auth_store().list_ssh_keys()`` returns rows whose ``created_at``
    column is a ``datetime`` from psycopg2. Stdlib ``json`` (used by
    ``JSONResponse``) cannot serialize ``datetime`` and raises ``TypeError``,
    which surfaces as a generic 500 ``Internal Server Error`` from FastAPI
    (no traceback at default log level) — exactly the failure mode the M8 W7
    SSH browser tests hit: POST ``/api/ssh-keys`` 500 → JS never runs the
    ``res.ok && data.public_key`` branch → ``new-pubkey-banner`` stays hidden.
    """
    return [_json_safe(dict(k)) for k in keys]


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


class CreateSshKeyBody(BaseModel):
    name: str


class ImportSshKeyBody(BaseModel):
    name: str
    private_key_pem: str


@router.get("")
async def list_ssh_keys(request: Request):
    """Return list of SSH keys as JSON."""
    keys = []
    error = None
    fernet_missing = not os.getenv("FERNET_KEY")

    try:
        from src.db.pg import auth_store

        keys = auth_store().list_ssh_keys()
    except Exception as e:
        error = str(e)

    return JSONResponse({
        "keys": _json_safe_keys(keys),
        "fernet_missing": fernet_missing,
        "error": error,
    })


@router.post("")
async def create_ssh_key(body: CreateSshKeyBody, request: Request):
    """Generate a new Ed25519 keypair, store encrypted, return public key once."""
    error = None
    new_public_key = None
    keys = []
    fernet_missing = not os.getenv("FERNET_KEY")

    if fernet_missing:
        return JSONResponse(
            {"error": "FERNET_KEY is not set. Cannot store SSH keys securely."},
            status_code=500,
        )

    try:
        from src.db.pg import auth_store

        pub, encrypted = generate_ed25519_keypair()
        auth_store().save_ssh_key(name=body.name, public_key=pub, private_key_encrypted=encrypted)
        new_public_key = pub
        keys = auth_store().list_ssh_keys()
    except RuntimeError as e:
        error = str(e)
        fernet_missing = True
    except Exception as e:
        error = str(e)

    if error:
        return JSONResponse({"error": error, "fernet_missing": fernet_missing}, status_code=500)

    return JSONResponse({"ok": True, "public_key": new_public_key, "keys": _json_safe_keys(keys)})


@router.post("/import")
async def import_ssh_key(body: ImportSshKeyBody, request: Request):
    """Import an existing Ed25519 private key (PEM). Server derives public key,
    validates Ed25519, encrypts with Fernet, stores in DB."""
    error = None
    new_public_key = None
    keys = []
    fernet_missing = not os.getenv("FERNET_KEY")

    if fernet_missing:
        return JSONResponse(
            {"error": "FERNET_KEY is not set. Cannot store SSH keys securely."},
            status_code=500,
        )

    if not body.name.strip() or not body.private_key_pem.strip():
        return JSONResponse(
            {"error": "Both name and private key PEM are required."},
            status_code=422,
        )

    try:
        from src.db.pg import auth_store

        pub, encrypted = parse_ed25519_private_pem(body.private_key_pem.encode())
        auth_store().save_ssh_key(name=body.name, public_key=pub, private_key_encrypted=encrypted)
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

    if error:
        return JSONResponse({"error": error, "fernet_missing": fernet_missing}, status_code=422)

    return JSONResponse({"ok": True, "public_key": new_public_key, "keys": _json_safe_keys(keys)})


@router.delete("/{key_id}")
async def delete_ssh_key(request: Request, key_id: int):
    """Delete an SSH key pair by id."""
    try:
        from src.db.pg import auth_store

        auth_store().delete_ssh_key(key_id)
        _logger.info("SSH key %s deleted", key_id)
    except Exception as e:
        _logger.warning("Delete SSH key %s failed: %s", key_id, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})
