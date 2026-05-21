# SPDX-License-Identifier: AGPL-3.0-or-later
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


def _make_repo_store(**kwargs):
    """Return a MagicMock RepoStore pre-configured with return values."""
    store = MagicMock()
    for attr, val in kwargs.items():
        if callable(val):
            getattr(store, attr).side_effect = val
        else:
            getattr(store, attr).return_value = val
    return store


def _make_auth_store(**kwargs):
    store = MagicMock()
    for attr, val in kwargs.items():
        if callable(val):
            getattr(store, attr).side_effect = val
        else:
            getattr(store, attr).return_value = val
    return store


# ---------------------------------------------------------------------------
# 1. Unknown repo → exit code 2
# ---------------------------------------------------------------------------

def test_main_unknown_repo_returns_2():
    repo_s = _make_repo_store(get_repo_by_id=None)
    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
    ):
        rc = main(["--repo-id", "99999"])
    assert rc == 2


# ---------------------------------------------------------------------------
# 2. HTTPS clone success → exit 0, status transitions, local_path written
# ---------------------------------------------------------------------------

def test_main_https_clone_success(tmp_path):
    statuses: list[tuple] = []

    def fake_set_status(rid, status, error_msg=None):
        statuses.append((status, error_msg))

    repo_s = _make_repo_store(
        get_repo_by_id=dict(_HTTPS_REPO),
        set_clone_status=fake_set_status,
    )

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
        patch("src.cloner.__main__.clone_repo") as fake_clone,
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "1"])

    assert rc == 0
    fake_clone.assert_called_once()
    repo_s.update_repo_local_path.assert_called_once_with(1, str(tmp_path / "cloned"))
    # Final status must be 'cloned'
    assert statuses[-1][0] == "cloned"


# ---------------------------------------------------------------------------
# 3. Clone failure → exit 1, status 'error', error_msg captured
# ---------------------------------------------------------------------------

def test_main_clone_failure_sets_error_status(tmp_path):
    statuses: list[tuple] = []

    def fake_set_status(rid, status, error_msg=None):
        statuses.append((status, error_msg))

    repo_s = _make_repo_store(
        get_repo_by_id=dict(_HTTPS_REPO),
        set_clone_status=fake_set_status,
    )

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
        patch("src.cloner.__main__.clone_repo", side_effect=Exception("git: timeout")),
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
    statuses: list[tuple] = []

    def fake_set_status(rid, status, error_msg=None):
        statuses.append((status, error_msg))

    repo_s = _make_repo_store(
        get_repo_by_id=dict(_SSH_REPO_NO_KEY),
        set_clone_status=fake_set_status,
    )

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
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
    repo_s = _make_repo_store(get_repo_by_id=dict(_SSH_REPO))
    auth_s = _make_auth_store(get_ssh_key_by_id=dict(_SSH_KEY_ROW))

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
        patch("src.db.pg.auth_store", return_value=auth_s),
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
    observed: list[str] = []

    def fake_set_status(rid, status, error_msg=None):
        observed.append(status)

    repo_s = _make_repo_store(
        get_repo_by_id=dict(_HTTPS_REPO),
        set_clone_status=fake_set_status,
    )

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
        patch("src.cloner.__main__.clone_repo"),
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "1"])

    assert rc == 0
    assert observed == ["pending", "cloned"]


# ---------------------------------------------------------------------------
# 7. file:// URL does NOT trigger the SSH-key gate (exit code must not be 2)
# ---------------------------------------------------------------------------

_FILE_REPO = {
    "id": 10,
    "url": "file:///tmp/some_local_repo",
    "branch": "17.0",
    "profile_name": "odoo17",
    "ssh_key_id": None,    # no SSH key — this is fine for file:// URLs
    "local_path": "",
    "clone_status": "manual",
}


def test_file_url_does_not_require_ssh_key(tmp_path):
    """file:// URL with ssh_key_id=NULL must not exit 2 (SSH-key-missing gate).

    The cloner will attempt git clone on the file:// path; that may fail because
    no real repo exists at /tmp/some_local_repo, but the SSH-key guard (which
    returns exit 2) must NOT be triggered.  We mock clone_repo to raise an
    exception (simulating a missing git repo) so exit code is 1, not 2.
    """
    statuses: list[str] = []

    def fake_set_status(rid, status, error_msg=None):
        statuses.append(status)

    repo_s = _make_repo_store(
        get_repo_by_id=dict(_FILE_REPO),
        set_clone_status=fake_set_status,
    )

    with (
        patch("src.cloner.__main__._init_pg"),
        patch("src.db.pg.repo_store", return_value=repo_s),
        patch(
            "src.cloner.__main__.clone_repo",
            side_effect=Exception("not a git repo"),
        ),
        patch("src.cloner.__main__.default_clone_dir", return_value=tmp_path / "cloned"),
    ):
        rc = main(["--repo-id", "10"])

    # SSH-key gate returns 2; clone failure returns 1. We must NOT get 2.
    assert rc != 2, "file:// URL must not trip the SSH-key-missing gate (exit 2)"
