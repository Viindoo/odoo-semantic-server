"""Trivial smoke test so `pytest -q` has non-empty output during WP-1.

Replaced by real tests from WP-3 onward.
"""

from __future__ import annotations


def test_truthy() -> None:
    assert True
