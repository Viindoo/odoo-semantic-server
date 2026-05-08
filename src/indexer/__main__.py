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
import sys

from src import config
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
    return Qwen3Embedder(url=url, model=model, dim=dim)


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "index-repo":
        embedder = None if args.no_embed else _build_embedder()
        pg = open_production_pg()
        try:
            if args.all:
                summary = index_all(pg, embedder=embedder)
            else:
                summary = index_profile(pg, profile_name=args.profile, embedder=embedder)
            print(f"Done: {summary}")
        finally:
            pg.close()

    elif args.subcommand == "index-core":
        _run_index_core(
            source=args.source,
            version=args.version,
            static_data_dir=args.static_data_dir,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
