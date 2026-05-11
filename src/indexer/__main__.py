"""Run the indexer pipeline from the command line.

Usage:
    python -m src.indexer index-repo --profile viindoo_17
    python -m src.indexer index-repo --all
    python -m src.indexer index-core --source ~/git/odoo_17.0 --version 17.0

Subcommands:
    index-repo   Index one or all registered profiles into Neo4j (existing behavior).
    index-core   Index Odoo core API symbols, lint rules, and CLI from a source checkout.
"""
import argparse
import logging
import os
import sys
from datetime import UTC, datetime

from src import config
from src.db import job_registry
from src.indexer.pipeline import (
    index_all,
    index_core,
    index_profile,
    open_production_pg,
)
from src.indexer.writer_neo4j import Neo4jWriter


def _build_embedder():
    """Build Qwen3Embedder from [embedder] config section.

    Returns None (with warning) if [embedder] url is not configured.
    """
    url = config.get("embedder", "url", fallback=None)
    if not url:
        logging.warning(
            "No [embedder] url in config — skipping embedding. "
            "Add [embedder] section to odoo-semantic.conf or use --no-embed to suppress."
        )
        return None
    from src.indexer.embedder import Qwen3Embedder
    model = config.get("embedder", "model", fallback="qwen3-embedding-q5km")
    dim = int(config.get("embedder", "dim", fallback="1024"))
    auth_token = config.from_env_or_ini(
        "EMBEDDER_AUTH_TOKEN", "embedder", "auth_token", fallback=None,
    )
    return Qwen3Embedder(url=url, model=model, dim=dim, auth_token=auth_token)


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
        help="Skip embedding step (Neo4j only). Default: embed using [embedder] config.",
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
            "Force full reindex (bypass incremental skip-unchanged + diff filter). "
            "Use periodically to clean up stale Module nodes from rename/move."
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
            "Garbage-collect stale Module nodes after scanning each repo. "
            "Compares Module nodes in Neo4j vs scanner output and DETACH DELETEs "
            "modules that no longer exist on disk (e.g. after a rename or removal). "
            "Risk-gated: only runs when scanner found ≥1 module to prevent data loss "
            "when scanner fails silently. Recommended for monthly runs or after "
            "module directory renames. See ADR-0007 §D5."
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
        gc = getattr(args, "gc", False)
        embedder = None if args.no_embed else _build_embedder()
        pg = open_production_pg()
        max_workers = getattr(args, "max_workers", 1)
        profile_workers = getattr(args, "profile_workers", 1)
        try:
            if job_id is not None:
                try:
                    job_registry.update_job(
                        pg, job_id,
                        status="running",
                        pid=os.getpid(),
                        started_at=datetime.now(UTC),
                    )
                except Exception:
                    # Don't block indexing if job tracking fails (job may have been deleted, etc.)
                    pass
            try:
                if args.all:
                    summary = index_all(
                        pg,
                        embedder=embedder,
                        progress=verbose,
                        max_workers=max_workers,
                        full_reindex=full_reindex,
                        profile_workers=profile_workers,
                        gc=gc,
                    )
                else:
                    summary = index_profile(
                        pg,
                        profile_name=args.profile,
                        embedder=embedder,
                        progress=verbose,
                        max_workers=max_workers,
                        full_reindex=full_reindex,
                        gc=gc,
                    )
                print(f"Done: {summary}")
                if args.no_embed:
                    print("Embeddings skipped (--no-embed).", file=sys.stdout)
                elif embedder is None:
                    print(
                        "Embeddings skipped — EMBEDDER_URL not configured. "
                        "Set [embedder] url in odoo-semantic.conf to enable.",
                        file=sys.stdout,
                    )
                if job_id is not None:
                    try:
                        job_registry.update_job(
                            pg, job_id,
                            status="done",
                            finished_at=datetime.now(UTC),
                        )
                    except Exception:
                        pass
            except Exception as e:
                if job_id is not None:
                    try:
                        job_registry.update_job(
                            pg, job_id,
                            status="error",
                            finished_at=datetime.now(UTC),
                            error_msg=str(e)[:1000],
                        )
                    except Exception:
                        pass
                raise
        finally:
            pg.close()

    elif args.subcommand == "index-core":
        _run_index_core(
            source=args.source,
            version=args.version,
            static_data_dir=args.static_data_dir,
        )

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
