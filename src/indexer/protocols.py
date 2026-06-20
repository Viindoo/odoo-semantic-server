# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/protocols.py - Structural Protocol interfaces for parsers and writers.
#
# Rules:
#   - Import ONLY from .models - no concrete parser or writer imports (avoids circular deps)
#   - All protocols use @runtime_checkable so isinstance() works in tests

from typing import Any, Protocol, runtime_checkable

from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    LintViolationInfo,
    ModuleInfo,
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
    def write_pattern_examples(self, patterns: list[PatternExample]) -> None: ...

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
    def gc_stale_modules(
        self, repo: str, odoo_version: str, live_paths: set[str],
    ) -> int: ...

    def gc_unresolved_placeholders(self, odoo_version: str) -> dict[str, int]:
        """DETACH DELETE '__unresolved__' placeholder nodes scoped to odoo_version."""
        ...

    def gc_null_repo_dep_stubs(self, odoo_version: str) -> int:
        """DETACH DELETE childless dep-stub Module nodes for odoo_version."""
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
