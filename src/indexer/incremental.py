# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental indexer helpers - git-based change detection (ADR-0007, ADR-0056).

Used by pipeline._index_repo() to skip unchanged repos, to re-index only the
modules whose source changed since the last indexed HEAD, and to explain module
lifecycle events:

  1. Each run records `git rev-parse HEAD` per repo (repos.head_sha).
  2. The next run compares it with the current HEAD. Force-push / history
     rewrite is detected with `git merge-base --is-ancestor` (stored sha not an
     ancestor of HEAD) and falls back to a full scan.
  3. `compute_changed_module_paths` maps `git diff --name-only old..new` onto
     module directories (closest ancestor holding a manifest) for re-parse.
  4. `compute_manifest_changes` lists manifests Added / Deleted / Renamed in the
     range. It is EVIDENCE, not the retirement trigger: which modules are gone
     comes from comparing the tracked-manifest scan with the presence ledger
     (src/indexer/lifecycle.py), which also works after a force-push or on the
     first run. A rename pair here only names the successor of a retired module.

Rename detection needs blobs: on a partial/blobless clone an unreachable blob
makes `compute_manifest_changes` degrade to [] with a WARNING (full clones are
the documented precondition, ADR-0008).
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.constants import TIMEOUT_GIT_DIFF
from src.git_utils import MANIFEST_FILENAMES
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

    Comparison is on string equality of `ModuleInfo.path`.

    Path conventions (important — both sides must be absolute):
    - `ModuleInfo.path` holds the ABSOLUTE module directory path as set by
      `registry._build_module_info` (`path=str(module_dir)`), e.g.
      "/srv/clones/odoo_17.0/addons/sale".
    - `compute_changed_module_paths()` returns repo-RELATIVE paths from
      `git diff` (e.g. "addons/sale").  `pipeline_repo._index_repo` converts
      them to absolute via `str(repo_path / rel)` before passing here, so the set
      elements in `changed_module_paths` are also absolute.
    - The equality check is therefore absolute-vs-absolute and is correct;
      do NOT pass relative paths in `changed_module_paths` without that
      conversion step.
    """
    return {
        name: m for name, m in modules.items()
        if m.path in changed_module_paths
    }


@dataclass(frozen=True)
class ManifestChange:
    """One manifest-level change between two commits (repo-relative paths).

    ``status``: ``A`` (added), ``D`` (deleted) or ``R`` (renamed, i.e. the module
    directory moved as a whole - ``old_path`` and ``similarity`` are set).
    ``path`` is the manifest after the change (A/R) or the deleted manifest (D).
    """
    status: str
    path: str
    old_path: str | None = None
    similarity: int | None = None

    @property
    def name(self) -> str:
        """Module (directory) name at ``path``."""
        return Path(self.path).parent.name

    @property
    def old_name(self) -> str | None:
        """Module name at ``old_path`` (R only)."""
        return Path(self.old_path).parent.name if self.old_path else None

    @property
    def module_dir(self) -> str:
        return str(Path(self.path).parent)

    @property
    def old_module_dir(self) -> str | None:
        return str(Path(self.old_path).parent) if self.old_path else None


def _git_z(repo_path: Path, *args: str) -> list[str] | None:
    """Run a NUL-delimited git command; list of fields, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, timeout=TIMEOUT_GIT_DIFF * 3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.decode("utf-8", errors="surrogateescape")
    return [f for f in out.split("\0") if f]


def _parse_name_status(fields: list[str]) -> list[tuple[str, int | None, str, str | None]]:
    """``--name-status -z`` fields -> [(status, similarity, path, old_path)]."""
    out: list[tuple[str, int | None, str, str | None]] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        kind = code[:1]
        if kind in ("R", "C"):
            if i + 2 >= len(fields):
                break
            old, new = fields[i + 1], fields[i + 2]
            try:
                similarity = int(code[1:]) if code[1:] else None
            except ValueError:
                similarity = None
            out.append((kind, similarity, new, old))
            i += 3
        else:
            if i + 1 >= len(fields):
                break
            out.append((kind, None, fields[i + 1], None))
            i += 2
    return out


def _is_manifest(path: str | None) -> bool:
    return bool(path) and Path(path).name in MANIFEST_FILENAMES


def _rename_is_directory_majority(
    repo_path: Path, old_sha: str, new_sha: str, old_dir: str, new_dir: str,
) -> bool | None:
    """True when most files of ``old_dir`` at ``old_sha`` were renamed into
    ``new_dir`` at ``new_sha``; None on git failure.

    Manifest-only rename detection runs on tiny boilerplate files, so a heavy
    manifest rewrite degrades to D+A and near-identical sibling manifests can
    cross-pair. Requiring the directory to move as a whole keeps a successor
    pair honest (real case: tvtmaaddons17 ``0240c6b77f`` test_pylint ->
    test_viin_pylint, manifest R093, all 15 files renamed).
    """
    if old_dir in ("", ".") or new_dir in ("", "."):
        return False
    old_files = _git_z(repo_path, "ls-tree", "-r", "-z", "--name-only", old_sha, "--", old_dir)
    if old_files is None:
        return None
    if not old_files:
        return False
    moved = _git_z(
        repo_path, "diff", "--name-status", "-z", "-M",
        f"{old_sha}..{new_sha}", "--", old_dir, new_dir,
    )
    if moved is None:
        return None
    old_prefix, new_prefix = old_dir.rstrip("/") + "/", new_dir.rstrip("/") + "/"
    n_moved = sum(
        1 for kind, _sim, path, old in _parse_name_status(moved)
        if kind == "R" and old and old.startswith(old_prefix) and path.startswith(new_prefix)
    )
    return 2 * n_moved > len(old_files)


def compute_manifest_changes(
    repo_path: Path, old_sha: str, new_sha: str,
) -> list[ManifestChange]:
    """Manifests Added / Deleted / Renamed between ``old_sha`` and ``new_sha``.

    Runs ``git diff --name-status -M --diff-filter=ADR old..new`` restricted to
    ``__manifest__.py`` / ``__openerp__.py``. A rename pair is kept as ``R`` only
    when its module directory moved as a whole (``_rename_is_directory_majority``);
    otherwise it is reported as ``D`` of the old manifest plus ``A`` of the new
    one. A pair whose module name did not change (directory moved inside the
    repo) stays ``R`` - callers compare ``name`` and ``old_name``.

    Any git failure (bad sha, unreachable blob on a partial clone) is logged at
    WARNING and returns []: the caller loses successor evidence, never
    correctness. Result order: sorted by (path, status).
    """
    repo_path = Path(repo_path)
    fields = _git_z(
        repo_path, "diff", "--name-status", "-z", "-M", "--diff-filter=ADR",
        f"{old_sha}..{new_sha}", "--", *(f"*{name}" for name in MANIFEST_FILENAMES),
    )
    if fields is None:
        logger.warning(
            "compute_manifest_changes %s %s..%s: git diff failed (partial clone "
            "or unknown revision?) - no rename/delete evidence for this range",
            repo_path, old_sha[:8], new_sha[:8],
        )
        return []

    changes: list[ManifestChange] = []
    for kind, similarity, path, old_path in _parse_name_status(fields):
        if kind == "R":
            if not _is_manifest(path) and not _is_manifest(old_path):
                continue
            if not (_is_manifest(path) and _is_manifest(old_path)):
                if _is_manifest(old_path):
                    changes.append(ManifestChange("D", old_path))  # type: ignore[arg-type]
                if _is_manifest(path):
                    changes.append(ManifestChange("A", path))
                continue
            majority = _rename_is_directory_majority(
                repo_path, old_sha, new_sha,
                str(Path(old_path).parent), str(Path(path).parent),  # type: ignore[arg-type]
            )
            if majority is None:
                logger.warning(
                    "compute_manifest_changes %s %s..%s: directory check failed "
                    "for %s -> %s - no rename/delete evidence for this range",
                    repo_path, old_sha[:8], new_sha[:8], old_path, path,
                )
                return []
            if majority:
                changes.append(ManifestChange("R", path, old_path, similarity))
            else:
                changes.append(ManifestChange("D", old_path))  # type: ignore[arg-type]
                changes.append(ManifestChange("A", path))
        elif kind in ("A", "D") and _is_manifest(path):
            changes.append(ManifestChange(kind, path))
    return sorted(changes, key=lambda c: (c.path, c.status))
