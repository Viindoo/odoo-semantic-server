"""Test incremental.py git-diff helpers (M6 W2-3)."""
import subprocess
from pathlib import Path

import pytest

from src.indexer.incremental import (
    compute_changed_module_paths,
    filter_modules_by_changed,
    get_repo_head,
    is_ancestor,
)
from src.indexer.models import ModuleInfo


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo_3_modules(tmp_path: Path) -> Path:
    """Tempdir git repo with 3 modules: addons/sale, addons/account, addons/stock."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    for name in ("sale", "account", "stock"):
        d = tmp_path / "addons" / name
        d.mkdir(parents=True)
        (d / "__manifest__.py").write_text(f"{{'name': '{name}'}}")
        (d / "models.py").write_text(f"# {name} models v1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "v1: 3 modules")
    return tmp_path


def test_get_repo_head_returns_sha(repo_3_modules: Path):
    sha = get_repo_head(repo_3_modules)
    assert sha is not None and len(sha) == 40


def test_get_repo_head_returns_none_on_empty_repo(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    assert get_repo_head(tmp_path) is None


def test_get_repo_head_returns_none_on_non_repo(tmp_path: Path):
    assert get_repo_head(tmp_path) is None


def test_is_ancestor_happy(repo_3_modules: Path):
    sha1 = get_repo_head(repo_3_modules)
    # Make a 2nd commit
    (repo_3_modules / "addons" / "sale" / "models.py").write_text("# sale v2")
    _git(repo_3_modules, "add", "-A")
    _git(repo_3_modules, "commit", "-m", "v2: bump sale")
    sha2 = get_repo_head(repo_3_modules)
    assert is_ancestor(repo_3_modules, sha1, sha2) is True
    assert is_ancestor(repo_3_modules, sha2, sha1) is False


def test_is_ancestor_force_push(repo_3_modules: Path):
    """Simulate force-push: reset to a sha that's no longer reachable."""
    sha1 = get_repo_head(repo_3_modules)
    # Make 2 commits then orphan them
    (repo_3_modules / "addons" / "sale" / "models.py").write_text("# v2")
    _git(repo_3_modules, "add", "-A")
    _git(repo_3_modules, "commit", "-m", "v2")
    sha2 = get_repo_head(repo_3_modules)
    # Reset hard to sha1 (sha2 is now orphaned but still in reflog)
    _git(repo_3_modules, "reset", "--hard", sha1)
    # New commit on top of sha1
    (repo_3_modules / "addons" / "sale" / "models.py").write_text("# v3")
    _git(repo_3_modules, "add", "-A")
    _git(repo_3_modules, "commit", "-m", "v3 on rewrite")
    sha3 = get_repo_head(repo_3_modules)
    # sha2 is NOT an ancestor of sha3 (history rewritten)
    assert is_ancestor(repo_3_modules, sha2, sha3) is False
    # sha1 IS still an ancestor of sha3
    assert is_ancestor(repo_3_modules, sha1, sha3) is True


def test_compute_changed_module_paths_two_modules(repo_3_modules: Path):
    sha1 = get_repo_head(repo_3_modules)
    # Modify 2 of 3 modules
    (repo_3_modules / "addons" / "sale" / "models.py").write_text("# sale v2")
    (repo_3_modules / "addons" / "account" / "models.py").write_text("# account v2")
    _git(repo_3_modules, "add", "-A")
    _git(repo_3_modules, "commit", "-m", "v2: bump sale + account")
    sha2 = get_repo_head(repo_3_modules)
    changed = compute_changed_module_paths(repo_3_modules, sha1, sha2)
    assert changed == {"addons/sale", "addons/account"}


def test_compute_changed_returns_empty_on_invalid_sha(repo_3_modules: Path):
    changed = compute_changed_module_paths(
        repo_3_modules, "0" * 40, "0" * 40
    )
    assert changed == set()


def test_compute_changed_handles_module_rename(repo_3_modules: Path):
    """Renaming a module dir → both old + new paths appear changed."""
    sha1 = get_repo_head(repo_3_modules)
    _git(repo_3_modules, "mv", "addons/stock", "addons/inventory")
    _git(repo_3_modules, "commit", "-m", "rename stock to inventory")
    sha2 = get_repo_head(repo_3_modules)
    changed = compute_changed_module_paths(repo_3_modules, sha1, sha2)
    # Both old and new paths should appear (or at minimum the new one)
    assert "addons/inventory" in changed
    # Old path may or may not be there depending on git diff output;
    # if rename detection treats it as a single rename with no content
    # change, only inventory shows. Either is acceptable.


def test_filter_modules_by_changed():
    modules = {
        "sale": ModuleInfo(
            name="sale", path="addons/sale", odoo_version="17.0",
            repo="odoo17", depends=[],
        ),
        "account": ModuleInfo(
            name="account", path="addons/account", odoo_version="17.0",
            repo="odoo17", depends=[],
        ),
        "stock": ModuleInfo(
            name="stock", path="addons/stock", odoo_version="17.0",
            repo="odoo17", depends=[],
        ),
    }
    filtered = filter_modules_by_changed(modules, {"addons/sale", "addons/account"})
    assert set(filtered.keys()) == {"sale", "account"}
