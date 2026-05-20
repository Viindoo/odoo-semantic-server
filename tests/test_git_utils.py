"""Tests for src/git_utils.py — URL detection + SSH clone helper."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_utils import (
    clone_repo,
    default_clone_dir,
    is_ssh_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Viindoo/odoo-semantic-server.git",
        "git@gitlab.example.com:viin/some-addon.git",
        "ssh://git@host/path/repo.git",
        "ssh://git@example.com:2222/repo.git",
    ],
)
def test_is_ssh_url_true(url):
    assert is_ssh_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Viindoo/odoo-semantic-server.git",
        "http://example.com/repo.git",
        "file:///tmp/local-bare.git",
        "/tmp/local-path",
    ],
)
def test_is_ssh_url_false(url):
    assert is_ssh_url(url) is False


def test_default_clone_dir_format(tmp_path, monkeypatch):
    # Redirect HOME so we don't pollute the real ~/.local/share
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    p = default_clone_dir("viindoo17", "git@github.com:odoo/odoo.git")
    assert p == tmp_path / ".local/share/odoo-semantic-mcp/clones/viindoo17/odoo"

    p2 = default_clone_dir("p", "https://example.com/Viin/some-addon")
    assert p2.name == "some-addon"


@pytest.mark.parametrize(
    "url,expected_slug",
    [
        ("git@github.com:odoo/odoo.git?token=abc", "odoo"),
        ("https://example.com/Viin/some-addon.git#readme", "some-addon"),
        ("ssh://git@host/path/repo.git?ref=main", "repo"),
        ("git@host:org/repo", "repo"),  # no .git, no query — regression guard
        ("git@github.com:org/repo.git#fragment", "repo"),  # fragment without query
    ],
)
def test_default_clone_dir_strips_query_and_fragment(tmp_path, monkeypatch, url, expected_slug):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = default_clone_dir("p", url)
    assert p.name == expected_slug, f"URL={url} → got {p.name}, expected {expected_slug}"


def test_clone_repo_ssh_writes_tmp_then_cleans(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "out"
    captured_env = {}
    captured_argv = []
    captured_tmp_path = []

    def fake_run(cmd, env=None, check=True, timeout=600):
        captured_env.update(env)
        captured_argv.extend(cmd)
        # Capture key file path BEFORE clone_repo's finally cleanup runs.
        gsc = env.get("GIT_SSH_COMMAND", "")
        import re

        m = re.search(r"-i (\S+)", gsc)
        if m:
            captured_tmp_path.append(m.group(1))
            assert os.path.exists(m.group(1))
            assert oct(os.stat(m.group(1)).st_mode & 0o777) == "0o600"
        # Don't actually run git
        return MagicMock(returncode=0)

    with patch("src.git_utils.subprocess.run", side_effect=fake_run):
        clone_repo(
            "git@example.com:org/repo.git",
            "main",
            target,
            private_key_pem=b"fake-pem-bytes",
        )

    # GIT_SSH_COMMAND set with the tmp key path + accept-new + project-local known_hosts
    assert "GIT_SSH_COMMAND" in captured_env
    gsc = captured_env["GIT_SSH_COMMAND"]
    assert "-i " in gsc
    assert "StrictHostKeyChecking=accept-new" in gsc
    assert "UserKnownHostsFile=" in gsc
    # Verify project-local known_hosts path (not ~/.ssh/known_hosts)
    assert "odoo-semantic-mcp" in gsc

    # Key path NOT in argv (D2)
    for piece in captured_argv:
        assert "osm-ssh-" not in piece, f"key path leaked into argv: {piece}"

    # Tempfile cleaned up after function returns
    assert captured_tmp_path  # we did capture
    assert not os.path.exists(captured_tmp_path[0]), "tmp key file leaked"


def test_clone_repo_cleans_tmp_on_subprocess_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "out"
    captured_tmp_path = []

    def fake_run(cmd, env=None, check=True, timeout=600):
        gsc = env.get("GIT_SSH_COMMAND", "")
        import re

        m = re.search(r"-i (\S+)", gsc)
        if m:
            captured_tmp_path.append(m.group(1))
        raise subprocess.CalledProcessError(128, cmd)

    with patch("src.git_utils.subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError):
            clone_repo(
                "git@example.com:org/repo.git",
                "main",
                target,
                private_key_pem=b"key",
            )

    # Even on subprocess failure, tempfile cleaned up (D3 try/finally)
    assert captured_tmp_path
    assert not os.path.exists(captured_tmp_path[0]), "tmp key leak on failure"


def test_clone_repo_file_url_integration(tmp_path):
    """End-to-end: clone a file:// URL bare repo into tmpdir. No SSH; key is None."""
    # Create a work repo with a commit, then clone it as bare source.
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "-C", str(work), "init", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    (work / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=test@x",
            "-c",
            "user.name=test",
            "commit",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )

    # Clone as bare (acts as the "remote" bare repo)
    src_repo = tmp_path / "source-bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(src_repo)],
        check=True,
        capture_output=True,
    )

    # Now clone into target via clone_repo()
    target = tmp_path / "target"
    clone_repo(
        url=f"file://{src_repo}",
        branch="main",
        target_dir=target,
        private_key_pem=None,
    )
    assert (target / "README.md").read_text() == "hello"
