# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_assets.py — Version-aware asset-bundle parser (WI-D).
#
# REFERENCE IMPLEMENTATION of the per-feature version-dispatch convention
# (ADR-0052): this feature owns its OWN VersionRegistry with its OWN boundaries,
# fans out to per-era handlers, and is exposed via ONE aggregate dispatcher
# (`parse_assets`). Callers (pipeline_repo) call only `parse_assets`; they never
# see the registry or the eras.
#
# The two Odoo asset eras (survey: /tmp/osm-assets-survey-eraA.md, eraBC.md):
#   - Era A (v8-v14): asset bundles are XML `<template id="web.assets_backend">`
#     elements (DEFINITION) and `<template inherit_id="web.assets_backend">`
#     elements (EXTENSION). These are ALREADY captured by parser_qweb as QWebTmpl
#     base nodes + QWebInfo extenders, so the era-A handler emits NO separate
#     AssetBundle contributions — it would only duplicate what parser_qweb writes.
#   - Era B (v15-v19): asset bundles live in `__manifest__.py` under the 'assets'
#     dict, `{bundle_name: [entry, ...]}`. The era-B handler parses that dict into
#     AssetBundleContribution objects. Grammar is uniform v15-v19 (no per-minor
#     branching) — only the bundle-name catalogue differs, which is data, not code.
#
# A new AssetBundle graph node + CONTRIBUTES_TO / INCLUDES_BUNDLE / EXTENDS_ASSET_BUNDLE
# edges are written by writer_neo4j_ui from the AssetParseResult this module produces.
from __future__ import annotations

from .models import AssetBundleContribution, AssetParseResult, ModuleInfo
from .registry import parse_manifest
from .version_registry import VersionRegistry

# --- Per-feature version-dispatch registry (ADR-0052) -----------------------
# Era A: XML <template> bundles (v8-v14) — handled by parser_qweb, so the era-A
#        handler is a no-op here (returns no contributions).
# Era B: __manifest__.py 'assets' dict (v15+, open-ended).
# First-match-wins; v20 is a one-line append (e.g. keep era-B open-ended via None).
# The handler value is a string TAG (not the function object): keeping it a tag
# lets the registry sit above the handler defs without a forward-reference dance;
# `_HANDLERS` (below) maps tag -> function. This is intentional, mirroring the
# string-prefix handlers in _PREFIX_REGISTRY (ADR-0032).
_ASSETS_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8, 14, "era_a"),   # XML <template> bundles — parser_qweb owns these
    (15, None, "era_b"),  # manifest 'assets' dict
])


# --- Era-B grammar: the 6 manifest tuple operations -------------------------
# (survey eraBC §1.3) — uniform across v15-v19. 2-tuple: (op, arg); 3-tuple:
# (op, ref, new). 'include' is the only op that references ANOTHER bundle.
_OP_2ARY = frozenset({"include", "remove", "prepend"})
_OP_3ARY = frozenset({"replace", "before", "after"})
_ALL_OPS = _OP_2ARY | _OP_3ARY


def _normalize_path(p: str) -> str:
    """Strip a leading '/' so absolute and module-relative forms store identically.

    Survey eraBC §1.1: a leading '/' means "Odoo-root-relative"; both forms
    resolve to the same file. Normalize to the slash-less form for stable storage.
    """
    return p.lstrip("/") if isinstance(p, str) else p


def _normalize_entry(entry) -> tuple[list | str | None, str | None]:
    """Normalize one manifest assets entry to a JSON-serializable form.

    Returns (normalized_entry, include_target):
      - str path/glob          -> (normalized_str, None)
      - (op, arg) / (op, r, n) -> ([op, ...normalized args], include_target_or_None)
    Unknown / malformed entries return (None, None) and are skipped by the caller
    (defensive: real Odoo manifests v15-v19 never produce these, but a stray entry
    must not crash a full re-index).
    """
    if isinstance(entry, str):
        return _normalize_path(entry), None

    # Manifest literals allow tuple OR list for operations.
    if isinstance(entry, (tuple, list)) and entry:
        op = entry[0]
        if not isinstance(op, str) or op not in _ALL_OPS:
            return None, None
        if op in _OP_2ARY and len(entry) == 2:
            arg = entry[1]
            include_target = arg if op == "include" and isinstance(arg, str) else None
            # 'include' arg is a bundle name (keep verbatim); path args normalized.
            norm_arg = arg if op == "include" else _normalize_path(arg)
            return [op, norm_arg], include_target
        if op in _OP_3ARY and len(entry) == 3:
            return [op, _normalize_path(entry[1]), _normalize_path(entry[2])], None

    return None, None


def _parse_era_b(module_info: ModuleInfo, manifest: dict) -> AssetParseResult:
    """Era B (v15+): parse the manifest `'assets'` dict into contributions.

    Only bundles whose entry list yields at least one usable entry are emitted.
    `includes` collects ('include', X) targets so the writer can draw
    INCLUDES_BUNDLE edges (AssetBundle -> AssetBundle).
    """
    result = AssetParseResult(module=module_info)
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        return result

    for bundle_name, raw_entries in assets.items():
        if not isinstance(bundle_name, str) or not isinstance(raw_entries, (list, tuple)):
            continue
        entries: list = []
        includes: list[str] = []
        for raw in raw_entries:
            norm, include_target = _normalize_entry(raw)
            if norm is None:
                continue
            entries.append(norm)
            if include_target:
                includes.append(include_target)
        # Emit even an empty-entry bundle: a bundle DECLARED in the manifest is a
        # real base node that legacy <template inherit_id> extenders must resolve
        # against (the whole point of WI-D). The bundle existing IS the signal.
        result.contributions.append(
            AssetBundleContribution(
                module=module_info.name,
                odoo_version=module_info.odoo_version,
                bundle_name=bundle_name,
                entries=entries,
                includes=includes,
            )
        )
    return result


def _parse_era_a(module_info: ModuleInfo, manifest: dict) -> AssetParseResult:
    """Era A (v8-v14): no-op — XML `<template>` bundles are captured by parser_qweb.

    The legacy bundle DEFINITION (`<template id="web.assets_backend">`) is already
    written as a QWebTmpl base node, and the EXTENSION
    (`<template inherit_id="web.assets_backend">`) as a QWebInfo extender. Emitting
    AssetBundle contributions here would duplicate them, so era A intentionally
    returns an empty result. Kept as an explicit handler (not a None default) so
    the era boundary is documented and a future era-A enrichment has a home.
    """
    return AssetParseResult(module=module_info)


_HANDLERS = {"era_a": _parse_era_a, "era_b": _parse_era_b}


def parse_assets(
    module_info: ModuleInfo, manifest: dict | None = None
) -> AssetParseResult:
    """Aggregate dispatcher (ADR-0052 single call point) for asset-bundle parsing.

    Resolves the era for *module_info.odoo_version* via `_ASSETS_REGISTRY` and
    invokes the matching handler. *manifest* may be passed by the caller (avoids a
    re-read when it already has the dict); when None it is read from
    ``<module path>/__manifest__.py`` (Option 2 in the arch survey — keeps the
    manifest assets dict out of ModuleInfo, which never stored it).

    A version that matches no era (e.g. v7 or unparseable) yields an empty result.
    """
    handler_tag = _ASSETS_REGISTRY.resolve_version(module_info.odoo_version)
    if handler_tag is None:
        return AssetParseResult(module=module_info)
    handler = _HANDLERS[handler_tag]

    # Era A never needs the manifest; only read it for era B to avoid disk I/O on
    # the (large) v8-v14 module set.
    if handler_tag == "era_b" and manifest is None:
        from pathlib import Path
        manifest_path = Path(module_info.path) / "__manifest__.py"
        manifest = parse_manifest(str(manifest_path)) if manifest_path.exists() else {}

    return handler(module_info, manifest or {})
