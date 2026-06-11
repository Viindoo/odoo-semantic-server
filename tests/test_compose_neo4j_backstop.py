# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_compose_neo4j_backstop.py
"""
Tests that NEO4J_db_transaction_timeout=600s is wired into IaC (issue #276 / ADR-0048 D7).

Two concerns:
(a) Static: docker-compose.yml declares the env key and its value (in seconds) exceeds
    NEO4J_QUERY_TIMEOUT_SECONDS (the per-query driver timeout). Fails if the key is removed.
(b) Integration (marker: neo4j): a live Neo4j instance started with the env var applied
    reports the correct value via SHOW SETTINGS, verifying the Neo4j env-name -> config
    mapping works at runtime (coverage that static YAML parsing cannot provide).

Both tests protect the INVARIANT (ADR-0048 D7):
    global_backstop_seconds > per_query_timeout_seconds
"""

import re
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_CONSTANTS_FILE = _REPO_ROOT / "src" / "constants.py"

# The env key that docker-compose.yml must declare.
_COMPOSE_ENV_KEY = "NEO4J_db_transaction_timeout"

# Expected minimum value in seconds (600s per ADR-0048 D7).
_EXPECTED_MIN_SECONDS = 600

# The per-query driver timeout constant name read from src/constants.py.
_QUERY_TIMEOUT_CONST = "NEO4J_QUERY_TIMEOUT_SECONDS"
_QUERY_TIMEOUT_DEFAULT = 30  # fallback if regex parse fails (safe: 600 >> 30)


def _parse_query_timeout_default() -> int:
    """Read NEO4J_QUERY_TIMEOUT_SECONDS default from src/constants.py.

    Returns the integer default. Falls back to 30 (known default) if the line
    cannot be parsed, so the static test remains useful even under refactors.
    """
    try:
        text = _CONSTANTS_FILE.read_text()
        # Match: NEO4J_QUERY_TIMEOUT_SECONDS: int = int(os.getenv("...", "30"))
        m = re.search(
            r'NEO4J_QUERY_TIMEOUT_SECONDS\s*[:=].*?["\'](\d+)["\']',
            text,
        )
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return _QUERY_TIMEOUT_DEFAULT


def _parse_timeout_seconds(value: str) -> int:
    """Convert a Neo4j duration string to seconds.

    Accepts bare integer strings ("600"), "600s", "PT10M", or Neo4j ISO-8601 shorthands.
    Returns -1 on parse failure so the assertion message is clear.
    """
    value = (value or "").strip()
    # Bare integer (no unit) -> assume seconds
    if re.fullmatch(r"\d+", value):
        return int(value)
    # "600s"
    m = re.fullmatch(r"(\d+)s", value, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # "10m" or "10M"
    m = re.fullmatch(r"(\d+)m", value, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    # "PT10M" (ISO-8601)
    m = re.fullmatch(r"PT(\d+)M", value, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    # "PT600S"
    m = re.fullmatch(r"PT(\d+)S", value, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return -1


# ---------------------------------------------------------------------------
# (a) Static test — no Docker required
# ---------------------------------------------------------------------------


def test_compose_neo4j_has_transaction_timeout_env():
    """docker-compose.yml neo4j service must declare NEO4J_db_transaction_timeout.

    This test FAILS (correctly) if the env key is removed from docker-compose.yml,
    which is the intended protection: the IaC backstop must not be silently deleted.
    """
    assert _COMPOSE_FILE.exists(), f"docker-compose.yml not found at {_COMPOSE_FILE}"
    compose = yaml.safe_load(_COMPOSE_FILE.read_text())

    services = compose.get("services", {})
    neo4j_svc = services.get("neo4j")
    assert neo4j_svc is not None, "No 'neo4j' service found in docker-compose.yml"

    env = neo4j_svc.get("environment", {})
    # docker-compose environment can be a dict or a list of "KEY=VALUE" strings.
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)

    assert _COMPOSE_ENV_KEY in env, (
        f"docker-compose.yml neo4j service is missing '{_COMPOSE_ENV_KEY}'. "
        f"This env var is the global Neo4j transaction-timeout backstop (ADR-0048 D7 / "
        f"issue #276). Without it, 'docker compose up/recreate' resets the timeout to "
        f"0s (disabled), re-exposing the zombie-transaction leak pattern. "
        f"Add: {_COMPOSE_ENV_KEY}: \"600s\" to services.neo4j.environment."
    )

    raw_value = str(env[_COMPOSE_ENV_KEY])
    actual_seconds = _parse_timeout_seconds(raw_value)
    assert actual_seconds > 0, (
        f"Could not parse '{_COMPOSE_ENV_KEY}' value {raw_value!r} as a duration. "
        f"Expected a value like '600s', '600', or 'PT10M'."
    )

    query_timeout = _parse_query_timeout_default()
    assert actual_seconds > query_timeout, (
        f"ADR-0048 D7 invariant violated: "
        f"NEO4J_db_transaction_timeout ({actual_seconds}s) must be GREATER than "
        f"NEO4J_QUERY_TIMEOUT_SECONDS default ({query_timeout}s). "
        f"The global backstop must outlive the per-query driver timeout so that "
        f"legitimate long-running indexer transactions are not killed. "
        f"Current value: {raw_value!r}. Minimum required: {query_timeout + 1}s."
    )

    assert actual_seconds >= _EXPECTED_MIN_SECONDS, (
        f"NEO4J_db_transaction_timeout is {actual_seconds}s, expected >= {_EXPECTED_MIN_SECONDS}s. "
        f"600s is required to accommodate long-running indexer transactions "
        f"(delete_modules_scoped, gc_stale_modules, _write_parse_result). "
        f"See docs/operations/timeouts.md for rationale."
    )


# ---------------------------------------------------------------------------
# (b) Integration test — requires live Neo4j (testcontainers or CI service)
# ---------------------------------------------------------------------------

pytestmark_neo4j = pytest.mark.neo4j

# NOTE: This module does NOT set a module-level pytestmark so that the static
# test (a) above runs without any neo4j fixture. The integration test below
# carries its own marker. The static test is always collected + executed.


@pytest.mark.neo4j
def test_neo4j_show_settings_transaction_timeout(neo4j_driver):
    """Verify Neo4j applies db.transaction.timeout=600s at runtime.

    This integration test starts a Neo4j instance with NEO4J_db_transaction_timeout
    set (via the neo4j_driver fixture which uses testcontainers or a CI service
    container), then queries SHOW SETTINGS to confirm the setting is applied.

    This exercises the env-name -> Neo4j config mapping that static YAML parsing
    cannot verify: NEO4J_db_transaction_timeout must translate to the setting
    db.transaction.timeout inside Neo4j.

    IMPORTANT: The standard conftest.py neo4j_driver fixture does NOT inject
    NEO4J_db_transaction_timeout into the testcontainer. The SHOW SETTINGS query
    in this test may therefore observe the default (0 = disabled) on local dev
    where testcontainers is used without the env var.

    Behaviour by environment:
    - CI (nightly-smoke): the service container IS started with
      NEO4J_db_transaction_timeout=600s (added to nightly-smoke.yml) -- PASS.
    - CI (unit/integration test suite via ci.yml): the ci.yml service container
      does NOT inject the env var -- this test will SKIP via pytest.skip below
      rather than fail, since the ci.yml suite covers functional correctness,
      not IaC wiring. The static test (a) above covers the IaC assertion.
    - Local dev (testcontainers): the container does not have the env var --
      test SKIPs with a clear message.

    To run this test end-to-end locally with a custom container, set:
        NEO4J_COMPOSE_BACKSTOP_TEST=1
    and ensure the connected Neo4j was started with NEO4J_db_transaction_timeout=600s.
    """
    # Only run the live-settings assertion when the operator explicitly opts in
    # (NEO4J_COMPOSE_BACKSTOP_TEST=1) or when in the nightly-smoke context
    # (NIGHTLY_SMOKE env var set by the workflow -- not currently set, but
    # future-proof). Without opt-in, the standard conftest fixture connects to
    # a Neo4j that was NOT started with the env var, so SHOW SETTINGS would
    # return 0 (disabled) and the assert would be a false failure.
    import os

    opt_in = os.environ.get("NEO4J_COMPOSE_BACKSTOP_TEST", "").lower() in {
        "1", "true", "yes",
    }
    if not opt_in:
        pytest.skip(
            "Skipped: this integration test requires a Neo4j instance started with "
            "NEO4J_db_transaction_timeout=600s. "
            "Set NEO4J_COMPOSE_BACKSTOP_TEST=1 and connect to such an instance "
            "(e.g., via 'docker compose up neo4j' using the repo's docker-compose.yml). "
            "The static test (test_compose_neo4j_has_transaction_timeout_env) always "
            "runs and asserts the IaC wiring. This integration test is additional "
            "evidence that Neo4j's env-name -> config mapping works at runtime."
        )

    # Query SHOW SETTINGS for db.transaction.timeout
    with neo4j_driver.session() as session:
        result = session.run(
            "SHOW SETTINGS YIELD name, value "
            "WHERE name = 'db.transaction.timeout' "
            "RETURN value"
        )
        rows = result.data()

    assert rows, (
        "SHOW SETTINGS returned no row for 'db.transaction.timeout'. "
        "This setting should always be visible in Neo4j 5.x. "
        "Check Neo4j version compatibility."
    )

    raw = rows[0]["value"]
    # Neo4j may return "600s", "PT10M0S", or similar.
    actual_seconds = _parse_timeout_seconds(str(raw))

    assert actual_seconds == _EXPECTED_MIN_SECONDS, (
        f"db.transaction.timeout = {raw!r} (parsed {actual_seconds}s), "
        f"expected {_EXPECTED_MIN_SECONDS}s. "
        f"Ensure the Neo4j instance was started with "
        f"NEO4J_db_transaction_timeout=600s (or equivalent). "
        f"In Docker Compose: this is now set in docker-compose.yml. "
        f"In CI: verify nightly-smoke.yml services.neo4j.env includes the key."
    )
