#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-sync the curated GLOBAL CLI flags from the real Odoo ``tools/config.py`` (issue #380 item 6).

Usage:
    python scripts/sync_cli_flags.py --check [--checkouts DIR] [--majors 16 19] [--report-file F]
    python scripts/sync_cli_flags.py --write [--checkouts DIR] [--majors 16 19] [--report-file F]
    python scripts/sync_cli_flags.py --print-majors

``--check``  exit 0 when every curated global flag equals what the oracle parses out of the
             checkout (and the provenance block is present), exit 1 on drift, exit 2 when a
             checkout is missing or cannot vouch for itself (wrong branch, dirty config.py).
``--write``  rewrite only the ``spec_data/cli_flags_<version>.json`` files that drifted;
             exit 0. Review the diff - flags added from source need hand-curation of
             ``replacement_flag_name`` / ``env_name`` / ``posix_only``.

Checkout layout: ``<DIR>/odoo<major>`` holding the ``<major>.0`` branch of odoo/odoo
(``DIR`` defaults to ``$OSM_ODOO_CHECKOUTS`` or ``~/git``, the convention of
``tests/_odoo_checkouts.py``). Only ``odoo/tools/config.py`` (``openerp/tools/config.py``
for v8/v9) has to be present, so a shallow sparse checkout is enough - see
``.github/workflows/cli-flags-resync.yml``.

Scope, the oracle and what is deliberately untouched: ``src/indexer/cli_flags_sync.py``
module docstring (ADR-0055). Per-command flags have no oracle and are never modified.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.indexer import cli_flags_sync as sync  # noqa: E402

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


def default_checkouts() -> Path:
    return Path(os.environ.get("OSM_ODOO_CHECKOUTS", str(Path.home() / "git")))


def plan_for_major(
    major: int, checkouts: Path, spec_dir: Path, today: date | None,
) -> sync.SyncPlan:
    version = f"{major}.0"
    checkout = checkouts / f"odoo{major}"
    cfg = sync.config_path(checkout, major)
    if not cfg.is_file():
        raise RuntimeError(f"v{major}: {cfg} not found")
    provenance = sync.read_provenance(checkout, major, today=today)
    source = cfg.read_text(encoding="utf-8", errors="ignore")
    oracle = sync.oracle_global_flags(source, version, file_path=str(cfg))
    if not oracle:
        # An empty parse would read as "every curated flag vanished" - never drift, always
        # an oracle/checkout failure.
        raise RuntimeError(f"v{major}: the oracle found no flags in {cfg}")
    return sync.plan_sync(version, sync.load_data(version, spec_dir), oracle, provenance)


def render_report(plans: list[sync.SyncPlan], mode: str) -> str:
    drifted = [p for p in plans if p.drifted]
    lines = [
        f"CLI flags re-sync ({mode}): {len(drifted)} of {len(plans)} version(s) drifted.",
        "",
        "Scope: GLOBAL flags only (`tools/config.py`, the one file the oracle reads). "
        "Per-command flags have no oracle and are never modified; flags added from source "
        "need hand-curation of replacement_flag_name / env_name / posix_only (ADR-0055).",
        "",
        "```",
        *[sync.format_plan(p) for p in plans],
        "```",
    ]
    return "\n".join(lines) + "\n"


def run(
    *,
    mode: str,
    majors: list[int],
    checkouts: Path,
    spec_dir: Path = sync.SPEC_DATA_DIR,
    report_file: Path | None = None,
    today: date | None = None,
    out=None,
) -> int:
    out = out or sys.stdout
    plans: list[sync.SyncPlan] = []
    for major in majors:
        try:
            plans.append(plan_for_major(major, checkouts, spec_dir, today))
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    for plan in plans:
        print(sync.format_plan(plan), file=out)
    report = render_report(plans, mode)
    if report_file is not None:
        report_file.write_text(report, encoding="utf-8")

    drifted = [p for p in plans if p.drifted]
    if mode == "write":
        for plan in drifted:
            sync.spec_path(plan.version, spec_dir).write_text(
                sync.dump_data(plan.new_data), encoding="utf-8",
            )
        print(f"wrote {len(drifted)} file(s): "
              f"{[f'cli_flags_{p.version}.json' for p in drifted]}", file=out)
        return EXIT_OK
    if drifted:
        print(f"DRIFT in {[p.version for p in drifted]} - run with --write and review "
              "the diff", file=out)
        return EXIT_DRIFT
    print(f"all {len(plans)} version(s) in sync", file=out)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_const", dest="mode", const="check")
    mode.add_argument("--write", action="store_const", dest="mode", const="write")
    mode.add_argument("--print-majors", action="store_const", dest="mode", const="print")
    ap.add_argument("--checkouts", type=Path, default=None,
                    help="parent dir holding odoo<major> checkouts")
    ap.add_argument("--majors", type=int, nargs="+", default=None,
                    help="restrict to these majors (default: every curated cli_flags file)")
    ap.add_argument("--report-file", type=Path, default=None,
                    help="also write a markdown report (used as the PR body)")
    args = ap.parse_args(argv)

    available = sync.curated_majors()
    if args.mode == "print":
        print(" ".join(str(m) for m in available))
        return EXIT_OK
    majors = args.majors or available
    unknown = [m for m in majors if m not in available]
    if unknown:
        print(f"error: no curated cli_flags file for major(s) {unknown}", file=sys.stderr)
        return EXIT_ERROR
    return run(
        mode=args.mode, majors=majors,
        checkouts=args.checkouts or default_checkouts(), report_file=args.report_file,
    )


if __name__ == "__main__":
    sys.exit(main())
