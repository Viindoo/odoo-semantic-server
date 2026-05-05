# tests/test_registry.py
from pathlib import Path

import pytest

from src.indexer.registry import build_registry, parse_manifest, resolve_odoo_version
from tests.conftest import make_git_repo, make_manifest

# --- Unit tests: parse_manifest ---

def test_parse_manifest_basic(tmp_path):
    manifest_path = tmp_path / "__manifest__.py"
    manifest_path.write_text("""
{
    'name': 'Sales',
    'version': '17.0.1.0.0',
    'depends': ['base', 'account'],
    'installable': True,
}
""")
    result = parse_manifest(str(manifest_path))
    assert result['name'] == 'Sales'
    assert result['depends'] == ['base', 'account']


def test_parse_manifest_returns_empty_on_error(tmp_path):
    bad = tmp_path / "__manifest__.py"
    bad.write_text("not valid python {{{")
    result = parse_manifest(str(bad))
    assert result == {}


# --- Unit tests: resolve_odoo_version ---

def test_resolve_from_long_format(tmp_path):
    repo = make_git_repo(tmp_path, "17.0")
    assert resolve_odoo_version("17.0.1.0.0", str(repo)) == "17.0"


def test_resolve_from_short_format_uses_branch(tmp_path):
    repo = make_git_repo(tmp_path, "16.0")
    assert resolve_odoo_version("1.0.0", str(repo)) == "16.0"


def test_resolve_returns_unknown_when_no_info(tmp_path):
    # Non-git dir, short version
    assert resolve_odoo_version("1.0.0", str(tmp_path)) == "unknown"


# --- Integration tests: build_registry ---

@pytest.fixture
def odoo_repo(tmp_path):
    """Repo với 3 modules: base, account, sale."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    make_manifest(repo / "base",    "Base",    "17.0.1.0.0", [])
    make_manifest(repo / "account", "Account", "17.0.1.0.0", ["base"])
    make_manifest(repo / "sale",    "Sales",   "17.0.1.0.0", ["base", "account"])
    return str(repo)


def test_build_registry_finds_all_modules(odoo_repo):
    registry = build_registry([(odoo_repo, "17.0")])
    assert set(registry["17.0"].keys()) >= {"base", "account", "sale"}


def test_build_registry_parses_depends(odoo_repo):
    registry = build_registry([(odoo_repo, "17.0")])
    assert registry["17.0"]["sale"].depends == ["base", "account"]


def test_build_registry_sets_repo_name(odoo_repo):
    registry = build_registry([(odoo_repo, "17.0")])
    assert registry["17.0"]["base"].repo == Path(odoo_repo).name


def test_build_registry_skips_non_installable(tmp_path):
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    make_manifest(repo / "disabled_mod", "Disabled", "17.0.1.0.0", [], installable=False)
    make_manifest(repo / "active_mod",   "Active",   "17.0.1.0.0", [])
    registry = build_registry([(str(repo), "17.0")])
    assert "disabled_mod" not in registry.get("17.0", {})
    assert "active_mod" in registry.get("17.0", {})


def test_build_registry_multi_repo(tmp_path):
    repo1 = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    repo2 = make_git_repo(tmp_path / "tvtmaaddons_17.0", "17.0")
    make_manifest(repo1 / "sale",         "Sales",       "17.0.1.0.0", ["base"])
    make_manifest(repo2 / "viin_sale",    "Viin Sales",  "17.0.1.0.0", ["sale"])
    registry = build_registry([(str(repo1), "17.0"), (str(repo2), "17.0")])
    assert "sale" in registry["17.0"]
    assert "viin_sale" in registry["17.0"]
    assert registry["17.0"]["viin_sale"].repo == "tvtmaaddons_17.0"
