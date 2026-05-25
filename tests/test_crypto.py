# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_crypto.py
"""Unit tests for src.crypto — central FERNET key provider (WI-7 / ADR-0020).

Business rules under test:
  1. CREDENTIALS_DIRECTORY/FERNET_KEY file takes priority over FERNET_KEY env var.
  2. Falls back to FERNET_KEY env var when CREDENTIALS_DIRECTORY is absent.
  3. Returns None when neither source is available and require=False.
  4. Raises RuntimeError when neither source is available and require=True.
  5. get_fernet() returns a usable Fernet instance (encrypt/decrypt round-trip).
  6. Empty credential file falls through to env var fallback.
"""
import pytest
from cryptography.fernet import Fernet


class TestGetFernetKeyCredentialsDirectory:
    """Rule 1: CREDENTIALS_DIRECTORY/FERNET_KEY takes precedence over env var."""

    def test_reads_from_credentials_directory(self, tmp_path, monkeypatch):
        """CREDENTIALS_DIRECTORY set + file exists → key from file."""
        from src.crypto import get_fernet_key

        key = Fernet.generate_key().decode()
        cred_file = tmp_path / "FERNET_KEY"
        cred_file.write_text(key)

        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        monkeypatch.delenv("FERNET_KEY", raising=False)

        result = get_fernet_key()
        assert result == key

    def test_credentials_directory_takes_priority_over_env(self, tmp_path, monkeypatch):
        """Key in CREDENTIALS_DIRECTORY wins over FERNET_KEY env var."""
        from src.crypto import get_fernet_key

        file_key = Fernet.generate_key().decode()
        env_key = Fernet.generate_key().decode()
        assert file_key != env_key  # sanity

        cred_file = tmp_path / "FERNET_KEY"
        cred_file.write_text(file_key)

        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("FERNET_KEY", env_key)

        result = get_fernet_key()
        assert result == file_key


class TestGetFernetKeyEnvFallback:
    """Rule 2: Falls back to FERNET_KEY env var when CREDENTIALS_DIRECTORY absent."""

    def test_env_var_used_when_no_credentials_dir(self, monkeypatch):
        """No CREDENTIALS_DIRECTORY → use FERNET_KEY env var."""
        from src.crypto import get_fernet_key

        key = Fernet.generate_key().decode()
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.setenv("FERNET_KEY", key)

        result = get_fernet_key()
        assert result == key

    def test_env_var_used_when_credentials_dir_has_no_file(self, tmp_path, monkeypatch):
        """CREDENTIALS_DIRECTORY exists but file absent → fall back to env var."""
        from src.crypto import get_fernet_key

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("FERNET_KEY", key)

        # No FERNET_KEY file in tmp_path
        result = get_fernet_key()
        assert result == key

    def test_empty_credential_file_falls_back_to_env(self, tmp_path, monkeypatch):
        """Empty CREDENTIALS_DIRECTORY/FERNET_KEY file → fall back to env var (Rule 6)."""
        from src.crypto import get_fernet_key

        key = Fernet.generate_key().decode()
        cred_file = tmp_path / "FERNET_KEY"
        cred_file.write_text("   \n")  # whitespace only

        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("FERNET_KEY", key)

        result = get_fernet_key()
        assert result == key


class TestGetFernetKeyAbsent:
    """Rules 3 & 4: Behaviour when neither source is available."""

    def test_returns_none_when_absent_and_require_false(self, monkeypatch):
        """Missing key + require=False → returns None (Rule 3)."""
        from src.crypto import get_fernet_key

        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.delenv("FERNET_KEY", raising=False)

        result = get_fernet_key(require=False)
        assert result is None

    def test_raises_when_absent_and_require_true(self, monkeypatch):
        """Missing key + require=True → raises RuntimeError (Rule 4)."""
        from src.crypto import get_fernet_key

        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.delenv("FERNET_KEY", raising=False)

        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            get_fernet_key(require=True)

    def test_default_require_false(self, monkeypatch):
        """Default require=False: no exception when key absent."""
        from src.crypto import get_fernet_key

        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.delenv("FERNET_KEY", raising=False)

        # Must not raise
        result = get_fernet_key()
        assert result is None


class TestGetFernet:
    """Rule 5: get_fernet() returns a usable Fernet instance."""

    def test_fernet_encrypt_decrypt_roundtrip(self, monkeypatch):
        """get_fernet() returns a Fernet instance that can round-trip plaintext."""
        from src.crypto import get_fernet

        key = Fernet.generate_key().decode()
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.setenv("FERNET_KEY", key)

        f = get_fernet()
        plaintext = b"hello-world-secret"
        token = f.encrypt(plaintext)
        assert f.decrypt(token) == plaintext

    def test_get_fernet_raises_when_key_absent(self, monkeypatch):
        """get_fernet() raises RuntimeError when FERNET_KEY is absent."""
        from src.crypto import get_fernet

        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        monkeypatch.delenv("FERNET_KEY", raising=False)

        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            get_fernet()
