# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/protocols.py — Structural Protocol interfaces for parsers and writers.
#
# Rules:
#   - Import ONLY from .models — no concrete parser or writer imports (avoids circular deps)
#   - All protocols use @runtime_checkable so isinstance() works in tests

from typing import Any, Protocol, runtime_checkable

from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    JSGraphResult,
    LintRuleInfo,
    ModuleInfo,
    ParseResult,
    PatternExample,
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

    # --- Parse result writers ------------------------------------------------
    def write_results(self, results: list[ParseResult]) -> None: ...
    def write_view_results(self, results: list[ViewParseResult]) -> None: ...
    def write_js_graph_results(self, results: list[JSGraphResult]) -> None: ...

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

    # --- Maintenance / GC ----------------------------------------------------
    def fetch_core_symbols(self, odoo_version: str) -> list: ...
    def delete_modules_scoped(self, repo_basename: str, odoo_version: str) -> dict: ...
    def gc_stale_modules(
        self, repo: str, odoo_version: str, live_paths: set[str],
    ) -> int: ...
