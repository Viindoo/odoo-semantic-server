# tests/test_timeout_defaults.py
"""Verify that timeout/batch constants have safe defaults for v17+ heavy reindex
and that env-var overrides work correctly.

All constants are in src/constants.py. Each relevant constant is:
  - env-configurable (os.getenv with a safe default)
  - at least as large as the values needed for Odoo v17+ (605 modules, ~46k embeddings)
  - tested here to confirm env-var override takes effect at import time.

These are unit tests — no Docker, no Neo4j, no PostgreSQL required.
"""
import importlib
import os
import sys


def _read_constants_with_env(env_overrides: dict[str, str]) -> dict:
    """Return a snapshot of selected constants with the given env overrides applied.

    Temporarily patches os.environ, force-reloads src.constants, snapshots the
    relevant values, then restores the environment AND the module to its original
    state. Returns a plain dict so the caller can assert on values after the
    module has been restored.

    This avoids the trap of returning the module object itself: since reload()
    returns the same object, the `finally` restore-reload would overwrite the
    values before the caller reads them.
    """
    _names = [
        "TIMEOUT_EMBEDDER_REQUEST",
        "TIMEOUT_GIT_CLONE",
        "TIMEOUT_GIT_DIFF",
        "TIMEOUT_GIT_SCAN",
        "EMBEDDER_MAX_BATCH",
        "NEO4J_WRITE_BATCH_SIZE",
    ]
    original_env = {k: os.environ.get(k) for k in env_overrides}
    try:
        for k, v in env_overrides.items():
            os.environ[k] = v

        # Force a clean reload so the module-level int(os.getenv(...)) calls
        # are re-evaluated with the patched environment.
        if "src.constants" in sys.modules:
            mod = importlib.reload(sys.modules["src.constants"])
        else:
            import src.constants as mod  # noqa: PLC0415

        # Snapshot before restoring.
        snapshot = {name: getattr(mod, name) for name in _names if hasattr(mod, name)}
        return snapshot
    finally:
        # Restore original env (or remove if the key was absent before).
        for k, original_v in original_env.items():
            if original_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original_v
        # Reload once more to restore module-level values to their defaults.
        if "src.constants" in sys.modules:
            importlib.reload(sys.modules["src.constants"])


def _defaults() -> dict:
    """Return a snapshot of all constants at their default (no env overrides)."""
    return _read_constants_with_env({})


class TestTimeoutDefaults:
    """Verify safe defaults for v17+ heavy reindex workload."""

    def test_embedder_timeout_default_1200(self):
        """TIMEOUT_EMBEDDER_REQUEST default must be >= 1200s.

        Rationale: a 50-text batch on qwen3-embedding-q5km can exceed 90s on
        CPU-only servers when Ollama queue is busy. 1200s (20 min) gives ample
        headroom for parallel-profile indexing.
        """
        snap = _defaults()
        assert snap["TIMEOUT_EMBEDDER_REQUEST"] >= 1200, (
            f"TIMEOUT_EMBEDDER_REQUEST={snap['TIMEOUT_EMBEDDER_REQUEST']} < 1200. "
            "Raise the default — Ollama CPU batches regularly exceed 90s."
        )

    def test_git_clone_timeout_default_3600(self):
        """TIMEOUT_GIT_CLONE default must be >= 3600s (1h).

        Rationale: odoo/odoo has 1M+ commits; fresh SSH clone on a slow link
        or busy server takes 30+ min.
        """
        snap = _defaults()
        assert snap["TIMEOUT_GIT_CLONE"] >= 3600, (
            f"TIMEOUT_GIT_CLONE={snap['TIMEOUT_GIT_CLONE']} < 3600. "
            "v17+ git clone can take >30 min — 600s would always kill it."
        )

    def test_git_diff_timeout_default_raised(self):
        """TIMEOUT_GIT_DIFF default must be >= 30s (was 10s).

        Rationale: git diff on a 600-module repo with slow NFS/network disk
        can exceed 10s. 30s is safe while still failing fast on hung processes.
        """
        snap = _defaults()
        assert snap["TIMEOUT_GIT_DIFF"] >= 30, (
            f"TIMEOUT_GIT_DIFF={snap['TIMEOUT_GIT_DIFF']} < 30. "
            "Large repo diff on slow disk can exceed 10s."
        )

    def test_git_scan_timeout_default_raised(self):
        """TIMEOUT_GIT_SCAN default must be >= 30s (was 10s)."""
        snap = _defaults()
        assert snap["TIMEOUT_GIT_SCAN"] >= 30, (
            f"TIMEOUT_GIT_SCAN={snap['TIMEOUT_GIT_SCAN']} < 30."
        )


class TestEnvVarOverride:
    """Verify that env-var overrides are picked up at module reload time."""

    def test_embedder_timeout_env_override(self):
        """EMBEDDER_TIMEOUT env var must override TIMEOUT_EMBEDDER_REQUEST."""
        snap = _read_constants_with_env({"EMBEDDER_TIMEOUT": "9999"})
        assert snap["TIMEOUT_EMBEDDER_REQUEST"] == 9999, (
            f"Expected TIMEOUT_EMBEDDER_REQUEST=9999, got {snap['TIMEOUT_EMBEDDER_REQUEST']}. "
            "Env var EMBEDDER_TIMEOUT not wired up correctly."
        )

    def test_git_clone_timeout_env_override(self):
        """TIMEOUT_GIT_CLONE env var must override TIMEOUT_GIT_CLONE."""
        snap = _read_constants_with_env({"TIMEOUT_GIT_CLONE": "7200"})
        assert snap["TIMEOUT_GIT_CLONE"] == 7200, (
            f"Expected TIMEOUT_GIT_CLONE=7200, got {snap['TIMEOUT_GIT_CLONE']}. "
            "Env var TIMEOUT_GIT_CLONE not wired up correctly."
        )

    def test_git_diff_timeout_env_override(self):
        """TIMEOUT_GIT_DIFF env var must override TIMEOUT_GIT_DIFF."""
        snap = _read_constants_with_env({"TIMEOUT_GIT_DIFF": "120"})
        assert snap["TIMEOUT_GIT_DIFF"] == 120, (
            f"Expected TIMEOUT_GIT_DIFF=120, got {snap['TIMEOUT_GIT_DIFF']}. "
            "Env var TIMEOUT_GIT_DIFF not wired up correctly."
        )

    def test_git_scan_timeout_env_override(self):
        """TIMEOUT_GIT_SCAN env var must override TIMEOUT_GIT_SCAN."""
        snap = _read_constants_with_env({"TIMEOUT_GIT_SCAN": "60"})
        assert snap["TIMEOUT_GIT_SCAN"] == 60, (
            f"Expected TIMEOUT_GIT_SCAN=60, got {snap['TIMEOUT_GIT_SCAN']}. "
            "Env var TIMEOUT_GIT_SCAN not wired up correctly."
        )

    def test_embedder_max_batch_env_override(self):
        """EMBEDDER_MAX_BATCH env var must override EMBEDDER_MAX_BATCH."""
        snap = _read_constants_with_env({"EMBEDDER_MAX_BATCH": "100"})
        assert snap["EMBEDDER_MAX_BATCH"] == 100, (
            f"Expected EMBEDDER_MAX_BATCH=100, got {snap['EMBEDDER_MAX_BATCH']}. "
            "Env var EMBEDDER_MAX_BATCH not wired up correctly."
        )

    def test_neo4j_write_batch_size_env_override(self):
        """NEO4J_WRITE_BATCH_SIZE env var must override NEO4J_WRITE_BATCH_SIZE."""
        snap = _read_constants_with_env({"NEO4J_WRITE_BATCH_SIZE": "200"})
        assert snap["NEO4J_WRITE_BATCH_SIZE"] == 200, (
            f"Expected NEO4J_WRITE_BATCH_SIZE=200, got {snap['NEO4J_WRITE_BATCH_SIZE']}. "
            "Env var NEO4J_WRITE_BATCH_SIZE not wired up correctly."
        )
