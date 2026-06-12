# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/orm.py
"""ORM-level validation tools (M10.5 Phase 2) — shared bottom layer + facade.

Four standalone MCP tools validate ORM constructs against the indexed Field/
Method graph *before* an AI client suggests them to a user:

- ``resolve_orm_chain``  — walk a dotted field path, return terminal type.
- ``validate_domain``    — check each domain term's field-path + operator.
- ``validate_depends``   — check ``@api.depends`` dependency paths.
- ``validate_relation``  — assert a field points at an expected comodel.

They read Neo4j Field/Method nodes (tagged by ``odoo_version`` at index time) so
the tools themselves are version-agnostic; the only version-aware logic is the
domain operator set (``valid_domain_operators`` in constants) and the era1 gate
in validate_depends (v8/v9 have no decorator depends — ``Method.depends`` empty).

MODULE LAYOUT (B2 structural split — NO behavior change):

- THIS module owns the shared BOTTOM LAYER: the per-query timeout infra
  (``OrmQueryTimeout`` + ``_is_tx_timeout`` + ``_lookup_timeout`` /
  ``_relation_timeout`` + ``_bounded``), the tenant-scope lazy shims
  (``_effective_allowed`` / ``_scope`` / ``_scope_pred`` → ``src.mcp.server``),
  and ``_edition_rank_cypher`` (the CE/EE tiebreak SSOT used by both layers).
- ``src/mcp/orm_queries.py`` owns the inherited-aware query helpers
  (``_lookup_field``, ``_traverse_field_chain``, the ``_*_with_inherited`` trio
  for fields + methods, ``_ancestor_owner_names``, ``_field_names_on_model`` and
  the ancestor-tagging Cypher prologue constants).
- ``src/mcp/orm_validators.py`` owns the four validator impls plus
  ``_parse_domain`` / ``_suggest`` / ``_broken_reason_text``.

This module RE-EXPORTS every public name from those two modules at the end of
the file, so all callers keep importing them via ``src.mcp.orm`` unchanged
(server.py / listings.py / tools/orm_tools.py / resources.py + the test suite).

Late imports of ``src.mcp.server`` avoid a circular dependency (server.py
imports this module to register the four ``@mcp.tool`` wrappers), mirroring
``src/mcp/inspect.py``.

See docs/adr/0023-tool-output-completeness.md (tree-grammar contract),
ADR-0048 (same-name INHERITS topology + ORM read bounds), and
TASKS.md M10.5 Phase 2.
"""
import neo4j
from neo4j.exceptions import ClientError

from src.constants import (
    EDITION_PRIORITY,
    EDITION_PRIORITY_ELSE,
    NEO4J_QUERY_TIMEOUT_SECONDS,
)


def _edition_rank_cypher(node_alias: str = "mod") -> str:
    """Cypher CASE expression for edition priority — mirrors server._edition_rank_cypher.

    Lower rank = higher priority (community=0 < enterprise=1 < viindoo=2 < oca=3).
    Used by the inherited-field/method dedup ORDER BY so the CE vs EE tiebreak
    matches the 5-tier ranking in server.py._resolve_field / _resolve_method.
    SSOT for the priority values is EDITION_PRIORITY in src/constants.py.
    """
    cases = " ".join(
        f"WHEN '{ed}' THEN {rank}"
        for ed, rank in sorted(EDITION_PRIORITY.items(), key=lambda x: x[1])
    )
    return (
        f"CASE {node_alias}.edition {cases} ELSE {EDITION_PRIORITY_ELSE} END"
        f" AS edition_rank"
    )


# Status codes raised when a transaction exceeds its timeout. There are TWO:
#   - Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration
#     is returned when the timeout comes from the *driver* (our per-query
#     neo4j.Query(text, timeout=...)) — verified against neo4j 5.28 + server 5.26.
#   - Neo.ClientError.Transaction.TransactionTimedOut
#     is returned when the timeout comes from the *server* config
#     (db.transaction.timeout, which Wave-0 sets to 600s on prod).
# We match the common prefix so BOTH surface as OrmQueryTimeout; any other
# ClientError (syntax, constraint, ...) still propagates unchanged.
#
# DRIVER-BUMP NOTE (L12): this reads exc.code (legacy Neo4j status string).
# neo4j-python driver 6.x moves to GQLSTATUS and may change how the code is
# exposed (e.g. exc.gql_status / a different attribute). When bumping the driver,
# re-verify _is_tx_timeout still matches both timeout variants, and update the
# matcher + the timeout-path test in tests/test_orm_dense_inheritance.py
# (which currently constructs the error by setting exc.code, itself deprecated).
_TX_TIMEOUT_CODE_PREFIX = "Neo.ClientError.Transaction.TransactionTimedOut"


class OrmQueryTimeout(Exception):
    """A bounded ORM read query exceeded NEO4J_QUERY_TIMEOUT_SECONDS.

    Carries a user-facing English message (ADR-0023 tone, no Cypher leaked).
    Interface contract with the MCP wrapper layer (server.py): the wrapper
    catches this, increments the timeout metric, and returns ``user_message``
    to the client. The traversal/validation helpers deliberately do NOT
    catch-and-render it — they let it propagate to that wrapper.
    """

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def _is_tx_timeout(exc: ClientError) -> bool:
    """True when a ClientError is a transaction-timeout (driver- or server-set)."""
    return (getattr(exc, "code", None) or "").startswith(_TX_TIMEOUT_CODE_PREFIX)


def _lookup_timeout(field: str, model: str, version: str) -> "OrmQueryTimeout":
    """Build an OrmQueryTimeout for a field-resolution timeout (ADR-0023 tone)."""
    return OrmQueryTimeout(
        f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while resolving "
        f"field '{field}' on '{model}' (Odoo {version}). The inheritance graph "
        f"for this model may be unusually dense - try a more specific model or "
        f"retry later."
    )


def _relation_timeout(comodel: str, target: str, version: str) -> "OrmQueryTimeout":
    """Build an OrmQueryTimeout for a relation subtype-check timeout (ADR-0023 tone)."""
    return OrmQueryTimeout(
        f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while checking "
        f"whether '{comodel}' is a subtype of '{target}' (Odoo {version}). The "
        f"inheritance graph may be unusually dense - try a more specific model "
        f"or retry later."
    )


def _bounded(text: str) -> "neo4j.Query":
    """Wrap Cypher text in a neo4j.Query carrying the per-query timeout.

    ``session.run`` does not accept a ``timeout`` kwarg for auto-commit
    transactions, but a ``neo4j.Query`` object does — this is the least-invasive
    way to bound every ORM read (issue #273).
    """
    return neo4j.Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)


def _effective_allowed(profile_name):
    """Lazy shim — avoids circular import (server imports orm at module level).

    Delegates to src.mcp.server._effective_allowed for the tenant boundary +
    profile_name narrowing logic (ADR-0034 WI-4, C2 enforcement).
    """
    from src.mcp.server import _effective_allowed as _ea  # lazy: avoid circular import
    return _ea(profile_name)


def _scope(profile_name=None):
    """Lazy shim → src.mcp.server._scope (Neo4j own/shared array-filter params)."""
    from src.mcp.server import _scope as _s  # lazy: avoid circular import
    return _s(profile_name)


def _scope_pred(alias: str) -> str:
    """Lazy shim → src.mcp.server._scope_pred (canonical fail-closed predicate)."""
    from src.mcp.server import _scope_pred as _sp  # lazy: avoid circular import
    return _sp(alias)


# ---------------------------------------------------------------------------
# Facade re-exports (B2 split - preserve the ``src.mcp.orm.<name>`` import path)
# ---------------------------------------------------------------------------
#
# The query helpers + validator impls were moved to orm_queries.py /
# orm_validators.py but every caller (server.py, listings.py, tools/orm_tools.py,
# resources.py, and ~10 test files) imports them via ``src.mcp.orm``. The block
# below re-exports them so that path stays intact with ZERO behavior change.
#
# This sits AFTER the bottom-layer definitions above so that, when orm_queries /
# orm_validators (which import _bounded / _scope / _edition_rank_cypher / the
# timeout infra FROM this module) are loaded, those names are already defined -
# the same circular-safe ordering used by the server tool-split (refactor plan
# section 2.2).
#
# WHY ``import module`` + ``_rebind`` instead of ``from child import name``:
# this module is BOTH the bottom-layer home AND the facade, so it forms a cycle
# with each child (child top-imports the bottom layer FROM here; here we import
# the child back). On the production path (``import src.mcp.orm`` or via server),
# the child fully loads before this tail runs, so the rebind succeeds. On a COLD
# direct ``import src.mcp.orm_queries`` (no production caller does this, but
# pytest collection could), the child is the entry point and is only
# partially initialized when its bottom-layer import re-enters this tail; a
# plain ``from child import name`` would raise ImportError there. ``import
# <module>`` only needs the (already-registered) module object, never a
# not-yet-bound attribute, so it never raises during that race - and ``_rebind``
# copies whatever public names the child has defined so far. The child then
# finishes loading and re-runs nothing; the facade names land on the
# fully-initialized ``src.mcp.orm`` that the production path observes.
import src.mcp.orm_queries as _orm_queries  # noqa: E402
import src.mcp.orm_validators as _orm_validators  # noqa: E402

_QUERY_REEXPORTS = (
    "_ANCESTOR_TAGGED_PROLOGUE",
    "_ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY",
    "_EDGE_KIND_EXPR",
    "_ancestor_owner_names",
    "_ancestor_tagged_prologue",
    "_count_fields_with_inherited",
    "_count_methods_with_inherited",
    "_field_names_on_model",
    "_list_fields_with_inherited",
    "_list_methods_with_inherited",
    "_lookup_field",
    "_resolve_field_inherited",
    "_resolve_method_inherited",
    "_traverse_field_chain",
)
_VALIDATOR_REEXPORTS = (
    "_broken_reason_text",
    "_parse_domain",
    "_resolve_orm_chain",
    "_suggest",
    "_validate_depends",
    "_validate_domain",
    "_validate_relation",
)


def _rebind(module, names) -> None:
    """Copy each child name onto this module's namespace (facade re-export)."""
    g = globals()
    for _name in names:
        if hasattr(module, _name):
            g[_name] = getattr(module, _name)


_rebind(_orm_queries, _QUERY_REEXPORTS)
_rebind(_orm_validators, _VALIDATOR_REEXPORTS)
