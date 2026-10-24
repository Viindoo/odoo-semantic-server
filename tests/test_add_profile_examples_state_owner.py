# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every documented ``src.manager add-profile`` command must state the owner.

Since the fail-closed profile tenant change (commit 9af4ce7), ``add-profile``
refuses a profile with no owner: it needs ``--tenant ID``, ``--shared`` or a
private ``--parent NAME``. The nightly smoke workflow still ran
``add-profile odoo8 --version 8.0`` and failed at registration three nights in a
row (issues #393, #394, #395), so the v8 parser smoke never ran; the same stale
form sat in docs/deploy.md and CONTRIBUTING.md.

The scan is static: it reads every tracked workflow, Markdown, shell and
Makefile in the repository (CHANGELOG.md excluded: it records history), joins
backslash-continued lines, and checks each ``src.manager add-profile`` command.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_OWNER_FLAGS = ("--tenant", "--shared", "--parent")
_SUFFIXES = (".md", ".yml", ".yaml", ".sh")
_EXCLUDED = {"CHANGELOG.md"}
# The command runs to the end of the (joined) line or to the closing backtick
# of an inline code span.
_COMMAND = re.compile(r"src\.manager add-profile([^`\n]*)")


def _scanned_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return [
        REPO_ROOT / rel
        for rel in out
        if (rel.endswith(_SUFFIXES) or Path(rel).name == "Makefile")
        and Path(rel).name not in _EXCLUDED
        and (REPO_ROOT / rel).is_file()
    ]


def _commands(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    return [m.group(1) for m in _COMMAND.finditer(joined)]


def _states_owner(args: str) -> bool:
    try:
        tokens = shlex.split(args, comments=True)
    except ValueError:
        tokens = args.split()
    return any(t == f or t.startswith(f + "=") for t in tokens for f in _OWNER_FLAGS)


def test_documented_add_profile_commands_state_the_owner():
    offenders = []
    for path in _scanned_files():
        for args in _commands(path.read_text(encoding="utf-8", errors="replace")):
            if not _states_owner(args):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: add-profile{args.rstrip()}"
                )
    assert not offenders, (
        "add-profile needs --tenant ID, --shared or --parent NAME (fail-closed "
        "profile owner); these commands would exit 1:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_finds_the_workflow_command():
    # Guards the guard: if the extraction stops seeing the smoke workflow's
    # command, the test above passes vacuously.
    workflow = REPO_ROOT / ".github/workflows/nightly-smoke.yml"
    assert workflow in _scanned_files()
    assert _commands(workflow.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "text",
    [
        "python -m src.manager add-profile odoo8 --version 8.0\n",
        "run `python -m src.manager add-profile x --version 19.0`, then",
        "$PY -m src.manager add-profile odoo_17 \\\n    --version 17.0\n",
        "python -m src.manager add-profile x --version 17.0  # --shared later\n",
    ],
)
def test_command_without_owner_is_flagged(text):
    assert [a for a in _commands(text) if not _states_owner(a)]


@pytest.mark.parametrize(
    "text",
    [
        "python -m src.manager add-profile odoo8 --version 8.0 --shared\n",
        "python -m src.manager add-profile v18 --version 18.0 --tenant 3 \\\n"
        '    --description "Viindoo addons"\n',
        "$PY -m src.manager add-profile odoo_17 \\\n    --version 17.0 \\\n    --shared\n",
        "`python -m src.manager add-profile x --version 19.0 --parent std_19`",
        "python -m src.manager add-profile x --version 17.0 --tenant=4\n",
    ],
)
def test_command_with_owner_passes(text):
    found = _commands(text)
    assert found and all(_states_owner(a) for a in found)
