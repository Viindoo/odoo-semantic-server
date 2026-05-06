"""Run the indexer pipeline from the command line.

Usage:
    python -m src.indexer --profile viindoo_17
    python -m src.indexer --all
"""
import argparse
import logging
import sys

from src.indexer.pipeline import index_all, index_profile, open_production_pg


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.indexer")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile", help="Index one profile by name")
    grp.add_argument("--all", action="store_true", help="Index every registered profile")
    args = parser.parse_args(argv)

    pg = open_production_pg()
    try:
        if args.all:
            summary = index_all(pg)
        else:
            summary = index_profile(pg, profile_name=args.profile)
        print(f"Done: {summary}")
    finally:
        pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
