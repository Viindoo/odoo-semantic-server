# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate ``spec_data/module_renames.json`` from OpenUpgrade (issue #380 item 2).

The generator behind ``scripts/sync_module_renames.py``. For every ``N.0``
branch (``N >= module_renames.MIN_MAJOR``) of the source repository:

1. the branch head sha comes from ``git ls-remote --heads`` (no clone, no API
   token, nothing written to disk);
2. ``apriori.py`` is read at THAT sha (raw file, first of
   ``module_renames.APRIORI_PATHS`` the commit has), so the recorded commit is
   exactly the content parsed;
3. :func:`module_renames.parse_apriori` extracts the two maps with ``ast``.

A branch whose content is unchanged keeps its recorded commit (a commit that
only touched other files is no drift, so the weekly job does not open a PR for
it). The check never trusts the file it compares against: every value is
re-derived from the source on each run.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from src.indexer import module_renames as mr

RAW_URL = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"
HTTP_TIMEOUT_SECONDS = 30

_DESCRIPTION = (
    "Cross-version module renames and merges, one block per OpenUpgrade branch N.0: "
    "renamed_modules / merged_modules of that branch's apriori.py (old name at N-1 -> "
    "new name at N). GENERATED - do not edit by hand: python scripts/sync_module_renames.py "
    "--write (weekly workflow module-renames-resync.yml opens a PR on drift)."
)
_ORACLE = (
    "ADR-0055 provenance + oracle. Provenance: each branch block records repo, branch, "
    "commit and path of the apriori.py it was parsed from (ast.literal_eval, never "
    "executed). Oracle: the read side (src/indexer/module_renames.resolve_chain) shows an "
    "edge only when the caller's index has old at N-1 and not from N to the asked version, "
    "and new at N; chains are composed hop by hop under the same rule."
)


def remote_branch_heads(repo: str, *, runner=subprocess.run) -> dict[str, str]:
    """``{branch: sha}`` of the ``N.0`` branches with ``N >= MIN_MAJOR``."""
    url = f"https://github.com/{repo}"
    result = runner(
        ["git", "ls-remote", "--heads", url], capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote {url} failed: {result.stderr.strip()[:300]}")
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        branch = ref.removeprefix("refs/heads/")
        major = mr.branch_major(branch)
        if major is not None and major >= mr.MIN_MAJOR and len(sha) == 40:
            out[branch] = sha
    if not out:
        raise RuntimeError(f"{url}: no N.0 branch found")
    return out


def http_fetch(repo: str, sha: str, path: str) -> str | None:
    """The file at *sha*, or None when the commit does not have it (HTTP 404)."""
    url = RAW_URL.format(repo=repo, sha=sha, path=path)
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"GET {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url}: {exc.reason}") from exc


Fetcher = Callable[[str, str, str], "str | None"]


@dataclass
class BranchResult:
    branch: str
    commit: str
    path: str | None
    modules: mr.AprioriModules


def fetch_branch(repo: str, branch: str, sha: str, fetch: Fetcher) -> BranchResult:
    for path in mr.APRIORI_PATHS:
        text = fetch(repo, sha, path)
        if text is not None:
            try:
                return BranchResult(branch, sha, path, mr.parse_apriori(text))
            except ValueError as exc:
                raise RuntimeError(f"{repo}@{branch} {path}: {exc}") from exc
    return BranchResult(branch, sha, None, mr.AprioriModules({}, {}))


def _content(block: Mapping | None) -> tuple:
    if not block:
        return (None, None, {}, {})
    return (
        block.get("repo"), block.get("path"),
        dict(block.get("renamed_modules") or {}), dict(block.get("merged_modules") or {}),
    )


@dataclass
class SyncPlan:
    new_data: dict
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    dropped: dict[str, tuple[str, ...]] = field(default_factory=dict)
    edge_count: int = 0

    @property
    def drifted(self) -> bool:
        return bool(self.changed or self.added or self.removed)


def plan_sync(
    current: Mapping | None, results: list[BranchResult], repo: str, today: date,
) -> SyncPlan:
    """The regenerated document and how it differs from *current* (content only:
    a branch whose maps, path and repo are unchanged keeps its recorded commit)."""
    old_branches = dict((current or {}).get("branches") or {})
    branches: dict[str, dict] = {}
    plan = SyncPlan(new_data={})
    for res in sorted(results, key=lambda r: mr.version_key(r.branch)):
        block = {
            "repo": repo,
            "branch": res.branch,
            "commit": res.commit,
            "path": res.path,
            "renamed_modules": res.modules.renamed,
            "merged_modules": res.modules.merged,
        }
        old = old_branches.get(res.branch)
        if old is None:
            plan.added.append(res.branch)
        elif _content(old) != _content(block):
            plan.changed.append(res.branch)
        else:
            block["commit"] = old.get("commit") or res.commit
        if res.modules.dropped:
            plan.dropped[res.branch] = res.modules.dropped
        plan.edge_count += len(res.modules.renamed) + len(res.modules.merged)
        branches[res.branch] = block
    plan.removed = sorted(set(old_branches) - set(branches), key=mr.version_key)
    generated = (current or {}).get("generated_at") if not plan.drifted else None
    plan.new_data = {
        "_description": _DESCRIPTION,
        "_oracle": _ORACLE,
        "generated_at": generated or today.isoformat(),
        "branches": branches,
    }
    return plan


def dump_data(data: Mapping) -> str:
    return json.dumps(data, indent=2, ensure_ascii=True, sort_keys=False) + "\n"


def format_plan(plan: SyncPlan) -> str:
    lines = [
        f"{len(plan.new_data.get('branches', {}))} branch(es), {plan.edge_count} edge(s); "
        f"drift: changed {plan.changed or '-'}, added {plan.added or '-'}, "
        f"removed {plan.removed or '-'}",
    ]
    for branch, block in plan.new_data.get("branches", {}).items():
        lines.append(
            f"  {branch}: {block['path'] or 'no apriori.py'} @ {block['commit'][:10]} "
            f"renamed {len(block['renamed_modules'])} merged {len(block['merged_modules'])}"
        )
    for branch, items in plan.dropped.items():
        for item in items:
            lines.append(f"  {branch}: dropped {item}")
    return "\n".join(lines)
