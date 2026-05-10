"""Tests for src/data/patterns.json + src/indexer/seed_patterns.py CLI."""
import json
import os
from pathlib import Path

import pytest

from src.indexer.seed_patterns import (
    _compute_patterns_sha256,
    _load_patterns,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

pytestmark = pytest.mark.neo4j

PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"


def _get_neo4j_writer_for_test() -> Neo4jWriter:
    """Helper: create Neo4jWriter from config for tests."""
    # Tests need to use NEO4J_TEST_* env vars which conftest sets up.
    uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    return Neo4jWriter(uri, user, password)


def test_patterns_json_valid_and_non_empty():
    """patterns.json parses as JSON list with ≥50 entries."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) >= 80, f"Expected ≥80 patterns, got {len(data)}"


def test_patterns_required_core_ids_present():
    """15 core pattern IDs from M4.6 plan WI4 — all must be present."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    ids = {p["pattern_id"] for p in data}
    required = {
        "computed-field-cross-model",
        "computed-field-lang-context",
        "create-multi-v17",
        "write-read-before-super",
        "xpath-avoid-replace",
        "xpath-specific-expr",
        "owl-patch-v17",
        "inherits-vs-inherit",
        "action-return-super",
        "crud-return-value",
        "old-style-super",
        "missing-return-override",
        "store-computed-field",
        "model-create-multi-batch",
        "depends-full-dotted-path",
    }
    missing = required - ids
    assert not missing, f"Missing required pattern IDs: {missing}"


def test_patterns_no_empty_snippet_or_gotchas():
    """Every pattern must have non-empty snippet_text + ≥1 gotcha."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    for p in data:
        assert p["snippet_text"].strip(), (
            f"Empty snippet for pattern_id={p['pattern_id']}"
        )
        assert len(p.get("gotchas") or []) >= 1, (
            f"No gotchas for pattern_id={p['pattern_id']}"
        )


def test_patterns_required_fields_per_entry():
    """Every entry has the canonical schema (per ADR-0003 §1)."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    required_fields = {
        "pattern_id", "intent_keywords", "file_ref", "snippet_text",
        "gotchas", "odoo_version_min", "language",
    }
    for p in data:
        missing = required_fields - p.keys()
        assert not missing, (
            f"Pattern {p.get('pattern_id', '<unknown>')} missing fields: {missing}"
        )
        assert p["language"] in ("python", "xml", "js"), (
            f"Pattern {p['pattern_id']} has invalid language={p['language']!r}"
        )
        # Slug constraint per ADR-0003 §4: pattern_id must NOT contain '__'
        assert "__" not in p["pattern_id"], (
            f"pattern_id {p['pattern_id']!r} contains '__' which collides "
            "with language__id slug encoding (per ADR-0003 §4)"
        )


def test_load_patterns_filters_by_version(tmp_path):
    """_load_patterns honours --version filter."""
    sample = [
        {
            "pattern_id": "p1",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "x",
            "gotchas": ["g1", "g2", "g3"],
            "odoo_version_min": "17.0",
            "language": "python",
        },
        {
            "pattern_id": "p2",
            "intent_keywords": ["test"],
            "file_ref": "f:2",
            "snippet_text": "y",
            "gotchas": ["g1", "g2", "g3"],
            "odoo_version_min": "18.0",
            "language": "python",
        },
    ]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(sample))

    only_17 = _load_patterns(path, version_filter="17.0")
    assert [p.pattern_id for p in only_17] == ["p1"]

    all_ver = _load_patterns(path, version_filter=None)
    assert {p.pattern_id for p in all_ver} == {"p1", "p2"}


def test_seed_cli_help_smoke():
    """`seed_patterns --help` exits 0 — argparse wiring sanity check."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "src.indexer.seed_patterns", "--help"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "patterns.json" in result.stdout

    # Cross-version uniqueness sanity: pattern_id is globally unique
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    ids = [p["pattern_id"] for p in data]
    assert len(ids) == len(set(ids)), (
        f"Duplicate pattern_id in patterns.json: "
        f"{[i for i in ids if ids.count(i) > 1]}"
    )


@pytest.mark.parametrize("language", ["python", "xml", "js"])
def test_patterns_each_language_has_entries(language):
    """Every supported language has at least one entry — coverage smoke."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    matches = [p for p in data if p["language"] == language]
    assert matches, f"No patterns for language={language!r}"


# ============================================================================
# SHA256 Hash-Gating Tests (M6 W2-6)
# ============================================================================

def test_compute_patterns_sha256():
    """_compute_patterns_sha256 returns consistent hex digest."""
    sha1 = _compute_patterns_sha256(PATTERNS_PATH)
    sha2 = _compute_patterns_sha256(PATTERNS_PATH)
    assert sha1 == sha2
    assert len(sha1) == 64  # SHA256 hex is 64 chars


def test_first_seed_writes_sentinel_sha(clean_neo4j, tmp_path, monkeypatch):
    """After first seed, _SeedMeta sentinel exists with sha256."""
    from src.indexer.seed_patterns import main

    # Set Neo4j config from test environment
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)
    monkeypatch.setenv("NEO4J_URI", neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", neo4j_user)

    # Create a minimal patterns.json for testing
    sample = [
        {
            "pattern_id": "test-pat",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "test snippet",
            "gotchas": ["gotcha"],
            "odoo_version_min": "99.0",
            "language": "python",
        },
    ]
    patterns_file = tmp_path / "patterns.json"
    patterns_file.write_text(json.dumps(sample))

    # Run seed once
    argv = ["--patterns-file", str(patterns_file), "--no-embed"]
    result = main(argv)
    assert result == 0

    # Check sentinel exists
    writer = _get_neo4j_writer_for_test()
    try:
        with writer.driver.session() as session:
            result = session.run(
                "MATCH (s:_SeedMeta {key: 'patterns'}) RETURN s.sha256 AS sha LIMIT 1"
            ).single()
            assert result is not None, "_SeedMeta sentinel not found"
            stored_sha = result["sha"]
            expected_sha = _compute_patterns_sha256(patterns_file)
            assert stored_sha == expected_sha
    finally:
        writer.close()


def test_second_seed_no_change_skips(clean_neo4j, tmp_path, caplog, monkeypatch):
    """Second seed with unchanged patterns.json skips reseed."""
    from src.indexer.seed_patterns import main

    # Set Neo4j config from test environment
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)
    monkeypatch.setenv("NEO4J_URI", neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", neo4j_user)

    sample = [
        {
            "pattern_id": "test-pat",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "test snippet",
            "gotchas": ["gotcha"],
            "odoo_version_min": "99.0",
            "language": "python",
        },
    ]
    patterns_file = tmp_path / "patterns.json"
    patterns_file.write_text(json.dumps(sample))

    # First seed
    argv = ["--patterns-file", str(patterns_file), "--no-embed"]
    result = main(argv)
    assert result == 0

    # Second seed — should skip
    caplog.clear()
    with caplog.at_level("INFO"):
        result = main(argv)
    assert result == 0
    assert "skipping reseed" in caplog.text


def test_second_seed_after_modification_reseeds(clean_neo4j, tmp_path, monkeypatch):
    """After modifying patterns.json, second seed proceeds."""
    from src.indexer.seed_patterns import main

    # Set Neo4j config from test environment
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)
    monkeypatch.setenv("NEO4J_URI", neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", neo4j_user)

    sample = [
        {
            "pattern_id": "test-pat",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "test snippet",
            "gotchas": ["gotcha"],
            "odoo_version_min": "99.0",
            "language": "python",
        },
    ]
    patterns_file = tmp_path / "patterns.json"
    patterns_file.write_text(json.dumps(sample))

    # First seed
    argv = ["--patterns-file", str(patterns_file), "--no-embed"]
    result = main(argv)
    assert result == 0
    sha_before = _compute_patterns_sha256(patterns_file)

    # Modify patterns.json
    sample[0]["snippet_text"] = "modified snippet"
    patterns_file.write_text(json.dumps(sample))
    sha_after = _compute_patterns_sha256(patterns_file)
    assert sha_before != sha_after

    # Second seed — should proceed
    result = main(argv)
    assert result == 0


def test_force_bypasses_sentinel(clean_neo4j, tmp_path, caplog, monkeypatch):
    """--force flag bypasses sentinel, forces reseed."""
    from src.indexer.seed_patterns import main

    # Set Neo4j config from test environment
    neo4j_uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
    neo4j_user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("NEO4J_PASSWORD", neo4j_password)
    monkeypatch.setenv("NEO4J_URI", neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", neo4j_user)

    sample = [
        {
            "pattern_id": "test-pat",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "test snippet",
            "gotchas": ["gotcha"],
            "odoo_version_min": "99.0",
            "language": "python",
        },
    ]
    patterns_file = tmp_path / "patterns.json"
    patterns_file.write_text(json.dumps(sample))

    # First seed
    argv = ["--patterns-file", str(patterns_file), "--no-embed"]
    result = main(argv)
    assert result == 0

    # Second seed with --force should not log "skipping reseed"
    caplog.clear()
    argv_force = ["--patterns-file", str(patterns_file), "--no-embed", "--force"]
    with caplog.at_level("INFO"):
        result = main(argv_force)
    assert result == 0
    assert "skipping reseed" not in caplog.text
