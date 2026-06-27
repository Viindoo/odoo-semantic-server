# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_registry.py
from pathlib import Path

import pytest

from src.indexer.registry import (
    DualManifestFinder,
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


def _write_raw_manifest(module_dir: Path, body: str) -> None:
    """Write a __manifest__.py with arbitrary dict *body* (for keys make_manifest lacks)."""
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__manifest__.py").write_text(body)


# --- WI-G: manifest correctness fixes (osm-audit-manifest) ---

def test_build_registry_indexes_installable_active_false_module(tmp_path):
    """`active: False` with `installable: True` MUST be indexed (parser MED-1).

    Behaviour contract: `active` is a legacy auto-install hint, NOT an
    index-exclusion signal — `installable: True` is what gates indexing. A
    real-world example is v10-v14 `account_test` (installable:True, active:False),
    which ships the queryable model `accounting.assert.test`. Skipping it on
    `active: False` silently dropped an installable module + its model from the
    index, so the skip was reverted.
    """
    repo = make_git_repo(tmp_path / "repo_14.0", "14.0")
    _write_raw_manifest(
        repo / "account_test_like",
        "{'name': 'Accounting Consistency Tests', 'version': '14.0.1.0.0', "
        "'depends': [], 'installable': True, 'active': False}\n",
    )
    make_manifest(repo / "live_mod", "Live", "14.0.1.0.0", [])
    registry = build_registry([(str(repo), "14.0")])
    assert "account_test_like" in registry.get("14.0", {}), (
        "an installable module must be indexed even when active: False"
    )
    assert "live_mod" in registry.get("14.0", {})


def test_build_registry_active_false_not_counted_as_skip(tmp_path, caplog):
    """An installable `active: False` module is registered, NOT skipped."""
    import logging

    repo = make_git_repo(tmp_path / "repo_14.0", "14.0")
    _write_raw_manifest(
        repo / "account_test_like",
        "{'name': 'Accounting Consistency Tests', 'version': '14.0.1.0.0', "
        "'depends': [], 'installable': True, 'active': False}\n",
    )
    make_manifest(repo / "live_mod", "Live", "14.0.1.0.0", [])
    with caplog.at_level(logging.INFO, logger="src.indexer.registry"):
        build_registry([(str(repo), "14.0")])
    summary = next(
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("registry scan")
    )
    assert "0 not-installable" in summary, summary
    assert "2 registered" in summary, summary


def test_build_registry_reads_countries_key(tmp_path):
    """v17+ `countries` manifest key is read into ModuleInfo.countries (GAP-3)."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    _write_raw_manifest(
        repo / "l10n_fr_mod",
        "{'name': 'FR localization', 'version': '17.0.1.0.0', 'depends': [], "
        "'installable': True, 'countries': ['fr', 'be']}\n",
    )
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["l10n_fr_mod"]
    assert info.countries == ["fr", "be"]


def test_build_registry_countries_defaults_empty(tmp_path):
    """A manifest without `countries` yields an empty list (no restriction)."""
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    make_manifest(repo / "global_mod", "Global", "17.0.1.0.0", [])
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["global_mod"].countries == []


def test_build_registry_multi_repo(tmp_path):
    repo1 = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    repo2 = make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    make_manifest(repo1 / "sale",         "Sales",       "17.0.1.0.0", ["base"])
    make_manifest(repo2 / "viin_sale",    "Viin Sales",  "17.0.1.0.0", ["sale"])
    registry = build_registry([(str(repo1), "17.0"), (str(repo2), "17.0")])
    assert "sale" in registry["17.0"]
    assert "viin_sale" in registry["17.0"]
    assert registry["17.0"]["viin_sale"].repo == "acme_addons_17.0"


# --- Per-repo skip-summary observability (feat/registry-skip-observability) ---


def test_build_registry_emits_skip_summary(tmp_path, caplog):
    """build_registry emits ONE per-repo INFO summary line with correct counts.

    Mix: 2 installable, 2 installable=False, 1 license-skip (OEEL-1), 1 unparseable.
    Behaviour unchanged — only the installable modules are registered — AND the
    summary line carries the right per-bucket counts. Distinguishing "low
    coverage" (a real gap) from "mostly not-installable" (expected) is the goal.
    """
    import logging

    repo = make_git_repo(tmp_path / "mixed_17.0", "17.0")
    # Installable (registered) ×2
    make_manifest(repo / "active_a", "Active A", "17.0.1.0.0", [])
    make_manifest(repo / "active_b", "Active B", "17.0.1.0.0", [])
    # installable=False ×2
    make_manifest(repo / "wip_a", "WIP A", "17.0.1.0.0", [], installable=False)
    make_manifest(repo / "wip_b", "WIP B", "17.0.1.0.0", [], installable=False)
    # license skip (OEEL-1 → action=skip per LICENSE_POLICY) ×1
    _write_full_manifest(repo / "ent_mod", license="OEEL-1")
    # unparseable ×1 (invalid Python that fails ast.parse AND regex extract)
    (repo / "broken_mod").mkdir(parents=True, exist_ok=True)
    (repo / "broken_mod" / "__manifest__.py").write_text("not valid python {{{")

    with caplog.at_level(logging.INFO, logger="src.indexer.registry"):
        registry = build_registry([(str(repo), "17.0")])

    # Behaviour preserved: only the 2 installable, serve-licensed modules register.
    keys = registry.get("17.0", {})
    assert "active_a" in keys
    assert "active_b" in keys
    assert "wip_a" not in keys
    assert "wip_b" not in keys
    assert "ent_mod" not in keys
    assert "broken_mod" not in keys

    summary_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("registry scan")
    ]
    assert len(summary_lines) == 1, f"expected exactly ONE summary line, got {summary_lines!r}"
    msg = summary_lines[0]
    assert "6 manifests" in msg, msg
    assert "2 registered" in msg, msg
    assert "2 not-installable" in msg, msg
    assert "1 license" in msg, msg
    assert "1 unparseable" in msg, msg
    assert "0 unknown-version" in msg, msg


def test_build_registry_summary_one_line_per_repo(tmp_path, caplog):
    """Two repos → exactly two summary lines (no per-module spam)."""
    import logging

    repo1 = make_git_repo(tmp_path / "r1_17.0", "17.0")
    repo2 = make_git_repo(tmp_path / "r2_17.0", "17.0")
    make_manifest(repo1 / "mod1", "Mod1", "17.0.1.0.0", [])
    make_manifest(repo2 / "mod2", "Mod2", "17.0.1.0.0", [], installable=False)

    with caplog.at_level(logging.INFO, logger="src.indexer.registry"):
        build_registry([(str(repo1), "17.0"), (str(repo2), "17.0")])

    summary_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("registry scan")
    ]
    assert len(summary_lines) == 2, f"expected one line per repo, got {summary_lines!r}"


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
    """v8/v9 → Legacy; v10 → Dual (transition era); v11+ → Modern; unknown → Modern.

    v10 is the transition era: most modules use __manifest__.py but a few l10n
    modules still ship only __openerp__.py, so v10 MUST scan both filenames.
    """
    assert isinstance(get_manifest_finder("8.0"), LegacyManifestFinder)
    assert isinstance(get_manifest_finder("9.0"), LegacyManifestFinder)
    assert isinstance(get_manifest_finder("10.0"), DualManifestFinder)
    assert isinstance(get_manifest_finder("11.0"), ModernManifestFinder)
    assert isinstance(get_manifest_finder("17.0"), ModernManifestFinder)
    assert isinstance(get_manifest_finder("unknown"), ModernManifestFinder)


def test_get_manifest_finder_v10_is_not_legacy_nor_modern():
    """Regression guard: v10 must be the Dual finder, never Legacy nor plain
    Modern — a plain Modern finder silently drops the __openerp__.py-only
    l10n modules (the original v10 indexing gap)."""
    finder = get_manifest_finder("10.0")
    assert not isinstance(finder, LegacyManifestFinder)
    assert type(finder) is DualManifestFinder


# --- v10 DualManifestFinder behaviour (GAP-1 fix) ---


def test_v10_dual_finder_finds_openerp_only_module(tmp_path):
    """A v10 module shipping ONLY __openerp__.py (legacy l10n) must be found —
    this is the exact group the old ModernManifestFinder silently dropped."""
    (tmp_path / "l10n_fr_pos_cert").mkdir()
    (tmp_path / "l10n_fr_pos_cert" / "__openerp__.py").write_text(
        "{'name': 'France POS Cert'}"
    )
    paths = DualManifestFinder().find(str(tmp_path))
    assert any(p.endswith("l10n_fr_pos_cert/__openerp__.py") for p in paths)


def test_v10_dual_finder_dedupes_prefer_manifest(tmp_path):
    """Tree: A=__openerp__.py only, B=__manifest__.py only, C=BOTH files.

    Expect exactly 3 paths (one per module — no double-count), and for module C
    the path MUST be __manifest__.py (modern wins; legacy is dropped on tie).
    """
    # Module A — legacy only
    (tmp_path / "mod_a").mkdir()
    (tmp_path / "mod_a" / "__openerp__.py").write_text("{'name': 'A'}")
    # Module B — modern only
    (tmp_path / "mod_b").mkdir()
    (tmp_path / "mod_b" / "__manifest__.py").write_text("{'name': 'B'}")
    # Module C — BOTH (transition artefact) → must dedupe to modern
    (tmp_path / "mod_c").mkdir()
    (tmp_path / "mod_c" / "__manifest__.py").write_text("{'name': 'C modern'}")
    (tmp_path / "mod_c" / "__openerp__.py").write_text("{'name': 'C legacy'}")

    paths = DualManifestFinder().find(str(tmp_path))

    # Exactly one entry per module — no double-count of C.
    assert len(paths) == 3, f"expected 3 paths (A, B, C-deduped), got {paths!r}"

    # Module A found via legacy file.
    assert any(p.endswith("mod_a/__openerp__.py") for p in paths)
    # Module B found via modern file.
    assert any(p.endswith("mod_b/__manifest__.py") for p in paths)
    # Module C resolved to modern, NOT legacy.
    assert any(p.endswith("mod_c/__manifest__.py") for p in paths), \
        "C must resolve to __manifest__.py"
    assert not any(p.endswith("mod_c/__openerp__.py") for p in paths), \
        "C must NOT include __openerp__.py when __manifest__.py exists (dedupe)"


def test_build_registry_v10_indexes_openerp_only_module(tmp_path):
    """End-to-end: build_registry under v10 must index a module that ships
    only __openerp__.py (the silent-drop gap), reading its name + depends."""
    repo = make_git_repo(tmp_path / "odoo_10.0", "10.0")
    make_manifest(repo / "sale_modern", "Sales Modern", "10.0.1.0.0", ["base"])
    make_legacy_manifest(repo / "l10n_fr_pos_cert", "FR POS Cert", "10.0.1.0", ["point_of_sale"])
    registry = build_registry([(str(repo), "10.0")])
    assert "sale_modern" in registry.get("10.0", {}), "modern module must still index"
    assert "l10n_fr_pos_cert" in registry.get("10.0", {}), \
        "__openerp__.py-only module must now index under v10"
    assert registry["10.0"]["l10n_fr_pos_cert"].depends == ["point_of_sale"]


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


def test_build_registry_summary(tmp_path):
    """summary key in manifest → ModuleInfo.summary populated."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_sum", summary="Manage sales orders")
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_sum"].summary == "Manage sales orders"


def test_build_registry_summary_absent_is_none(tmp_path):
    """summary absent from manifest → ModuleInfo.summary is None."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_nosum")
    registry = build_registry([(str(repo), "17.0")])
    assert registry["17.0"]["mod_nosum"].summary is None


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


def test_build_registry_external_dependencies_none_value(tmp_path):
    """external_dependencies=None in manifest must NOT crash; external_python/bin = [].

    Some manifests set the key explicitly to None (e.g. a merge artefact or a
    module that cleared its deps).  Previously this caused AttributeError
    ('NoneType' has no attribute 'get') — regression guard.
    """
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(repo / "mod_extdep_none", external_dependencies=None)
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["mod_extdep_none"]
    assert info.external_python == [], "external_python must be [] when key is None"
    assert info.external_bin == [], "external_bin must be [] when key is None"


def test_build_registry_external_dependencies_python_none(tmp_path):
    """external_dependencies={'python': None} must NOT crash; external_python = [].

    A manifest that sets the python list explicitly to None should be treated
    the same as an absent key — the indexer must not abort the whole repo.
    """
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_full_manifest(
        repo / "mod_extdep_py_none",
        external_dependencies={"python": None, "bin": ["wkhtmltopdf"]},
    )
    registry = build_registry([(str(repo), "17.0")])
    info = registry["17.0"]["mod_extdep_py_none"]
    assert info.external_python == [], "external_python must be [] when value is None"
    assert "wkhtmltopdf" in info.external_bin, "external_bin must still be populated"


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
