# src/mcp/server.py
import os

from dotenv import load_dotenv
from fastmcp import FastMCP
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

mcp = FastMCP("odoo-semantic")
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def _latest_version(session) -> str:
    rec = session.run("""
        MATCH (m:Module)
        WITH DISTINCT m.odoo_version AS v
        RETURN v ORDER BY toFloat(v) DESC LIMIT 1
    """).single()
    return rec["v"] if rec else "17.0"


def _resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        layers = session.run("""
            MATCH (m:Model {name: $name, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
            RETURN m.module AS module_name, mod.repo AS repo
            ORDER BY COUNT { ()-[:INHERITS]->(m) } ASC
        """, name=model_name, v=odoo_version).data()

        if not layers:
            return f"Không tìm thấy model '{model_name}' trong Odoo {odoo_version}."

        base = layers[0]
        extensions = layers[1:]

        parents = session.run("""
            MATCH (:Model {name: $name, odoo_version: $v})-[r:INHERITS]->(p:Model)
            WHERE p.name <> $name
              AND NOT coalesce(r.unresolved, false)
            OPTIONAL MATCH (p)-[:DEFINED_IN]->(mod:Module)
            RETURN DISTINCT p.name AS pname, mod.name AS module_name
        """, name=model_name, v=odoo_version).data()

        fields_count = session.run(
            "MATCH (f:Field {model: $n, odoo_version: $v}) RETURN count(f) AS c",
            n=model_name, v=odoo_version
        ).single()["c"]

        methods_count = session.run(
            "MATCH (m:Method {model: $n, odoo_version: $v}) RETURN count(m) AS c",
            n=model_name, v=odoo_version
        ).single()["c"]

    lines = [f"{model_name} (Odoo {odoo_version})"]
    lines.append(f"├─ Định nghĩa tại: [{base['repo']}] {base['module_name']}")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Kế thừa từ:    {parents_str}")

    if extensions:
        lines.append("├─ Mở rộng bởi:")
        for ext in extensions:
            lines.append(f"│   └─ [{ext['repo']}] {ext['module_name']}")

    lines.append(f"├─ Tổng số field:  {fields_count}")
    lines.append(f"└─ Tổng số method: {methods_count}")
    return "\n".join(lines)


def _resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        records = session.run("""
            MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: f.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $mn, module: f.module, odoo_version: $v})
            RETURN f, f.module AS module_name, mod.repo AS repo,
                   COUNT { ()-[:INHERITS]->(m_node) } AS depth
            ORDER BY depth ASC
        """, fn=field_name, mn=model_name, v=odoo_version).data()

    if not records:
        return (
            f"Không tìm thấy field '{field_name}' trên model"
            f" '{model_name}' trong Odoo {odoo_version}."
        )

    base_f = records[0]["f"]
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Loại:     {base_f.get('ttype', '?')}",
        f"├─ Computed: {'Có' if base_f.get('compute') else 'Không'}"
        + (f" ({base_f['compute']})" if base_f.get('compute') else ""),
        f"├─ Stored:   {'Có' if base_f.get('stored', True) else 'Không'}",
        f"├─ Required: {'Có' if base_f.get('required', False) else 'Không'}",
        f"├─ Related:  {base_f.get('related') or '—'}",
        "└─ Khai báo trong:",
    ]
    for r in records:
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        lines.append(f"    └─ {repo_str}{r['module_name']}")
    return "\n".join(lines)


def _resolve_method(model_name: str, method_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        records = session.run("""
            MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $model, module: mth.module, odoo_version: $v})
            RETURN mth, mth.module AS module_name, mod.repo AS repo,
                   COUNT { ()-[:INHERITS]->(m_node) } AS depth
            ORDER BY depth ASC
        """, mn=method_name, model=model_name, v=odoo_version).data()

    if not records:
        return (
            f"Không tìm thấy method '{method_name}' trên model"
            f" '{model_name}' trong Odoo {odoo_version}."
        )

    lines = [f"{model_name}.{method_name}() (Odoo {odoo_version})", "Override chain:"]
    for r in records:
        mth = r["mth"]
        super_info = "✓ gọi super()" if mth.get("has_super_call") else "✗ không gọi super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        lines.append(f"  {repo_str}{r['module_name']} — {super_info} — decorators: {decs}")
    return "\n".join(lines)


@mcp.tool()
def resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    """Trả về thông tin đầy đủ về Odoo model: inheritance chain, field summary, method summary."""
    return _resolve_model(model_name, odoo_version)


@mcp.tool()
def resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    """Trả về chi tiết một field: type, computed/related metadata, module nguồn."""
    return _resolve_field(model_name, field_name, odoo_version)


@mcp.tool()
def resolve_method(model_name: str, method_name: str, odoo_version: str = "auto") -> str:
    """Trả về override chain của một method theo thứ tự base→top."""
    return _resolve_method(model_name, method_name, odoo_version)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8002, path="/mcp")
