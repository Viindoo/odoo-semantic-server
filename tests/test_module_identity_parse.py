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

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import _detect_module_edition, _normalize_author
from src.indexer.registry import _coerce_float, _coerce_int, build_registry
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


# --- Issue #121 (extended) - additional manifest metadata parse --------------

# Ground-truth values read off ~/git/tvtmaaddons17 (artifact 11 + manifests).
_VNINVOICE_FULL = {
    **_VNINVOICE,
    "version": "17.0.1.0.0",
    "depends": ["account"],
    "installable": True,
    "description": "Long RST description.\nKey Features\n===========\n#. Issue.",
    "website": "https://viindoo.com/apps/app/17.0/l10n_vn_viin_accounting_vninvoice",
    "live_test_url": "https://v17demo-int.viindoo.com",
    "demo_video_url": "https://www.youtube.com/watch?v=d9aFhGaQSoQ",
    "support": "apps.support@viindoo.com",
    "sequence": 31,
    "old_technical_name": "viin_l10n_vn_accounting_vninvoice",
    "price": 13.5,
    "currency": "EUR",
}


def test_build_registry_parses_extended_metadata(tmp_path):
    """All 9 extended manifest keys land on ModuleInfo with the real values."""
    repo = make_git_repo(tmp_path / "tvtmaaddons_17.0", "17.0")
    _write_full_manifest(
        repo / "l10n_vn_viin_accounting_vninvoice", _VNINVOICE_FULL,
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"][
        "l10n_vn_viin_accounting_vninvoice"
    ]
    assert mod.description.startswith("Long RST description.")
    assert mod.website == "https://viindoo.com/apps/app/17.0/l10n_vn_viin_accounting_vninvoice"
    assert mod.live_test_url == "https://v17demo-int.viindoo.com"
    assert mod.demo_video_url == "https://www.youtube.com/watch?v=d9aFhGaQSoQ"
    assert mod.support == "apps.support@viindoo.com"
    assert mod.sequence == 31
    assert mod.old_technical_name == "viin_l10n_vn_accounting_vninvoice"
    assert mod.price == 13.5
    assert mod.currency == "EUR"


def test_build_registry_extended_missing_keys_are_none(tmp_path):
    """CE-core-style manifest (none of the extended keys) -> all None, NOT '' / 0.

    website is '' in CE core manifests -> must coerce to None via `or None`.
    """
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _write_full_manifest(
        repo / "addons" / "ce_plain",
        {"name": "CE Plain", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "LGPL-3", "website": ""},
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"]["ce_plain"]
    assert mod.description is None
    assert mod.website is None  # '' coerced to None
    assert mod.live_test_url is None
    assert mod.demo_video_url is None
    assert mod.support is None
    assert mod.sequence is None
    assert mod.old_technical_name is None
    assert mod.price is None
    assert mod.currency is None


def test_build_registry_price_int_coerced_to_float(tmp_path):
    """Manifest 'price' is sometimes an int (e.g. 27) - must coerce to float."""
    repo = make_git_repo(tmp_path / "tvtmaaddons_17.0", "17.0")
    _write_full_manifest(
        repo / "viin_intprice",
        {"name": "Int Price", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "OPL-1", "author": "Viindoo",
         "price": 27, "currency": "EUR"},
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"]["viin_intprice"]
    assert mod.price == 27.0
    assert isinstance(mod.price, float)


def test_build_registry_price_zero_is_kept_not_none(tmp_path):
    """price=0.0 (a priced-but-free marketplace module) must be kept, NOT None -
    the None default only means "key absent"."""
    repo = make_git_repo(tmp_path / "tvtmaaddons_17.0", "17.0")
    _write_full_manifest(
        repo / "viin_freeprice",
        {"name": "Free Price", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "OPL-1", "author": "Viindoo",
         "price": 0.0, "currency": "EUR"},
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"]["viin_freeprice"]
    assert mod.price == 0.0
    assert mod.price is not None


def test_build_registry_sequence_string_coerced_to_int(tmp_path):
    """'sequence' given as a numeric string must coerce to int (defensive)."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    _write_full_manifest(
        repo / "addons" / "seqstr",
        {"name": "Seq Str", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "LGPL-3", "sequence": "5"},
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"]["seqstr"]
    assert mod.sequence == 5
    assert isinstance(mod.sequence, int)


def test_moduleinfo_extended_fields_default_none():
    """The 9 extended fields default to None on a bare ModuleInfo."""
    info = ModuleInfo(name="m", odoo_version="17.0", repo="r", path="/p", depends=[])
    for attr in (
        "description", "website", "live_test_url", "demo_video_url", "support",
        "sequence", "old_technical_name", "price", "currency",
    ):
        assert getattr(info, attr) is None, f"{attr} must default to None"


# --- Defensive numeric coercion (crash guard) --------------------------------


@pytest.mark.parametrize("value", ["abc", "1.5", "13,5", [1, 2], {}, object()])
def test_coerce_int_non_numeric_returns_none(value):
    """A non-numeric 'sequence' must yield None, NOT raise (else one odd manifest
    crashes the whole index run - the per-module loop has no try/except)."""
    assert _coerce_int(value) is None


@pytest.mark.parametrize("value", ["free", "13,5", [1, 2], {}, object()])
def test_coerce_float_non_numeric_returns_none(value):
    """A non-numeric 'price' must yield None, NOT raise."""
    assert _coerce_float(value) is None


def test_coerce_int_rejects_bool():
    """bool is an int subclass in Python, but 'sequence': True/False must NOT
    become 1/0 - it is not a real sequence."""
    assert _coerce_int(True) is None
    assert _coerce_int(False) is None


def test_coerce_float_rejects_bool():
    """'price': True/False must NOT become 1.0/0.0."""
    assert _coerce_float(True) is None
    assert _coerce_float(False) is None


def test_coerce_int_valid_numeric():
    assert _coerce_int(5) == 5
    assert _coerce_int("5") == 5
    assert _coerce_int(0) == 0  # real 0 is kept (distinct from None)


def test_coerce_float_valid_numeric():
    assert _coerce_float(13.5) == 13.5
    assert _coerce_float(27) == 27.0  # int coerced to float
    assert _coerce_float("13.5") == 13.5
    assert _coerce_float(0.0) == 0.0  # real 0.0 is kept (distinct from None)


def test_build_registry_garbage_sequence_price_do_not_crash(tmp_path):
    """End-to-end: a manifest with junk sequence/price values must index cleanly
    (those fields -> None), NOT raise and abort the whole repo scan.

    Red-before-green: the previous bare int()/float() coerce raised ValueError on
    these, and the per-module build loop has no try/except - so this would crash."""
    repo = make_git_repo(tmp_path / "junk_17.0", "17.0")
    _write_full_manifest(
        repo / "junk_mod",
        {"name": "Junk", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "OPL-1", "author": "Viindoo",
         "sequence": "abc", "price": "free", "currency": "EUR"},
    )
    # Must not raise.
    registry = build_registry([(str(repo), "17.0")])
    mod = registry["17.0"]["junk_mod"]
    assert mod.sequence is None
    assert mod.price is None
    # The module is still fully indexed (currency, which is a plain str, survives).
    assert mod.currency == "EUR"
    assert mod.shortdesc == "Junk"


def test_build_registry_bool_sequence_price_coerced_to_none(tmp_path):
    """A manifest with bool sequence/price (odd but seen) -> None, not 1/1.0."""
    repo = make_git_repo(tmp_path / "boolmod_17.0", "17.0")
    _write_full_manifest(
        repo / "bool_mod",
        {"name": "Bool", "version": "17.0.1.0.0", "depends": [],
         "installable": True, "license": "OPL-1", "author": "Viindoo",
         "sequence": False, "price": True},
    )
    mod = build_registry([(str(repo), "17.0")])["17.0"]["bool_mod"]
    assert mod.sequence is None
    assert mod.price is None
