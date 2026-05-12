# src/indexer/protocols.py — Structural Protocol interfaces for parsers and writers.
#
# Rules:
#   - Import ONLY from .models — no concrete parser or writer imports (avoids circular deps)
#   - All protocols use @runtime_checkable so isinstance() works in tests

from typing import Protocol, runtime_checkable

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
    """Backend writer that persists parsed module data to a graph/vector store."""

    def write_results(self, results: list[ParseResult]) -> None: ...
    def write_view_results(self, results: list[ViewParseResult]) -> None: ...
    def write_js_graph_results(self, results: list[JSGraphResult]) -> None: ...
    def write_core_symbols(self, symbols: list[CoreSymbolInfo]) -> None: ...
    def write_lint_rules(self, rules: list[LintRuleInfo]) -> None: ...
    def write_cli_commands(self, commands: list[CLICommandInfo]) -> None: ...
    def write_cli_flags(self, flags: list[CLIFlagInfo]) -> None: ...
    def write_pattern_examples(self, patterns: list[PatternExample]) -> None: ...
