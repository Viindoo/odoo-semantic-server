# tests/test_resolver.py
from src.indexer.models import ModuleInfo
from src.indexer.resolver import topological_sort


def make_mod(name: str, depends: list[str]) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version="17.0", repo="test",
        path="/tmp", depends=depends,
    )


def test_simple_linear_chain():
    modules = {
        "base":    make_mod("base", []),
        "mail":    make_mod("mail", ["base"]),
        "sale":    make_mod("sale", ["base", "mail"]),
    }
    result = topological_sort(modules)
    assert result.index("base") < result.index("mail")
    assert result.index("mail") < result.index("sale")


def test_all_modules_present_in_result():
    modules = {
        "base":    make_mod("base", []),
        "account": make_mod("account", ["base"]),
        "sale":    make_mod("sale", ["account"]),
    }
    result = topological_sort(modules)
    assert set(result) == {"base", "account", "sale"}


def test_missing_dependency_is_ignored():
    modules = {
        "sale": make_mod("sale", ["base", "nonexistent_module"]),
        "base": make_mod("base", []),
    }
    result = topological_sort(modules)
    assert "sale" in result
    assert "base" in result
    assert result.index("base") < result.index("sale")


def test_circular_dependency_does_not_hang():
    modules = {
        "a": make_mod("a", ["b"]),
        "b": make_mod("b", ["a"]),
    }
    result = topological_sort(modules)
    assert set(result) == {"a", "b"}


def test_no_modules():
    assert topological_sort({}) == []


def test_single_module_no_deps():
    modules = {"base": make_mod("base", [])}
    assert topological_sort(modules) == ["base"]


def test_deterministic_for_same_input():
    modules = {
        "b": make_mod("b", []),
        "a": make_mod("a", []),
        "c": make_mod("c", ["a", "b"]),
    }
    result1 = topological_sort(modules)
    result2 = topological_sort(modules)
    assert result1 == result2
