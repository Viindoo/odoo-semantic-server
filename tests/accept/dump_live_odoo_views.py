"""Dump canonical view arch for ``top50_views.json`` from a live Odoo CE.

Boots a minimal Odoo 17 environment in-process, iterates every xmlid in
``tests/accept/top50_views.json`` (or a file passed via ``--views-json``),
resolves each via ``env.ref(xmlid)``, calls ``_get_combined_arch()``, and
writes the canonicalised XML to ``tests/fixtures/golden/resolve_view_live/``.

Studio-origin views are skipped: they live in the DB only and have no
source-file equivalent, so the MCP index cannot reproduce them.

Output filenames escape dots → underscores, e.g.
``base.view_res_partner_form`` → ``base__view_res_partner_form.xml``.

Idempotent: re-running overwrites existing files byte-for-byte when the
DB state is unchanged.

Run under an Odoo 17 venv with the addons path covering all CE modules:

    /path/to/odoo17/venv/bin/python tests/accept/dump_live_odoo_views.py \\
        --config /etc/odoo/odoo.conf \\
        --database odoo17_full \\
        --views-json tests/accept/top50_views.json \\
        --out-dir tests/fixtures/golden/resolve_view_live

Expected duration: 5-10 minutes for 50 views on first run (Odoo boot +
arch combine; a warm Postgres cache drops subsequent runs to ~1 minute).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent

_logger = logging.getLogger("dump_live_odoo_views")


# ---------------------------------------------------------------------------
# Odoo bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_odoo(
    config: str | None,
    addons_path: str | None,
    database: str,
) -> Any:
    """Parse Odoo config + open a cursor-backed ``Environment`` for ``database``.

    Returns the Odoo ``api.Environment`` ready for ``env.ref(...)``.
    Caller is responsible for closing the cursor once the dump finishes.
    """
    import odoo  # type: ignore[import-not-found]
    from odoo import api
    from odoo.tools import config as odoo_config  # type: ignore[import-not-found]

    args: list[str] = []
    if config:
        args.extend(["-c", config])
    if addons_path:
        args.extend(["--addons-path", addons_path])
    args.extend(["-d", database])
    odoo_config.parse_config(args)

    registry = odoo.modules.registry.Registry(database)
    cr = registry.cursor()
    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
    return env


# ---------------------------------------------------------------------------
# View iteration
# ---------------------------------------------------------------------------


def _load_view_list(views_json: Path) -> list[str]:
    blob = json.loads(views_json.read_text(encoding="utf-8"))
    views = blob.get("views") or []
    return [v["xmlid"] for v in views if isinstance(v, dict) and v.get("xmlid")]


def _escape_xmlid(xmlid: str) -> str:
    # base.view_res_partner_form → base__view_res_partner_form
    return xmlid.replace(".", "__")


def _is_studio_origin(view: Any) -> bool:
    """Best-effort Studio detection.

    Studio views carry a ``<xpath expr="." position="before">...</xpath>``
    arch and a ``model_data`` row with ``module='studio_customization'``.
    Treat any xmlid starting with ``studio_customization.`` as Studio, and
    also inspect ``xml_id`` module as a secondary signal.
    """
    xml_id = view.get_external_id().get(view.id, "")
    if xml_id.startswith("studio_customization."):
        return True
    # Secondary check: metadata model_data_id module
    md = view.env["ir.model.data"].search(
        [("model", "=", "ir.ui.view"), ("res_id", "=", view.id)], limit=1
    )
    if md and md.module == "studio_customization":
        return True
    return False


def _dump_one(env: Any, xmlid: str, out_dir: Path) -> tuple[str, str]:
    """Resolve + dump a single xmlid. Returns (status, detail)."""
    try:
        view = env.ref(xmlid, raise_if_not_found=False)
    except Exception as exc:  # pragma: no cover — defensive
        return "error", f"env.ref crashed: {exc}"
    if view is None:
        return "skipped", "xmlid not found"
    if view._name != "ir.ui.view":
        return "skipped", f"resolved to {view._name}, not ir.ui.view"
    if _is_studio_origin(view):
        return "skipped_studio", "studio origin — no source equivalent"

    try:
        combined = view._get_combined_arch()
    except Exception as exc:
        return "error", f"_get_combined_arch failed: {exc}"

    from lxml import etree

    # _get_combined_arch returns an lxml element; canonicalize returns str.
    xml_str = etree.tostring(combined, encoding="unicode")
    canonical = etree.canonicalize(xml_str, strip_text=True)
    out_path = out_dir / f"{_escape_xmlid(xmlid)}.xml"
    out_path.write_text(canonical, encoding="utf-8")
    return "ok", str(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--database", required=True,
        help="Odoo DB name to connect (must have the CE addons installed)",
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        help="Path to odoo.conf; reads addons_path + db settings from it",
    )
    group.add_argument(
        "--addons-path",
        help="Comma-separated addons path (use this OR --config, not both)",
    )
    ap.add_argument(
        "--views-json",
        default=str(REPO / "tests" / "accept" / "top50_views.json"),
        help="JSON file with view xmlids to dump",
    )
    ap.add_argument(
        "--out-dir",
        default=str(REPO / "tests" / "fixtures" / "golden" / "resolve_view_live"),
        help="Directory for canonical XML output",
    )
    ap.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (DEBUG/INFO/WARNING)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    views_json = Path(args.views_json)
    if not views_json.is_file():
        print(f"error: views JSON not found: {views_json}", file=sys.stderr)
        return 2
    xmlids = _load_view_list(views_json)
    if not xmlids:
        print("error: views JSON has no xmlids", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _logger.info("bootstrapping Odoo (database=%s)...", args.database)
    env = _bootstrap_odoo(args.config, args.addons_path, args.database)

    counts: dict[str, int] = {"ok": 0, "skipped": 0, "skipped_studio": 0, "error": 0}
    try:
        for idx, xmlid in enumerate(xmlids, 1):
            status, detail = _dump_one(env, xmlid, out_dir)
            counts[status] = counts.get(status, 0) + 1
            if status == "ok":
                _logger.info("[%d/%d] %s -> %s", idx, len(xmlids), xmlid, detail)
            else:
                _logger.warning("[%d/%d] %s %s: %s", idx, len(xmlids), status, xmlid, detail)
    finally:
        env.cr.close()

    _logger.info(
        "done: ok=%d skipped=%d studio=%d error=%d",
        counts["ok"],
        counts["skipped"],
        counts["skipped_studio"],
        counts["error"],
    )
    # Non-zero iff every view failed — partial runs are still useful.
    return 0 if counts["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
