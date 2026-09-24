# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/protocols.py - Structural Protocol interfaces for parsers and writers.
#
# Rules:
#   - Import ONLY from .models - no concrete parser or writer imports (avoids circular deps)
#   - All protocols use @runtime_checkable so isinstance() works in tests

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    LintViolationInfo,
    ModuleInfo,
    ModuleOwner,
    ParseResult,
    PatternExample,
    StylesheetInfo,
    ViewParseResult,
)


@runtime_checkable
class PythonParserProtocol(Protocol):
    """Parser that extracts Odoo model/field/method data from a Python module."""

    def parse_module(self, info: ModuleInfo) -> ParseResult: ...


@runtime_checkable
class ViewParserProtocol(Protocol):
    """Parser that extracts view/QWeb data from XML files in a module."""

    def parse_module(self, info: ModuleInfo) -> ViewParseResult: ...


@runtime_checkable
class JSGraphParserProtocol(Protocol):
    """Parser that extracts JS patch/OWL component graph from a module."""

    def parse_module_graph(self, info: ModuleInfo) -> JSGraphResult: ...


@runtime_checkable
class IndexWriterProtocol(Protocol):
    """Full contract for a graph/vector store backend writer.

    Neo4jWriter satisfies this protocol via structural subtyping (no explicit
    declaration needed). Future backends (e.g. PostgresWriter) must implement
    all methods below.

    The `driver` attribute is intentionally typed as `Any` so the protocol
    stays backend-agnostic while still documenting that low-level DB access
    is available when needed (e.g. cross-repo dependency queries).
    """

    driver: Any  # backend-specific connection handle (e.g. neo4j.Driver)

    def close(self) -> None: ...
    def setup_indexes(self) -> None: ...
    def setup_pattern_indexes(self) -> None: ...  # patterns-only reseed subset

    # --- Parse result writers ------------------------------------------------
    def write_results(self, results: list[ParseResult]) -> None: ...
    def write_view_results(self, results: list[ViewParseResult]) -> None: ...
    def write_js_graph_results(self, results: list[JSGraphResult]) -> None: ...

    def write_asset_results(
        self, results: list, profiles: list[str] | None = None,
    ) -> None:
        """Persist :AssetBundle nodes + CONTRIBUTES_TO/INCLUDES_BUNDLE (WI-D).

        Must run BEFORE write_view_results so legacy XML asset-bundle extenders
        resolve against the AssetBundle base nodes (EXTENDS_ASSET_BUNDLE).
        """
        ...

    # --- Test-surface writers (WI-1/WI-3) ------------------------------------
    def write_test_results(
        self, results: list, profiles: list[str] | None = None,
    ) -> None:
        """Persist TestClass/TestMethod (+addon TestHelper) nodes."""
        ...

    def write_js_test_results(
        self, suites: list, profiles: list[str] | None = None,
    ) -> None:
        """Persist JsTestSuite nodes (no COVERS_MODEL edge; MED-1)."""
        ...

    def write_framework_test_helpers(
        self, helpers: list, profiles: list[str] | None = None,
    ) -> None:
        """Persist framework TestHelper nodes (module='@framework', no DEFINED_IN)."""
        ...

    def prune_framework_test_helpers(
        self, odoo_version: str, live_names: Iterable[str],
    ) -> int:
        """DETACH DELETE stale '@framework' TestHelper nodes (issue #362 WI-4).

        Deletes only names outside the current era's live set AND inside
        KNOWN_FRAMEWORK_BASE_NAMES (the prune universe) — never a name a
        source-bearing parse discovered that the curated table does not know
        about yet (no create/delete ping-pong). ``live_names`` must be a pure
        function of ``odoo_version`` alone, never of ``odoo_source_root`` or
        profile, so one profile's prune can never delete what another profile
        still needs. Returns the count of nodes deleted.
        """
        ...

    def reconcile_test_inherits(self, odoo_version: str) -> int:
        """MERGE INHERITS_TEST edges (version-wide post-pass, idempotent)."""
        ...

    def reconcile_test_coverage(self, odoo_version: str) -> int:
        """MERGE COVERS_MODEL/FIELD/METHOD edges to is_definition nodes."""
        ...

    def finalize_is_helper(self, odoo_version: str) -> int:
        """Promote subclassed no-test-method TestClass nodes to is_helper."""
        ...

    def gc_stale_test_nodes(
        self,
        odoo_version: str,
        live_module_names: list[str],
        live_file_paths: list[str] | None = None,
    ) -> int:
        """Delete stale TestClass/TestMethod nodes on --full cleanup (MISSED-2/M6)."""
        ...

    # --- Spec layer writers --------------------------------------------------
    def write_core_symbols(self, symbols: list[CoreSymbolInfo]) -> None: ...
    def write_lint_rules(self, rules: list[LintRuleInfo]) -> None: ...
    def write_cli_commands(self, commands: list[CLICommandInfo]) -> None: ...
    def write_cli_flags(self, flags: list[CLIFlagInfo]) -> None: ...
    def write_cli_flag_replacements(
        self,
        replaced: list[tuple[str, str]],
        *,
        command_name: str,
        from_version: str,
        to_version: str,
    ) -> None: ...

    def prune_lint_rules(
        self, odoo_version: str, live_rule_ids: Iterable[str],
    ) -> int:
        """DETACH DELETE stale LintRule nodes at ``odoo_version`` (issue #364).

        ``write_lint_rules`` is MERGE-only; index_core writes the FULL rule set
        per version, so a rule_id removed upstream (e.g. #364 dropped W8140 from
        v14-v19) is deleted by comparing against this run's live id set.
        Empty-guard: an empty ``live_rule_ids`` NEVER deletes. Soft-drop gate:
        skips + warns if the prune would remove a large fraction of the version's
        nodes. CoreSymbol is exempt (lifecycle history). Returns count deleted.
        """
        ...

    def prune_cli_commands(
        self, odoo_version: str, live_names: Iterable[str],
    ) -> int:
        """DETACH DELETE stale CLICommand nodes at ``odoo_version`` (issue #364).

        Same prune-on-full-write contract as :meth:`prune_lint_rules`, keyed on
        the CLICommand ``name``. Empty-guard + soft-drop gate. Returns count.
        """
        ...

    def prune_cli_flags(
        self, odoo_version: str, live_keys: Iterable[str],
    ) -> int:
        """DETACH DELETE stale CLIFlag nodes at ``odoo_version`` (issue #364).

        CLIFlag identity is (flag_name, command_name, odoo_version) with a
        NULLABLE command_name; ``live_keys`` are joined strings
        ``f"{flag_name}|{command_name or ''}"`` so the null case is unambiguous.
        Empty-guard + soft-drop gate. Returns count deleted.
        """
        ...
    def write_spec_metadata(
        self, kind: str, odoo_version: str, curate_status: str,
    ) -> None: ...
    def write_diff_edges(
        self, diff: Any, *, from_version: str, to_version: str,
    ) -> None: ...
    def write_lifecycle_properties(
        self, diff: Any, *, from_version: str, to_version: str,
    ) -> None: ...

    # --- Pattern layer -------------------------------------------------------
    def write_pattern_examples(
        self, patterns: list[PatternExample], *, prune: bool = False,
    ) -> int:
        """Persist PatternExample nodes (idempotent MERGE on ``pattern_id``).

        *prune* (default False): after MERGE-ing, DETACH DELETE every
        PatternExample whose ``pattern_id`` is NOT in *patterns* (the R1
        orphan-on-rename fix, issue #362 follow-up — mirrors
        :meth:`prune_framework_test_helpers`). Because PatternExample is keyed on
        ``pattern_id`` alone the prune is GLOBAL, so ``prune=True`` is safe ONLY
        on the FULL-catalogue write path; a partial (version-filtered) batch MUST
        pass ``prune=False`` or it would delete every other version's patterns.
        An empty *patterns* list never prunes. Returns the count pruned.
        """
        ...

    # --- Stylesheet + violation writers (ADR-0025) ---------------------------
    def write_stylesheets(
        self,
        stylesheets: list[StylesheetInfo],
        profiles: list[str] | None = None,
        repo_root: Any = None,
        repo_id: Any = None,
    ) -> None: ...
    """Persist :Stylesheet nodes + :IMPORTS edges."""

    def write_lint_violations(
        self,
        violations: list[LintViolationInfo],
        profiles: list[str] | None = None,
        repo_root: Any = None,
    ) -> None: ...
    """Persist :LintViolation nodes + :HAS_VIOLATION edges."""

    # --- Maintenance / GC ----------------------------------------------------
    def fetch_core_symbols(self, odoo_version: str) -> list: ...
    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict: ...

    # --- Module retirement cascade (ADR-0056 D9) ------------------------------
    def server_now(self) -> Any:
        """Store clock as an aware datetime; the ``run_started_at`` source."""
        ...

    def retire_modules(
        self, odoo_version: str, names: Iterable[str], *, run_started_at: Any,
    ) -> dict:
        """Delete modules + their MODULE_CHILD_LABELS subtree (guarded, idempotent)."""
        ...

    def drop_module_owner(
        self, odoo_version: str, name: str, owners: Iterable[ModuleOwner],
    ) -> dict:
        """Reset a surviving module's ownership to exactly *owners* (subtree-wide)."""
        ...

    def stamp_module_presence(
        self,
        odoo_version: str,
        rows: Iterable[Mapping[str, Any]],
        head: str | None,
        now: Any = None,
    ) -> int:
        """Stamp last_seen_sha/at + repos on existing Module nodes; returns matched count."""
        ...

    def orphan_module_names(
        self, odoo_version: str, present_names: Iterable[str], *, repo: str | None = None,
    ) -> list[str]: ...
    def module_profiles(
        self, odoo_version: str, names: Iterable[str] | None = None,
    ) -> dict[str, list[str]]: ...
    def orphan_child_keys(self, odoo_version: str) -> dict[str, dict[str, int]]: ...

    def gc_unresolved_placeholders(self, odoo_version: str) -> dict[str, int]:
        """DETACH DELETE '__unresolved__' placeholder nodes scoped to odoo_version."""
        ...

    def gc_null_repo_dep_stubs(self, odoo_version: str) -> int:
        """DETACH DELETE childless dep-stub Module nodes for odoo_version."""
        ...

    def gc_orphan_asset_bundles(self, odoo_version: str) -> int:
        """DETACH DELETE orphaned :AssetBundle nodes for odoo_version (WI-D).

        Called once per version on --full after all live contributions are
        re-written. Deletes only AssetBundle nodes with zero inbound
        CONTRIBUTES_TO and no INCLUDES_BUNDLE/EXTENDS_ASSET_BUNDLE edge.
        Idempotent (a second run returns 0).
        """
        ...

    def reconcile_same_name_inherits(self, odoo_version: str) -> int:
        """MERGE missing extender-to-definition INHERITS edges (ADR-0048, #273).

        Called once per version after ALL repos for that version are indexed.
        Non-fatal on error (logs WARNING, returns 0). Idempotent (MERGE).
        Concurrent same-version calls from --profile-workers can hit MERGE
        deadlocks; warn-and-continue policy catches them but leaves silent gaps -
        re-run or accept the miss (next full reindex fills it).
        """
        ...
