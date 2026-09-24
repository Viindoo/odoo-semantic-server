# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j.py
import logging
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from neo4j import GraphDatabase, NotificationMinimumSeverity, Query
from neo4j.exceptions import DriverError, Neo4jError

from src.constants import (
    NEO4J_DELETE_BATCH_ROWS,
    NEO4J_WRITE_BATCH_SIZE,
    REL_INHERITS,
)

from .diff_engine import DiffResult
from .framework_bases import KNOWN_FRAMEWORK_BASE_NAMES
from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    LintViolationInfo,
    ModuleOwner,
    ParseResult,
    PatternExample,
    StylesheetInfo,
    TestHelperInfo,  # WI-1
    TestParseResult,  # WI-1
    ViewParseResult,
)

_logger = logging.getLogger(__name__)

# Soft-drop gate for the version-scoped spec prunes (LintRule/CLICommand/CLIFlag,
# issue #364 follow-up). If a single index-core would delete MORE than this
# fraction of a version's existing nodes, the prune is SKIPPED with a WARNING
# instead of applied - the skip-and-warn shape of ADR-0005's ">20% CoreSymbol
# drop = suspect path refactor". This protects
# against a degraded parse (e.g. a checkout missing odoo/addons/test_lint/tests/)
# silently wiping a whole version's curated rows. See ADR-0055.
_PRUNE_SOFT_DROP_MAX_FRACTION = 0.5

# --- Module retirement cascade (ADR-0056 D9) --------------------------------
# Every node label the writers attach to ONE module at one version. This is the
# single source of truth for the retirement cascade (retire_modules), the owner
# reset (drop_module_owner) and the child-orphan finder (orphan_child_keys).
# Membership rule: the label is written by a module's own index run and is
# selected by a module-scoped predicate:
#   * carries a ``module`` property naming the owning module (MERGE key or SET):
#     Model, Field, Method, View, QWebTmpl, Report, JSPatch, OWLComp,
#     Stylesheet, JsTestSuite, TestClass, TestMethod, TestHelper (addon helpers
#     and finalize_is_helper projections - never module='@framework');
#   * has NO ``module`` property but belongs to one of the module's Views:
#     LintViolation, via ``view_xmlid`` or the (:View)-[:HAS_VIOLATION]-> edge.
# A new label written with a ``module`` property must be added here or to
# MODULE_SHARED_LABELS; the structural test compares both against the writers.
MODULE_CHILD_LABELS: tuple[str, ...] = (
    "LintViolation",
    "Method",
    "Field",
    "Model",
    "View",
    "QWebTmpl",
    "Report",
    "JSPatch",
    "OWLComp",
    "Stylesheet",
    "JsTestSuite",
    "TestMethod",
    "TestClass",
    "TestHelper",
)

# Labels that carry a ``module`` property but are version-global and shared by
# many modules; they are NEVER part of a per-module cascade. AssetBundle stores
# its first contributor as ``module`` (ON CREATE only) - deleting it with that
# module would cut every other contributor's CONTRIBUTES_TO edge. Reclaimed by
# gc_orphan_asset_bundles once nothing references it.
MODULE_SHARED_LABELS: tuple[str, ...] = ("AssetBundle",)

# Sentinel ``module`` values owned by no repo: framework test bases
# (TestHelper module='@framework', seeded per version) and forward-reference
# placeholders (module='__unresolved__', reclaimed by gc_unresolved_placeholders).
# The cascade and the orphan finders never select them.
NON_RETIRABLE_MODULE_NAMES: frozenset[str] = frozenset({"@framework", "__unresolved__"})

# Cypher predicate selecting the LintViolation nodes (bound as ``lv``) owned by
# the modules in ``$names`` at ``$v``; ``$xmlids`` = those modules' View xmlids.
# LintViolation has no ``module`` property: it belongs to the module whose View
# it was raised on - by xmlid, by HAS_VIOLATION edge, or (View already gone) by
# the ``<module>.`` prefix of ``view_xmlid``. Shared by retire_modules and
# drop_module_owner so both act on the same set.
_MODULE_LINT_VIOLATION_PREDICATE = """
    lv.view_xmlid IN $xmlids
    OR EXISTS {
        MATCH (owner_view:View)-[:HAS_VIOLATION]->(lv)
        WHERE owner_view.module IN $names
    }
    OR (split(coalesce(lv.view_xmlid, ''), '.')[0] IN $names
        AND NOT EXISTS {
            MATCH (:View {xmlid: lv.view_xmlid, odoo_version: $v})
        })
"""

def _instant(expr: str) -> str:
    """Cypher: the instant of DateTime *expr* in epoch milliseconds.

    Every guard that orders a server stamp (``written_at``, ``last_seen_at``)
    against a run start compares through this, never with ``<`` / ``>=`` on
    the DateTime values: Cypher orders two DateTimes of the SAME instant by
    their zone (``datetime()`` carries offset ``Z``, a Python UTC datetime
    parameter arrives as zone id ``UTC``), so a stamp taken in the same
    millisecond as the run start compared as earlier than it. Epoch
    milliseconds are zone-free and match the resolution of the server's
    statement clock (``datetime()``), so a stamp in the run's first
    millisecond counts as at-or-after the start.
    """
    return f"{expr}.epochMillis"


def _written_before_run(alias: str) -> str:
    """Cypher predicate: child *alias* was not written at or after ``$run_at``.

    The cascade guard of :meth:`Neo4jWriter.retire_modules`: a child a
    concurrent run MERGEd (and stamped) after the retiring run started is
    never deleted; one without ``written_at`` (derived projections, nodes
    written before the stamp existed) counts as stale.
    """
    return (
        f"({alias}.written_at IS NULL "
        f"OR {_instant(f'{alias}.written_at')} < {_instant('$run_at')})"
    )


# Transient Neo4j failures (deadlock against a concurrent MERGE, leader switch,
# lost connection) are retried per cascade step. Every step is an idempotent
# DETACH DELETE / SET, so replaying a step after a partial commit is safe.
_CASCADE_RETRY_ATTEMPTS = 4
_CASCADE_RETRY_BACKOFF_S = 0.5


def _profile_union_set(alias: str) -> str:
    """Cypher fragment for ON MATCH SET union-add of profile names (write-side).

    Returns the canonical dedup-add expression used by every defining node's
    ``ON MATCH SET <alias>.profile = ...`` clause:

        [x IN coalesce(<alias>.profile, []) WHERE NOT x IN $profiles] + $profiles

    Union-only by construction (ADR-0034): never resets, never removes an
    existing owner — it appends $profiles after stripping any names already
    present, so a node co-owned by a genuine collision keeps BOTH owners and
    stays fail-closed at the ADR-0034 read-side choke. Mirrors the read-side
    ``_scope_pred`` builder in src/mcp/server.py — SEE ALSO that function: the
    write-side union shape here and the read-side predicate there are coupled
    (a change to one's profile/empty-node semantics must be reflected in both).

    The ``$profiles`` token is a literal Cypher parameter in the returned string;
    it is bound by the caller's ``tx.run(..., profiles=...)`` kwargs and is NOT
    an f-string variable here.
    """
    return f"[x IN coalesce({alias}.profile, []) WHERE NOT x IN $profiles] + $profiles"


def _chunked(items, size):
    """Yield successive chunks of `items` of length up to `size`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _require_aware_datetime(value, param: str) -> datetime:
    """Return *value* as a timezone-aware ``datetime`` or raise ValueError.

    The cascade guard compares ``Module.last_seen_at`` (a zoned Cypher
    ``datetime()``) with this value. A naive Python datetime is sent as a
    Cypher LocalDateTime, which has no instant - the guard would silently
    match nothing (or everything, if negated). Accepts a neo4j ``DateTime``
    too (``to_native()``). The result is converted to the fixed ``UTC``
    offset, so a value stamped from it is stored in the same form as the
    server's own ``datetime()``.
    """
    if hasattr(value, "to_native"):
        value = value.to_native()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(
            f"{param} must be a timezone-aware datetime (got {value!r}); use "
            "Neo4jWriter.server_now() so it shares the Neo4j server clock"
        )
    return value.astimezone(UTC)


def _is_transient_neo4j_error(exc: BaseException) -> bool:
    """True for errors a replay of the same idempotent statement can clear."""
    if isinstance(exc, (Neo4jError, DriverError)) and exc.is_retryable():
        return True
    # A deadlock raised inside CALL {} IN TRANSACTIONS can reach the client
    # wrapped in a non-transient outer status; the inner code survives in the
    # message.
    return "DeadlockDetected" in f"{getattr(exc, 'code', '')} {exc}"


def _run_single_with_retry(session, what: str, query: str, **params):
    """``session.run(query).single()`` retried on transient errors.

    For auto-commit statements only (CALL {} IN TRANSACTIONS cannot run in a
    managed transaction). Callers pass idempotent statements (DETACH DELETE /
    SET / read), so a replay after a partially committed batch is safe.
    """
    for attempt in range(1, _CASCADE_RETRY_ATTEMPTS + 1):
        try:
            return session.run(query, **params).single()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if attempt == _CASCADE_RETRY_ATTEMPTS or not _is_transient_neo4j_error(exc):
                raise
            delay = _CASCADE_RETRY_BACKOFF_S * (2 ** (attempt - 1))
            _logger.warning(
                "%s: transient Neo4j error (attempt %d/%d): %s - retrying in %.1fs",
                what, attempt, _CASCADE_RETRY_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    return None  # unreachable: the last attempt returns or raises


# PatternExample indexes (M4.6 pattern layer, ADR-0003) — single source of truth
# shared by setup_indexes() (full schema) and setup_pattern_indexes()
# (patterns-only reseed) so the two paths can never drift. Adding a
# PatternExample index here updates BOTH (CLAUDE.md: duplicate = SoT conflict);
# tests/test_writer_setup_pattern_indexes.py guards that setup_indexes() still
# contains every statement in this tuple.
_PATTERN_EXAMPLE_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
    " ON (n.pattern_id)",
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
    " ON (n.language, n.odoo_version_min)",
    "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
    " ON (n.category)",
)


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        # notifications_min_severity=WARNING is a Bolt-level, SERVER-SIDE filter
        # (neo4j 5.28.4): the DBMS simply never returns INFORMATION-severity
        # notifications, so the driver's neo4j.notifications logger is never
        # invoked for them. This is the write path — every `setup_indexes()` /
        # `setup_pattern_indexes()` run against an already-indexed DB emits one
        # expected `IndexOrConstraintAlreadyExists` INFORMATION notice per
        # `CREATE INDEX IF NOT EXISTS`, which carries no actionable signal (the
        # IF NOT EXISTS guard is the whole point). Genuine WARNING/ERROR
        # notifications are still returned and logged. This is deliberately NOT
        # applied to the MCP READ driver (src/mcp/server.py) — read queries may
        # surface useful INFORMATION-level hints (e.g. cartesian product) we
        # want to keep.
        self.driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            notifications_min_severity=NotificationMinimumSeverity.WARNING,
        )
        # Per-query timeout (seconds) of the lifecycle READ methods
        # (orphan_module_names, module_profiles, module_identity,
        # modules_by_old_technical_name, orphan_child_keys,
        # modules_without_profile). None = bounded only by the
        # server's db.transaction.timeout, as every indexer statement;
        # ``lifecycle-audit`` sets LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS.
        self.read_timeout_s: float | None = None

    def close(self) -> None:
        self.driver.close()

    def _read_query(self, text: str) -> str | Query:
        """*text* bounded by :attr:`read_timeout_s` when it is set."""
        if self.read_timeout_s is None:
            return text
        return Query(text, timeout=self.read_timeout_s)

    def setup_indexes(self) -> None:
        with self.driver.session() as session:
            for stmt in [
                "CREATE INDEX IF NOT EXISTS FOR (n:Module) ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Model)  ON (n.name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Field)"
                " ON (n.name, n.model, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Method)"
                " ON (n.name, n.model, n.module, n.odoo_version)",
                # T1: enable index-backed lookup by (model, odoo_version) without name
                # Covers impact_analysis Q3 (field/model entity_type) — avoids full scan
                # on deep-inheritance models (sale.order has 50+ extending modules).
                "CREATE INDEX IF NOT EXISTS FOR (n:Method)"
                " ON (n.model, n.odoo_version)",
                # T2: per-hop anchor lookup for ORM read rewrite (#273).
                # Each hop in the per-hop name-dedup CALL subquery MATCHes
                # Model(name, odoo_version) — without this index that is a full label
                # scan repeated for every ancestor set expansion.
                "CREATE INDEX IF NOT EXISTS FOR (n:Model)"
                " ON (n.name, n.odoo_version)",
                # T3: _field_names_on_model helper lookup (#273).
                # Covers the "did you mean" field-suggestion path that queries
                # Field(model, odoo_version) — currently a label scan on every miss.
                "CREATE INDEX IF NOT EXISTS FOR (n:Field)"
                " ON (n.model, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:View) ON (n.xmlid, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:QWebTmpl) ON (n.xmlid, n.odoo_version)",
                # GAP-2/GAP-5 report layer: composite key (xmlid, odoo_version)
                # backs the Report MERGE; the (model, odoo_version) index backs the
                # entity_lookup(kind='report', model=...) lookup by business model.
                "CREATE INDEX IF NOT EXISTS FOR (n:Report) ON (n.xmlid, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Report) ON (n.model, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:JSPatch)"
                " ON (n.target, n.patch_name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:OWLComp)"
                " ON (n.name, n.module, n.odoo_version)",
                # WI-D asset-bundle layer (ADR-0052): composite key (name, odoo_version).
                # Backs the EXTENDS_ASSET_BUNDLE base-lookup (resolves v15+ legacy
                # <template inherit_id="web.assets_backend"> extenders) + CONTRIBUTES_TO
                # / INCLUDES_BUNDLE writes — all (name, odoo_version)-keyed.
                "CREATE INDEX IF NOT EXISTS FOR (n:AssetBundle)"
                " ON (n.name, n.odoo_version)",
                # M4.5 spec layer (per ADR-0002):
                "CREATE INDEX IF NOT EXISTS FOR (n:CoreSymbol)"
                " ON (n.qualified_name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:LintRule)"
                " ON (n.rule_id, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:CLICommand)"
                " ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:CLIFlag)"
                " ON (n.flag_name, n.command_name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:SpecMetadata)"
                " ON (n.kind, n.odoo_version)",
                # M4.6 pattern layer (per ADR-0003) — shared SoT (module constant):
                *_PATTERN_EXAMPLE_INDEX_STATEMENTS,
                # WI-A1 stylesheet layer (per ADR-0025):
                "CREATE INDEX IF NOT EXISTS FOR (n:Stylesheet)"
                " ON (n.file_path, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Stylesheet)"
                " ON (n.module, n.odoo_version)",
                # WI-E RelaxNG lint violation layer (M11):
                "CREATE INDEX IF NOT EXISTS FOR (n:LintViolation)"
                " ON (n.file_path, n.line, n.rule, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:LintViolation)"
                " ON (n.view_xmlid, n.odoo_version)",
                # WI-1: test surface index layer (§2.7)
                # CRITICAL-1 + Defect H: MERGE key now includes repo (5-part).
                "CREATE INDEX IF NOT EXISTS FOR (n:TestClass)"
                " ON (n.name, n.module, n.file_path, n.repo, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestClass)"
                " ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestClass)"
                " ON (n.module, n.repo, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestMethod)"
                " ON (n.name, n.test_class, n.module, n.file_path, n.repo, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestMethod)"
                " ON (n.test_class, n.module, n.repo, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestMethod)"
                " ON (n.module, n.repo, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestHelper)"
                " ON (n.name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:TestHelper)"
                " ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:JsTestSuite)"
                " ON (n.file_path, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:JsTestSuite)"
                " ON (n.module, n.odoo_version, n.framework)",
                # Module retirement cascade + child-orphan finder (ADR-0056):
                # every MODULE_CHILD_LABELS member selected by `module` needs a
                # (module, odoo_version) lookup, not a label scan.
                *(
                    f"CREATE INDEX IF NOT EXISTS FOR (n:{_label})"
                    " ON (n.module, n.odoo_version)"
                    for _label in (
                        "Model", "Field", "Method", "View", "QWebTmpl",
                        "Report", "JSPatch", "OWLComp", "TestHelper",
                    )
                ),
                # Version-wide lifecycle reads (orphan_child_keys, the orphan
                # sweep, lifecycle-audit) select `{odoo_version: $v}` with
                # `module IS NOT NULL`: a composite index is seekable only on
                # its leading key, so these lead with odoo_version. A composite
                # range index holds only nodes carrying every property, hence
                # LintViolation (usually module-less) gets odoo_version alone.
                *(
                    f"CREATE INDEX IF NOT EXISTS FOR (n:{_label})"
                    " ON (n.odoo_version, n.module)"
                    for _label in MODULE_CHILD_LABELS
                    if _label != "LintViolation"
                ),
                "CREATE INDEX IF NOT EXISTS FOR (n:LintViolation) ON (n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Module) ON (n.odoo_version, n.name)",
            ]:
                session.run(stmt)

    def setup_pattern_indexes(self) -> None:
        """Create ONLY the PatternExample indexes (patterns-only reseed path).

        A patterns reseed (``seed_patterns._write_neo4j``) writes only
        ``PatternExample`` nodes, so it does not need the full ~33-statement
        schema setup that :meth:`setup_indexes` issues for every node label.
        Running the full setup against an already-indexed DB is harmless
        (``IF NOT EXISTS`` no-ops) but emits ~30 unrelated
        ``IndexOrConstraintAlreadyExists`` notifications. Both this method and
        :meth:`setup_indexes` draw their statements from
        :data:`_PATTERN_EXAMPLE_INDEX_STATEMENTS` (single source of truth) so
        the two paths cannot drift. The full indexer (``pipeline.py`` /
        ``indexer/__main__.py``) still calls :meth:`setup_indexes` for every
        index on a fresh DB.
        """
        with self.driver.session() as session:
            for stmt in _PATTERN_EXAMPLE_INDEX_STATEMENTS:
                session.run(stmt)

    def write_results(
        self,
        results: list[ParseResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist ParseResult nodes (Module/Model/Field/Method).

        *profiles* is the ancestor profile name array (self at index 0, root last)
        written as a ``profile`` list property on every node. Empty list written
        when caller doesn't supply a value (backward-compat for unit tests).
        """
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_parse_result, result, _profiles)

    def write_view_results(
        self,
        results: list[ViewParseResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist View and QWebTmpl nodes. *profiles* written as node property."""
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_view_parse_result, result, _profiles)

    def write_js_graph_results(
        self,
        results: list[JSGraphResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist OWLComp and JSPatch nodes. *profiles* written as node property."""
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_js_graph_result, result, _profiles)

    def write_asset_results(
        self,
        results: list,
        profiles: list[str] | None = None,
    ) -> None:
        """Persist :AssetBundle nodes + CONTRIBUTES_TO/INCLUDES_BUNDLE edges (WI-D).

        MUST be called BEFORE write_view_results so the legacy
        ``<template inherit_id="web.assets_backend">`` extenders (written in the
        view/qweb pass) can resolve against the AssetBundle base nodes via the
        EXTENDS_ASSET_BUNDLE fallback. *profiles* written as node property
        (ADR-0034 single-owner provenance, same as every other writer).
        """
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_asset_parse_result, result, _profiles)

    # --- M4.5 spec layer (CoreSymbol + diff edges) -------------------------

    def write_core_symbols(self, symbols: list[CoreSymbolInfo]) -> None:
        """Persist a batch of CoreSymbol nodes (idempotent MERGE).

        Composite key: (qualified_name, odoo_version). Mutable props (kind,
        signature, file_path, line, status, replacement_qname) updated via SET.
        Batched at 500/transaction to stay under driver memory budget.
        """
        if not symbols:
            return
        with self.driver.session() as session:
            for batch in _chunked(symbols, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_core_symbols_batch, batch)

    def write_diff_edges(
        self, diff: DiffResult, *, from_version: str, to_version: str,
    ) -> None:
        """Persist cross-version diff edges (currently REPLACED_BY only).

        Per ADR-0002 §2: ADDED_IN / REMOVED_IN are represented via `cs.status`
        property (set during write_core_symbols), not as separate edges. Only
        REPLACED_BY needs an actual edge because it links two distinct nodes.
        """
        if not diff.replaced:
            return
        with self.driver.session() as session:
            session.execute_write(
                _write_replaced_by_edges,
                diff.replaced, from_version, to_version,
            )

    def write_lint_rules(self, rules: list[LintRuleInfo]) -> None:
        """Persist a batch of LintRule nodes (idempotent MERGE).

        Composite key: (rule_id, odoo_version). Optionally creates a
        CHECKS edge to a CoreSymbol when `core_symbol_qname` is set and
        the target node already exists.
        """
        if not rules:
            return
        with self.driver.session() as session:
            for batch in _chunked(rules, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_lint_rules_batch, batch)

    def write_cli_commands(self, commands: list[CLICommandInfo]) -> None:
        """Persist CLICommand nodes (idempotent MERGE on (name, odoo_version))."""
        if not commands:
            return
        with self.driver.session() as session:
            for batch in _chunked(commands, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_cli_commands_batch, batch)

    def write_cli_flags(self, flags: list[CLIFlagInfo]) -> None:
        """Persist CLIFlag nodes + OF_COMMAND edges (when target CLICommand exists)."""
        if not flags:
            return
        with self.driver.session() as session:
            for batch in _chunked(flags, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(_write_cli_flags_batch, batch)

    def write_cli_flag_replacements(
        self,
        replaced: list[tuple[str, str]],
        *,
        command_name: str,
        from_version: str,
        to_version: str,
    ) -> None:
        """Persist REPLACED_BY edges between CLIFlag nodes."""
        if not replaced:
            return
        with self.driver.session() as session:
            session.execute_write(
                _write_cli_flag_replacements,
                replaced, command_name, from_version, to_version,
            )

    def _prune_versioned_spec_nodes(
        self,
        *,
        label: str,
        odoo_version: str,
        key_expr: str,
        live_values: list[str],
        method_name: str,
    ) -> int:
        """Shared version-scoped prune for the MERGE-only spec writers (#364).

        DETACH DELETE every ``label`` node at ``odoo_version`` whose identity
        (``key_expr``, a Cypher expression over the matched node ``n``) is NOT in
        ``live_values`` - the set produced by THIS run's full write for THIS
        version.  Two safety guards, both mandatory:

        * **EMPTY-GUARD:** an empty ``live_values`` NEVER deletes (returns 0). A
          transient/degraded parse that produced no rows must never wipe a
          version. Mirrors the ``write_pattern_examples`` empty-guard.
        * **SOFT-DROP GATE:** if the prune would delete more than
          :data:`_PRUNE_SOFT_DROP_MAX_FRACTION` of the version's existing nodes,
          it is SKIPPED with a WARNING and returns 0 - the skip-and-warn shape
          of ADR-0005's ">20% CoreSymbol drop = suspect path refactor". This
          catches a checkout that
          silently lost its source (e.g. ``odoo/addons/test_lint/tests/``) before
          it can delete the whole version's curated set.

        Version-scoped by construction: the MATCH is bound to ``odoo_version`` and
        the delete predicate only ever compares within that version, so a prune
        for one version can never touch another (each version is written +
        pruned with its own live set). CoreSymbol is deliberately NOT pruned by
        any method (its cross-version lifecycle - added_in/removed_in/
        deprecated_in + REPLACED_BY - must be preserved; see ADR-0055).

        Returns the number of nodes deleted (0 when either guard fired).
        """
        if not live_values:
            _logger.warning(
                "%s: empty live set for version %s - skipping prune (refusing to "
                "delete every %s node for the version; suspected degraded parse)",
                method_name, odoo_version, label,
            )
            return 0
        with self.driver.session() as session:
            counts = session.run(
                f"""
                MATCH (n:{label} {{odoo_version: $v}})
                WITH count(n) AS total,
                     sum(CASE WHEN NOT ({key_expr}) IN $live THEN 1 ELSE 0 END) AS stale
                RETURN total, stale
                """,
                v=odoo_version,
                live=live_values,
            ).single()
            total = counts["total"] if counts is not None else 0
            stale = (counts["stale"] or 0) if counts is not None else 0
            if total == 0 or stale == 0:
                return 0
            if stale > total * _PRUNE_SOFT_DROP_MAX_FRACTION:
                _logger.warning(
                    "%s: would delete %d of %d %s node(s) for version %s "
                    "(> %.0f%%) - SKIPPING as a suspected degraded parse "
                    "(ADR-0005 skip-and-warn guard). Re-run a "
                    "--full index-core against a verified checkout to prune.",
                    method_name, stale, total, label, odoo_version,
                    _PRUNE_SOFT_DROP_MAX_FRACTION * 100,
                )
                return 0
            row = session.run(
                f"""
                MATCH (n:{label} {{odoo_version: $v}})
                WHERE NOT ({key_expr}) IN $live
                DETACH DELETE n
                RETURN count(n) AS deleted
                """,
                v=odoo_version,
                live=live_values,
            ).single()
            deleted = row["deleted"] if row is not None else 0
            if deleted:
                _logger.info(
                    "%s: deleted %d stale %s node(s) for version %s",
                    method_name, deleted, label, odoo_version,
                )
            return deleted

    def prune_lint_rules(
        self, odoo_version: str, live_rule_ids: Iterable[str],
    ) -> int:
        """DETACH DELETE stale LintRule nodes at ``odoo_version`` (issue #364).

        ``write_lint_rules`` is MERGE-only, so a rule_id REMOVED from a version's
        curated set (e.g. #364 dropped ``W8140`` from v14-v19) otherwise survives
        forever. ``index_core`` always writes the FULL rule set for the version,
        so an unconditional prune with that run's live id set is correct.
        Empty-guard + soft-drop gate apply (see :meth:`_prune_versioned_spec_nodes`).
        """
        return self._prune_versioned_spec_nodes(
            label="LintRule",
            odoo_version=odoo_version,
            key_expr="n.rule_id",
            live_values=list(live_rule_ids),
            method_name="prune_lint_rules",
        )

    def prune_cli_commands(
        self, odoo_version: str, live_names: Iterable[str],
    ) -> int:
        """DETACH DELETE stale CLICommand nodes at ``odoo_version`` (issue #364).

        CLICommand is MERGE-keyed on (name, odoo_version); ``index_core`` writes
        the full command set per version, so a command removed upstream is pruned
        by comparing against this run's live ``name`` set. Empty-guard + soft-drop
        gate apply.
        """
        return self._prune_versioned_spec_nodes(
            label="CLICommand",
            odoo_version=odoo_version,
            key_expr="n.name",
            live_values=list(live_names),
            method_name="prune_cli_commands",
        )

    def prune_cli_flags(
        self, odoo_version: str, live_keys: Iterable[str],
    ) -> int:
        """DETACH DELETE stale CLIFlag nodes at ``odoo_version`` (issue #364).

        CLIFlag is MERGE-keyed on (flag_name, command_name, odoo_version). The
        SAME ``flag_name`` can appear under DIFFERENT commands (distinct nodes),
        so the prune identity MUST be the composite ``flag_name|command_name``,
        not ``flag_name`` alone - otherwise a flag kept under one command would
        wrongly protect (or be protected by) a same-named flag under another.
        The caller builds ``live_keys`` the same way:
        ``{f"{f.flag_name}|{f.command_name or ''}" for f in flags}``.

        NOTE on the ``coalesce(command_name, '')``: ``command_name`` is never
        actually NULL in the stored graph - Neo4j MERGE rejects a null key
        property (``Neo.ClientError.Statement.SemanticError``), and
        ``parse_cli_flags`` defaults it to the owning command name (``"server"``
        for the global ``odoo/tools/config.py`` flags), so a bare global flag is
        stored under ``command_name="server"``, not null. The ``coalesce`` (and
        the caller's ``or ''``) are defensive belt-and-suspenders, not a live
        path. Empty-guard + soft-drop gate apply.
        """
        return self._prune_versioned_spec_nodes(
            label="CLIFlag",
            odoo_version=odoo_version,
            key_expr="n.flag_name + '|' + coalesce(n.command_name, '')",
            live_values=list(live_keys),
            method_name="prune_cli_flags",
        )

    def fetch_core_symbols(self, odoo_version: str) -> list:
        """Fetch all CoreSymbolInfo for a version from Neo4j.

        Returns a list of CoreSymbolInfo-like dicts re-constructed as CoreSymbolInfo
        objects so diff_engine can compare them. Used by index_core lifecycle diff.
        """
        from .models import CoreSymbolInfo
        with self.driver.session() as session:
            rows = session.run("""
                MATCH (cs:CoreSymbol {odoo_version: $v})
                RETURN cs.qualified_name AS qualified_name,
                       cs.kind AS kind,
                       cs.odoo_version AS odoo_version,
                       cs.signature AS signature,
                       cs.file_path AS file_path,
                       cs.line AS line,
                       cs.status AS status,
                       cs.replacement_qname AS replacement_qname,
                       cs.note AS note
            """, v=odoo_version).data()
        return [
            CoreSymbolInfo(
                qualified_name=r["qualified_name"],
                kind=r["kind"] or "function",
                odoo_version=r["odoo_version"],
                signature=r.get("signature"),
                file_path=r.get("file_path"),
                line=r.get("line"),
                status=r.get("status") or "stable",
                replacement_qname=r.get("replacement_qname"),
                note=r.get("note"),
            )
            for r in rows
        ]

    def write_lifecycle_properties(
        self,
        diff,  # DiffResult — import avoided at module level for circularity
        *,
        from_version: str,
        to_version: str,
    ) -> None:
        """Write added_in / removed_in / deprecated_in properties on CoreSymbol nodes.

        Per ADR-0002 §2 (revised): lifecycle expressed as properties on CoreSymbol
        for query simplicity. REPLACED_BY is the only true edge.

        - added (in to_version)   → cs.added_in = to_version  on the NEW node
        - removed (from from_version) → cs.removed_in = to_version  on the OLD node
        - deprecated (in to_version)  → cs.deprecated_in = to_version  on the NEW node
        """
        if not diff:
            return
        with self.driver.session() as session:
            for sym in diff.added:
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.added_in = $added_in
                """, qn=sym.qualified_name, v=sym.odoo_version, added_in=to_version)

            for sym in diff.removed:
                # sym.odoo_version is from_version (old list)
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.removed_in = $removed_in
                """, qn=sym.qualified_name, v=from_version, removed_in=to_version)

            deprecated = getattr(diff, "deprecated", [])
            for sym in deprecated:
                session.run("""
                    MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                    SET cs.deprecated_in = $deprecated_in
                """, qn=sym.qualified_name, v=sym.odoo_version, deprecated_in=to_version)

    def write_pattern_examples(
        self, patterns: list[PatternExample], *, prune: bool = False,
    ) -> int:
        """Persist PatternExample nodes (idempotent MERGE on `pattern_id`).

        USES_CORE_SYMBOL edges to CoreSymbol nodes are silently skipped when
        the target does not exist — M4.5 graceful skip per ADR-0003 §5.
        Batched at 200/transaction (smaller than CoreSymbol's 500 because
        each pattern can fan-out N edge MERGEs).

        *prune* (default False): after MERGE-ing the incoming catalogue, DETACH
        DELETE every PatternExample node whose ``pattern_id`` is NOT in
        *patterns*. This is the R1 orphan-on-rename fix (issue #362 follow-up):
        PatternExample is MERGE-keyed on ``pattern_id`` ALONE, so a renamed or
        removed id (e.g. the #364 ``owl2-component-v15`` ->
        ``owl1-component-v15`` rename) otherwise leaves the OLD node reachable
        forever via the ``odoo://{version}/pattern/{pattern_id}`` resource. The
        MERGE-only write never deletes; the pgvector side is already a clean
        DELETE-then-INSERT (``seed_patterns._write_pgvector_with_embedder``), so
        this closes the Neo4j-only gap and mirrors
        :meth:`prune_framework_test_helpers` for style, logging and idempotence.

        CONTRACT — ``prune=True`` is safe ONLY when *patterns* is the FULL
        catalogue. PatternExample has no version/profile/repo in its identity, so
        the prune is necessarily GLOBAL (delete-not-in-incoming across the whole
        label). A caller that writes a PARTIAL batch (e.g. a version-filtered
        ``seed-patterns --version 15.0`` run, which loads only patterns whose
        ``odoo_version_min`` matches) MUST pass ``prune=False`` — a global prune
        there would delete every live pattern of the OTHER versions. The two
        production callers gate on exactly this: ``seed_patterns.run()`` prunes
        only when ``odoo_version_min_filter is None`` and the ``seed-patterns``
        CLI (``seed_patterns._write_neo4j``) prunes only when ``--version`` is
        unset. Safety: an empty *patterns* list NEVER prunes (early return
        below) — a transient empty load can never wipe the catalogue.

        Concurrency (analogous to the two safety properties documented on
        :meth:`prune_framework_test_helpers`): PatternExample has no per-profile
        scoping, so ``--profile-workers N`` can run several full-catalogue
        reseeds concurrently — one per profile finishing its own
        ``index_profile()`` pass (each profile's auto-reseed always loads the
        FULL catalogue; see ``pipeline.py``'s call into ``seed_patterns.run()``).
        Because every concurrent reseed loads the SAME source-of-truth
        (``_load_patterns_source`` with no version filter), each computes an
        identical ``live_ids`` set, so the MERGE+prune is idempotent across
        them — the only cost is duplicate work, never data loss. The one
        narrow window this does NOT cover: an admin CRUD insert
        (``src/web_ui/routes/admin_patterns.py``) landing in Postgres between
        two concurrent reseeds' loads could have its brand-new row DETACH
        DELETEd by whichever reseed loaded before that insert committed — this
        self-heals on the very next content-changed reseed cycle (the sha256
        sentinel gate re-triggers a full write once the DB content differs from
        what's stored), so it can only ever delay a new pattern's appearance,
        never permanently lose it.

        Returns the number of stale PatternExample nodes pruned (0 when
        ``prune=False``, when the incoming list is empty, or when nothing was
        stale).
        """
        if not patterns:
            return 0
        with self.driver.session() as session:
            for batch in _chunked(patterns, 200):
                session.execute_write(_write_pattern_examples_batch, batch)
            if not prune:
                return 0
            live_ids = [p.pattern_id for p in patterns]
            row = session.run(
                """
                MATCH (pe:PatternExample)
                WHERE NOT pe.pattern_id IN $live_ids
                DETACH DELETE pe
                RETURN count(pe) AS deleted
                """,
                live_ids=live_ids,
            ).single()
            deleted = row["deleted"] if row is not None else 0
            if deleted > 0:
                _logger.info(
                    "write_pattern_examples: pruned %d stale PatternExample "
                    "node(s) not in the current %d-pattern catalogue",
                    deleted, len(live_ids),
                )
            return deleted

    def write_stylesheets(
        self,
        stylesheets: list[StylesheetInfo],
        profiles: list[str] | None = None,
        repo_root=None,
        repo_id=None,
    ) -> None:
        """Persist :Stylesheet nodes + :DEFINED_IN + :IMPORTS edges.

        Idempotent MERGE on composite key (file_path, module, odoo_version).
        *profiles* is the ancestor profile name array (per ADR-0016 Option Y).
        *repo_root* relativizes file_path + @import targets to repo-relative
        form (ADR-0037); all stylesheets in one run share one repo_root.
        *repo_id* scopes the :IMPORTS target MATCH so a relative path shared
        across repos at the same version cannot create a cross-repo edge
        (ADR-0037); all stylesheets in one run share one repo_id.
        Batched at NEO4J_WRITE_BATCH_SIZE per transaction.
        IMPORTS edge write silently skips when the target file_path is not indexed.
        """
        if not stylesheets:
            return
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for batch in _chunked(stylesheets, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(
                    _write_stylesheets_batch, batch, _profiles, repo_root, repo_id,
                )

    def write_lint_violations(
        self,
        violations: list[LintViolationInfo],
        profiles: list[str] | None = None,
        repo_root=None,
    ) -> None:
        """Persist :LintViolation nodes + :HAS_VIOLATION edges to :View (WI-E, M11).

        Idempotent MERGE on composite key (file_path, line, rule, odoo_version).
        *profiles* is the ancestor profile name array (per ADR-0016 Option Y).
        *repo_root* relativizes file_path (MERGE-key component) per ADR-0037 — all
        violations in one run share one repo_root.
        Batched at NEO4J_WRITE_BATCH_SIZE per transaction.
        The :HAS_VIOLATION edge is silently skipped when the target :View has
        not yet been written — the edge is written on the next incremental run.
        Should be called after write_view_results() so View nodes exist.
        """
        if not violations:
            return
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for batch in _chunked(violations, NEO4J_WRITE_BATCH_SIZE):
                session.execute_write(
                    _write_lint_violations_batch, batch, _profiles, repo_root,
                )

    # --- Module retirement cascade (ADR-0056 D9) -----------------------------

    def server_now(self) -> datetime:
        """Return the Neo4j server's current ``datetime()`` as an aware datetime.

        ``Module.last_seen_at`` is stamped with the server clock inside the
        Module MERGE, so a ``run_started_at`` taken from the indexer host's
        clock would be skewed against it on a split-tier deploy. Callers take
        the run start from here. It is the same ``datetime()`` (statement
        clock, millisecond resolution) every stamp uses, and every guard
        compares it with a stamp through :func:`_instant`, so a stamp written
        after this call is never ordered before it.
        """
        with self.driver.session() as session:
            row = session.run("RETURN datetime() AS now").single()
        return row["now"].to_native().astimezone(UTC)

    def retire_modules(
        self,
        odoo_version: str,
        names: Iterable[str],
        *,
        run_started_at,
    ) -> dict:
        """Delete retired modules and every node they own at *odoo_version*.

        The ONE module-deletion primitive (ADR-0056 D9). The caller has already
        decided, from the lifecycle ledger, that no repo still ships these
        names at this version; this method does not re-check ownership.

        Guarantees:

        * **Scope.** Only nodes at *odoo_version* whose owning module is in
          *names*. Other versions, other module names, framework test bases
          (``@framework``), ``__unresolved__`` placeholders, and labels outside
          :data:`MODULE_CHILD_LABELS` (CoreSymbol, LintRule, CLICommand,
          CLIFlag, SpecMetadata, PatternExample, AssetBundle) are never
          touched. DETACH DELETE removes the edges other modules hold to the
          deleted nodes (DEPENDS_ON, INHERITS, IMPORTS, INHERITS_TEST, ...).
        * **Concurrent re-MERGE guard (H2).** A name whose Module node has
          ``last_seen_at >= run_started_at`` was written by some run after the
          caller started; it is reported in ``skipped_recent`` and NEITHER the
          Module NOR any of its children are deleted. A Module without
          ``last_seen_at`` (written before ADR-0056) counts as stale. The same
          guard holds for every child during the cascade: a child whose
          ``written_at`` is at or after ``run_started_at`` (a concurrent run
          that does not hold ``retire:<v>`` re-MERGEd the module after the
          first check) is kept, and the Module node itself is deleted only if
          still stale at the final step; a name whose Module survived that
          step is reported in ``skipped_recent`` too (so the caller neither
          deletes its embeddings nor records it retired). Relationships go
          only with a deleted endpoint, so a kept child keeps its
          relationships.
        * **Children without a Module node** (debris from the old Module-only
          ``--gc``) are deleted for every name in *names* that is not
          ``skipped_recent`` - the same call cleans both.
        * **Order (M12):** LintViolation (by ``view_xmlid`` of the module's
          Views, by ``HAS_VIOLATION`` edge from them, or dangling with a
          ``<module>.`` xmlid prefix and no View) -> Method, Field, Model and
          the other module-property children -> TestMethod, TestClass (by
          ``module`` alone, every repo) -> non-framework TestHelper -> Module.
          Views are deleted after their violations, because a View is the
          only path to a LintViolation that has no ``module`` property.
        * **Idempotent and resumable.** Each step is an auto-commit
          ``CALL {} IN TRANSACTIONS OF NEO4J_DELETE_BATCH_ROWS ROWS`` retried
          on transient errors (deadlock, leader switch); a re-run after a
          failure deletes what is left and returns zero for what is gone.

        Args:
            odoo_version:   version label, e.g. ``'17.0'``.
            names:          module technical names to retire.
            run_started_at: timezone-aware start of the caller's run, from
                            :meth:`server_now` (ValueError when naive).

        Returns ``{"modules": int, "children": int, "by_label": {label: int},
        "retired": [names whose Module or children were considered],
        "skipped_recent": [names]}``; ``retired`` and ``skipped_recent`` are
        sorted.
        """
        run_at = _require_aware_datetime(run_started_at, "run_started_at")
        wanted = sorted({n for n in names if n and n not in NON_RETIRABLE_MODULE_NAMES})
        result: dict = {
            "modules": 0,
            "children": 0,
            "by_label": {label: 0 for label in MODULE_CHILD_LABELS},
            "retired": [],
            "skipped_recent": [],
        }
        if not wanted:
            return result

        with self.driver.session() as session:
            recent_row = _run_single_with_retry(
                session, "retire_modules[guard]",
                f"""
                UNWIND $names AS name
                MATCH (m:Module {{name: name, odoo_version: $v}})
                WHERE {_instant('m.last_seen_at')} >= {_instant('$run_at')}
                RETURN collect(DISTINCT name) AS recent
                """,
                names=wanted, v=odoo_version, run_at=run_at,
            )
            recent = sorted(recent_row["recent"]) if recent_row is not None else []
            if recent:
                _logger.warning(
                    "retire_modules: %d module(s) at version %s were re-written "
                    "after this run started - NOT deleted (concurrent owner): %s",
                    len(recent), odoo_version, ", ".join(recent),
                )
            targets = [n for n in wanted if n not in set(recent)]
            result["skipped_recent"] = recent
            result["retired"] = targets
            if not targets:
                return result

            by_label = result["by_label"]
            by_label["LintViolation"] = self._delete_module_lint_violations(
                session, odoo_version, targets, run_at=run_at,
            )
            for label in MODULE_CHILD_LABELS:
                if label == "LintViolation":
                    continue
                row = _run_single_with_retry(
                    session, f"retire_modules[{label}]",
                    f"""
                    MATCH (n:{label})
                    WHERE n.module IN $names AND n.odoo_version = $v
                      AND {_written_before_run('n')}
                    CALL (n) {{
                        DETACH DELETE n
                    }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                    RETURN count(n) AS deleted
                    """,
                    names=targets, v=odoo_version, run_at=run_at,
                )
                by_label[label] = row["deleted"] if row is not None else 0

            module_row = _run_single_with_retry(
                session, "retire_modules[Module]",
                f"""
                UNWIND $names AS name
                MATCH (m:Module {{name: name, odoo_version: $v}})
                WHERE m.last_seen_at IS NULL
                   OR {_instant('m.last_seen_at')} < {_instant('$run_at')}
                CALL (m) {{
                    DETACH DELETE m
                }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                RETURN count(m) AS deleted
                """,
                names=targets, v=odoo_version, run_at=run_at,
            )
            result["modules"] = module_row["deleted"] if module_row is not None else 0
            late_row = _run_single_with_retry(
                session, "retire_modules[late guard]",
                """
                UNWIND $names AS name
                MATCH (m:Module {name: name, odoo_version: $v})
                RETURN collect(DISTINCT name) AS late
                """,
                names=targets, v=odoo_version,
            )
            late = sorted(late_row["late"]) if late_row is not None else []
            if late:
                _logger.warning(
                    "retire_modules: %d module(s) at version %s were re-written "
                    "during the cascade - Module and fresh children kept "
                    "(concurrent owner): %s",
                    len(late), odoo_version, ", ".join(late),
                )
                result["skipped_recent"] = sorted(set(recent) | set(late))
                result["retired"] = [n for n in targets if n not in set(late)]

        result["children"] = sum(by_label.values())
        _logger.info(
            "retire_modules: version %s retired %d module(s) (%d Module node(s), "
            "%d child node(s)) %s",
            odoo_version, len(targets), result["modules"], result["children"],
            {k: v for k, v in by_label.items() if v},
        )
        return result

    @staticmethod
    def _module_view_xmlids(session, odoo_version: str, names: list[str]) -> list[str]:
        """xmlids of every View owned by *names* at *odoo_version*."""
        row = _run_single_with_retry(
            session, "module View xmlids",
            """
            MATCH (view:View)
            WHERE view.module IN $names AND view.odoo_version = $v
            RETURN collect(DISTINCT view.xmlid) AS xmlids
            """,
            names=names, v=odoo_version,
        )
        return list(row["xmlids"]) if row is not None else []

    @classmethod
    def _delete_module_lint_violations(
        cls, session, odoo_version: str, names: list[str], *, run_at,
    ) -> int:
        """Cascade step 1: LintViolation nodes owned by *names* (M12).

        Selection: :data:`_MODULE_LINT_VIOLATION_PREDICATE`, minus violations
        written at or after *run_at* (the cascade guard). Runs before the
        Views are deleted - a View is the only path to its violations.
        """
        xmlids = cls._module_view_xmlids(session, odoo_version, names)
        row = _run_single_with_retry(
            session, "retire_modules[LintViolation]",
            f"""
            MATCH (lv:LintViolation {{odoo_version: $v}})
            WHERE ({_MODULE_LINT_VIOLATION_PREDICATE})
              AND {_written_before_run('lv')}
            CALL (lv) {{
                DETACH DELETE lv
            }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
            RETURN count(lv) AS deleted
            """,
            names=names, xmlids=xmlids, v=odoo_version, run_at=run_at,
        )
        return row["deleted"] if row is not None else 0

    def drop_module_owner(
        self,
        odoo_version: str,
        name: str,
        owners: Iterable[ModuleOwner],
    ) -> dict:
        """Remove every non-surviving owner from module *name* and its subtree.

        Used when one repo stops shipping a module another repo still ships
        (cross-repo move, CE->EE, same-name fork). The node survives; its
        ownership becomes EXACTLY *owners* (ADR-0034 amendment: union on write,
        exact reset from the ledger on retire).

        Effects at *odoo_version*:

        * Module: ``profile`` = sorted distinct ``owners.profile_name``;
          ``repos`` = sorted distinct ``owners.repo_basename``; ``repo`` /
          ``path`` (and ``repo_id`` / ``repo_url`` when given) from the primary
          owner = first owner by ``(profile_name, repo_basename)``. Other
          identity properties (edition, license, summary, ...) stay until the
          survivor rewrites the module (the caller marks ``needs_rewrite``).
        * Every :data:`MODULE_CHILD_LABELS` node of the module - including
          LintViolations of its Views and addon TestHelpers - gets the same
          exact ``profile`` array (M4), so a tenant that only has a surviving
          owner's profile sees the module's models, fields, views, tests.
        * TestClass / TestMethod carry the shipping repo in their key: those
          whose ``repo`` is not a surviving owner's basename are DELETED (the
          retiring repo's copy); a surviving repo with the same basename keeps
          its nodes (G7 same-slug).

        Raises ValueError when *owners* is empty - with no survivor the module
        must be retired with :meth:`retire_modules` instead.

        Returns ``{"module": 0|1 matched, "children": nodes reset,
        "tests_deleted": TestClass+TestMethod deleted}``.
        """
        owner_list = sorted(
            {
                (o.profile_name, o.repo_basename): o
                for o in owners
            }.values(),
            key=lambda o: (o.profile_name, o.repo_basename),
        )
        if not owner_list:
            raise ValueError(
                f"drop_module_owner({odoo_version!r}, {name!r}): no surviving "
                "owner - use retire_modules"
            )
        if name in NON_RETIRABLE_MODULE_NAMES:
            return {"module": 0, "children": 0, "tests_deleted": 0}
        profiles = sorted({o.profile_name for o in owner_list})
        repos = sorted({o.repo_basename for o in owner_list})
        primary = owner_list[0]
        params = {
            "name": name, "v": odoo_version, "profiles": profiles, "repos": repos,
        }

        with self.driver.session() as session:
            module_row = _run_single_with_retry(
                session, "drop_module_owner[Module]",
                """
                MATCH (m:Module {name: $name, odoo_version: $v})
                SET m.profile = $profiles,
                    m.repos = $repos,
                    m.repo = $repo,
                    m.path = $path,
                    m.repo_id = coalesce($repo_id, m.repo_id),
                    m.repo_url = coalesce($repo_url, m.repo_url)
                RETURN count(m) AS matched
                """,
                repo=primary.repo_basename, path=primary.path,
                repo_id=primary.repo_id, repo_url=primary.repo_url,
                **params,
            )
            tests_deleted = 0
            for label in ("TestMethod", "TestClass"):
                row = _run_single_with_retry(
                    session, f"drop_module_owner[{label} delete]",
                    f"""
                    MATCH (n:{label})
                    WHERE n.module = $name AND n.odoo_version = $v
                      AND NOT coalesce(n.repo, '') IN $repos
                    CALL (n) {{
                        DETACH DELETE n
                    }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                    RETURN count(n) AS deleted
                    """,
                    **params,
                )
                tests_deleted += row["deleted"] if row is not None else 0

            reset = 0
            for label in MODULE_CHILD_LABELS:
                if label == "LintViolation":
                    row = _run_single_with_retry(
                        session, "drop_module_owner[LintViolation]",
                        f"""
                        MATCH (lv:LintViolation {{odoo_version: $v}})
                        WHERE {_MODULE_LINT_VIOLATION_PREDICATE}
                        CALL (lv) {{
                            SET lv.profile = $profiles
                        }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                        RETURN count(lv) AS reset
                        """,
                        names=[name],
                        xmlids=self._module_view_xmlids(session, odoo_version, [name]),
                        **params,
                    )
                else:
                    row = _run_single_with_retry(
                        session, f"drop_module_owner[{label}]",
                        f"""
                        MATCH (n:{label})
                        WHERE n.module = $name AND n.odoo_version = $v
                        CALL (n) {{
                            SET n.profile = $profiles
                        }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                        RETURN count(n) AS reset
                        """,
                        **params,
                    )
                reset += row["reset"] if row is not None else 0

        matched = module_row["matched"] if module_row is not None else 0
        _logger.info(
            "drop_module_owner: %s@%s now owned by %s (%d child node(s) reset, "
            "%d test node(s) of departed repos deleted)",
            name, odoo_version, repos, reset, tests_deleted,
        )
        return {"module": matched, "children": reset, "tests_deleted": tests_deleted}

    def stamp_module_presence(
        self,
        odoo_version: str,
        rows: Iterable[Mapping],
        head: str | None,
        now=None,
    ) -> int:
        """Stamp the ledger's view of presence onto existing Module nodes.

        *rows*: one mapping per module name the stamping repo observed present
        at its HEAD, with key ``name`` and optional ``repos`` (the ledger's
        present owners for that name, all repos - L6), ``version_mismatch``
        and ``version_raw`` (the version rule's verdict for the manifest).
        Per matched Module: ``last_seen_sha = head``, ``last_seen_at = now``
        (server ``datetime()`` when *now* is None), and ``repos`` /
        ``version_mismatch`` / ``version_raw`` when given (left unchanged
        otherwise). A mismatch that disappeared is stamped False.

        MATCH only - never creates a Module node. The return value is the
        number of Module nodes matched; a count below the number of rows means
        some present names have no node (lost to a concurrent retire or a
        failed write) and must be re-written (H2 self-heal).
        """
        now_value = None if now is None else _require_aware_datetime(now, "now")
        payload = []
        for r in rows:
            name = r["name"]
            repos = r.get("repos")
            mismatch = r.get("version_mismatch")
            payload.append({
                "name": name,
                "repos": sorted(set(repos)) if repos is not None else None,
                "version_mismatch": bool(mismatch) if mismatch is not None else None,
                "version_raw": r.get("version_raw"),
            })
        if not payload:
            return 0
        matched = 0
        with self.driver.session() as session:
            for batch in _chunked(payload, NEO4J_WRITE_BATCH_SIZE):
                row = _run_single_with_retry(
                    session, "stamp_module_presence",
                    """
                    UNWIND $rows AS r
                    MATCH (m:Module {name: r.name, odoo_version: $v})
                    SET m.last_seen_sha = $head,
                        m.last_seen_at = coalesce($now, datetime()),
                        m.repos = coalesce(r.repos, m.repos),
                        m.version_mismatch = coalesce(r.version_mismatch, m.version_mismatch),
                        m.version_raw = coalesce(r.version_raw, m.version_raw)
                    RETURN count(m) AS matched
                    """,
                    rows=batch, v=odoo_version, head=head, now=now_value,
                )
                matched += row["matched"] if row is not None else 0
        return matched

    def orphan_module_names(
        self,
        odoo_version: str,
        present_names: Iterable[str],
        *,
        repo: str | None = None,
    ) -> list[str]:
        """Module names at *odoo_version* that are indexed but not present.

        A Module counts as indexed when it has an owner (non-empty ``profile``)
        or a ``repo_id``; profile-less dependency stubs are excluded (they are
        reclaimed by :meth:`gc_null_repo_dep_stubs`). *repo* narrows the scan
        to ``Module.repo = repo``. Read-only; sorted by name.
        """
        repo_clause = "AND m.repo = $repo" if repo is not None else ""
        with self.driver.session() as session:
            row = _run_single_with_retry(
                session, "orphan_module_names",
                self._read_query(f"""
                MATCH (m:Module {{odoo_version: $v}})
                WHERE NOT m.name IN $present
                  AND NOT m.name IN $sentinels
                  AND (size(coalesce(m.profile, [])) > 0 OR m.repo_id IS NOT NULL)
                  {repo_clause}
                WITH m.name AS name ORDER BY name ASC
                RETURN collect(DISTINCT name) AS names
                """),
                v=odoo_version, present=sorted(set(present_names)),
                sentinels=sorted(NON_RETIRABLE_MODULE_NAMES), repo=repo,
            )
        return sorted(row["names"]) if row is not None else []

    def module_profiles(
        self, odoo_version: str, names: Iterable[str] | None = None,
    ) -> dict[str, list[str]]:
        """``{module name: sorted Module.profile}`` at *odoo_version*.

        *names* None returns every Module with a non-empty ``profile`` (the
        live (module, profile) pairs an embeddings-orphan sweep compares
        against); otherwise only the given names that have a Module node
        (possibly with an empty list). Read-only.
        """
        with self.driver.session() as session:
            if names is None:
                result = session.run(
                    self._read_query("""
                    MATCH (m:Module {odoo_version: $v})
                    WHERE size(coalesce(m.profile, [])) > 0
                    RETURN m.name AS name, coalesce(m.profile, []) AS profile
                    ORDER BY name ASC
                    """),
                    v=odoo_version,
                ).data()
            else:
                result = session.run(
                    self._read_query("""
                    UNWIND $names AS name
                    MATCH (m:Module {name: name, odoo_version: $v})
                    RETURN m.name AS name, coalesce(m.profile, []) AS profile
                    ORDER BY name ASC
                    """),
                    names=sorted(set(names)), v=odoo_version,
                ).data()
        return {r["name"]: sorted(set(r["profile"])) for r in result}

    def module_identity(
        self, odoo_version: str, names: Iterable[str],
    ) -> dict[str, dict]:
        """``{name: {repo, repo_id, path, profile}}`` of existing Module nodes.

        Read before a retire so the caller can attribute a deleted node to the
        repo that last wrote it (ledger evidence for the orphan sweep).
        Names without a Module node are absent. Read-only.
        """
        wanted = sorted(set(names))
        if not wanted:
            return {}
        with self.driver.session() as session:
            result = session.run(
                self._read_query("""
                UNWIND $names AS name
                MATCH (m:Module {name: name, odoo_version: $v})
                RETURN m.name AS name, m.repo AS repo, m.repo_id AS repo_id,
                       m.path AS path, coalesce(m.profile, []) AS profile
                ORDER BY name ASC
                """),
                names=wanted, v=odoo_version,
            ).data()
        return {
            r["name"]: {
                "repo": r["repo"], "repo_id": r["repo_id"], "path": r["path"],
                "profile": sorted(set(r["profile"])),
            }
            for r in result
        }

    def repo_module_baseline(
        self, repo_id: int | None, repo_basename: str, profile_name: str,
    ) -> list[tuple[str, str]]:
        """``(odoo_version, name)`` of every owned Module node the graph
        attributes to one repo, sorted - the G-B baseline of a repo the ledger
        never reflected (``lifecycle.apply_gates``).

        A node belongs to the repo when its ``repo_id`` is the repo's, or when
        it is owned by *profile_name* and names *repo_basename* in ``repo`` or
        ``repos`` (nodes written before ``repo_id`` / ``repos`` existed).
        Dependency stubs (no profile) and the ``@framework`` /
        ``__unresolved__`` sentinels are left out. Read-only.
        """
        with self.driver.session() as session:
            result = session.run(
                self._read_query("""
                MATCH (m:Module)
                WITH m, properties(m) AS p
                WHERE size(coalesce(p.profile, [])) > 0
                  AND NOT m.name IN $sentinels
                  AND (
                    ($repo_id IS NOT NULL AND p.repo_id = $repo_id)
                    OR ($profile IN p.profile
                        AND (p.repo = $basename OR $basename IN coalesce(p.repos, [])))
                  )
                RETURN DISTINCT m.odoo_version AS v, m.name AS name
                ORDER BY v ASC, name ASC
                """),
                repo_id=repo_id, basename=repo_basename, profile=profile_name,
                sentinels=sorted(NON_RETIRABLE_MODULE_NAMES),
            ).data()
        return [(r["v"], r["name"]) for r in result]

    def modules_by_old_technical_name(
        self, odoo_version: str, old_names: Iterable[str],
        profiles: Iterable[str] | None = None,
    ) -> dict[str, list[str]]:
        """``{old name: sorted Module names}`` of indexed Modules at the version
        whose manifest ``old_technical_name`` is one of *old_names* (successor
        evidence declared by the survivor). Read-only."""
        wanted = sorted(set(old_names))
        if not wanted:
            return {}
        with self.driver.session() as session:
            result = session.run(
                self._read_query("""
                MATCH (m:Module {odoo_version: $v})
                WHERE m.old_technical_name IN $old AND m.old_technical_name <> m.name
                  AND size(coalesce(m.profile, [])) > 0
                RETURN m.old_technical_name AS old, m.name AS name
                ORDER BY old ASC, name ASC
                """),
                old=wanted, v=odoo_version,
            ).data()
        out: dict[str, list[str]] = {}
        for r in result:
            out.setdefault(r["old"], []).append(r["name"])
        return out

    def orphan_child_keys(self, odoo_version: str) -> dict[str, dict[str, int]]:
        """Module-owned nodes whose Module node no longer exists (M6).

        Returns ``{module name: {label: count}}`` (sorted by module name) for
        every :data:`MODULE_CHILD_LABELS` node at *odoo_version* whose owning
        module has no ``:Module {name, odoo_version}`` node. LintViolation is
        attributed through its View, or - when that View is gone too - through
        the ``<module>.`` prefix of ``view_xmlid``. ``@framework`` and
        ``__unresolved__`` are never reported; labels outside
        MODULE_CHILD_LABELS (CoreSymbol, LintRule, CLI*) are never scanned.
        Read-only: pass the keys to :meth:`retire_modules` to delete them.
        """
        found: dict[str, dict[str, int]] = {}
        sentinels = sorted(NON_RETIRABLE_MODULE_NAMES)
        with self.driver.session() as session:
            for label in MODULE_CHILD_LABELS:
                if label == "LintViolation":
                    query = """
                        MATCH (lv:LintViolation {odoo_version: $v})
                        OPTIONAL MATCH (view:View {xmlid: lv.view_xmlid, odoo_version: $v})
                        WITH coalesce(view.module,
                                      split(coalesce(lv.view_xmlid, ''), '.')[0]) AS module
                        WHERE module <> '' AND NOT module IN $sentinels
                        WITH module, count(*) AS n
                        WHERE NOT EXISTS {
                            MATCH (:Module {name: module, odoo_version: $v})
                        }
                        RETURN module, n ORDER BY module ASC
                    """
                else:
                    query = f"""
                        MATCH (c:{label} {{odoo_version: $v}})
                        WHERE c.module IS NOT NULL AND NOT c.module IN $sentinels
                        WITH c.module AS module, count(*) AS n
                        WHERE NOT EXISTS {{
                            MATCH (:Module {{name: module, odoo_version: $v}})
                        }}
                        RETURN module, n ORDER BY module ASC
                    """
                for rec in session.run(
                    self._read_query(query), v=odoo_version, sentinels=sentinels,
                ).data():
                    found.setdefault(rec["module"], {})[label] = rec["n"]
        return {m: found[m] for m in sorted(found)}

    def modules_without_profile(self, odoo_version: str) -> list[dict]:
        """Module nodes with a real ``path`` but an empty ``profile`` list.

        The read side's existence rule is ``size(profile) > 0`` (a dependency
        stub has no path and no profile), so such a node - a real module whose
        owner list was lost - answers "Indexed: No" although it was indexed
        (rollout check F24). ``[{name, path, repo, repo_id, children}]`` sorted
        by name, ``children`` = its ``DEFINED_IN`` child count. Read-only.
        """
        with self.driver.session() as session:
            result = session.run(
                self._read_query("""
                MATCH (m:Module {odoo_version: $v})
                WHERE size(coalesce(m.profile, [])) = 0
                  AND m.path IS NOT NULL AND trim(toString(m.path)) <> ''
                  AND NOT m.name IN $sentinels
                RETURN m.name AS name, m.path AS path, m.repo AS repo,
                       m.repo_id AS repo_id,
                       COUNT { (m)<-[:DEFINED_IN]-() } AS children
                ORDER BY name ASC, path ASC
                """),
                v=odoo_version, sentinels=sorted(NON_RETIRABLE_MODULE_NAMES),
            ).data()
        return [dict(r) for r in result]

    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict:
        """Retire every module whose ``Module.repo`` is *repo_basename* at *odoo_version*.

        Web UI repo/profile delete path, on the :meth:`retire_modules` cascade
        (full MODULE_CHILD_LABELS subtree, same guards). The module names are
        taken from ``Module.repo``, i.e. the repo that last wrote the node; a
        name another repo still ships must be reconciled through the ledger
        (drop_module_owner) by the caller before this runs.

        Returns ``{"modules": N, "children": M}``.
        """
        with self.driver.session() as session:
            names_row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                RETURN collect(DISTINCT m.name) AS names
                """,
                repo=repo_basename,
                version=odoo_version,
            ).single()
        names = list(names_row["names"]) if names_row is not None else []
        if not names:
            return {"modules": 0, "children": 0}
        counts = self.retire_modules(
            odoo_version, names, run_started_at=self.server_now(),
        )
        return {"modules": counts["modules"], "children": counts["children"]}

    def gc_unresolved_placeholders(self, odoo_version: str) -> dict[str, int]:
        """DETACH DELETE inert '__unresolved__' placeholder nodes for odoo_version.

        Placeholder nodes are created when the writer encounters a reference to
        a Model / View / QWebTmpl / OWLComp that has not been indexed yet (parent
        not found at write time).  All queries in server.py already filter these
        out at read time (``module <> '__unresolved__'`` / ``coalesce(unresolved,
        false) = false``), so they are invisible to users.  Over time they
        accumulate (2,068 on prod as of 2026-05-26) and produce "shadow" View
        pairs when the real View is later indexed against the old 3-key MERGE.

        This method deletes ALL placeholder nodes that carry ``unresolved=true``
        AND ``module='__unresolved__'``, scoped strictly to ``odoo_version``.
        DETACH DELETE removes incident edges (the ``{unresolved:true}`` relation
        edges) along with the node — no orphan edges remain.

        After deleting placeholders this method also calls
        :meth:`heal_resolved_unresolved_flags` as a defense-in-depth step
        (ADR-0007 §D5 extension).  That sibling clears ``unresolved=true`` flags
        that survived on already-resolved nodes/edges (stale artefacts from the
        old placeholder path before PR #194) — making 153 prod nodes and 326
        prod edges visible to MCP clients again.

        Safety argument:
        - server.py filters every placeholder at read time → deleting them
          changes nothing visible to MCP clients or the Web UI.
        - Scoped by ``odoo_version`` so cross-version/tenant data is never touched.
        - Idempotent: a second run returns zeros.
        - This is the companion cleanup for the writer fix (ADR-0007 §D5 extension)
          that closes the shadow-View producer going forward; this gc removes
          existing stale placeholders on the current graph.

        Returns a dict with per-label deleted counts, e.g.::

            {"Model": 260, "View": 629, "QWebTmpl": 373, "OWLComp": 806}
        """
        counts: dict[str, int] = {}
        labels = ["Model", "View", "QWebTmpl", "OWLComp"]
        with self.driver.session() as session:
            for label in labels:
                row = session.run(
                    f"""
                    MATCH (n:{label})
                    WHERE n.odoo_version = $version
                      AND n.module = '__unresolved__'
                      AND coalesce(n.unresolved, false) = true
                    DETACH DELETE n
                    RETURN count(n) AS deleted
                    """,
                    version=odoo_version,
                ).single()
                counts[label] = row["deleted"] if row is not None else 0
                if counts[label] > 0:
                    _logger.info(
                        "Placeholder GC: deleted %d __unresolved__ %s nodes for version %s",
                        counts[label], label, odoo_version,
                    )
        total = sum(counts.values())
        _logger.info(
            "Placeholder GC complete for version %s: %d total nodes deleted %s",
            odoo_version, total, counts,
        )
        # Defense-in-depth: heal any stale unresolved=true flags on already-resolved
        # nodes/edges that survived from the pre-PR-#194 placeholder path.
        self.heal_resolved_unresolved_flags(odoo_version)
        return counts

    def gc_orphan_asset_bundles(self, odoo_version: str) -> int:
        """DETACH DELETE orphaned :AssetBundle nodes for *odoo_version*.

        graph MED-1 / integration LOW: AssetBundle is version-global (shared
        across modules, :data:`MODULE_SHARED_LABELS`), so it is deliberately NOT
        in the per-module ``retire_modules`` cascade - deleting it per-module
        would orphan other live modules' CONTRIBUTES_TO edges. Instead, any
        AssetBundle with NO inbound CONTRIBUTES_TO and that participates in NO
        INCLUDES_BUNDLE / EXTENDS_ASSET_BUNDLE edge is genuinely unreferenced - a
        leftover from a bundle whose sole contributor module was retired - and
        can be reclaimed.

        Safety:
        - Scoped strictly by ``odoo_version`` (cross-version data untouched).
        - Only deletes nodes with zero inbound CONTRIBUTES_TO AND no
          INCLUDES_BUNDLE (either direction) AND no inbound EXTENDS_ASSET_BUNDLE,
          so a forward-referenced or still-extended bundle is preserved.
        - Idempotent: a second run returns 0.
        - Safe on incremental runs: CONTRIBUTES_TO edges of unchanged modules
          persist between runs, so a live bundle always keeps an inbound edge;
          only a retired module's DETACH DELETE removes one.
        """
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (b:AssetBundle {odoo_version: $version})
                WHERE NOT (:Module)-[:CONTRIBUTES_TO]->(b)
                  AND NOT (b)-[:INCLUDES_BUNDLE]-()
                  AND NOT (b)<-[:EXTENDS_ASSET_BUNDLE]-()
                DETACH DELETE b
                RETURN count(b) AS deleted
                """,
                version=odoo_version,
            ).single()
        deleted = row["deleted"] if row is not None else 0
        if deleted > 0:
            _logger.info(
                "AssetBundle orphan GC: deleted %d unreferenced bundles for "
                "version %s", deleted, odoo_version,
            )
        return deleted

    def reconcile_same_name_inherits(self, odoo_version: str) -> int:
        """MERGE any missing extender-to-definition INHERITS edges for odoo_version.

        Background — topology change (#273, ADR new):
        The writer (W1) now emits K×D edges: each extender Model node (same name,
        is_definition=false) gets one INHERITS edge per definition node (is_definition=true)
        of the same name+version.  Before this fix it emitted K² mesh edges.

        Cross-repo write-order gap:
        When an extender repo is indexed BEFORE the definition repo (no topo order between
        repos, only within a single repo's module dependency tree), the definition node does
        not exist at write time → the writer MATCH tip returns 0 rows → 0 edges created.
        A subsequent index_repo run for the definition repo writes the definition node but
        does NOT retroactively connect the extenders in other repos that were written earlier.

        This post-pass reconciliation fills those gaps after all repos for the version have
        been written.  It is designed to be:
        - **Idempotent** (MERGE — safe to run twice, only creates missing edges).
        - **Version-scoped** (odoo_version parameter — only touches the current run's version).
          Physical plan note: the driving ``MATCH (ext:Model) WHERE ext.odoo_version = $version``
          carries no ``name`` predicate, so it is ONE :Model label scan filtered by version
          (the Model(name, odoo_version) index needs a name anchor and cannot serve a
          version-only filter). It does not scan other versions' rows beyond the label-scan
          membership test, but on a large graph this is a per-run linear cost that grows with
          the total :Model count. Acceptable under the 600s db.transaction.timeout for current
          graph sizes; if it becomes a hotspot, scope by the run's module-name set or add a
          Model(odoo_version) index (deferred — no new index added in this wave).
        - **Safe in both incremental and full-reindex runs** (runs at the end of
          _index_repo, after gc_unresolved_placeholders, for every version indexed in the run).

        Selection criterion for "extender" (who gets a reconciled edge):
        A Model node M is treated as an extender requiring reconciliation when ALL of:
          1. M.odoo_version == odoo_version (version-scoped).
          2. coalesce(M.is_definition, false) = false  (M is not the definition).
          3. M.module <> '__unresolved__'  (skip placeholder nodes — gc handles them).
          4. There exists at least one definition node D with the same name+version:
             D.name = M.name, D.odoo_version = M.odoo_version,
             coalesce(D.is_definition, false) = true, D.module <> M.module.
          5. M does NOT already have an INHERITS edge to D (the gap to fill).

        The Cypher MATCH for "extender has same-name out-edges" is NOT used as the criterion
        (that would conflate cross-name parent edges with same-name self-extend edges).
        Instead, the criterion is purely structural: name-match to a definition node and
        missing edge.  This is correct because:
        - If M is a pure cross-name extender (only has `_inherit['other.model']`), it will
          have a different name from any definition, so condition 4 never matches → no
          spurious edges created.
        - If M is a same-name extender that was written before its definition existed, it may
          have 0 same-name INHERITS out-edges currently → this pass creates the missing one.
        - If M already has the correct extender→definition edge, condition 5 excludes it
          from the MERGE → idempotent.

        `r.order` on the new edge:
        Prefer the minimum `r.order` from any existing same-name INHERITS out-edge on M
        (preserves the MRO position recorded at write time if at least one edge exists).
        Falls back to 0 when M has no same-name out-edges yet (cross-repo gap: no edge was
        created at write time, so we use the "lowest priority" sentinel — consistent with
        the writer's ON CREATE default for a model that only has `_inherit = ['own.name']`).

        Failure policy:
        Logs a WARNING and returns 0 on any Neo4j error — does NOT raise.  This mirrors
        the auto-reseed pattern (ADR-0007): a post-pass failure should never abort the
        indexer run; the graph is still correct up to this point, and the next run will
        retry the reconciliation.

        Concurrency (--profile-workers):
        Two profiles indexing the SAME version in parallel can run this MERGE pass
        concurrently and hit a Neo4j MERGE deadlock on the shared definition node. The
        failure policy above absorbs the deadlock (WARNING + return 0) rather than
        aborting, but the loser then leaves its gap unfilled for that run — re-run the
        profile, or accept the miss (the next full reindex fills it). Idempotent MERGE
        makes a re-run safe. An advisory lock per-version for the reconcile pass would
        serialize same-version parallel runs and prevent the deadlock entirely; this is
        tracked as future work (issue #279).

        Returns the number of INHERITS edges created (0 if already complete or on error).
        """
        try:
            with self.driver.session() as session:
                row = session.run(
                    f"""
                    // For each extender Model that lacks an edge to its definition node,
                    // determine the order to stamp on the new edge.
                    MATCH (ext:Model)
                    WHERE ext.odoo_version = $version
                      AND NOT coalesce(ext.is_definition, false)
                      AND ext.module <> '__unresolved__'
                    // Collect the minimum order from any existing same-name out-edge
                    // (there may be none if this is a pure cross-repo gap).
                    OPTIONAL MATCH (ext)-[existing_r:{REL_INHERITS}]->(same_name:Model)
                    WHERE same_name.name = ext.name
                      AND same_name.odoo_version = ext.odoo_version
                    WITH ext, min(existing_r.order) AS edge_order
                    // Find the definition node(s) for this extender's model name.
                    MATCH (def:Model)
                    WHERE def.name = ext.name
                      AND def.odoo_version = ext.odoo_version
                      AND coalesce(def.is_definition, false) = true
                      AND def.module <> ext.module
                    WITH ext, def, edge_order
                    // Only process pairs that are missing the edge (idempotency guard).
                    WHERE NOT (ext)-[:{REL_INHERITS}]->(def)
                    // MERGE creates the edge only when it does not already exist.
                    MERGE (ext)-[r:{REL_INHERITS}]->(def)
                    ON CREATE SET r.order = coalesce(edge_order, 0)
                    RETURN count(r) AS created
                    """,
                    version=odoo_version,
                ).single()
                created = row["created"] if row is not None else 0
                if created > 0:
                    _logger.info(
                        "Same-name INHERITS reconciliation: created %d edge(s) "
                        "for version %s (cross-repo write-order gap fill)",
                        created, odoo_version,
                    )
                else:
                    _logger.debug(
                        "Same-name INHERITS reconciliation: no gaps found for version %s",
                        odoo_version,
                    )
                return created
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Same-name INHERITS reconciliation failed for version %s: %s — "
                "indexer run continues; next run will retry",
                odoo_version, exc,
            )
            return 0

    def heal_resolved_unresolved_flags(self, odoo_version: str) -> dict[str, int]:
        """Clear stale ``unresolved=true`` flags on already-resolved View/QWebTmpl nodes
        and their incident edges, scoped to ``odoo_version``.

        **Why these flags are stale.**  Before PR #194, View/QWebTmpl placeholder MERGE
        keys used three properties (``{xmlid, module:'__unresolved__', odoo_version}``).
        When the real node was later indexed the old real-write SET block updated
        ``module=<real>`` but never cleared ``unresolved=true``.  The subsequent
        ``ops/cleanup_unresolved_placeholders.cypher`` deleted nodes where
        ``module='__unresolved__'``, but these nodes already had their module rewritten
        to the real value — so they survived with ``module=<real>`` AND
        ``unresolved=true``.  Their incident edges kept ``unresolved=true`` too.

        **Correctness argument.**  A node with ``module <> '__unresolved__'`` was
        written by a real indexer pass.  Its ``unresolved=true`` is an artefact of
        the old placeholder path; clearing it restores the correct visible state.
        An edge whose target has ``module <> '__unresolved__'`` is a resolved
        relationship; its ``unresolved=true`` is likewise stale.

        **Scope.**  Only ``View`` and ``QWebTmpl`` nodes are affected — their real
        MERGE key is ``{xmlid, odoo_version}`` (no module), so a real write can
        converge onto a former placeholder.  ``Model`` / ``OWLComp`` include
        ``module`` in their MERGE key, so real and placeholder are always distinct
        nodes and this gap never applies to them.

        **Safety.**  This method only SETs flag properties; it does NOT delete any
        nodes or edges.  Scoped by ``odoo_version`` so cross-version/tenant data is
        never touched.  Idempotent: a second run returns zeros.

        This is called automatically by ``gc_unresolved_placeholders`` as a
        defense-in-depth step (ADR-0007 §D5 extension).  A one-time ops script
        (``ops/cleanup_resolved_unresolved_flags.cypher``) clears the existing prod
        backlog independently of a GC run.

        Returns a dict with heal counts, e.g.::

            {"nodes": 153, "edges": 326}
        """
        with self.driver.session() as session:
            node_row = session.run(
                """
                MATCH (n)
                WHERE (n:View OR n:QWebTmpl)
                  AND n.odoo_version = $version
                  AND coalesce(n.unresolved, false) = true
                  AND coalesce(n.module, '') <> '__unresolved__'
                SET n.unresolved = false
                RETURN count(n) AS healed
                """,
                version=odoo_version,
            ).single()
            nodes_healed = node_row["healed"] if node_row is not None else 0

            edge_row = session.run(
                """
                MATCH ()-[r]->(t)
                WHERE r.unresolved = true
                  AND t.odoo_version = $version
                  AND coalesce(t.module, '') <> '__unresolved__'
                SET r.unresolved = false
                RETURN count(r) AS healed
                """,
                version=odoo_version,
            ).single()
            edges_healed = edge_row["healed"] if edge_row is not None else 0

        if nodes_healed > 0 or edges_healed > 0:
            _logger.info(
                "Heal resolved flags: cleared %d stale-unresolved nodes, "
                "%d stale-unresolved edges for version %s",
                nodes_healed, edges_healed, odoo_version,
            )
        else:
            _logger.debug(
                "Heal resolved flags: no stale flags found for version %s",
                odoo_version,
            )
        return {"nodes": nodes_healed, "edges": edges_healed}

    def gc_null_repo_dep_stubs(self, odoo_version: str) -> int:
        """DETACH DELETE childless dep-stub Module nodes for odoo_version.

        These are :Module nodes created by the dep-target MERGE
        (write_parse_result) for ``module.depends`` entries that were never
        indexed under their own profile.  Their MERGE key is
        ``{name, odoo_version}`` only — no ``repo``, no ``repo_id``, no
        ``DEFINED_IN`` children.  They are not owned by any repo, so the
        lifecycle ledger never retires them (``orphan_module_names`` skips
        profile-less stubs too); this sweep is their only reclaim path.

        Safety:
        - Only deletes where ``m.repo_id IS NULL`` (absent) AND no
          ``DEFINED_IN`` child exists.
        - A node with ``repo_id`` set was written by a real indexer run —
          never deleted here.
        - DETACH DELETE removes incident DEPENDS_ON edges along with the node.
        - The dep-MERGE re-creates the stub + edge on the very next indexer
          run for any dep still declared, so deletion is safe — the stub
          resurrects automatically.
        - Scoped by ``odoo_version`` so cross-version data is never touched.
        - Idempotent: a second run returns 0.

        Must run AFTER all profiles for ``odoo_version`` have completed
        indexing in this pass (so a stub promoted to a real module in a
        later-running profile is not deleted before that profile runs).
        Place in ``index_all()`` AFTER the parallel/sequential profile loop,
        NOT per-profile and NOT per-repo.

        Returns count of deleted nodes.
        """
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (m:Module {odoo_version: $version})
                WHERE m.repo_id IS NULL
                  AND NOT EXISTS { (m)<-[:DEFINED_IN]-() }
                DETACH DELETE m
                RETURN count(m) AS deleted
                """,
                version=odoo_version,
            ).single()
        deleted = row["deleted"] if row is not None else 0
        if deleted > 0:
            _logger.info(
                "Dep-stub GC: deleted %d childless repo_id=NULL Module nodes "
                "for version %s",
                deleted, odoo_version,
            )
        else:
            _logger.debug(
                "Dep-stub GC: no childless NULL-repo Module stubs found "
                "for version %s",
                odoo_version,
            )
        return deleted

    # --- WI-1: test surface index layer ----------------------------------------

    def write_test_results(
        self,
        results: list[TestParseResult],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist TestClass + TestMethod nodes from one or more TestParseResult objects.

        Also writes TestHelper nodes for framework bases (module='@framework')
        that appear in result.test_helpers. Framework helpers do NOT get a
        DEFINED_IN edge (MED-3).

        profiles: the owning profile name array (ADR-0034 union-only). Empty list
        used when caller doesn't supply (backward-compat for unit tests).
        """
        _profiles = profiles if profiles is not None else []
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_test_classes_batch, result, _profiles)
                if result.test_helpers:
                    session.execute_write(_write_test_helpers_batch, result.test_helpers, _profiles)

    def write_js_test_results(
        self,
        suites: list,
        profiles: list[str] | None = None,
    ) -> None:
        """Persist JsTestSuite nodes from a list of JsTestSuiteInfo objects (WI-3).

        Each suite produces one JsTestSuite node (file-grained, §4.4).
        NO COVERS_MODEL edge is emitted (MED-1 contract: mock_models are test-doubles).

        profiles: the owning profile name array (ADR-0034 union-only).
        """
        _profiles = profiles if profiles is not None else []
        if not suites:
            return
        with self.driver.session() as session:
            session.execute_write(_write_js_test_batch, suites, _profiles)

    def write_framework_test_helpers(
        self,
        helpers: list[TestHelperInfo],
        profiles: list[str] | None = None,
    ) -> None:
        """Persist TestHelper nodes for framework bases (TransactionCase, HttpCase, etc.).

        Called from the core indexing path (parser_odoo_core seeding, §4.5).
        Framework helpers use module='@framework' (MED-3) and get NO DEFINED_IN edge.
        """
        _profiles = profiles if profiles is not None else []
        if not helpers:
            return
        with self.driver.session() as session:
            session.execute_write(_write_test_helpers_batch, helpers, _profiles)

    def prune_framework_test_helpers(
        self, odoo_version: str, live_names: Iterable[str],
    ) -> int:
        """DETACH DELETE stale '@framework' TestHelper nodes (issue #362 WI-4).

        Without this method, no code path anywhere ever deletes a TestHelper:
        ``write_framework_test_helpers`` (``_write_test_helpers_batch``) is
        MERGE+SET only, ``gc_stale_test_nodes`` DETACH DELETEs TestMethod/TestClass
        only, and the per-module ``retire_modules`` cascade deletes only addon
        TestHelpers, never ``module='@framework'``. A class removed from an era
        (e.g. SavepointCase leaving the
        menu at v17+) would otherwise survive on every already-indexed server
        forever.

        Two safety properties (ADR-0054) that MUST hold:

        1. **No ping-pong.** The ``th.name IN $known_universe`` clause restricts
           deletion to names this feature has EVER emitted
           (``KNOWN_FRAMEWORK_BASE_NAMES``). A source-less run at some future
           version can therefore never delete a class that a source-bearing
           parse discovered and is not yet in the curated table — without this
           clause the source-bearing and source-less paths would alternately
           create and delete the very same node on every reindex.
        2. **Profile-invariant.** ``live_names`` is a pure function of
           ``odoo_version`` alone (``framework_bases(odoo_version)`` never takes
           a profile), so profile A's prune call can never delete a node profile
           B's reindex still needs. This is why, unlike ``gc_stale_test_nodes``,
           this method needs no ``repo``/profile scoping parameter.

        Returns the number of TestHelper nodes deleted.
        """
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (th:TestHelper {module: '@framework', odoo_version: $version})
                WHERE NOT th.name IN $live_names AND th.name IN $known_universe
                DETACH DELETE th
                RETURN count(th) AS deleted
                """,
                version=odoo_version,
                live_names=list(live_names),
                known_universe=list(KNOWN_FRAMEWORK_BASE_NAMES),
            ).single()
            deleted = row["deleted"] if row is not None else 0
            if deleted > 0:
                _logger.info(
                    "prune_framework_test_helpers: deleted %d stale '@framework' "
                    "TestHelper node(s) for version %s",
                    deleted, odoo_version,
                )
            return deleted

    def reconcile_test_inherits(self, odoo_version: str) -> int:
        """MERGE missing INHERITS_TEST edges for all TestClass nodes at odoo_version.

        Post-pass (like reconcile_same_name_inherits): runs VERSION-WIDE after all
        repos for the version have been written. Resolves base class names to
        TestHelper OR TestClass nodes, creating directed INHERITS_TEST edges.

        Resolution priority: TestHelper first (framework bases), then TestClass.
        Multi-base fan-out is correct (one TestClass can inherit N bases -> N edges).
        Uses flat OPTIONAL MATCH, no VLP (ADR-0048).

        Idempotent (MERGE). Safe in both incremental and full-reindex runs.
        Returns count of edges created.
        """
        try:
            with self.driver.session() as session:
                row = session.run(
                    """
                    // For each TestClass, unwind its ordered base list and resolve
                    // each base to a TestHelper or TestClass at the same version.
                    // INHERITS_TEST fan-out is OK (K x D, not K^2).
                    MATCH (tc:TestClass {odoo_version: $version})
                    UNWIND tc.base_classes_ordered AS base_name
                    // Resolve to TestHelper first (framework + addon helpers)
                    OPTIONAL MATCH (h:TestHelper {name: base_name, odoo_version: $version})
                    // If no TestHelper, resolve to a same-version TestClass
                    OPTIONAL MATCH (bc:TestClass {name: base_name, odoo_version: $version})
                    WHERE h IS NULL
                    WITH tc, base_name,
                         CASE WHEN h IS NOT NULL THEN h ELSE bc END AS target
                    WHERE target IS NOT NULL
                      AND NOT (tc)-[:INHERITS_TEST]->(target)
                    MERGE (tc)-[:INHERITS_TEST]->(target)
                    RETURN count(*) AS created
                    """,
                    version=odoo_version,
                ).single()
                created = row["created"] if row is not None else 0
                if created > 0:
                    _logger.info(
                        "INHERITS_TEST reconciliation: created %d edge(s) for version %s",
                        created, odoo_version,
                    )
                else:
                    _logger.debug(
                        "INHERITS_TEST reconciliation: no gaps found for version %s",
                        odoo_version,
                    )
                return created
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "INHERITS_TEST reconciliation failed for version %s: %s — "
                "indexer run continues; next run will retry",
                odoo_version, exc,
            )
            return 0

    def reconcile_test_coverage(self, odoo_version: str) -> int:
        """MERGE COVERS_MODEL/COVERS_FIELD/COVERS_METHOD edges from TestMethod refs.

        Post-pass (VERSION-WIDE, idempotent MERGE). Resolves model_refs/field_refs
        to is_definition=true nodes only (ADR-0013, ADR-0048 K x D rule).
        Gracefully skips unknown refs (no dangling edges, design §2.3).

        COVERS_* edges carry a `via` property ('setup'|'assert'|'body') from
        TestMethod.via, enabling tools to rank assert-coverage above setup-coverage.

        Returns total count of edges created.
        """
        total = 0
        try:
            with self.driver.session() as session:
                # COVERS_MODEL: from model_refs -> is_definition Model node
                row_m = session.run(
                    """
                    MATCH (tm:TestMethod {odoo_version: $version})
                    UNWIND tm.model_refs AS mref
                    OPTIONAL MATCH (md:Model {name: mref, odoo_version: $version})
                    WHERE md.is_definition = true
                    WITH tm, md WHERE md IS NOT NULL
                      AND NOT (tm)-[:COVERS_MODEL]->(md)
                    MERGE (tm)-[r:COVERS_MODEL]->(md)
                    ON CREATE SET r.via = coalesce(tm.via, 'body')
                    RETURN count(r) AS created
                    """,
                    version=odoo_version,
                ).single()
                total += row_m["created"] if row_m is not None else 0

                # COVERS_FIELD: from field_refs (attr names) -> Field nodes on definition model
                # field_refs are simple attr names; we need the model to scope the lookup.
                # We join via COVERS_MODEL to get the model context, then match Field by name.
                row_f = session.run(
                    """
                    MATCH (tm:TestMethod {odoo_version: $version})-[:COVERS_MODEL]->(md:Model)
                    WHERE md.is_definition = true
                    UNWIND tm.field_refs AS fname
                    OPTIONAL MATCH (fd:Field {name: fname, model: md.name, odoo_version: $version})
                    WITH tm, fd WHERE fd IS NOT NULL
                      AND NOT (tm)-[:COVERS_FIELD]->(fd)
                    MERGE (tm)-[r:COVERS_FIELD]->(fd)
                    ON CREATE SET r.via = coalesce(tm.via, 'body')
                    RETURN count(r) AS created
                    """,
                    version=odoo_version,
                ).single()
                total += row_f["created"] if row_f is not None else 0

                # COVERS_METHOD: from method_refs (method name strings) -> Method nodes on
                # the is_definition model. Mirrors COVERS_FIELD: join via COVERS_MODEL to get
                # the model context (method_refs are plain names, no model prefix), then match
                # Method by (name, model) on the is_definition node only (ADR-0048 K×D rule).
                # Graceful-skip when method_refs is empty or method name is not indexed.
                row_m2 = session.run(
                    """
                    MATCH (tm:TestMethod {odoo_version: $version})-[:COVERS_MODEL]->(md:Model)
                    WHERE md.is_definition = true
                    UNWIND tm.method_refs AS mname
                    OPTIONAL MATCH (meth:Method {name: mname, model: md.name,
                                                 odoo_version: $version})
                    WITH tm, meth WHERE meth IS NOT NULL
                      AND NOT (tm)-[:COVERS_METHOD]->(meth)
                    MERGE (tm)-[r:COVERS_METHOD]->(meth)
                    ON CREATE SET r.via = coalesce(tm.via, 'body')
                    RETURN count(r) AS created
                    """,
                    version=odoo_version,
                ).single()
                total += row_m2["created"] if row_m2 is not None else 0

                if total > 0:
                    _logger.info(
                        "COVERS_* reconciliation: created %d edge(s) for version %s",
                        total, odoo_version,
                    )
                else:
                    _logger.debug(
                        "COVERS_* reconciliation: no new edges for version %s",
                        odoo_version,
                    )
                return total
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "COVERS_* reconciliation failed for version %s: %s — "
                "indexer run continues; next run will retry",
                odoo_version, exc,
            )
            return 0

    def finalize_is_helper(self, odoo_version: str) -> int:
        """Promote TestClass nodes to TestHelper when: subclassed AND defines_no_test_methods.

        Post-pass (MISSED-1): is_helper is provisional at parse time (parser only
        sets defines_no_test_methods). This pass finalizes it after INHERITS_TEST
        edges exist, counting actual inbound edges from other TestClass nodes.

        Also creates a TestHelper projection node for each promoted class so that
        test_base_classes queries can find them consistently (the TestClass node
        still exists; the TestHelper node is the canonical query target).

        Idempotent (SET + MERGE). Returns count of TestClass nodes promoted.
        """
        try:
            with self.driver.session() as session:
                # Step 1: mark TestClass.is_helper=true where subclassed and no test methods
                row = session.run(
                    """
                    MATCH (tc:TestClass {odoo_version: $version, defines_no_test_methods: true})
                    WHERE COUNT { ()-[:INHERITS_TEST]->(tc) } > 0
                    SET tc.is_helper = true
                    RETURN count(tc) AS promoted
                    """,
                    version=odoo_version,
                ).single()
                promoted = row["promoted"] if row is not None else 0

                if promoted > 0:
                    # Step 2: ensure a TestHelper projection node exists for each promoted class
                    session.run(
                        """
                        MATCH (tc:TestClass {odoo_version: $version, is_helper: true})
                        MERGE (th:TestHelper {
                            name: tc.name, module: tc.module, odoo_version: $version
                        })
                        ON CREATE SET th.origin = 'addon',
                                      th.test_type = tc.test_type,
                                      th.commit_allowed = tc.commit_allowed,
                                      th.file_path = tc.file_path,
                                      th.line = tc.line,
                                      th.profile = coalesce(tc.profile, [])
                        ON MATCH SET th.origin = 'addon',
                                     th.test_type = tc.test_type,
                                     th.profile = [
                                         x IN coalesce(th.profile, [])
                                         WHERE NOT x IN coalesce(tc.profile, [])
                                     ] + coalesce(tc.profile, [])
                        """,
                        version=odoo_version,
                    )
                    _logger.info(
                        "finalize_is_helper: promoted %d TestClass nodes to is_helper=true "
                        "and created TestHelper projections for version %s",
                        promoted, odoo_version,
                    )
                else:
                    _logger.debug(
                        "finalize_is_helper: no promotions needed for version %s",
                        odoo_version,
                    )
                return promoted
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "finalize_is_helper failed for version %s: %s — "
                "indexer run continues; next run will retry",
                odoo_version, exc,
            )
            return 0

    def gc_stale_test_nodes(
        self,
        odoo_version: str,
        live_module_names: list[str],
        live_file_paths: list[str] | None = None,
        repo: str | None = None,
        live_modules_for_file_gc: list[str] | None = None,
    ) -> int:
        """Delete stale TestClass/TestMethod/COVERS_* nodes (MISSED-2, Defect H fix).

        Two prune granularities (M6):
        1. MODULE-level: remove TestClass/TestMethod whose ``module`` is no longer
           in ``live_module_names`` for this repo (a whole module renamed/removed).
           Scoped by ``repo`` (Defect H): without repo-scoping a per-repo GC call
           would delete nodes belonging to another repo at the same version whose
           module names are not in this repo's live set.
        2. FILE-level: when ``live_file_paths`` is supplied, remove TestClass/TestMethod
           whose ``module`` IS in ``live_modules_for_file_gc`` but whose ``file_path``
           is NOT live (i.e. a test file was deleted INSIDE a still-present module).
           ``live_modules_for_file_gc`` MUST be only the modules actually re-parsed this
           run (the changed-module subset on incremental) — NOT all live modules from
           the registry (Defect I fix). Scoping to re-parsed modules only ensures that
           unchanged modules whose test files were not re-emitted are never pruned.

        Both prune queries are repo-scoped when ``repo`` is supplied (Defect H).
        DETACH DELETE also drops the INHERITS_TEST / COVERS_* / BELONGS_TO_TEST edges.
        Returns total count of deleted nodes.

        Args:
            odoo_version:              Odoo version label.
            live_module_names:         Full set of live module names for this repo+version
                                       (from the full pre-incremental registry scan).
                                       Used for MODULE-level prune.
            live_file_paths:           Repo-relative test file paths emitted this run.
                                       None skips file-level prune.
            repo:                      Repo dir basename (e.g. 'odoo_17.0'). Scopes
                                       BOTH prune queries so cross-repo deletion cannot
                                       happen (Defect H). None disables repo-scoping
                                       (backwards compat for tests that pre-date repo).
            live_modules_for_file_gc:  Subset of modules whose test files were actually
                                       re-parsed this run (Defect I fix). The file-level
                                       prune restricts to ONLY these modules so unchanged
                                       modules are never candidates. Defaults to
                                       live_module_names when None (safe for --full
                                       reindex where ALL modules are re-parsed).
        """
        try:
            with self.driver.session() as session:
                if not live_module_names:
                    return 0

                # Repo-scope predicate (Defect H): added to BOTH prune queries.
                # When repo is None (backwards compat) no repo filter is applied.
                repo_filter = "AND tm.repo = $repo" if repo is not None else ""
                repo_filter_tc = "AND tc.repo = $repo" if repo is not None else ""
                extra_params: dict = {"repo": repo} if repo is not None else {}

                # Defect I: file-level prune must scope to the re-parsed modules only,
                # not ALL live modules. On --full, all modules are re-parsed so both
                # sets are equal. On incremental, live_modules_for_file_gc is the
                # changed-module subset; unchanged modules are excluded from file-prune.
                _file_gc_modules = (
                    live_modules_for_file_gc
                    if live_modules_for_file_gc is not None
                    else live_module_names
                )

                # 1. MODULE-level prune (whole module gone from this repo).
                row_tm = session.run(
                    f"""
                    MATCH (tm:TestMethod {{odoo_version: $version}})
                    WHERE NOT tm.module IN $live_modules
                    {repo_filter}
                    DETACH DELETE tm
                    RETURN count(tm) AS deleted
                    """,
                    version=odoo_version,
                    live_modules=live_module_names,
                    **extra_params,
                ).single()
                deleted_tm = row_tm["deleted"] if row_tm is not None else 0

                row_tc = session.run(
                    f"""
                    MATCH (tc:TestClass {{odoo_version: $version}})
                    WHERE NOT tc.module IN $live_modules
                    {repo_filter_tc}
                    DETACH DELETE tc
                    RETURN count(tc) AS deleted
                    """,
                    version=odoo_version,
                    live_modules=live_module_names,
                    **extra_params,
                ).single()
                deleted_tc = row_tc["deleted"] if row_tc is not None else 0

                deleted_tm_file = 0
                deleted_tc_file = 0
                # 2. FILE-level prune (file deleted inside a re-parsed module).
                # Scoped to _file_gc_modules (re-parsed subset) not all live modules
                # so unchanged modules are never touched (Defect I fix).
                if live_file_paths is not None:
                    row_tmf = session.run(
                        f"""
                        MATCH (tm:TestMethod {{odoo_version: $version}})
                        WHERE tm.module IN $file_gc_modules
                          AND NOT tm.file_path IN $live_files
                        {repo_filter}
                        DETACH DELETE tm
                        RETURN count(tm) AS deleted
                        """,
                        version=odoo_version,
                        file_gc_modules=_file_gc_modules,
                        live_files=live_file_paths,
                        **extra_params,
                    ).single()
                    deleted_tm_file = row_tmf["deleted"] if row_tmf is not None else 0

                    row_tcf = session.run(
                        f"""
                        MATCH (tc:TestClass {{odoo_version: $version}})
                        WHERE tc.module IN $file_gc_modules
                          AND NOT tc.file_path IN $live_files
                        {repo_filter_tc}
                        DETACH DELETE tc
                        RETURN count(tc) AS deleted
                        """,
                        version=odoo_version,
                        file_gc_modules=_file_gc_modules,
                        live_files=live_file_paths,
                        **extra_params,
                    ).single()
                    deleted_tc_file = row_tcf["deleted"] if row_tcf is not None else 0

                total = deleted_tm + deleted_tc + deleted_tm_file + deleted_tc_file
                if total > 0:
                    _logger.info(
                        "Test node GC: deleted %d TestMethod + %d TestClass (module-gone), "
                        "%d TestMethod + %d TestClass (file-gone) for version %s",
                        deleted_tm, deleted_tc, deleted_tm_file, deleted_tc_file,
                        odoo_version,
                    )
                return total
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Test node GC failed for version %s: %s — skipping",
                odoo_version, exc,
            )
            return 0

    def write_spec_metadata(
        self, kind: str, odoo_version: str, curate_status: str,
    ) -> None:
        """Upsert a SpecMetadata node recording curation status for a spec kind + version.

        Composite key: (kind, odoo_version). MERGE is idempotent.

        Args:
            kind:          'lint' | 'cli' — which spec category this metadata covers.
            odoo_version:  Odoo version label, e.g. '8.0', '17.0'.
            curate_status: 'pending' | 'complete' (the two values actually written
                by the pipeline: 'complete' when a static spec JSON declares
                ``_curate_status: complete``, else the 'pending' default). Any
                string is accepted per ADR-0002 §4; the MCP read side treats
                anything other than 'pending'/'complete' (incl. absent) as a
                data gap and discloses accordingly.
        """
        with self.driver.session() as session:
            session.run("""
                MERGE (sm:SpecMetadata {kind: $kind, odoo_version: $v})
                SET sm.curate_status = $curate_status
            """, kind=kind, v=odoo_version, curate_status=curate_status)


# ---------------------------------------------------------------------------
# B5 split: module-level write functions extracted by node-group.
# Imported here at the BOTTOM (after _profile_union_set, _chunked and the
# Neo4jWriter class are all defined above) because Neo4jWriter.write_* methods
# call these _write_* functions as BARE names via session.execute_write(...),
# resolving them through this module namespace at call time. They are therefore
# GENUINE facade-internal dependencies, not re-export shims - every external
# caller now imports directly from the writer_neo4j_{orm,spec,ui} child modules
# (Phase 7.5 codemod). The child modules import _profile_union_set lazily
# (function-local) so each is independently cold-importable without a cycle.
# ---------------------------------------------------------------------------
from .writer_neo4j_orm import _write_parse_result  # noqa: E402,I001
from .writer_neo4j_spec import (  # noqa: E402,I001
    _write_cli_commands_batch,
    _write_cli_flag_replacements,
    _write_cli_flags_batch,
    _write_core_symbols_batch,
    _write_lint_rules_batch,
    _write_lint_violations_batch,
    _write_pattern_examples_batch,
    _write_replaced_by_edges,
)
from .writer_neo4j_ui import (  # noqa: E402,I001
    _write_asset_parse_result,
    _write_js_graph_result,
    _write_stylesheets_batch,
    _write_view_parse_result,
)


# ---------------------------------------------------------------------------
# WI-1: test surface write helpers (module-level, called via execute_write)
# ---------------------------------------------------------------------------

def _write_test_classes_batch(tx, result: "TestParseResult", profiles: list[str]) -> None:
    """Write TestClass + TestMethod nodes from one TestParseResult (one module).

    MERGE key for TestClass: (name, module, file_path, repo, odoo_version) - CRITICAL-1.
    MERGE key for TestMethod: (name, test_class, module, file_path, repo, odoo_version).
    `repo` is included in the MERGE key (Defect H fix): two repos at the same version can
    both define a class with the same (name, module, file_path) — e.g. odoo/sale and
    enterprise/sale both having tests/common.py::TestSaleCommon. Without repo in the key
    the second write would silently overwrite the first (cross-repo collision).
    profile[] is union-only (ADR-0034, mirrors _profile_union_set pattern).
    DEFINED_IN edge is created to the owning Module node (addon nodes only; skip
    when module='@framework' - framework helpers go through _write_test_helpers_batch).
    """
    union_expr = _profile_union_set("tc")
    union_expr_m = _profile_union_set("tm")
    repo = result.module.repo  # repo dir basename, same value carried on Module nodes

    for tc in result.test_classes:
        # MERGE TestClass node (CRITICAL-1 + Defect H: 5-part key including repo)
        tx.run(
            f"""
            MERGE (tc:TestClass {{
                name: $name,
                module: $module,
                file_path: $file_path,
                repo: $repo,
                odoo_version: $ver
            }})
            SET tc.test_type = $test_type,
                tc.base_classes_ordered = $base_classes_ordered,
                tc.tagged = $tagged,
                tc.commit_allowed = $commit_allowed,
                tc.defines_no_test_methods = $defines_no_test_methods,
                tc.is_helper = $is_helper,
                tc.docstring = $docstring,
                tc.line = $line,
                tc.profile = {union_expr}
            WITH tc
            MATCH (m:Module {{name: $module, odoo_version: $ver}})
            MERGE (tc)-[:DEFINED_IN]->(m)
            """,
            name=tc.name,
            module=tc.module,
            file_path=tc.file_path,
            repo=repo,
            ver=tc.odoo_version,
            test_type=tc.test_type,
            base_classes_ordered=tc.base_classes_ordered,
            tagged=tc.tagged,
            commit_allowed=tc.commit_allowed,
            defines_no_test_methods=tc.defines_no_test_methods,
            is_helper=tc.is_helper,
            docstring=tc.docstring,
            line=tc.line,
            profiles=profiles,
        )

        # MERGE TestMethod nodes for this class
        for meth in tc.methods:
            tx.run(
                f"""
                MATCH (tc:TestClass {{
                    name: $test_class,
                    module: $module,
                    file_path: $file_path,
                    repo: $repo,
                    odoo_version: $ver
                }})
                MERGE (tm:TestMethod {{
                    name: $name,
                    test_class: $test_class,
                    module: $module,
                    file_path: $file_path,
                    repo: $repo,
                    odoo_version: $ver
                }})
                SET tm.tagged = $tagged,
                    tm.docstring = $docstring,
                    tm.field_refs = $field_refs,
                    tm.model_refs = $model_refs,
                    tm.method_refs = $method_refs,
                    tm.asserts_count = $asserts_count,
                    tm.via = $via,
                    tm.line = $line,
                    tm.profile = {union_expr_m}
                MERGE (tm)-[:BELONGS_TO_TEST]->(tc)
                """,
                name=meth.name,
                test_class=tc.name,
                module=tc.module,
                file_path=tc.file_path,
                repo=repo,
                ver=tc.odoo_version,
                tagged=meth.tagged,
                docstring=meth.docstring,
                field_refs=meth.field_refs,
                model_refs=meth.model_refs,
                method_refs=meth.method_refs,
                asserts_count=meth.asserts_count,
                via=meth.via,
                line=meth.line,
                profiles=profiles,
            )


def _write_test_helpers_batch(
    tx, helpers: "list[TestHelperInfo]", profiles: list[str],
) -> None:
    """Write TestHelper nodes. Framework helpers (module='@framework') get no DEFINED_IN edge.

    MERGE key: (name, module, odoo_version).
    profile[] is union-only (ADR-0034).

    ``file_path``/``line`` use coalesce-ON-MATCH (issue #362 WI-4, mirrors the
    Module identity-card pattern in ``writer_neo4j_orm.py::_write_parse_result``
    ON MATCH SET, e.g. ``m.shortdesc = coalesce($shortdesc, m.shortdesc)``):
    the profile reindex path (``reconcile_test_surface``) seeds framework
    helpers WITHOUT a source root, so ``h.file_path``/``h.line`` arrive as
    None on that call. Unconditionally overwriting would erase every
    parse-derived enrichment on the very next nightly reindex. A later
    source-less seed therefore never wipes a previously parse-derived value;
    a source-bearing seed (non-None) still updates it.
    """
    union_expr = _profile_union_set("th")
    for h in helpers:
        if h.module == "@framework":
            # Framework helper: no DEFINED_IN edge (MED-3)
            tx.run(
                f"""
                MERGE (th:TestHelper {{name: $name, module: $module, odoo_version: $ver}})
                ON CREATE SET th.origin = $origin,
                              th.test_type = $test_type,
                              th.setup_summary = $setup_summary,
                              th.commit_allowed = $commit_allowed,
                              th.file_path = $file_path,
                              th.line = $line
                ON MATCH  SET th.origin = $origin,
                              th.test_type = $test_type,
                              th.setup_summary = $setup_summary,
                              th.commit_allowed = $commit_allowed,
                              th.file_path = coalesce($file_path, th.file_path),
                              th.line = coalesce($line, th.line)
                SET th.profile = {union_expr}
                """,
                name=h.name, module=h.module, ver=h.odoo_version,
                origin=h.origin, test_type=h.test_type,
                setup_summary=h.setup_summary, commit_allowed=h.commit_allowed,
                file_path=h.file_path, line=h.line, profiles=profiles,
            )
        else:
            # Addon helper: create DEFINED_IN edge if Module exists
            tx.run(
                f"""
                MERGE (th:TestHelper {{name: $name, module: $module, odoo_version: $ver}})
                ON CREATE SET th.origin = $origin,
                              th.test_type = $test_type,
                              th.setup_summary = $setup_summary,
                              th.commit_allowed = $commit_allowed,
                              th.file_path = $file_path,
                              th.line = $line
                ON MATCH  SET th.origin = $origin,
                              th.test_type = $test_type,
                              th.setup_summary = $setup_summary,
                              th.commit_allowed = $commit_allowed,
                              th.file_path = coalesce($file_path, th.file_path),
                              th.line = coalesce($line, th.line)
                SET th.profile = {union_expr}
                WITH th
                MATCH (m:Module {{name: $module, odoo_version: $ver}})
                MERGE (th)-[:DEFINED_IN]->(m)
                """,
                name=h.name, module=h.module, ver=h.odoo_version,
                origin=h.origin, test_type=h.test_type,
                setup_summary=h.setup_summary, commit_allowed=h.commit_allowed,
                file_path=h.file_path, line=h.line, profiles=profiles,
            )


# ---------------------------------------------------------------------------
# WI-3: JsTestSuite write helper (module-level, called via execute_write)
# ---------------------------------------------------------------------------

def _write_js_test_batch(
    tx,
    suites: "list",
    profiles: list[str],
) -> None:
    """Write JsTestSuite nodes for a list of JS test files.

    MERGE key: (file_path, module, odoo_version) - file-grained (one node per file).
    profile[] is union-only (ADR-0034, mirrors _profile_union_set pattern).
    DEFINED_IN edge is created to the owning Module node.

    MED-1 contract: NO COVERS_MODEL edge is emitted here (mock_models are test-doubles,
    not real Odoo models). The writer MUST NOT add COVERS_MODEL edges from JsTestSuite.
    """
    union_expr = _profile_union_set("js")
    for suite in suites:
        tx.run(
            f"""
            MERGE (js:JsTestSuite {{
                file_path: $file_path,
                module: $module,
                odoo_version: $ver
            }})
            SET js.framework = $framework,
                js.describe_blocks = $describe_blocks,
                js.test_names = $test_names,
                js.tags = $tags,
                js.mounts = $mounts,
                js.mock_models = $mock_models,
                js.line = $line,
                js.profile = {union_expr}
            WITH js
            MATCH (m:Module {{name: $module, odoo_version: $ver}})
            MERGE (js)-[:DEFINED_IN]->(m)
            """,
            file_path=suite.file_path,
            module=suite.module,
            ver=suite.odoo_version,
            framework=suite.framework,
            describe_blocks=suite.describe_blocks,
            test_names=suite.test_names,
            tags=suite.tags,
            mounts=suite.mounts,
            mock_models=suite.mock_models,
            line=suite.line,
            profiles=profiles,
        )
