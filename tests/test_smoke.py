"""Trivial smoke test so `pytest -q` always has at least one collection."""

from __future__ import annotations


def test_truthy() -> None:
    assert True
