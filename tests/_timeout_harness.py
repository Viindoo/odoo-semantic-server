# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared timeout-simulation harness for the MCP read-surface hardening tests.

No Neo4j marker — these helpers monkeypatch the driver so the timeout-surface
tests run in the fast (no-Docker) unit lane. Mirrors the `_TxTimeoutDriver` /
`_TxTimeoutSession` shape already used by ``tests/test_orm_offload_bounded.py``
so the simulation stays byte-consistent across the suite.

A tx-timeout is simulated by raising the neo4j driver's transaction-timeout
``ClientError`` (the exact code the server's ``_is_tx_timeout`` matches) from
``Session.run`` — the converting helpers ``_data_bounded`` / ``_single_bounded``
then translate it into ``OrmQueryTimeout``.
"""

from __future__ import annotations


def make_tx_timeout_error(
    code: str = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
    message: str = "transaction timed out",
):
    """Build a ClientError carrying the given Neo4j code WITHOUT the deprecated
    `.code` setter (neo4j 5.x deprecated assigning .code post-construction).
    Uses ClientError._basic_hydrate — see DRIVER-BUMP NOTE in src/mcp/orm.py."""
    from neo4j.exceptions import ClientError

    return ClientError._basic_hydrate(neo4j_code=code, message=message)


# An explicit, sentinel-free version string. The timeout-surface tests pass this
# as an EXPLICIT version so ``_resolve_version`` short-circuits at Tier-1 without
# touching the (timing-out) session — the very first bounded query is then the
# hot path under test.
TIMEOUT_TEST_VERSION = "99.0"


class _TxTimeoutSession:
    """Context-manager Neo4j session whose ``.run()`` always raises a tx-timeout."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, *a, **k):
        raise make_tx_timeout_error()


class _TxTimeoutDriver:
    """Driver stub whose every session raises a tx-timeout on ``.run()``."""

    def session(self, *a, **k):
        return _TxTimeoutSession()


def assert_clean_timeout_string(result) -> None:
    """Assert *result* is a clean ADR-0023 timeout string (no internals leaked).

    Checks: it is a ``str``, says "timed out", and leaks none of the Cypher /
    traceback tokens the raw-text contract forbids.
    """
    assert isinstance(result, str), f"expected clean str, got {type(result)!r}"
    assert "timed out" in result.lower(), f"not a timeout message: {result!r}"
    for token in ("MATCH ", "RETURN ", "session.run", "Traceback"):
        assert token not in result, f"leaked internal text {token!r}: {result!r}"
