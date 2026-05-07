"""pgvector writer — chunk, embed, and store Odoo code in PostgreSQL embeddings table."""
from __future__ import annotations

from dataclasses import dataclass

from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values

from .embedder import EmbedderClient
from .models import JSChunk, ParseResult, ViewParseResult

_WINDOW_CHARS = 2048
_OVERLAP_CHARS = 256

_INSERT_SQL = """
INSERT INTO embeddings
    (chunk_type, module, odoo_version, entity_name, model_name, file_path, chunk_idx, content, vec)
VALUES %s
ON CONFLICT ON CONSTRAINT ux_embeddings_chunk
DO UPDATE SET content = EXCLUDED.content, vec = EXCLUDED.vec, indexed_at = NOW()
"""


@dataclass
class EmbeddingChunk:
    """A single embeddable text unit derived from Odoo source code."""
    chunk_type: str     # 'method'|'field'|'view'|'qweb'|'js_era1'|'js_era2'|'js_era3'
    module: str
    odoo_version: str
    entity_name: str
    model_name: str | None
    file_path: str
    chunk_idx: int
    content: str

    def as_tuple(self, vec: list[float]) -> tuple:
        return (
            self.chunk_type, self.module, self.odoo_version,
            self.entity_name, self.model_name, self.file_path,
            self.chunk_idx, self.content, vec,
        )


def _sliding(
    raw: str,
    entity_name: str,
    chunk_type: str,
    module: str,
    version: str,
    file_path: str,
    model_name: str | None,
) -> list[EmbeddingChunk]:
    """Split large content into overlapping window EmbeddingChunks."""
    if len(raw) <= _WINDOW_CHARS:
        return [
            EmbeddingChunk(chunk_type, module, version, entity_name, model_name, file_path, 0, raw)
        ]
    chunks: list[EmbeddingChunk] = []
    start, idx = 0, 0
    while start < len(raw):
        end = min(start + _WINDOW_CHARS, len(raw))
        chunks.append(EmbeddingChunk(
            chunk_type, module, version, entity_name, model_name, file_path, idx, raw[start:end]
        ))
        if end == len(raw):
            break
        start = end - _OVERLAP_CHARS
        idx += 1
    return chunks


def make_chunks(
    module: str,
    version: str,
    parse_result: ParseResult,
    view_result: ViewParseResult | None,
    js_chunks: list[JSChunk] | None,
) -> list[EmbeddingChunk]:
    """Convert ParseResult + ViewParseResult + JSChunks into EmbeddingChunks."""
    chunks: list[EmbeddingChunk] = []

    for model in parse_result.models:
        for method in model.methods:
            prefix = f"[{module}] {model.name}.{method.name} ({version})"
            body = method.source_code or f"def {method.name}(self): ..."
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{method.name}", "method",
                module, version, parse_result.module.path, model.name,
            ))

        for fld in model.fields:
            prefix = f"[{module}] {model.name}: {fld.name} ({fld.ttype})"
            body = fld.source_definition or f"{fld.name} = fields.{fld.ttype.capitalize()}(...)"
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{fld.name}", "field",
                module, version, parse_result.module.path, model.name,
            ))

    if view_result:
        for view in view_result.views:
            inherit_str = f", inherit={view.inherit_xmlid}" if view.inherit_xmlid else ""
            prefix = f"[{module}] {view.xmlid} ({view.view_type}{inherit_str})"
            body = view.arch or f"<!-- arch missing for {view.xmlid} -->"
            fp = view.file_path or parse_result.module.path
            chunks.extend(
                _sliding(f"{prefix}\n{body}", view.xmlid, "view", module, version, fp, view.model)
            )

        for qweb in view_result.qweb:
            prefix = f"[{module}] {qweb.xmlid}"
            body = qweb.content or f"<!-- content missing for {qweb.xmlid} -->"
            fp = qweb.file_path or parse_result.module.path
            chunks.extend(
                _sliding(f"{prefix}\n{body}", qweb.xmlid, "qweb", module, version, fp, None)
            )

    for jsc in (js_chunks or []):
        chunk_type = f"js_{jsc.era}"
        chunks.append(EmbeddingChunk(
            chunk_type, module, version,
            jsc.entity_name, None, jsc.file_path, jsc.chunk_idx, jsc.content,
        ))

    return chunks


def write_module_embeddings(
    conn,
    module: str,
    version: str,
    chunks: list[EmbeddingChunk],
    embedder: EmbedderClient,
) -> None:
    """Delete-then-insert embeddings for (module, version) atomically."""
    if not chunks:
        return
    register_vector(conn)
    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)
    saved_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM embeddings WHERE module = %s AND odoo_version = %s",
                (module, version),
            )
            execute_values(cur, _INSERT_SQL, [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = saved_autocommit
