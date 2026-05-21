# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Env versions sync guard — unit tests (no Docker needed).

Asserts that the image version strings declared as source-of-truth in
.env.example are present verbatim in .github/workflows/nightly-smoke.yml.

GitHub Actions service containers are started before any step can run, so
they cannot read .env.example at parse time and must hardcode image versions.
This test fails CI if someone bumps .env.example without updating the workflow.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly-smoke.yml"


def _parse_image(var_name: str) -> str:
    """Extract the value of VAR_NAME=... from .env.example."""
    content = ENV_EXAMPLE.read_text()
    match = re.search(rf"^{re.escape(var_name)}=(.+)$", content, re.MULTILINE)
    assert match, f"{var_name} not found in .env.example"
    return match.group(1).strip()


def test_neo4j_image_synced():
    """NEO4J_IMAGE in .env.example must appear verbatim in nightly-smoke.yml."""
    image = _parse_image("NEO4J_IMAGE")
    workflow_text = WORKFLOW.read_text()
    assert f"image: {image}" in workflow_text, (
        f"NEO4J_IMAGE '{image}' from .env.example not found in {WORKFLOW.name}.\n"
        "When bumping NEO4J_IMAGE, update .github/workflows/nightly-smoke.yml too."
    )


def test_pg_image_synced():
    """PG_IMAGE in .env.example must appear verbatim in nightly-smoke.yml."""
    image = _parse_image("PG_IMAGE")
    workflow_text = WORKFLOW.read_text()
    assert f"image: {image}" in workflow_text, (
        f"PG_IMAGE '{image}' from .env.example not found in {WORKFLOW.name}.\n"
        "When bumping PG_IMAGE, update .github/workflows/nightly-smoke.yml too."
    )


def test_both_images_present_in_workflow():
    """Both NEO4J_IMAGE and PG_IMAGE must be declared in .env.example."""
    content = ENV_EXAMPLE.read_text()
    assert re.search(r"^NEO4J_IMAGE=\S+", content, re.MULTILINE), (
        "NEO4J_IMAGE missing from .env.example — it is the source of truth for Neo4j image version."
    )
    assert re.search(r"^PG_IMAGE=\S+", content, re.MULTILINE), (
        "PG_IMAGE missing from .env.example — "
        "it is the source of truth for PostgreSQL image version."
    )
