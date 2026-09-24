# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manifest-level change detection between two commits (OSM #378 node B3, T27/L1/L10).

Business rules:
- A module directory moved as a whole (``git mv old new``) is a RENAME, and the
  pair is kept so the successor of the retired name can be named from git
  evidence (real case: tvtmaaddons17 ``0240c6b77f`` test_pylint ->
  test_viin_pylint).
- A partial move (only the manifest and a minority of files, the rest dropped)
  is NOT a rename: it is the deletion of one module plus the addition of
  another, even though git pairs the identical manifest files.
- Any git failure yields ``[]`` and a WARNING - never an exception, never a
  guessed change set.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from src.indexer import incremental  # module import: collectable with B3 reverted
from tests._odoo_checkouts import checkouts_parent


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    ).stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _head(repo)


def _module(root: Path, name: str, files: dict[str, str]) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': '1.0.0', 'depends': ['base'],"
        f" 'license': 'LGPL-3', 'data': ['views/{name}.xml']}}\n"
    )
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


_BODY = {
    "__init__.py": "from . import models\n",
    "models/__init__.py": "from . import thing\n",
    "models/thing.py": "class Thing:\n    _name = 'x.thing'\n    a = 1\n    b = 2\n    c = 3\n",
    "models/other.py": "class Other:\n    _name = 'x.other'\n    d = 4\n    e = 5\n",
    "views/v.xml": "<odoo>\n  <record id='a' model='ir.ui.view'/>\n</odoo>\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "addons"
    r.mkdir()
    _git(r, "init", "-q", "-b", "17.0")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "config", "commit.gpgsign", "false")
    _module(r, "stock", _BODY)
    _module(r, "sale", {"__init__.py": ""})
    _commit_all(r, "base")
    return r


def test_whole_directory_move_is_a_rename_pair(repo):
    old = _head(repo)
    _git(repo, "mv", "stock", "inventory")
    new = _commit_all(repo, "[REF] stock: rename to inventory")

    changes = incremental.compute_manifest_changes(repo, old, new)
    assert len(changes) == 1
    ch = changes[0]
    assert ch.status == "R"
    assert ch.path == "inventory/__manifest__.py"
    assert ch.old_path == "stock/__manifest__.py"
    assert (ch.name, ch.old_name) == ("inventory", "stock")
    assert (ch.module_dir, ch.old_module_dir) == ("inventory", "stock")
    assert ch.similarity is not None and ch.similarity > 50


def test_partial_move_is_delete_plus_add_not_a_rename(repo):
    """L1: only the manifest and __init__ moved, the rest of the old module was
    dropped - git still pairs the identical manifests, but the module did not
    move as a whole, so no rename (and no successor) may be claimed."""
    old = _head(repo)
    (repo / "new_mod").mkdir()
    _git(repo, "mv", "stock/__manifest__.py", "new_mod/__manifest__.py")
    _git(repo, "mv", "stock/__init__.py", "new_mod/__init__.py")
    _git(repo, "rm", "-q", "-r", "stock")
    new = _commit_all(repo, "split-ish")

    changes = incremental.compute_manifest_changes(repo, old, new)
    assert {(c.status, c.path) for c in changes} == {
        ("D", "stock/__manifest__.py"),
        ("A", "new_mod/__manifest__.py"),
    }
    assert all(c.status != "R" for c in changes)


def test_plain_add_and_delete_are_reported(repo):
    old = _head(repo)
    _git(repo, "rm", "-q", "-r", "sale")
    _module(repo, "brand_new", {"__init__.py": "# new\n"})
    new = _commit_all(repo, "add+del")
    changes = incremental.compute_manifest_changes(repo, old, new)
    assert [(c.status, c.path) for c in changes] == [
        ("A", "brand_new/__manifest__.py"),
        ("D", "sale/__manifest__.py"),
    ]


def test_non_manifest_changes_are_ignored(repo):
    old = _head(repo)
    (repo / "stock" / "models" / "thing.py").write_text("# changed\n")
    new = _commit_all(repo, "edit code")
    assert incremental.compute_manifest_changes(repo, old, new) == []


def test_same_name_directory_move_stays_a_rename_with_equal_names(repo):
    """A module moved inside the repo keeps its name; callers compare
    name/old_name to tell a move from a rename."""
    old = _head(repo)
    (repo / "extra").mkdir()
    _git(repo, "mv", "stock", "extra/stock")
    new = _commit_all(repo, "move")
    changes = incremental.compute_manifest_changes(repo, old, new)
    assert len(changes) == 1
    assert changes[0].status == "R"
    assert changes[0].name == changes[0].old_name == "stock"
    assert changes[0].module_dir == "extra/stock"


def test_git_failure_returns_empty_list_with_warning(repo, caplog):
    """L10: an unknown revision (e.g. unreachable blob on a partial clone) must
    not raise and must not guess."""
    with caplog.at_level(logging.WARNING):
        result = incremental.compute_manifest_changes(repo, "0" * 40, _head(repo))
    assert result == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_not_a_repo_returns_empty_list_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        result = incremental.compute_manifest_changes(tmp_path, "HEAD~1", "HEAD")
    assert result == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.odoo_source
def test_real_tvtmaaddons17_test_pylint_rename_is_detected():
    """T27 on the real checkout: 0240c6b77f renamed test_pylint to
    test_viin_pylint (the #378 trigger)."""
    repo = checkouts_parent() / "tvtmaaddons17"
    if not repo.is_dir():
        pytest.skip("tvtmaaddons17 checkout not on disk")
    sha = "0240c6b77fd567422440d6962d536da81866e12a"
    try:
        _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError:
        pytest.skip("commit 0240c6b77f not in this checkout")

    changes = incremental.compute_manifest_changes(repo, f"{sha}^", sha)
    renames = [c for c in changes if c.status == "R"]
    assert [(c.old_name, c.name) for c in renames] == [("test_pylint", "test_viin_pylint")]
    assert renames[0].old_path == "test_pylint/__manifest__.py"
    assert renames[0].path == "test_viin_pylint/__manifest__.py"
    assert not [c for c in changes if c.status == "D" and c.name == "test_pylint"]
