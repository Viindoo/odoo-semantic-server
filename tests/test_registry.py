# SPDX-License-Identifier: AGPL-3.0-or-later
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


# ---------------------------------------------------------------------------
# A2b — Manifest enrichment (auto_install, application, category,
#        external_python, external_bin)
# ---------------------------------------------------------------------------


def _write_full_manifest(module_dir: Path, **extra_keys) -> None:
    """Write a __manifest__.py with standard keys + any extra_keys provided."""
    module_dir.mkdir(parents=True, exist_ok=True)
    d = {
        "name": "Test Module",
        "version": "17.0.1.0.0",
        "depends": ["base"],
        "installable": True,
    }
    d.update(extra_keys)
    content = repr(d) + "\n"
    (module_dir / "__manifest__.py").write_text(content)


def test_build_registry_auto_install_bool(tmp_path):
    """auto_install=True in manifest → ModuleInfo.auto_install is True."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_ai", auto_install=True)
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_ai"].auto_install is True


def test_build_registry_auto_install_list_coerces_to_bool(tmp_path):
    """auto_install=['base', 'sale'] (trigger list) → coerced to True (bool(list))."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_trig", auto_install=["base", "sale"])
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_trig"].auto_install is True


def test_build_registry_auto_install_false_default(tmp_path):
    """auto_install absent from manifest → ModuleInfo.auto_install defaults to False."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_no_ai")
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_no_ai"].auto_install is False


def test_build_registry_application_true(tmp_path):
    """application=True in manifest → ModuleInfo.application is True."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_app", application=True)
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_app"].application is True


def test_build_registry_category(tmp_path):
    """category key in manifest → ModuleInfo.category populated."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_cat", category="Accounting")
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_cat"].category == "Accounting"


def test_build_registry_category_absent_is_none(tmp_path):
    """category absent from manifest → ModuleInfo.category is None."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_nocat")
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_nocat"].category is None


def test_build_registry_external_dependencies(tmp_path):
    """external_dependencies dict parsed into external_python + external_bin lists."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(
        repo / "mod_extdep",
        external_dependencies={"python": ["pdfminer", "reportlab"], "bin": ["wkhtmltopdf"]},
    )
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["mod_extdep"]
    assert "pdfminer" in info.external_python
    assert "reportlab" in info.external_python
    assert "wkhtmltopdf" in info.external_bin


def test_build_registry_external_dependencies_absent(tmp_path):
    """No external_dependencies in manifest → external_python=[] and external_bin=[]."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_noextdep")
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["mod_noextdep"]
    assert info.external_python == []
    assert info.external_bin == []


# ---------------------------------------------------------------------------
# A2c — Repo provenance (repo_url, repo_id)
# ---------------------------------------------------------------------------


def test_build_registry_repo_provenance(tmp_path):
    """repo_url and repo_id passed to build_registry → stamped on all ModuleInfo."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    make_manifest(repo / "mod_a", "Mod A", "17.0.1.0.0", [])
    make_manifest(repo / "mod_b", "Mod B", "17.0.1.0.0", [])
    registry = build_registry(
        [(str(repo), "17.0")],
        repo_url="https://github.com/example/odoo",
        repo_id=42,
    )
    for mod_name in ("mod_a", "mod_b"):
        info = registry["17.0"][mod_name]
        assert info.repo_url == "https://github.com/example/odoo"
        assert info.repo_id == 42


def test_build_registry_repo_provenance_defaults_none(tmp_path):
    """Callers not passing repo_url/repo_id → both default to None (backward-compat)."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    make_manifest(repo / "mod_c", "Mod C", "17.0.1.0.0", [])
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["mod_c"]
    assert info.repo_url is None
    assert info.repo_id is None
