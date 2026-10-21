# SPDX-License-Identifier: AGPL-3.0-or-later
"""The weekly re-sync bots only ever build their PR from the default branch.

The jobs check out the repository, ``git switch -C`` a bot branch from it and
open a PR against the default branch. A ``workflow_dispatch`` from another ref
would carry that ref's unrelated commits into the bot PR, so each job must
refuse any ref but the default branch and check out the default branch
explicitly. No network, no runner: the
workflow files are parsed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = [
    REPO_ROOT / ".github" / "workflows" / "cli-flags-resync.yml",
    REPO_ROOT / ".github" / "workflows" / "module-renames-resync.yml",
]
DEFAULT_REF_GUARD = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_a_manual_run_from_another_branch_is_refused(workflow):
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
    for name, job in jobs.items():
        condition = " ".join(str(job.get("if", "")).split())
        assert DEFAULT_REF_GUARD in condition, f"{workflow.name}:{name} runs on any ref"
        assert "github.repository == 'Viindoo/odoo-semantic-server'" in condition


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_the_bot_branch_is_built_from_the_default_branch(workflow):
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
    for name, job in jobs.items():
        checkouts = [
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkouts, f"{workflow.name}:{name} has no checkout"
        for step in checkouts:
            ref = step.get("with", {}).get("ref")
            assert ref == "${{ github.event.repository.default_branch }}"
