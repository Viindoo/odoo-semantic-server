# tests/test_registry.py
from pathlib import Path

import pytest

from src.indexer.registry import (
    LegacyManifestFinder,
    ModernManifestFinder,
    build_registry,
    get_manifest_finder,
    parse_manifest,
    resolve_odoo_version,
)
from tests.conftest import make_git_repo, make_manifest


def make_legacy_manifest(
    module_dir: Path,
    name: str,
    version: str,
    depends: list,
    installable: bool = True,
) -> None:
    """Create __openerp__.py for v8/v9 module."""
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__openerp__.py").write_text(
        f"{{'name': {name!r}, 'version': {version!r}, "
        f"'depends': {depends!r}, 'installable': {installable!r}}}\n"
    )

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
    repo2 = make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    make_manifest(repo1 / "sale",         "Sales",       "17.0.1.0.0", ["base"])
    make_manifest(repo2 / "viin_sale",    "Viin Sales",  "17.0.1.0.0", ["sale"])
    registry = build_registry([(str(repo1), "17.0"), (str(repo2), "17.0")])
    assert "sale" in registry["17.0"]
    assert "viin_sale" in registry["17.0"]
    assert registry["17.0"]["viin_sale"].repo == "acme_addons_17.0"


# --- Phase 0 v8/v9: ManifestFinder Protocol (M4.5 WI1.1) ---


def test_legacy_manifest_finder_finds_openerp_py(tmp_path):
    """LegacyManifestFinder rglob __openerp__.py."""
    (tmp_path / "v8mod").mkdir()
    (tmp_path / "v8mod" / "__openerp__.py").write_text("{'name': 'V8 Module'}")
    paths = LegacyManifestFinder().find(str(tmp_path))
    assert any("__openerp__.py" in p for p in paths)


def test_modern_manifest_finder_ignores_openerp(tmp_path):
    """ModernManifestFinder không match __openerp__.py."""
    (tmp_path / "v8mod").mkdir()
    (tmp_path / "v8mod" / "__openerp__.py").write_text("{'name': 'X'}")
    assert ModernManifestFinder().find(str(tmp_path)) == []


def test_get_manifest_finder_dispatches_by_version():
    """v8/v9 → Legacy; v10+ → Modern; unknown version → Modern (default)."""
    assert isinstance(get_manifest_finder("8.0"), LegacyManifestFinder)
    assert isinstance(get_manifest_finder("9.0"), LegacyManifestFinder)
    assert isinstance(get_manifest_finder("10.0"), ModernManifestFinder)
    assert isinstance(get_manifest_finder("17.0"), ModernManifestFinder)
    assert isinstance(get_manifest_finder("unknown"), ModernManifestFinder)


def test_build_registry_v8_module_with_openerp_py(tmp_path):
    """Registry với __openerp__.py extract đúng name + depends."""
    repo = make_git_repo(tmp_path / "odoo_8.0", "8.0")
    make_legacy_manifest(repo / "account", "Accounting", "8.0.1.0", ["base"])
    registry = build_registry([(str(repo), "8.0")])
    assert "account" in registry["8.0"]
    assert registry["8.0"]["account"].depends == ["base"]


def test_build_registry_v8_skips_modern_manifest(tmp_path):
    """v8 dispatch chỉ tìm __openerp__.py — module có __manifest__.py bị bỏ qua."""
    repo = make_git_repo(tmp_path / "mixed_8.0", "8.0")
    # Legacy module (v8 style)
    make_legacy_manifest(repo / "sale_v8", "Sales V8", "8.0.1.0", [])
    # Modern manifest in same repo (should NOT be picked up under v8 finder)
    make_manifest(repo / "sale_modern", "Sales Modern", "8.0.1.0", [])
    registry = build_registry([(str(repo), "8.0")])
    assert "sale_v8" in registry.get("8.0", {})
    assert "sale_modern" not in registry.get("8.0", {})
