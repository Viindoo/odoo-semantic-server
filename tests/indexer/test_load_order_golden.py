"""Golden-file test: simulator output over the 10-module Odoo CE subset.

The golden file at ``tests/fixtures/golden/load_order_ce_subset.json`` was
produced by running the OSM simulator over frozen ``__manifest__.py`` copies
(GOLDEN_SOURCE: simulator_self, manual_verify_once -- see
``tests/fixtures/generate_golden_load_order.py`` for rationale and
verification instructions).
"""

from __future__ import annotations

import json
from pathlib import Path

from osm.indexer.load_order import compute_load_order
from osm.indexer.manifest import scan_addon_root

FIXTURES = Path(__file__).parent.parent / "fixtures"
SUBSET_DIR = FIXTURES / "odoo_ce_subset_manifests"
GOLDEN_FILE = FIXTURES / "golden" / "load_order_ce_subset.json"


def test_golden_load_order_matches() -> None:
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    expected = [
        {"name": row["name"], "depth": row["depth"], "load_order": row["load_order"]}
        for row in golden
    ]

    records = scan_addon_root(SUBSET_DIR)
    result = compute_load_order(records)
    actual = [
        {"name": r.name, "depth": r.depth, "load_order": r.load_order}
        for r in result
    ]

    assert actual == expected
