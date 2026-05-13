"""
Nightly-smoke env-var guard — unit tests (no Docker needed).

Asserts that .github/workflows/nightly-smoke.yml uses production env-var
names (NEO4J_URI/USER/PASSWORD) — NOT the test-fixture names
(NEO4J_TEST_*).

Why: src/indexer/pipeline.py:_neo4j_creds() deliberately reads only the
NEO4J_* names. The NEO4J_TEST_* names exist solely as a pytest-side
bridge in tests/conftest.py and never flow into production code paths.
Nightly-smoke jobs invoke the indexer CLI directly (not via pytest), so
the conftest bridge is inactive — the workflow MUST set NEO4J_* directly
or _neo4j_creds() raises RuntimeError("Neo4j password missing").

This file is the regression guard for the 2026-05-12 nightly failure
(run 25760219927) where every indexer step crashed before writing data.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly-smoke.yml"


def test_no_neo4j_test_env_vars_in_nightly_smoke():
    """Nightly-smoke MUST NOT use NEO4J_TEST_* env names.

    The CLI path (`python -m src.indexer ...`) reads NEO4J_PASSWORD via
    pipeline._neo4j_creds(); NEO4J_TEST_PASSWORD is silently ignored,
    leading to RuntimeError and an empty Neo4j on the verify step.
    """
    text = WORKFLOW.read_text()
    for var in ("NEO4J_TEST_URI", "NEO4J_TEST_USER", "NEO4J_TEST_PASSWORD"):
        assert var not in text, (
            f"{var} found in {WORKFLOW.name}. Use {var.replace('_TEST', '')} "
            "instead — nightly-smoke runs the production CLI, not pytest, so "
            "tests/conftest.py NEO4J_TEST_* → NEO4J_* bridge is inactive. "
            "See src/indexer/pipeline.py:_neo4j_creds() docstring."
        )


def test_indexer_steps_set_neo4j_password():
    """Every step invoking `python -m src.indexer` must set NEO4J_PASSWORD.

    pipeline._neo4j_creds() raises RuntimeError if NEO4J_PASSWORD is
    missing — there is no fallback. The verify step that follows would
    then see an empty Neo4j and fail with a confusing assertion error,
    masking the real RuntimeError. This guard catches the wiring bug at
    PR time, not at 02:00 Vietnam time.
    """
    text = WORKFLOW.read_text()
    indexer_invocations = len(re.findall(r"python -m src\.indexer", text))
    password_settings = len(re.findall(r"^\s+NEO4J_PASSWORD:\s*\S", text, re.MULTILINE))
    assert indexer_invocations > 0, (
        f"Expected at least one `python -m src.indexer` invocation in "
        f"{WORKFLOW.name}; found none — has the workflow been gutted?"
    )
    assert password_settings >= indexer_invocations, (
        f"{WORKFLOW.name}: found {indexer_invocations} indexer CLI "
        f"invocation(s) but only {password_settings} `NEO4J_PASSWORD:` "
        "env entries. Each CLI step must set NEO4J_PASSWORD in its `env:` "
        "block. See src/indexer/pipeline.py:_neo4j_creds()."
    )
