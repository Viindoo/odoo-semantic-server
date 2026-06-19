# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Pattern Catalogue CRUD (ADR-0042 + ADR-0007 + ADR-0009).

Routes (admin-only):
- GET    /api/admin/patterns                    list (paginated, filterable)
- GET    /api/admin/patterns/{pattern_id}        single
- POST   /api/admin/patterns                    create (bumps sentinel)
- PATCH  /api/admin/patterns/{pattern_id}        update (bumps sentinel)
- DELETE /api/admin/patterns/{pattern_id}        soft-delete (bumps sentinel)
- POST   /api/admin/patterns/sentinel/recompute  manual sentinel refresh

ADR-0007: any CRUD write recomputes _SeedMeta sentinel SHA -> next index_profile()
run auto-reseeds pgvector chunks for changed patterns.
ADR-0009: minimum 80-pattern regression guard is preserved; soft-deleted rows do
not count toward the minimum.
"""
from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.db.audit import audit_action
from src.web_ui.auth import require_admin, require_admin_with_fresh_mfa
from src.web_ui.routes._admin_helpers import coerce_actor_id

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/patterns", tags=["admin-patterns"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PatternCreate(BaseModel):
    pattern_id: str = Field(
        min_length=3,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9\-_]*$",
    )
    intent_keywords: list[str] = Field(min_length=1, max_length=20)
    file_ref: str = Field(min_length=1, max_length=500)
    snippet_text: str = Field(min_length=1, max_length=50000)
    gotchas: list[dict] = Field(default_factory=list)
    odoo_version_min: str = Field(min_length=1)
    odoo_version_max: str | None = None
    language: Literal["python", "xml", "js"]
    category: Literal["test", "production"] | None = None
    core_symbol_names: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=500)


class PatternPatch(BaseModel):
    intent_keywords: list[str] | None = None
    file_ref: str | None = None
    snippet_text: str | None = None
    gotchas: list[dict] | None = None
    odoo_version_min: str | None = None
    odoo_version_max: str | None = None
    language: Literal["python", "xml", "js"] | None = None
    category: Literal["test", "production"] | None = None
    core_symbol_names: list[str] | None = None
    metadata: dict | None = None
    soft_deleted: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------------
# Sentinel bump helper
# ---------------------------------------------------------------------------


def _bump_sentinel() -> str:
    """Recompute _SeedMeta sentinel SHA from current DB content.

    Delegates to recompute_sentinel_sha() in seed_patterns — that function
    reads the live DB rows (or falls back to JSON when DB is empty) and
    writes the new SHA to both Neo4j sentinel keys.

    Returns the new SHA so the endpoint response can echo it.
    """
    from src.indexer.seed_patterns import recompute_sentinel_sha

    new_sha = recompute_sentinel_sha()
    log.info("Pattern sentinel SHA bumped to %s", new_sha[:12])
    return new_sha


# ---------------------------------------------------------------------------
# GET /api/admin/patterns
# ---------------------------------------------------------------------------


@router.get("")
async def list_patterns(
    include_deleted: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    language: Literal["python", "xml", "js"] | None = None,
    category: Literal["test", "production"] | None = None,
    actor_id: int = Depends(require_admin),
) -> dict:
    """List pattern catalogue with optional filters and pagination."""
    from src.db.pg import get_pool

    pool = get_pool()
    where: list[str] = []
    params: list = []
    if not include_deleted:
        where.append("soft_deleted = FALSE")
    if language:
        where.append("language = %s")
        params.append(language)
    if category:
        where.append("category = %s")
        params.append(category)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT pattern_id, intent_keywords, file_ref, snippet_text, gotchas, "
                f"odoo_version_min, odoo_version_max, language, category, core_symbol_names, "
                f"metadata, soft_deleted, created_at, updated_at "
                f"FROM patterns {clause} ORDER BY pattern_id "
                f"LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = [
                {
                    "pattern_id": r[0],
                    "intent_keywords": r[1],
                    "file_ref": r[2],
                    "snippet_text": r[3],
                    "gotchas": r[4],
                    "odoo_version_min": r[5],
                    "odoo_version_max": r[6],
                    "language": r[7],
                    "category": r[8],
                    "core_symbol_names": r[9],
                    "metadata": r[10],
                    "soft_deleted": r[11],
                    "created_at": r[12].isoformat() if r[12] else None,
                    "updated_at": r[13].isoformat() if r[13] else None,
                }
                for r in cur.fetchall()
            ]
            cur.execute(f"SELECT COUNT(*) FROM patterns {clause}", params)
            total = cur.fetchone()[0]

    return {"patterns": rows, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# GET /api/admin/patterns/sentinel/recompute  (must be before /{pattern_id})
# This path is NOT a pattern_id — declare it before the {pattern_id} route.
# ---------------------------------------------------------------------------

# (The POST /sentinel/recompute is declared last in the module, but that's fine
#  because FastAPI resolves POST vs GET separately.  The GET for a literal
#  "sentinel" pattern_id would still clash — in practice no real pattern_id
#  equals "sentinel".  The sentinel recompute POST is the only sentinel route.)


# ---------------------------------------------------------------------------
# GET /api/admin/patterns/{pattern_id}
# ---------------------------------------------------------------------------


@router.get("/{pattern_id}")
async def get_pattern(
    pattern_id: str,
    actor_id: int = Depends(require_admin),
) -> dict:
    """Fetch a single pattern by pattern_id (includes soft-deleted)."""
    from src.db.pg import get_pool

    pool = get_pool()
    with pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern_id, intent_keywords, file_ref, snippet_text, gotchas, "
                "odoo_version_min, odoo_version_max, language, category, core_symbol_names, "
                "metadata, soft_deleted "
                "FROM patterns WHERE pattern_id = %s",
                (pattern_id,),
            )
            r = cur.fetchone()
            if r is None:
                raise HTTPException(404, "Pattern not found")
            return {
                "pattern_id": r[0],
                "intent_keywords": r[1],
                "file_ref": r[2],
                "snippet_text": r[3],
                "gotchas": r[4],
                "odoo_version_min": r[5],
                "odoo_version_max": r[6],
                "language": r[7],
                "category": r[8],
                "core_symbol_names": r[9],
                "metadata": r[10],
                "soft_deleted": r[11],
            }


# ---------------------------------------------------------------------------
# POST /api/admin/patterns  (create)
# ---------------------------------------------------------------------------


@router.post("")
@audit_action("pattern.create", target_param=None)
async def create_pattern(
    payload: PatternCreate,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Create a new pattern. Bumps ADR-0007 sentinel SHA on success."""
    from src.db.pg import get_pool

    pool = get_pool()
    with pool.checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO patterns
                         (pattern_id, intent_keywords, file_ref, snippet_text,
                          gotchas, odoo_version_min, odoo_version_max, language,
                          category, core_symbol_names, metadata, updated_by)
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s)
                       ON CONFLICT (pattern_id) DO NOTHING""",
                    (
                        payload.pattern_id,
                        payload.intent_keywords,
                        payload.file_ref,
                        payload.snippet_text,
                        json.dumps(payload.gotchas),
                        payload.odoo_version_min,
                        payload.odoo_version_max,
                        payload.language,
                        payload.category,
                        payload.core_symbol_names,
                        json.dumps(payload.metadata),
                        updated_by,
                    ),
                )
                if cur.rowcount == 0:
                    raise HTTPException(409, f"Pattern {payload.pattern_id!r} already exists")
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    new_sha = _bump_sentinel()
    return {
        "pattern_id": payload.pattern_id,
        "created": True,
        "sentinel_sha": new_sha[:16],
        "reseed_status": "pending - next index_profile() run",
    }


# ---------------------------------------------------------------------------
# PATCH /api/admin/patterns/{pattern_id}  (update)
# ---------------------------------------------------------------------------


@router.patch("/{pattern_id}")
@audit_action("pattern.update", target_param="pattern_id")
async def update_pattern(
    pattern_id: str,
    payload: PatternPatch,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Update one or more fields of an existing pattern. Bumps sentinel SHA."""
    updates = payload.model_dump(exclude_none=True, exclude={"reason"})
    if not updates:
        raise HTTPException(400, "No fields to update")

    _JSONB_FIELDS = {"gotchas", "metadata"}
    set_clauses: list[str] = []
    values: list = []
    for col, val in updates.items():
        if col in _JSONB_FIELDS:
            set_clauses.append(f"{col} = %s::jsonb")
            values.append(json.dumps(val))
        else:
            set_clauses.append(f"{col} = %s")
            values.append(val)
    set_clauses += ["updated_at = now()", "updated_by = %s"]

    from src.db.pg import get_pool

    pool = get_pool()
    with pool.checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        values += [updated_by, pattern_id]
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE patterns SET {', '.join(set_clauses)} "
                    f"WHERE pattern_id = %s RETURNING pattern_id",
                    tuple(values),
                )
                if cur.fetchone() is None:
                    raise HTTPException(404, "Pattern not found")
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    new_sha = _bump_sentinel()
    return {"pattern_id": pattern_id, "updated": True, "sentinel_sha": new_sha[:16]}


# ---------------------------------------------------------------------------
# DELETE /api/admin/patterns/{pattern_id}  (soft-delete)
# ---------------------------------------------------------------------------


@router.delete("/{pattern_id}")
@audit_action("pattern.delete", target_param="pattern_id")
async def soft_delete_pattern(
    pattern_id: str,
    actor_id: int = Depends(require_admin_with_fresh_mfa),
) -> dict:
    """Soft-delete a pattern (sets soft_deleted=TRUE). Bumps sentinel SHA."""
    from src.db.pg import get_pool

    pool = get_pool()
    with pool.checkout() as conn:
        # Test bypass sentinel may not exist in webui_users → NULL fallback.
        updated_by = coerce_actor_id(actor_id, conn)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE patterns SET soft_deleted = TRUE, updated_at = now(), "
                    "updated_by = %s "
                    "WHERE pattern_id = %s AND soft_deleted = FALSE "
                    "RETURNING pattern_id",
                    (updated_by, pattern_id),
                )
                if cur.fetchone() is None:
                    raise HTTPException(
                        404, "Pattern not found or already soft-deleted"
                    )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    new_sha = _bump_sentinel()
    return {"pattern_id": pattern_id, "soft_deleted": True, "sentinel_sha": new_sha[:16]}


# ---------------------------------------------------------------------------
# POST /api/admin/patterns/sentinel/recompute  (manual refresh)
# ---------------------------------------------------------------------------


@router.post("/sentinel/recompute")
@audit_action("pattern.sentinel_recompute", target_param=None)
async def recompute_sentinel(
    actor_id: int = Depends(require_admin),
) -> dict:
    """Manually recompute _SeedMeta sentinel SHA from current DB content.

    Normally this is automatic after every CRUD write. Use this endpoint after
    a direct DB intervention (e.g. ops/backfill_patterns.py run) to force
    ADR-0007 auto-reseed on the next index_profile() run.
    """
    new_sha = _bump_sentinel()
    return {"sentinel_sha": new_sha, "manual_recompute": True}
