# src/web_ui/routes/operations.py
"""Operations page — long-running indexer commands with background job tracking."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

_logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request):
    """Render operations shell page."""
    templates = request.app.state.templates
    flash = request.query_params.get("flash")
    return templates.TemplateResponse(
        request,
        "operations.html",
        {"flash": flash},
    )
