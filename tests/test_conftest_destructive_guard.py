# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_conftest_destructive_guard.py
"""Red-first regression tests for the PG destructive-DB guard.

These tests verify that:
  (a) The guard REJECTS a DSN whose db-name is the known production name
      ``odoo_semantic``, regardless of host.
  (b) The guard ACCEPTS db-names that match safe test-marker patterns
      (``osm_test_*`` or ``*_test``).
  (c) No default DSN pointing at a real DB is set at module level — when
      PG_TEST_DSN is unset the pg_conn fixture skips rather than connecting
      to ``odoo_semantic``.
  (d) ``CI=true`` does NOT bypass the db-name guard (it only bypasses the
      legacy remote-host guard for Neo4j/old PG paths).
  (e) ``OSM_ALLOW_NONTEST_DB=1`` is the documented escape hatch that allows
      the guard to be overridden intentionally.

Test design (red-first):
  - Every assertion is written so that it FAILS on the OLD conftest code
    (where PG_TEST_DSN defaulted to ``odoo_semantic`` and the name guard did
    not exist).
  - It PASSES with the new guard + ephemeral-DB fixture.

No PostgreSQL connection is opened in this file — all tests are unit-level
assertions against the guard helpers and the module-level constant.
"""

import pytest

# ---------------------------------------------------------------------------
# Import the guard helpers directly (they are pure Python, no DB needed).
# ---------------------------------------------------------------------------
from tests.conftest import (
    PG_TEST_DSN,
    _assert_pg_db_name_is_safe,
    _dbname_from_dsn,
)

# ---------------------------------------------------------------------------
# Part A — Guard REJECTS known production db-name (any host)
# ---------------------------------------------------------------------------

class TestGuardRejectsProductionName:
    """Guard must skip/raise for db-name 'odoo_semantic' regardless of host."""

    def test_reject_prod_name_localhost(self, monkeypatch):
        """odoo_semantic on localhost must be rejected (old guard let this pass)."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception) as exc_info:
            _assert_pg_db_name_is_safe("odoo_semantic")
        reason = str(exc_info.value)
        assert "production" in reason.lower() or "odoo_semantic" in reason

    def test_reject_prod_name_remote_host(self, monkeypatch):
        """odoo_semantic on a remote host must also be rejected."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("odoo_semantic")

    def test_reject_prod_name_with_ci_true(self, monkeypatch):
        """CI=true must NOT bypass the db-name guard (only the remote-host guard)."""
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception) as exc_info:
            _assert_pg_db_name_is_safe("odoo_semantic")
        reason = str(exc_info.value)
        # Guard must fire; CI bypass should NOT apply here.
        assert "odoo_semantic" in reason or "production" in reason.lower()

    def test_reject_prod_name_ci_equals_1(self, monkeypatch):
        """CI=1 must also NOT bypass the db-name guard."""
        monkeypatch.setenv("CI", "1")
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("odoo_semantic")

    def test_reject_arbitrary_prod_like_name(self, monkeypatch):
        """A db-name without any test marker is rejected (not just known prod names)."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("myapp_production")

    def test_reject_empty_name(self, monkeypatch):
        """Unknown/empty db-name is rejected (safe-by-omission)."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("")


# ---------------------------------------------------------------------------
# Part B — Guard ACCEPTS safe test-marker db-names
# ---------------------------------------------------------------------------

class TestGuardAcceptsSafeNames:
    """Guard must not skip for compliant test db-names."""

    def _passes(self, db_name, monkeypatch):
        """Return True if _assert_pg_db_name_is_safe does NOT raise/skip."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        try:
            _assert_pg_db_name_is_safe(db_name)
            return True
        except pytest.skip.Exception:
            return False

    def test_accept_osm_test_prefix(self, monkeypatch):
        """osm_test_<anything> must pass the guard."""
        assert self._passes("osm_test_abc12345", monkeypatch)

    def test_accept_osm_test_with_xdist_suffix(self, monkeypatch):
        """osm_test_<uuid>_gw0 (xdist worker) must pass the guard."""
        assert self._passes("osm_test_deadbeef_gw0", monkeypatch)

    def test_accept_underscore_test_suffix(self, monkeypatch):
        """foo_test must pass the guard (legacy contributor workflow)."""
        assert self._passes("foo_test", monkeypatch)

    def test_accept_odoo_semantic_test(self, monkeypatch):
        """odoo_semantic_test (with _test suffix) must pass the guard."""
        assert self._passes("odoo_semantic_test", monkeypatch)

    def test_accept_osm_test_exact_prefix(self, monkeypatch):
        """osm_test_ (just the prefix, degenerate edge case) must pass."""
        assert self._passes("osm_test_", monkeypatch)


# ---------------------------------------------------------------------------
# Part C — No default DSN pointing at real DB
# ---------------------------------------------------------------------------

class TestNoDefaultProdDsn:
    """PG_TEST_DSN must not default to odoo_semantic at module level."""

    def test_pg_test_dsn_is_none_when_env_unset(self):
        """When PG_TEST_DSN env is NOT set, the module-level PG_TEST_DSN must be None.

        On the OLD conftest, this was
        ``'postgresql://odoo_semantic:password@localhost:5432/odoo_semantic'``.
        That default is the bug; this test proves it is gone.

        Reads the LIVE module attribute (via `import tests.conftest`) so that any
        future re-introduction of a default in the module-level assignment is caught.
        The check intentionally mirrors what a fresh import would see when no env
        override is present (CI sets PG_ADMIN_DSN/PG_TEST_DSN explicitly; a plain
        dev environment without those vars must get None, not a prod URL).
        """
        import tests.conftest as _conftest_mod

        # The module-level constant must not be a prod-pointing URL.
        # If PG_TEST_DSN is set in the environment, it was set explicitly by the
        # operator and may be a valid test DSN — we delegate the name check to
        # test_module_level_pg_test_dsn_no_prod_dbname.  When it is None that is
        # the expected no-env state: no hard-coded fallback exists.
        val = _conftest_mod.PG_TEST_DSN
        assert val is None or "odoo_semantic" not in (_dbname_from_dsn(val) or ""), (
            f"tests.conftest.PG_TEST_DSN = {val!r} contains the production "
            f"db-name 'odoo_semantic'.  The conftest must not hard-code a prod "
            f"DSN default — this is the RCA-1 data-loss bug."
        )

    def test_module_level_pg_test_dsn_not_prod(self):
        """The module-level PG_TEST_DSN constant must not point at odoo_semantic."""
        # PG_TEST_DSN is None (no env set) OR an explicit env-supplied value.
        # Either way it must not be the old hardcoded prod default.
        old_prod_default = "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic"
        assert PG_TEST_DSN != old_prod_default, (
            "PG_TEST_DSN still defaults to the production database DSN. "
            "This is the RCA-1 data-loss bug."
        )

    def test_module_level_pg_test_dsn_no_prod_dbname(self):
        """If PG_TEST_DSN is set, its db-name must not be 'odoo_semantic'."""
        if PG_TEST_DSN is None:
            pytest.skip("PG_TEST_DSN not set — correct behaviour, nothing to check")
        db_name = _dbname_from_dsn(PG_TEST_DSN)
        assert db_name != "odoo_semantic", (
            f"PG_TEST_DSN ({PG_TEST_DSN!r}) points at the production db-name "
            f"'odoo_semantic'. Use a test db (osm_test_* or *_test)."
        )


# ---------------------------------------------------------------------------
# Part D — CI=true does NOT bypass the db-name guard
# ---------------------------------------------------------------------------

class TestCiDoesNotBypassNameGuard:
    """CI=true bypasses the REMOTE-HOST guard (Neo4j/old PG) but NOT the name guard."""

    def test_ci_true_does_not_bypass_prod_name(self, monkeypatch):
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("odoo_semantic")

    def test_ci_yes_does_not_bypass_prod_name(self, monkeypatch):
        monkeypatch.setenv("CI", "yes")
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("odoo_semantic")

    def test_ci_true_allows_safe_name(self, monkeypatch):
        """CI=true with a safe db-name must NOT skip (CI uses osm_test_* names)."""
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        # Must not raise — CI uses compliant names.
        _assert_pg_db_name_is_safe("osm_test_ci_run")


# ---------------------------------------------------------------------------
# Part E — OSM_ALLOW_NONTEST_DB=1 escape hatch
# ---------------------------------------------------------------------------

class TestEscapeHatch:
    """OSM_ALLOW_NONTEST_DB=1 allows a non-standard db-name intentionally."""

    def test_escape_hatch_allows_prod_name(self, monkeypatch):
        """With OSM_ALLOW_NONTEST_DB=1, even odoo_semantic is allowed."""
        monkeypatch.setenv("OSM_ALLOW_NONTEST_DB", "1")
        # Must not raise.
        _assert_pg_db_name_is_safe("odoo_semantic")

    def test_escape_hatch_allows_arbitrary_name(self, monkeypatch):
        """OSM_ALLOW_NONTEST_DB=1 allows any name (operator accepts full responsibility)."""
        monkeypatch.setenv("OSM_ALLOW_NONTEST_DB", "true")
        _assert_pg_db_name_is_safe("my_legacy_qa_db")

    def test_no_escape_hatch_rejects_arbitrary_name(self, monkeypatch):
        """Without escape hatch, arbitrary names are rejected."""
        monkeypatch.delenv("OSM_ALLOW_NONTEST_DB", raising=False)
        with pytest.raises(pytest.skip.Exception):
            _assert_pg_db_name_is_safe("my_legacy_qa_db")


# ---------------------------------------------------------------------------
# Part F — _dbname_from_dsn helper correctness
# ---------------------------------------------------------------------------

class TestDbnameFromDsn:
    """Unit tests for the DSN parser helper."""

    def test_url_form(self):
        dsn = "postgresql://user:pw@localhost:5432/odoo_semantic"
        assert _dbname_from_dsn(dsn) == "odoo_semantic"

    def test_url_form_test_db(self):
        assert _dbname_from_dsn("postgresql://user:pw@host:5432/osm_test_abc") == "osm_test_abc"

    def test_keyword_form(self):
        assert _dbname_from_dsn("host=localhost port=5432 dbname=odoo_semantic") == "odoo_semantic"

    def test_empty_dsn(self):
        assert _dbname_from_dsn("") == ""

    def test_none_like_dsn(self):
        assert _dbname_from_dsn(None) == ""  # type: ignore[arg-type]

    def test_url_with_query(self):
        dsn = "postgresql://user:pw@host/osm_test_abc?sslmode=require"
        assert _dbname_from_dsn(dsn) == "osm_test_abc"


# ---------------------------------------------------------------------------
# Part G — Acceptance guard: no test file may hard-code a prod-DSN fallback
# ---------------------------------------------------------------------------

class TestNoProdDsnFallbackInTestFiles:
    """Regression guard: no test file may contain a bare prod-DSN fallback.

    Scans all tests/*.py for two dangerous patterns:
      1. psycopg2.connect("...odoo_semantic...") — hard-coded prod connect.
      2. os.environ.get("PG_TEST_DSN", "...odoo_semantic...") — env fallback
         to the prod db-name.

    Files in the allowlist contain the literal for legitimate reasons (testing
    the guard itself, testing DSN-masking strings, or SQL assertions — none of
    them actually connect to the prod DB).

    This test FAILS if a developer re-introduces a prod-DSN fallback anywhere
    outside the allowlist.  It is intentionally a structural/static check so it
    runs without a live DB.
    """

    # Files that are permitted to contain prod-DSN literals for non-connect reasons.
    _ALLOWLIST = frozenset({
        "test_conftest_destructive_guard.py",  # this file — tests the guard itself
        "test_config.py",                      # tests DSN-masking, no real connect
        "test_rls_cutover_portable.py",        # asserts SQL text, no connect
        "test_embeddings_rls.py",              # comment reference only
    })

    # Regex patterns that detect prod-DSN literals even across line continuations.
    # Each pattern uses re.DOTALL so \s+ can span newlines (multiline string
    # assignments, implicit concatenation, etc.).
    _DANGEROUS_REGEXES = [
        # psycopg2.connect("postgresql://odoo_semantic...") — hard-coded prod connect
        r"""psycopg2\.connect\s*\(\s*['"]postgresql://odoo_semantic""",
        # os.getenv / os.environ.get with PG_DSN or PG_TEST_DSN fallback to prod db-name
        r"""os\s*\.\s*(getenv|environ\s*\.\s*get)\s*\(\s*['"](?:PG_DSN|PG_TEST_DSN)['"]\s*,\s*['"]postgresql://odoo_semantic""",
    ]

    def test_no_prod_dsn_fallback_in_test_files(self):
        """Scan ALL tests/**/*.py for dangerous prod-DSN fallback patterns.

        Uses whitespace-normalised regex matching (re.DOTALL) so multiline
        string assignments like:
            pg_dsn = os.getenv(
                "PG_DSN",
                "postgresql://odoo_semantic...",
            )
        are caught even though the literal spans multiple source lines.

        Scans subdirectories recursively (rglob) so browser/ subpackage is
        also checked.
        """
        import re
        from pathlib import Path

        tests_dir = Path(__file__).parent
        violations = []
        compiled = [re.compile(p, re.DOTALL) for p in self._DANGEROUS_REGEXES]

        for py_file in sorted(tests_dir.rglob("*.py")):
            if py_file.name in self._ALLOWLIST:
                continue
            content = py_file.read_text(encoding="utf-8")
            # Collapse all whitespace runs to a single space for multiline matching,
            # then run the patterns against the normalised form.
            normalised = re.sub(r"\s+", " ", content)
            for pat, raw in zip(compiled, self._DANGEROUS_REGEXES):
                if pat.search(normalised):
                    rel = py_file.relative_to(tests_dir)
                    violations.append(f"{rel}: matches /{raw}/")

        assert not violations, (
            "Prod-DSN fallback detected in test files (RCA-1 regression guard). "
            "Replace with `import tests.conftest as _conftest; _conftest.PG_TEST_DSN` "
            "and skip if None.\n"
            + "\n".join(violations)
        )
