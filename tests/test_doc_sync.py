"""
Doc sync guard — unit tests (no Neo4j needed).

Catches two drift categories from M1 (21 drift points across 4 commits):
  1. TASKS.md marks [x] but file doesn't exist on disk
  2. Stale [~] in-progress markers left by incomplete agent work
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


_HTTP_METHOD_PREFIXES = (
    "GET ", "POST ", "PUT ", "PATCH ", "DELETE ", "HEAD ", "OPTIONS ",
)


def _completed_file_refs() -> list[str]:
    """Extract file paths from [x] tasks in TASKS.md."""
    content = (REPO_ROOT / "TASKS.md").read_text()
    files = []
    for line in content.splitlines():
        m = re.match(r"\s*-\s*\[x\]\s*`([^`]+)`", line)
        if not m:
            continue
        raw = m.group(1).split(":")[0].strip()
        # Skip API endpoint refs like `POST /api/foo` — they share the "/"
        # heuristic with file paths but are not files on disk.
        if raw.startswith(_HTTP_METHOD_PREFIXES):
            continue
        if "/" in raw or raw.endswith((".py", ".yml", ".toml", ".md", ".sh")):
            files.append(raw)
    return files


def test_tasks_md_completed_files_exist():
    """Every [x] file in TASKS.md must exist on disk.

    Failing scenario: agent adds '- [x] `src/new_file.py`: description' to TASKS.md
    without creating the file → test fails:
    "TASKS.md marks these [x] but files don't exist: src/new_file.py"
    """
    completed = _completed_file_refs()
    assert len(completed) >= 1, "Regex parsed zero [x] file refs — check TASKS.md format"
    missing = [f for f in completed if not (REPO_ROOT / f).exists()]
    assert not missing, (
        "TASKS.md marks these [x] but files don't exist on disk:\n"
        + "\n".join(f"  {f}" for f in missing)
        + "\nFix: create the file, or revert [x] → [ ] in TASKS.md."
    )


def test_no_stale_in_progress_markers():
    """[~] markers must not remain when a milestone has no open [ ] tasks left.

    During active work: [~] + [x] + [ ] coexist — this is NORMAL, test passes.
    Stale case: [~] + [x] but NO [ ] remaining — milestone looks complete
    but someone forgot to flip [~] → [x].

    Failing scenario: agent marks ALL tasks [x] but leaves one [~] behind:
    "Milestone 2 looks complete but has stale [~]: update [~] → [x] in TASKS.md."
    """
    content = (REPO_ROOT / "TASKS.md").read_text()
    milestone_blocks = re.split(r"(?=## Milestone \d+)", content)
    for block in milestone_blocks:
        if "## Milestone" not in block:
            continue
        header = re.match(r"## Milestone (\d+)", block)
        num = header.group(1) if header else "?"
        has_wip = bool(re.search(r"- \[~\]", block))
        has_done = bool(re.search(r"- \[x\]", block))
        has_pending = bool(re.search(r"- \[ \]", block))
        # Only stale if: has [~] AND has [x] AND no [ ] left (milestone appears complete)
        if has_wip and has_done and not has_pending:
            assert False, (
                f"Milestone {num} looks complete (all done [x], no pending [ ]) "
                f"but has stale [~]. Update [~] → [x] in TASKS.md."
            )
