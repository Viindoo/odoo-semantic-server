# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_cli.py
"""Extract CLICommand + CLIFlag from Odoo upstream source (M4.5 WI4).

Sources:
    <pkg>/cli/<name>.py — `class X(Command)` subclasses → CLICommandInfo.
    <pkg>/tools/config.py — `parser.add_option / group.add_option / parser.add_argument`
                            AST calls → CLIFlagInfo.
    (<pkg> is ``odoo`` for v10+, ``openerp`` for v8/v9.)

NOTE on optparse vs argparse: Odoo upstream historically uses optparse
(`add_option`). We accept both `add_option` and `add_argument` AST shapes for
forward compatibility — same argument shape (positional flag str + kwargs).

Static fallback: spec_data/cli_flags_<version>.json for v8-v16 when no Odoo
source is available (per ADR-0002 §4).

Version-aware paths: v8/v9 use the ``openerp/`` package prefix; v10+ use
``odoo/``. This mirrors ``_version_prefix()`` in parser_odoo_core.py and uses
the same threshold constant (``ODOO_NAMESPACE_LEGACY_MAX_MAJOR``).
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR

from .models import CLICommandInfo, CLIFlagInfo
from .parser_util import parse_external_source
from .version_registry import VersionRegistry

_CLI_OPTION_FUNCS = {"add_option", "add_argument"}
_DEPRECATED_HELP_TOKENS = ("deprecated", "obsolete")

# Version-dispatch registry for namespace-prefix selection (ADR-0032).
# v8/v9: openerp (pre-rename era).  v10+: odoo (modern namespace, open-ended).
# To add v20 with a hypothetical new namespace: append one entry here.
_PKG_PREFIX_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp"),  # v8-v9
    (10, None,                            "odoo"),      # v10+, open-ended
])


def _pkg_prefix(odoo_version: str) -> str:
    """Return the framework package prefix for *odoo_version*.

    v8/v9 shipped as ``openerp/``; v10+ renamed to ``odoo/``.
    Mirrors ``_version_prefix()`` in parser_odoo_core.py — same threshold.
    Delegates to ``_PKG_PREFIX_REGISTRY`` per ADR-0032 so v20+ is a 1-line change.
    """
    return _PKG_PREFIX_REGISTRY.resolve_version(odoo_version, default="odoo")  # type: ignore[return-value]


# --- CLI command parsing (odoo/cli/*.py) ----------------------------------

def _is_command_subclass(class_node: ast.ClassDef) -> bool:
    """True if class subclasses `Command` (by simple name match)."""
    for base in class_node.bases:
        if isinstance(base, ast.Name) and base.id == "Command":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "Command":
            return True
    return False


def _command_name_attr(class_node: ast.ClassDef) -> str | None:
    """Return the value of a class-body `name = '...'` string assignment, if any.

    Odoo CLI command classes may declare an explicit command name via a class
    attribute (`name = 'upgrade_code'`); this is the authoritative name and must
    win over the lowercased class name.

    Handles both the plain assignment (`name = 'db'`) and the annotated form
    (`name: str = 'db'`, ast.AnnAssign) — parser LOW-4, mirroring the AnnAssign
    handling in parser_python._parse_class.
    """
    for stmt in class_node.body:
        # Plain assignment: name = '...'  (one or more targets)
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (isinstance(target, ast.Name)
                        and target.id == "name"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    return stmt.value.value
        # Annotated assignment: name: str = '...'  (single target, optional value)
        elif isinstance(stmt, ast.AnnAssign):
            if (isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "name"
                    and stmt.value is not None
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)):
                return stmt.value.value
    return None


def _parse_cli_module(
    source: str, odoo_version: str, file_path: str | None,
) -> list[CLICommandInfo]:
    """Extract `class X(Command):` definitions → CLICommandInfo list."""
    try:
        # External Odoo CLI source — scope away SyntaxWarning noise, pass the real
        # path so any diagnostic is attributable (not <unknown>). See parser_util.
        tree = parse_external_source(source, filename=file_path)
    except SyntaxError:
        return []
    out: list[CLICommandInfo] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_command_subclass(node):
            continue
        # Prefer an explicit `name = '...'` class attribute (e.g.
        # `class UpgradeCode(Command): name = 'upgrade_code'`) over the
        # lowercased class name, which would mangle multi-word names into
        # `upgradecode` (osm-audit-manifest GAP-2). Fall back to the
        # lowercased class name (Odoo convention) when no attribute is set.
        cmd_name = _command_name_attr(node) or node.name.lower()
        description = ast.get_docstring(node)
        out.append(CLICommandInfo(
            name=cmd_name,
            odoo_version=odoo_version,
            description=description,
            file_path=file_path,
        ))
    return out


def _load_static_cli_commands(
    odoo_version: str, static_data_dir: str | Path | None,
) -> list[CLICommandInfo]:
    """Load the ``"commands"`` array from ``cli_flags_<version>.json``.

    Returns an empty list when the file is missing, the JSON is malformed, or
    the ``"commands"`` key is absent — callers never need to guard against these
    cases.
    """
    base = (
        Path(static_data_dir) if static_data_dir
        else Path(__file__).parent / "spec_data"
    )
    static_path = base / f"cli_flags_{odoo_version}.json"
    if not static_path.is_file():
        return []
    try:
        data = json.loads(static_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[CLICommandInfo] = []
    for c in data.get("commands", []):
        if not isinstance(c, dict) or "name" not in c:
            continue
        out.append(CLICommandInfo(
            name=c["name"],
            odoo_version=odoo_version,
            description=c.get("description"),
            file_path=c.get("file_path"),
        ))
    return out


def parse_cli_commands(
    odoo_source_root: str, odoo_version: str,
    static_data_dir: str | Path | None = None,
) -> list[CLICommandInfo]:
    """Scan <pkg>/cli/*.py + static JSON commands array → CLICommandInfo list.

    For v8/v9 the CLI lives under ``openerp/cli/``; for v10+ under ``odoo/cli/``.
    Static ``cli_flags_<version>.json`` commands are merged in so versions without
    an indexed source root still produce CLICommand nodes.
    """
    pkg = _pkg_prefix(odoo_version)
    cli_dir = Path(odoo_source_root) / pkg / "cli"

    out: list[CLICommandInfo] = []
    seen: set[str] = set()

    if cli_dir.is_dir():
        for f in sorted(cli_dir.glob("*.py")):
            if f.name in {"__init__.py", "command.py"} or f.stem.startswith("_"):
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # ADR-0037: store source-root-relative path (e.g. "odoo/cli/server.py"),
            # matching the static cli_flags_*.json convention — never absolute.
            try:
                cli_fp = str(f.relative_to(Path(odoo_source_root)))
            except ValueError:
                cli_fp = str(f)
            for cmd in _parse_cli_module(src, odoo_version, cli_fp):
                if cmd.name not in seen:
                    seen.add(cmd.name)
                    out.append(cmd)

    for cmd in _load_static_cli_commands(odoo_version, static_data_dir):
        if cmd.name not in seen:
            seen.add(cmd.name)
            out.append(cmd)

    return out


# --- CLI flag parsing (odoo/tools/config.py) -------------------------------

def _extract_kwargs_strings(call_node: ast.Call) -> dict[str, object]:
    """Collect simple string/int/bool kwargs from an add_option call."""
    out: dict[str, object] = {}
    for kw in call_node.keywords:
        if not kw.arg:
            continue
        if isinstance(kw.value, ast.Constant):
            out[kw.arg] = kw.value.value
    return out


def _is_option_call(call_node: ast.Call) -> bool:
    """True if call is `<X>.add_option(...)` or `<X>.add_argument(...)`."""
    func = call_node.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr in _CLI_OPTION_FUNCS


def _flag_name_from_args(args: list[ast.expr]) -> str | None:
    """Pick the long-form flag (--something) from positional args; fall back to first."""
    string_args = [
        a.value for a in args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    long_form = next((s for s in string_args if s.startswith("--")), None)
    if long_form:
        return long_form
    if string_args:
        return string_args[0]
    return None


def _parse_options_calls(
    source: str, odoo_version: str, command_name: str = "server",
    file_path: str | None = None,
) -> list[CLIFlagInfo]:
    """Walk source AST, extract every `<X>.add_option(...)` / `<X>.add_argument(...)`."""
    try:
        # External Odoo config source — scope away SyntaxWarning noise, pass the real
        # path so any diagnostic is attributable (not <unknown>). See parser_util.
        tree = parse_external_source(source, filename=file_path)
    except SyntaxError:
        return []

    out: list[CLIFlagInfo] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_option_call(node):
            continue
        flag_name = _flag_name_from_args(node.args)
        if not flag_name or flag_name in seen:
            continue
        seen.add(flag_name)
        kwargs = _extract_kwargs_strings(node)
        help_text = kwargs.get("help")
        # Promote to deprecated when help text mentions "deprecated".
        status = "stable"
        if isinstance(help_text, str) and any(
            tok in help_text.lower() for tok in _DEPRECATED_HELP_TOKENS
        ):
            status = "deprecated"

        # Default value lives under either `default` or `my_default` (Odoo idiom).
        default = kwargs.get("default", kwargs.get("my_default"))
        flag_type = kwargs.get("type")
        out.append(CLIFlagInfo(
            flag_name=flag_name,
            command_name=command_name,
            odoo_version=odoo_version,
            status=status,
            default=str(default) if default is not None else None,
            type=str(flag_type) if flag_type else None,
            help=help_text if isinstance(help_text, str) else None,
        ))
    return out


def _load_static_cli_flags(
    odoo_version: str, static_data_dir: str | Path | None,
) -> list[CLIFlagInfo]:
    """Load static placeholder JSON for cli flags. Returns [] when missing/empty."""
    base = (
        Path(static_data_dir) if static_data_dir
        else Path(__file__).parent / "spec_data"
    )
    static_path = base / f"cli_flags_{odoo_version}.json"
    if not static_path.is_file():
        return []
    try:
        data = json.loads(static_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[CLIFlagInfo] = []
    for f in data.get("flags", []):
        if not isinstance(f, dict) or "flag_name" not in f:
            continue
        out.append(CLIFlagInfo(
            flag_name=f["flag_name"],
            command_name=f.get("command_name") or "server",
            odoo_version=odoo_version,
            status=f.get("status", "stable"),
            default=f.get("default"),
            type=f.get("type"),
            help=f.get("help"),
            replacement_flag_name=f.get("replacement_flag_name"),
            env_name=f.get("env_name"),
            posix_only=f.get("posix_only", False),
        ))
    return out


def parse_cli_flags(
    odoo_source_root: str, odoo_version: str,
    static_data_dir: str | Path | None = None,
) -> list[CLIFlagInfo]:
    """Aggregate CLI flags: parse odoo/tools/config.py + merge static placeholders."""
    out: list[CLIFlagInfo] = []
    seen: set[tuple[str, str]] = set()  # (flag_name, command_name)

    def _add(f: CLIFlagInfo) -> None:
        key = (f.flag_name, f.command_name)
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    pkg = _pkg_prefix(odoo_version)
    config_path = Path(odoo_source_root) / pkg / "tools" / "config.py"
    if config_path.is_file():
        try:
            src = config_path.read_text(encoding="utf-8", errors="ignore")
            for f in _parse_options_calls(
                src, odoo_version, command_name="server", file_path=str(config_path),
            ):
                _add(f)
        except OSError:
            pass

    for f in _load_static_cli_flags(odoo_version, static_data_dir):
        _add(f)

    return out


# --- Cross-version diff for flags -----------------------------------------

@dataclass
class CLIFlagDiff:
    added: list[CLIFlagInfo] = field(default_factory=list)
    removed: list[CLIFlagInfo] = field(default_factory=list)
    stable: list[tuple[CLIFlagInfo, CLIFlagInfo]] = field(default_factory=list)
    replaced: list[tuple[str, str]] = field(default_factory=list)


def compute_cli_flag_diff(
    old_flags: list[CLIFlagInfo],
    new_flags: list[CLIFlagInfo],
) -> CLIFlagDiff:
    """Diff two CLIFlag lists. Pure function — no DB, no IO.

    REPLACED is set ONLY when an old flag has `replacement_flag_name` AND that
    successor is present in the new list. Replaced flags are excluded from
    the `removed` bucket (matches CoreSymbol diff_engine semantics).
    """
    by_old = {f.flag_name: f for f in old_flags}
    by_new = {f.flag_name: f for f in new_flags}

    only_old = by_old.keys() - by_new.keys()
    only_new = by_new.keys() - by_old.keys()
    common = by_old.keys() & by_new.keys()

    added = [by_new[n] for n in only_new]
    stable = [(by_old[n], by_new[n]) for n in common]

    replaced: list[tuple[str, str]] = []
    replaced_old: set[str] = set()
    for f in old_flags:
        if (
            f.replacement_flag_name
            and f.flag_name in only_old
            and f.replacement_flag_name in by_new
        ):
            replaced.append((f.flag_name, f.replacement_flag_name))
            replaced_old.add(f.flag_name)

    removed = [by_old[n] for n in only_old if n not in replaced_old]

    return CLIFlagDiff(
        added=added, removed=removed, stable=stable, replaced=replaced,
    )
