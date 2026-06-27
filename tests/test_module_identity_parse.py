# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_module_identity_parse.py
"""Index-layer unit tests for the module identity card + edition reclassify (#121).

Pure-logic tests (no Neo4j, no Docker): they exercise the manifest -> ModuleInfo
parse path:
  * ``_normalize_author``  - str|list|tuple|None coercion (issue #121 P2)
  * ``_detect_module_edition`` - the OPL-1 + Viindoo/TVTMA author reclassify rule
    (issue #121 P5) while preserving the ADR-0036 invariant "OPL-1 != enterprise"
  * ``build_registry`` - shortdesc (manifest 'name') + author parsed onto
    ModuleInfo, None when the manifest omits the key (cross-version N3)

Ground-truth manifests (author/name/license values) are the REAL ones read off
disk in artifact 04 §6 (tvtmaaddons17 e-invoice modules), so a regression in the
parse path reds against production reality, not a fabricated fixture.
"""
from pathlib import Path

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import _detect_module_edition, _normalize_author
from src.indexer.registry import build_registry
from tests.conftest import make_git_repo

# --- Ground-truth manifests (artifact 04 §6, read off ~/git/tvtmaaddons17) ----

_MEINVOICE = {
    "name": "E-Invoice - Misa meInvoice Integrator",
    "summary": "Integrate with Misa meInvoice service to issue legal e-Invoice",
    "author": "T.V.T Marine Automation (aka TVTMA),Viindoo",
    "license": "OPL-1",
    "category": "Accounting/Localizations/EDI",
}
_VNINVOICE = {
    "name": "E-Invoice - VNIs VN-Invoice Integrator",
    "summary": "Integrates with VN-Invoice service to issue legal e-Invoice",
    "author": "T.V.T Marine Automation (aka TVTMA),Viindoo",
    "license": "OPL-1",
    "category": "Accounting/Localizations/EDI",
}


# --- _normalize_author ------------------------------------------------------


def test_normalize_author_plain_string():
    assert _normalize_author({"author": "Viindoo"}) == "Viindoo"


def test_normalize_author_strips_whitespace():
    assert _normalize_author({"author": "  Viindoo  "}) == "Viindoo"


def test_normalize_author_list_is_joined():
    # CE l10n_* manifests ship author as a list (artifact 04 §5.2).
    assert _normalize_author({"author": ["Odoo S.A.", "Vauxoo"]}) == "Odoo S.A., Vauxoo"


def test_normalize_author_tuple_is_joined():
    assert _normalize_author({"author": ("Odoo S.A.", "Vauxoo")}) == "Odoo S.A., Vauxoo"


def test_normalize_author_missing_key_is_none():
    # CE core v9-v17 omits `author` entirely -> None (NOT '') so the identity card
    # can distinguish "not declared" from "declared empty" (N3).
    assert _normalize_author({}) is None


def test_normalize_author_none_value_is_none():
    assert _normalize_author({"author": None}) is None


def test_normalize_author_empty_string_is_none():
    assert _normalize_author({"author": ""}) is None


def test_normalize_author_empty_list_is_none():
    assert _normalize_author({"author": []}) is None


# --- _detect_module_edition: P5 reclassify + ADR-0036 invariant -------------


def test_detect_edition_opl1_viindoo_author_is_viindoo_meinvoice():
    """Issue #121 P5: l10n_vn_viin_accounting_meinvoice (OPL-1, author Viindoo)
    must reclassify custom -> viindoo. Red-before-green: before the rule this
    returned 'custom' (no viin_/to_ prefix, not OEEL-1/OCA/CE)."""
    edition = _detect_module_edition(
        _MEINVOICE, "l10n_vn_viin_accounting_meinvoice", "/git/tvtmaaddons17/x",
    )
    assert edition == "viindoo"


def test_detect_edition_opl1_viindoo_author_is_viindoo_vninvoice():
    edition = _detect_module_edition(
        _VNINVOICE, "l10n_vn_viin_accounting_vninvoice", "/git/tvtmaaddons17/x",
    )
    assert edition == "viindoo"


def test_detect_edition_opl1_tvtma_only_author_is_viindoo():
    """TVTMA alone (no 'Viindoo' literal) still maps to viindoo."""
    manifest = {"license": "OPL-1", "author": "T.V.T Marine Automation (aka TVTMA)"}
    assert _detect_module_edition(manifest, "l10n_vn_x", "/p") == "viindoo"


def test_detect_edition_opl1_third_party_author_stays_custom():
    """OPL-1 authored by a NON-Viindoo third party must NOT be over-claimed as
    viindoo - it stays custom (false-positive guard)."""
    manifest = {"license": "OPL-1", "author": "Some Other Vendor Ltd"}
    assert _detect_module_edition(manifest, "third_party_mod", "/p") == "custom"


def test_detect_edition_opl1_no_author_stays_custom():
    manifest = {"license": "OPL-1"}
    assert _detect_module_edition(manifest, "mystery_mod", "/p") == "custom"


def test_detect_edition_oeel1_stays_enterprise_even_with_viindoo_author():
    """ADR-0036 invariant: OEEL-1 is Odoo S.A.'s OWN Enterprise license. It is
    checked BEFORE the OPL-1 rule, so it stays 'enterprise' even if the author
    string mentions Viindoo - OPL-1 reclassify must never bleed into OEEL-1."""
    manifest = {"license": "OEEL-1", "author": "Viindoo"}
    assert _detect_module_edition(manifest, "ee_mod", "/p") == "enterprise"


def test_detect_edition_viin_prefix_wins_over_opl1():
    """The viin_/to_ prefix rule runs first, so it wins regardless of license."""
    assert _detect_module_edition(
        {"license": "OPL-1", "author": "Whoever"}, "viin_helpdesk", "/p",
    ) == "viindoo"


def test_detect_edition_opl1_author_as_list_is_viindoo():
    """author as list[str] containing Viindoo also reclassifies (coercion path)."""
    manifest = {"license": "OPL-1", "author": ["TVTMA", "Viindoo"]}
    assert _detect_module_edition(manifest, "l10n_vn_x", "/p") == "viindoo"


# --- ModuleInfo defaults + build_registry parse -----------------------------


def test_moduleinfo_identity_fields_default_none():
    """New identity fields default to None (NOT '') - distinguishes absent."""
    info = ModuleInfo(name="m", odoo_version="17.0", repo="r", path="/p", depends=[])
    assert info.shortdesc is None
    assert info.author is None


def _write_full_manifest(module_dir: Path, body: dict) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__manifest__.py").write_text(repr(body) + "\n")


def test_build_registry_parses_shortdesc_and_author(tmp_path):
    """manifest 'name'/'author' land on ModuleInfo.shortdesc/author + the OPL-1
    Viindoo module is reclassified to edition 'viindoo' end-to-end."""
    repo = make_git_repo(tmp_path / "tvtmaaddons_17.0", "17.0")
    _write_full_manifest(
        repo / "l10n_vn_viin_accounting_meinvoice",
        {**_MEINVOICE, "version": "17.0.1.0.0", "depends": ["account"],
         "installable": True},
    )
    registry = build_registry([(str(repo), "17.0")])
    mod = registry["17.0"]["l10n_vn_viin_accounting_meinvoice"]
    assert mod.shortdesc == "E-Invoice - Misa meInvoice Integrator"
    assert mod.author == "T.V.T Marine Automation (aka TVTMA),Viindoo"
    assert mod.summary == "Integrate with Misa meInvoice service to issue legal e-Invoice"
    assert mod.edition == "viindoo"


def test_build_registry_missing_name_author_are_none(tmp_path):
    """A CE-core-style manifest with no 'author' (and a deliberately blank name)
    yields author=None - graceful cross-version (N3)."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    # No 'author' key at all (CE core v9-v17 reality); minimal 'name'. Nested under
    # addons/ so the CE-path heuristic classifies it as community.
    _write_full_manifest(
        repo / "addons" / "plain_ce_mod",
        {"name": "Plain CE", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "LGPL-3"},
    )
    registry = build_registry([(str(repo), "17.0")])
    mod = registry["17.0"]["plain_ce_mod"]
    assert mod.shortdesc == "Plain CE"
    assert mod.author is None
    # And a real CE license still classifies as community (regression check).
    assert mod.edition == "community"
