#!/usr/bin/env python3
"""
dump_graph_snippet.py — Bake graph-snapshot.json from live Neo4j for the landing page hero.

Usage:
    python scripts/dump_graph_snippet.py [options]

Options:
    --output FILE           Output file path [default: site/public/graph-snapshot.json]
    --model MODEL           Model name to query [default: sale.order]
    --version VERSION       Odoo version [default: 17.0]
    --depth N               INHERITS traversal depth [default: 2]
    --include-private       Include EE/private modules (default: CE only)
    --dry-run               Print JSON to stdout, do not write file
    --pretty                Pretty-print JSON (implied by --dry-run)

Environment variables:
    NEO4J_URI               Neo4j bolt URI [default: bolt://localhost:7687]
    NEO4J_USER              Neo4j username [default: neo4j]
    NEO4J_PASS              Neo4j password [default: neo4j]

If Neo4j is unavailable or query returns no results, a minimal 3-node placeholder
is written so the landing page hero animation still renders.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Placeholder fallback (used when Neo4j unavailable or query empty)
# ---------------------------------------------------------------------------

PLACEHOLDER_SNAPSHOT: dict[str, Any] = {
    "_meta": {
        "generated_by": "scripts/dump_graph_snippet.py",
        "note": "placeholder — run against live Neo4j to regenerate",
        "model": "sale.order",
        "odoo_version": "17.0",
        "source": "fallback",
    },
    "nodes": [
        {
            "id": "sale.order:sale",
            "type": "default",
            "position": {"x": 300, "y": 150},
            "data": {
                "label": "sale.order\n(sale)",
                "module": "sale",
                "is_definition": True,
                "field_count": 148,
                "method_count": 394,
                "odoo_version": "17.0",
            },
        },
        {
            "id": "sale.order:viin_sale",
            "type": "default",
            "position": {"x": 80, "y": 300},
            "data": {
                "label": "sale.order\n(viin_sale)",
                "module": "viin_sale",
                "is_definition": False,
                "field_count": 12,
                "odoo_version": "17.0",
            },
        },
        {
            "id": "sale.order:sale_management",
            "type": "default",
            "position": {"x": 520, "y": 300},
            "data": {
                "label": "sale.order\n(sale_management)",
                "module": "sale_management",
                "is_definition": False,
                "field_count": 5,
                "odoo_version": "17.0",
            },
        },
    ],
    "edges": [
        {
            "id": "e-viin_sale-sale",
            "source": "sale.order:viin_sale",
            "target": "sale.order:sale",
            "type": "inherits",
            "label": "INHERITS",
            "data": {"order": 1},
        },
        {
            "id": "e-sale_management-sale",
            "source": "sale.order:sale_management",
            "target": "sale.order:sale",
            "type": "inherits",
            "label": "INHERITS",
            "data": {"order": 2},
        },
    ],
}


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _layout_nodes(
    nodes: list[dict[str, Any]],
    definition_id: str | None,
) -> list[dict[str, Any]]:
    """Assign simple radial positions: definition node centre, others in a ring."""
    if not nodes:
        return nodes

    # Find definition (root)
    root_idx = next(
        (i for i, n in enumerate(nodes) if n["id"] == definition_id or n["data"].get("is_definition")),
        0,
    )
    result = list(nodes)
    root = result[root_idx]
    root["position"] = {"x": 300, "y": 200}

    others = [n for i, n in enumerate(result) if i != root_idx]
    count = len(others)
    radius = max(160, count * 50)
    for i, node in enumerate(others):
        angle = (2 * math.pi * i / count) - math.pi / 2 + math.pi / 4
        x = 300 + radius * math.cos(angle)
        y = 200 + radius * math.sin(angle)
        node["position"] = {"x": round(x), "y": round(y)}

    return result


# ---------------------------------------------------------------------------
# Neo4j query
# ---------------------------------------------------------------------------

CYPHER_QUERY = """
MATCH (mod:Module)-[:DEFINES]->(m:Model {name: $model_name, odoo_version: $odoo_version})
OPTIONAL MATCH (m)<-[inh:INHERITS*0..$depth]-(child_m:Model)<-[:DEFINES]-(child_mod:Module)
WHERE child_m.odoo_version = $odoo_version
RETURN
    mod.name        AS root_module,
    m.field_count   AS root_field_count,
    m.method_count  AS root_method_count,
    m.is_definition AS root_is_definition,
    collect(DISTINCT {
        model_name:   child_m.name,
        module:       child_mod.name,
        field_count:  child_m.field_count,
        is_private:   child_mod.is_private
    }) AS inheritors
"""


def _query_neo4j(
    uri: str,
    user: str,
    password: str,
    model_name: str,
    odoo_version: str,
    depth: int,
    include_private: bool,
) -> dict[str, Any] | None:
    """Query Neo4j and return graph snapshot dict. Returns None on failure."""
    try:
        from neo4j import GraphDatabase  # type: ignore[import]
    except ImportError:
        print("[warn] neo4j driver not installed — using fallback", file=sys.stderr)
        return None

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
    except Exception as exc:
        print(f"[warn] Neo4j connection failed ({exc}) — using fallback", file=sys.stderr)
        return None

    try:
        with driver.session() as session:
            result = session.run(
                CYPHER_QUERY,
                model_name=model_name,
                odoo_version=odoo_version,
                depth=depth,
            )
            rows = result.data()
    except Exception as exc:
        print(f"[warn] Query failed ({exc}) — using fallback", file=sys.stderr)
        driver.close()
        return None
    finally:
        try:
            driver.close()
        except Exception:
            pass

    if not rows:
        print("[warn] Query returned no rows — using fallback", file=sys.stderr)
        return None

    row = rows[0]
    root_module: str = row["root_module"]
    root_field_count: int = row.get("root_field_count") or 0
    root_method_count: int = row.get("root_method_count") or 0
    root_is_definition: bool = bool(row.get("root_is_definition", True))
    inheritors: list[dict[str, Any]] = row.get("inheritors") or []

    # Build nodes
    root_id = f"{model_name}:{root_module}"
    nodes: list[dict[str, Any]] = [
        {
            "id": root_id,
            "type": "default",
            "position": {"x": 0, "y": 0},
            "data": {
                "label": f"{model_name}\n({root_module})",
                "module": root_module,
                "is_definition": root_is_definition,
                "field_count": root_field_count,
                "method_count": root_method_count,
                "odoo_version": odoo_version,
            },
        }
    ]

    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {root_id}

    for idx, inh in enumerate(inheritors):
        if not isinstance(inh, dict):
            continue
        mod = inh.get("module")
        if not mod or mod == root_module:
            continue
        if not include_private and inh.get("is_private"):
            continue

        node_id = f"{model_name}:{mod}"
        if node_id in seen_ids:
            continue
        seen_ids.add(node_id)

        nodes.append(
            {
                "id": node_id,
                "type": "default",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": f"{model_name}\n({mod})",
                    "module": mod,
                    "is_definition": False,
                    "field_count": inh.get("field_count") or 0,
                    "odoo_version": odoo_version,
                },
            }
        )
        edges.append(
            {
                "id": f"e-{mod}-{root_module}-{idx}",
                "source": node_id,
                "target": root_id,
                "type": "inherits",
                "label": "INHERITS",
                "data": {"order": idx + 1},
            }
        )

    nodes = _layout_nodes(nodes, root_id)

    return {
        "_meta": {
            "generated_by": "scripts/dump_graph_snippet.py",
            "model": model_name,
            "odoo_version": odoo_version,
            "source": "neo4j",
            "include_private": include_private,
        },
        "nodes": nodes,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bake graph-snapshot.json from Neo4j for the landing page hero.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        default="site/public/graph-snapshot.json",
        help="Output file path (default: site/public/graph-snapshot.json)",
    )
    parser.add_argument(
        "--model",
        default="sale.order",
        help="Model name to query (default: sale.order)",
    )
    parser.add_argument(
        "--version",
        default="17.0",
        dest="odoo_version",
        help="Odoo version (default: 17.0)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="INHERITS traversal depth (default: 2)",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        default=False,
        help="Include EE/private modules (default: CE only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print JSON to stdout, do not write file",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=False,
        help="Pretty-print JSON output",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_pass = os.environ.get("NEO4J_PASS", "neo4j")

    snapshot = _query_neo4j(
        uri=neo4j_uri,
        user=neo4j_user,
        password=neo4j_pass,
        model_name=args.model,
        odoo_version=args.odoo_version,
        depth=args.depth,
        include_private=args.include_private,
    )

    if snapshot is None:
        print("[info] Using placeholder fallback snapshot", file=sys.stderr)
        snapshot = dict(PLACEHOLDER_SNAPSHOT)

    # Summarise
    node_count = len(snapshot.get("nodes", []))
    edge_count = len(snapshot.get("edges", []))
    source = snapshot.get("_meta", {}).get("source", "?")
    print(
        f"[info] snapshot ready — source={source} nodes={node_count} edges={edge_count}",
        file=sys.stderr,
    )

    indent = 2 if (args.pretty or args.dry_run) else None
    json_str = json.dumps(snapshot, indent=indent, ensure_ascii=False)

    if args.dry_run:
        print(json_str)
        return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json_str + "\n", encoding="utf-8")
    print(f"[ok] Written to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
