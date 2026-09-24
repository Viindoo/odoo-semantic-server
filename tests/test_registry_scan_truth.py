# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan truth = git-TRACKED manifests (ADR-0056, OSM #378 node B3).

Business rules protected here (plan 08 tests T16-T19, T25, M1a, M1b, M2, M8,
M10, L11):

- A manifest git does not track (ignored scratch copy, untracked cruft) is
  neither indexed nor counted live, and never steals the path of the real
  tracked module of the same name (real case: tvtmaaddons13 ``.odoo-ai/...``).
- Legacy ``__openerp__.py`` (v8/9), v10 per-module mixed manifests, a stray
  legacy file inside a v11+ tree, a module with no ``__init__.py`` and a
  submodule-vendored addon are all handled like Odoo itself handles them.
- A directory that merely carries a module's name (v8/9 namespace overlay) is
  not a module.
- One deterministic winner per name inside a repo; losers are recorded.
- A checkout whose HEAD is not the registered branch is untrusted (M8).

Temp repos are real git repos (no git mocking): the rule is about what git
tracks, so git itself is the oracle.
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

# Module imports (not name imports) for the APIs node B3 introduced, so the file
# still collects when B3 is reverted for the revert proof: the tests that reach
# the behaviour through the pre-existing ``build_registry`` then fail by
# assertion, the others by the missing attribute.
from src import git_utils
from src.indexer import registry as reg
from src.indexer.registry import build_registry, get_manifest_finder
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root, checkouts_parent

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _init(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _manifest(
    module_dir: Path,
    *,
    version: str = "1.0.0",
    installable: bool = True,
    filename: str = "__manifest__.py",
    init: bool = True,
    extra: str = "",
) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / filename).write_text(
        f"{{'name': {module_dir.name!r}, 'version': {version!r}, "
        f"'depends': [], 'installable': {installable!r}, "
        f"'license': 'LGPL-3'{extra}}}\n"
    )
    if init:
        (module_dir / "__init__.py").write_text("")


def _registered_paths(registry: dict) -> dict[str, str]:
    """{name: absolute module path} over every version key of build_registry."""
    return {n: m.path for mods in registry.values() for n, m in mods.items()}


# --------------------------------------------------------------------------
# list_tracked_manifests
# --------------------------------------------------------------------------

def test_tracked_manifests_lists_every_manifest_name_at_any_depth(tmp_path):
    """Both manifest filenames count, at any depth; other files do not."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "addons" / "sale")
    _manifest(repo / "deep" / "a" / "b" / "legacy", filename="__openerp__.py")
    (repo / "README.md").write_text("x")
    _commit_all(repo)
    assert git_utils.list_tracked_manifests(repo) == {
        "addons/sale/__manifest__.py",
        "deep/a/b/legacy/__openerp__.py",
    }


def test_tracked_manifests_is_none_outside_a_git_work_tree(tmp_path):
    """No git -> tracking unavailable (None), never an empty 'nothing tracked' set."""
    _manifest(tmp_path / "plain" / "mod")
    assert git_utils.list_tracked_manifests(tmp_path / "plain") is None


def test_tracked_manifests_is_none_on_unborn_head(tmp_path):
    """Contract deviation D1: a repo with no commit yet reports None (tracking
    unavailable), not an empty set that would make every module vanish."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "mod")
    assert git_utils.list_tracked_manifests(repo) is None


def test_tracked_manifests_excludes_ignored_and_untracked_files(tmp_path):
    """Only the index counts: an ignored copy and a never-added manifest are absent."""
    repo = _init(tmp_path / "r", "17.0")
    (repo / ".gitignore").write_text(".odoo-ai/\n")
    _manifest(repo / "real")
    _commit_all(repo)
    _manifest(repo / ".odoo-ai" / "wt" / "real")
    _manifest(repo / "phantom")
    assert git_utils.list_tracked_manifests(repo) == {"real/__manifest__.py"}


def test_submodule_vendored_addon_is_tracked(tmp_path):
    """M1b: an addon vendored through a git submodule is tracked and indexed."""
    sub = _init(tmp_path / "vendor_src", "17.0")
    _manifest(sub / "vendored_mod")
    _commit_all(sub, "vendor")

    repo = _init(tmp_path / "main", "17.0")
    _manifest(repo / "own_mod")
    _commit_all(repo, "own")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "protocol.file.allow=always",
         "submodule", "add", "-q", str(sub), "third_party"],
        check=True, capture_output=True,
    )
    _git(repo, "commit", "-q", "-m", "add submodule")

    tracked = git_utils.list_tracked_manifests(repo)
    assert "third_party/vendored_mod/__manifest__.py" in tracked
    assert "own_mod/__manifest__.py" in tracked

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.present_names() == {"own_mod", "vendored_mod"}
    assert scan.complete is True
    assert scan.untracked == frozenset()


# --------------------------------------------------------------------------
# T16 + L11: untracked manifests are never live
# --------------------------------------------------------------------------

def _repo_with_ignored_copy(tmp_path: Path) -> Path:
    """tvtmaaddons13 shape: the real tracked module plus an ignored agent scratch
    copy of the SAME name under .odoo-ai/ (with a long-form version, which the
    old cross-copy preference favoured) and an untracked phantom module."""
    repo = _init(tmp_path / "tvtma_13", "13.0")
    (repo / ".gitignore").write_text(".odoo-ai/\n")
    _manifest(repo / "to_git", version="1.0")
    _manifest(repo / "viin_account_approval", version="1.0")
    _commit_all(repo)
    rb = repo / ".odoo-ai" / "git-rebase" / "x" / "rb-integration"
    _manifest(rb / "to_git", version="13.0.2.0.0")
    _manifest(rb / "viin_account_approval", version="13.0.2.0.0")
    _manifest(rb / "phantom_only_in_scratch", version="13.0.1.0.0")
    return repo


def test_untracked_manifest_is_neither_indexed_nor_counted_live(tmp_path, caplog):
    """T16: the untracked copies are listed as untracked (with a WARNING), never
    as present or excluded modules."""
    repo = _repo_with_ignored_copy(tmp_path)
    with caplog.at_level(logging.WARNING):
        scan = reg.build_registry_scan(str(repo), "13.0")

    assert scan.present_names() == {"to_git", "viin_account_approval"}
    assert "phantom_only_in_scratch" not in scan.excluded
    assert scan.untracked == frozenset({
        ".odoo-ai/git-rebase/x/rb-integration/to_git/__manifest__.py",
        ".odoo-ai/git-rebase/x/rb-integration/viin_account_approval/__manifest__.py",
        ".odoo-ai/git-rebase/x/rb-integration/phantom_only_in_scratch/__manifest__.py",
    })
    # The ignored copies are not "losers" of a same-name contest either: they
    # never entered the contest.
    assert "to_git" not in scan.shadowed
    assert scan.complete is True
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_untracked_same_name_copy_does_not_steal_the_real_module_path(tmp_path):
    """L11 through the pre-existing build_registry API: the real tracked
    directory is the indexed path, and the scratch-only name is not indexed."""
    repo = _repo_with_ignored_copy(tmp_path)
    paths = _registered_paths(build_registry([(str(repo), "13.0")]))
    assert set(paths) == {"to_git", "viin_account_approval"}
    assert paths["to_git"] == str(repo / "to_git")
    assert paths["viin_account_approval"] == str(repo / "viin_account_approval")


@pytest.mark.odoo_source
def test_real_tvtmaaddons13_ignored_copies_are_not_indexed(monkeypatch):
    """L11 positive control on the real local tvtmaaddons13 checkout: it holds
    ignored ``.odoo-ai/...`` copies of real module names. Nothing indexed may
    live under an untracked path, and every indexed/excluded manifest is tracked."""
    repo = checkouts_parent() / "tvtmaaddons13"
    if not repo.is_dir():
        pytest.skip("tvtmaaddons13 checkout not on disk")
    if not any(repo.glob(".odoo-ai/**/__manifest__.py")):
        pytest.skip("this checkout carries no ignored .odoo-ai copies (nothing to prove)")
    # Speed only: per-module `git log` for commit_sha is irrelevant to paths.
    monkeypatch.setattr("src.indexer.registry.get_module_commit_sha", lambda *a: None)

    scan = reg.build_registry_scan(str(repo), "13.0")
    assert scan.untracked, "positive control: the ignored copies must be seen as untracked"
    assert all(".odoo-ai/" in p for p in scan.untracked)
    tracked = scan.tracked_paths
    for name in scan.present_names():
        rel = str(Path(scan.module(name).path).relative_to(repo))
        assert ".odoo-ai" not in rel, name
        assert any(t.startswith(rel + "/") for t in tracked), name
    for name, ex in scan.excluded.items():
        assert f"{ex.path}/{ex.manifest_file}" in tracked, name


# --------------------------------------------------------------------------
# Completeness (M1c)
# --------------------------------------------------------------------------

def test_tracked_manifest_missing_on_disk_makes_scan_incomplete(tmp_path):
    """A half checkout (tracked manifest not on disk) is incomplete, and the
    missing path is named - the gate G-A relies on this."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "a")
    _manifest(repo / "b")
    _commit_all(repo)
    (repo / "b" / "__manifest__.py").unlink()

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.missing == frozenset({"b/__manifest__.py"})
    assert scan.complete is False
    assert "b" not in scan.present_names()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file")
def test_unreadable_tracked_manifest_makes_the_scan_incomplete_not_unparseable(tmp_path):
    """E2E-D1 (E8a: chmod 000 to_approvals/__manifest__.py): a read fault says
    nothing about the module. It is neither present nor excluded - above all
    never ``unparseable``, which the reconcile would retire - and the scan is
    incomplete, so G-A blocks every retirement of the repo."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "to_approvals")
    _manifest(repo / "viin_hr")
    _commit_all(repo)
    manifest = repo / "to_approvals" / "__manifest__.py"
    manifest.chmod(0)
    try:
        scan = reg.build_registry_scan(str(repo), "17.0")
    finally:
        manifest.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    assert "to_approvals" not in scan.excluded, scan.excluded
    assert "to_approvals" not in scan.present_names()
    assert scan.complete is False
    assert getattr(scan, "unreadable", None) == frozenset({"to_approvals/__manifest__.py"})
    assert scan.present_names() == {"viin_hr"}


def test_readable_manifest_that_does_not_parse_is_unparseable_and_the_scan_complete(tmp_path):
    # GUARD: pre-existing behaviour (only a read fault changed classification)
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "viin_hr")
    (repo / "to_approvals").mkdir()
    (repo / "to_approvals" / "__manifest__.py").write_text("{\n    'name' 'to_approvals',\n")
    _commit_all(repo)
    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.excluded["to_approvals"].reason == "unparseable"
    assert scan.complete is True


def test_complete_scan_when_every_tracked_manifest_is_on_disk(tmp_path):
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "a")
    _commit_all(repo)
    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.complete is True
    assert scan.missing == frozenset()
    assert scan.tracked_paths == frozenset({"a/__manifest__.py"})


def test_non_git_directory_scans_the_working_tree_without_tracking(tmp_path):
    """No git: tracking unavailable, working-tree modules are still indexed and
    completeness cannot be measured (trust must then come from git: False)."""
    _manifest(tmp_path / "plain" / "mod")
    scan = reg.build_registry_scan(str(tmp_path / "plain"), "17.0")
    assert scan.tracked_paths is None
    assert scan.present_names() == {"mod"}
    assert scan.untracked == frozenset() and scan.missing == frozenset()
    assert scan.complete is True
    assert git_utils.head_matches_remote_branch(tmp_path / "plain", "17.0") is False


# --------------------------------------------------------------------------
# T17 + M1a: manifest filename dispatch by era
# --------------------------------------------------------------------------

@pytest.mark.parametrize("branch", ["8.0", "9.0"])
def test_legacy_era_indexes_openerp_manifests_only(tmp_path, branch):
    """T17 v8/9: ``__openerp__.py`` is the manifest; a ``__manifest__.py``-only
    directory is not a module of that era and does not break completeness."""
    repo = _init(tmp_path / "r", branch)
    _manifest(repo / "addons" / "sale", filename="__openerp__.py")
    _manifest(repo / "addons" / "future_mod")  # __manifest__.py only
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), branch)
    assert scan.present_names() == {"sale"}
    assert "future_mod" not in scan.excluded
    assert scan.tracked_paths == frozenset({"addons/sale/__openerp__.py"})
    assert scan.complete is True and scan.untracked == frozenset()


def test_v10_mixed_manifests_are_decided_per_module(tmp_path):
    """T17 v10: the same branch holds ``__manifest__.py`` modules, legacy-only
    ``__openerp__.py`` modules (odoo10 account_cash_basis_base_account shape)
    and dirs with both files (indexed once, modern file preferred)."""
    repo = _init(tmp_path / "r", "10.0")
    _manifest(repo / "addons" / "sale")
    _manifest(repo / "addons" / "l10n_fr_pos_cert", filename="__openerp__.py")
    _manifest(repo / "addons" / "both")
    _manifest(repo / "addons" / "both", filename="__openerp__.py", init=False)
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "10.0")
    assert scan.present_names() == {"sale", "l10n_fr_pos_cert", "both"}
    assert scan.tracked_paths == frozenset({
        "addons/sale/__manifest__.py",
        "addons/l10n_fr_pos_cert/__openerp__.py",
        "addons/both/__manifest__.py",
    })
    assert scan.shadowed == {}
    assert scan.complete is True and scan.untracked == frozenset()


def test_stray_legacy_manifest_in_modern_tree_does_not_break_completeness(tmp_path):
    """M1a: a tracked leftover ``__openerp__.py`` in a v11+ tree (next to a
    ``__manifest__.py`` or alone) is outside that era's manifest set, so it is
    neither 'missing' nor a module."""
    repo = _init(tmp_path / "r", "12.0")
    _manifest(repo / "sale")
    _manifest(repo / "sale", filename="__openerp__.py", init=False)
    _manifest(repo / "old_leftover", filename="__openerp__.py")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "12.0")
    assert scan.complete is True
    assert scan.missing == frozenset()
    assert scan.present_names() == {"sale"}
    assert "old_leftover" not in scan.excluded


def test_filter_manifest_paths_follows_the_finder_dispatch():
    paths = {"a/__manifest__.py", "b/__openerp__.py", "c/__manifest__.py", "c/__openerp__.py"}
    assert reg.filter_manifest_paths(paths, "9.0") == {"b/__openerp__.py", "c/__openerp__.py"}
    assert reg.filter_manifest_paths(paths, "10.0") == {
        "a/__manifest__.py", "b/__openerp__.py", "c/__manifest__.py",
    }
    assert reg.filter_manifest_paths(paths, "17.0") == {"a/__manifest__.py", "c/__manifest__.py"}
    # A saas minor is a modern version too.
    assert reg.filter_manifest_paths(paths, "17.1") == {"a/__manifest__.py", "c/__manifest__.py"}


# --------------------------------------------------------------------------
# T18 + T19: what counts as a module
# --------------------------------------------------------------------------

def test_module_without_init_py_is_live(tmp_path):
    """T18: Odoo installs pure-data modules with no ``__init__.py``
    (test_data_module v14+, l10n_co_pos v13) - they are present."""
    repo = _init(tmp_path / "r", "14.0")
    _manifest(repo / "odoo" / "addons" / "test_data_module", init=False)
    _commit_all(repo)
    scan = reg.build_registry_scan(str(repo), "14.0")
    assert scan.present_names() == {"test_data_module"}


def test_same_named_directory_without_manifest_is_not_a_module(tmp_path):
    """T19 (v8/9 namespace overlay): ``odoo/addons/base`` has code but no
    manifest; the real module is ``openerp/addons/base``. The overlay neither
    wins, nor is it a shadowed copy."""
    repo = _init(tmp_path / "r", "8.0")
    _manifest(repo / "openerp" / "addons" / "base", filename="__openerp__.py")
    overlay = repo / "odoo" / "addons" / "base" / "models"
    overlay.mkdir(parents=True)
    (overlay / "res_partner.py").write_text("# overlay code\n")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "8.0")
    assert scan.present_names() == {"base"}
    assert scan.module("base").path == str(repo / "openerp" / "addons" / "base")
    assert "base" not in scan.shadowed


# --------------------------------------------------------------------------
# Present vs excluded, and one winner per name (M2)
# --------------------------------------------------------------------------

def test_excluded_modules_carry_their_reason(tmp_path):
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "live")
    _manifest(repo / "wip", installable=False)
    (repo / "broken").mkdir()
    (repo / "broken" / "__manifest__.py").write_text("not valid python {{{")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.present_names() == {"live"}
    assert scan.excluded["wip"].reason == "installable_false"
    assert scan.excluded["wip"].path == "wip"
    assert scan.excluded["wip"].manifest_file == "__manifest__.py"
    assert scan.excluded["broken"].reason == "unparseable"


def test_present_copy_beats_excluded_copy_of_the_same_name(tmp_path):
    """A name is in at most one of present/excluded; the installable copy wins
    even when the non-installable one is shallower."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "point_of_sale", installable=False)
    _manifest(repo / "vendor" / "deep" / "point_of_sale")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.module("point_of_sale").path == str(repo / "vendor" / "deep" / "point_of_sale")
    assert "point_of_sale" not in scan.excluded
    assert scan.shadowed["point_of_sale"] == ["point_of_sale"]


def test_shallower_copy_wins_among_equal_present_copies(tmp_path):
    """posbox shape (M2): a nested stub of the same name loses to the real
    module; the loser directory is recorded as shadowed."""
    repo = _init(tmp_path / "r", "17.0")
    real = repo / "addons" / "point_of_sale"
    stub = real / "tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale"
    _manifest(real, version="1.0.1")
    _manifest(stub, version="1.0.1")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.module("point_of_sale").path == str(real)
    assert scan.shadowed == {"point_of_sale": [str(stub.relative_to(repo))]}


def test_nested_same_name_stub_never_wins_through_build_registry(tmp_path):
    """M2 through the pre-existing build_registry API: whatever the filesystem
    walk order, the real ``addons/point_of_sale`` is the registered module,
    not the posbox stub nested inside it."""
    repo = _init(tmp_path / "r", "17.0")
    real = repo / "addons" / "point_of_sale"
    stub = real / "tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale"
    _manifest(real, version="17.0.1.0.1")
    _manifest(stub, version="17.0.1.0.1")
    _commit_all(repo)
    assert _registered_paths(build_registry([(str(repo), "17.0")]))["point_of_sale"] == str(real)


def test_three_part_manifest_version_beats_depth(tmp_path):
    """Ranking rule 1: a copy whose manifest version has >= 3 numeric parts
    beats a shallower copy without one."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "dup", version="1.0")
    _manifest(repo / "x" / "y" / "dup", version="17.0.1.2.0")
    _commit_all(repo)

    scan = reg.build_registry_scan(str(repo), "17.0")
    assert scan.module("dup").path == str(repo / "x" / "y" / "dup")
    assert scan.shadowed["dup"] == ["dup"]


def test_winner_does_not_depend_on_filesystem_order(tmp_path):
    """Two equally ranked copies at the same depth: path order decides, the
    same way on every run."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "b_dir" / "dup")
    _manifest(repo / "a_dir" / "dup")
    _commit_all(repo)
    first = reg.build_registry_scan(str(repo), "17.0")
    second = reg.build_registry_scan(str(repo), "17.0")
    assert first.module("dup").path == str(repo / "a_dir" / "dup")
    assert second.module("dup").path == first.module("dup").path
    assert first.shadowed["dup"] == ["b_dir/dup"]


# --------------------------------------------------------------------------
# M8: scan trust
# --------------------------------------------------------------------------

@pytest.fixture
def cloned_repo(tmp_path: Path) -> Path:
    """A clone of an origin with branches 17.0 and 16.0."""
    origin = _init(tmp_path / "origin", "17.0")
    _manifest(origin / "mod")
    _commit_all(origin, "17")
    _git(origin, "branch", "16.0")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    _git(clone, "config", "commit.gpgsign", "false")
    return clone


def test_checkout_at_origin_branch_tip_is_trusted(cloned_repo):
    assert git_utils.head_matches_remote_branch(cloned_repo, "17.0") is True


def test_checkout_of_another_branch_is_untrusted(cloned_repo):
    """M8: HEAD is 17.0 but the registered branch is 16.0 (different commit
    once 16.0 diverges) -> untrusted."""
    _git(cloned_repo, "checkout", "-q", "-b", "feature")
    (cloned_repo / "x.txt").write_text("x")
    _commit_all(cloned_repo, "feature work")
    assert git_utils.head_matches_remote_branch(cloned_repo, "17.0") is False
    assert git_utils.head_matches_remote_branch(cloned_repo, "16.0") is False


def test_local_commit_ahead_of_origin_is_untrusted(cloned_repo):
    """M8: the right branch NAME is not enough when origin/<branch> exists -
    HEAD must be exactly that commit."""
    (cloned_repo / "local.txt").write_text("x")
    _commit_all(cloned_repo, "unpushed")
    assert _git(cloned_repo, "symbolic-ref", "--short", "HEAD").strip() == "17.0"
    assert git_utils.head_matches_remote_branch(cloned_repo, "17.0") is False


def test_trust_requires_a_branch_and_a_commit(cloned_repo, tmp_path):
    assert git_utils.head_matches_remote_branch(cloned_repo, "") is False
    assert git_utils.head_matches_remote_branch(cloned_repo, None) is False
    unborn = _init(tmp_path / "unborn", "17.0")
    assert git_utils.head_matches_remote_branch(unborn, "17.0") is False


def test_origin_less_repo_is_trusted_only_on_the_registered_branch(tmp_path):
    """Deviation D2 (M8 allows it): with no origin/<branch> ref, the symbolic
    branch name decides. Wrong name or detached HEAD -> untrusted."""
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "mod")
    sha = _commit_all(repo)
    assert git_utils.head_matches_remote_branch(repo, "17.0") is True
    assert git_utils.head_matches_remote_branch(repo, "16.0") is False
    _git(repo, "checkout", "-q", "--detach", sha)
    assert git_utils.head_matches_remote_branch(repo, "17.0") is False


@pytest.mark.odoo_source
def test_real_tvtmaaddons17_trust_follows_the_branch():
    repo = checkouts_parent() / "tvtmaaddons17"
    if not repo.is_dir():
        pytest.skip("tvtmaaddons17 checkout not on disk")
    head = _git(repo, "rev-parse", "HEAD").strip()
    try:
        origin = _git(repo, "rev-parse", "--verify", "refs/remotes/origin/17.0").strip()
    except subprocess.CalledProcessError:
        pytest.skip("no origin/17.0 ref in this checkout")
    assert git_utils.head_matches_remote_branch(repo, "17.0") is (head == origin)
    assert git_utils.head_matches_remote_branch(repo, "16.0") is False


# --------------------------------------------------------------------------
# removing_commit
# --------------------------------------------------------------------------

def test_removing_commit_names_the_newest_deleting_commit(tmp_path):
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "gone")
    _manifest(repo / "kept")
    _commit_all(repo, "add")
    _git(repo, "rm", "-q", "-r", "gone")
    sha = _commit_all(repo, "[REM] gone: drop it")

    found = git_utils.removing_commit(repo, "gone/__manifest__.py")
    assert found is not None
    assert found[0] == sha
    assert found[2] == "[REM] gone: drop it"
    assert git_utils.removing_commit(repo, "kept/__manifest__.py") is None


def test_removing_commit_treats_a_rename_as_deletion_of_the_old_path(tmp_path):
    repo = _init(tmp_path / "r", "17.0")
    _manifest(repo / "old_name")
    _commit_all(repo, "add")
    _git(repo, "mv", "old_name", "new_name")
    sha = _commit_all(repo, "[REF] rename")
    found = git_utils.removing_commit(repo, "old_name/__manifest__.py")
    assert found is not None and found[0] == sha


def test_removing_commit_is_none_on_git_error(tmp_path):
    assert git_utils.removing_commit(tmp_path, "x/__manifest__.py") is None


@pytest.mark.odoo_source
def test_real_tvtmaaddons17_test_pylint_removed_by_rename_commit():
    repo = checkouts_parent() / "tvtmaaddons17"
    if not repo.is_dir():
        pytest.skip("tvtmaaddons17 checkout not on disk")
    try:
        _git(repo, "cat-file", "-e", "0240c6b77fd567422440d6962d536da81866e12a^{commit}")
    except subprocess.CalledProcessError:
        pytest.skip("rename commit 0240c6b77f not in this checkout")
    found = git_utils.removing_commit(repo, "test_pylint/__manifest__.py")
    assert found is not None
    assert found[0].startswith("0240c6b77f")
    assert "test_viin_pylint" in found[2]


# --------------------------------------------------------------------------
# T25 / M10: real Odoo checkouts - tracked paths == finder paths
# --------------------------------------------------------------------------

def _git_tracked_manifests_oracle(repo: Path) -> set[str]:
    """Independent oracle: ask git directly (not through src.git_utils)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--recurse-submodules"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {p for p in out.split("\0")
            if p.rsplit("/", 1)[-1] in ("__manifest__.py", "__openerp__.py")}


def _finder_rel_paths(repo: Path, version: str) -> set[str]:
    return {str(Path(p).relative_to(repo)) for p in get_manifest_finder(version).find(str(repo))}


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_real_checkout_tracked_manifest_paths_equal_finder_paths(major):
    """T25 (M10): on each real ``odoo<N>`` checkout, the era-dispatched finder
    sees exactly the manifests git tracks - compared as PATHS, not counts."""
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"odoo{major} checkout not on disk")
    version = f"{major}.0"
    oracle = reg.filter_manifest_paths(_git_tracked_manifests_oracle(root), version)
    assert oracle, "positive control: a real Odoo checkout tracks manifests"
    tracked = git_utils.list_tracked_manifests(root)
    assert reg.filter_manifest_paths(tracked, version) == oracle
    assert _finder_rel_paths(root, version) == oracle


# Real nested fixture modules that must be present (M10 pin). {major: [module dir]}
_NESTED_FIXTURES: dict[int, list[str]] = {
    8: ["addons/base_import_module/tests/test_module"],
    9: ["addons/base_import_module/tests/test_module"],
    10: ["addons/base_import_module/tests/test_module"],
    14: ["addons/base_import_module/tests/test_module"],
    19: [
        "odoo/addons/test_translation_import",
        "odoo/addons/base/tests/test_install_addons/test_install_auto",
        "odoo/addons/base/tests/test_install_addons/test_install_base",
        "odoo/addons/base/tests/test_install_addons/test_install_fail",
    ],
}
_POSBOX_STUB = ("addons/point_of_sale/tools/posbox/overwrite_after_init/"
                "home/pi/odoo/addons/point_of_sale")
_POSBOX_MAJORS = range(12, 19)
_V10_LEGACY_ONLY = ["account_cash_basis_base_account", "l10n_fr_pos_cert", "l10n_fr_sale_closing"]


_PINNED_MAJORS = sorted(set(_NESTED_FIXTURES) | set(_POSBOX_MAJORS) | {8, 10, 13, 14})


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _PINNED_MAJORS)
def test_real_checkout_scan_pins_nested_fixtures_and_posbox_stub(major, monkeypatch):
    """M2/M10 pins on the real scan: nested fixture modules are observed at
    their real paths, the posbox ``point_of_sale`` stub (odoo12-18) loses to
    ``addons/point_of_sale`` and is recorded as shadowed, v10 legacy-only
    modules are present, the v8 base overlay is not the base module, and
    no-``__init__`` modules are present."""
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"odoo{major} checkout not on disk")
    monkeypatch.setattr("src.indexer.registry.get_module_commit_sha", lambda *a: None)
    version = f"{major}.0"
    scan = reg.build_registry_scan(str(root), version)

    assert scan.complete is True
    assert scan.untracked == frozenset()
    assert scan.odoo_version == version

    def observed_dir(name: str) -> str | None:
        mod = scan.module(name)
        if mod is not None:
            return str(Path(mod.path).relative_to(root))
        ex = scan.excluded.get(name)
        return ex.path if ex else None

    for rel in _NESTED_FIXTURES.get(major, []):
        name = rel.rsplit("/", 1)[-1]
        assert observed_dir(name) == rel, name

    if major in _POSBOX_MAJORS:
        assert observed_dir("point_of_sale") == "addons/point_of_sale"
        assert scan.module("point_of_sale") is not None
        assert _POSBOX_STUB in scan.shadowed.get("point_of_sale", [])
    else:
        assert "point_of_sale" not in scan.shadowed

    if major == 10:
        for name in _V10_LEGACY_ONLY:
            assert observed_dir(name) == f"addons/{name}", name
    if major == 8:
        assert observed_dir("base") == "openerp/addons/base"
    if major == 13:
        assert observed_dir("l10n_co_pos") == "addons/l10n_co_pos"
    if major == 14:
        assert observed_dir("test_data_module") == "odoo/addons/test_data_module"
