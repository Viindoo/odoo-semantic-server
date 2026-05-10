"""Lifecycle tests for src.cloner background clone job.

Tests are pure-unit (no Docker required): DB calls are mocked via patch.
The six tests cover:
    1. Unknown repo → exit code 2
    2. HTTPS clone success → status 'cloned', local_path written, exit 0
    3. Clone failure → status 'error', error_msg set, exit 1
    4. SSH URL without ssh_key_id → status 'error', exit 2
    5. SSH URL with key → decrypt called + clone_repo receives private_key_pem, exit 0
    6. Status transition order: pending then cloned (no other transitions)
"""
from unittest.mock import MagicMock, patch

from src.cloner.__main__ import main

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HTTPS_REPO = {
    "id": 1,
    "url": "https://github.com/odoo/odoo.git",
    "branch": "17.0",
    "profile_name": "odoo17",
    "ssh_key_id": None,
    "local_path": "",
    "clone_status": "manual",
}

_SSH_REPO = {
    "id": 2,
    "url": "git@github.com:Viindoo/viindoo.git",
    "branch": "17.0",
    "profile_name": "viindoo17",
    "ssh_key_id": 42,
    "local_path": "",
    "clone_status": "manual",
}

_SSH_REPO_NO_KEY = {
    "id": 3,
    "url": "git@github.com:Viindoo/viindoo.git",
    "branch": "17.0",
    "profile_name": "viindoo17",
    "ssh_key_id": None,   # <- no key set despite SSH URL
    "local_path": "",
    "clone_status": "manual",
}

_SSH_KEY_ROW = {
    "id": 42,
    "name": "deploy",
    "public_key": "ssh-ed25519 AAAA...",
    "private_key_encrypted": "gAAAAA...",
    "key_version": 1,
}


def _make_conn():
    """Return an autocommit-style mock psycopg2 connection."""
    conn = MagicMock()
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# 1. Unknown repo → exit code 2
# ---------------------------------------------------------------------------

def test_main_unknown_repo_returns_2():
    conn = _make_conn()
    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=None),
    ):
        rc = main(["--repo-id", "99999"])
    assert rc == 2


# ---------------------------------------------------------------------------
# 2. HTTPS clone success → exit 0, status transitions, local_path written
# ---------------------------------------------------------------------------

def test_main_https_clone_success(tmp_path):
    conn = _make_conn()
    statuses: list[tuple] = []

    def fake_set_status(c, rid, status, error_msg=None):
        statuses.append((status, error_msg))

    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=dict(_HTTPS_REPO)),
        patch("src.cloner.__main__.set_clone_status", side_effect=fake_set_status),
        patch("src.cloner.__main__.clone_repo") as fake_clone,
        patch("src.cloner.__main__.update_repo_local_path") as fake_update,
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "1"])

    assert rc == 0
    fake_clone.assert_called_once()
    fake_update.assert_called_once_with(conn, 1, str(tmp_path / "cloned"))
    # Final status must be 'cloned'
    assert statuses[-1][0] == "cloned"


# ---------------------------------------------------------------------------
# 3. Clone failure → exit 1, status 'error', error_msg captured
# ---------------------------------------------------------------------------

def test_main_clone_failure_sets_error_status(tmp_path):
    conn = _make_conn()
    statuses: list[tuple] = []

    def fake_set_status(c, rid, status, error_msg=None):
        statuses.append((status, error_msg))

    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=dict(_HTTPS_REPO)),
        patch("src.cloner.__main__.set_clone_status", side_effect=fake_set_status),
        patch(
            "src.cloner.__main__.clone_repo",
            side_effect=Exception("git: timeout"),
        ),
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "1"])

    assert rc == 1
    # Last status must be 'error' with the exception message
    last_status, last_msg = statuses[-1]
    assert last_status == "error"
    assert last_msg is not None and "timeout" in last_msg


# ---------------------------------------------------------------------------
# 4. SSH URL without ssh_key_id → exit 2, status 'error'
# ---------------------------------------------------------------------------

def test_main_ssh_url_without_key_id_returns_2():
    conn = _make_conn()
    statuses: list[tuple] = []

    def fake_set_status(c, rid, status, error_msg=None):
        statuses.append((status, error_msg))

    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=dict(_SSH_REPO_NO_KEY)),
        patch("src.cloner.__main__.set_clone_status", side_effect=fake_set_status),
    ):
        rc = main(["--repo-id", "3"])

    assert rc == 2
    assert statuses  # at least one status written
    assert statuses[-1][0] == "error"
    assert "ssh_key_id" in (statuses[-1][1] or "")


# ---------------------------------------------------------------------------
# 5. SSH URL with key → decrypt called + clone_repo gets private_key_pem
# ---------------------------------------------------------------------------

def test_main_ssh_url_with_key_decrypts_and_clones(tmp_path):
    conn = _make_conn()

    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=dict(_SSH_REPO)),
        patch("src.cloner.__main__.get_ssh_key_by_id", return_value=dict(_SSH_KEY_ROW)),
        patch("src.cloner.__main__.set_clone_status"),
        patch("src.cloner.__main__.update_repo_local_path"),
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
        patch(
            "src.cloner.__main__.decrypt_private_key",
            return_value=b"fake-pem",
        ) as fake_decrypt,
        patch("src.cloner.__main__.clone_repo") as fake_clone,
    ):
        rc = main(["--repo-id", "2"])

    assert rc == 0
    fake_decrypt.assert_called_once_with(_SSH_KEY_ROW["private_key_encrypted"])
    fake_clone.assert_called_once()
    assert fake_clone.call_args.kwargs["private_key_pem"] == b"fake-pem"


# ---------------------------------------------------------------------------
# 6. Status transition order: pending then cloned
# ---------------------------------------------------------------------------

def test_main_lifecycle_status_transitions(tmp_path):
    """Verify status transitions: pending → cloned (no others)."""
    conn = _make_conn()
    observed: list[str] = []

    def fake_set_status(c, rid, status, error_msg=None):
        observed.append(status)

    with (
        patch("src.cloner.__main__._open_conn", return_value=conn),
        patch("src.cloner.__main__.get_repo_by_id", return_value=dict(_HTTPS_REPO)),
        patch("src.cloner.__main__.set_clone_status", side_effect=fake_set_status),
        patch("src.cloner.__main__.clone_repo"),
        patch("src.cloner.__main__.update_repo_local_path"),
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "1"])

    assert rc == 0
    assert observed == ["pending", "cloned"]
