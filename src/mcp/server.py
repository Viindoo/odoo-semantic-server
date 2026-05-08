# src/mcp/server.py
import math

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
        # Resolution order per from_env_or_ini: env var → INI → fallback
        uri = config.from_env_or_ini(
            "NEO4J_URI", "database", "neo4j_uri",
            fallback="bolt://localhost:7687",
        )
        user = config.from_env_or_ini(
            "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
        )
        password = config.from_env_or_ini(
            "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
        )
        if not password:
            raise RuntimeError(
                "Neo4j password missing. Set NEO4J_PASSWORD env var OR "
                "neo4j_password in [database] section of odoo-semantic.conf."
            )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver


def _get_pg_conn():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        from pgvector.psycopg2 import register_vector

        from src import config
        dsn = config.from_env_or_ini(
            "PG_DSN", "database", "pg_dsn", fallback=None,
        )
        if not dsn:
            raise RuntimeError(
                "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
                "in [database] section of odoo-semantic.conf."
            )
        _pg_conn = psycopg2.connect(dsn)
        register_vector(_pg_conn)
    return _pg_conn


def _get_embedder():
    global _embedder_instance
    if _embedder_instance is None:
        from src import config
        from src.indexer.embedder import Qwen3Embedder
        url = config.from_env_or_ini(
            "EMBEDDER_URL", "embedder", "url",
            fallback="http://localhost:11434",
        )
        model = config.from_env_or_ini(
            "EMBEDDER_MODEL", "embedder", "model",
            fallback="qwen3-embedding-q5km",
        )
        dim_str = config.from_env_or_ini(
            "EMBEDDER_DIM", "embedder", "dim", fallback="1024",
        )
        _embedder_instance = Qwen3Embedder(url, model, dim=int(dim_str))
    return _embedder_instance


def _latest_version(session) -> str | None:
    """Return the latest Odoo version present in the index, by NUMERIC compare.

    Filters:
      - excludes 'unknown' and any non-semver-shaped string (must match `\\d+\\.\\d+`)
      - sorts by `toInteger(split(v,'.')[0])` then minor — handles 9.0 < 17.0 correctly
        (lexicographic compare would put '9.0' > '17.0', a Neo4j 5.x gotcha — see
        project CLAUDE.md).

    Returns None when no indexed data exists (no hardcoded fallback). Callers
    should surface a clear error instructing the user to run the indexer.
    """
    rec = session.run("""
        MATCH (m:Module)
        WITH DISTINCT m.odoo_version AS v
        WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
        RETURN v
        ORDER BY toInteger(split(v, '.')[0]) DESC,
                 toInteger(split(v, '.')[1]) DESC
        LIMIT 1
    """).single()
    return rec["v"] if rec else None


def _resolve_version(version_arg: str, session) -> str:
    """Translate a user-facing version arg ('auto' or explicit) into a concrete version.

    Raises ValueError when 'auto' is requested but the index is empty.
    Explicit versions pass through unchanged.
    """
    if version_arg != "auto":
        return version_arg
    v = _latest_version(session)
    if v is None:
        raise ValueError(
            "No data indexed. Run `python -m src.indexer --profile <name>` first."
        )
    return v


def _resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

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
        odoo_version = _resolve_version(odoo_version, session)

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
        odoo_version = _resolve_version(odoo_version, session)

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
        odoo_version = _resolve_version(odoo_version, session)

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
    try:
        embedder = _embedder or _get_embedder()
    except Exception as e:
        return (
            f"find_examples: embedder unavailable — {type(e).__name__}: {e}\n"
            "Hint: check Ollama server is running (default: http://localhost:11434) "
            "and EMBEDDER_MODEL is loaded.\nFound 0 results\n"
        )

    with driver.session() as session:
        if odoo_version in ("auto", "latest"):
            odoo_version = _resolve_version("auto", session)

    try:
        query_vec = embedder.embed([INSTRUCT_NL_TO_CODE + query])[0]
    except Exception as e:
        return (
            f"find_examples: embedding query failed — {type(e).__name__}: {e}\n"
            "Hint: Ollama may be down, model not loaded, or network issue. "
            "Verify with: curl http://localhost:11434/api/tags\n"
            "Found 0 results\n"
        )

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
    """Return full info for an Odoo model: inheritance chain, field summary, method summary.

    Args:
        model_name: Odoo dotted name, e.g. 'sale.order', 'res.partner'.
        odoo_version: e.g. '17.0', '18.0'. Default 'auto' = latest indexed version.

    Returns:
        Tree-formatted text. Use to discover which modules extend a model
        before adding a new override.

    Example:
        resolve_model("sale.order", "17.0")
        →  sale.order (Odoo 17.0)
           ├─ Base module: [odoo] sale
           ├─ Extended by:
           │   ├─ [viin_sale]
           │   └─ [to_sale_custom]
           ├─ Fields: 47   Methods: 23
    """
    return _resolve_model(model_name, odoo_version)


@mcp.tool()
def resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    """Return field details: type, computed/related metadata, declaring module.

    Args:
        model_name: e.g. 'sale.order'.
        field_name: e.g. 'amount_total'.
        odoo_version: e.g. '17.0'. Default 'auto'.

    Returns:
        Tree-formatted text including all extension layers, compute method,
        related path, store flag, source snippet. Use before changing a field
        to discover all writers/readers across modules.

    Example:
        resolve_field("sale.order", "amount_total", "17.0")
        → sale.order.amount_total (Odoo 17.0)
          Type: monetary | Computed: _compute_amounts | Stored: Yes
          Defined in:
          ├─ [odoo] sale          ← base
          └─ [viin_sale]          ← override
    """
    return _resolve_field(model_name, field_name, odoo_version)


@mcp.tool()
def resolve_method(model_name: str, method_name: str, odoo_version: str = "auto") -> str:
    """Return override chain of a method, ordered base→top.

    Args:
        model_name: e.g. 'sale.order'.
        method_name: e.g. 'action_confirm'.
        odoo_version: e.g. '17.0'. Default 'auto'.

    Returns:
        Override chain (base → topmost). Use before super()-overriding
        to know which modules already extend the method and in what order.

    Example:
        resolve_method("sale.order", "action_confirm", "17.0")
        → sale.order.action_confirm (Odoo 17.0)
          Override chain (base → top):
          1. [odoo] sale            (base, no super)
          2. [viin_sale]            (calls super)
          3. [to_sale_workflow]     (calls super)
    """
    return _resolve_method(model_name, method_name, odoo_version)


@mcp.tool()
def resolve_view(xmlid: str, odoo_version: str = "auto") -> str:
    """Return view inheritance chain and XPath modifications from all extension modules.

    Args:
        xmlid: External ID of the view, e.g. 'sale.view_order_form'.
        odoo_version: e.g. '17.0'. Default 'auto'.

    Returns:
        View tree + XPath operations applied by each extending module.
        Use before adding XPath override to avoid conflicts with existing patches.

    Example:
        resolve_view("sale.view_order_form", "17.0")
        → sale.view_order_form (form view of sale.order, Odoo 17.0)
          Base in [odoo] sale
          Extensions (in apply order):
          1. [viin_sale] adds 3 xpath ops (after, before, replace)
          2. [to_sale_custom] adds 1 xpath op (after //field[@name='partner_id'])
    """
    return _resolve_view(xmlid, odoo_version)


@mcp.tool()
def find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
) -> str:
    """Tìm code examples từ codebase Odoo theo mô tả ngôn ngữ tự nhiên (semantic search).

    Yêu cầu Ollama đang chạy với model `qwen3-embedding-q5km` (xem README §Tool dependencies).

    Args:
        query: Mô tả tính năng cần tìm (VN hoặc EN).
        odoo_version: Version Odoo (ví dụ "17.0"). Mặc định "auto" = version mới nhất được index.
        limit: Số kết quả trả về (mặc định 5, tối đa 20).
        context_module: Module đang làm việc. Kết quả từ các module mà module này depends on
            được ưu tiên cao hơn (+0.20 score boost).
        chunk_types: Lọc theo loại code. Giá trị hợp lệ: method, field, view, qweb,
            js_era1, js_era2, js_era3. Mặc định: tất cả loại.

    Returns:
        Header + N kết quả ranked theo cosine + centrality + context boost.
        Mỗi kết quả: score, type, module, entity, file path, content snippet.

    Example:
        find_examples("xác nhận đơn bán và gửi email cho khách", "17.0", limit=3)
        → find_examples: "xác nhận đơn bán và gửi email cho khách" (17.0)
          Found 3 results
          ─────────────
          #1 · score 0.82 · method · [sale] sale.order.action_confirm
             File: sale/models/sale_order.py
             ┌────────
             │ def action_confirm(self):
             │     ...
    """
    return _find_examples(query, odoo_version, limit, context_module, chunk_types)


def _compute_risk(view_count: int, method_count: int, js_count: int) -> str:
    """Risk threshold v0 — tunable at M6.

    HIGH >= 10 affected entities, MEDIUM 4-9, LOW < 4.
    # Thresholds calibrated qualitatively against Odoo 17 + Viindoo addons typical fan-out:
    # <4 changes = isolated, 4-9 = module-scope review needed, ≥10 = cross-module impact
    # requiring full regression. M6 will recalibrate against held-out eval set.
    """
    total = view_count + method_count + js_count
    if total >= 10:
        return "HIGH"
    if total >= 4:
        return "MEDIUM"
    return "LOW"


def _impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: str = "auto",
) -> str:
    """Return everything affected by changing the given entity. Risk-scored."""
    valid_types = ("field", "method", "model")
    if entity_type not in valid_types:
        return (
            f"Invalid entity_type '{entity_type}'. Use: field, method, model."
        )

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # ------------------------------------------------------------------ #
        # Parse entity_name per entity_type                                   #
        # ------------------------------------------------------------------ #
        if entity_type in ("field", "method"):
            if "." not in entity_name:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}. "
                    f"Expected format: '<model>.<{entity_type}>' "
                    f"(e.g. 'sale.order.amount_total' for a field)."
                )
            # Split on LAST dot: model has dots, field/method does not
            last_dot = entity_name.rfind(".")
            model_name = entity_name[:last_dot]
            member_name = entity_name[last_dot + 1:]
        else:
            # entity_type == "model"
            model_name = entity_name
            member_name = None

        # ------------------------------------------------------------------ #
        # Query 1: verify entity exists                                        #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            exists = session.run(
                "MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v}) "
                "RETURN count(f) AS c",
                fn=member_name, mn=model_name, v=odoo_version,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        elif entity_type == "method":
            exists = session.run(
                "MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v}) "
                "RETURN count(mth) AS c",
                mn=member_name, model=model_name, v=odoo_version,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        else:  # model
            exists = session.run(
                "MATCH (m:Model {name: $mn, odoo_version: $v}) "
                "WHERE coalesce(m.unresolved, false) = false "
                "AND m.module <> '__unresolved__' "
                "RETURN count(m) AS c",
                mn=model_name, v=odoo_version,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )

        # ------------------------------------------------------------------ #
        # Query 2: views targeting model (DISTINCT to avoid TARGETS_MODEL fan-out)
        # ------------------------------------------------------------------ #
        views = session.run("""
            MATCH (m:Model {name: $mn, odoo_version: $v})<-[:TARGETS_MODEL]-(view:View)
            RETURN DISTINCT view.xmlid AS xmlid, view.module AS module
            ORDER BY view.module, view.xmlid
        """, mn=model_name, v=odoo_version).data()

        # ------------------------------------------------------------------ #
        # Query 3: methods on this model (with super call filter for field;   #
        #          all overrides for method entity_type)                       #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            methods = session.run("""
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE mth.has_super_call = true
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, mn=model_name, v=odoo_version).data()
        elif entity_type == "method":
            methods = session.run("""
                MATCH (mth:Method {name: $mn2, model: $mn, odoo_version: $v})
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module
            """, mn2=member_name, mn=model_name, v=odoo_version).data()
        else:  # model
            methods = session.run("""
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, mn=model_name, v=odoo_version).data()

        # ------------------------------------------------------------------ #
        # Query 4: JS patches on components bound to this model               #
        # ------------------------------------------------------------------ #
        js_patches = session.run("""
            MATCH (m:Model {name: $mn, odoo_version: $v})<-[:BOUND_TO]-(comp:OWLComp)
                  <-[:PATCHES]-(jp:JSPatch)
            RETURN DISTINCT jp.target AS target, jp.patch_name AS patch_name,
                   jp.module AS module, jp.era AS era
            ORDER BY jp.module, jp.target
        """, mn=model_name, v=odoo_version).data()

        # ------------------------------------------------------------------ #
        # Query 5: dependent modules of all modules defining this model       #
        # ------------------------------------------------------------------ #
        dep_modules = session.run("""
            MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(defmod:Module)
                  <-[:DEPENDS_ON]-(depmod:Module)
            RETURN DISTINCT depmod.name AS dep_name
            ORDER BY depmod.name
        """, mn=model_name, v=odoo_version).data()

        # For model entity_type: also collect defining modules as "extensions"
        if entity_type == "model":
            def_modules = session.run("""
                MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
                RETURN DISTINCT m.module AS module_name
                ORDER BY m.module
            """, mn=model_name, v=odoo_version).data()
        else:
            def_modules = []

    # ---------------------------------------------------------------------- #
    # Build output tree                                                        #
    # ---------------------------------------------------------------------- #
    view_count = len(views)
    method_count = len(methods)
    js_count = len(js_patches)
    total = view_count + method_count + js_count
    risk = _compute_risk(view_count, method_count, js_count)

    lines = [f"impact_analysis({entity_type}, {entity_name}, {odoo_version})"]
    lines.append(f"├─ Risk: {risk} ({total} affected entities)")

    # Views section
    if views:
        lines.append(f"├─ Views ({view_count}):")
        for i, v_item in enumerate(views):
            connector = "└─" if i == view_count - 1 else "├─"
            lines.append(f"│   {connector} [{v_item['module']}] {v_item['xmlid']}")
    else:
        lines.append("├─ Views: none")

    # Methods section
    if entity_type == "field":
        methods_label = (
            f"Methods on {model_name} with super() ({method_count})"
            f" — field-level filter not yet implemented (M5)"
        )
    elif entity_type == "method":
        methods_label = "Override chain"
    else:
        methods_label = "Methods"

    if entity_type == "field":
        # For field: use pre-built label that already contains count
        if methods:
            lines.append(f"├─ {methods_label}:")
            for i, m_item in enumerate(methods):
                connector = "└─" if i == method_count - 1 else "├─"
                lines.append(f"│   {connector} [{m_item['module']}] {m_item['name']}")
        else:
            lines.append(f"├─ {methods_label}: none")
        lines.append(
            "│   Note: field-level impact requires F4 USES_FIELD edge"
            " (deferred to M5). Current scope: model-level."
        )
    elif methods:
        lines.append(f"├─ {methods_label} ({method_count}):")
        for i, m_item in enumerate(methods):
            connector = "└─" if i == method_count - 1 else "├─"
            lines.append(f"│   {connector} [{m_item['module']}] {m_item['name']}")
    else:
        lines.append(f"├─ {methods_label}: none")

    # JS patches section
    if js_patches:
        lines.append(f"├─ JS patches ({js_count}):")
        for i, jp in enumerate(js_patches):
            connector = "└─" if i == js_count - 1 else "├─"
            lines.append(
                f"│   {connector} [{jp['module']}] {jp['target']}"
                f" via {jp['patch_name']} (era: {jp['era']})"
            )
    else:
        lines.append("├─ JS patches: none")

    # For model entity_type: extension modules section
    if entity_type == "model" and def_modules:
        mod_names = [d["module_name"] for d in def_modules]
        lines.append(f"├─ Defined/extended in ({len(mod_names)}): {', '.join(mod_names)}")

    # Dependent modules section
    if dep_modules:
        dep_names = [d["dep_name"] for d in dep_modules]
        lines.append(f"└─ Dependent modules ({len(dep_names)}): {', '.join(dep_names)}")
    else:
        lines.append("└─ Dependent modules: none")

    return "\n".join(lines)


@mcp.tool()
def impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: str = "auto",
) -> str:
    """List everything affected if you change <entity>. Risk-scored (LOW/MEDIUM/HIGH).

    Args:
        entity_type: One of 'field', 'method', 'model'.
        entity_name: For field/method: '<model>.<name>' e.g. 'sale.order.amount_total'.
                     For model: '<model>' e.g. 'sale.order'.
        odoo_version: Version Odoo (e.g. '17.0'). Default 'auto' = latest indexed version.

    Returns:
        Risk score + breakdown of affected views, methods, JS patches across modules.
        Use BEFORE renaming/removing a field, signature-changing a method,
        or restructuring a model to estimate blast radius.

    Example:
        impact_analysis("field", "sale.order.amount_total", "17.0")
        → Impact of changing sale.order.amount_total (17.0): MEDIUM (7 entities)
          ├─ Views modifying field: 3 ([odoo]sale, [viin_sale], [to_sale_custom])
          ├─ Methods reading/writing: 4
          ├─ JS patches binding: 0
          └─ Recommendation: confirm with [viin_sale, to_sale_custom] owners.
    """
    return _impact_analysis(entity_type, entity_name, odoo_version)


# --- M4.5 spec layer tools ----------------------------------------------

def _format_core_symbol(rec: dict, version: str) -> str:
    """Tree-format a single CoreSymbol query record."""
    qn = rec.get("qualified_name") or "?"
    kind = rec.get("kind") or "?"
    status = rec.get("status") or "stable"
    sig = rec.get("signature")
    repl = rec.get("replacement_qname")
    file_path = rec.get("file_path")
    line = rec.get("line")

    lines = [f"{qn} (Odoo {version})"]
    lines.append(f"├─ Kind:        {kind}")
    lines.append(f"├─ Status:      {status}")
    if sig:
        lines.append(f"├─ Signature:   {sig}")
    if repl:
        lines.append(f"├─ Replacement: {repl}")
    if file_path:
        loc = file_path + (f":{line}" if line else "")
        lines.append(f"└─ Source:      {loc}")
    else:
        # Re-cap last branch as terminal
        lines[-1] = lines[-1].replace("├─", "└─")
    return "\n".join(lines)


def _lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Return signature + status + replacement for a single Odoo core API symbol."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (cs:CoreSymbol {odoo_version: $v})
            WHERE cs.qualified_name = $name
               OR cs.qualified_name ENDS WITH '.' + $name
            RETURN cs.qualified_name AS qualified_name,
                   cs.kind AS kind,
                   cs.status AS status,
                   cs.signature AS signature,
                   cs.replacement_qname AS replacement_qname,
                   cs.file_path AS file_path,
                   cs.line AS line
            ORDER BY size(cs.qualified_name) ASC
            LIMIT 1
        """, name=name, v=odoo_version).single()
    if rec is None:
        return (
            f"lookup_core_api({name!r}, {odoo_version!r})\n"
            f"└─ not found in indexed Odoo core for version {odoo_version}"
        )
    return _format_core_symbol(dict(rec), odoo_version)


def _format_api_diff(
    sym_old: dict | None,
    sym_new: dict | None,
    name: str,
    from_version: str,
    to_version: str,
) -> str:
    """Render the diff of one symbol between two versions."""
    header = f"api_version_diff({name!r}: {from_version} → {to_version})"
    lines = [header]
    if sym_old and not sym_new:
        lines.append(f"├─ Status:    removed in {to_version}")
        lines.append(f"├─ Was:       {sym_old.get('signature') or '?'}")
        repl = sym_old.get("replacement_qname")
        if repl:
            lines.append(f"└─ Replaced by: {repl}")
        else:
            lines[-1] = lines[-1].replace("├─", "└─")
        return "\n".join(lines)
    if sym_new and not sym_old:
        lines.append(f"├─ Status:    added in {to_version}")
        lines.append(f"└─ Now:       {sym_new.get('signature') or '?'}")
        return "\n".join(lines)
    # Both exist
    sig_old = sym_old.get("signature") if sym_old else None
    sig_new = sym_new.get("signature") if sym_new else None
    lines.append(f"├─ {from_version}: {sig_old or '?'} (status={sym_old.get('status')})")
    lines.append(f"├─ {to_version}: {sig_new or '?'} (status={sym_new.get('status')})")
    if sig_old and sig_new and sig_old != sig_new:
        lines.append("└─ Signature changed")
    else:
        lines.append("└─ Stable across versions")
    return "\n".join(lines)


def _fetch_core_symbol(session, name: str, version: str) -> dict | None:
    rec = session.run("""
        MATCH (cs:CoreSymbol {odoo_version: $v})
        WHERE cs.qualified_name = $name
           OR cs.qualified_name ENDS WITH '.' + $name
        RETURN cs.qualified_name AS qualified_name,
               cs.kind AS kind,
               cs.status AS status,
               cs.signature AS signature,
               cs.replacement_qname AS replacement_qname,
               cs.file_path AS file_path,
               cs.line AS line
        ORDER BY size(cs.qualified_name) ASC
        LIMIT 1
    """, name=name, v=version).single()
    return dict(rec) if rec else None


def _api_version_diff(
    symbol: str, from_version: str, to_version: str,
) -> str:
    """Diff a single API symbol between two indexed Odoo versions."""
    if from_version == to_version:
        return (
            f"api_version_diff({symbol!r}, {from_version!r}, {to_version!r})\n"
            f"└─ same version, no diff"
        )
    with _get_driver().session() as session:
        sym_old = _fetch_core_symbol(session, symbol, from_version)
        sym_new = _fetch_core_symbol(session, symbol, to_version)

    if sym_old is None and sym_new is None:
        return (
            f"api_version_diff({symbol!r})\n"
            f"└─ not found in either {from_version} or {to_version}"
        )
    return _format_api_diff(sym_old, sym_new, symbol, from_version, to_version)


@mcp.tool()
def lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Look up an Odoo core API symbol by name, return its signature, status, replacement.

    Args:
        name: Symbol name (full qualified or short, e.g. 'safe_eval' or
              'odoo.tools.safe_eval.safe_eval').
        odoo_version: e.g. '17.0', '18.0'. Default 'auto' = latest indexed.

    Returns:
        Tree-formatted text. Use this BEFORE writing code that calls Odoo
        upstream API to verify the symbol exists at the target version and
        learn its replacement if deprecated/removed.

    Example:
        lookup_core_api("name_get", "18.0")
        → odoo.models.BaseModel.name_get (Odoo 18.0)
          ├─ Status:      removed
          ├─ Signature:   name_get(self)
          └─ Replacement: odoo.models.BaseModel.display_name
    """
    return _lookup_core_api(name, odoo_version)


def _format_deprecated_usage(records: list[dict], version: str) -> str:
    header = f"find_deprecated_usage(Odoo {version}) — {len(records)} hits"
    if not records:
        return header + "\n└─ no deprecated usage found in indexed code"
    lines = [header]
    last_idx = len(records) - 1
    for i, r in enumerate(records):
        connector = "└─" if i == last_idx else "├─"
        loc = f"[{r['module']}] {r['model']}.{r['method']}"
        sym = r["deprecated_symbol"]
        status = r["status"]
        repl = r.get("replacement") or "(no replacement set)"
        lines.append(f"{connector} {loc}")
        lines.append(
            f"   ├─ uses: {sym} (status={status})"
        )
        lines.append(f"   └─ replacement: {repl}")
    return "\n".join(lines)


def _find_deprecated_usage(
    odoo_version: str = "auto", kind: str | None = None,
) -> str:
    """Quét user code dùng CoreSymbol có status deprecated/removed."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        cypher = """
            MATCH (mth:Method {odoo_version: $v})-[:USES_CORE_SYMBOL]->(cs:CoreSymbol)
            WHERE cs.status IN ['deprecated', 'removed']
        """
        params: dict = {"v": odoo_version}
        if kind:
            cypher += " AND cs.kind = $kind"
            params["kind"] = kind
        cypher += """
            RETURN mth.module AS module, mth.model AS model, mth.name AS method,
                   cs.qualified_name AS deprecated_symbol,
                   cs.status AS status,
                   cs.replacement_qname AS replacement
            ORDER BY mth.module, mth.model, mth.name
        """
        records = session.run(cypher, **params).data()
    return _format_deprecated_usage(records, odoo_version)


_VALID_LINT_LANGUAGES = {"python", "javascript", "xml"}


def _format_lint_check(
    violations: list[dict], version: str, code: str,
) -> str:
    header = f"lint_check(Odoo {version}, language=python) — {len(violations)} violations"
    code_preview = (code or "")[:60].replace("\n", " ")
    lines = [header, f"├─ Code: {code_preview!r}"]
    if not violations:
        lines.append("└─ no violations")
        return "\n".join(lines)
    last_idx = len(violations) - 1
    for i, r in enumerate(violations):
        connector = "└─" if i == last_idx else "├─"
        rule_id = r.get("rule_id") or "?"
        sev = r.get("severity") or "warning"
        msg = (r.get("message") or "").strip()
        lines.append(f"{connector} {rule_id} ({sev}): {msg}")
    return "\n".join(lines)


def _match_lint_rule(code: str, rule: dict) -> bool:
    """V0 lint match: case-insensitive substring on rule.message keyword tokens.

    Each rule's message is split into significant words (>3 chars, not stop-word).
    A rule fires if at least one significant word appears in the code.
    """
    msg = (rule.get("message") or "").lower()
    if not msg:
        return False
    code_lc = (code or "").lower()
    # Tokenize on non-alpha boundaries.
    tokens = [t for t in __import__("re").split(r"[^a-z_]+", msg) if len(t) > 3]
    stopwords = {
        "with", "from", "this", "that", "have", "must", "should", "must",
        "function", "usage", "literal", "string", "alias", "option",
    }
    significant = [t for t in tokens if t not in stopwords]
    return any(t in code_lc for t in significant)


def _lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Pattern-match user code against indexed LintRule.message (V0)."""
    if language not in _VALID_LINT_LANGUAGES:
        valid = ", ".join(sorted(_VALID_LINT_LANGUAGES))
        return (
            f"lint_check: invalid language {language!r}. "
            f"Valid options: {valid}."
        )
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        kind_prefix = (
            "pylint" if language == "python"
            else "eslint" if language == "javascript"
            else "static-xml"
        )
        rules = session.run("""
            MATCH (l:LintRule {odoo_version: $v})
            WHERE l.kind STARTS WITH $kp
            RETURN l.rule_id AS rule_id,
                   l.severity AS severity,
                   l.message AS message,
                   l.kind AS kind
        """, v=odoo_version, kp=kind_prefix).data()
    violations = [r for r in rules if _match_lint_rule(code, r)]
    return _format_lint_check(violations, odoo_version, code)


@mcp.tool()
def find_deprecated_usage(
    odoo_version: str = "auto", kind: str | None = None,
) -> str:
    """List indexed user methods that call deprecated/removed Odoo core APIs.

    Args:
        odoo_version: e.g. '17.0', '18.0'. Default 'auto' = latest indexed.
        kind: Optional filter — restrict to one CoreSymbol.kind
              (e.g. 'orm_method', 'function').

    Returns:
        Tree-formatted text grouped by module → model.method → core symbol →
        replacement. Use BEFORE upgrading a Viindoo addon to a new Odoo
        version to plan code changes.

    Example:
        find_deprecated_usage("18.0")
        → find_deprecated_usage(Odoo 18.0) — 12 hits
          ├─ [viin_sale] sale.order.legacy_label
          │    ├─ uses: odoo.models.BaseModel.name_get (status=deprecated)
          │    └─ replacement: odoo.models.BaseModel.display_name
          └─ ...
    """
    return _find_deprecated_usage(odoo_version, kind=kind)


@mcp.tool()
def lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Quick lint check of a code snippet against indexed Odoo lint rules (V0).

    Args:
        code: Source code chunk to check.
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.
        language: 'python' | 'javascript' | 'xml'.

    Returns:
        Tree-formatted text listing matched rules (rule_id, severity, message).
        V0 matcher is substring-on-rule-message — fast but fuzzy. Use as a
        first-pass screen, NOT as authoritative pylint/ruff/eslint output.

    Example:
        lint_check("raise UserError('Hello %s' % name)", "19.0", "python")
        → lint_check(Odoo 19.0, language=python) — 1 violations
          ├─ Code: \"raise UserError('Hello %s' % name)\"
          └─ E8502 (error): Bad usage of _, _lt function...
    """
    return _lint_check(code, odoo_version, language)


@mcp.tool()
def api_version_diff(symbol: str, from_version: str, to_version: str) -> str:
    """Diff a single Odoo core API symbol between two indexed versions.

    Args:
        symbol: Symbol name (full qualified or short).
        from_version: Older Odoo version, e.g. '17.0'.
        to_version: Newer Odoo version, e.g. '19.0'.

    Returns:
        Tree-formatted text describing whether the symbol was added, removed,
        replaced, or had its signature changed.

    Example:
        api_version_diff("name_get", "17.0", "18.0")
        → api_version_diff('name_get': 17.0 → 18.0)
          ├─ Status:    removed in 18.0
          ├─ Was:       name_get(self)
          └─ Replaced by: odoo.models.BaseModel.display_name
    """
    return _api_version_diff(symbol, from_version, to_version)


def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=_mcp_host(),
            port=_mcp_port(), path="/mcp")
