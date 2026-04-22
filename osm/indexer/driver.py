"""Indexer driver: ties manifest scan, Python parser, and override resolver
into a single idempotent, git-aware, per-file delta pipeline.

Pipeline
--------

1. Scan addon roots -> ManifestRecord[].
2. compute_load_order -> canonical (depth, name) ordering.
3. Per-file blake2b-16 content_hash vs cache_metadata.content_hash:
   - match      -> bump cache_metadata.indexed_at only (no data-table writes)
   - miss/new   -> queue file for re-parse
4. parse_file() across ALL python sources (libcst cost is ms-level; keeps the
   resolver's view global and consistent).
5. compute_resolver_result() -> FieldOverrideLink[] + MethodOverrideLink[].
6. Row-level upsert of modules -> models -> fields -> methods. For each row we
   compare content_hash and only UPDATE when it differs; orphans within a
   re-parsed file are DELETEd.
7. Second pass applies override_of from resolver output (UPDATE only when the
   stored value differs).
8. Write cache_metadata rows for every touched file.

All work runs inside a single transaction per run. search_path is pinned to
the caller-supplied tenant so `tenant` columns default to current_schema().
"""

from __future__ import annotations

import hashlib
import json as _jsonlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from osm.indexer.load_order import compute_load_order
from osm.indexer.manifest import ManifestRecord, scan_addon_roots
from osm.indexer.python_parser import (
    FileParseResult,
    ParsedField,
    ParsedMethod,
    ParsedModel,
    parse_file,
    scan_models_package,
)
from osm.indexer.resolver import (
    FieldOverrideLink,
    MethodOverrideLink,
    compute_resolver_result,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IndexStats:
    """Observable outcome of an index() run. Used by tests + CLI reporting."""

    modules_scanned: int = 0
    modules_upserted: int = 0
    files_reparsed: int = 0
    files_skipped: int = 0
    models_inserted: int = 0
    models_updated: int = 0
    fields_inserted: int = 0
    fields_updated: int = 0
    methods_inserted: int = 0
    methods_updated: int = 0
    rows_deleted: int = 0
    override_links_written: int = 0
    cache_rows_touched: int = 0
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def rows_written(self) -> int:
        return (
            self.modules_upserted
            + self.models_inserted + self.models_updated
            + self.fields_inserted + self.fields_updated
            + self.methods_inserted + self.methods_updated
            + self.override_links_written
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """blake2b-16 hex of file bytes. Streams to avoid large reads."""
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _auto_install_to_bool(value: bool | tuple[str, ...]) -> bool:
    """Collapse auto_install to a single boolean for the modules.auto_install col.

    A non-empty trigger tuple is truthy; `False` / empty tuple is falsy.
    Distinction between bool-True and tuple-form is preserved in parser output
    but the DB column only records activation intent.
    """
    if isinstance(value, tuple):
        return len(value) > 0
    return bool(value)


def _set_search_path(cur: Any, tenant: str) -> None:
    """Pin search_path for the current transaction to <tenant>, public.

    Re-validates the tenant name locally as a fail-fast guard — defence in
    depth, so a future caller that forgets the boundary check still cannot
    inject DDL. Identifier is then double-quoted for Postgres.
    """
    from osm.server.tenancy import validate_tenant
    validate_tenant(tenant)
    cur.execute(f'SET LOCAL search_path TO "{tenant}", public')


def _model_names_for(model: ParsedModel) -> list[str]:
    """Mirror resolver._model_names_for; avoids circular call."""
    if model.name:
        return [model.name]
    if model.inherit:
        return list(model.inherit)
    return []


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------


def _collect_python_files(module: ManifestRecord) -> list[Path]:
    """Return all .py files under <module_dir>/models/ (recursive).

    Includes __init__.py so the conditional-import scanner can be fed later.
    Only models/ is walked in P1; controllers/, wizards/ are out of scope for
    Phase 1 indexer.
    """
    module_dir = module.path.parent
    models_dir = module_dir / "models"
    if not models_dir.is_dir():
        return []
    return sorted(p for p in models_dir.rglob("*.py") if p.is_file())


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------


def _lookup_cache(cur: Any, file_path: str) -> str | None:
    cur.execute(
        "SELECT content_hash FROM cache_metadata WHERE file_path = %s",
        (file_path,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _touch_cache(cur: Any, file_path: str) -> None:
    """Bump indexed_at on an unchanged cache row without rewriting content_hash."""
    cur.execute(
        "UPDATE cache_metadata SET indexed_at = now() WHERE file_path = %s",
        (file_path,),
    )


def _write_cache(
    cur: Any,
    *,
    file_path: str,
    module_name: str,
    content_hash: str,
    git_sha: str,
    file_kind: str,
    byte_size: int,
) -> None:
    cur.execute(
        """
        INSERT INTO cache_metadata
            (file_path, module_name, content_hash, git_sha, file_kind, byte_size)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant, file_path) DO UPDATE SET
            module_name = EXCLUDED.module_name,
            content_hash = EXCLUDED.content_hash,
            git_sha = EXCLUDED.git_sha,
            file_kind = EXCLUDED.file_kind,
            byte_size = EXCLUDED.byte_size,
            indexed_at = now()
        """,
        (file_path, module_name, content_hash, git_sha, file_kind, byte_size),
    )


# ---------------------------------------------------------------------------
# modules upsert
# ---------------------------------------------------------------------------


def _upsert_module(
    cur: Any,
    module: ManifestRecord,
    load_order: int,
    source_repo: str,
    manifest_hash: str,
    git_sha: str,
) -> tuple[int, bool]:
    """Return (module_id, changed). changed=True iff any column shifted."""
    cur.execute(
        """
        SELECT id, manifest_path, version, depends, auto_install, installable,
               load_order, content_hash
          FROM modules
         WHERE name = %s AND source_repo = %s
         LIMIT 1
        """,
        (module.name, source_repo),
    )
    row = cur.fetchone()
    new_values = (
        str(module.path),
        module.version,
        list(module.depends),
        _auto_install_to_bool(module.auto_install),
        module.installable,
        load_order,
        manifest_hash,
    )

    if row is not None:
        (
            mod_id,
            cur_manifest,
            cur_version,
            cur_depends,
            cur_auto,
            cur_installable,
            cur_load_order,
            cur_hash,
        ) = row
        existing = (
            cur_manifest,
            cur_version,
            list(cur_depends) if cur_depends is not None else [],
            cur_auto,
            cur_installable,
            cur_load_order,
            cur_hash,
        )
        if existing == new_values:
            return mod_id, False
        cur.execute(
            """
            UPDATE modules
               SET manifest_path=%s, version=%s, depends=%s, auto_install=%s,
                   installable=%s, load_order=%s, content_hash=%s,
                   indexed_at_sha=%s
             WHERE id=%s
            """,
            (*new_values, git_sha, mod_id),
        )
        return mod_id, True

    cur.execute(
        """
        INSERT INTO modules
          (name, manifest_path, version, depends, auto_install, installable,
           source_repo, load_order, content_hash, indexed_at_sha)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            module.name,
            str(module.path),
            module.version,
            list(module.depends),
            _auto_install_to_bool(module.auto_install),
            module.installable,
            source_repo,
            load_order,
            manifest_hash,
            git_sha,
        ),
    )
    fetched = cur.fetchone()
    assert fetched is not None
    return int(fetched[0]), True


# ---------------------------------------------------------------------------
# models upsert
# ---------------------------------------------------------------------------


def _upsert_model_row(
    cur: Any,
    *,
    module_id: int,
    model_name: str,
    parsed: ParsedModel,
    is_primary: bool,
    git_sha: str,
) -> tuple[int, str]:
    """Return (model_id, action) where action in {"insert", "update", "same"}."""
    cur.execute(
        """
        SELECT id, content_hash, is_primary_declaration, inherits_from, delegates_to,
               "table", rec_name, "order", abstract, transient, file_path,
               start_line, end_line, indexer_notes
          FROM models
         WHERE module_id = %s AND name = %s
        """,
        (module_id, model_name),
    )
    row = cur.fetchone()
    inherits_from = list(parsed.inherit)
    delegates_to: dict[str, str] = dict(parsed.inherits)
    indexer_notes_new: dict[str, Any] = dict(parsed.indexer_notes)

    if row is not None:
        (
            model_id,
            cur_hash,
            cur_primary,
            cur_inherits_from,
            cur_delegates,
            cur_table,
            cur_rec_name,
            cur_order,
            cur_abstract,
            cur_transient,
            cur_file,
            cur_start,
            cur_end,
            cur_notes,
        ) = row
        # If the DB row already points at a different file in the same module,
        # this is a "multiple files extend the same model" case. First-file
        # wins: return the existing id without UPDATE so fields/methods from
        # the second file still attach to the first row.
        if cur_file != parsed.file_path:
            return int(model_id), "same"
        # Same file + same (module, name): compare content-stable attributes.
        cur_notes_dict = dict(cur_notes) if cur_notes else {}
        same = (
            cur_hash == parsed.content_hash
            and bool(cur_primary) == is_primary
            and list(cur_inherits_from or []) == inherits_from
            and dict(cur_delegates or {}) == delegates_to
            and cur_table == parsed.table
            and cur_rec_name == parsed.rec_name
            and cur_order == parsed.order
            and bool(cur_abstract) == parsed.abstract
            and bool(cur_transient) == parsed.transient
            and cur_start == parsed.start_line
            and cur_end == parsed.end_line
            and cur_notes_dict == indexer_notes_new
        )
        if same:
            return int(model_id), "same"
        cur.execute(
            """
            UPDATE models
               SET is_primary_declaration=%s, inherits_from=%s, delegates_to=%s::jsonb,
                   "table"=%s, rec_name=%s, "order"=%s, abstract=%s, transient=%s,
                   file_path=%s, start_line=%s, end_line=%s, content_hash=%s,
                   indexed_at_sha=%s, indexer_notes=%s::jsonb
             WHERE id=%s
            """,
            (
                is_primary,
                inherits_from,
                _json(delegates_to),
                parsed.table,
                parsed.rec_name,
                parsed.order,
                parsed.abstract,
                parsed.transient,
                parsed.file_path,
                parsed.start_line,
                parsed.end_line,
                parsed.content_hash,
                git_sha,
                _json(indexer_notes_new),
                model_id,
            ),
        )
        return int(model_id), "update"

    cur.execute(
        """
        INSERT INTO models
          (name, module_id, is_primary_declaration, inherits_from, delegates_to,
           "table", rec_name, "order", abstract, transient, file_path,
           start_line, end_line, content_hash, indexed_at_sha, indexer_notes)
        VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING id
        """,
        (
            model_name,
            module_id,
            is_primary,
            inherits_from,
            _json(delegates_to),
            parsed.table,
            parsed.rec_name,
            parsed.order,
            parsed.abstract,
            parsed.transient,
            parsed.file_path,
            parsed.start_line,
            parsed.end_line,
            parsed.content_hash,
            git_sha,
            _json(indexer_notes_new),
        ),
    )
    fetched = cur.fetchone()
    assert fetched is not None
    return int(fetched[0]), "insert"


def _json(obj: dict[str, Any]) -> str:
    """Render a dict as JSON text for ::jsonb casts. Keys are sorted for
    deterministic compare across re-runs."""
    return _jsonlib.dumps(obj, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# fields upsert
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# methods upsert
# ---------------------------------------------------------------------------


def _upsert_method_row(
    cur: Any,
    *,
    model_id: int,
    parsed: ParsedMethod,
    file_path: str,
    git_sha: str,
) -> tuple[int, str]:
    cur.execute(
        """
        SELECT id, content_hash, signature, decorators, calls_super,
               file_path, start_line, end_line
          FROM methods
         WHERE model_id = %s AND method_name = %s
        """,
        (model_id, parsed.method_name),
    )
    row = cur.fetchone()
    dec_list = list(parsed.decorators)

    if row is not None:
        (
            method_id, cur_hash, cur_sig, cur_dec, cur_calls_super,
            cur_file, cur_start, cur_end,
        ) = row
        same = (
            cur_hash == parsed.content_hash
            and cur_sig == parsed.signature
            and list(cur_dec or []) == dec_list
            and bool(cur_calls_super) == parsed.calls_super
            and cur_file == file_path
            and cur_start == parsed.start_line
            and cur_end == parsed.end_line
        )
        if same:
            return method_id, "same"
        cur.execute(
            """
            UPDATE methods
               SET signature=%s, decorators=%s, calls_super=%s,
                   file_path=%s, start_line=%s, end_line=%s,
                   content_hash=%s, indexed_at_sha=%s
             WHERE id=%s
            """,
            (
                parsed.signature,
                dec_list,
                parsed.calls_super,
                file_path,
                parsed.start_line,
                parsed.end_line,
                parsed.content_hash,
                git_sha,
                method_id,
            ),
        )
        return method_id, "update"

    cur.execute(
        """
        INSERT INTO methods
          (model_id, method_name, signature, decorators, calls_super,
           file_path, start_line, end_line, content_hash, indexed_at_sha)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            model_id,
            parsed.method_name,
            parsed.signature,
            dec_list,
            parsed.calls_super,
            file_path,
            parsed.start_line,
            parsed.end_line,
            parsed.content_hash,
            git_sha,
        ),
    )
    fetched = cur.fetchone()
    assert fetched is not None
    return int(fetched[0]), "insert"


# ---------------------------------------------------------------------------
# Orphan deletion
# ---------------------------------------------------------------------------


def _delete_orphans_in_file(
    cur: Any,
    *,
    file_path: str,
    keep_model_keys: set[tuple[int, str]],
    keep_field_keys: set[tuple[int, str]],
    keep_method_keys: set[tuple[int, str]],
) -> int:
    """Delete rows whose file_path matches but whose natural key is no longer
    produced by the parser. Returns count of deleted rows.
    """
    total = 0
    # fields
    cur.execute(
        "SELECT id, model_id, field_name FROM fields WHERE file_path = %s",
        (file_path,),
    )
    to_del_field_ids: list[int] = []
    for fid, model_id, fname in cur.fetchall():
        if (model_id, fname) not in keep_field_keys:
            to_del_field_ids.append(fid)
    if to_del_field_ids:
        cur.execute("DELETE FROM fields WHERE id = ANY(%s)", (to_del_field_ids,))
        total += len(to_del_field_ids)

    # methods
    cur.execute(
        "SELECT id, model_id, method_name FROM methods WHERE file_path = %s",
        (file_path,),
    )
    to_del_method_ids: list[int] = []
    for mid, model_id, mname in cur.fetchall():
        if (model_id, mname) not in keep_method_keys:
            to_del_method_ids.append(mid)
    if to_del_method_ids:
        cur.execute("DELETE FROM methods WHERE id = ANY(%s)", (to_del_method_ids,))
        total += len(to_del_method_ids)

    # models (cascades to fields+methods; cheaper to leave model rows that a
    # later parse may re-emit). Only delete when the file no longer declares
    # that (module_id, name).
    cur.execute(
        "SELECT id, module_id, name FROM models WHERE file_path = %s",
        (file_path,),
    )
    to_del_model_ids: list[int] = []
    for id_, module_id, name in cur.fetchall():
        if (module_id, name) not in keep_model_keys:
            to_del_model_ids.append(id_)
    if to_del_model_ids:
        cur.execute("DELETE FROM models WHERE id = ANY(%s)", (to_del_model_ids,))
        total += len(to_del_model_ids)

    return total


# ---------------------------------------------------------------------------
# Override_of write-back
# ---------------------------------------------------------------------------


def _apply_override_links(
    cur: Any,
    field_links: list[FieldOverrideLink],
    method_links: list[MethodOverrideLink],
    field_id_map: dict[tuple[str, str, str, int], int],
    method_id_map: dict[tuple[str, str, str, int], int],
) -> int:
    """Set fields.override_of / methods.override_of to match resolver output.

    The resolver emits one link per ParsedField / ParsedMethod, but the DB
    collapses multiple ParsedX within the same module to a single row (the
    last-wins class under UNIQUE(model_id, name)). To make override_of
    cross-module-meaningful and re-run idempotent we walk each (model, name)
    chain in resolver order, deduplicate by DB row id, and write each row's
    override_of to the id of the row from the previous module in the chain.

    Returns count of rows whose override_of was changed.
    """
    from collections import defaultdict

    changed = 0

    # Fields ----------------------------------------------------------------
    field_groups: dict[tuple[str, str], list[FieldOverrideLink]] = defaultdict(list)
    for link in field_links:
        if link.source_row is None:
            continue  # synthesized — not persisted in fields table
        field_groups[(link.model_name, link.field_name)].append(link)

    for chain in field_groups.values():
        seen: set[int] = set()
        prev_row: int | None = None
        for link in chain:
            assert link.source_row is not None
            key = (link.model_name, link.module_name, link.field_name,
                   link.source_row.start_line)
            my_id = field_id_map.get(key)
            if my_id is None:
                continue
            if my_id in seen:
                continue  # same DB row as an earlier link in this group
            seen.add(my_id)
            cur.execute("SELECT override_of FROM fields WHERE id = %s", (my_id,))
            row = cur.fetchone()
            if row is None:
                continue
            if row[0] != prev_row:
                cur.execute(
                    "UPDATE fields SET override_of = %s WHERE id = %s",
                    (prev_row, my_id),
                )
                changed += 1
            prev_row = my_id

    # Methods ---------------------------------------------------------------
    method_groups: dict[tuple[str, str], list[MethodOverrideLink]] = defaultdict(list)
    for mlink in method_links:
        if mlink.source_row is None:
            continue
        method_groups[(mlink.model_name, mlink.method_name)].append(mlink)

    for mchain in method_groups.values():
        m_seen: set[int] = set()
        m_prev_row: int | None = None
        for mlink in mchain:
            assert mlink.source_row is not None
            mkey = (mlink.model_name, mlink.module_name, mlink.method_name,
                    mlink.source_row.start_line)
            my_id = method_id_map.get(mkey)
            if my_id is None:
                continue
            if my_id in m_seen:
                continue
            m_seen.add(my_id)
            cur.execute("SELECT override_of FROM methods WHERE id = %s", (my_id,))
            row = cur.fetchone()
            if row is None:
                continue
            if row[0] != m_prev_row:
                cur.execute(
                    "UPDATE methods SET override_of = %s WHERE id = %s",
                    (m_prev_row, my_id),
                )
                changed += 1
            m_prev_row = my_id

    return changed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class _FilePlan:
    module_name: str
    module_id: int
    path: Path
    file_path_key: str  # str(path) — used as cache key + DB column
    file_hash: str
    byte_size: int
    reparse: bool  # True iff cache miss or content changed


def index(
    addon_roots: Sequence[Path],
    conn: Any,
    tenant: str,
    git_sha: str,
) -> IndexStats:
    """Run a full idempotent index pass.

    Caller owns the psycopg connection. Everything executes in a single
    transaction opened by the caller (autocommit=False). On success the
    caller commits; on exception the caller rolls back.

    `tenant` is pinned into search_path so unqualified DDL/DML lands in the
    tenant schema. The public schema is second in the path so shared tables
    (if any) remain visible for reads, matching ADR-0004.
    """
    stats = IndexStats()
    manifests = scan_addon_roots(list(addon_roots))
    load_orders = compute_load_order(manifests)
    lo_map = {r.name: r for r in load_orders}
    # Only materialise modules that survived the load-order simulator
    active_manifests = [m for m in manifests if m.name in lo_map]
    stats.modules_scanned = len(active_manifests)

    # Determine each module's source_repo (parent of module dir that was scanned).
    module_source_repo: dict[str, str] = {}
    for mr in active_manifests:
        # mr.path is <root>/<module_name>/__manifest__.py; root is mr.path.parent.parent
        module_source_repo[mr.name] = str(mr.path.parent.parent)

    with conn.cursor() as cur:
        _set_search_path(cur, tenant)

        # ---- modules upsert ----
        module_ids: dict[str, int] = {}
        for mr in active_manifests:
            manifest_hash = _hash_file(mr.path)
            mod_id, changed = _upsert_module(
                cur,
                module=mr,
                load_order=lo_map[mr.name].load_order,
                source_repo=module_source_repo[mr.name],
                manifest_hash=manifest_hash,
                git_sha=git_sha,
            )
            module_ids[mr.name] = mod_id
            if changed:
                stats.modules_upserted += 1

            # cache_metadata for __manifest__.py
            prev_manifest_hash = _lookup_cache(cur, str(mr.path))
            if prev_manifest_hash == manifest_hash:
                _touch_cache(cur, str(mr.path))
            else:
                _write_cache(
                    cur,
                    file_path=str(mr.path),
                    module_name=mr.name,
                    content_hash=manifest_hash,
                    git_sha=git_sha,
                    file_kind="manifest",
                    byte_size=mr.path.stat().st_size,
                )
            stats.cache_rows_touched += 1

        # ---- plan python files ----
        plans: list[_FilePlan] = []
        parsed_results: dict[str, FileParseResult] = {}
        # conditional-import cache per module (scanned once per index run)
        conditional_map: dict[str, set[str]] = {}
        for mr in active_manifests:
            init_path = mr.path.parent / "models" / "__init__.py"
            if init_path.is_file():
                conditional_map[mr.name] = scan_models_package(init_path)
            else:
                conditional_map[mr.name] = set()

        for mr in active_manifests:
            for py in _collect_python_files(mr):
                key = str(py)
                file_hash = _hash_file(py)
                prev = _lookup_cache(cur, key)
                reparse = prev != file_hash
                plans.append(
                    _FilePlan(
                        module_name=mr.name,
                        module_id=module_ids[mr.name],
                        path=py,
                        file_path_key=key,
                        file_hash=file_hash,
                        byte_size=py.stat().st_size,
                        reparse=reparse,
                    )
                )

        # Parse EVERY file (reparse or not) so the resolver sees a complete
        # universe. libcst is fast enough at P1 scale.
        for plan in plans:
            parsed_results[plan.file_path_key] = parse_file(
                plan.path,
                conditional_submodules=conditional_map.get(plan.module_name),
            )
            if plan.reparse:
                stats.files_reparsed += 1
            else:
                stats.files_skipped += 1

        # ---- compute resolver output ----
        resolver_result = compute_resolver_result(
            list(parsed_results.values()),
            load_orders,
        )
        stats.warnings.extend(resolver_result.warnings)

        # ---- upsert rows for reparsed files only ----
        # id maps keyed by (model_name, module_name, entity_name, start_line)
        field_id_map: dict[tuple[str, str, str, int], int] = {}
        method_id_map: dict[tuple[str, str, str, int], int] = {}

        for plan in plans:
            if not plan.reparse:
                # Still need id_map entries for override-of linking; collect
                # from DB for cached files.
                _populate_id_maps_from_db(
                    cur,
                    plan=plan,
                    field_id_map=field_id_map,
                    method_id_map=method_id_map,
                )
                continue

            result = parsed_results[plan.file_path_key]
            _upsert_file_rows(
                cur,
                plan=plan,
                parse_result=result,
                git_sha=git_sha,
                stats=stats,
                field_id_map=field_id_map,
                method_id_map=method_id_map,
            )

            # cache_metadata
            _write_cache(
                cur,
                file_path=plan.file_path_key,
                module_name=plan.module_name,
                content_hash=plan.file_hash,
                git_sha=git_sha,
                file_kind="python",
                byte_size=plan.byte_size,
            )
            stats.cache_rows_touched += 1

        # Unchanged files get their indexed_at bumped (no content write).
        for plan in plans:
            if plan.reparse:
                continue
            _touch_cache(cur, plan.file_path_key)
            stats.cache_rows_touched += 1

        # ---- override_of write-back ----
        link_updates = _apply_override_links(
            cur,
            resolver_result.field_chains,
            resolver_result.method_chains,
            field_id_map,
            method_id_map,
        )
        stats.override_links_written = link_updates

    return stats


# ---------------------------------------------------------------------------
# Row writer for a single re-parsed file
# ---------------------------------------------------------------------------


def _upsert_file_rows(
    cur: Any,
    *,
    plan: _FilePlan,
    parse_result: FileParseResult,
    git_sha: str,
    stats: IndexStats,
    field_id_map: dict[tuple[str, str, str, int], int],
    method_id_map: dict[tuple[str, str, str, int], int],
) -> None:
    # Build per-file natural-key keep-sets so orphans can be deleted.
    keep_model_keys: set[tuple[int, str]] = set()
    keep_field_keys: set[tuple[int, str]] = set()
    keep_method_keys: set[tuple[int, str]] = set()

    # Track which ParsedModel produced each (module_id, model_name) so fields
    # and methods attach to the right model row. first-file-wins if the same
    # module emits multiple ParsedModel for the same model_name.
    model_row_for: dict[tuple[int, str], tuple[int, ParsedModel]] = {}

    for parsed_model in parse_result.models:
        if parsed_model.indexer_notes.get("dynamic_inherit"):
            # spec §5c case 3: omit from chain; still needs a models row so
            # resolve_model can flag it. is_primary depends on whether _name set.
            pass
        names = _model_names_for(parsed_model)
        for mname in names:
            key = (plan.module_id, mname)
            if key in model_row_for:
                # First-file wins for the `models` row's own metadata
                # (file_path, start_line, inherits_from, indexer_notes).
                # Fields/methods from THIS file still attach to that same
                # model_id via their own UNIQUE(model_id, name) — the
                # upsert path handles name collisions per-field/method
                # with last-wins DB semantics. This is not data loss.
                stats.warnings.append(
                    f"module {plan.module_name!r} has multiple ParsedModel "
                    f"for {mname!r}; model row pinned to "
                    f"{model_row_for[key][1].file_path}:L{model_row_for[key][1].start_line}; "
                    "fields/methods from additional files merge into that row."
                )
                continue
            is_primary = bool(parsed_model.name)  # _name set -> primary declaration
            # For extension classes contributing to inherited model, is_primary=False.
            if parsed_model.name is None:
                is_primary = False
            model_id, action = _upsert_model_row(
                cur,
                module_id=plan.module_id,
                model_name=mname,
                parsed=parsed_model,
                is_primary=is_primary,
                git_sha=git_sha,
            )
            if action == "insert":
                stats.models_inserted += 1
            elif action == "update":
                stats.models_updated += 1
            model_row_for[key] = (model_id, parsed_model)
            keep_model_keys.add(key)

    # fields
    for pf in parse_result.fields:
        # Locate the parent ParsedModel this field belongs to (by class_name).
        parent_model: ParsedModel | None = None
        for m in parse_result.models:
            if m.class_name == pf.model_class_name:
                parent_model = m
                break
        if parent_model is None:
            continue
        for mname in _model_names_for(parent_model):
            model_id_tuple = model_row_for.get((plan.module_id, mname))
            if model_id_tuple is None:
                continue
            model_id = model_id_tuple[0]
            fid, action = _upsert_field_row_via_driver(
                cur,
                model_id=model_id,
                parsed=pf,
                file_path=parent_model.file_path,
                git_sha=git_sha,
            )
            if action == "insert":
                stats.fields_inserted += 1
            elif action == "update":
                stats.fields_updated += 1
            keep_field_keys.add((model_id, pf.field_name))
            field_id_map[(mname, plan.module_name, pf.field_name, pf.start_line)] = fid

    # methods
    for pm in parse_result.methods:
        parent_model = None
        for m in parse_result.models:
            if m.class_name == pm.model_class_name:
                parent_model = m
                break
        if parent_model is None:
            continue
        for mname in _model_names_for(parent_model):
            model_id_tuple = model_row_for.get((plan.module_id, mname))
            if model_id_tuple is None:
                continue
            model_id = model_id_tuple[0]
            mid, action = _upsert_method_row(
                cur,
                model_id=model_id,
                parsed=pm,
                file_path=parent_model.file_path,
                git_sha=git_sha,
            )
            if action == "insert":
                stats.methods_inserted += 1
            elif action == "update":
                stats.methods_updated += 1
            keep_method_keys.add((model_id, pm.method_name))
            method_id_map[(mname, plan.module_name, pm.method_name, pm.start_line)] = mid

    deleted = _delete_orphans_in_file(
        cur,
        file_path=plan.file_path_key,
        keep_model_keys=keep_model_keys,
        keep_field_keys=keep_field_keys,
        keep_method_keys=keep_method_keys,
    )
    stats.rows_deleted += deleted


def _upsert_field_row_via_driver(
    cur: Any,
    *,
    model_id: int,
    parsed: ParsedField,
    file_path: str,
    git_sha: str,
) -> tuple[int, str]:
    """Field row upsert that knows the file_path (ParsedField itself does not)."""
    cur.execute(
        """
        SELECT id, content_hash, field_type, related_model, related_field,
               compute, inverse, search, store, required, readonly, "default",
               related_path, depends, file_path, start_line, end_line
          FROM fields
         WHERE model_id = %s AND field_name = %s
        """,
        (model_id, parsed.field_name),
    )
    row = cur.fetchone()
    depends_list = list(parsed.depends)

    if row is not None:
        (
            field_id, cur_hash, cur_type, cur_rmodel, cur_rfield, cur_compute,
            cur_inverse, cur_search, cur_store, cur_required, cur_readonly,
            cur_default, cur_related_path, cur_depends, cur_file,
            cur_start, cur_end,
        ) = row
        # P1 always inserts related_field=None — the parser does not yet split
        # `related="a.b.c"` into (related_model="a", related_field="b.c"). When
        # P2 populates it, replace `cur_rfield is None` with a proper compare
        # OR the UPDATE will never fire on drift.
        same = (
            cur_hash == parsed.content_hash
            and cur_type == parsed.field_type
            and cur_rmodel == parsed.comodel_name
            and cur_rfield is None
            and cur_compute == parsed.compute
            and cur_inverse == parsed.inverse
            and cur_search == parsed.search
            and cur_store == parsed.store
            and cur_required == parsed.required
            and cur_readonly == parsed.readonly
            and cur_default == parsed.default_source
            and cur_related_path == parsed.related
            and list(cur_depends or []) == depends_list
            and cur_file == file_path
            and cur_start == parsed.start_line
            and cur_end == parsed.end_line
        )
        if same:
            return field_id, "same"
        cur.execute(
            """
            UPDATE fields
               SET field_type=%s, related_model=%s, related_field=%s,
                   compute=%s, inverse=%s, search=%s, store=%s, required=%s,
                   readonly=%s, "default"=%s, related_path=%s, depends=%s,
                   file_path=%s, start_line=%s, end_line=%s,
                   content_hash=%s, indexed_at_sha=%s
             WHERE id=%s
            """,
            (
                parsed.field_type, parsed.comodel_name, None,  # related_field: P1 always None
                parsed.compute, parsed.inverse, parsed.search,
                parsed.store, parsed.required, parsed.readonly,
                parsed.default_source, parsed.related, depends_list,
                file_path, parsed.start_line, parsed.end_line,
                parsed.content_hash, git_sha, field_id,
            ),
        )
        return field_id, "update"

    cur.execute(
        """
        INSERT INTO fields
          (model_id, field_name, field_type, related_model, related_field,
           compute, inverse, search, store, required, readonly, "default",
           related_path, depends, file_path, start_line, end_line,
           content_hash, indexed_at_sha)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            model_id, parsed.field_name, parsed.field_type,
            parsed.comodel_name, None, parsed.compute, parsed.inverse,
            parsed.search, parsed.store, parsed.required, parsed.readonly,
            parsed.default_source, parsed.related, depends_list,
            file_path, parsed.start_line, parsed.end_line,
            parsed.content_hash, git_sha,
        ),
    )
    fetched = cur.fetchone()
    assert fetched is not None
    return int(fetched[0]), "insert"


def _populate_id_maps_from_db(
    cur: Any,
    *,
    plan: _FilePlan,
    field_id_map: dict[tuple[str, str, str, int], int],
    method_id_map: dict[tuple[str, str, str, int], int],
) -> None:
    """For unchanged files, look up existing row ids so override_of links can
    still be cross-referenced.

    Key format matches the resolver's link keys:
    (model_name, module_name, field_or_method_name, start_line).
    """
    cur.execute(
        """
        SELECT m.name, f.field_name, f.start_line, f.id
          FROM fields f
          JOIN models m ON m.id = f.model_id
         WHERE f.file_path = %s AND m.module_id = %s
        """,
        (plan.file_path_key, plan.module_id),
    )
    for model_name, field_name, start_line, fid in cur.fetchall():
        field_id_map[(model_name, plan.module_name, field_name, start_line)] = fid

    cur.execute(
        """
        SELECT m.name, me.method_name, me.start_line, me.id
          FROM methods me
          JOIN models m ON m.id = me.model_id
         WHERE me.file_path = %s AND m.module_id = %s
        """,
        (plan.file_path_key, plan.module_id),
    )
    for model_name, method_name, start_line, mid in cur.fetchall():
        method_id_map[(model_name, plan.module_name, method_name, start_line)] = mid


# ---------------------------------------------------------------------------
# Public helpers re-exported for tests
# ---------------------------------------------------------------------------


__all__ = [
    "IndexStats",
    "index",
]
