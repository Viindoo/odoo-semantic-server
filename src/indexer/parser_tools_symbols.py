# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_tools_symbols.py
"""Load curated odoo.tools.* symbol data from static JSON files (per ADR-0033).

Mirrors the _load_static_lint_rules pattern from parser_lint_rules.py:
one JSON file per Odoo version, named tools_symbols_<version>.json.

Public API:
    load_tools_symbols(odoo_version, static_data_dir=None) -> list[CoreSymbolInfo]
"""
import json
from pathlib import Path

from .models import CoreSymbolInfo

_SPEC_DATA_DIR_DEFAULT = Path(__file__).parent / "spec_data"


def _load_static_tools_symbols(
    odoo_version: str,
    static_data_dir: str | Path | None = None,
) -> list[CoreSymbolInfo]:
    """Load curated tool_export CoreSymbolInfo from tools_symbols_<version>.json.

    Returns an empty list when the file is absent or unparseable — callers
    should treat absence as "no curated data for this version" (not an error).

    Args:
        odoo_version:    Odoo version label, e.g. "17.0".
        static_data_dir: Override directory for static spec_data JSON files.
                         Defaults to src/indexer/spec_data/.
    """
    base = Path(static_data_dir) if static_data_dir else _SPEC_DATA_DIR_DEFAULT
    path = base / f"tools_symbols_{odoo_version}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    out: list[CoreSymbolInfo] = []
    for entry in data.get("symbols", []):
        if not isinstance(entry, dict):
            continue
        qname = entry.get("qualified_name", "").strip()
        if not qname:
            continue
        kind = entry.get("kind", "tool_export")
        status = entry.get("status", "stable")
        out.append(CoreSymbolInfo(
            qualified_name=qname,
            kind=kind,
            odoo_version=odoo_version,
            signature=entry.get("signature"),
            file_path=None,
            line=None,
            status=status,
            replacement_qname=entry.get("replacement_qname"),
        ))
    return out


# Public alias with cleaner name for external callers (e.g. pipeline.py).
load_tools_symbols = _load_static_tools_symbols
