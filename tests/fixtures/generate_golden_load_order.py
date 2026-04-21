"""Generate the golden load-order fixture for the 10-module Odoo CE subset.

GOLDEN_SOURCE: simulator_self, manual_verify_once

This script produces the golden fixture by running the OSM load-order
simulator over frozen copies of 10 Odoo CE 17.0 ``__manifest__.py`` files.
It does NOT boot Odoo or query the database -- it is simulator-produced,
not Odoo-produced.

Manual verification:
    Once a live Odoo 17.0 database is available, verify that the load_order
    sequence for these 10 modules matches the order in ``ir.module.module``
    (sorted by the order Odoo actually installs them).  Update this golden
    file if they diverge and document the delta.

Modules whose declared ``depends`` fall outside the 10-module subset are
warned and dropped by the simulator.  This is intentional: the golden
file captures only the ordering of the modules that *can* be resolved
within this isolated subset.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))  # noqa: E402

from osm.indexer.load_order import compute_load_order  # noqa: E402
from osm.indexer.manifest import scan_addon_root  # noqa: E402

SUBSET_DIR = pathlib.Path(__file__).resolve().parent / "odoo_ce_subset_manifests"
GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "golden"
GOLDEN_FILE = GOLDEN_DIR / "load_order_ce_subset.json"


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    records = scan_addon_root(SUBSET_DIR)
    print(f"Scanned {len(records)} module(s): {[r.name for r in records]}")

    load_order = compute_load_order(records)
    print(f"Ordered {len(load_order)} module(s):")
    for r in load_order:
        print(f"  [{r.load_order}] {r.name!r:30s} depth={r.depth}")

    golden = [
        {"name": r.name, "depth": r.depth, "load_order": r.load_order}
        for r in load_order
    ]
    GOLDEN_FILE.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
    print(f"\nGolden written to {GOLDEN_FILE}")


if __name__ == "__main__":
    main()
