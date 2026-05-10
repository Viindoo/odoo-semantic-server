"""Test scanner.get_module_commit_sha() helper (M6 W2-2)."""
import subprocess
from pathlib import Path

import pytest

from src.indexer.scanner import get_module_commit_sha


def _git(cwd: Path, *args: str) -> str:
    """Run git command, return stdout."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo_with_module(tmp_path: Path) -> Path:
    """Create a tempdir git repo with addons/sale module."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    module_dir = tmp_path / "addons" / "sale"
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text("{'name': 'sale'}")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add sale module")
    return tmp_path


def test_get_module_commit_sha_returns_hex(repo_with_module: Path):
    sha = get_module_commit_sha(repo_with_module, Path("addons/sale"))
    assert sha is not None
    assert len(sha) == 40  # SHA-1 hex
    assert all(c in "0123456789abcdef" for c in sha)


def test_get_module_commit_sha_returns_none_on_empty_repo(tmp_path: Path):
    """Empty repo (init but no commits) returns None gracefully."""
    _git(tmp_path, "init", "-b", "main")
    sha = get_module_commit_sha(tmp_path, Path("addons/sale"))
    assert sha is None


def test_get_module_commit_sha_returns_none_outside_repo(tmp_path: Path):
    """Path that's not a git repo returns None."""
    sha = get_module_commit_sha(tmp_path, Path("addons/sale"))
    assert sha is None


def test_get_module_commit_sha_returns_none_for_nonexistent_path(repo_with_module: Path):
    """Path that doesn't exist in repo returns None (or empty stdout)."""
    sha = get_module_commit_sha(repo_with_module, Path("addons/nonexistent_module"))
    assert sha is None
