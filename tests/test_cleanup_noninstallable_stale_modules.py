# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cleanup_noninstallable_stale_modules.py
"""Unit tests for the pure manifest-classification helper of the ops cleanup
script (ops/cleanup_noninstallable_stale_modules.py). No DB, no Neo4j."""
from ops.cleanup_noninstallable_stale_modules import noninstallable_module_names
from tests.conftest import make_git_repo, make_manifest


def test_noninstallable_only_returns_false_modules(tmp_path):
    """Returns exactly the module dirs whose manifest sets installable=False."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    make_manifest(repo / "active_a", "Active A", "17.0.1.0.0", [])
    make_manifest(repo / "active_b", "Active B", "17.0.1.0.0", [])
    make_manifest(repo / "wip_a", "WIP A", "17.0.1.0.0", [], installable=False)
    make_manifest(repo / "wip_b", "WIP B", "17.0.1.0.0", [], installable=False)

    result = noninstallable_module_names(str(repo), "17.0")
    assert result == {"wip_a", "wip_b"}


def test_noninstallable_default_true_is_installable(tmp_path):
    """A manifest with NO installable key defaults to installable=True (registry
    semantics) → NOT classified as non-installable."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    (repo / "no_key").mkdir(parents=True, exist_ok=True)
    (repo / "no_key" / "__manifest__.py").write_text(
        "{'name': 'No Key', 'version': '17.0.1.0.0', 'depends': []}\n"
    )
    assert noninstallable_module_names(str(repo), "17.0") == set()


def test_noninstallable_unparseable_not_classified(tmp_path):
    """An unparseable manifest (empty parse) is NOT counted as non-installable —
    it's a different skip bucket and never produced graph nodes via this path."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    (repo / "broken").mkdir(parents=True, exist_ok=True)
    (repo / "broken" / "__manifest__.py").write_text("not valid python {{{")
    assert noninstallable_module_names(str(repo), "17.0") == set()


def test_noninstallable_empty_repo(tmp_path):
    """No manifests on disk → empty set (no crash)."""
    repo = make_git_repo(tmp_path / "empty_17.0", "17.0")
    assert noninstallable_module_names(str(repo), "17.0") == set()


def test_noninstallable_uses_version_dispatched_finder(tmp_path):
    """v8 dispatch → only __openerp__.py is scanned; an installable=False legacy
    module is correctly classified."""
    repo = make_git_repo(tmp_path / "repo_8.0", "8.0")
    (repo / "legacy_wip").mkdir(parents=True, exist_ok=True)
    (repo / "legacy_wip" / "__openerp__.py").write_text(
        "{'name': 'Legacy WIP', 'version': '8.0.1.0', "
        "'depends': [], 'installable': False}\n"
    )
    assert noninstallable_module_names(str(repo), "8.0") == {"legacy_wip"}


def test_noninstallable_returns_dir_name_not_manifest_name(tmp_path):
    """The returned key is the module DIRECTORY name (Module.name), not the
    manifest 'name' field — must match how the registry/graph keys modules."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    make_manifest(
        repo / "tech_dir_name", "Human Readable Name", "17.0.1.0.0", [],
        installable=False,
    )
    result = noninstallable_module_names(str(repo), "17.0")
    assert result == {"tech_dir_name"}
    assert "Human Readable Name" not in result
