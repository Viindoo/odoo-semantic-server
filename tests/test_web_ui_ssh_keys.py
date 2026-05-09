# tests/test_web_ui_ssh_keys.py
"""Tests for SSH key generation — no DB required (unit tests)."""
import os
import unittest.mock as mock

import pytest

from src.web_ui.routes.ssh_keys import decrypt_private_key, generate_ed25519_keypair


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
