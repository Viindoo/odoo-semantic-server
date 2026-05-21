# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_ssh_keys.py
"""Tests for SSH key generation — no DB required (unit tests)."""
import os
import unittest.mock as mock

import pytest

from src.web_ui.routes.ssh_keys import (
    decrypt_private_key,
    generate_ed25519_keypair,
    parse_ed25519_private_pem,
)


def _ed25519_openssh_pem() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    private = Ed25519PrivateKey.generate()
    return private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())


def _ed25519_pkcs8_pem() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    private = Ed25519PrivateKey.generate()
    return private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _rsa_pem() -> bytes:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    """Provide a valid FERNET_KEY for tests."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())


class TestGenerateEd25519Keypair:
    def test_returns_public_and_encrypted_private(self):
        pub, enc = generate_ed25519_keypair()
        assert pub.startswith("ssh-ed25519 ")
        assert isinstance(enc, str)
        assert len(enc) > 50

    def test_round_trip_decrypt(self):
        pub, enc = generate_ed25519_keypair()
        private_pem = decrypt_private_key(enc)
        assert b"OPENSSH PRIVATE KEY" in private_pem

    def test_two_calls_different_keys(self):
        pub1, _ = generate_ed25519_keypair()
        pub2, _ = generate_ed25519_keypair()
        assert pub1 != pub2

    def test_missing_fernet_key_raises(self, monkeypatch):
        monkeypatch.delenv("FERNET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            generate_ed25519_keypair()

    def test_decrypt_with_wrong_key_raises(self):
        from cryptography.fernet import Fernet, InvalidToken

        _, enc = generate_ed25519_keypair()
        # Override with a different key — decryption must fail
        with mock.patch.dict(os.environ, {"FERNET_KEY": Fernet.generate_key().decode()}):
            with pytest.raises((InvalidToken, Exception)):
                decrypt_private_key(enc)


class TestImportEd25519Keypair:
    def test_import_round_trip_openssh_pem(self):
        pem = _ed25519_openssh_pem()
        pub, enc = parse_ed25519_private_pem(pem)
        assert pub.startswith("ssh-ed25519 ")
        assert isinstance(enc, str) and len(enc) > 50
        # Decrypt round-trips back to a usable PEM
        assert b"OPENSSH PRIVATE KEY" in decrypt_private_key(enc)

    def test_import_accepts_traditional_pkcs8_pem(self):
        pem = _ed25519_pkcs8_pem()
        pub, enc = parse_ed25519_private_pem(pem)
        assert pub.startswith("ssh-ed25519 ")
        # Re-serialized to OpenSSH on storage for consistency with generate flow
        assert b"OPENSSH PRIVATE KEY" in decrypt_private_key(enc)

    def test_import_rejects_rsa(self):
        with pytest.raises(ValueError, match="Ed25519"):
            parse_ed25519_private_pem(_rsa_pem())

    def test_import_rejects_garbage(self):
        with pytest.raises(ValueError, match="parse"):
            parse_ed25519_private_pem(b"not a key at all")

    def test_import_missing_fernet_raises(self, monkeypatch):
        monkeypatch.delenv("FERNET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            parse_ed25519_private_pem(_ed25519_openssh_pem())
