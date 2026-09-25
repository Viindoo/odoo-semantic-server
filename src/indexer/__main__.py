# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the indexer pipeline from the command line.

Usage:
    python -m src.indexer index-repo --profile viindoo_17
    python -m src.indexer index-repo --all
    python -m src.indexer index-core --source ~/git/odoo_17.0 --version 17.0
    python -m src.indexer lifecycle-audit --all --json --fail-on-findings

Subcommands:
    index-repo       Index one or all registered profiles into Neo4j and reconcile the
                     module lifecycle (retire modules no repo ships any more).
    index-core       Index Odoo core API symbols, lint rules, and CLI from a source checkout.
    lifecycle-audit  Dry run of the module lifecycle: what the next index run would
                     retire, orphans, and rollout checks. Writes nothing.
"""
import argparse
import logging
import os
import signal
import sys
from datetime import UTC, datetime

from src import config
from src.constants import DEFAULT_EMBEDDER_MODEL
from src.db import job_registry
from src.indexer.lifecycle_audit import AUDIT_SCHEMA, FINDING_KEYS
from src.indexer.pipeline import (
    IndexRunError,
    audit_repo_for_profile,
    index_all,
    index_core,
    index_profile,
    open_production_pg,
    production_pg_dsn,
    reembed_stubs_for_profile,
)
from src.indexer.writer_neo4j import Neo4jWriter


def _build_embedder():
    """Build the configured embedder from the [embedder] config section.

    Routes through ``make_embedder()`` so the indexer honours ``EMBEDDER_BACKEND``
    (ollama|openai|tei|fake) exactly like the MCP query path and pattern seeder —
    otherwise indexing and querying could land in different embedding spaces
    (ADR-0045). Returns None (with warning) if [embedder] url is not configured.
    """
    url = config.get("embedder", "url", fallback=None)
    if not url:
        logging.warning(
            "No [embedder] url in config — skipping embedding. "
            "Add [embedder] section to odoo-semantic.conf or use --no-embed to suppress."
        )
        return None
    from src.indexer.embedder import make_embedder
    model = config.get("embedder", "model", fallback=DEFAULT_EMBEDDER_MODEL)
    dim = int(config.get("embedder", "dim", fallback="1024"))
    auth_token = config.from_env_or_ini(
        "EMBEDDER_AUTH_TOKEN", "embedder", "auth_token", fallback=None,
    )
    return make_embedder(url=url, model=model, dim=dim, auth_token=auth_token)


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser. Exported for testing."""
    parser = argparse.ArgumentParser(prog="python -m src.indexer")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- index-repo subcommand (existing behavior) -------------------------
    sub_repo = subparsers.add_parser(
        "index-repo",
        help="Index one or all registered profiles into Neo4j.",
    )
    grp = sub_repo.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile", help="Index one profile by name")
    grp.add_argument("--all", action="store_true", help="Index every registered profile")
    sub_repo.add_argument(
        "--no-embed", action="store_true",
        help=(
            "Compute no new embeddings (Neo4j only). Rows of entities the run's entity "
            "prune removes are still deleted. Default: embed using [embedder] config."
        ),
    )
    sub_repo.add_argument(
        "--no-fetch", action="store_true",
        help=(
            "Skip the pre-scan `git fetch` + `reset --hard origin/<branch>` "
            "(index the on-disk clone as-is). Default: fetch each repo first so "
            "upstream merges are picked up by the incremental check."
        ),
    )
    sub_repo.add_argument(
        "--verbose", action="store_true", default=False,
        help="Enable INFO logging and progress bar.",
    )
    sub_repo.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="(Optional) indexer_jobs.id to update lifecycle status during run.",
    )
    sub_repo.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help=(
            "Number of parallel threads for repo scanning within a profile. "
            "Default 1 (sequential). Set >1 to scan multiple repos concurrently."
        ),
    )
    sub_repo.add_argument(
        "--full",
        action="store_true",
        default=False,
        help=(
            "Re-parse and re-write every module (bypass the unchanged skip and the "
            "diff filter): backfills node properties and re-stamps every entity. "
            "Not needed for cleanup - module retirement, the entity prune and the "
            "orphan sweep run on every index run (ADR-0056)."
        ),
    )
    sub_repo.add_argument(
        "--profile-workers",
        type=int,
        default=1,
        help=(
            "Number of profiles to index in parallel (default 1, sequential). "
            "Per-profile advisory lock ensures safety across workers. "
            "Only effective with --all."
        ),
    )
    sub_repo.add_argument(
        "--gc",
        action="store_true",
        default=False,
        help=(
            "Deprecated, no effect: module retirement (with its safety gates), the "
            "orphan sweep and the version-wide GCs run on every index run "
            "(ADR-0056). Accepted so existing timers and scripts keep working."
        ),
    )
    sub_repo.add_argument(
        "--no-retire",
        action="store_true",
        default=False,
        help=(
            "Incident escape hatch: scan and write as usual but delete nothing (no "
            "retirement, owner drop, orphan sweep or entity prune). Modules that are "
            "no longer shipped stay pending in the lifecycle ledger; the next run "
            "without this flag retires and prunes them."
        ),
    )
    sub_repo.add_argument(
        "--allow-mass-retire",
        action="store_true",
        default=False,
        help=(
            "Bypass the mass-drop safety gates: module retirement / orphan sweep "
            "(more than half of a repo's modules, at least 20, or all of them, "
            "vanishing in one run) and the entity prune of one module, shared or "
            "not (more than half of its nodes or relationships, at least 20). A "
            "tripped gate makes the run exit 3. Never set it in a timer; use it "
            "once after checking lifecycle_attention."
        ),
    )

    # --- index-core subcommand (new in WI-F1) ------------------------------
    sub_core = subparsers.add_parser(
        "index-core",
        help="Index Odoo core API symbols + lint rules + CLI for one version.",
    )
    sub_core.add_argument(
        "--source", required=True,
        help="Path to Odoo upstream checkout root (parent of odoo/ directory).",
    )
    sub_core.add_argument(
        "--version", required=True,
        help="Odoo version label, e.g. '17.0'.",
    )
    sub_core.add_argument(
        "--static-data-dir", default=None,
        help="Override path for static spec_data JSON files (optional).",
    )
    sub_core.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="(Optional) indexer_jobs.id to update lifecycle status during run.",
    )

    # --- reembed-stubs subcommand (M10 WI-3) -----------------------------------
    sub_reembed = subparsers.add_parser(
        "reembed-stubs",
        help=(
            "Re-embed modules that have Neo4j nodes but zero embeddings (catch-up). "
            "Idempotent: modules already embedded are skipped."
        ),
    )
    sub_reembed.add_argument(
        "--profile", required=True,
        help="Profile name to scan for stub modules.",
    )

    # --- audit-repo subcommand (M10 WI-3) -------------------------------------
    sub_audit = subparsers.add_parser(
        "audit-repo",
        help=(
            "Read-only: export per-module coverage stats (model/field/method/"
            "view/embedding counts) as JSON. Does not write to DB."
        ),
    )
    sub_audit.add_argument(
        "--profile", required=True,
        help="Profile name to audit.",
    )
    sub_audit.add_argument(
        "--output", required=True,
        help="Path to write the JSON output file.",
    )

    # --- lifecycle-audit subcommand (ADR-0056) --------------------------------
    sub_lc = subparsers.add_parser(
        "lifecycle-audit",
        help=(
            "Dry run of the module lifecycle (writes nothing): per repo the scan "
            "verdict, what the next index run would retire and why, orphan "
            "modules/children/embeddings, and rollout checks."
        ),
    )
    lc_grp = sub_lc.add_mutually_exclusive_group(required=True)
    lc_grp.add_argument("--profile", help="Audit the repos of one profile")
    lc_grp.add_argument("--all", action="store_true", help="Audit every registered profile")
    sub_lc.add_argument(
        "--version", default=None,
        help="Only repos keyed at this Odoo version (e.g. 17.0) and its reconcile.",
    )
    sub_lc.add_argument(
        "--json", action="store_true",
        help=f"Print the report as JSON (schema {AUDIT_SCHEMA}) instead of text.",
    )
    sub_lc.add_argument(
        "--fail-on-findings", action="store_true",
        help=(
            f"Exit {EXIT_AUDIT_FINDINGS} when any finding count is non-zero: "
            f"{', '.join(FINDING_KEYS)} (the report's 'findings'). The shared-module "
            "bootstrap backlog is reported but is not a finding. The weekly drift "
            "detector."
        ),
    )

    # --- seed-patterns subcommand (new in WI-W2-6) -------------------------
    sub_seed = subparsers.add_parser(
        "seed-patterns",
        help="Load patterns.json → write Neo4j PatternExample nodes + embed pgvector.",
    )
    sub_seed.add_argument(
        "--version", default=None,
        help="Filter to a specific odoo_version_min (e.g. 17.0). Default: all versions.",
    )
    sub_seed.add_argument(
        "--no-embed", action="store_true",
        help="Skip the pgvector embed+write step (Neo4j only).",
    )
    sub_seed.add_argument(
        "--patterns-file", default=None,
        help="Path to patterns.json (optional, defaults to src/data/patterns.json).",
    )
    sub_seed.add_argument(
        "--force", action="store_true",
        help="Bypass sha256 gating, force reseed even if patterns.json unchanged.",
    )
    sub_seed.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="(Optional) indexer_jobs.id to update lifecycle status during run.",
    )

    return parser


def subcommand_accepts_job_id(subcommand: str) -> bool:
    """True when *subcommand* declares ``--job-id`` (it reports its state on
    its Web UI job row). Read from the real parser, so the Web UI spawn helper
    never hands the flag to a subcommand whose argparse would exit 2 on it."""
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get(subcommand)
            return sub is not None and "--job-id" in sub._option_string_actions
    return False


def _track_job(pg, job_id: int, **fields) -> None:
    """Report the run's state on its Web UI job row (``indexer_jobs``).

    Never fatal - a deleted job row or a PG hiccup must not fail the index
    run - but never silent either: the failure is logged at WARNING.
    """
    try:
        job_registry.update_job(pg, job_id, **fields)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "index job %s: status update %s failed: %s: %s",
            job_id, fields.get("status"), type(exc).__name__, exc,
        )


# Seconds the SIGTERM handler waits to reach PostgreSQL before giving up the
# job update and exiting anyway.
_SIGTERM_CONNECT_TIMEOUT_S = 5


def _mark_job_terminated(job_id: int) -> None:
    """Write the SIGTERM error on the job row through a connection of its own.

    Never the run's main connection: SIGTERM can land while it is inside a
    ``_write_scope`` transaction (autocommit off), and an update written there
    is rolled back when the process exits - the job then stayed ``running``
    with a dead pid (#381 F6). ``open_production_pg`` returns an autocommit
    connection, so the update is committed before ``sys.exit``.

    A plain ``psycopg2.connect`` with a short ``connect_timeout``: an
    unreachable database must not hold the exit until systemd's SIGKILL, and a
    signal handler must not bootstrap the shared pool (``open_production_pg``)."""
    try:
        import psycopg2  # noqa: PLC0415

        conn = psycopg2.connect(
            production_pg_dsn(), connect_timeout=_SIGTERM_CONNECT_TIMEOUT_S,
        )
        conn.autocommit = True
    except Exception as exc:  # noqa: BLE001 - the process is exiting anyway
        logging.getLogger(__name__).warning(
            "index job %s: cannot record SIGTERM: %s: %s", job_id, type(exc).__name__, exc,
        )
        return
    try:
        _track_job(
            conn, job_id,
            status="error",
            finished_at=datetime.now(UTC),
            error_msg="Process received SIGTERM",
        )
    finally:
        conn.close()


def _install_sigterm_handler(job_id: int | None) -> None:
    """On SIGTERM: record the error on the Web UI job (if any), then exit 1
    through ``SystemExit`` so the run's ``finally`` blocks close its
    connections."""

    def _sigterm_handler(signum, frame):
        if job_id is not None:
            _mark_job_terminated(job_id)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _sigterm_handler)


EXIT_LIFECYCLE_ATTENTION = 3
# lifecycle-audit --fail-on-findings: drift found (distinct from 1, a crash).
EXIT_AUDIT_FINDINGS = 4


def _lifecycle_exit_code(lifecycle: dict, *, run_failed: bool = False) -> int:
    """0, or EXIT_LIFECYCLE_ATTENTION when the run's module lifecycle needs an
    operator: a safety gate tripped, a name was undecidable, or the reconcile
    failed (review H4 - systemd ``OnFailure=`` must fire, the data was indexed
    but ghosts may remain). The details go to stderr; the same text is in
    ``repos.lifecycle_attention``.

    *run_failed*: a repo or profile failed to index. The run exits 1, which
    outranks 3, but the lifecycle details are printed all the same."""
    if run_failed:
        code = 1
    elif lifecycle.get("needs_attention"):
        code = EXIT_LIFECYCLE_ATTENTION
    else:
        code = 0
    if not lifecycle.get("needs_attention"):
        return code
    print(f"Lifecycle needs attention (exit {code}):", file=sys.stderr)
    for key in ("gates_tripped", "undecidable", "errors"):
        for item in lifecycle.get(key) or []:
            print(f"  {key}: {item}", file=sys.stderr)
    # The embedding gate id names no profile; say which ones it holds (#381 F3).
    for report in lifecycle.get("reports") or []:
        held = report.get("embedding_orphans_held") or []
        if not held:
            continue
        profiles = sorted({
            g["profile"] if isinstance(g, dict) else g[1] for g in held
        })
        print(
            f"  embedding_orphans_held: {report.get('odoo_version')}: "
            f"{len(held)} group(s) of profile(s) {', '.join(profiles)}",
            file=sys.stderr,
        )
    return code


def _print_empty_profiles(summary, profile: str | None) -> None:
    """Name the profiles that had no repo to index (neither ok nor failed, exit 0)."""
    if not isinstance(summary, dict):
        return
    if summary.get("no_repos"):
        print(f"Profile {profile!r} has no repo registered: nothing indexed.", file=sys.stderr)
    empty = summary.get("profiles_empty") or []
    if empty:
        print(
            f"{len(empty)} profile(s) with no repo registered, nothing indexed: "
            + ", ".join(empty),
            file=sys.stderr,
        )


def _run_lifecycle_audit(args) -> int:
    """Execute lifecycle-audit: print the report; 0, or EXIT_AUDIT_FINDINGS
    under ``--fail-on-findings`` when the report has findings."""
    import json  # noqa: PLC0415

    from src.indexer.lifecycle_audit import render_text, run_lifecycle_audit

    try:
        report = run_lifecycle_audit(
            profile=args.profile, all_profiles=args.all, version=args.version,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, default=str))
    else:
        print(render_text(report))
    if args.fail_on_findings and report["has_findings"]:
        counts = ", ".join(f"{k}={n}" for k, n in report["findings"].items() if n)
        print(
            f"Lifecycle audit found drift (exit {EXIT_AUDIT_FINDINGS}): {counts}",
            file=sys.stderr,
        )
        return EXIT_AUDIT_FINDINGS
    return 0


def _run_index_core(
    source: str,
    version: str,
    static_data_dir: str | None,
) -> None:
    """Execute index-core: open Neo4j, run index_core, close. Separated for testability."""
    from src.indexer.pipeline import _neo4j_creds
    uri, user, password = _neo4j_creds()
    writer = Neo4jWriter(uri, user, password)
    try:
        writer.setup_indexes()
        summary = index_core(
            source_root=source,
            odoo_version=version,
            writer=writer,
            static_data_dir=static_data_dir,
        )
        print(
            f"Done: {summary['core_symbols']} CoreSymbol, "
            f"{summary['lint_rules']} LintRule, "
            f"{summary['cli_commands']} CLICommand, "
            f"{summary['cli_flags']} CLIFlag"
        )
    finally:
        writer.close()


def main(argv: list[str] | None = None) -> int:
    # ADR-0031: load `.env` at the CLI entry point so PG_DSN / NEO4J_* / EMBEDDER_*
    # (with secrets) resolve on a fresh prod box without manually sourcing .env.
    # Idempotent + main()-only (never at import) so pytest is unaffected; mirrors
    # src/db/migrate.py::main().
    config.init_dotenv()
    from src.logging_config import configure_logging
    parser = _build_parser()
    args = parser.parse_args(argv)

    _verbose_mode = args.subcommand == "index-repo" and getattr(args, "verbose", False)
    log_level = logging.INFO if _verbose_mode else logging.WARNING
    configure_logging(level=log_level)

    if args.subcommand == "index-repo":
        verbose = getattr(args, "verbose", False)
        job_id = getattr(args, "job_id", None)
        full_reindex = getattr(args, "full", False)
        if getattr(args, "gc", False):
            logging.getLogger(__name__).warning(
                "--gc is deprecated and has no effect: module retirement now runs on "
                "every index run (ADR-0056)"
            )
        retire = not getattr(args, "no_retire", False)
        allow_mass_retire = getattr(args, "allow_mass_retire", False)
        refresh = not getattr(args, "no_fetch", False)
        embedder = None if args.no_embed else _build_embedder()
        pg = open_production_pg()
        max_workers = getattr(args, "max_workers", 1)
        profile_workers = getattr(args, "profile_workers", 1)

        _install_sigterm_handler(job_id)

        try:
            if job_id is not None:
                _track_job(
                    pg, job_id,
                    status="running",
                    pid=os.getpid(),
                    started_at=datetime.now(UTC),
                )
            try:
                # A run with a failed repo or profile still reconciled the
                # healthy ones: its summary comes with the IndexRunError.
                failure: IndexRunError | None = None
                try:
                    if args.all:
                        summary = index_all(
                            pg,
                            embedder=embedder,
                            progress=verbose,
                            max_workers=max_workers,
                            full_reindex=full_reindex,
                            profile_workers=profile_workers,
                            refresh=refresh,
                            retire=retire,
                            allow_mass_retire=allow_mass_retire,
                        )
                    else:
                        summary = index_profile(
                            pg,
                            profile_name=args.profile,
                            embedder=embedder,
                            progress=verbose,
                            max_workers=max_workers,
                            full_reindex=full_reindex,
                            refresh=refresh,
                            retire=retire,
                            allow_mass_retire=allow_mass_retire,
                        )
                except IndexRunError as exc:
                    failure = exc
                    summary = dict(exc.summary)
                lifecycle = (
                    summary.pop("lifecycle", None) if isinstance(summary, dict) else None
                )
                if not isinstance(lifecycle, dict):
                    lifecycle = {}
                failed_profiles = (
                    summary.get("profiles_failed") if isinstance(summary, dict) else None
                ) or []
                run_failed = failure is not None or bool(failed_profiles)
                print(f"{'Finished with failures' if run_failed else 'Done'}: {summary}")
                _print_empty_profiles(summary, args.profile)
                if args.no_embed:
                    print("Embeddings skipped (--no-embed).", file=sys.stdout)
                elif embedder is None:
                    print(
                        "Embeddings skipped — EMBEDDER_URL not configured. "
                        "Set [embedder] url in odoo-semantic.conf to enable.",
                        file=sys.stdout,
                    )
                exit_code = _lifecycle_exit_code(lifecycle, run_failed=run_failed)
                if failure is not None:
                    raise failure
                if failed_profiles:
                    raise IndexRunError(
                        f"{len(failed_profiles)} profile(s) failed: "
                        + ", ".join(failed_profiles),
                        summary,
                    )
                if job_id is not None:
                    _track_job(
                        pg, job_id,
                        status="done",
                        finished_at=datetime.now(UTC),
                    )
            except BaseException as e:
                if job_id is not None and not isinstance(e, SystemExit):
                    # Don't overwrite job status already set by SIGTERM handler
                    _track_job(
                        pg, job_id,
                        status="error",
                        finished_at=datetime.now(UTC),
                        error_msg=str(e)[:1000],
                    )
                raise
        finally:
            if embedder is not None:
                close = getattr(embedder, "close", None)
                if callable(close):
                    close()
            pg.close()
        return exit_code

    elif args.subcommand == "index-core":
        job_id = getattr(args, "job_id", None)
        pg = open_production_pg() if job_id is not None else None
        # Only a tracked run has a job to record; an untracked index-core keeps
        # the default SIGTERM (killed, exit 143) as before #381. index-repo
        # always installs it (pre-existing: its finally closes the embedder).
        if job_id is not None:
            _install_sigterm_handler(job_id)
        try:
            if job_id is not None:
                _track_job(
                    pg, job_id,
                    status="running",
                    pid=os.getpid(),
                    started_at=datetime.now(UTC),
                )
            try:
                _run_index_core(
                    source=args.source,
                    version=args.version,
                    static_data_dir=args.static_data_dir,
                )
            except BaseException as e:
                if job_id is not None and not isinstance(e, SystemExit):
                    _track_job(
                        pg, job_id,
                        status="error",
                        finished_at=datetime.now(UTC),
                        error_msg=str(e)[:1000],
                    )
                raise
            if job_id is not None:
                _track_job(pg, job_id, status="done", finished_at=datetime.now(UTC))
        finally:
            if pg is not None:
                pg.close()

    elif args.subcommand == "reembed-stubs":
        embedder = _build_embedder()
        if embedder is None:
            print(
                "Error: [embedder] url not configured — cannot reembed. "
                "Set [embedder] url in odoo-semantic.conf or EMBEDDER_URL env var.",
                file=sys.stderr,
            )
            return 1
        pg = open_production_pg()
        try:
            summary = reembed_stubs_for_profile(
                pg,
                profile_name=args.profile,
                embedder=embedder,
            )
            print(
                f"Done: checked {summary['modules_checked']} modules, "
                f"re-embedded {summary['modules_reembedded']}, "
                f"{summary['total_embed_calls']} embed call(s)."
            )
        finally:
            close = getattr(embedder, "close", None)
            if callable(close):
                close()
            pg.close()

    elif args.subcommand == "audit-repo":
        import json  # noqa: PLC0415

        pg = open_production_pg()
        try:
            rows = audit_repo_for_profile(pg, profile_name=args.profile)
            output_path = args.output
            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2, ensure_ascii=False)
            print(f"Audit complete: {len(rows)} module(s) written to {output_path}")
        finally:
            pg.close()

    elif args.subcommand == "lifecycle-audit":
        return _run_lifecycle_audit(args)

    elif args.subcommand == "seed-patterns":
        from src.indexer import seed_patterns as seed_patterns_module
        argv_seed = ["--version", args.version] if args.version else []
        if args.no_embed:
            argv_seed.append("--no-embed")
        if args.patterns_file:
            argv_seed.extend(["--patterns-file", args.patterns_file])
        if args.force:
            argv_seed.append("--force")
        if getattr(args, "job_id", None) is not None:
            argv_seed.extend(["--job-id", str(args.job_id)])
        return seed_patterns_module.main(argv_seed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
