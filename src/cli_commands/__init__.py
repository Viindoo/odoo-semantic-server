# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subcommand handlers for the admin CLI (``python -m src.cli``).

Each module here holds one ``_cmd_*`` handler plus the helpers private to that
command. Cross-command shared helpers live in :mod:`src.cli_commands._common`.
``src/cli.py`` remains the entrypoint: it builds the argparse parser, dispatches
to these handlers, and re-exports their public symbols so existing
``from src.cli import ...`` imports and ``patch("src.cli.<name>")`` test targets
keep working unchanged (no behaviour change — see B1 refactor).
"""
