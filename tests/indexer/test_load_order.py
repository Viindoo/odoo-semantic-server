"""Tests for ``osm.indexer.load_order`` — fix-point graph and load-order output."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from osm.indexer.load_order import CyclicDependencyError, LoadOrderRecord, compute_load_order
from osm.indexer.manifest import ManifestRecord

FIXTURES = Path(__file__).parent.parent / "fixtures" / "addons"


def _make(
    name: str, depends: list[str], auto_install: bool | tuple[str, ...] = False
) -> ManifestRecord:
    return ManifestRecord(
        name=name,
        path=Path(f"/fake/{name}/__manifest__.py"),
        depends=tuple(depends),
        auto_install=auto_install,
        version="17.0.1.0.0",
        category="",
        application=False,
        installable=True,
    )


def _by_name(records: list[LoadOrderRecord]) -> dict[str, LoadOrderRecord]:
    return {r.name: r for r in records}


class TestLinearChain:
    def test_depth_increases_linearly(self) -> None:
        manifests = [_make("a", []), _make("b", ["a"]), _make("c", ["b"])]
        result = _by_name(compute_load_order(manifests))
        assert result["a"].depth == 0
        assert result["b"].depth == 1
        assert result["c"].depth == 2

    def test_load_order_matches_depth_sort(self) -> None:
        manifests = [_make("c", ["b"]), _make("b", ["a"]), _make("a", [])]
        result = compute_load_order(manifests)
        names = [r.name for r in result]
        assert names == ["a", "b", "c"]

    def test_load_order_indices_sequential(self) -> None:
        manifests = [_make("a", []), _make("b", ["a"]), _make("c", ["b"])]
        result = compute_load_order(manifests)
        assert [r.load_order for r in result] == [0, 1, 2]


class TestDiamondDependency:
    def test_diamond_shape(self) -> None:
        manifests = [
            _make("a", []),
            _make("b", ["a"]),
            _make("c", ["a"]),
            _make("d", ["b", "c"]),
        ]
        result = _by_name(compute_load_order(manifests))
        assert result["a"].depth == 0
        assert result["b"].depth == 1
        assert result["c"].depth == 1
        assert result["d"].depth == 2

    def test_diamond_b_before_c_alphabetical(self) -> None:
        manifests = [
            _make("a", []),
            _make("b", ["a"]),
            _make("c", ["a"]),
            _make("d", ["b", "c"]),
        ]
        result = compute_load_order(manifests)
        names = [r.name for r in result]
        assert names.index("b") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")


class TestCycleDetection:
    def test_simple_cycle_raises(self) -> None:
        manifests = [_make("a", ["b"]), _make("b", ["a"])]
        with pytest.raises(CyclicDependencyError) as exc_info:
            compute_load_order(manifests)
        assert "a" in exc_info.value.cycle
        assert "b" in exc_info.value.cycle

    def test_cycle_error_contains_both_names(self) -> None:
        manifests = [_make("x", ["y"]), _make("y", ["x"])]
        with pytest.raises(CyclicDependencyError) as exc_info:
            compute_load_order(manifests)
        cycle = exc_info.value.cycle
        assert len(cycle) >= 2

    def test_three_node_cycle(self) -> None:
        manifests = [_make("a", ["c"]), _make("b", ["a"]), _make("c", ["b"])]
        with pytest.raises(CyclicDependencyError):
            compute_load_order(manifests)


class TestMissingDependency:
    def test_missing_dep_drops_module(self, caplog: pytest.LogCaptureFixture) -> None:
        manifests = [_make("a", []), _make("x", ["a", "nonexistent"])]
        with caplog.at_level(logging.WARNING, logger="osm.indexer.load_order"):
            result = _by_name(compute_load_order(manifests))
        assert "x" not in result
        assert "a" in result

    def test_missing_dep_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        manifests = [_make("a", []), _make("x", ["a", "nonexistent"])]
        with caplog.at_level(logging.WARNING, logger="osm.indexer.load_order"):
            compute_load_order(manifests)
        assert any("nonexistent" in m or "x" in m for m in caplog.messages)

    def test_fully_missing_module_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        manifests = [_make("depends_on_ghost", ["ghost_module"])]
        with caplog.at_level(logging.WARNING, logger="osm.indexer.load_order"):
            result = compute_load_order(manifests)
        assert result == []


class TestAutoInstall:
    def test_auto_install_true_preserved(self) -> None:
        manifests = [_make("base", []), _make("web", ["base"], auto_install=True)]
        result = _by_name(compute_load_order(manifests))
        assert "web" in result

    def test_auto_install_iterable_preserved(self) -> None:
        manifests = [
            _make("sale", []),
            _make("stock", []),
            _make("sale_stock", ["sale", "stock"], auto_install=("sale", "stock")),
        ]
        result = _by_name(compute_load_order(manifests))
        assert "sale_stock" in result
        assert result["sale_stock"].depth == 1

    def test_auto_install_does_not_affect_graph_depth(self) -> None:
        manifests = [_make("a", []), _make("b", ["a"], auto_install=True)]
        result = _by_name(compute_load_order(manifests))
        assert result["b"].depth == 1


class TestNameSortTieBreak:
    def test_same_depth_sorted_alphabetically(self) -> None:
        manifests = [
            _make("base", []),
            _make("zzz_mod", ["base"]),
            _make("aaa_mod", ["base"]),
            _make("mmm_mod", ["base"]),
        ]
        result = compute_load_order(manifests)
        depth_1 = [r.name for r in result if r.depth == 1]
        assert depth_1 == sorted(depth_1)

    def test_load_order_reflects_alpha_sort(self) -> None:
        manifests = [
            _make("base", []),
            _make("z_ext", ["base"]),
            _make("a_ext", ["base"]),
        ]
        result = compute_load_order(manifests)
        by_name = _by_name(result)
        assert by_name["a_ext"].load_order < by_name["z_ext"].load_order


