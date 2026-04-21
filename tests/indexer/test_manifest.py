"""Tests for ``osm.indexer.manifest`` — addon root walker and manifest parser."""

from __future__ import annotations

from pathlib import Path

from osm.indexer.manifest import scan_addon_root, scan_addon_roots

FIXTURES = Path(__file__).parent.parent / "fixtures" / "addons"


def test_scan_returns_manifest_records() -> None:
    records = scan_addon_root(FIXTURES)
    names = {r.name for r in records}
    assert "mod_a" in names
    assert "mod_b" in names


def test_studio_customization_filtered() -> None:
    records = scan_addon_root(FIXTURES)
    names = {r.name for r in records}
    assert "studio_customization" not in names


def test_installable_false_skipped(tmp_path: Path) -> None:
    mod = tmp_path / "not_installable"
    mod.mkdir()
    (mod / "__init__.py").touch()
    (mod / "__manifest__.py").write_text(
        "{'name': 'N', 'depends': [], 'installable': False}"
    )
    records = scan_addon_root(tmp_path)
    assert not any(r.name == "not_installable" for r in records)


def test_missing_manifest_skipped(tmp_path: Path) -> None:
    mod = tmp_path / "no_manifest"
    mod.mkdir()
    (mod / "__init__.py").touch()
    records = scan_addon_root(tmp_path)
    assert not records


def test_auto_install_bool_true(tmp_path: Path) -> None:
    mod = tmp_path / "auto_mod"
    mod.mkdir()
    (mod / "__manifest__.py").write_text(
        "{'name': 'X', 'depends': ['base'], 'installable': True, 'auto_install': True}"
    )
    records = scan_addon_root(tmp_path)
    assert len(records) == 1
    assert records[0].auto_install is True


def test_auto_install_bool_false(tmp_path: Path) -> None:
    mod = tmp_path / "noauto_mod"
    mod.mkdir()
    (mod / "__manifest__.py").write_text(
        "{'name': 'X', 'depends': [], 'installable': True, 'auto_install': False}"
    )
    records = scan_addon_root(tmp_path)
    assert records[0].auto_install is False


def test_auto_install_iterable(tmp_path: Path) -> None:
    mod = tmp_path / "trigger_mod"
    mod.mkdir()
    (mod / "__manifest__.py").write_text(
        "{'name': 'Y', 'depends': ['sale', 'stock'], 'installable': True,"
        " 'auto_install': ['sale', 'stock']}"
    )
    records = scan_addon_root(tmp_path)
    assert records[0].auto_install == ("sale", "stock")


def test_depends_captured() -> None:
    records = scan_addon_root(FIXTURES)
    by_name = {r.name: r for r in records}
    assert by_name["mod_b"].depends == ("mod_a",)
    assert by_name["mod_d"].depends == ("mod_b", "mod_c")


def test_scan_multiple_roots_deduplication(tmp_path: Path) -> None:
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    root1.mkdir()
    root2.mkdir()
    for root in (root1, root2):
        mod = root / "dup_mod"
        mod.mkdir()
        (mod / "__manifest__.py").write_text(
            "{\"name\": \"Dup\", \"depends\": [], \"installable\": True,"
            f" \"version\": \"{root.name}\"}}"
        )
    records = scan_addon_roots([root1, root2])
    dup = [r for r in records if r.name == "dup_mod"]
    assert len(dup) == 1
    assert dup[0].version == "root1"


def test_broken_manifest_skipped(tmp_path: Path) -> None:
    mod = tmp_path / "bad_mod"
    mod.mkdir()
    (mod / "__manifest__.py").write_text("this is not a dict")
    records = scan_addon_root(tmp_path)
    assert not records


def test_openerp_manifest_fallback(tmp_path: Path) -> None:
    mod = tmp_path / "legacy_mod"
    mod.mkdir()
    (mod / "__openerp__.py").write_text(
        "{'name': 'Legacy', 'depends': [], 'installable': True}"
    )
    records = scan_addon_root(tmp_path)
    assert len(records) == 1
    assert records[0].name == "legacy_mod"
