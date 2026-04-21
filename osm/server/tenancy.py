"""Tenant resolution + validation for the MCP server.

P1 dev topology: tenant comes from `OSM_TENANT` env var (or explicit param).
Hosted auth layer (P5) replaces this with a token-derived tenant, but
handlers keep reading from a pluggable context object so the change stays
isolated to this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_TENANT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$|^public$")


class InvalidTenantError(ValueError):
    """Raised when a tenant identifier fails validation."""


def validate_tenant(name: str) -> str:
    """Return `name` if it matches the safe-identifier pattern, else raise."""
    if not _TENANT_PATTERN.match(name):
        raise InvalidTenantError(
            f"invalid tenant {name!r}; must match ^[a-z][a-z0-9_]{{1,62}}$ or be 'public'"
        )
    return name


@dataclass(frozen=True)
class TenantContext:
    """Per-request tenant context consumed by every handler.

    `schemas` lists the schemas a query should UNION ALL across, in load-order
    precedence: `public` first (shared Odoo CE index), tenant last (customer
    overlay). For P1 a tenant of `public` collapses to a single-schema query.
    """

    tenant: str
    schemas: tuple[str, ...]


def context_from_env(env_var: str = "OSM_TENANT") -> TenantContext:
    """Build a TenantContext using the named env var (default OSM_TENANT).

    Defaults to `public` when the var is unset so local dev-loop usage against
    a single schema does not need any env plumbing.
    """
    name = os.environ.get(env_var, "public")
    validated = validate_tenant(name)
    if validated == "public":
        return TenantContext(tenant="public", schemas=("public",))
    return TenantContext(tenant=validated, schemas=("public", validated))


def context_from_tenant(name: str) -> TenantContext:
    """Explicit-tenant constructor used by tests and the CLI."""
    validated = validate_tenant(name)
    if validated == "public":
        return TenantContext(tenant="public", schemas=("public",))
    return TenantContext(tenant=validated, schemas=("public", validated))
