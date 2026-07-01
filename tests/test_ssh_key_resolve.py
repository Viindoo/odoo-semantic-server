# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src.ssh_key_resolve.resolve_ssh_key_pem (shared SSH-key SSOT).

Pure unit (no Docker / DB / network): auth_store + decrypt_private_key are mocked.
NEVER mark neo4j/postgres on this box (shared prod DB).

Business rules (the resolver's contract, shared by the cloner AND the nightly
pre-scan refresh):
  1. HTTPS url -> None (no SSH credential needed).
  2. SSH url + usable key row -> decrypt_private_key(private_key_encrypted).
  3. SSH url + ssh_key_id is None -> SshKeyUnavailable (surfaced, never keyless).
  4. SSH url + ssh_key_id set but row missing -> SshKeyUnavailable.
  5. Decrypt/DB errors propagate (RuntimeError etc.) - a genuine failure, distinct
     from the benign SshKeyUnavailable.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.ssh_key_resolve import SshKeyUnavailable, resolve_ssh_key_pem


def test_https_url_returns_none():
    """Rule 1: an HTTPS url needs no SSH key -> None, no auth_store lookup."""
    with patch("src.db.pg.auth_store") as auth:
        result = resolve_ssh_key_pem(
            {"id": 1, "url": "https://github.com/o/r.git", "ssh_key_id": None}
        )
    assert result is None
    auth.assert_not_called()


def test_ssh_url_with_key_decrypts():
    """Rule 2: SSH url + usable key row -> decrypted PEM via the SSOT."""
    fake_auth = MagicMock()
    fake_auth.get_ssh_key_by_id.return_value = {"private_key_encrypted": "ENC"}

    with (
        patch("src.db.pg.auth_store", return_value=fake_auth),
        patch("src.ssh_key_resolve.decrypt_private_key", return_value=b"PEM") as dec,
    ):
        result = resolve_ssh_key_pem(
            {"id": 2, "url": "git@github.com:o/r.git", "ssh_key_id": 9}
        )

    assert result == b"PEM"
    fake_auth.get_ssh_key_by_id.assert_called_once_with(9)
    dec.assert_called_once_with("ENC")


def test_ssh_url_without_ssh_key_id_raises():
    """Rule 3: SSH url but ssh_key_id None -> SshKeyUnavailable (no keyless fetch)."""
    with patch("src.db.pg.auth_store") as auth:
        with pytest.raises(SshKeyUnavailable, match="no ssh_key_id"):
            resolve_ssh_key_pem(
                {"id": 3, "url": "git@github.com:o/r.git", "ssh_key_id": None}
            )
    # ssh_key_id None short-circuits before any DB lookup.
    auth.assert_not_called()


def test_ssh_url_missing_key_row_raises():
    """Rule 4: SSH url + ssh_key_id set but row missing -> SshKeyUnavailable."""
    fake_auth = MagicMock()
    fake_auth.get_ssh_key_by_id.return_value = None

    with patch("src.db.pg.auth_store", return_value=fake_auth):
        with pytest.raises(SshKeyUnavailable, match="not found"):
            resolve_ssh_key_pem(
                {"id": 4, "url": "ssh://git@host/o/r.git", "ssh_key_id": 77}
            )
    fake_auth.get_ssh_key_by_id.assert_called_once_with(77)


def test_decrypt_error_propagates():
    """Rule 5: a decrypt failure (e.g. FERNET_KEY absent) propagates as RuntimeError,
    NOT swallowed as SshKeyUnavailable - the caller treats it as a genuine failure."""
    fake_auth = MagicMock()
    fake_auth.get_ssh_key_by_id.return_value = {"private_key_encrypted": "ENC"}

    with (
        patch("src.db.pg.auth_store", return_value=fake_auth),
        patch(
            "src.ssh_key_resolve.decrypt_private_key",
            side_effect=RuntimeError("FERNET_KEY is not set"),
        ),
    ):
        with pytest.raises(RuntimeError, match="FERNET_KEY"):
            resolve_ssh_key_pem(
                {"id": 5, "url": "git@github.com:o/r.git", "ssh_key_id": 1}
            )
