# SPDX-License-Identifier: AGPL-3.0-or-later
"""N:1 merges name their successor from git alone (#380 item 3) - no DB.

Real case: tvtmaaddons 17.0 merged viin_ai_rag, viin_ai_agent, viin_ai_skill,
viin_ai_search and viin_ai_memory into the pre-existing viin_ai (commits
"[MERGE] viin_ai_<x>: merge into viin_ai"). viin_ai's manifest was only
modified, so the A/R manifest list never offered it and the ledger recorded no
successor. Rules protected (synthetic git repos in ``tmp_path``):

* the removing commit moving at least half of the old module's files into ONE
  pre-existing module directory is a ``git_merge`` successor - 50% exactly
  qualifies (viin_ai_memory: 17 of 34), 49% does not;
* the target's manifest may be modified or not touched at all;
* the share is per ONE target: a split names only a directory that got half,
  none when no directory did (40% + 40%);
* the commit subject is never evidence: "[MERGE] x: merge into y" with the
  files deleted (not moved) gives no successor;
* a target the caller does not observe is not named;
* a rename to a module the commit creates stays ``git_rename``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.indexer.lifecycle import (
    SUCCESSOR_GIT_MERGE,
    SUCCESSOR_GIT_RENAME,
    merge_subject_target,
    merged_into,
    removal_evidence,
)
from tests._lifecycle_repo import run_git


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q")
    run_git(path, "config", "user.email", "d@e.x")
    run_git(path, "config", "user.name", "D")
    return path


def _commit(path: Path, msg: str) -> str:
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "--allow-empty", "-m", msg)
    return run_git(path, "rev-parse", "HEAD")


def _module(repo: Path, name: str, n_files: int) -> None:
    """A module with a manifest and *n_files* distinct python files (manifest
    included in the count of the directory)."""
    mod = repo / name
    (mod / "models").mkdir(parents=True)
    (mod / "__manifest__.py").write_text(repr({"name": name, "depends": ["base"]}) + "\n")
    for i in range(n_files - 1):
        # Distinct, non-trivial content so -M pairs each file with itself.
        (mod / "models" / f"part_{i:03d}.py").write_text(
            f"# {name} part {i}\n" + "".join(
                f"def {name}_f{i}_{j}(x):\n    return x * {i} + {j}\n" for j in range(6)
            )
        )


def _move(repo: Path, old: str, new_dir: str, files: list[Path]) -> None:
    dest = repo / new_dir / "models"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = dest / f"{old}_{f.name}"
        run_git(repo, "mv", str(f.relative_to(repo)), str(target.relative_to(repo)))


def _merge(repo: Path, old: str, into: list[tuple[str, int]], *, touch_target_manifest: bool,
           subject: str | None = None) -> str:
    """Move the first k files of each (target, k) of *old*'s models into target,
    delete the rest of *old*, commit."""
    files = sorted((repo / old / "models").iterdir())
    start = 0
    for target, k in into:
        _move(repo, old, target, files[start:start + k])
        if touch_target_manifest:
            m = repo / target / "__manifest__.py"
            m.write_text(m.read_text() + f"# absorbed {old}\n")
        start += k
    run_git(repo, "rm", "-r", "-q", old)
    return _commit(repo, subject or f"[MERGE] {old}: merge into {into[0][0]}")


def _evidence(repo: Path, old: str, observed=("viin_ai", "viin_ai_extra", "new_mod")):
    return removal_evidence(str(repo), old, ["__manifest__.py"], set(observed))


@pytest.fixture
def repo(tmp_path):
    r = _repo(tmp_path / "tvtmaaddons")
    _module(r, "viin_ai", 10)
    _module(r, "viin_ai_extra", 10)
    return r


# ---------------------------------------------------------------------------
# The 50% boundary (owner decision: inclusive)
# ---------------------------------------------------------------------------

def test_exactly_half_of_the_files_moved_into_an_existing_module_is_a_merge(repo):
    """viin_ai_memory shape: 17 of 34 files git-renamed into viin_ai/."""
    _module(repo, "viin_ai_memory", 34)
    _commit(repo, "add memory")
    _merge(repo, "viin_ai_memory", [("viin_ai", 17)], touch_target_manifest=True)
    ev, succ = _evidence(repo, "viin_ai_memory")
    assert ev is not None and ev.subject == "[MERGE] viin_ai_memory: merge into viin_ai"
    assert succ is not None
    assert (succ.names, succ.source) == (("viin_ai",), SUCCESSOR_GIT_MERGE)


def test_49_percent_moved_is_not_a_merge(repo):
    _module(repo, "viin_ai_part", 100)
    _commit(repo, "add part")
    _merge(repo, "viin_ai_part", [("viin_ai", 49)], touch_target_manifest=True)
    ev, succ = _evidence(repo, "viin_ai_part")
    assert ev is not None, "the removing commit is still recorded"
    assert succ is None


def test_50_percent_of_100_files_is_a_merge(repo):
    _module(repo, "viin_ai_part", 100)
    _commit(repo, "add part")
    _merge(repo, "viin_ai_part", [("viin_ai", 50)], touch_target_manifest=True)
    _ev, succ = _evidence(repo, "viin_ai_part")
    assert succ is not None and succ.names == ("viin_ai",)
    assert succ.source == SUCCESSOR_GIT_MERGE


# ---------------------------------------------------------------------------
# Minimum moved files: half of a tiny module is no merge
# ---------------------------------------------------------------------------

def test_two_moved_files_are_too_few_for_a_merge_even_at_half(repo):
    """4 files (manifest + 3), 2 moved = 50% but below MERGE_MIN_MOVED_FILES."""
    _module(repo, "viin_ai_tiny", 4)
    _commit(repo, "add tiny")
    _merge(repo, "viin_ai_tiny", [("viin_ai", 2)], touch_target_manifest=True)
    ev, succ = _evidence(repo, "viin_ai_tiny")
    assert ev is not None, "the removing commit is still recorded"
    assert succ is None


def test_three_moved_files_at_half_are_a_merge(repo):
    """6 files, 3 moved = 50% and exactly MERGE_MIN_MOVED_FILES."""
    _module(repo, "viin_ai_small", 6)
    _commit(repo, "add small")
    _merge(repo, "viin_ai_small", [("viin_ai", 3)], touch_target_manifest=True)
    _ev, succ = _evidence(repo, "viin_ai_small")
    assert succ is not None
    assert (succ.names, succ.source) == (("viin_ai",), SUCCESSOR_GIT_MERGE)


def test_share_convention_merge_inclusive_rename_strict():
    from src.indexer.lifecycle import _share_reached

    assert _share_reached(17, 34, inclusive=True)
    assert not _share_reached(17, 34, inclusive=False)
    assert _share_reached(18, 34, inclusive=False)
    assert not _share_reached(16, 34, inclusive=True)


# ---------------------------------------------------------------------------
# Target manifest status does not matter
# ---------------------------------------------------------------------------

def test_target_whose_manifest_is_only_modified_is_found(repo):
    """The #380 root cause: compute_manifest_changes (A/D/R only) never lists viin_ai."""
    _module(repo, "viin_ai_rag", 38)
    _commit(repo, "add rag")
    _merge(repo, "viin_ai_rag", [("viin_ai", 29)], touch_target_manifest=True)
    _ev, succ = _evidence(repo, "viin_ai_rag")
    assert succ is not None
    assert (succ.names, succ.source) == (("viin_ai",), SUCCESSOR_GIT_MERGE)


def test_target_whose_manifest_is_not_touched_at_all_is_found(repo):
    _module(repo, "viin_ai_search", 50)
    _commit(repo, "add search")
    _merge(repo, "viin_ai_search", [("viin_ai", 40)], touch_target_manifest=False)
    _ev, succ = _evidence(repo, "viin_ai_search")
    assert succ is not None and succ.names == ("viin_ai",)


# ---------------------------------------------------------------------------
# Splits: the share is per ONE target directory
# ---------------------------------------------------------------------------

def test_a_split_where_no_single_target_gets_half_names_none(repo):
    """20 files: 8 into viin_ai, 8 into viin_ai_extra (40% each) - no merge."""
    _module(repo, "viin_ai_split", 20)
    _commit(repo, "add split")
    _merge(repo, "viin_ai_split", [("viin_ai", 8), ("viin_ai_extra", 8)],
           touch_target_manifest=True)
    ev, succ = _evidence(repo, "viin_ai_split")
    assert ev is not None
    assert succ is None


def test_a_split_names_only_the_target_that_got_half(repo):
    """20 files: 10 into viin_ai (50%), 8 into viin_ai_extra (40%)."""
    _module(repo, "viin_ai_split", 20)
    _commit(repo, "add split")
    _merge(repo, "viin_ai_split", [("viin_ai", 10), ("viin_ai_extra", 8)],
           touch_target_manifest=False)
    _ev, succ = _evidence(repo, "viin_ai_split")
    assert succ is not None and succ.names == ("viin_ai",)


# ---------------------------------------------------------------------------
# Evidence is git, never prose or names
# ---------------------------------------------------------------------------

def test_a_merge_subject_without_moved_files_is_no_evidence(repo):
    """'[MERGE] viin_ai_skill: merge into viin_ai' but the files were deleted."""
    _module(repo, "viin_ai_skill", 12)
    _commit(repo, "add skill")
    run_git(repo, "rm", "-r", "-q", "viin_ai_skill")
    _commit(repo, "[MERGE] viin_ai_skill: merge into viin_ai")
    ev, succ = _evidence(repo, "viin_ai_skill")
    assert ev is not None and merge_subject_target(ev.subject) == "viin_ai"
    assert succ is None


def test_a_target_the_caller_does_not_observe_is_not_named(repo):
    _module(repo, "viin_ai_agent", 12)
    _commit(repo, "add agent")
    _merge(repo, "viin_ai_agent", [("viin_ai", 11)], touch_target_manifest=True)
    _ev, succ = _evidence(repo, "viin_ai_agent", observed=("viin_ai_extra",))
    assert succ is None


def test_a_rename_to_a_module_the_commit_creates_stays_a_git_rename(repo):
    """GUARD: pre-existing behaviour - the new directory did not exist before."""
    _module(repo, "test_pylint", 15)
    _commit(repo, "add test_pylint")
    run_git(repo, "mv", "test_pylint", "new_mod")
    _commit(repo, "[REF] test_pylint: rename the module to new_mod")
    _ev, succ = _evidence(repo, "test_pylint")
    assert succ is not None
    assert (succ.names, succ.source) == (("new_mod",), SUCCESSOR_GIT_RENAME)


def test_half_moved_into_a_module_the_commit_creates_is_not_a_merge(repo):
    """A merge target must exist BEFORE the commit. new_mod is created by the
    commit and gets 5 of 10 files: not a rename (no strict majority) and not
    a merge (it did not exist) - no successor."""
    _module(repo, "old_mod", 10)
    _commit(repo, "add old_mod")
    (repo / "new_mod").mkdir()
    (repo / "new_mod" / "__manifest__.py").write_text(repr({"name": "new_mod"}) + "\n")
    _merge(repo, "old_mod", [("new_mod", 5)], touch_target_manifest=False)
    _ev, succ = _evidence(repo, "old_mod")
    assert succ is None


def test_files_moved_into_a_directory_without_a_manifest_are_no_merge(repo):
    """A plain folder (no manifest before or after) is not a module."""
    _module(repo, "viin_ai_doc", 10)
    _commit(repo, "add doc")
    _merge(repo, "viin_ai_doc", [("docs_archive", 9)], touch_target_manifest=False)
    assert merged_into(repo, run_git(repo, "rev-parse", "HEAD^"),
                       run_git(repo, "rev-parse", "HEAD"), "viin_ai_doc",
                       {"docs_archive", "viin_ai"}) == []


@pytest.mark.parametrize(("subject", "target"), [
    ("[MERGE] viin_ai_rag: merge into viin_ai", "viin_ai"),
    ("[REM] coupon: merged inside of sale_loyalty", "sale_loyalty"),
    ("[IMP] viin_ai: tidy", None),
    (None, None),
])
def test_merge_subject_target_reads_the_usual_wordings(subject, target):
    assert merge_subject_target(subject) == target

