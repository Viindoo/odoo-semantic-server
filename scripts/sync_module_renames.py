#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the cross-version module rename data from OpenUpgrade (issue #380 item 2).

Usage:
    python scripts/sync_module_renames.py --check [--repo OCA/OpenUpgrade] [--report-file F]
    python scripts/sync_module_renames.py --write [--repo OCA/OpenUpgrade] [--report-file F]

``--check``  exit 0 when ``src/indexer/spec_data/module_renames.json`` equals what the
             ``apriori.py`` of every ``N.0`` branch says today, exit 1 on drift, exit 2
             when the source cannot be read (network, a file that does not parse).
``--write``  rewrite the file (only the branches whose content changed get a new
             commit); exit 0. Review the diff before merging.

Network only, nothing is cloned or written outside the spec file: the branch heads come
from ``git ls-remote``, each apriori.py is read at that exact commit. The data is a claim
of OpenUpgrade; the read side validates every edge against the index before showing it
(``src/indexer/module_renames.py``, ADR-0055).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.indexer import module_renames as mr  # noqa: E402
from src.indexer import module_renames_sync as sync  # noqa: E402

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


def run(
    *,
    mode: str,
    repo: str = mr.DEFAULT_SOURCE_REPO,
    spec_path: Path = mr.SPEC_PATH,
    report_file: Path | None = None,
    heads=sync.remote_branch_heads,
    fetch=sync.http_fetch,
    today: date | None = None,
    out=None,
) -> int:
    out = out or sys.stdout
    today = today or date.today()
    try:
        branch_heads = heads(repo)
        results = [
            sync.fetch_branch(repo, branch, sha, fetch)
            for branch, sha in sorted(branch_heads.items(), key=lambda kv: mr.version_key(kv[0]))
        ]
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not any(r.path for r in results):
        print(f"error: no branch of {repo} has an apriori.py", file=sys.stderr)
        return EXIT_ERROR
    current = mr.load_data(spec_path) if spec_path.is_file() else None
    plan = sync.plan_sync(current, results, repo, today)
    text = sync.format_plan(plan)
    print(text, file=out)
    if report_file is not None:
        report_file.write_text(
            f"Module renames re-sync ({mode}) from {repo}\n\n```\n{text}\n```\n",
            encoding="utf-8",
        )
    if mode == "write":
        spec_path.write_text(sync.dump_data(plan.new_data), encoding="utf-8")
        print(f"wrote {spec_path}", file=out)
        return EXIT_OK
    if plan.drifted:
        print("DRIFT - run with --write and review the diff", file=out)
        return EXIT_DRIFT
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--repo", default=mr.DEFAULT_SOURCE_REPO,
                        help="GitHub owner/name of the OpenUpgrade repository")
    parser.add_argument("--spec", type=Path, default=mr.SPEC_PATH)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args(argv)
    return run(
        mode="write" if args.write else "check", repo=args.repo,
        spec_path=args.spec, report_file=args.report_file,
    )


if __name__ == "__main__":
    sys.exit(main())
