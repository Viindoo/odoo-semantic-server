# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_scanner.py
from src.indexer.scanner import get_git_branch, is_odoo_version_branch, scan_repos
from tests.conftest import make_git_repo


def test_get_git_branch_returns_version(tmp_path):
    repo = make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    assert get_git_branch(str(repo)) == "17.0"


def test_get_git_branch_returns_none_for_non_repo(tmp_path):
    assert get_git_branch(str(tmp_path / "not_a_repo")) is None


def test_is_odoo_version_branch():
    assert is_odoo_version_branch("17.0") is True
    assert is_odoo_version_branch("8.0") is True
    assert is_odoo_version_branch("19.0") is True
    assert is_odoo_version_branch("main") is False
    assert is_odoo_version_branch("feature/foo") is False
    assert is_odoo_version_branch("") is False


def test_scan_repos_finds_versioned_subdirs(tmp_path):
    make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    make_git_repo(tmp_path / "odoo_16.0", "16.0")
    results = scan_repos([str(tmp_path)])
    versions = {v for _, v in results}
    assert "17.0" in versions
    assert "16.0" in versions


def test_scan_repos_ignores_non_odoo_branches(tmp_path):
    make_git_repo(tmp_path / "some_repo", "main")
    results = scan_repos([str(tmp_path)])
    assert not any(str(tmp_path / "some_repo") == p for p, _ in results)


def test_scan_repos_handles_missing_base_dir():
    results = scan_repos(["/nonexistent/path"])
    assert results == []


def test_scan_repos_base_dir_itself_is_repo(tmp_path):
    repo = make_git_repo(tmp_path, "17.0")
    results = scan_repos([str(repo)])
    assert (str(repo), "17.0") in results
