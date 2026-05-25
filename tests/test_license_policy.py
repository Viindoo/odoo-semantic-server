# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_license_policy.py
# Unit + integration tests for ADR-0036 license policy engine.
from pathlib import Path

from src.constants import (
    LICENSE_POLICY,
    default_license_for_missing,
    license_policy_action,
)
from src.indexer.parser_python import _derive_copyright_owner, _resolve_effective_license
from src.indexer.registry import build_registry
from tests.conftest import make_git_repo

# ---------------------------------------------------------------------------
# Unit tests: default_license_for_missing
# ---------------------------------------------------------------------------


def test_default_license_v8_is_agpl():
    assert default_license_for_missing(8) == "AGPL-3"


def test_default_license_v9_is_lgpl():
    assert default_license_for_missing(9) == "LGPL-3"


def test_default_license_v10_plus_is_lgpl():
    for major in (10, 13, 16, 17, 19):
        assert default_license_for_missing(major) == "LGPL-3", major


# ---------------------------------------------------------------------------
# Unit tests: _resolve_effective_license
# ---------------------------------------------------------------------------


def test_resolve_explicit_license_passthrough():
    manifest = {"license": "OPL-1"}
    assert _resolve_effective_license(manifest, 17) == "OPL-1"


def test_resolve_explicit_oeel1_passthrough():
    manifest = {"license": "OEEL-1"}
    assert _resolve_effective_license(manifest, 17) == "OEEL-1"


def test_resolve_missing_license_v8():
    assert _resolve_effective_license({}, 8) == "AGPL-3"


def test_resolve_missing_license_v9():
    assert _resolve_effective_license({}, 9) == "LGPL-3"


def test_resolve_missing_license_v17():
    assert _resolve_effective_license({}, 17) == "LGPL-3"


def test_resolve_strips_whitespace():
    manifest = {"license": "  LGPL-3  "}
    assert _resolve_effective_license(manifest, 17) == "LGPL-3"


# ---------------------------------------------------------------------------
# Unit tests: _derive_copyright_owner
# ---------------------------------------------------------------------------


def test_copyright_oeel1_always_odoo_sa():
    # Even if author says something else, OEEL-1 overrides.
    assert _derive_copyright_owner({"author": "Some Company"}, "OEEL-1") == "Odoo S.A."
    assert _derive_copyright_owner({}, "OEEL-1") == "Odoo S.A."


def test_copyright_author_contains_odoo_sa():
    manifest = {"author": "Odoo S.A., Some Partner"}
    assert _derive_copyright_owner(manifest, "LGPL-3") == "Odoo S.A."


def test_copyright_author_contains_viindoo():
    manifest = {"author": "Viindoo Technology"}
    assert _derive_copyright_owner(manifest, "OPL-1") == "Viindoo"


def test_copyright_author_contains_tvtma():
    manifest = {"author": "TVTMA"}
    assert _derive_copyright_owner(manifest, "OPL-1") == "Viindoo"


def test_copyright_other_author_truncated():
    long_author = "A" * 200
    result = _derive_copyright_owner({"author": long_author}, "MIT")
    assert result is not None
    assert len(result) == 100


def test_copyright_no_author_lgpl_defaults_odoo_sa():
    assert _derive_copyright_owner({}, "LGPL-3") == "Odoo S.A."


def test_copyright_no_author_agpl_defaults_odoo_sa():
    assert _derive_copyright_owner({}, "AGPL-3") == "Odoo S.A."


def test_copyright_no_author_unknown_returns_none():
    assert _derive_copyright_owner({}, "unknown") is None


def test_copyright_no_author_opl1_returns_none():
    # OPL-1 with no author and not CE copyleft → None (submitter's responsibility)
    assert _derive_copyright_owner({}, "OPL-1") is None


def test_copyright_author_as_list_odoo_sa():
    # Regression: Odoo CE l10n_* manifests ship author as list e.g. ['Odoo S.A.', 'Vauxoo']
    # Must NOT raise AttributeError: 'list' object has no attribute 'strip'.
    manifest = {"author": ["Odoo S.A.", "Vauxoo"]}
    assert _derive_copyright_owner(manifest, "LGPL-3") == "Odoo S.A."


def test_copyright_author_as_str_unchanged():
    # Normal string path must still work after coerce refactor.
    manifest = {"author": "Odoo S.A."}
    assert _derive_copyright_owner(manifest, "LGPL-3") == "Odoo S.A."


def test_copyright_author_is_none():
    # author=None (or missing) must not crash; treated as empty.
    assert _derive_copyright_owner({"author": None}, "LGPL-3") == "Odoo S.A."


def test_copyright_author_as_tuple():
    # Tuple is a valid sequence type in some manifest parsers.
    manifest = {"author": ("Viindoo Technology", "Some Partner")}
    assert _derive_copyright_owner(manifest, "OPL-1") == "Viindoo"


# ---------------------------------------------------------------------------
# Unit tests: license_policy_action
# ---------------------------------------------------------------------------


def test_policy_action_serve_licenses():
    for lic in ("LGPL-3", "AGPL-3", "GPL-3", "OPL-1", "unknown"):
        assert license_policy_action(lic) == "serve", lic


def test_policy_action_oeel1_is_skip():
    assert license_policy_action("OEEL-1") == "skip"


def test_policy_action_unmapped_defaults_serve():
    # Any license not in the map falls back to 'serve' (submitter responsibility).
    assert license_policy_action("MIT") == "serve"
    assert license_policy_action("Apache-2.0") == "serve"


def test_policy_map_contains_required_keys():
    # Ensure all documented licenses are present in the config map.
    required = {"LGPL-3", "AGPL-3", "GPL-3", "OPL-1", "unknown", "OEEL-1"}
    assert required <= set(LICENSE_POLICY.keys())


# ---------------------------------------------------------------------------
# Helper for registry integration tests
# ---------------------------------------------------------------------------


def _make_manifest_with_license(
    module_dir: Path,
    name: str,
    version: str,
    depends: list,
    license_str: str | None = None,
    author: str | None = None,
    installable: bool = True,
) -> None:
    """Write a __manifest__.py with optional license + author fields."""
    module_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        f"'name': {name!r}",
        f"'version': {version!r}",
        f"'depends': {depends!r}",
        f"'installable': {installable!r}",
    ]
    if license_str is not None:
        parts.append(f"'license': {license_str!r}")
    if author is not None:
        parts.append(f"'author': {author!r}")
    content = "{" + ", ".join(parts) + "}\n"
    (module_dir / "__manifest__.py").write_text(content)


# ---------------------------------------------------------------------------
# Integration tests: build_registry policy chokepoint
# ---------------------------------------------------------------------------


def test_build_registry_skips_oeel1_module(tmp_path):
    """OEEL-1 modules must NOT appear in the registry (action=skip)."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _make_manifest_with_license(
        repo / "sale", "Sales", "17.0.1.0.0", [],
        license_str="LGPL-3",
    )
    _make_manifest_with_license(
        repo / "account_payment_term", "Payment Terms", "17.0.1.0.0", [],
        license_str="OEEL-1", author="Odoo S.A.",
    )
    registry = build_registry([(str(repo), "17.0")])
    v = registry.get("17.0", {})
    assert "sale" in v, "LGPL-3 module must be served"
    assert "account_payment_term" not in v, "OEEL-1 module must be skipped"


def test_build_registry_oeel1_skip_is_config_driven(tmp_path, monkeypatch):
    """Flipping LICENSE_POLICY['OEEL-1'] = 'serve' makes OEEL-1 modules served."""
    import src.constants as consts
    monkeypatch.setitem(consts.LICENSE_POLICY, "OEEL-1", "serve")

    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _make_manifest_with_license(
        repo / "account_payment_term", "Payment Terms", "17.0.1.0.0", [],
        license_str="OEEL-1", author="Odoo S.A.",
    )
    registry = build_registry([(str(repo), "17.0")])
    assert "account_payment_term" in registry.get("17.0", {}), (
        "With OEEL-1=serve in config, the module must be indexed"
    )


def test_build_registry_ingest_flagged_sets_notice(tmp_path, monkeypatch):
    """ingest_flagged action sets license_notice on the ModuleInfo."""
    import src.constants as consts
    monkeypatch.setitem(consts.LICENSE_POLICY, "OEEL-1", "ingest_flagged")

    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _make_manifest_with_license(
        repo / "certificate", "Certificate", "17.0.1.0.0", [],
        license_str="OEEL-1", author="Odoo S.A.",
    )
    registry = build_registry([(str(repo), "17.0")])
    v = registry.get("17.0", {})
    assert "certificate" in v, "ingest_flagged module must be present in registry"
    module = v["certificate"]
    assert module.license_notice is not None
    assert "ingest_flagged" in module.license_notice
    assert "certificate" in module.license_notice


def test_build_registry_serve_modules_have_no_notice(tmp_path):
    """Modules with serve action must have license_notice = None."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _make_manifest_with_license(
        repo / "sale", "Sales", "17.0.1.0.0", [],
        license_str="LGPL-3", author="Odoo S.A.",
    )
    registry = build_registry([(str(repo), "17.0")])
    module = registry["17.0"]["sale"]
    assert module.license_notice is None


def test_build_registry_records_license_and_copyright(tmp_path):
    """Every module in the registry must have license + copyright_owner set."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _make_manifest_with_license(
        repo / "sale", "Sales", "17.0.1.0.0", [],
        license_str="LGPL-3", author="Odoo S.A.",
    )
    _make_manifest_with_license(
        repo / "viin_sale", "Viin Sales", "17.0.1.0.0", [],
        license_str="OPL-1", author="Viindoo",
    )
    registry = build_registry([(str(repo), "17.0")])
    sale = registry["17.0"]["sale"]
    assert sale.license == "LGPL-3"
    assert sale.copyright_owner == "Odoo S.A."

    viin = registry["17.0"]["viin_sale"]
    assert viin.license == "OPL-1"
    assert viin.copyright_owner == "Viindoo"


def test_build_registry_missing_license_v8_defaults_agpl(tmp_path):
    """v8 module with no 'license' key gets AGPL-3 effective license."""
    repo = make_git_repo(tmp_path / "odoo_8.0", "8.0")
    # Write a v8-style __openerp__.py (no license key)
    (repo / "account").mkdir(parents=True, exist_ok=True)
    (repo / "account" / "__openerp__.py").write_text(
        "{'name': 'Account', 'version': '8.0.1.0', 'depends': [], 'installable': True}\n"
    )
    registry = build_registry([(str(repo), "8.0")])
    assert "account" in registry.get("8.0", {})
    module = registry["8.0"]["account"]
    assert module.license == "AGPL-3"


def test_build_registry_missing_license_v17_defaults_lgpl(tmp_path):
    """v17 module with no 'license' key gets LGPL-3 effective license."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    # __manifest__.py with no 'license' key
    (repo / "base").mkdir(parents=True, exist_ok=True)
    (repo / "base" / "__manifest__.py").write_text(
        "{'name': 'Base', 'version': '17.0.1.0.0', 'depends': [], 'installable': True}\n"
    )
    registry = build_registry([(str(repo), "17.0")])
    assert "base" in registry.get("17.0", {})
    module = registry["17.0"]["base"]
    assert module.license == "LGPL-3"
    assert module.license_notice is None  # LGPL-3 → serve → no notice
