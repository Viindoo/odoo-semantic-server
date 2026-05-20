# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/scanner.py
import re
import subprocess
from pathlib import Path

from src.constants import TIMEOUT_GIT_SCAN


def get_git_branch(repo_path: str) -> str | None:
    """Return the current git branch of a repo path. Returns None if not a git repo."""
    try:
        # Use symbolic-ref to handle unborn branches (newly created repos)
        result = subprocess.run(
            ["git", "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        return branch if branch else None
    except Exception:
        return None


def get_module_commit_sha(repo_path: Path, module_relpath: Path) -> str | None:
    """Get HEAD commit sha that last touched the module.

    Runs `git -C <repo_path> log -1 --format=%H -- <module_relpath>`.
    Returns None on any failure (no commits, path outside repo, git error).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", "-1", "--format=%H", "--", str(module_relpath)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_GIT_SCAN,
        )
        sha = result.stdout.strip()
        if result.returncode != 0 or not sha:
            return None
        return sha
    except (subprocess.SubprocessError, OSError):
        return None


def is_odoo_version_branch(branch: str) -> bool:
    """Return True if branch matches Odoo version format (e.g. 17.0, 8.0, 16.0)."""
    if not branch:
        return False
    return bool(re.match(r'^\d+\.\d+$', branch))


def scan_repos(base_dirs: list[str]) -> list[tuple[str, str]]:
    """
    Scan base_dirs for git repos on an Odoo version branch.
    Return list of (repo_path, odoo_version) tuples.
    """
    results = []
    for base_dir in base_dirs:
        base_path = Path(base_dir)
        if not base_path.exists():
            continue

        # Check if base_dir itself is a git repo with Odoo version branch
        branch = get_git_branch(str(base_path))
        if branch and is_odoo_version_branch(branch):
            results.append((str(base_path), branch))
            continue

        # Check subdirectories
        for subdir in base_path.iterdir():
            if not subdir.is_dir():
                continue
            branch = get_git_branch(str(subdir))
            if branch and is_odoo_version_branch(branch):
                results.append((str(subdir), branch))

    return results
