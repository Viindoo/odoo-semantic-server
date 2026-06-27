# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_cli.py
"""CLI parser tests (M4.5 WI4).

Sources:
  - odoo/cli/<command>.py — class X(Command) subclasses
  - odoo/tools/config.py — group.add_option / parser.add_option (optparse) calls

Notes:
  - Odoo uses optparse (not argparse) — `group.add_option('--longpolling-port', ...)`.
  - We accept both add_option and add_argument shapes for forward compat.
"""
import json
import os
from pathlib import Path

import pytest

from src.indexer.models import CLIFlagInfo
from src.indexer.parser_cli import (
    _load_static_cli_commands,
    _load_static_cli_flags,
    _parse_cli_module,
    _parse_options_calls,
    _pkg_prefix,
    compute_cli_flag_diff,
    parse_cli_commands,
    parse_cli_flags,
)

ODOO8_SRC = os.environ.get("ODOO8_SRC", "/nonexistent/odoo8")
ODOO9_SRC = os.environ.get("ODOO9_SRC", "/nonexistent/odoo9")
ODOO17_SRC = os.environ.get("ODOO17_SRC", "/nonexistent/odoo17")


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
    """Missing source root + empty static dir → empty list, no exception.

    ``static_data_dir`` is pointed at an empty tmp dir so the real spec_data
    files are not consulted (parse_cli_commands now merges static commands).
    """
    assert parse_cli_commands(
        str(tmp_path / "missing"), "17.0", static_data_dir=str(tmp_path),
    ) == []


def test_parse_cli_flags_returns_empty_for_nonexistent_root(tmp_path):
    """Missing source root + no static file → empty list, no exception.

    Uses version "20.0" which has no static cli_flags_20.0.json on disk.
    (v17 now has a curated static file so it would return data; use a
    version not yet in the static catalogue to keep the test intent intact.)
    """
    assert parse_cli_flags(str(tmp_path / "missing"), "20.0") == []


@pytest.mark.skipif(
    not Path(ODOO17_SRC + "/odoo/cli/server.py").exists(),
    reason="Real Odoo 17 cli dir not on disk",
)
def test_parse_cli_commands_smoke_real_v17():
    """Smoke: real Odoo 17 has at least 8 well-known cli commands."""
    cmds = parse_cli_commands(ODOO17_SRC, "17.0")
    names = {c.name for c in cmds}
    # Stable subset across v17→v19
    expected_subset = {"server", "shell", "scaffold", "db", "deploy"}
    assert expected_subset <= names, f"got {names}"


@pytest.mark.skipif(
    not Path(ODOO17_SRC + "/odoo/tools/config.py").exists(),
    reason="Real Odoo 17 config.py not on disk",
)
def test_parse_cli_flags_smoke_real_v17_picks_up_http_port():
    """Smoke: real Odoo 17 config.py has --http-port flag."""
    flags = parse_cli_flags(ODOO17_SRC, "17.0")
    flag_names = {f.flag_name for f in flags}
    assert "--http-port" in flag_names


def test_load_static_cli_flags_coalesces_null_command_to_server(tmp_path):
    """Static JSON with `command_name: null` (global flag) must coalesce to "server".

    Regression: WI-A5 curated 12 spec_data files with `command_name: null` for
    global flags like --config. Neo4j MERGE rejects null property values in
    node identity keys (`Cannot merge ... null property value for 'command_name'`),
    so the loader must coerce null to the live-parser default "server" before
    handing CLIFlagInfo to the writer.
    """
    static_dir = tmp_path
    (static_dir / "cli_flags_99.0.json").write_text(json.dumps({
        "_curate_status": "complete",
        "flags": [
            {"flag_name": "--config", "command_name": None, "help": "global flag"},
            {"flag_name": "--http-port", "command_name": "server", "help": "scoped"},
            {"flag_name": "--save"},  # key missing entirely → also "server"
        ],
    }))
    out = _load_static_cli_flags("99.0", static_dir)
    by_name = {f.flag_name: f.command_name for f in out}
    assert by_name["--config"] == "server", "explicit null must coalesce"
    assert by_name["--http-port"] == "server", "explicit server preserved"
    assert by_name["--save"] == "server", "missing key default unchanged"


# --- _pkg_prefix tests -------------------------------------------------------

def test_pkg_prefix_v8_returns_openerp():
    """v8.0 → openerp (legacy namespace)."""
    assert _pkg_prefix("8.0") == "openerp"


def test_pkg_prefix_v9_returns_openerp():
    """v9.0 → openerp (legacy namespace, boundary value)."""
    assert _pkg_prefix("9.0") == "openerp"


def test_pkg_prefix_v10_returns_odoo():
    """v10.0 → odoo (first modern version)."""
    assert _pkg_prefix("10.0") == "odoo"


def test_pkg_prefix_v17_returns_odoo():
    """v17.0 → odoo (modern namespace)."""
    assert _pkg_prefix("17.0") == "odoo"


# --- _load_static_cli_commands tests ----------------------------------------

def test_load_static_cli_commands_reads_commands_array(tmp_path):
    """``"commands"`` array in static JSON → CLICommandInfo list."""
    static_dir = tmp_path
    (static_dir / "cli_flags_99.0.json").write_text(json.dumps({
        "commands": [
            {"name": "server", "description": "Start server", "file_path": "odoo/cli/server.py"},
            {"name": "scaffold", "description": "Scaffold module"},
        ],
        "flags": [],
    }))
    cmds = _load_static_cli_commands("99.0", static_dir)
    names = {c.name for c in cmds}
    assert names == {"server", "scaffold"}
    by_name = {c.name: c for c in cmds}
    assert by_name["server"].description == "Start server"
    assert by_name["server"].file_path == "odoo/cli/server.py"
    assert by_name["scaffold"].file_path is None  # absent key → None


def test_load_static_cli_commands_missing_file_returns_empty(tmp_path):
    """No static file → empty list, no exception."""
    result = _load_static_cli_commands("20.0", tmp_path)
    assert result == []


def test_load_static_cli_commands_missing_commands_key_returns_empty(tmp_path):
    """JSON without ``"commands"`` key → empty list (flags-only file)."""
    static_dir = tmp_path
    (static_dir / "cli_flags_99.0.json").write_text(json.dumps({
        "flags": [{"flag_name": "--config"}],
    }))
    assert _load_static_cli_commands("99.0", static_dir) == []


def test_load_static_cli_commands_skips_entries_without_name(tmp_path):
    """Malformed entry without ``"name"`` key is silently skipped."""
    static_dir = tmp_path
    (static_dir / "cli_flags_99.0.json").write_text(json.dumps({
        "commands": [
            {"description": "no name here"},
            {"name": "shell", "description": "Python shell"},
        ],
    }))
    cmds = _load_static_cli_commands("99.0", static_dir)
    assert len(cmds) == 1
    assert cmds[0].name == "shell"


def test_load_static_cli_commands_uses_spec_data_by_default():
    """With static_data_dir=None the real spec_data files are read.

    v8.0 has a curated ``commands`` array → at least one CLICommandInfo returned.
    """
    cmds = _load_static_cli_commands("8.0", None)
    assert len(cmds) > 0, "cli_flags_8.0.json must have at least one command entry"
    names = {c.name for c in cmds}
    assert "server" in names, f"expected 'server' in {names}"


# --- parse_cli_commands v8/v9 path-prefix tests ------------------------------

@pytest.mark.skipif(
    not Path(ODOO8_SRC + "/openerp/cli/server.py").exists(),
    reason="Real Odoo 8 cli dir not on disk",
)
def test_parse_cli_commands_smoke_real_v8():
    """Smoke: real Odoo 8 source yields CLICommand count > 0 using openerp/cli/."""
    cmds = parse_cli_commands(ODOO8_SRC, "8.0")
    assert len(cmds) > 0, f"expected >0 CLICommand nodes for v8, got {cmds}"
    names = {c.name for c in cmds}
    assert "server" in names, f"expected 'server' in {names}"


@pytest.mark.skipif(
    not Path(ODOO9_SRC + "/openerp/cli/server.py").exists(),
    reason="Real Odoo 9 cli dir not on disk",
)
def test_parse_cli_commands_smoke_real_v9():
    """Smoke: real Odoo 9 source yields CLICommand count > 0 using openerp/cli/."""
    cmds = parse_cli_commands(ODOO9_SRC, "9.0")
    assert len(cmds) > 0, f"expected >0 CLICommand nodes for v9, got {cmds}"
    names = {c.name for c in cmds}
    assert "server" in names, f"expected 'server' in {names}"


def test_parse_cli_commands_v8_static_fallback_yields_commands(tmp_path):
    """parse_cli_commands with nonexistent source + v8 spec_data → commands via static."""
    # Use real spec_data dir (has cli_flags_8.0.json with commands array).
    from pathlib import Path as P
    spec_dir = P(__file__).parent.parent / "src" / "indexer" / "spec_data"
    cmds = parse_cli_commands(str(tmp_path / "missing_v8"), "8.0", static_data_dir=str(spec_dir))
    assert len(cmds) > 0, "static fallback must yield CLICommandInfo for v8"
    names = {c.name for c in cmds}
    assert "server" in names


def test_parse_cli_commands_v9_static_fallback_yields_commands(tmp_path):
    """parse_cli_commands with nonexistent source + v9 spec_data → commands via static."""
    from pathlib import Path as P
    spec_dir = P(__file__).parent.parent / "src" / "indexer" / "spec_data"
    cmds = parse_cli_commands(str(tmp_path / "missing_v9"), "9.0", static_data_dir=str(spec_dir))
    assert len(cmds) > 0, "static fallback must yield CLICommandInfo for v9"
    names = {c.name for c in cmds}
    assert "server" in names


def test_parse_cli_commands_deduplicates_source_and_static(tmp_path):
    """Command present in both source scan and static JSON appears only once."""
    # Fake source root with openerp/cli/server.py (v8 era).
    cli_dir = tmp_path / "openerp" / "cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "server.py").write_text(
        "from openerp.cli import Command\nclass Server(Command):\n    pass\n"
    )
    # Static JSON also has "server".
    (tmp_path / "cli_flags_8.0.json").write_text(json.dumps({
        "commands": [{"name": "server", "description": "from static"}],
        "flags": [],
    }))
    cmds = parse_cli_commands(str(tmp_path), "8.0", static_data_dir=str(tmp_path))
    server_cmds = [c for c in cmds if c.name == "server"]
    assert len(server_cmds) == 1, "duplicate server entry must be collapsed"


# --- parse_cli_flags v8/v9 path-prefix tests ---------------------------------

@pytest.mark.skipif(
    not Path(ODOO8_SRC + "/openerp/tools/config.py").exists(),
    reason="Real Odoo 8 config.py not on disk",
)
def test_parse_cli_flags_smoke_real_v8_picks_up_config_flag():
    """Smoke: real Odoo 8 openerp/tools/config.py contains --config flag."""
    flags = parse_cli_flags(ODOO8_SRC, "8.0")
    flag_names = {f.flag_name for f in flags}
    assert "--config" in flag_names, f"expected --config in v8 flags, got {flag_names}"


@pytest.mark.skipif(
    not Path(ODOO9_SRC + "/openerp/tools/config.py").exists(),
    reason="Real Odoo 9 config.py not on disk",
)
def test_parse_cli_flags_smoke_real_v9_picks_up_config_flag():
    """Smoke: real Odoo 9 openerp/tools/config.py contains --config flag."""
    flags = parse_cli_flags(ODOO9_SRC, "9.0")
    flag_names = {f.flag_name for f in flags}
    assert "--config" in flag_names, f"expected --config in v9 flags, got {flag_names}"


# --- WI-G: prefer `name = '...'` class attribute over lowercased classname ---
# (osm-audit-manifest GAP-2)

def test_parse_cli_prefers_name_attribute_over_classname():
    """`class UpgradeCode(Command): name='upgrade_code'` → name is 'upgrade_code'.

    Behaviour contract: the explicit class attribute is the authoritative CLI
    command name; lowercasing the class name would mangle it to 'upgradecode'.
    """
    src = (
        "from . import Command\n"
        "class UpgradeCode(Command):\n"
        "    name = 'upgrade_code'\n"
        "    def run(self, args):\n"
        "        pass\n"
    )
    cmds = _parse_cli_module(src, "18.0", "/odoo/cli/upgrade_code.py")
    assert len(cmds) == 1
    assert cmds[0].name == "upgrade_code"


def test_parse_cli_falls_back_to_lower_classname_without_name_attr():
    """No `name =` attribute → fall back to lowercased class name (convention)."""
    src = (
        "from . import Command\n"
        "class Server(Command):\n"
        "    def run(self, args):\n"
        "        pass\n"
    )
    cmds = _parse_cli_module(src, "17.0", "/odoo/cli/server.py")
    assert len(cmds) == 1
    assert cmds[0].name == "server"
