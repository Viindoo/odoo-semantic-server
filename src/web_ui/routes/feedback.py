# src/web_ui/routes/feedback.py
"""Pattern feedback API route — thumbs-up/down ratings for PatternExample nodes."""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from src.db.audit import audit_action

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback")


class FeedbackBody(BaseModel):
    pattern_id: str
    rating: str  # "up" or "down"
    comment: str | None = None


@router.post("")
@audit_action("feedback.submit")
async def submit_feedback(body: FeedbackBody, request: Request):
    """Submit a thumbs-up or thumbs-down rating for a PatternExample.

    The api_key_id is taken from request.state (set by AuthMiddleware if present),
    or None for anonymous submissions (Web UI loopback requests).
    """
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=422, detail="rating must be 'up' or 'down'")

    # api_key_id from MCP auth middleware (may be absent in Web UI context)
    api_key_id = getattr(request.state, "api_key_id", None)

    try:
        from src.db.pg import auth_store

        feedback_id = auth_store().create_feedback(
            pattern_node_id=body.pattern_id,
            api_key_id=api_key_id,
            rating=body.rating,
            comment=body.comment,
        )
        return JSONResponse({"ok": True, "id": feedback_id})
    except Exception as e:
        _logger.error("Failed to store feedback: %s", e)
        raise HTTPException(status_code=500, detail="Failed to store feedback")


@router.get("/{pattern_id:path}")
async def get_feedback(pattern_id: str, request: Request):
    """List all feedback entries for a given pattern node id."""
    try:
        from src.db.pg import auth_store

        results = auth_store().list_feedback(pattern_id)
        return JSONResponse({"pattern_id": pattern_id, "feedback": results})
    except Exception as e:
        _logger.error("Failed to retrieve feedback: %s", e)
        raise HTTPException(status_code=500, detail="Failed to retrieve feedback")
