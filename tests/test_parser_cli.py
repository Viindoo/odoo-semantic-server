# tests/test_parser_cli.py
"""CLI parser tests (M4.5 WI4).

Sources:
  - odoo/cli/<command>.py — class X(Command) subclasses
  - odoo/tools/config.py — group.add_option / parser.add_option (optparse) calls

Notes:
  - Odoo uses optparse (not argparse) — `group.add_option('--longpolling-port', ...)`.
  - We accept both add_option and add_argument shapes for forward compat.
"""
from pathlib import Path

import pytest

from src.indexer.models import CLIFlagInfo
from src.indexer.parser_cli import (
    _parse_cli_module,
    _parse_options_calls,
    compute_cli_flag_diff,
    parse_cli_commands,
    parse_cli_flags,
)


def test_parse_cli_command_class_subclass_of_command():
    """`class Server(Command):` → CLICommandInfo(name='server')."""
    src = (
        "from . import Command\n"
        "class Server(Command):\n"
        "    \"\"\"Run Odoo server\"\"\"\n"
        "    def run(self, args):\n"
        "        pass\n"
    )
    cmds = _parse_cli_module(src, "17.0", "/odoo/cli/server.py")
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd.name == "server"
    assert cmd.odoo_version == "17.0"
    assert "Run Odoo" in (cmd.description or "")


def test_parse_cli_skips_non_command_classes():
    """Only Command subclasses become CLICommandInfo."""
    src = (
        "class Helper:\n"
        "    pass\n"
        "class Db(Command):\n"
        "    pass\n"
    )
    cmds = _parse_cli_module(src, "17.0", "/odoo/cli/db.py")
    names = {c.name for c in cmds}
    assert names == {"db"}


def test_parse_options_calls_extracts_flag_name_and_type():
    """parser.add_option / group.add_option → CLIFlagInfo with --long flag + type."""
    src = (
        "group.add_option('--http-port', dest='http_port', "
        "my_default=8069, type='int')\n"
        "group.add_option('--longpolling-port', dest='longpolling_port', "
        "help='Deprecated alias to the gevent-port option', type='int')\n"
        "group.add_option('--gevent-port', dest='gevent_port', "
        "my_default=8072, type='int')\n"
    )
    flags = _parse_options_calls(src, "17.0", command_name="server")
    by_name = {f.flag_name: f for f in flags}
    assert "--http-port" in by_name
    assert "--longpolling-port" in by_name
    assert "--gevent-port" in by_name
    assert by_name["--http-port"].type == "int"
    # Help text containing "deprecated" promotes the flag's status.
    assert by_name["--longpolling-port"].status == "deprecated"


def test_parse_options_calls_picks_long_form_when_short_first():
    """`group.add_option('-p', '--http-port', ...)` → flag_name='--http-port'."""
    src = "group.add_option('-p', '--http-port', dest='http_port', type='int')\n"
    flags = _parse_options_calls(src, "17.0", command_name="server")
    assert len(flags) == 1
    assert flags[0].flag_name == "--http-port"


def test_compute_cli_flag_diff_marks_removed_when_only_in_old():
    """v17 has --longpolling-port, v18 doesn't → marked removed."""
    old = [CLIFlagInfo("--longpolling-port", "server", "17.0")]
    new = [CLIFlagInfo("--gevent-port", "server", "18.0")]
    diff = compute_cli_flag_diff(old, new)
    removed_names = {f.flag_name for f in diff.removed}
    added_names = {f.flag_name for f in diff.added}
    assert "--longpolling-port" in removed_names
    assert "--gevent-port" in added_names


def test_compute_cli_flag_diff_replacement_via_replacement_flag_name():
    """Old flag with replacement_flag_name pointing to a new flag → replaced edge."""
    old = [CLIFlagInfo(
        "--longpolling-port", "server", "17.0",
        status="deprecated",
        replacement_flag_name="--gevent-port",
    )]
    new = [CLIFlagInfo("--gevent-port", "server", "18.0")]
    diff = compute_cli_flag_diff(old, new)
    assert ("--longpolling-port", "--gevent-port") in diff.replaced


def test_parse_cli_commands_returns_empty_for_nonexistent_root(tmp_path):
    """Missing source root → empty list, no exception."""
    assert parse_cli_commands(str(tmp_path / "missing"), "17.0") == []


def test_parse_cli_flags_returns_empty_for_nonexistent_root(tmp_path):
    """Missing source root → empty list, no exception."""
    assert parse_cli_flags(str(tmp_path / "missing"), "17.0") == []


@pytest.mark.skipif(
    not Path("/home/tuan/git/odoo17/odoo/cli/server.py").exists(),
    reason="Real Odoo 17 cli dir not on disk",
)
def test_parse_cli_commands_smoke_real_v17():
    """Smoke: real Odoo 17 has at least 8 well-known cli commands."""
    cmds = parse_cli_commands("/home/tuan/git/odoo17", "17.0")
    names = {c.name for c in cmds}
    # Stable subset across v17→v19
    expected_subset = {"server", "shell", "scaffold", "db", "deploy"}
    assert expected_subset <= names, f"got {names}"


@pytest.mark.skipif(
    not Path("/home/tuan/git/odoo17/odoo/tools/config.py").exists(),
    reason="Real Odoo 17 config.py not on disk",
)
def test_parse_cli_flags_smoke_real_v17_picks_up_http_port():
    """Smoke: real Odoo 17 config.py has --http-port flag."""
    flags = parse_cli_flags("/home/tuan/git/odoo17", "17.0")
    flag_names = {f.flag_name for f in flags}
    assert "--http-port" in flag_names
