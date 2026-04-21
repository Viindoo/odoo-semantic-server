"""Unit tests for tenancy.py — no DB required."""

from __future__ import annotations

import pytest

from osm.server.tenancy import (
    InvalidTenantError,
    context_from_env,
    context_from_tenant,
    validate_tenant,
)


def test_validate_public_ok() -> None:
    assert validate_tenant("public") == "public"


def test_validate_lowercase_ident_ok() -> None:
    assert validate_tenant("viindoo") == "viindoo"
    assert validate_tenant("cust_acme_1") == "cust_acme_1"


def test_validate_rejects_uppercase() -> None:
    with pytest.raises(InvalidTenantError):
        validate_tenant("Viindoo")


def test_validate_rejects_semicolon() -> None:
    with pytest.raises(InvalidTenantError):
        validate_tenant("a; DROP TABLE users;")


def test_validate_rejects_leading_digit() -> None:
    with pytest.raises(InvalidTenantError):
        validate_tenant("1tenant")


def test_context_public_single_schema() -> None:
    ctx = context_from_tenant("public")
    assert ctx.schemas == ("public",)


def test_context_tenant_overlays_public() -> None:
    ctx = context_from_tenant("viindoo")
    assert ctx.schemas == ("public", "viindoo")
    assert ctx.tenant == "viindoo"


def test_context_from_env_default_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OSM_TENANT", raising=False)
    ctx = context_from_env()
    assert ctx.tenant == "public"


def test_context_from_env_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSM_TENANT", "cust_acme")
    ctx = context_from_env()
    assert ctx.tenant == "cust_acme"
    assert ctx.schemas == ("public", "cust_acme")
