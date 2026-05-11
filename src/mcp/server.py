# src/mcp/server.py
import math
import os
from contextlib import contextmanager, nullcontext

import psycopg2
import psycopg2.pool
from fastmcp import FastMCP
from neo4j import GraphDatabase
from starlette.requests import Request

mcp = FastMCP("odoo-semantic")
_driver = None
_pg_pool: psycopg2.pool.SimpleConnectionPool | None = None
_embedder_instance = None
_version_checked = False


def _get_driver():
    global _driver, _version_checked
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

        # Version check: fail-fast if Neo4j < 5.x (unless in CI with pinned image)
        if not _version_checked and os.getenv("CI") != "true":
            with _driver.session() as _s:
                _row = _s.run(
                    "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
                ).single()
                if _row:
                    _v = str(_row["v"])
                    _major = int(_v.split(".")[0])
                    if _major < 5:
                        raise RuntimeError(
                            f"Neo4j 5.x+ required (found {_v}). "
                            f"Update docker-compose.yml NEO4J_IMAGE and re-run."
                        )
            _version_checked = True

    return _driver


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    """Return (creating on first call) the module-level connection pool."""
    global _pg_pool
    if _pg_pool is None:
        from src import config
        dsn = config.from_env_or_ini(
            "PG_DSN", "database", "pg_dsn", fallback=None,
        )
        if not dsn:
            raise RuntimeError(
                "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
                "in [database] section of odoo-semantic.conf."
            )
        _pg_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10, dsn=dsn)
    return _pg_pool


@contextmanager
def _checkout_pg():
    """Check out a pooled PG connection, register pgvector, return on exit.

    pgvector registration is idempotent — safe to call on every checkout.
    This ensures any connection (including newly grown pool slots) works for
    ``::vector`` casts without a separate eager-registration step.
    """
    from pgvector.psycopg2 import register_vector

    pool = _get_pool()
    conn = pool.getconn()
    try:
        register_vector(conn)
        yield conn
    finally:
        pool.putconn(conn)


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
        auth_token = config.from_env_or_ini(
            "EMBEDDER_AUTH_TOKEN", "embedder", "auth_token", fallback=None,
        )
        _embedder_instance = Qwen3Embedder(
            url, model, dim=int(dim_str), auth_token=auth_token,
        )
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
            "No data indexed. Run `python -m src.indexer index-repo --profile <name>` first."
        )
    return v


def _resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Ranking tiers — see docs/adr/0004:
        # T1 is_def_rank: m.is_definition flag (post-reindex, authoritative).
        # T2 field_count: Field nodes declared on this model in this module —
        #                 100% accurate signal pre-reindex on real data
        #                 (defining module always has the most fields).
        # T3 dependents : DEPENDS_ON inbound on Module (manifest depends).
        # T4 edition    : community < enterprise < viindoo < oca < custom.
        # T5 mod_name   : alphabetical tiebreak — eliminates arbitrary order.
        layers = session.run(
            """
            MATCH (m:Model {name: $name, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
            WITH m, mod,
                 CASE WHEN coalesce(m.is_definition, false) THEN 0 ELSE 1 END AS is_def_rank,
                 COUNT {
                     (:Field {model: $name, module: m.module, odoo_version: $v})
                 } AS field_count,
                 COUNT { ()-[:DEPENDS_ON]->(mod) } AS dependents,
                 CASE mod.edition
                      WHEN 'community'  THEN 0
                      WHEN 'enterprise' THEN 1
                      WHEN 'viindoo'    THEN 2
                      WHEN 'oca'        THEN 3
                      ELSE 4 END AS edition_rank,
                 mod.name AS mod_name
            RETURN m.module AS module_name, mod.repo AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
            """,
            name=model_name, v=odoo_version,
        ).data()

        if not layers:
            return f"Model '{model_name}' not found in Odoo {odoo_version}."

        base = layers[0]
        extensions = layers[1:]

        # DISTINCT on p.name only — the same parent (e.g. mail.thread) is reachable
        # via multiple INHERITS edges (one per module that declares _inherit), and
        # each one resolves to a separate (parent_name, module) pair. Without
        # collapsing here the rendered list shows duplicates like
        # "mail.thread, mail.thread, mail.thread, ..." (M5 install audit).
        parents = session.run("""
            MATCH (:Model {name: $name, odoo_version: $v})-[r:INHERITS]->(p:Model)
            WHERE p.name <> $name
              AND NOT coalesce(r.unresolved, false)
            RETURN DISTINCT p.name AS pname
            ORDER BY pname
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
        last_ext = len(extensions) - 1
        for i, ext in enumerate(extensions):
            connector = "└─" if i == last_ext else "├─"
            lines.append(f"│   {connector} [{ext['repo']}] {ext['module_name']}")

    lines.append(f"├─ Fields:         {fields_count}")
    lines.append(f"└─ Methods:        {methods_count}")
    return "\n".join(lines)


def _resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # 5-tier ranking via m_node proxy — see docs/adr/0004
        records = session.run("""
            MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: f.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $mn, module: f.module, odoo_version: $v})
            WITH f, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {
                     (:Field {model: $mn, module: f.module, odoo_version: $v})
                 } AS field_count,
                 COUNT { ()-[:DEPENDS_ON]->(mod) } AS dependents,
                 CASE mod.edition
                      WHEN 'community'  THEN 0
                      WHEN 'enterprise' THEN 1
                      WHEN 'viindoo'    THEN 2
                      WHEN 'oca'        THEN 3
                      ELSE 4 END AS edition_rank,
                 mod.name AS mod_name
            RETURN f, f.module AS module_name, mod.repo AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
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

        # 5-tier ranking via m_node proxy — see docs/adr/0004
        records = session.run("""
            MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $model, module: mth.module, odoo_version: $v})
            WITH mth, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {
                     (:Field {model: $model, module: mth.module, odoo_version: $v})
                 } AS field_count,
                 COUNT { ()-[:DEPENDS_ON]->(mod) } AS dependents,
                 CASE mod.edition
                      WHEN 'community'  THEN 0
                      WHEN 'enterprise' THEN 1
                      WHEN 'viindoo'    THEN 2
                      WHEN 'oca'        THEN 3
                      ELSE 4 END AS edition_rank,
                 mod.name AS mod_name
            RETURN mth, mth.module AS module_name, mod.repo AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """, mn=method_name, model=model_name, v=odoo_version).data()

    if not records:
        return (
            f"Method '{method_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}."
        )

    lines = [
        f"{model_name}.{method_name}() (Odoo {odoo_version})",
        f"└─ Override chain ({len(records)}):",
    ]
    last_idx = len(records) - 1
    for i, r in enumerate(records):
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        connector = "└─" if i == last_idx else "├─"
        lines.append(
            f"    {connector} {repo_str}{r['module_name']}"
            f" — {super_info} — decorators: {decs}"
        )
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

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
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
    # Coefficients are v0 placeholders — tune at M7 with held-out eval set.
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
    """Risk threshold v0 — tunable at M7 with held-out dataset.

    HIGH >= 10 affected entities, MEDIUM 4-9, LOW < 4.
    # Thresholds calibrated qualitatively against Odoo 17 + Viindoo addons typical fan-out:
    # <4 changes = isolated, 4-9 = module-scope review needed, ≥10 = cross-module impact
    # requiring full regression. M7 will recalibrate against held-out eval set.
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
    added_in = rec.get("added_in")
    removed_in = rec.get("removed_in")
    deprecated_in = rec.get("deprecated_in")

    lines = [f"{qn} (Odoo {version})"]
    lines.append(f"├─ Kind:        {kind}")
    lines.append(f"├─ Status:      {status}")
    if sig:
        lines.append(f"├─ Signature:   {sig}")
    if repl:
        lines.append(f"├─ Replacement: {repl}")
    if added_in:
        lines.append(f"├─ Added in:    {added_in}")
    if deprecated_in:
        lines.append(f"├─ Deprecated:  {deprecated_in}")
    if removed_in:
        lines.append(f"├─ Removed in:  {removed_in}")
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
                   cs.line AS line,
                   cs.added_in AS added_in,
                   cs.removed_in AS removed_in,
                   cs.deprecated_in AS deprecated_in
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
               cs.line AS line,
               cs.added_in AS added_in,
               cs.removed_in AS removed_in,
               cs.deprecated_in AS deprecated_in
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
    violations: list[dict], version: str, code: str, language: str = "python",
) -> str:
    header = f"lint_check(Odoo {version}, language={language}) — {len(violations)} violations"
    code_preview = (code or "")[:60].replace("\n", " ")
    lines = [_LINT_V0_BANNER, header, f"├─ Code: {code_preview!r}"]
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


# V0 lint matcher constant — surface in every lint_check output so users know this
# is a fuzzy approximation requiring manual verification.
_LINT_V0_BANNER = (
    "⚠ V0 fuzzy matcher — verify manually before action. "
    "Requires ≥2 significant token overlap between rule message and code."
)

_LINT_STOPWORDS = frozenset({
    "with", "from", "this", "that", "have", "must", "should",
    "function", "usage", "literal", "string", "alias", "option",
    "the", "and", "use", "not", "for", "are", "when", "avoid",
    "call", "called", "calling", "instead", "please", "using",
})


def _match_lint_rule(code: str, rule: dict) -> bool:
    """V0 lint match: ≥2 significant token overlap between rule.message and code.

    Significant token: >3 chars, alpha-only (after split on [^a-z_]), not in stopword set.
    Requires at least 2 such tokens from the rule message to appear in the code.
    This reduces single-word false positives common in the previous ≥1 threshold.

    Returns False if rule.message is empty or has fewer than 2 significant tokens.
    """
    import re as _re

    msg = (rule.get("message") or "").lower()
    if not msg:
        return False
    code_lc = (code or "").lower()
    # Tokenize on non-alpha-underscore boundaries, keep tokens > 3 chars.
    rule_tokens = {
        t for t in _re.split(r"[^a-z_]+", msg)
        if len(t) > 3 and t not in _LINT_STOPWORDS
    }
    if len(rule_tokens) < 2:
        # Not enough significant tokens in the rule message itself → never fires.
        return False
    code_tokens = set(_re.split(r"[^a-z_]+", code_lc))
    overlap = rule_tokens & code_tokens
    return len(overlap) >= 2


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
        curate_rec = session.run("""
            MATCH (sm:SpecMetadata {kind: 'lint', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
        """, v=odoo_version).single()
        curate_status = curate_rec["curate_status"] if curate_rec else None
    violations = [r for r in rules if _match_lint_rule(code, r)]
    result = _format_lint_check(violations, odoo_version, code, language)
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation — limited results.\n" + result
        )
    return result


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


def _format_cli_flag_detail(rec: dict, replacement: str | None, version: str) -> str:
    """Format a single CLIFlag detail."""
    flag = rec.get("flag_name") or "?"
    cmd = rec.get("command_name") or "?"
    status = rec.get("status") or "stable"
    typ = rec.get("type")
    default = rec.get("default")
    help_text = rec.get("help")
    lines = [f"cli_help({cmd!r}, {flag!r}, Odoo {version})"]
    lines.append(f"├─ Status:      {status}")
    if typ:
        lines.append(f"├─ Type:        {typ}")
    if default is not None:
        lines.append(f"├─ Default:     {default}")
    if help_text:
        lines.append(f"├─ Help:        {help_text}")
    if replacement:
        lines.append(f"└─ Replacement: {replacement}")
    else:
        lines[-1] = lines[-1].replace("├─", "└─")
    return "\n".join(lines)


def _format_cli_command_summary(
    cmd_rec: dict, flags: list[dict], version: str,
) -> str:
    name = cmd_rec.get("name") or "?"
    desc = cmd_rec.get("description")
    lines = [f"cli_help({name!r}, Odoo {version})"]
    if desc:
        lines.append(f"├─ Description: {desc}")
    if not flags:
        lines.append("└─ no flags indexed")
        return "\n".join(lines)
    lines.append(f"├─ Flags ({len(flags)}):")
    last_idx = len(flags) - 1
    for i, f in enumerate(flags):
        connector = "└─" if i == last_idx else "├─"
        flag = f.get("flag_name") or "?"
        status = f.get("status") or "stable"
        suffix = f" (status={status})" if status != "stable" else ""
        lines.append(f"   {connector} {flag}{suffix}")
    return "\n".join(lines)


def _cli_help(
    command: str | None,
    flag: str | None = None,
    odoo_version: str = "auto",
) -> str:
    """Return CLICommand spec or CLIFlag status + replacement."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Query SpecMetadata curation status for CLI at this version.
        curate_rec = session.run("""
            MATCH (sm:SpecMetadata {kind: 'cli', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
        """, v=odoo_version).single()
        curate_status = curate_rec["curate_status"] if curate_rec else None

        if command and flag:
            rec = session.run("""
                MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd, odoo_version: $v})
                OPTIONAL MATCH (f)-[:REPLACED_BY]->(repl:CLIFlag)
                RETURN f.flag_name AS flag_name,
                       f.command_name AS command_name,
                       f.status AS status,
                       f.type AS type,
                       f.default AS default,
                       f.help AS help,
                       repl.flag_name AS replacement
            """, flag=flag, cmd=command, v=odoo_version).single()
            if rec is None:
                result = (
                    f"cli_help({command!r}, {flag!r}, Odoo {odoo_version})\n"
                    f"└─ flag {flag!r} not found on command {command!r}"
                )
            else:
                data = dict(rec)
                replacement = data.pop("replacement", None)
                # Fallback: replacement_flag_name property when no REPLACED_BY edge.
                if not replacement:
                    fallback = session.run("""
                        MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd,
                                          odoo_version: $v})
                        RETURN f.replacement_flag_name AS r
                    """, flag=flag, cmd=command, v=odoo_version).single()
                    replacement = fallback["r"] if fallback else None
                result = _format_cli_flag_detail(data, replacement, odoo_version)
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        if command:
            cmd_rec = session.run("""
                MATCH (c:CLICommand {name: $cmd, odoo_version: $v})
                RETURN c.name AS name, c.description AS description
            """, cmd=command, v=odoo_version).single()
            if cmd_rec is None:
                result = (
                    f"cli_help({command!r}, Odoo {odoo_version})\n"
                    f"└─ command {command!r} not found"
                )
            else:
                flags = session.run("""
                    MATCH (f:CLIFlag {command_name: $cmd, odoo_version: $v})
                    RETURN f.flag_name AS flag_name, f.status AS status
                    ORDER BY f.flag_name
                """, cmd=command, v=odoo_version).data()
                result = _format_cli_command_summary(dict(cmd_rec), flags, odoo_version)
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        # No command — list all CLI commands at this version.
        cmds = session.run("""
            MATCH (c:CLICommand {odoo_version: $v})
            RETURN c.name AS name
            ORDER BY c.name
        """, v=odoo_version).data()
    if not cmds:
        result = (
            f"cli_help(Odoo {odoo_version})\n"
            f"└─ no CLI commands indexed for this version"
        )
        if curate_status == "pending":
            result = (
                f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                + result
            )
        return result
    lines = [f"cli_help(Odoo {odoo_version}) — {len(cmds)} commands"]
    last_idx = len(cmds) - 1
    for i, c in enumerate(cmds):
        connector = "└─" if i == last_idx else "├─"
        lines.append(f"{connector} {c['name']}")
    result = "\n".join(lines)
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation — limited results.\n" + result
        )
    return result


@mcp.tool()
def cli_help(
    command: str | None = None,
    flag: str | None = None,
    odoo_version: str = "auto",
) -> str:
    """Look up odoo-bin subcommand or flag info: status, replacement, help text.

    Args:
        command: Subcommand name (e.g. 'server', 'shell', 'scaffold').
                 If None, list all known commands at this version.
        flag: Optional flag name (e.g. '--http-port'). When set with command,
              returns full flag details including replacement.
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.

    Returns:
        Tree-formatted text. Use to verify a flag's status before scripting
        an odoo-bin invocation, or to pick the replacement for a deprecated flag.

    Example:
        cli_help("server", "--longpolling-port", "18.0")
        → cli_help('server', '--longpolling-port', Odoo 18.0)
          ├─ Status:      removed
          ├─ Help:        Deprecated alias to the gevent-port option
          └─ Replacement: --gevent-port
    """
    return _cli_help(command, flag, odoo_version)


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


# --- M4.6 Pattern Wow tools -------------------------------------------------

_VALID_PATTERN_LANGUAGES = ("python", "xml", "js", "all")


def _suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
) -> str:
    """ANN-rank curated PatternExample chunks by intent string.

    Per ADR-0003: pgvector ANN over embeddings (chunk_type='pattern_example') →
    Neo4j batch fetch metadata via UNWIND on pattern_id list. Language filter
    via entity_name slug LIKE '<language>__%'.
    """
    if not intent.strip():
        return (
            "suggest_pattern: intent is required (empty input).\n"
            "Hint: pass a natural-language description, e.g. "
            "'computed field cross-model partner'."
        )
    if language not in _VALID_PATTERN_LANGUAGES:
        valid = ", ".join(_VALID_PATTERN_LANGUAGES)
        return (
            f"suggest_pattern: invalid language={language!r}. Valid: {valid}."
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _get_driver()
    try:
        embedder = _embedder or _get_embedder()
    except Exception as e:
        return (
            f"suggest_pattern: embedder unavailable — {type(e).__name__}: {e}\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )

    with driver.session() as session:
        v = _resolve_version(odoo_version, session)

    try:
        intent_vec = embedder.embed([INSTRUCT_NL_TO_CODE + intent])[0]
    except Exception as e:
        return (
            f"suggest_pattern: embedding query failed — {type(e).__name__}: {e}"
        )

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            if language == "all":
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, intent_vec, limit],
                )
            else:
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND entity_name LIKE %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, f"{language}__%", intent_vec, limit],
                )
            ranked = cur.fetchall()

    if not ranked:
        return (
            f"suggest_pattern({intent!r}, {v!r}, language={language})\n"
            "└─ no patterns indexed. Run: "
            "python -m src.indexer.seed_patterns"
        )

    # Decode pattern_id from entity_name slug (<language>__<id>)
    pattern_ids = []
    score_map: dict[str, float] = {}
    for entity_name, _file, cosine in ranked:
        if "__" in entity_name:
            _lang, pid = entity_name.split("__", 1)
        else:
            pid = entity_name
        pattern_ids.append(pid)
        score_map[pid] = float(cosine)

    with driver.session() as session:
        records = session.run("""
            UNWIND $ids AS pid
            MATCH (p:PatternExample {pattern_id: pid})
            RETURN p.pattern_id AS id, p.intent_keywords AS kw,
                   p.file_ref AS fr, p.snippet_text AS sn,
                   p.gotchas AS g, p.language AS lang,
                   p.odoo_version_min AS vmin
        """, ids=pattern_ids).data()

    by_id = {r["id"]: r for r in records}
    return _format_suggest_pattern(
        ordered_ids=pattern_ids, by_id=by_id, score_map=score_map,
        intent=intent, version=v, language=language,
    )


def _format_suggest_pattern(
    *, ordered_ids: list[str], by_id: dict[str, dict],
    score_map: dict[str, float], intent: str, version: str, language: str,
) -> str:
    lines = [
        f"suggest_pattern({intent!r}, {version}, language={language}) "
        f"— {len(ordered_ids)} matches",
    ]
    last = len(ordered_ids) - 1
    for i, pid in enumerate(ordered_ids):
        rec = by_id.get(pid)
        if not rec:
            continue
        connector = "└─" if i == last else "├─"
        score = score_map.get(pid, 0.0)
        lines.append(f"{connector} #{i + 1} · score {score:.2f} · {pid}")
        prefix = "    " if i == last else "│   "
        lines.append(f"{prefix}├─ Language: {rec['lang']} (min v{rec['vmin']})")
        lines.append(f"{prefix}├─ File:     {rec['fr']}")
        snippet_lines = (rec.get("sn") or "").splitlines()
        if snippet_lines:
            lines.append(f"{prefix}├─ Snippet:")
            for sl in snippet_lines[:5]:
                lines.append(f"{prefix}│    {sl}")
            if len(snippet_lines) > 5:
                lines.append(f"{prefix}│    ... ({len(snippet_lines) - 5} more lines)")
        gotchas = rec.get("g") or []
        if gotchas:
            lines.append(f"{prefix}└─ Gotchas:")
            for g in gotchas:
                lines.append(f"{prefix}     • {g}")
    return "\n".join(lines)


def _check_module_exists(
    name: str, odoo_version: str = "auto", *, _driver=None,
) -> str:
    """Report whether `name` is indexed + flag EE-confusion (per ADR-0003 §2).

    Edition-first strategy: query Neo4j for indexed edition (OEEL-1 detected),
    fallback to hardcoded dict if not indexed. Both paths produce same EE warning.
    """
    from src.data.ee_modules import EE_CONFUSION

    driver = _driver or _get_driver()
    with driver.session() as session:
        v = _resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (m:Module {name: $n, odoo_version: $v})
            RETURN m.edition AS edition,
                   m.viindoo_equivalent_qname AS vvq,
                   m.repo AS repo
        """, n=name, v=v).single()

    indexed = rec is not None
    edition = rec["edition"] if rec else None
    repo = rec.get("repo") if rec else None
    vvq_db = rec.get("vvq") if rec else None

    # Edition-first: check Neo4j for 'enterprise' (from OEEL-1 license detection)
    is_ee_confusion = False
    ee_source = ""  # track source for output messaging
    viindoo_equivalent = None

    if indexed and edition == "enterprise":
        # Indexed data has OEEL-1 → is EE module
        is_ee_confusion = True
        ee_source = "indexed"
        viindoo_equivalent = vvq_db or EE_CONFUSION.get(name)
    elif name in EE_CONFUSION:
        # Not indexed (or not marked 'enterprise') but in hardcoded dict
        is_ee_confusion = True
        ee_source = "dict"
        viindoo_equivalent = EE_CONFUSION.get(name)

    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition, repo=repo,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
        ee_source=ee_source,
    )


def _format_check_module_exists(
    *, name: str, version: str, indexed: bool, edition: str | None,
    repo: str | None, is_ee_confusion: bool, viindoo_equivalent: str | None,
    ee_source: str = "",
) -> str:
    lines = [f"check_module_exists({name!r}, {version})"]
    lines.append(f"├─ Indexed:         {'Yes' if indexed else 'No'}")
    if indexed and edition:
        repo_suffix = f" [{repo}]" if repo else ""
        lines.append(f"├─ Edition:         {edition}{repo_suffix}")
    lines.append(
        f"├─ Is EE confusion: {'Yes' if is_ee_confusion else 'No'}"
    )
    if is_ee_confusion:
        if viindoo_equivalent:
            lines.append(f"├─ Viindoo equiv:   {viindoo_equivalent}")
        else:
            lines.append("├─ Viindoo equiv:   (none — feature not in Viindoo stack)")
        # Differentiate source for debugging
        source_hint = ""
        if ee_source == "indexed":
            source_hint = " (license=OEEL-1)"
        elif ee_source == "dict":
            source_hint = " (legacy hardcoded dict)"
        lines.append(
            f"└─ ⚠ WARNING: this is an Odoo Enterprise module{source_hint}. "
            "Do NOT depend on it in a Viindoo Community stack — "
            "vi phạm GPL/Enterprise license boundary."
        )
    elif not indexed:
        lines.append(
            "└─ Hint: module not indexed in this profile. "
            "If it should be, run: python -m src.indexer index-repo --profile <name>"
        )
    else:
        lines[-1] = lines[-1].replace("├─", "└─")
    return "\n".join(lines)


_ANTI_PATTERNS_BASE = [
    "Old-style super(ClassName, self) — use plain super() in Python 3",
    "Missing return after super() — caller gets None, breaks chain",
]


def _anti_patterns_for_convention(kind: str) -> list[str]:
    """Return convention-specific anti-pattern hints for find_override_point."""
    if kind == "compute":
        return [
            "Calling super() in compute method — Odoo rebinds via @api.depends, "
            "super-chain semantically meaningless",
            "Forgetting @api.depends — silent stale data on field reads",
        ]
    if kind in ("inverse", "search", "default"):
        return [
            f"Calling super() in {kind} method — Odoo rebinds via decorator, "
            "super-chain has no effect",
        ]
    if kind == "action":
        return list(_ANTI_PATTERNS_BASE) + [
            "Returning bool/None instead of action_window dict — UI can't refresh",
        ]
    if kind == "crud":
        return list(_ANTI_PATTERNS_BASE) + [
            "Missing @api.model_create_multi on create() override — slow batch import",
            "Treating vals as single dict instead of vals_list — silent data loss",
        ]
    return list(_ANTI_PATTERNS_BASE)


def _fetch_method_for_diff(session, model: str, method: str, version: str) -> dict | None:
    """Fetch a single Method node's properties for cross-version diff.

    Returns a dict with keys: decorators, convention_kind, super_safety,
    has_super_call, signature. Returns None when no Method found.
    Aggregates across all modules (decorators union, super_call OR).
    """
    rows = session.run("""
        MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
        RETURN mth.decorators AS decorators,
               mth.convention_kind AS ck,
               mth.super_safety AS ss,
               coalesce(mth.has_super_call, false) AS has_super,
               mth.signature AS signature
        ORDER BY mth.module
    """, method=method, model=model, v=version).data()
    if not rows:
        return None
    # Merge across override chain: union decorators, OR has_super, first non-null sig
    all_decs: list[str] = []
    seen_decs: set[str] = set()
    has_super = False
    sig: str | None = None
    ck = rows[0]["ck"] or "private"
    ss = rows[0]["ss"] or "usually"
    for r in rows:
        for d in (r["decorators"] or []):
            if d not in seen_decs:
                seen_decs.add(d)
                all_decs.append(d)
        if r["has_super"]:
            has_super = True
        if sig is None and r["signature"] is not None:
            sig = r["signature"]
    return {
        "decorators": all_decs,
        "convention_kind": ck,
        "super_safety": ss,
        "has_super_call": has_super,
        "signature": sig,
    }


def _diff_method_across_versions(
    model: str, method: str, from_version: str, to_version: str,
    *, _driver=None,
) -> str:
    """Diff a method between two Odoo versions.

    Compares decorator set, convention_kind, super_safety, and signature
    between from_version and to_version. Returns tree-formatted string.
    """
    driver = _driver or _get_driver()
    with driver.session() as session:
        from_data = _fetch_method_for_diff(session, model, method, from_version)
        to_data = _fetch_method_for_diff(session, model, method, to_version)

    header = f"Method version diff ({model}.{method}: {from_version} → {to_version})"
    lines = [header]

    # Presence
    if from_data and to_data:
        presence_label = "both versions present"
    elif from_data and not to_data:
        presence_label = f"deleted in {to_version} (not found)"
    elif not from_data and not to_data:
        presence_label = (
            f"absent in both {from_version} and {to_version}"
            " (model/method may not be indexed)"
        )
        lines.append(f"├─ Status:           {presence_label}")
        return "\n".join(lines)
    else:
        presence_label = f"added in {to_version} (not in {from_version})"
    lines.append(f"├─ Status:           {presence_label}")

    # Decorator diff
    from_decs = set(from_data["decorators"]) if from_data else set()
    to_decs = set(to_data["decorators"]) if to_data else set()
    removed = sorted(from_decs - to_decs)
    added = sorted(to_decs - from_decs)
    if removed or added:
        lines.append("├─ Decorator changes:")
        for d in removed:
            lines.append(f"│   ├─ Removed in {to_version}: {d}")
        for d in added:
            lines.append(f"│   └─ Added in {to_version}:   {d}")
    else:
        lines.append("├─ Decorator changes: none")

    # Convention diff
    from_ck = from_data["convention_kind"] if from_data else "?"
    to_ck = to_data["convention_kind"] if to_data else "?"
    if from_ck != to_ck:
        lines.append(f"├─ Convention:        changed ({from_ck} → {to_ck})")
    else:
        lines.append(f"├─ Convention:        unchanged ({from_ck})")

    # Signature diff
    _NULL_HINT = "(not stored, run 'index-repo --full' to populate)"
    from_sig = from_data["signature"] if from_data else None
    to_sig = to_data["signature"] if to_data else None
    from_sig_str = from_sig if from_sig is not None else _NULL_HINT
    to_sig_str = to_sig if to_sig is not None else _NULL_HINT
    if from_sig is None or to_sig is None:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig_str!r}"
            f" → {to_version}={to_sig_str!r}"
        )
    elif from_sig != to_sig:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig!r}"
            f" → {to_version}={to_sig!r}"
        )
    else:
        lines.append(f"├─ Signature:         unchanged ({from_sig!r})")

    # Super safety
    from_ss = from_data["super_safety"] if from_data else "?"
    to_ss = to_data["super_safety"] if to_data else "?"
    if from_ss != to_ss:
        lines.append(f"└─ Super safety:      changed ({from_ss} → {to_ss})")
    else:
        lines.append(f"└─ Super safety:      unchanged ({from_ss})")

    return "\n".join(lines)


def _find_override_point(
    model: str, method: str, odoo_version: str = "auto",
    *, to_version: str = "", _driver=None,
) -> str:
    """Inspect Method override chain + surface convention hints + anti-patterns.

    When to_version is non-empty and differs from odoo_version, performs a
    cross-version diff instead of single-version inspection.
    """
    driver = _driver or _get_driver()
    with driver.session() as session:
        v = _resolve_version(odoo_version, session)

    # Cross-version diff mode
    if to_version and to_version != v:
        return _diff_method_across_versions(
            model, method, from_version=v, to_version=to_version, _driver=driver,
        )

    # Single-version mode (existing behaviour)
    with driver.session() as session:
        records = session.run("""
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super,
                   mod.repo AS repo, mod.edition AS edition
            ORDER BY mth.module
        """, method=method, model=model, v=v).data()

    if not records:
        return (
            f"find_override_point({model!r}, {method!r}, {v})\n"
            f"└─ method not found on model {model!r} in Odoo {v}"
        )

    convention_kind = records[0]["ck"] or "private"
    super_safety = records[0]["ss"] or "usually"
    return_required = bool(records[0]["rr"])
    super_count = sum(1 for r in records if r["has_super"])
    super_ratio = f"{super_count}/{len(records)}"
    anti_patterns = _anti_patterns_for_convention(convention_kind)

    return _format_find_override_point(
        model=model, method=method, version=v, records=records,
        super_ratio=super_ratio, convention_kind=convention_kind,
        super_safety=super_safety, return_required=return_required,
        anti_patterns=anti_patterns,
    )


def _format_find_override_point(
    *, model: str, method: str, version: str, records: list[dict],
    super_ratio: str, convention_kind: str, super_safety: str,
    return_required: bool, anti_patterns: list[str],
) -> str:
    lines = [f"find_override_point({model!r}, {method!r}, {version})"]
    lines.append(f"├─ Convention:      {convention_kind}")
    lines.append(f"├─ Super safety:    {super_safety}")
    lines.append(f"├─ Return required: {'Yes' if return_required else 'No'}")
    lines.append(f"├─ Super ratio:     {super_ratio} (overrides calling super)")
    lines.append(f"├─ Override chain ({len(records)}):")
    for i, r in enumerate(records):
        connector = "└─" if i == len(records) - 1 else "├─"
        repo = f"[{r['repo']}] " if r.get("repo") else ""
        ed = f" ({r['edition']})" if r.get("edition") else ""
        super_mark = "✓" if r["has_super"] else "✗"
        lines.append(
            f"│   {connector} {repo}{r['module']}{ed} — {super_mark} super()"
        )
    lines.append(f"└─ Anti-patterns ({len(anti_patterns)}):")
    for i, ap in enumerate(anti_patterns):
        connector = "└─" if i == len(anti_patterns) - 1 else "├─"
        lines.append(f"    {connector} {ap}")
    return "\n".join(lines)


@mcp.tool()
def suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
) -> str:
    """Recommend curated Odoo patterns from a natural-language intent.

    Patterns include canonical snippet + 3+ gotchas to avoid common bugs
    (anti-patterns, version-specific renames, security pitfalls).

    Args:
        intent: NL description of what you're trying to do, e.g.
                'computed field cross-model partner'.
        odoo_version: '17.0' / '18.0' / 'auto' (latest indexed).
        language: 'python' | 'xml' | 'js' | 'all'. Default 'python'.
        limit: Max patterns to return (default 5).

    Returns:
        Tree-formatted list of patterns ranked by cosine score, each with
        snippet (first 5 lines), file ref, and gotchas list. Empty index =>
        instruction to run `python -m src.indexer.seed_patterns`.

    Example:
        suggest_pattern("override write to read old value", "17.0")
        → suggest_pattern('override write to read old value', 17.0, language=python) — 1 matches
          └─ #1 · score 0.81 · write-read-before-super
              ├─ Language: python (min v17.0)
              ├─ File:     addons/account/models/account_move.py:2891
              └─ Gotchas:
                   • Reading old values AFTER super().write() returns the new value
                   • Always return the result of super().write()
    """
    return _suggest_pattern(intent, odoo_version, language, limit)


@mcp.tool()
def check_module_exists(name: str, odoo_version: str = "auto") -> str:
    """Verify if a module name is indexed AND flag EE-confusion (Viindoo stack).

    Used by AI tools BEFORE generating `depends=['<name>']` in __manifest__.py
    to avoid hallucinating Odoo Enterprise modules (knowledge, helpdesk, sign,
    etc.) that don't exist on Viindoo Community stack.

    Args:
        name: Module technical name (e.g. 'sale', 'helpdesk', 'viin_helpdesk').
        odoo_version: '17.0' / '18.0' / 'auto'.

    Returns:
        Tree text: Indexed yes/no, edition, EE-confusion flag, Viindoo
        equivalent (if any), and a WARNING when name is an EE-only module.

    Example:
        check_module_exists('helpdesk', '17.0')
        → check_module_exists('helpdesk', 17.0)
          ├─ Indexed:         No
          ├─ Is EE confusion: Yes
          ├─ Viindoo equiv:   viin_helpdesk
          └─ ⚠ WARNING: this is an Odoo Enterprise module. Do NOT depend on it
             in a Viindoo Community stack...
    """
    return _check_module_exists(name, odoo_version)


@mcp.tool()
def find_override_point(
    model: str, method: str, odoo_version: str = "auto", to_version: str = "",
) -> str:
    """Show override chain of a method + super-call ratio + convention guidance.

    Used BEFORE writing an override to know: (a) which modules already extend
    the method, (b) whether super() call is required (action/crud) or
    forbidden (compute/inverse), and (c) common anti-patterns to avoid.

    When to_version is provided and differs from odoo_version, performs a
    cross-version diff (decorator changes, signature changes, convention changes)
    between odoo_version and to_version — useful when migrating addons.

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        method: Method name (e.g. 'action_confirm', '_compute_amount').
        odoo_version: '17.0' / '18.0' / 'auto'. Acts as from_version in diff mode.
        to_version: Optional. When set and different from odoo_version, activates
                    cross-version diff mode (e.g. '18.0' to diff 17.0 → 18.0).
                    Default '' = single-version mode (backward compatible).

    Returns:
        Single-version mode: Tree text with convention_kind, super_safety,
        return_required, super_ratio, full override chain, and anti-patterns.

        Cross-version diff mode: Tree text with presence, decorator changes,
        convention change, signature diff, super safety change.

    Example (single-version):
        find_override_point('sale.order', 'action_confirm', '17.0')
        → find_override_point('sale.order', 'action_confirm', 17.0)
          ├─ Convention:      action
          ├─ Super safety:    always
          ├─ Return required: Yes
          ├─ Super ratio:     7/7 (overrides calling super)
          ├─ Override chain (7):
          │   ├─ [odoo] sale (community) — ✗ super()
          │   └─ [tvtmaaddons17] viin_sale (viindoo) — ✓ super()
          └─ Anti-patterns (3):
              ├─ Old-style super(ClassName, self) — use plain super() in Python 3
              └─ ...

    Example (cross-version diff):
        find_override_point('sale.order', 'action_confirm', '17.0', to_version='18.0')
        → Method version diff (sale.order.action_confirm: 17.0 → 18.0)
          ├─ Status:           both versions present
          ├─ Decorator changes:
          │   ├─ Removed in 18.0: api.multi
          ├─ Convention:        unchanged (action)
          ├─ Signature:         17.0='self' → 18.0='self, *, ctx=None'
          └─ Super safety:      unchanged (always)
    """
    return _find_override_point(model, method, odoo_version, to_version=to_version)


def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))


# Health endpoint — registered as custom route on MCP app
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    from src.mcp.health import health_handler
    return await health_handler(request)


if __name__ == "__main__":
    import logging as _logging

    from src.logging_config import configure_logging as _configure_logging
    _configure_logging(level=_logging.INFO)

    from pathlib import Path as _Path

    import uvicorn as _uvicorn
    from starlette.middleware import Middleware as _Middleware
    from starlette.staticfiles import StaticFiles as _StaticFiles

    from src.mcp.middleware import AuthMiddleware

    # Replace mcp.run(...) with explicit app+uvicorn so we can mount /install StaticFiles.
    _app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[_Middleware(AuthMiddleware)],
    )

    _install_dir = _Path(__file__).parent / "static" / "install"
    if _install_dir.is_dir():
        _app.mount(
            "/install",
            _StaticFiles(directory=str(_install_dir), html=True),
            name="install",
        )

    # Mount feedback API on MCP port so remote users can submit thumbs-up/down.
    # feedback.router exposes POST /api/feedback and GET /api/feedback/{pattern_id}.
    # Auth is already enforced by AuthMiddleware above — no loopback guard needed.
    # We wrap the router in a mini FastAPI sub-app (include_router) and mount it
    # at the root prefix '' so its /api/feedback paths remain unchanged.
    from fastapi import FastAPI as _FastAPI

    from src.web_ui.routes import feedback as _feedback

    _feedback_app = _FastAPI()
    _feedback_app.include_router(_feedback.router)
    _app.mount("", _feedback_app)

    _uvicorn.run(
        _app,
        host=_mcp_host(),
        port=_mcp_port(),
        timeout_graceful_shutdown=0,
        lifespan="on",
    )
