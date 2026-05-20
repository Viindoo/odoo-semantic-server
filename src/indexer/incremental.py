# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental indexer helpers — git-diff based change detection (M6 W2-3).

Used by pipeline._index_repo() to skip unchanged repos and filter scan
results to only modules whose source actually changed since last indexed
HEAD. Foundation for re-index <30s when 1 module changes.

The strategy:
  1. Each indexer run records the current `git rev-parse HEAD` per repo
     (persisted to repos.head_sha — added W2-1).
  2. Next run compares stored sha to current sha. Equal → skip entirely.
  3. Otherwise, `git diff --name-only old..new` lists changed files;
     filter to top-level module dirs (those containing __manifest__.py
     or __openerp__.py). Re-index only those modules.
  4. Force-push detection via `git merge-base --is-ancestor` — if old
     sha is not an ancestor of current, history was rewritten →
     fall back to full reindex.

Module rename caveat: when a module dir is renamed, both old + new
paths appear in the diff → both treated as changed. Stale Module nodes
for the old path remain in Neo4j; recommend periodic --full to clean.
"""

import logging
import subprocess
from pathlib import Path

from src.constants import TIMEOUT_GIT_DIFF
from src.indexer.models import ModuleInfo

logger = logging.getLogger(__name__)


def get_repo_head(repo_path: Path) -> str | None:
    """Return current HEAD sha of repo, or None on error/empty repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_DIFF,
        )
        sha = result.stdout.strip()
        if result.returncode != 0 or not sha:
            return None
        return sha
    except (subprocess.SubprocessError, OSError):
        return None


def is_ancestor(repo_path: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    """True if ancestor_sha is a proper ancestor of descendant_sha (or equal).

    False on force-push / history rewrite where ancestor no longer exists
    in current history. Used to decide between incremental diff vs
    full reindex.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "merge-base", "--is-ancestor",
             ancestor_sha, descendant_sha],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_DIFF,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def compute_changed_module_paths(
    repo_path: Path, old_sha: str, new_sha: str
) -> set[str]:
    """Return set of top-level module dirs (relative paths) changed between
    old_sha and new_sha.

    Runs `git diff --name-only old..new` and walks each changed file path
    upward to find the closest ancestor dir containing __manifest__.py or
    __openerp__.py. That dir's path (relative to repo_path) is the module.
    Files not under any module dir are skipped.

    Returns empty set on git error.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only",
             f"{old_sha}..{new_sha}"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_DIFF * 3,
        )
        if result.returncode != 0:
            return set()
    except (subprocess.SubprocessError, OSError):
        return set()

    changed_files = [line for line in result.stdout.splitlines() if line.strip()]
    module_paths: set[str] = set()

    for file_rel in changed_files:
        file_path = Path(file_rel)
        # Walk up looking for a manifest
        current = file_path.parent
        while str(current) and str(current) != '.':
            for manifest in ("__manifest__.py", "__openerp__.py"):
                if (repo_path / current / manifest).is_file():
                    module_paths.add(str(current))
                    break
            else:
                # No manifest at this level — keep walking up
                if current == current.parent:
                    break
                current = current.parent
                continue
            break  # found a manifest, stop walking

    return module_paths


def filter_modules_by_changed(
    modules: dict[str, ModuleInfo],
    changed_module_paths: set[str],
) -> dict[str, ModuleInfo]:
    """Return subset of modules dict whose `path` field matches any
    entry in changed_module_paths.

    Comparison is on string equality of `ModuleInfo.path` (which is
    typically the relative path from repo root, e.g. "addons/sale").
    """
    return {
        name: m for name, m in modules.items()
        if m.path in changed_module_paths
    }
