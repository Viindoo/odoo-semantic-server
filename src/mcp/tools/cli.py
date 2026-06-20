"""CLI spec MCP tool (split out of src/mcp/tools/spec.py, issue #336).

One ``@offload_neo4j`` tool and its implementation helpers:
  - ``cli_help`` — odoo-bin CLICommand / CLIFlag spec lookup.

The ``cli_help`` tool was previously co-located in ``spec.py`` alongside the
other spec tools (lookup_core_api / api_version_diff / find_deprecated_usage /
lint_check).  The sub-command listing logic added for v19 (13 subparser
sub-actions: db/i18n/module) pushed spec.py over the TOOL_MODULE_MAX_LINES
ceiling, so the CLI tool cluster was extracted into its own module.

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module just after spec.py so the decorator runs.

The implementation helper ``_cli_help`` and its formatters reach the shared
resolver/state hub through the module-level ``_srv`` server reference bound at
the END of this file (see the note there) and ``_srv.<name>`` attribute lookups
performed at call time.
"""

import sys

from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)


def _format_cli_flag_detail(rec: dict, replacement: str | None, version: str) -> str:
    """Format a single CLIFlag detail."""
    flag = rec.get("flag_name") or "?"
    cmd = rec.get("command_name") or "?"
    status = rec.get("status") or "stable"
    typ = rec.get("type")
    default = rec.get("default")
    help_text = rec.get("help")
    lines = [f"cli_help({cmd!r}, {flag!r}, Odoo {version})"]
    lines.append(f"├─ Status:      {status}")
    if typ:
        lines.append(f"├─ Type:        {typ}")
    if default is not None:
        lines.append(f"├─ Default:     {default}")
    if help_text:
        lines.append(f"├─ Help:        {help_text}")
    if replacement:
        lines.append(f"└─ Replacement: {replacement}")
    else:
        lines[-1] = lines[-1].replace("├─", "└─")
    return "\n".join(lines)


def _format_cli_command_summary(
    cmd_rec: dict, flags: list[dict], version: str,
    sub_cmds: list[str] | None = None,
) -> str:
    # Convention: compound command_name uses space as separator, matching the
    # actual CLI syntax ("odoo-bin i18n export", "odoo-bin db init").
    # Subparser parents (db/i18n/module) store shared flags under the parent
    # command_name; each sub-action's own flags use the compound name.
    name = cmd_rec.get("name") or "?"
    desc = cmd_rec.get("description")
    lines = [f"cli_help({name!r}, Odoo {version})"]
    if desc:
        lines.append(f"├─ Description: {desc}")

    has_sub_cmds = bool(sub_cmds)
    has_flags = bool(flags)

    if not has_flags and not has_sub_cmds:
        lines.append("└─ no flags indexed")
        return "\n".join(lines)

    if has_sub_cmds:
        # ADR-0023 §1.3: sub-commands branch before flags branch.
        sub_connector = "├─" if has_flags else "└─"
        lines.append(f"{sub_connector} Sub-commands ({len(sub_cmds)}):")
        last_sub_idx = len(sub_cmds) - 1
        for i, sc in enumerate(sub_cmds):
            sub_name_connector = "└─" if i == last_sub_idx else "├─"
            lines.append(f"    {sub_name_connector} {sc}")
        lines.append(
            f"    Tip: use cli_help('{name} <sub-command>', ...) for sub-command flags"
        )

    if has_flags:
        # ADR-0023 §1.3: Flags is the last branch → sublist indent is 4 spaces.
        lines.append(f"└─ Flags ({len(flags)}):")
        last_idx = len(flags) - 1
        for i, f in enumerate(flags):
            connector = "└─" if i == last_idx else "├─"
            flag = f.get("flag_name") or "?"
            status = f.get("status") or "stable"
            suffix = f" (status={status})" if status != "stable" else ""
            lines.append(f"    {connector} {flag}{suffix}")
    return "\n".join(lines)


def _cli_help(
    command: str | None,
    flag: str | None = None,
    odoo_version: str = "auto",
) -> str:
    """Return CLICommand spec or CLIFlag status + replacement."""
    with _srv._get_driver().session() as session:
        odoo_version = _srv._resolve_version(odoo_version, session)

        # Query SpecMetadata curation status for CLI at this version.
        curate_rec = _srv._single_bounded(
            session,
            """
            MATCH (sm:SpecMetadata {kind: 'cli', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
            """,
            label=f"CLI curation status (Odoo {odoo_version})",
            v=odoo_version,
        )
        curate_status = curate_rec["curate_status"] if curate_rec else None

        if command and flag:
            rec = _srv._single_bounded(
                session,
                """
                MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd, odoo_version: $v})
                OPTIONAL MATCH (f)-[:REPLACED_BY]->(repl:CLIFlag)
                RETURN f.flag_name AS flag_name,
                       f.command_name AS command_name,
                       f.status AS status,
                       f.type AS type,
                       f.default AS default,
                       f.help AS help,
                       repl.flag_name AS replacement
                """,
                label=f"CLI flag {flag!r} of {command!r} (Odoo {odoo_version})",
                flag=flag, cmd=command, v=odoo_version,
            )
            if rec is None:
                result = (
                    f"cli_help({command!r}, {flag!r}, Odoo {odoo_version})\n"
                    f"└─ flag {flag!r} not found on command {command!r}"
                )
            else:
                data = dict(rec)
                replacement = data.pop("replacement", None)
                # Fallback: replacement_flag_name property when no REPLACED_BY edge.
                if not replacement:
                    fallback = _srv._single_bounded(
                        session,
                        """
                        MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd,
                                          odoo_version: $v})
                        RETURN f.replacement_flag_name AS r
                        """,
                        label=f"CLI flag replacement for {flag!r} (Odoo {odoo_version})",
                        flag=flag, cmd=command, v=odoo_version,
                    )
                    replacement = fallback["r"] if fallback else None
                result = _format_cli_flag_detail(data, replacement, odoo_version)
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        if command:
            cmd_rec = _srv._single_bounded(
                session,
                """
                MATCH (c:CLICommand {name: $cmd, odoo_version: $v})
                RETURN c.name AS name, c.description AS description
                """,
                label=f"CLI command {command!r} (Odoo {odoo_version})",
                cmd=command, v=odoo_version,
            )
            if cmd_rec is None:
                result = (
                    f"cli_help({command!r}, Odoo {odoo_version})\n"
                    f"└─ command {command!r} not found"
                )
            else:
                flags = _srv._data_bounded(
                    session,
                    """
                    MATCH (f:CLIFlag {command_name: $cmd, odoo_version: $v})
                    RETURN f.flag_name AS flag_name, f.status AS status
                    ORDER BY f.flag_name
                    """,
                    label=f"CLI flags of {command!r} (Odoo {odoo_version})",
                    cmd=command, v=odoo_version,
                )
                # Also query for sub-commands (compound names with a space prefix).
                # This handles subparser commands (db/i18n/module) that have
                # sub-actions stored as CLICommand nodes with compound names
                # like "db init", "i18n export" (space-separated, matching CLI syntax).
                sub_cmd_recs = _srv._data_bounded(
                    session,
                    """
                    MATCH (c:CLICommand {odoo_version: $v})
                    WHERE c.name STARTS WITH ($cmd + ' ')
                    RETURN c.name AS name
                    ORDER BY c.name
                    """,
                    label=f"CLI sub-commands of {command!r} (Odoo {odoo_version})",
                    cmd=command, v=odoo_version,
                )
                sub_cmds = [r["name"] for r in sub_cmd_recs] if sub_cmd_recs else []
                result = _format_cli_command_summary(
                    dict(cmd_rec), flags, odoo_version, sub_cmds=sub_cmds
                )
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        # No command — list all CLI commands at this version.
        cmds = _srv._data_bounded(
            session,
            """
            MATCH (c:CLICommand {odoo_version: $v})
            RETURN c.name AS name
            ORDER BY c.name
            """,
            label=f"CLI command list (Odoo {odoo_version})",
            v=odoo_version,
        )
    if not cmds:
        result = (
            f"cli_help(Odoo {odoo_version})\n"
            f"└─ no CLI commands indexed for this version"
        )
        if curate_status == "pending":
            result = (
                f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                + result
            )
        return result
    lines = [f"cli_help(Odoo {odoo_version}) — {len(cmds)} commands"]
    last_idx = len(cmds) - 1
    for i, c in enumerate(cmds):
        connector = "└─" if i == last_idx else "├─"
        lines.append(f"{connector} {c['name']}")
    result = "\n".join(lines)
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation — limited results.\n" + result
        )
    return result


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def cli_help(
    command: str | None = None,
    flag: str | None = None,
    *,
    odoo_version: RequiredOdooVersion,
) -> str:
    """Look up odoo-bin subcommand or flag: status, help text, replacement.

    TRIGGER when: "how to run odoo-bin scaffold", "what CLI options does
    odoo-bin have", "odoo-bin command for database update", "cách dùng
    odoo-bin shell", "tham số nào để cài module mới", "is --longpolling-port
    still valid in Odoo 18"
    PREFER over: reading Odoo docs — returns version-specific CLI info from
    indexed CLICommand catalogue, including deprecated flag replacements
    SKIP when: user wants API reference → use lookup_core_api; user wants to
    check module existence → use check_module_exists

    Args:
        command: Subcommand name (e.g. 'server', 'shell', 'scaffold').
            If None, lists all known commands at this version.
        flag: Optional flag (e.g. '--http-port'). With command, returns full
            flag details including replacement when deprecated.

    Returns:
        Tree text: flag status, type, default, help text, replacement.

    Example:
        cli_help("server", "--longpolling-port", odoo_version="18.0")
        → cli_help('server', '--longpolling-port', Odoo 18.0)
          ├─ Status:      removed
          ├─ Help:        Deprecated alias to the gevent-port option
          └─ Replacement: --gevent-port
    """
    return _cli_help(command, flag, odoo_version)


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. That restores the
# pre-refactor bare-name behaviour: the impl bodies read the hub through
# `_srv.<name>` at call time so monkeypatch.setattr(srv, "_get_driver", ...) and
# friends still take effect, and the test_mcp_spec_tools `spec_tools` fixture
# (pop + re-import) sees the impls re-exported on the fresh generation.
_srv = sys.modules["src.mcp.server"]
