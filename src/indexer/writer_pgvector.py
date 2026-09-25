# SPDX-License-Identifier: AGPL-3.0-or-later
"""pgvector writer — chunk, embed, and store Odoo code in PostgreSQL embeddings table."""
from __future__ import annotations

import logging
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass

from psycopg2.extras import execute_values

from src.constants import EMBEDDER_TOKEN_BUDGET, GLOBAL_PROFILE

from . import parse_health
from .embedder import EmbedderClient, estimate_tokens, split_by_token_budget
from .models import (
    CSSChunk,
    JSChunk,
    JsTestSuiteInfo,
    ModuleInfo,
    ParseResult,
    PatternExample,
    SCSSChunk,
    TestParseResult,
    ViewParseResult,
)

_logger = logging.getLogger(__name__)

_WINDOW_CHARS = 2048
_OVERLAP_CHARS = 256

_INSERT_SQL = """
INSERT INTO embeddings
    (chunk_type, module, odoo_version, entity_name, model_name, file_path, chunk_idx, content, vec,
     profile_name, line_start, repo, repo_id, embedding_model, embedding_dim)
VALUES %s
ON CONFLICT ON CONSTRAINT ux_embeddings_chunk
DO UPDATE SET content = EXCLUDED.content, vec = EXCLUDED.vec, indexed_at = NOW(),
              line_start = EXCLUDED.line_start, repo = EXCLUDED.repo, repo_id = EXCLUDED.repo_id,
              embedding_model = EXCLUDED.embedding_model, embedding_dim = EXCLUDED.embedding_dim
"""


@dataclass
class EmbeddingChunk:
    """A single embeddable text unit derived from Odoo source code."""
    chunk_type: str     # 'method'|'field'|'view'|'qweb'|'js_era1'|'js_era2'|'js_era3'|'css'|'scss'|'less'  # noqa: E501
    module: str
    odoo_version: str
    entity_name: str
    model_name: str | None
    file_path: str
    chunk_idx: int
    content: str
    # global rows use '__global__' sentinel (m13_021); NULL no longer valid post-migration
    profile_name: str | None = None
    # A3 — provenance columns (reindex-forcing; NULL for css/scss/less/pattern chunks)
    line_start: int | None = None   # 1-based source line of the entity (method def / field assign)
    repo: str | None = None         # repo basename (ModuleInfo.repo)
    repo_id: int | None = None      # FK to repos.id (ModuleInfo.repo_id)

    def as_tuple(
        self, vec: list[float],
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> tuple:
        return (
            self.chunk_type, self.module, self.odoo_version,
            self.entity_name, self.model_name, self.file_path,
            self.chunk_idx, self.content, vec,
            self.profile_name,
            self.line_start, self.repo, self.repo_id,
            embedding_model, embedding_dim,
        )


def _embedder_meta(embedder: object) -> tuple[str | None, int | None]:
    """Return (model, dim) from an embedder object; None for missing attrs.

    Tolerates pre-ADR-0045 embedders and test doubles that do not expose
    .model / .dim.  Callers stamp NULL and skip the dim guard rather than
    crash.  Centralises the three identical getattr pairs in writer +
    seed_patterns._write_pgvector*.
    """
    return getattr(embedder, "model", None), getattr(embedder, "dim", None)


def _token_split_chunk(
    base_chunk: EmbeddingChunk, content: str, start_idx: int,
) -> list[EmbeddingChunk]:
    """Split *content* by token budget, producing EmbeddingChunks starting at *start_idx*.

    Returns a single-element list when content fits within EMBEDDER_TOKEN_BUDGET
    (using start_idx as chunk_idx).  Otherwise splits and returns one chunk per
    piece with monotonically increasing chunk_idx starting at start_idx.

    Uses dataclasses.replace so all provenance fields (repo, repo_id, line_start,
    profile_name) are copied from *base_chunk* without repetition.
    """
    import dataclasses

    if estimate_tokens(content) <= EMBEDDER_TOKEN_BUDGET:
        return [dataclasses.replace(base_chunk, chunk_idx=start_idx, content=content)]
    pieces = split_by_token_budget(content, EMBEDDER_TOKEN_BUDGET)
    return [
        dataclasses.replace(base_chunk, chunk_idx=start_idx + i, content=piece)
        for i, piece in enumerate(pieces)
    ]


def _sliding(
    raw: str,
    entity_name: str,
    chunk_type: str,
    module: str,
    version: str,
    file_path: str,
    model_name: str | None,
    *,
    line_start: int | None = None,
    repo: str | None = None,
    repo_id: int | None = None,
) -> list[EmbeddingChunk]:
    """Split large content into overlapping window EmbeddingChunks.

    A3: optional keyword arguments `line_start`, `repo`, `repo_id` are
    propagated to every produced chunk (all windows share the same provenance —
    line_start points to the first line of the entity regardless of window).

    WI-B: after char-window splitting, each window is further split by
    split_by_token_budget if it exceeds EMBEDDER_TOKEN_BUDGET tokens. All
    sub-chunks from token splitting keep the same provenance.  chunk_idx is
    monotonically allocated across all windows and their token-split pieces
    so no two chunks for the same (entity_name, file_path) share an index.
    """
    # Build a prototype chunk; _token_split_chunk copies it with correct idx/content.
    _proto = EmbeddingChunk(
        chunk_type, module, version, entity_name, model_name, file_path, 0, "",
        line_start=line_start, repo=repo, repo_id=repo_id,
    )

    if len(raw) <= _WINDOW_CHARS:
        return _token_split_chunk(_proto, raw, 0)

    chunks: list[EmbeddingChunk] = []
    start = 0
    idx = 0
    while start < len(raw):
        end = min(start + _WINDOW_CHARS, len(raw))
        window = raw[start:end]
        sub_chunks = _token_split_chunk(_proto, window, idx)
        chunks.extend(sub_chunks)
        idx += len(sub_chunks)
        if end == len(raw):
            break
        start = end - _OVERLAP_CHARS

    return chunks


def _embed_chunks_resilient(
    embedder: EmbedderClient,
    chunks: list[EmbeddingChunk],
) -> tuple[list[EmbeddingChunk], list[list[float]], int]:
    """Embed all chunks, retrying the batch once on failure then degrading per-chunk.

    Happy path: embedder.embed([all contents]) in one call.
    If the batch raises (RuntimeError / any exception): retry the full batch
    once (reduces request storm for transient errors).  If the retry also
    fails: degrade to embedding chunk-by-chunk; any chunk that raises
    individually is logged as a warning and skipped.  The returned lists are
    aligned: chunks_ok[i] produced vecs[i].

    Returns:
        (chunks_ok, vecs, embed_calls)
        - chunks_ok: surviving chunks (may be shorter than input on failures)
        - vecs:      corresponding embedding vectors (same length as chunks_ok)
        - embed_calls: number of embed() calls made (for observability)
    """
    if not chunks:
        return [], [], 0

    texts = [c.content for c in chunks]
    count_before = getattr(embedder, "call_count", None)
    try:
        vecs = embedder.embed(texts)
        count_after = getattr(embedder, "call_count", None)
        if count_before is not None and count_after is not None:
            embed_calls = count_after - count_before
        else:
            embed_calls = 1
        return chunks, vecs, embed_calls
    except Exception as batch_exc:
        _logger.warning(
            "embed batch failed (%s) — retrying full batch once before per-chunk fallback",
            batch_exc,
        )

    # Retry full batch once (fix #7: reduce request storm on transient errors)
    try:
        count_before2 = getattr(embedder, "call_count", None)
        vecs = embedder.embed(texts)
        count_after2 = getattr(embedder, "call_count", None)
        if count_before2 is not None and count_after2 is not None:
            embed_calls = count_after2 - count_before2
        else:
            embed_calls = 1
        _logger.info("embed batch retry succeeded for %d chunks", len(chunks))
        return chunks, vecs, embed_calls
    except Exception as retry_exc:
        _logger.warning(
            "embed batch retry also failed (%s) — degrading to per-chunk embed; "
            "individual failures will be logged and skipped",
            retry_exc,
        )

    # Per-chunk degraded path
    ok_chunks: list[EmbeddingChunk] = []
    ok_vecs: list[list[float]] = []
    embed_calls = 0
    for c in chunks:
        try:
            [vec] = embedder.embed([c.content])
            embed_calls += 1
            ok_chunks.append(c)
            ok_vecs.append(vec)
        except Exception as chunk_exc:
            _logger.warning(
                "embed chunk skipped (module=%s entity=%s file=%s version=%s): %s",
                c.module, c.entity_name, c.file_path, c.odoo_version, chunk_exc,
            )
    return ok_chunks, ok_vecs, embed_calls


def make_chunks(
    module: str,
    version: str,
    parse_result: ParseResult,
    view_result: ViewParseResult | None,
    js_chunks: list[JSChunk] | None,
) -> list[EmbeddingChunk]:
    """Convert ParseResult + ViewParseResult + JSChunks into EmbeddingChunks.

    A3: method/field chunks carry real source file_path (from model.file_path),
    line_start (from method.line / field.line), repo and repo_id (from module).
    view/qweb chunks carry their existing real file_path plus line_start / repo /
    repo_id.  JS chunks carry repo / repo_id.  css/scss/less/pattern helpers are
    not touched here (they have no ParseResult/module context in their callers).
    """
    chunks: list[EmbeddingChunk] = []

    mod = parse_result.module
    mod_repo = mod.repo
    mod_repo_id = mod.repo_id

    for model in parse_result.models:
        # A3: use real source file_path when available; fall back to module dir.
        # ADR-0037: relativize to repo root so the stored file_path is portable
        # (idempotent — a path already relative is returned unchanged).
        model_fp = mod.relative_path(model.file_path or mod.path)

        for method in model.methods:
            prefix = f"[{module}] {model.name}.{method.name} ({version})"
            body = method.source_code or f"def {method.name}(self): ..."
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{method.name}", "method",
                module, version, model_fp, model.name,
                line_start=method.line, repo=mod_repo, repo_id=mod_repo_id,
            ))

        for fld in model.fields:
            prefix = f"[{module}] {model.name}: {fld.name} ({fld.ttype})"
            body = fld.source_definition or f"{fld.name} = fields.{fld.ttype.capitalize()}(...)"
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{fld.name}", "field",
                module, version, model_fp, model.name,
                line_start=fld.line, repo=mod_repo, repo_id=mod_repo_id,
            ))

    if view_result:
        for view in view_result.views:
            inherit_str = f", inherit={view.inherit_xmlid}" if view.inherit_xmlid else ""
            prefix = f"[{module}] {view.xmlid} ({view.view_type}{inherit_str})"
            body = view.arch or f"<!-- arch missing for {view.xmlid} -->"
            fp = mod.relative_path(view.file_path or mod.path)
            chunks.extend(
                _sliding(
                    f"{prefix}\n{body}", view.xmlid, "view", module, version, fp, view.model,
                    line_start=view.line, repo=mod_repo, repo_id=mod_repo_id,
                )
            )

        for qweb in view_result.qweb:
            prefix = f"[{module}] {qweb.xmlid}"
            body = qweb.content or f"<!-- content missing for {qweb.xmlid} -->"
            fp = mod.relative_path(qweb.file_path or mod.path)
            chunks.extend(
                _sliding(
                    f"{prefix}\n{body}", qweb.xmlid, "qweb", module, version, fp, None,
                    line_start=qweb.line, repo=mod_repo, repo_id=mod_repo_id,
                )
            )

    for jsc in (js_chunks or []):
        chunk_type = f"js_{jsc.era}"
        content = jsc.content
        fp = mod.relative_path(jsc.file_path)
        _proto = EmbeddingChunk(
            chunk_type, module, version,
            jsc.entity_name, None, fp,
            0, "",
            repo=mod_repo, repo_id=mod_repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, content, jsc.chunk_idx))

    return chunks


def make_css_chunks(
    css_chunks: list[CSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert CSSChunk list → EmbeddingChunk list (chunk_type='css').

    Each CSSChunk (variable block, selector group, @media query, or raw window)
    becomes one EmbeddingChunk. entity_name encodes the semantic unit label
    (selector text, mixin name, variable group prefix, etc.) for ANN filtering.
    model_name is always None — CSS has no model binding.

    ADR-0037: *module_info* (when supplied) stamps repo + repo_id provenance —
    parity with method/field/view chunks so stylesheet chunks keep their repo
    identity after the file_path is relativized — and relativizes file_path to
    repo-relative form.  None → file_path verbatim, repo/repo_id NULL (back-compat).

    WI-B: each CSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in css_chunks:
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="css",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=c.entity_name,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_scss_chunks(
    scss_chunks: list[SCSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert SCSSChunk list → EmbeddingChunk list (chunk_type='scss').

    Same pattern as make_css_chunks. chunk_kind is embedded into entity_name
    as ``<kind>:<entity_name>`` so ANN results can be filtered by kind
    (e.g. find only mixin definitions across versions) without schema changes.
    model_name is always None — SCSS has no model binding.

    ADR-0037: *module_info* stamps repo + repo_id and relativizes file_path
    (see make_css_chunks).

    WI-B: each SCSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in scss_chunks:
        entity = f"{c.chunk_kind}:{c.entity_name}"
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="scss",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=entity,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_less_chunks(
    less_chunks: list[SCSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert SCSSChunk list (from parser_less) → EmbeddingChunk list (chunk_type='less').

    Mirrors make_scss_chunks exactly — LESS chunks share the SCSSChunk dataclass
    because the structure is identical (mixin/variable/selector/import/media/raw).
    chunk_kind is embedded into entity_name as ``<kind>:<entity_name>`` for ANN
    kind-filtering without schema changes.
    model_name is always None — LESS has no model binding.

    ADR-0037: *module_info* stamps repo + repo_id and relativizes file_path
    (see make_css_chunks).

    WI-B: each SCSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in less_chunks:
        entity = f"{c.chunk_kind}:{c.entity_name}"
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="less",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=entity,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_test_chunks(
    module: str,
    version: str,
    test_result: TestParseResult,
) -> list[EmbeddingChunk]:
    """Convert TestParseResult into EmbeddingChunks for the pgvector store.

    Uses an asymmetric intent-header text shape (design §3.2) so NL queries like
    "how is amount_total tested" match against the header, not just raw method code:

        [test] sale.order.amount_total via TestSaleOrder.test_amount_total (17.0, transaction)
        @tagged('post_install','-at_install')
        def test_amount_total_computed(self): ...

    chunk_type:
    - 'test_method' for individual test methods (test_* prefix)
    - 'test_class' for whole class headers (setUpClass + docstring)
    """
    chunks: list[EmbeddingChunk] = []
    mod = test_result.module
    repo = mod.repo
    repo_id = mod.repo_id

    for tc in test_result.test_classes:
        fp = tc.file_path  # already repo-relative from parser

        # test_class chunk: header + base_classes + docstring
        base_str = ", ".join(tc.base_classes_ordered) if tc.base_classes_ordered else "object"
        class_header_lines = [
            f"[test class] {tc.name}({base_str}) in {module} ({version}, {tc.test_type})",
        ]
        if tc.tagged:
            class_header_lines.append(f"@tagged({', '.join(repr(t) for t in tc.tagged)})")
        if tc.docstring:
            class_header_lines.append(tc.docstring)
        class_content = "\n".join(class_header_lines)
        chunks.extend(_sliding(
            class_content,
            f"{tc.name}.__class__",
            "test_class",
            module, version, fp, None,
            line_start=tc.line,
            repo=repo, repo_id=repo_id,
        ))

        # test_method chunks: one per test_* method
        for meth in tc.methods:
            if not meth.name.startswith("test"):
                continue
            # Build intent header from model_refs[0] and field_refs
            model_hint = meth.model_refs[0] if meth.model_refs else None
            field_hint = meth.field_refs[0] if meth.field_refs else None
            if model_hint and field_hint:
                coverage_str = f"{model_hint}.{field_hint}"
            elif model_hint:
                coverage_str = model_hint
            else:
                coverage_str = "<no model ref>"

            tagged_str = ""
            if meth.tagged:
                tagged_str = f"\n@tagged({', '.join(repr(t) for t in meth.tagged)})"
            header = (
                f"[test] {coverage_str} via {tc.name}.{meth.name} ({version}, {tc.test_type})"
                f"{tagged_str}"
            )
            body = meth.source_code or f"def {meth.name}(self): ..."
            content = f"{header}\n{body}"
            chunks.extend(_sliding(
                content,
                f"{tc.name}.{meth.name}",
                "test_method",
                module, version, fp, model_hint,
                line_start=meth.line,
                repo=repo, repo_id=repo_id,
            ))

    return chunks


def make_js_test_chunks(
    suites: list[JsTestSuiteInfo],
    module: str,
    version: str,
    repo: str | None = None,
    repo_id: int | None = None,
) -> list[EmbeddingChunk]:
    """Convert JsTestSuiteInfo objects into EmbeddingChunks for the pgvector store (WI-3).

    Produces one chunk per JsTestSuiteInfo (file-level, matching §4.4 design decision).
    The intent-header text shape enables NL queries like "Hoot test for account.move":

        [js:hoot] account/static/tests/foo.test.js (18.0)
        describe: X2many buttons
        tests: renders add line, handles keyboard input
        mounts: account.move, account.account
        tags: desktop

    chunk_type='js_test' (already in VALID_CHUNK_TYPES in constants.py).
    """
    chunks: list[EmbeddingChunk] = []
    for suite in suites:
        parts = [
            f"[js:{suite.framework}] {suite.file_path} ({version})",
        ]
        if suite.describe_blocks:
            parts.append(f"describe: {', '.join(suite.describe_blocks)}")
        if suite.test_names:
            # Limit to first 10 test names to keep chunk size reasonable
            names_preview = suite.test_names[:10]
            parts.append(f"tests: {', '.join(names_preview)}")
            if len(suite.test_names) > 10:
                parts.append(f"(+ {len(suite.test_names) - 10} more tests)")
        if suite.mounts:
            parts.append(f"mounts: {', '.join(suite.mounts)}")
        if suite.tags:
            parts.append(f"tags: {', '.join(suite.tags)}")
        if suite.mock_models:
            parts.append(f"mock_models: {', '.join(suite.mock_models[:5])}")

        content = "\n".join(parts)
        _proto = EmbeddingChunk(
            chunk_type="js_test",
            module=module,
            odoo_version=version,
            entity_name=suite.file_path,
            model_name=suite.mounts[0] if suite.mounts else None,
            file_path=suite.file_path,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
            line_start=suite.line,
        )
        chunks.extend(_token_split_chunk(_proto, content, 0))

    return chunks


def make_pattern_chunks(patterns: list[PatternExample]) -> list[EmbeddingChunk]:
    """Convert PatternExample → EmbeddingChunk (chunk_type='pattern_example').

    Per ADR-0003 §4: language is encoded into entity_name slug as
    `<language>__<pattern_id>` so `suggest_pattern` can filter by language
    via B-tree LIKE without ALTERing the embeddings table.
    Module sentinel is `__patterns__`. odoo_version = pattern.odoo_version_min.

    WI-B: large patterns (content > EMBEDDER_TOKEN_BUDGET tokens) are split into
    multiple EmbeddingChunks with increasing chunk_idx. Each split chunk shares
    the same entity_name/file_path/module/odoo_version; unique key is
    (chunk_type, entity_name, file_path, chunk_idx).
    """
    chunks: list[EmbeddingChunk] = []
    for p in patterns:
        text_parts = [p.snippet_text]
        if p.gotchas:
            text_parts.append("---")
            text_parts.extend(p.gotchas)
        text = "\n".join(text_parts)
        entity_name = f"{p.language}__{p.pattern_id}"
        _proto = EmbeddingChunk(
            chunk_type="pattern_example",
            module="__patterns__",
            odoo_version=p.odoo_version_min,
            entity_name=entity_name,
            model_name=None,
            file_path=p.file_ref,
            chunk_idx=0,
            content="",
        )
        chunks.extend(_token_split_chunk(_proto, text, 0))
    return chunks


def write_module_embeddings(
    module: str,
    version: str,
    chunks: list[EmbeddingChunk],
    embedder: EmbedderClient,
    profile_name: str | None = None,
    *,
    replace: bool = True,
) -> int:
    """Delete-then-insert embeddings for (module, version[, profile_name]) atomically.

    ``replace=False`` upserts without the delete: rows of chunks the parse did
    not produce are kept. The pipeline uses it for a module whose parse was
    degraded (a file could not be read or parsed, ADR-0056 B14), so a chunk is
    never dropped because its file was unreadable this run.

    profile_name scopes the delete to a single tenant's chunks so re-indexing
    profile A does not erase profile B's chunks for the same module/version.
    NULL no longer valid post-m13_021 (NOT NULL column) — passing None raises
    ValueError immediately (fail-fast guard), turning a cryptic NotNullViolation
    into a clear error. Global rows (pattern catalogue) use the '__global__'
    sentinel via src.constants.GLOBAL_PROFILE.

    Obtains a pgvector-capable connection from the shared pool (get_pool().checkout_vec()).
    Returns the number of embed() calls made to the embedder during this
    write (0 when chunks is empty, 1 for a normal module batch). Callers
    use this to aggregate embed_calls for the run-level observability log.

    WI-B: uses _embed_chunks_resilient to degrade gracefully on partial failures;
    writes embedding_model and embedding_dim into each row; calls assert_dim_matches
    once per batch as fail-fast guard against incompatible vector spaces.
    """
    if not chunks:
        return 0
    if profile_name is None:
        raise ValueError(
            "write_module_embeddings: profile_name is required (embeddings.profile_name "
            "is NOT NULL post-m13_021). Pass the owning profile, or seed_patterns uses "
            "GLOBAL_PROFILE ('__global__') for catalogue rows."
        )
    # Stamp every chunk with the profile_name supplied at write time.
    # This is cleaner than threading profile_name through every make_*_chunks
    # helper: the helpers remain profile-agnostic; the write call is the single
    # authoritative place that knows which profile owns these chunks.
    for c in chunks:
        c.profile_name = profile_name
    # Dedup by the ux_embeddings_chunk unique key — make_module_chunks can emit
    # the same (chunk_type, entity_name, file_path, chunk_idx) twice for one
    # module (e.g. partial classes split across files). Postgres rejects a
    # single INSERT batch containing such duplicates with `ON CONFLICT DO UPDATE
    # command cannot affect row a second time` even when the ON CONFLICT clause
    # would otherwise resolve them across separate statements. Last-wins.
    seen: dict[tuple, EmbeddingChunk] = {}
    for c in chunks:
        seen[(c.chunk_type, c.entity_name, c.file_path, c.chunk_idx)] = c
    chunks = list(seen.values())

    live_chunks, vecs, embed_calls = _embed_chunks_resilient(embedder, chunks)
    if len(live_chunks) < len(chunks):
        parse_health.note_embeddings_incomplete()

    # Guard: if all chunks failed embedding (total failure), do NOT delete existing
    # rows and insert nothing — that would silently wipe the module's embeddings.
    # Preserve the existing rows and let the caller retry later.
    if not live_chunks:
        _logger.error(
            "embed produced 0 usable vectors for module=%s version=%s — "
            "preserving existing rows, skipping destructive rewrite",
            module, version,
        )
        return embed_calls

    # Embedders that pre-date the provider-abstraction (ADR-0045) — and some test
    # doubles — may not expose .model/.dim. Tolerate their absence: stamp NULL
    # (the columns are nullable) and skip the dim guard rather than crash.
    emb_model, emb_dim = _embedder_meta(embedder)

    from src.db.pg import get_pool  # noqa: PLC0415
    with get_pool().checkout_vec() as conn:
        # Fail-fast guard: raises EmbedderDimMismatch if configured dim != stored dim.
        # Pass emb_model so R5's extended guard can also detect model-switch with
        # same dim (assert_dim_matches signature extended per R5 cross-contract).
        if emb_dim is not None:
            from src.db.embedding_guard import assert_dim_matches  # noqa: PLC0415
            assert_dim_matches(conn, emb_dim, emb_model)

        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(_WRITE_SCOPE_SQL)
                if replace:
                    _delete_module_embeddings_cur(cur, module, version, [profile_name])
                rows = [
                    c.as_tuple(vecs[i], emb_model, emb_dim)
                    for i, c in enumerate(live_chunks)
                ]
                execute_values(cur, _INSERT_SQL, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True  # restore for pool reuse
    return embed_calls


# The embeddings_tenant RLS policy (0001) admits a row only when
# app.allowed_profiles is '*' or lists its profile. After ops/rls_cutover.sh the
# table is FORCEd, so the policy binds the table owner too unless the owner is
# a superuser / BYPASSRLS role (the docker-compose default). The indexer and the
# Web UI never set the GUC, so on a deploy whose owner role does not bypass RLS
# every DELETE / sweep read here would match 0 rows and silently keep ghost
# embeddings (and every INSERT would fail its WITH CHECK). Write-side statements
# therefore run with the unrestricted scope, transaction-local.
_WRITE_SCOPE_SQL = "SELECT set_config('app.allowed_profiles', '*', true)"


@contextmanager
def _write_scope(conn) -> Generator[None, None, None]:
    """Run the block with RLS scope '*' (every profile), transaction-local.

    On an autocommit connection the block becomes one transaction (committed
    on success); inside a caller transaction the scope lasts until the
    caller's transaction ends and nothing is committed here.
    """
    if not conn.autocommit:
        with conn.cursor() as cur:
            cur.execute(_WRITE_SCOPE_SQL)
        yield
        return
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(_WRITE_SCOPE_SQL)
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


_DELETE_MODULE_EMBEDDINGS_SQL = (
    "DELETE FROM embeddings "
    "WHERE module = %s AND odoo_version = %s AND profile_name = ANY(%s)"
)


def _delete_module_embeddings_cur(cur, module: str, version: str, profile_names: list[str]) -> int:
    """Profile-scoped DELETE of one module's chunks on an open cursor."""
    cur.execute(_DELETE_MODULE_EMBEDDINGS_SQL, (module, version, profile_names))
    return cur.rowcount


def delete_module_embeddings(
    conn,
    module: str,
    version: str,
    profile_names: Iterable[str],
    *,
    expected: int | None = None,
) -> int:
    """Delete the embeddings of *module* at *version* owned by *profile_names*.

    The same profile-scoped DELETE the write path uses before re-inserting a
    module (``write_module_embeddings``), exposed for retirement: only rows
    whose ``profile_name`` is in *profile_names* go, so retiring a module from
    one tenant never erases another tenant's chunks for the same name.

    * An empty *profile_names* deletes nothing (returns 0) - never "all
      profiles".
    * ``GLOBAL_PROFILE`` ('__global__', the pattern catalogue) is dropped from
      *profile_names*; catalogue rows are owned by ``seed_patterns``.
    * Runs on the caller's *conn* with the unrestricted RLS scope
      (:func:`_write_scope`, so a FORCEd policy cannot hide the rows): on an
      autocommit connection the DELETE is its own committed transaction;
      inside a caller transaction it commits or rolls back with it.
    * *expected*: the row count a prior read reported (orphan sweep). Fewer
      rows deleted logs a WARNING - the rows were invisible or already gone.

    Returns the number of rows deleted.
    """
    profiles = sorted({p for p in profile_names if p and p != GLOBAL_PROFILE})
    if not profiles:
        return 0
    with _write_scope(conn), conn.cursor() as cur:
        deleted = _delete_module_embeddings_cur(cur, module, version, profiles)
    _warn_short_delete(module, version, profiles, deleted, expected)
    return deleted


def _warn_short_delete(
    module: str, version: str, profiles: list[str], deleted: int, expected: int | None,
) -> None:
    """WARNING when a delete removed fewer rows than a prior read reported."""
    if expected is not None and deleted < expected:
        _logger.warning(
            "embeddings delete for module=%s version=%s profiles=%s removed %d of "
            "%d expected row(s); the rest were invisible to this session (RLS "
            "scope) or deleted concurrently", module, version, ",".join(profiles),
            deleted, expected,
        )


def delete_module_embeddings_except(
    conn,
    module: str,
    version: str,
    profile_name: str,
    keep_keys: Iterable[tuple],
    *,
    expected: int | None = None,
    by_entity: bool = False,
    delete: bool = True,
) -> int:
    """Delete *module*'s rows for *profile_name* whose chunk key is not in *keep_keys*.

    The embedding half of the intra-module entity prune (ADR-0056 B14): the
    pipeline upserts a module's chunks, and once the prune decided the
    module may lose what its parse no longer produced, this removes the
    remaining rows. A key is ``(chunk_type, entity_name, file_path,
    chunk_idx)`` - the ``ux_embeddings_chunk`` identity within one module,
    version and profile. An empty *keep_keys* deletes every row of the
    module for that profile. ``GLOBAL_PROFILE`` is never touched. Runs on
    the caller's *conn* with the unrestricted RLS scope and the same commit
    rules and *expected* WARNING as :func:`delete_module_embeddings` (a
    FORCEd policy must not hide the rows the prune decided to remove).

    *by_entity*: a row is kept when its ``(chunk_type, entity_name)`` is among
    *keep_keys*, whatever its file path or chunk index - the rule of a parse
    that wrote no rows (``--no-embed``): only rows of entities the parse no
    longer produces go, never a live entity's row that simply was not
    re-embedded. *delete* False only counts.

    Returns the number of rows deleted (counted).
    """
    if not profile_name or profile_name == GLOBAL_PROFILE:
        return 0
    keys = sorted(
        {(str(k[0]), k[1], k[2], int(k[3])) for k in keep_keys},
        key=lambda k: (k[0], k[1] or "", k[2] or "", k[3]),
    )
    match = (
        "k.chunk_type = e.chunk_type AND k.entity_name IS NOT DISTINCT FROM e.entity_name"
    )
    if not by_entity:
        match += (
            " AND k.file_path IS NOT DISTINCT FROM e.file_path AND k.chunk_idx = e.chunk_idx"
        )
    verb = "DELETE FROM embeddings e" if delete else "SELECT count(*) FROM embeddings e"
    with _write_scope(conn), conn.cursor() as cur:
        cur.execute(
            f"""
            {verb}
            WHERE e.module = %s AND e.odoo_version = %s AND e.profile_name = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[])
                       AS k(chunk_type, entity_name, file_path, chunk_idx)
                  WHERE {match}
              )
            """,
            (
                module, version, profile_name,
                [k[0] for k in keys], [k[1] for k in keys],
                [k[2] for k in keys], [k[3] for k in keys],
            ),
        )
        deleted = cur.rowcount if delete else cur.fetchone()[0]
    if delete:
        _warn_short_delete(module, version, [profile_name], deleted, expected)
    return deleted


_SHARED_STALE_EMBEDDINGS_WHERE = """
    e.module = %(module)s AND e.odoo_version = %(v)s AND e.profile_name = ANY(%(profiles)s)
    AND NOT EXISTS (
        SELECT 1
        FROM embeddings l
        JOIN unnest(%(owner_profiles)s::text[], %(owner_since)s::timestamptz[])
             AS o(profile_name, since)
          ON l.profile_name = o.profile_name AND l.indexed_at >= o.since
        WHERE l.module = e.module AND l.odoo_version = e.odoo_version
          AND l.chunk_type = e.chunk_type
          AND l.entity_name IS NOT DISTINCT FROM e.entity_name
          AND l.file_path IS NOT DISTINCT FROM e.file_path
          AND l.chunk_idx = e.chunk_idx
    )
"""


def shared_module_stale_embeddings(
    conn,
    module: str,
    version: str,
    owners: Iterable[tuple[str, object]],
    *,
    delete: bool,
    expected: int | None = None,
) -> int:
    """Count or delete the rows of a shared module no owner's latest parse produced.

    The embedding half of the shared-module prune (ADR-0056 B14). *owners* is
    one ``(profile_name, embedded_since)`` per present owner: the PostgreSQL
    time taken before that owner's latest complete parse, whose chunk upserts
    all stamped ``indexed_at`` at or after it. A chunk key (chunk_type,
    entity_name, file_path, chunk_idx) is live when some owner's profile has
    a row of it stamped since that owner's time; every row of the module in
    the owners' profiles whose key is not live goes - every chunk kind
    (fields, methods, views, templates, JS, CSS/SCSS/LESS, tests, JS tests).
    ``GLOBAL_PROFILE`` is never touched. Runs with the unrestricted RLS scope
    (:func:`_write_scope`, a FORCEd policy must not hide rows); *delete*
    False only counts. *expected* (a prior count): fewer rows deleted logs a
    WARNING. Returns the number of rows counted or deleted.
    """
    pairs = [(p, since) for p, since in owners if p and p != GLOBAL_PROFILE]
    profiles = sorted({p for p, _ in pairs})
    if not profiles:
        return 0
    params = {
        "module": module, "v": version, "profiles": profiles,
        "owner_profiles": [p for p, _ in pairs], "owner_since": [s for _, s in pairs],
    }
    verb = "DELETE FROM embeddings e WHERE" if delete else "SELECT count(*) FROM embeddings e WHERE"
    with _write_scope(conn), conn.cursor() as cur:
        cur.execute(f"{verb} {_SHARED_STALE_EMBEDDINGS_WHERE}", params)
        n = cur.rowcount if delete else cur.fetchone()[0]
    if delete:
        _warn_short_delete(module, version, profiles, n, expected)
    return n


def embedding_groups(conn, version: str) -> list[tuple[str, str, int]]:
    """Every ``(module, profile_name, row_count)`` embedding group at *version*.

    Catalogue rows (``profile_name = GLOBAL_PROFILE``) are left out; sorted by
    module then profile. Read-only; the reconcile's orphan embedding sweep
    subtracts the live ``(module, profile)`` owners from it and judges the
    rest (and its size, the baseline of the embedding sweep gate). Reads with
    the unrestricted RLS scope (:func:`_write_scope`): a tenant-scoped view
    would see no orphans at all. Delete a group with
    :func:`delete_module_embeddings`.
    """
    with _write_scope(conn), conn.cursor() as cur:
        cur.execute(
            "SELECT module, profile_name, count(*) FROM embeddings "
            "WHERE odoo_version = %s AND profile_name <> %s "
            "GROUP BY module, profile_name ORDER BY module, profile_name",
            (version, GLOBAL_PROFILE),
        )
        rows = cur.fetchall()
    return [(module, profile, int(count)) for module, profile, count in rows]
