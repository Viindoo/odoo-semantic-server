# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression: NEO4J_TEST_* must never influence pipeline._neo4j_creds().

Before this fix, `_neo4j_creds()` preferred NEO4J_TEST_* over NEO4J_*. When
.env or systemd exposed both (production password + test fixture password),
the indexer subprocess spawned by the Web UI inherited NEO4J_TEST_PASSWORD
and tried to authenticate against the real Neo4j with the test password —
producing repeating `Neo.ClientError.Security.Unauthorized` errors visible
as "Last Job: error" badges in the admin UI.

Production code paths must consult NEO4J_* only.
"""
from src.indexer import pipeline


def test_neo4j_test_password_does_not_override_prod(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://prod-host:7687")
    monkeypatch.setenv("NEO4J_USER", "prod_user")
    monkeypatch.setenv("NEO4J_PASSWORD", "real_prod_secret")
    monkeypatch.setenv("NEO4J_TEST_URI", "bolt://test-host:7687")
    monkeypatch.setenv("NEO4J_TEST_USER", "test_user")
    monkeypatch.setenv("NEO4J_TEST_PASSWORD", "fixture_password")

    uri, user, pw = pipeline._neo4j_creds()

    assert uri == "bolt://prod-host:7687"
    assert user == "prod_user"
    assert pw == "real_prod_secret"


def test_neo4j_test_vars_alone_do_not_satisfy_password_requirement(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.setenv("NEO4J_TEST_PASSWORD", "fixture_password")
    monkeypatch.setattr(
        "src.config.from_env_or_ini",
        lambda env_var, *a, **kw: None if env_var == "NEO4J_PASSWORD" else "neo4j",
    )

    import pytest
    with pytest.raises(RuntimeError, match="Neo4j password missing"):
        pipeline._neo4j_creds()
