# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_conftest_priority2_guard.py
"""Meta-tests for the Priority 2 bolt-fallback guard in conftest.py.

These tests verify the guard *condition* logic — they do NOT invoke the
actual Neo4j connect attempt (that would defeat the purpose of the guard).
The helper `_priority2_guard_blocks_run` is imported directly from conftest
and called with various monkeypatched env combinations.
"""
import pytest

# Import the guard helper directly — it is a module-level function, not a
# fixture, so it is safe to call from tests without triggering the fixture.
from tests.conftest import _priority2_guard_blocks_run


class TestPriority2Guard:
    """Parametrised + individual tests for the Priority 2 guard condition."""

    def test_guard_skips_when_defaults_and_no_ci(self, monkeypatch):
        """Guard returns True (= would skip) when all defaults, CI absent.

        Scenario: developer box (or prod box) with no special env set.
        Expected: guard fires to prevent accidental hit on prod Neo4j.
        """
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)

        assert _priority2_guard_blocks_run() is True

    def test_guard_allows_in_ci(self, monkeypatch):
        """Guard returns False (= allows) when CI=true, even with defaults.

        Scenario: GitHub Actions — service container may be the only Neo4j,
        so Priority 2 must succeed.
        Expected: guard does not fire.
        """
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)

        assert _priority2_guard_blocks_run() is False

    def test_guard_allows_when_non_default_password(self, monkeypatch):
        """Guard returns False when NEO4J_TEST_PASSWORD is non-default.

        Scenario: developer running tests against a local Neo4j that requires
        a real password. Override path must work — guard must not block them.
        Expected: guard does not fire.
        """
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
        monkeypatch.setenv("NEO4J_TEST_PASSWORD", "devpassword")

        assert _priority2_guard_blocks_run() is False

    def test_guard_allows_when_non_default_uri(self, monkeypatch):
        """Guard returns False when NEO4J_TEST_URI points to a non-default host.

        Scenario: developer running tests against a remote or containerised
        Neo4j on a non-standard port / host.
        Expected: guard does not fire.
        """
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("NEO4J_TEST_URI", "bolt://neo4j-test:7687")
        monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)

        assert _priority2_guard_blocks_run() is False

    def test_guard_requires_all_three_defaults_to_block(self, monkeypatch):
        """Only all-three-defaults simultaneously triggers the guard.

        If either URI or password differs from default, guard stays silent
        regardless of CI flag.
        """
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("NEO4J_TEST_URI", "bolt://localhost:7687")  # default
        monkeypatch.setenv("NEO4J_TEST_PASSWORD", "custom-pw")  # non-default

        assert _priority2_guard_blocks_run() is False

    @pytest.mark.parametrize("ci_val", ["true", "TRUE", "True", "1", "yes"])
    def test_guard_allows_for_common_ci_values(self, monkeypatch, ci_val):
        """Common CI-env signals all unlock Priority 2 — guard does not block.

        GitHub Actions sets CI=true; Jenkins/GitLab/Travis often CI=1; some envs
        use True/TRUE/yes. All are treated as "we are in CI" and Priority 2 is
        allowed (the CI service container may be the only Neo4j available).
        """
        monkeypatch.setenv("CI", ci_val)
        monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)

        assert _priority2_guard_blocks_run() is False

    @pytest.mark.parametrize("ci_val", ["", "false", "FALSE", "0", "no", "off"])
    def test_guard_blocks_for_unset_or_false_ci_values(self, monkeypatch, ci_val):
        """Empty / false-looking CI values do NOT count as CI; guard fires.

        Defensive: any non-canonical truthy variant is treated as non-CI so an
        accidental CI=0 on a developer box still triggers the prod-collision
        guard.
        """
        monkeypatch.setenv("CI", ci_val)
        monkeypatch.delenv("NEO4J_TEST_URI", raising=False)
        monkeypatch.delenv("NEO4J_TEST_PASSWORD", raising=False)

        assert _priority2_guard_blocks_run() is True
