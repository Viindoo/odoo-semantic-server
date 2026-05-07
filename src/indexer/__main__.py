"""Run the indexer pipeline from the command line.

Usage:
    python -m src.indexer --profile viindoo_17
    python -m src.indexer --profile viindoo_17 --no-embed
    python -m src.indexer --all
"""
import argparse
import logging
import sys

from src import config
from src.indexer.pipeline import index_all, index_profile, open_production_pg


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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.indexer")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile", help="Index one profile by name")
    grp.add_argument("--all", action="store_true", help="Index every registered profile")
    parser.add_argument(
        "--no-embed", action="store_true",
        help="Skip embedding step (Neo4j only). Default: embed using [embedder] config.",
    )
    args = parser.parse_args(argv)

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
