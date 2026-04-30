"""Unit tests for indexer driver helpers that don't require a live Postgres."""

from __future__ import annotations

import hashlib
from pathlib import Path

from osm.indexer.driver import (
    IndexStats,
    _auto_install_to_bool,
    _collect_python_files,
    _hash_file,
    _model_names_for,
)
from osm.indexer.manifest import ManifestRecord, scan_addon_root
from osm.indexer.python_parser import ParsedModel

FIXTURES = Path(__file__).parent.parent / "fixtures"
CUSTOM_ADDONS = FIXTURES / "custom_addons"
CE_SUBSET = FIXTURES / "odoo_ce_subset"


# ---------------------------------------------------------------------------
# _hash_file
# ---------------------------------------------------------------------------


def test_hash_file_matches_blake2b_16(tmp_path: Path) -> None:
    data = b"hello world\n" * 100
    p = tmp_path / "sample.txt"
    p.write_bytes(data)

    expected = hashlib.blake2b(data, digest_size=16).hexdigest()
    assert _hash_file(p) == expected


def test_hash_file_streams_large_input(tmp_path: Path) -> None:
    # 1 MB chunk; _hash_file reads in 64 KB blocks, so >1 read.
    data = b"x" * (1024 * 1024)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert _hash_file(p) == hashlib.blake2b(data, digest_size=16).hexdigest()


def test_hash_file_stable_across_calls(tmp_path: Path) -> None:
    p = tmp_path / "x.py"
    p.write_text("class Foo: pass\n")
    assert _hash_file(p) == _hash_file(p)


# ---------------------------------------------------------------------------
# _auto_install_to_bool
# ---------------------------------------------------------------------------


def test_auto_install_false_is_false() -> None:
    assert _auto_install_to_bool(False) is False


def test_auto_install_true_is_true() -> None:
    assert _auto_install_to_bool(True) is True


def test_auto_install_empty_tuple_is_false() -> None:
    assert _auto_install_to_bool(()) is False


def test_auto_install_non_empty_tuple_is_true() -> None:
    assert _auto_install_to_bool(("base", "web")) is True


# ---------------------------------------------------------------------------
# _model_names_for
# ---------------------------------------------------------------------------


def _mk_model(name: str | None, inherit: tuple[str, ...] = ()) -> ParsedModel:
    return ParsedModel(
        name=name,
        inherit=inherit,
        inherits={},
        table=None,
        rec_name=None,
        order=None,
        abstract=False,
        transient=False,
        register_false=False,
        start_line=1,
        end_line=2,
        content_hash="0" * 32,
        file_path="/tmp/x.py",
        class_name="X",
        indexer_notes={},
    )


def test_model_names_for_declared_model() -> None:
    m = _mk_model("sale.order")
    assert _model_names_for(m) == ["sale.order"]


def test_model_names_for_pure_extension() -> None:
    m = _mk_model(None, ("sale.order",))
    assert _model_names_for(m) == ["sale.order"]


def test_model_names_for_multi_inherit() -> None:
    m = _mk_model(None, ("mail.thread", "mail.activity.mixin"))
    assert _model_names_for(m) == ["mail.thread", "mail.activity.mixin"]


def test_model_names_for_empty() -> None:
    m = _mk_model(None, ())
    assert _model_names_for(m) == []


# ---------------------------------------------------------------------------
# _collect_python_files
# ---------------------------------------------------------------------------


def _manifest(name: str, addon_root: Path) -> ManifestRecord:
    records = scan_addon_root(addon_root)
    for r in records:
        if r.name == name:
            return r
    raise AssertionError(f"fixture missing: {name}")


def test_collect_python_files_returns_sorted_models() -> None:
    mr = _manifest("viin_fixture_multi_inherit", CUSTOM_ADDONS)
    files = _collect_python_files(mr)
    names = [f.name for f in files]
    # at least __init__.py + sale_order.py, and sorted
    assert "__init__.py" in names
    assert "sale_order.py" in names
    assert names == sorted(names)


def test_collect_python_files_handles_ce_product() -> None:
    mr = _manifest("product", CE_SUBSET)
    files = _collect_python_files(mr)
    names = {f.name for f in files}
    assert {"__init__.py", "product_product.py", "product_template.py"}.issubset(names)


def test_collect_python_files_empty_when_no_models_dir(tmp_path: Path) -> None:
    mod = tmp_path / "mod_empty"
    mod.mkdir()
    manifest = mod / "__manifest__.py"
    manifest.write_text("{'name': 'empty', 'depends': ['base']}")
    mr = ManifestRecord(
        name="mod_empty",
        path=manifest,
        depends=("base",),
        auto_install=False,
        version="",
        category="",
        application=False,
        installable=True,
    )
    assert _collect_python_files(mr) == []


# ---------------------------------------------------------------------------
# IndexStats
# ---------------------------------------------------------------------------


def test_index_stats_rows_written_counts_all_row_writes() -> None:
    s = IndexStats(
        modules_upserted=2,
        models_inserted=3,
        models_updated=1,
        fields_inserted=10,
        fields_updated=2,
        methods_inserted=5,
        methods_updated=0,
        override_links_written=7,
    )
    assert s.rows_written == 2 + 3 + 1 + 10 + 2 + 5 + 0 + 7


def test_index_stats_default_empty_warnings() -> None:
    s = IndexStats()
    assert s.warnings == []
    assert s.rows_written == 0
