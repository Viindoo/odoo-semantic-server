# SPDX-License-Identifier: AGPL-3.0-or-later
"""cli_help renders one tree line per field, whatever the curated text holds.

A command description is the command class docstring and a flag help is the
source help text; both can span several lines (e.g. the v12-v15 ``cloc``
docstring). Every line of the answer must still belong to the tree.
"""
from src.mcp.tools.cli import _format_cli_command_summary, _format_cli_flag_detail

_TREE_PREFIXES = ("cli_help(", "├─", "└─", "│", "    ")


def test_multi_line_command_description_stays_on_one_tree_line():
    out = _format_cli_command_summary(
        {"name": "cloc",
         "description": "Count lines of code per module.\n\n    Usage: cloc -p PATH"},
        [{"flag_name": "--path", "status": "stable"}],
        "13.0",
    )
    assert all(line.startswith(_TREE_PREFIXES) for line in out.splitlines()), out
    assert "├─ Description: Count lines of code per module. Usage: cloc -p PATH" in out


def test_multi_line_flag_help_stays_on_one_tree_line():
    out = _format_cli_flag_detail(
        {"flag_name": "--path", "command_name": "cloc",
         "help": "Directory.\n   Can be repeated."},
        None, "13.0",
    )
    assert all(line.startswith(_TREE_PREFIXES) for line in out.splitlines()), out
    assert "Help:        Directory. Can be repeated." in out
