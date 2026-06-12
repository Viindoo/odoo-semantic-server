# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j.py
import logging

from neo4j import GraphDatabase

from src.constants import (
    NEO4J_DELETE_BATCH_ROWS,
    NEO4J_WRITE_BATCH_SIZE,
    REL_INHERITS,
)

from .diff_engine import DiffResult
from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    LintViolationInfo,
    ParseResult,
    PatternExample,
    StylesheetInfo,
    ViewParseResult,
)

_logger = logging.getLogger(__name__)


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


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

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
                "CREATE INDEX IF NOT EXISTS FOR (n:JSPatch)"
                " ON (n.target, n.patch_name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:OWLComp)"
                " ON (n.name, n.module, n.odoo_version)",
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
                # M4.6 pattern layer (per ADR-0003):
                "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
                " ON (n.pattern_id)",
                "CREATE INDEX IF NOT EXISTS FOR (n:PatternExample)"
                " ON (n.language, n.odoo_version_min)",
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
            ]:
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
                       cs.replacement_qname AS replacement_qname
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

    def write_pattern_examples(self, patterns: list[PatternExample]) -> None:
        """Persist PatternExample nodes (idempotent MERGE on `pattern_id`).

        USES_CORE_SYMBOL edges to CoreSymbol nodes are silently skipped when
        the target does not exist — M4.5 graceful skip per ADR-0003 §5.
        Batched at 200/transaction (smaller than CoreSymbol's 500 because
        each pattern can fan-out N edge MERGEs).
        """
        if not patterns:
            return
        with self.driver.session() as session:
            for batch in _chunked(patterns, 200):
                session.execute_write(_write_pattern_examples_batch, batch)

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

    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict:
        """DETACH DELETE Module(s) matching (repo, odoo_version) + cascading child nodes.

        Child nodes (Model/Field/Method/View/QWebTmpl/JSPatch/OWLComp) are
        scoped by (module_name, odoo_version) — they're deleted ONLY if their
        Module parent is being deleted in this call, to avoid orphan cleanup of
        nodes that belong to other repos in the same version.

        Implementation note: steps 2 and 3 use CALL {} IN TRANSACTIONS (batched
        implicit-transaction form) to avoid exceeding db.transaction.timeout on large
        repos (odoo core 17.0 can have millions of child nodes). CALL IN TRANSACTIONS
        must run in an auto-commit (implicit) session — this method already uses
        self.driver.session() directly, so NO managed execute_write wrapper is used.
        The per-repo Postgres advisory lock held by the caller (web_ui/routes/repos.py)
        guarantees no concurrent writes to the same repo+version pair during deletion.

        Outer-tx timeout caveat (verified Neo4j 5.26.25, 2026-06-10): batching bounds
        each INNER transaction, but the OUTER coordinating transaction of
        CALL IN TRANSACTIONS is itself subject to db.transaction.timeout. Deleting a
        very large repo (millions of nodes, hundreds of batches) whose TOTAL elapsed
        exceeds the configured timeout (600s, see docs/operations/timeouts.md) will
        have its outer tx terminated part-way. This is recoverable — already-committed
        batches persist and a re-run resumes the delete (idempotent DETACH DELETE) —
        but the Web UI surfaces a TransactionTimedOut error. For an exceptionally
        large repo delete, temporarily raise/disable db.transaction.timeout
        (CALL dbms.setConfigValue('db.transaction.timeout','0')) per the same
        guidance as ops/cleanup_same_name_inherits_mesh.cypher.

        Returns: {"modules": N, "children": M} counts.
        """
        with self.driver.session() as session:
            # Step 1: collect module names being deleted (lightweight point-lookup)
            module_names_row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                RETURN collect(m.name) AS names
                """,
                repo=repo_basename,
                version=odoo_version,
            ).single()

            if module_names_row is None or not module_names_row["names"]:
                return {"modules": 0, "children": 0}

            module_names = module_names_row["names"]

            # Step 2: delete child nodes in batches (NEO4J_DELETE_BATCH_ROWS) to stay
            # well under db.transaction.timeout (600s). CALL {} IN TRANSACTIONS requires
            # an implicit (auto-commit) transaction — session.run() here, NOT execute_write.
            # CALL (child) { ... } syntax required for Neo4j 5.23+ (5.x deprecates
            # CALL { WITH <var> } in favour of CALL (<var>) { }; both work on 5.26.25).
            children_row = session.run(
                f"""
                MATCH (child)
                WHERE child.module IN $names AND child.odoo_version = $version
                  AND (child:Model OR child:Field OR child:Method OR child:View
                       OR child:QWebTmpl OR child:JSPatch OR child:OWLComp)
                CALL (child) {{
                    DETACH DELETE child
                }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                RETURN count(child) AS cc
                """,
                names=module_names,
                version=odoo_version,
            ).single()
            children_deleted = children_row["cc"] if children_row is not None else 0

            # Step 3: delete the Module nodes themselves (batched, same rationale)
            modules_row = session.run(
                f"""
                MATCH (m:Module {{repo: $repo, odoo_version: $version}})
                CALL (m) {{
                    DETACH DELETE m
                }} IN TRANSACTIONS OF {NEO4J_DELETE_BATCH_ROWS} ROWS
                RETURN count(m) AS mc
                """,
                repo=repo_basename,
                version=odoo_version,
            ).single()
            modules_deleted = modules_row["mc"] if modules_row is not None else 0

        return {"modules": modules_deleted, "children": children_deleted}

    def gc_stale_modules(
        self, repo: str, odoo_version: str, live_paths: set[str],
    ) -> int:
        """Delete Module nodes for this repo+version whose 'path' is not in live_paths.

        Returns count deleted. Uses DETACH DELETE so all edges (DEFINED_IN,
        DEPENDS_ON, etc.) are removed along with the stale node.

        Args:
            repo:         m.repo value (repo root dir name, e.g. 'odoo_17.0').
            odoo_version: Odoo version label, e.g. '17.0'.
            live_paths:   Repo-relative module path strings for this repo in this
                          run (ADR-0037: must match the relative form stored in
                          Module.path).  Modules NOT in this set are stale.  The
                          caller (pipeline) is responsible for relativizing the
                          scanner output before passing it here — a mismatch
                          (absolute live_paths vs relative Module.path) would
                          mark every node stale and DETACH DELETE the graph.

        Risk gate (enforced by caller): only called when len(live_paths) >= 1.

        ADR-0037 mixed-graph guard: live_paths is now repo-RELATIVE.  If this
        runs against a graph still holding pre-ADR-0037 ABSOLUTE Module.path
        (starts with '/'), EVERY module would mismatch live_paths and be DETACH
        DELETEd.  So before deleting, count absolute-path Module nodes for this
        repo+version; if any exist the graph is mixed/legacy — SKIP GC, log a
        warning, and return 0 so the operator runs a full ``--full`` reindex
        first (per docs/deploy/reindex-v8-v19-runbook.md).
        """
        with self.driver.session() as session:
            abs_count = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                WHERE m.path STARTS WITH '/'
                RETURN count(m) AS n
                """,
                repo=repo,
                version=odoo_version,
            ).single()
            if abs_count is not None and abs_count["n"] > 0:
                _logger.warning(
                    "Module GC skipped: %d Module node(s) for repo %s version %s "
                    "still carry ABSOLUTE paths (pre-ADR-0037). Running relative-path "
                    "GC against them would delete the whole repo. Run a full --full "
                    "reindex first (see reindex-v8-v19-runbook.md).",
                    abs_count["n"], repo, odoo_version,
                )
                return 0
            row = session.run(
                """
                MATCH (m:Module {repo: $repo, odoo_version: $version})
                WHERE NOT m.path IN $live_paths
                DETACH DELETE m
                RETURN count(m) AS n
                """,
                repo=repo,
                version=odoo_version,
                live_paths=list(live_paths),
            ).single()
        return row["n"] if row is not None else 0

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
        ``DEFINED_IN`` children.  Because ``gc_stale_modules`` keys on a
        concrete non-NULL ``repo`` string it never matches these stubs.

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
from .writer_neo4j_orm import _write_parse_result  # noqa: E402
from .writer_neo4j_spec import (  # noqa: E402
    _write_cli_commands_batch,
    _write_cli_flag_replacements,
    _write_cli_flags_batch,
    _write_core_symbols_batch,
    _write_lint_rules_batch,
    _write_lint_violations_batch,
    _write_pattern_examples_batch,
    _write_replaced_by_edges,
)
from .writer_neo4j_ui import (  # noqa: E402
    _write_js_graph_result,
    _write_stylesheets_batch,
    _write_view_parse_result,
)
