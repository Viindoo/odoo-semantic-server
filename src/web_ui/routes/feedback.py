# src/web_ui/routes/feedback.py
"""Pattern feedback API route — thumbs-up/down ratings for PatternExample nodes."""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_logger = logging.getLogger(__name__)
router = APIRouter()


def _get_conn():
    """Open a PostgreSQL connection using PG_DSN from config/env. Returns None on failure."""
    import psycopg2

    from src import config

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        return None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as e:
        _logger.warning("Cannot connect to PostgreSQL for feedback route: %s", e)
        return None


class FeedbackBody(BaseModel):
    pattern_id: str
    rating: str  # "up" or "down"
    comment: str | None = None


@router.post("/api/feedback")
async def submit_feedback(body: FeedbackBody, request: Request):
    """Submit a thumbs-up or thumbs-down rating for a PatternExample.

    The api_key_id is taken from request.state (set by AuthMiddleware if present),
    or None for anonymous submissions (Web UI loopback requests).
    """
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=422, detail="rating must be 'up' or 'down'")

    # api_key_id from MCP auth middleware (may be absent in Web UI context)
    api_key_id = getattr(request.state, "api_key_id", None)

    conn = _get_conn()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        from src.db.auth_registry import create_feedback

        feedback_id = create_feedback(
            conn,
            pattern_node_id=body.pattern_id,
            api_key_id=api_key_id,
            rating=body.rating,
            comment=body.comment,
        )
        return JSONResponse({"ok": True, "id": feedback_id})
    except Exception as e:
        _logger.error("Failed to store feedback: %s", e)
        raise HTTPException(status_code=500, detail="Failed to store feedback")
    finally:
        conn.close()


@router.get("/api/feedback/{pattern_id:path}")
async def get_feedback(pattern_id: str, request: Request):
    """List all feedback entries for a given pattern node id."""
    conn = _get_conn()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        from src.db.auth_registry import list_feedback

        results = list_feedback(conn, pattern_id)
        return JSONResponse({"pattern_id": pattern_id, "feedback": results})
    except Exception as e:
        _logger.error("Failed to retrieve feedback: %s", e)
        raise HTTPException(status_code=500, detail="Failed to retrieve feedback")
    finally:
        conn.close()
