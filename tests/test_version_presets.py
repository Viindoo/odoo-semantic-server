# tests/test_version_presets.py
"""Unit tests for src/indexer/version_presets.py and apply-preset CLI."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from src.db.migrate import run_migrations
from src.indexer.version_presets import list_presets, load_preset


def test_list_presets():
    presets = list_presets()
    assert "viindoo-17.0" in presets
    assert "viindoo-18.0" in presets
    assert presets == sorted(presets), "list_presets() must return sorted names"


def test_load_preset_known():
    preset = load_preset("viindoo-17.0")
    assert "profile_name" in preset
    assert "odoo_version" in preset
    assert "description" in preset
    assert "repos" in preset
    assert isinstance(preset["repos"], list)
    assert len(preset["repos"]) > 0
    for repo in preset["repos"]:
        assert "url" in repo
        assert "branch" in repo
        assert "local_path_hint" in repo


def test_load_preset_unknown_raises_keyerror():
    try:
        load_preset("nope")
        assert False, "Expected KeyError"
    except KeyError as e:
        assert "available" in str(e), f"Expected 'available' in error message, got: {e}"


def test_load_preset_returns_deep_copy():
    preset1 = load_preset("viindoo-17.0")
    # Mutate the returned dict
    preset1["profile_name"] = "mutated"
    preset1["repos"].append({"url": "injected", "branch": "x", "local_path_hint": "y"})

    # Second call must return the original data, unaffected by mutation
    preset2 = load_preset("viindoo-17.0")
    assert preset2["profile_name"] == "viindoo17", "Deep-copy guard failed: profile_name mutated"
    urls = [r["url"] for r in preset2["repos"]]
    assert "injected" not in urls, "Deep-copy guard failed: repos list mutated"


def test_apply_preset_local_path_missing_clean_error():
    """apply-preset with a nonexistent repo-base-dir must exit non-zero with clean error."""
    # Use a path that definitely doesn't exist
    with tempfile.TemporaryDirectory() as td:
        nonexistent = str(Path(td) / "no-such-subdir-osm-test")
        # nonexistent was NOT created — it's a path inside a real tmpdir but the subdir itself
        # does not exist, so derived paths under it will fail is_dir() check.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.manager",
                "apply-preset",
                "viindoo-17.0",
                "--repo-base-dir",
                nonexistent,
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0, (
        f"Expected non-zero exit, got 0. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    # Error message must name the missing path
    assert nonexistent in combined or "does not exist" in combined, (
        f"Expected missing path in error output. Got: {combined!r}"
    )
    # Error message must hint about --repo-map
    assert "--repo-map" in combined, (
        f"Expected '--repo-map' hint in error output. Got: {combined!r}"
    )


def test_apply_preset_dry_run_no_db_writes():
    """apply-preset --dry-run with valid paths must exit 0 + print summary without DB access."""
    with tempfile.TemporaryDirectory() as td:
        # Create the expected derived paths for viindoo-17.0 repos
        # Derived pattern: base_dir / f"{stem}_{branch}"
        # odoo/odoo branch 17.0 → odoo_17.0
        # Viindoo/tvtmaaddons branch 17.0 → tvtmaaddons_17.0
        base = Path(td)
        (base / "odoo_17.0").mkdir()
        (base / "tvtmaaddons_17.0").mkdir()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.manager",
                "apply-preset",
                "viindoo-17.0",
                "--repo-base-dir",
                td,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "viindoo17" in result.stdout, f"Expected 'viindoo17' in stdout: {result.stdout!r}"
    assert "python -m src.indexer" in result.stdout, (
        f"Expected indexer run hint in stdout: {result.stdout!r}"
    )


def test_apply_preset_profile_name_collision(clean_pg):
    """apply-preset must fail cleanly when profile with same name already exists.

    Regression guard: after a partial failure (profile created but repo registration
    failed), re-running apply-preset should not produce a Python traceback.
    """
    from tests.conftest import PG_TEST_DSN

    # Ensure schema exists before subprocess runs
    run_migrations(clean_pg)

    # Get PG DSN — either from os.environ (if fixture set it) or from conftest default
    pg_dsn = os.environ.get("PG_DSN", PG_TEST_DSN)

    with tempfile.TemporaryDirectory() as td:
        # Create the derived paths for viindoo-17.0
        base = Path(td)
        (base / "odoo_17.0").mkdir()
        (base / "tvtmaaddons_17.0").mkdir()

        # Subprocess env: pass PG_DSN so apply-preset can access DB
        env = os.environ.copy()
        env["PG_DSN"] = pg_dsn

        # Run apply-preset once successfully
        result1 = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.manager",
                "apply-preset",
                "viindoo-17.0",
                "--repo-base-dir",
                td,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result1.returncode == 0, (
            f"First apply-preset run failed: {result1.stdout!r} {result1.stderr!r}"
        )

        # Second run with same preset should fail (profile already created)
        result2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.manager",
                "apply-preset",
                "viindoo-17.0",
                "--repo-base-dir",
                td,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result2.returncode != 0, "Expected non-zero exit on duplicate profile name"
        combined = result2.stdout + result2.stderr
        # Error message must be clean (no traceback), mentioning the duplicate
        assert "Traceback" not in combined, (
            f"Expected clean error, got Python traceback: {combined!r}"
        )
        assert (
            "viindoo17" in combined or "already" in combined or "exists" in combined
        ), (
            f"Expected error to mention profile name or collision. Got: {combined!r}"
        )
