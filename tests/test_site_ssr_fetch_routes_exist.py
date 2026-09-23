# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every FastAPI route the Astro site fetches server-side exists (F31).

Real case: ``site/src/pages/admin/repos.astro`` fetched ``GET /api/repos/repos``
on every render since M8 Wave D. No such route ever existed (``repos_crud``
serves only ``POST /repos`` and ``/repos/{id}/...``), so every render paid a
405 and the page silently relied on a fallback. A page must only call routes
the backend serves, with the method it serves them under.

Scope: SSR ``fetch(`${FASTAPI_BASE}/api/...`)`` calls with a literal path (the
backend contract the pages depend on). Paths built from template variables are
skipped - their shape is not statically known.
"""
from __future__ import annotations

import re
from pathlib import Path

from starlette.routing import Match

SITE_SRC = Path(__file__).resolve().parents[1] / "site" / "src"
_FETCH = re.compile(r"fetch\(\s*`\$\{FASTAPI_BASE\}(/api/[^`$]*)`(?=(?P<rest>[^;]{0,400}))")
_METHOD = re.compile(r"""method\s*:\s*['"](\w+)['"]""")


def _site_calls() -> list[tuple[str, str, str]]:
    calls = []
    for path in sorted(SITE_SRC.rglob("*")):
        if path.suffix not in {".astro", ".ts", ".tsx"} or "__tests__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in _FETCH.finditer(text):
            url = m.group(1).split("?", 1)[0]
            if url.endswith("..."):
                continue  # a doc comment example, not a call
            options = m.group("rest").split("fetch(", 1)[0]
            method = _METHOD.search(options)
            calls.append((str(path.relative_to(SITE_SRC)), url,
                          method.group(1).upper() if method else "GET"))
    return calls


def _served(app, url: str, method: str) -> bool:
    scope = {"type": "http", "path": url, "method": method, "root_path": "",
             "query_string": b"", "headers": []}
    return any(route.matches(scope)[0] is Match.FULL for route in app.routes)


def test_the_site_calls_real_backend_routes():
    from src.web_ui.app import create_app

    app = create_app()
    calls = _site_calls()
    assert any(u == "/api/repos/profiles" for _f, u, _m in calls), (
        "positive control: the admin repos page's /profiles fetch is discovered")

    missing = [(f, m, u) for f, u, m in calls if not _served(app, u, m)]
    assert missing == [], f"site fetches routes the backend does not serve: {missing}"
