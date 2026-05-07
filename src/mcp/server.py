# src/mcp/server.py
import math
import os

import psycopg2
from fastmcp import FastMCP
from neo4j import GraphDatabase

mcp = FastMCP("odoo-semantic")
_driver = None
_pg_conn = None
_embedder_instance = None


def _get_driver():
    global _driver
    if _driver is None:
        from src import config
        # os.getenv returns None when var is unset; "" also falls through to config — intentional
        uri = (
            os.getenv("NEO4J_URI")
            or config.get("database", "neo4j_uri", fallback="bolt://localhost:7687")
        )
        user = (
            os.getenv("NEO4J_USER")
            or config.get("database", "neo4j_user", fallback="neo4j")
        )
        password = (
            os.getenv("NEO4J_PASSWORD")
            or config.get("database", "neo4j_password", fallback="password")
        )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver


def _get_pg_conn():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        from pgvector.psycopg2 import register_vector

        from src import config
        dsn = (
            os.getenv("PG_DSN")
            or config.get("database", "pg_dsn",
                          fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic")
        )
        _pg_conn = psycopg2.connect(dsn)
        register_vector(_pg_conn)
    return _pg_conn


def _get_embedder():
    global _embedder_instance
    if _embedder_instance is None:
        from src import config
        from src.indexer.embedder import Qwen3Embedder
        url = (
            os.getenv("EMBEDDER_URL")
            or config.get("embedder", "url", fallback="http://localhost:11434")
        )
        model = (
            os.getenv("EMBEDDER_MODEL")
            or config.get("embedder", "model", fallback="qwen3-embedding-q5km")
        )
        dim = int(
            os.getenv("EMBEDDER_DIM") or config.get("embedder", "dim", fallback="1024")
        )
        _embedder_instance = Qwen3Embedder(url, model, dim=dim)
    return _embedder_instance


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
            return f"Model '{model_name}' not found in Odoo {odoo_version}."

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
    lines.append(f"├─ Defined in:     [{base['repo']}] {base['module_name']}")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Inherits from:  {parents_str}")

    if extensions:
        lines.append("├─ Extended by:")
        for ext in extensions:
            lines.append(f"│   └─ [{ext['repo']}] {ext['module_name']}")

    lines.append(f"├─ Fields:         {fields_count}")
    lines.append(f"└─ Methods:        {methods_count}")
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
            f"Field '{field_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}."
        )

    base_f = records[0]["f"]
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Type:     {base_f.get('ttype', '?')}",
        f"├─ Computed: {'Yes' if base_f.get('compute') else 'No'}"
        + (f" ({base_f['compute']})" if base_f.get('compute') else ""),
        f"├─ Stored:   {'Yes' if base_f.get('stored', True) else 'No'}",
        f"├─ Required: {'Yes' if base_f.get('required', False) else 'No'}",
        f"├─ Related:  {base_f.get('related') or '—'}",
        "└─ Declared in:",
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
            f"Method '{method_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}."
        )

    lines = [f"{model_name}.{method_name}() (Odoo {odoo_version})", "Override chain:"]
    for r in records:
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        lines.append(f"  {repo_str}{r['module_name']} — {super_info} — decorators: {decs}")
    return "\n".join(lines)


def _resolve_view(xmlid: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        view_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            OPTIONAL MATCH (v)-[:DEFINED_IN]->(mod:Module)
            RETURN v, mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version).single()

        if not view_rec:
            return f"View '{xmlid}' not found in Odoo {odoo_version}."

        parent_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
                  -[r:INHERITS_VIEW]->(parent:View {odoo_version: $ver})
            WHERE NOT coalesce(r.unresolved, false)
            RETURN parent.xmlid AS parent_xmlid
        """, xmlid=xmlid, ver=odoo_version).single()

        extensions = session.run("""
            MATCH (ext:View {odoo_version: $ver})-[:INHERITS_VIEW]->
                  (v:View {xmlid: $xmlid, odoo_version: $ver})
            WHERE NOT coalesce(ext.unresolved, false)
            OPTIONAL MATCH (ext)-[:DEFINED_IN]->(mod:Module)
            RETURN ext.xmlid AS ext_xmlid,
                   ext.xpaths_exprs AS xpaths_exprs,
                   ext.xpaths_positions AS xpaths_positions,
                   mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version).data()

    v_props = view_rec["v"]
    repo_str = f"[{view_rec['repo']}] " if view_rec.get("repo") else ""
    mode_label = " (extension)" if v_props.get("mode") == "extension" else ""

    lines = [f"{xmlid} (Odoo {odoo_version})"]
    lines.append(f"├─ Type:   {v_props.get('type', '?')}")
    lines.append(f"├─ Model:  {v_props.get('model', '?')}")
    lines.append(f"├─ Module: {repo_str}{view_rec.get('module_name', '?')}{mode_label}")

    if parent_rec:
        lines.append(f"├─ Inherits from: {parent_rec['parent_xmlid']}")
        own_exprs = list(v_props.get("xpaths_exprs") or [])
        own_positions = list(v_props.get("xpaths_positions") or [])
        if own_exprs:
            lines.append(f"├─ XPath modifications ({len(own_exprs)}):")
            for expr, pos in zip(own_exprs, own_positions):
                lines.append(f"│   ├─ {expr} [{pos}]")

    if extensions:
        lines.append(f"└─ Extended by ({len(extensions)} modules):")
        for i, ext in enumerate(extensions):
            ext_repo = f"[{ext['repo']}] " if ext.get("repo") else ""
            connector = "    └─" if i == len(extensions) - 1 else "    ├─"
            lines.append(
                f"{connector} {ext['ext_xmlid']}  →  {ext_repo}{ext.get('module_name', '?')}"
            )
            exprs = list(ext.get("xpaths_exprs") or [])
            positions = list(ext.get("xpaths_positions") or [])
            for expr, pos in zip(exprs, positions):
                lines.append(f"    │   └─ xpath: {expr} [{pos}]")
    else:
        lines.append("└─ No extensions")

    return "\n".join(lines)


def _find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
) -> str:
    if not query.strip():
        return "find_examples: query rỗng — hãy nhập mô tả tính năng cần tìm\nFound 0 results\n"

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    pg = _pg_conn or _get_pg_conn()
    driver = _driver or _get_driver()
    embedder = _embedder or _get_embedder()

    with driver.session() as session:
        if odoo_version in ("auto", "latest"):
            odoo_version = _latest_version(session)

    query_vec = embedder.embed([INSTRUCT_NL_TO_CODE + query])[0]

    VALID_TYPES = {"method", "field", "view", "qweb", "js_era1", "js_era2", "js_era3"}
    selected_types = [t for t in (chunk_types or []) if t in VALID_TYPES]

    with pg.cursor() as cur:
        if selected_types:
            placeholders = ",".join(["%s"] * len(selected_types))
            cur.execute(
                f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                           chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine
                    FROM embeddings
                    WHERE odoo_version = %s AND chunk_type IN ({placeholders})
                    ORDER BY vec <=> %s::vector LIMIT 20""",
                [query_vec, odoo_version] + selected_types + [query_vec],
            )
        else:
            cur.execute(
                """SELECT chunk_type, module, entity_name, model_name, file_path,
                          chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine
                   FROM embeddings WHERE odoo_version = %s
                   ORDER BY vec <=> %s::vector LIMIT 20""",
                [query_vec, odoo_version, query_vec],
            )
        raw = [
            dict(chunk_type=r[0], module=r[1], entity_name=r[2], model_name=r[3],
                 file_path=r[4], chunk_idx=r[5], content=r[6], cosine=float(r[7]))
            for r in cur.fetchall()
        ]

    raw = [c for c in raw if c["module"] != "__unresolved__"]

    # Neo4j centrality rerank + optional context_module boost.
    # Two UNWIND batch queries replace the previous N+1 per-chunk loop.
    # Coefficients are v0 placeholders — tune at M6 with held-out eval set.
    module_names = list({c["module"] for c in raw})
    with driver.session() as session:
        dep_rows = session.run(
            "UNWIND $names AS name"
            " MATCH (m:Module {name: name, odoo_version: $v})"
            " WITH m, name"
            " OPTIONAL MATCH (dep)-[:DEPENDS_ON]->(m)"
            " RETURN name, count(dep) AS dependents",
            names=module_names, v=odoo_version,
        ).data()
        dependents_map = {r["name"]: r["dependents"] for r in dep_rows}

        in_chain_set: set[str] = set()
        if context_module and module_names:
            chain_rows = session.run(
                "MATCH (ctx:Module {name: $ctx, odoo_version: $v})"
                " -[:DEPENDS_ON*1..]->(tgt:Module)"
                " WHERE tgt.name IN $names"
                " RETURN DISTINCT tgt.name AS name",
                ctx=context_module, v=odoo_version, names=module_names,
            ).data()
            in_chain_set = {r["name"] for r in chain_rows}

    for chunk in raw:
        dependents = dependents_map.get(chunk["module"], 0)
        chunk["score"] = chunk["cosine"] * (1 + 0.02 * math.log(dependents + 1))
        if chunk["module"] in in_chain_set:
            chunk["score"] += 0.20

    reranked = sorted(raw, key=lambda c: c["score"], reverse=True)[:limit]

    header = f'find_examples: "{query}" ({odoo_version})\nFound {len(reranked)} results\n'
    if not reranked:
        return header

    sep = "─" * 41
    lines = [header]
    for i, chunk in enumerate(reranked, 1):
        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
        # For view chunks, show the model so readers know which UI the view belongs to
        if chunk["model_name"] and chunk["chunk_type"] == "view":
            entity += f" (model: {chunk['model_name']})"
        # For sliding-window chunks, show the window index so readers know it's a partial
        chunk_label = chunk["chunk_type"]
        if chunk["chunk_idx"] > 0:
            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
        lines.append(sep)
        lines.append(f"#{i} · score {chunk['score']:.2f} · {chunk_label} · {entity}")
        lines.append(f"   File: {chunk['file_path']}")
        lines.append("   ┌" + "─" * 42)
        for line in chunk["content"].splitlines():
            lines.append(f"   │ {line}")
        lines.append("   └" + "─" * 42)
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    """Return full info for an Odoo model: inheritance chain, field summary, method summary."""
    return _resolve_model(model_name, odoo_version)


@mcp.tool()
def resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    """Return field details: type, computed/related metadata, declaring module."""
    return _resolve_field(model_name, field_name, odoo_version)


@mcp.tool()
def resolve_method(model_name: str, method_name: str, odoo_version: str = "auto") -> str:
    """Return override chain of a method, ordered base→top."""
    return _resolve_method(model_name, method_name, odoo_version)


@mcp.tool()
def resolve_view(xmlid: str, odoo_version: str = "auto") -> str:
    """Return view inheritance chain and XPath modifications from all extension modules."""
    return _resolve_view(xmlid, odoo_version)


@mcp.tool()
def find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
) -> str:
    """Tìm code examples từ codebase Odoo theo mô tả ngôn ngữ tự nhiên.

    Args:
        query: Mô tả tính năng cần tìm (VN hoặc EN).
        odoo_version: Version Odoo (ví dụ "17.0"). Mặc định "auto" = version mới nhất được index.
        limit: Số kết quả trả về (mặc định 5, tối đa 20).
        context_module: Module đang làm việc. Kết quả từ các module mà module này depends on
            được ưu tiên cao hơn (+0.20 score boost).
        chunk_types: Lọc theo loại code. Giá trị hợp lệ: method, field, view, qweb,
            js_era1, js_era2, js_era3. Mặc định: tất cả loại.
    """
    return _find_examples(query, odoo_version, limit, context_module, chunk_types)


def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=_mcp_host(),
            port=_mcp_port(), path="/mcp")
