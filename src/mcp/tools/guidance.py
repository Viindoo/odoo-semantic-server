"""Guidance-layer MCP tools (split out of src/mcp/tools/discovery.py, A2).

Three guidance tools and their implementation helpers:
  - ``suggest_pattern``      — ANN-rank curated PatternExample chunks by intent.
  - ``check_module_exists``  — module-indexed check + EE-confusion guard.
  - ``find_override_point``  — Method override chain + convention/anti-pattern
                               hints (+ cross-version method diff).

``suggest_pattern`` is ``async def`` (no offload decorator): it embeds the
intent on the event loop (bounded, short timeout) and then offloads the
blocking Neo4j/PG body via ``asyncio.to_thread`` (ADR-0046).
``check_module_exists`` and ``find_override_point`` use ``@offload_neo4j``
(per-query bounded + clean-string-on-timeout; #287).

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The implementation helpers (``_suggest_pattern`` / ``_format_suggest_pattern`` /
``_ee_confusion_live`` / ``_check_module_exists`` / ``_format_check_module_exists``
/ ``_anti_patterns_for_convention`` / ``_fetch_method_for_diff`` /
``_diff_method_across_versions`` / ``_find_override_point`` /
``_format_find_override_point``) live HERE now (moved from tools/discovery.py).
They reach the shared resolver/state hub (``_get_driver`` / ``_get_embedder`` /
``_resolve_version`` / ``_scope`` / ``_scope_pred`` / ``_checkout_pg`` /
``_edition_label`` / ``_cap_query_text`` / ``_embed_query`` / ``EmbedOverloaded``
/ ``logger``) through the module-level ``_srv`` server reference bound at the END
of this file (see the note there) and ``_srv.<name>`` attribute lookups performed
at call time.

Two properties must hold together, which is why ``_srv`` is bound the way it is:

1. The bodies read the hub through ``_srv.<name>`` at CALL time (not by binding
   the names at import time) so that ``monkeypatch.setattr(srv, "_get_driver",
   ...)`` / ``monkeypatch.setattr(srv, "_get_embedder", ...)`` etc. in the tests
   are observed — the patch lands on the live server module object and the
   attribute is re-read off that object on each call.
2. ``_srv`` is bound from ``sys.modules['src.mcp.server']`` at end-of-module, so
   it is the SAME server generation that imported this module and registered
   these tools.  After a ``sys.modules.pop('src.mcp.server')`` + re-import, a
   test that holds a stale ``src.mcp.server`` reference (e.g. the pattern-tool
   fixtures) sees the impls re-exported on the fresh generation, exactly as
   pre-refactor when these bodies were bare-name globals in server.py.

The two guidance-only constants ``_VALID_PATTERN_LANGUAGES`` and
``_ANTI_PATTERNS_BASE`` live HERE in full (§2.7, verified guidance-only — no
cross-module / test importer reads them via server).

server.py re-exports the three public tools plus the impl symbols that tests
import via ``src.mcp.server`` (see the re-export block at the end of server.py).
The pure helpers (``format_next_step`` / the ``src.constants`` values) carry no
state and are imported directly — NOT through ``_srv``.
"""

import asyncio
import sys
from contextlib import nullcontext

from src.constants import (
    GLOBAL_PROFILE,
    SNIPPET_PREVIEW_MAX_LINES,
)
from src.mcp.hints import format_next_step
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)

# Guidance-only module-level constants (§2.7 — verified used only by the tools
# in this module; no cross-module / test importer reads them via server).
_VALID_PATTERN_LANGUAGES = ("python", "xml", "js", "all")
_ANTI_PATTERNS_BASE = [
    "Old-style super(ClassName, self) — use plain super() in Python 3",
    "Missing return after super() — caller gets None, breaks chain",
]


def _suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
    _query_vec=None,
) -> str:
    """ANN-rank curated PatternExample chunks by intent string.

    Per ADR-0003: pgvector ANN over embeddings (chunk_type='pattern_example') →
    Neo4j batch fetch metadata via UNWIND on pattern_id list. Language filter
    via entity_name slug LIKE '<language>__%'.
    """
    if not intent.strip():
        return (
            "suggest_pattern: intent is required (empty input).\n"
            "Hint: pass a natural-language description, e.g. "
            "'computed field cross-model partner'."
        )
    if language not in _VALID_PATTERN_LANGUAGES:
        valid = ", ".join(_VALID_PATTERN_LANGUAGES)
        return (
            f"suggest_pattern: invalid language={language!r}. Valid: {valid}."
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _srv._get_driver()
    try:
        embedder = _embedder or _srv._get_embedder()
    except Exception:
        _srv.logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )

    # #287 (review): _resolve_version can hit Tier-3 _latest_version(), itself a
    # bounded Neo4j read that may raise OrmQueryTimeout. This async body has no
    # @offload_neo4j backstop, so the resolution must be caught inline too (not
    # only the PatternExample fetch below) — else a resolve timeout escapes as a
    # protocol error (the exact ADR-0023 hole #287 closes).
    from src.mcp.orm import OrmQueryTimeout
    try:
        with driver.session() as session:
            v = _srv._resolve_version(odoo_version, session)
    except OrmQueryTimeout as exc:
        return _srv._nonorm_timeout_response(exc, "suggest_pattern")

    if _query_vec is not None:
        intent_vec = _query_vec
    else:
        try:
            # Cap intent to the token budget before INSTRUCT (#227, sync path).
            capped = _srv._cap_query_text(embedder, intent)
            instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
            intent_vec = embedder.embed([instruct + capped])[0]
        except Exception:
            _srv.logger.warning("suggest_pattern: embedding query failed", exc_info=True)
            return (
                "suggest_pattern: embedding query failed — try again shortly, "
                "or verify the embedder service is reachable."
            )

    # Use injected connection (test path) or check out from pool (production).
    # RLS note (WI-7 / ADR-0034 D3/A2 / FUFU-2): pattern catalogue chunks carry
    # the explicit profile_name = '__global__' sentinel (m13_021). The SELECT
    # filters on it directly so this read is immune to GUC state; the
    # embeddings_tenant RLS policy passes the sentinel unconditionally via the
    # "profile_name = '__global__'" branch — no GUC wiring needed here.
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _srv._checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            if language == "all":
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND profile_name = %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, intent_vec, limit],
                )
            else:
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND profile_name = %s
                         AND entity_name LIKE %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, f"{language}__%", intent_vec, limit],
                )
            ranked = cur.fetchall()

    if not ranked:
        next_line = format_next_step([
            f"find_examples(query='{intent}', odoo_version='{v}')"
            " for real-world variants",
        ])
        return (
            f"suggest_pattern({intent!r}, {v!r}, language={language})\n"
            "├─ No curated patterns available for this query. "
            "The pattern catalogue may not be populated for this version/profile.\n"
            + next_line
        )

    # Decode pattern_id from entity_name slug (<language>__<id>)
    pattern_ids = []
    score_map: dict[str, float] = {}
    for entity_name, _file, cosine in ranked:
        if "__" in entity_name:
            _lang, pid = entity_name.split("__", 1)
        else:
            pid = entity_name
        pattern_ids.append(pid)
        score_map[pid] = float(cosine)

    # #287: bound the PatternExample batch fetch under the per-query Neo4j
    # timeout. suggest_pattern is async (embeds on the event loop, then offloads
    # this body via asyncio.to_thread) so it has NO @offload_neo4j backstop — the
    # OrmQueryTimeout must be caught INLINE here, emit the metric once, and return
    # the clean string (ADR-0023 raw-text contract).
    try:
        with driver.session() as session:
            records = _srv._data_bounded(
                session,
                """
                UNWIND $ids AS pid
                MATCH (p:PatternExample {pattern_id: pid})
                RETURN p.pattern_id AS id, p.intent_keywords AS kw,
                       p.file_ref AS fr, p.snippet_text AS sn,
                       p.gotchas AS g, p.language AS lang,
                       p.odoo_version_min AS vmin
                """,
                label=f"pattern metadata batch (Odoo {v})",
                ids=pattern_ids,
            )
    except OrmQueryTimeout as exc:
        return _srv._nonorm_timeout_response(exc, "suggest_pattern")

    by_id = {r["id"]: r for r in records}
    return _format_suggest_pattern(
        ordered_ids=pattern_ids, by_id=by_id, score_map=score_map,
        intent=intent, version=v, language=language,
    )


def _format_suggest_pattern(
    *, ordered_ids: list[str], by_id: dict[str, dict],
    score_map: dict[str, float], intent: str, version: str, language: str,
) -> str:
    lines = [
        f"suggest_pattern({intent!r}, {version}, language={language}) "
        f"— {len(ordered_ids)} matches",
    ]
    # Wave 5: all pattern branches become ├─ so the Next: footer is the
    # final └─ (ADR-0023 §4).
    for i, pid in enumerate(ordered_ids):
        rec = by_id.get(pid)
        if not rec:
            continue
        connector = "├─"
        score = score_map.get(pid, 0.0)
        lines.append(f"{connector} #{i + 1} · score {score:.2f} · {pid}")
        prefix = "│   "
        lines.append(f"{prefix}├─ Language: {rec['lang']} (min v{rec['vmin']})")
        lines.append(f"{prefix}├─ File:     {rec['fr']}")
        snippet_lines = (rec.get("sn") or "").splitlines()
        if snippet_lines:
            lines.append(f"{prefix}├─ Snippet:")
            # Snippet is a non-last child → sublist indent is "│   " (4 chars).
            for sl in snippet_lines[:SNIPPET_PREVIEW_MAX_LINES]:
                lines.append(f"{prefix}│   {sl}")
            if len(snippet_lines) > SNIPPET_PREVIEW_MAX_LINES:
                extra = len(snippet_lines) - SNIPPET_PREVIEW_MAX_LINES
                # G7: add escape-hatch hint to odoo://pattern/{id} resource
                lines.append(
                    f"{prefix}│   ... ({extra} more lines"
                    f" — read full via odoo://{version}/pattern/{pid})"
                )
        gotchas = rec.get("g") or []
        if gotchas:
            lines.append(f"{prefix}└─ Gotchas:")
            # Gotchas is the last child → sublist indent is "    " (4 spaces).
            for g in gotchas:
                lines.append(f"{prefix}    • {g}")
    lines.append(format_next_step([
        f"find_examples(query='{intent}', odoo_version='{version}')"
        " for real-world variants",
    ]))
    return "\n".join(lines)


def _ee_confusion_live() -> dict[str, str | None]:
    """Build EE confusion map from live DB (cached 60 s by get_ee_modules).

    Falls back to static list when DB is unreachable — same as get_ee_modules().
    Called on every _check_module_exists invocation so admin CRUD changes
    propagate within one 60 s cache window (WI-R F-007 fix).
    """
    from src.data.ee_modules import get_ee_modules
    return {m["name"]: m["vt_equivalent"] for m in get_ee_modules()}


def _check_module_exists(
    name: str, odoo_version: str = "auto", *,
    profile_name: str | None = None,
    _driver=None,
) -> str:
    """Report whether `name` is indexed + flag EE-confusion (per ADR-0003 §2).

    Edition-first strategy: query Neo4j for indexed edition (OEEL-1 detected),
    fallback to DB-backed guard list if not indexed.  Both paths produce the same
    EE warning.  Guard list is read via get_ee_modules() (60 s cache) so that
    admin CRUD changes take effect within one cache window (WI-R F-007).
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)
        rec = _srv._single_bounded(
            session,
            """
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN m.edition AS edition,
                   m.license AS license,
                   m.viindoo_equivalent_qname AS vvq,
                   m.repo AS repo
            """,
            label=f"module {name!r} existence (Odoo {v})",
            n=name, v=v, **_srv._scope(profile_name),
        )

    indexed = rec is not None
    edition = rec["edition"] if rec else None
    license_val = rec["license"] if rec else None
    repo = rec.get("repo") if rec else None
    vvq_db = rec.get("vvq") if rec else None

    # Build live EE confusion map from DB (cached 60 s).  Falls back to static
    # list when DB is unreachable — transparent to callers (WI-R F-007 fix).
    confusion = _ee_confusion_live()

    # Edition-first: check Neo4j for 'enterprise' (from OEEL-1 detection at index time).
    # OPL-1 is NOT mapped to 'enterprise' by _detect_module_edition (it falls to 'custom'),
    # so it never trips the EE-confusion gate below — the indexed `edition` enum is the
    # sole signal (ADR-0036; #263 regression fix removed the prior license-based check).
    is_ee_confusion = False
    ee_source = ""  # track source for output messaging
    viindoo_equivalent = None

    # Gate EE-confusion on the indexed `edition` enum only (ADR-0036). OEEL-1
    # (Odoo S.A.'s OWN Enterprise license) is detected as edition="enterprise" at
    # index time, so the enum check covers it. OPL-1 is the Odoo Proprietary License
    # for third-party/proprietary apps (edition="viindoo"/"custom") and must NOT be
    # flagged as Odoo Enterprise — doing so mislabeled Viindoo OPL-1 addons such as
    # to_base / viin_hr (#263, regression from PR #165).
    if indexed and edition == "enterprise":
        is_ee_confusion = True
        ee_source = "indexed"
        viindoo_equivalent = vvq_db or confusion.get(name)
    elif name in confusion:
        # Not indexed (or not marked 'enterprise') but in guard list
        is_ee_confusion = True
        ee_source = "dict"
        viindoo_equivalent = confusion.get(name)

    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition,
        license_val=license_val, repo=repo,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
        ee_source=ee_source,
    )


def _format_check_module_exists(
    *, name: str, version: str, indexed: bool, edition: str | None,
    license_val: str | None = None,
    repo: str | None, is_ee_confusion: bool, viindoo_equivalent: str | None,
    ee_source: str = "",
) -> str:
    lines = [f"check_module_exists({name!r}, {version})"]
    lines.append(f"├─ Indexed:         {'Yes' if indexed else 'No'}")
    if indexed and edition:
        repo_suffix = f" [{repo}]" if repo else ""
        # WG-5 T1: derive human-readable edition label from license (preferred)
        # or from indexed edition enum.
        ed_label = _srv._edition_label(edition, license_val)
        lines.append(f"├─ Edition:         {ed_label}{repo_suffix}")
    lines.append(
        f"├─ Is EE confusion: {'Yes' if is_ee_confusion else 'No'}"
    )
    if is_ee_confusion:
        if viindoo_equivalent:
            lines.append(f"├─ Viindoo equiv:   {viindoo_equivalent}")
        else:
            lines.append("├─ Viindoo equiv:   (none — feature not in Viindoo stack)")
        # Differentiate source for debugging
        source_hint = ""
        if ee_source == "indexed":
            source_hint = f" (license={license_val})" if license_val else ""
        elif ee_source == "dict":
            source_hint = " (legacy hardcoded dict)"
        # ADR-0023 §2: English-only tool output.
        lines.append(
            f"├─ ⚠ WARNING: this is an Odoo Enterprise module{source_hint}. "
            "Do NOT depend on it in a Viindoo Community stack — "
            "this violates the GPL/Enterprise license boundary."
        )
    elif not indexed:
        # ADR-0023 §4.4: terminal branch — module genuinely not found, no
        # operator-shell hint (agents cannot execute shell commands).
        lines.append(
            "└─ Not indexed in this profile. "
            "Verify the module name, or call list_available_profiles to see indexed scope."
        )
        return "\n".join(lines)
    # Wave 5: YES branch emits Next: footer (ADR-0023 §4).
    lines.append(format_next_step([
        f"describe_module(name='{name}', odoo_version='{version}')"
        " for full overview",
    ]))
    return "\n".join(lines)


def _anti_patterns_for_convention(kind: str) -> list[str]:
    """Return convention-specific anti-pattern hints for find_override_point."""
    if kind == "compute":
        return [
            "Calling super() in compute method — Odoo rebinds via @api.depends, "
            "super-chain semantically meaningless",
            "Forgetting @api.depends — silent stale data on field reads",
        ]
    if kind in ("inverse", "search", "default"):
        return [
            f"Calling super() in {kind} method — Odoo rebinds via decorator, "
            "super-chain has no effect",
        ]
    if kind == "action":
        return list(_ANTI_PATTERNS_BASE) + [
            "Returning bool/None instead of action_window dict — UI can't refresh",
        ]
    if kind == "crud":
        return list(_ANTI_PATTERNS_BASE) + [
            "Missing @api.model_create_multi on create() override — slow batch import",
            "Treating vals as single dict instead of vals_list — silent data loss",
        ]
    return list(_ANTI_PATTERNS_BASE)


def _fetch_method_for_diff(session, model: str, method: str, version: str) -> dict | None:
    """Fetch a single Method node's properties for cross-version diff.

    Returns a dict with keys: decorators, convention_kind, super_safety,
    has_super_call, signature. Returns None when no Method found.
    Aggregates across all modules (decorators union, super_call OR).
    """
    rows = _srv._data_bounded(
        session,
        f"""
        MATCH (mth:Method {{name: $method, model: $model, odoo_version: $v}})
        WHERE {_srv._scope_pred("mth")}
        RETURN mth.decorators AS decorators,
               mth.convention_kind AS ck,
               mth.super_safety AS ss,
               coalesce(mth.has_super_call, false) AS has_super,
               mth.signature AS signature
        ORDER BY mth.module
        """,
        label=f"method {model}.{method} for diff (Odoo {version})",
        method=method, model=model, v=version, **_srv._scope(),
    )
    if not rows:
        return None
    # Merge across override chain: union decorators, OR has_super, first non-null sig
    all_decs: list[str] = []
    seen_decs: set[str] = set()
    has_super = False
    sig: str | None = None
    ck = rows[0]["ck"] or "private"
    ss = rows[0]["ss"] or "usually"
    for r in rows:
        for d in (r["decorators"] or []):
            if d not in seen_decs:
                seen_decs.add(d)
                all_decs.append(d)
        if r["has_super"]:
            has_super = True
        if sig is None and r["signature"] is not None:
            sig = r["signature"]
    return {
        "decorators": all_decs,
        "convention_kind": ck,
        "super_safety": ss,
        "has_super_call": has_super,
        "signature": sig,
    }


def _diff_method_across_versions(
    model: str, method: str, from_version: str, to_version: str,
    *, _driver=None,
) -> str:
    """Diff a method between two Odoo versions.

    Compares decorator set, convention_kind, super_safety, and signature
    between from_version and to_version. Returns tree-formatted string.
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        from_data = _fetch_method_for_diff(session, model, method, from_version)
        to_data = _fetch_method_for_diff(session, model, method, to_version)

    header = f"Method version diff ({model}.{method}: {from_version} → {to_version})"
    lines = [header]

    # Presence
    if from_data and to_data:
        presence_label = "both versions present"
    elif from_data and not to_data:
        presence_label = f"deleted in {to_version} (not found)"
    elif not from_data and not to_data:
        presence_label = (
            f"absent in both {from_version} and {to_version}"
            " (model/method may not be indexed)"
        )
        lines.append(f"├─ Status:           {presence_label}")
        lines.append(format_next_step([
            f"model_inspect(model='{model}', method='methods', odoo_version='{to_version}')"
            " to verify the method name",
        ]))
        return "\n".join(lines)
    else:
        presence_label = f"added in {to_version} (not in {from_version})"
    lines.append(f"├─ Status:           {presence_label}")

    # Decorator diff
    from_decs = set(from_data["decorators"]) if from_data else set()
    to_decs = set(to_data["decorators"]) if to_data else set()
    removed = sorted(from_decs - to_decs)
    added = sorted(to_decs - from_decs)
    if removed or added:
        lines.append("├─ Decorator changes:")
        items = [f"Removed in {to_version}: {d}" for d in removed]
        items += [f"Added in {to_version}:   {d}" for d in added]
        last_idx = len(items) - 1
        for i, text in enumerate(items):
            connector = "└─" if i == last_idx else "├─"
            lines.append(f"│   {connector} {text}")
    else:
        lines.append("├─ Decorator changes: none")

    # Convention diff
    from_ck = from_data["convention_kind"] if from_data else "?"
    to_ck = to_data["convention_kind"] if to_data else "?"
    if from_ck != to_ck:
        lines.append(f"├─ Convention:        changed ({from_ck} → {to_ck})")
    else:
        lines.append(f"├─ Convention:        unchanged ({from_ck})")

    # Signature diff
    _NULL_HINT = "(signature not available for this version)"
    from_sig = from_data["signature"] if from_data else None
    to_sig = to_data["signature"] if to_data else None
    from_sig_str = from_sig if from_sig is not None else _NULL_HINT
    to_sig_str = to_sig if to_sig is not None else _NULL_HINT
    if from_sig is None or to_sig is None:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig_str}"
            f" → {to_version}={to_sig_str}"
        )
    elif from_sig != to_sig:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig}"
            f" → {to_version}={to_sig}"
        )
    else:
        lines.append(f"├─ Signature:         unchanged ({from_sig})")

    # Super safety
    from_ss = from_data["super_safety"] if from_data else "?"
    to_ss = to_data["super_safety"] if to_data else "?"
    if from_ss != to_ss:
        lines.append(f"├─ Super safety:      changed ({from_ss} → {to_ss})")
    else:
        lines.append(f"├─ Super safety:      unchanged ({from_ss})")

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
        f", odoo_version='{to_version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{to_version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


def _find_override_point(
    model: str, method: str, odoo_version: str = "auto",
    *, to_version: str = "", _driver=None,
) -> str:
    """Inspect Method override chain + surface convention hints + anti-patterns.

    When to_version is non-empty and differs from odoo_version, performs a
    cross-version diff instead of single-version inspection.
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # Cross-version diff mode
    if to_version and to_version != v:
        return _diff_method_across_versions(
            model, method, from_version=v, to_version=to_version, _driver=driver,
        )

    # Single-version mode (existing behaviour)
    # ADR-0034 WI-4 (R-09 fix): apply tenant boundary filter even though
    # find_override_point has no profile_name param — use None so admin is
    # unrestricted and tenant boundary is still enforced via _srv._effective_allowed.
    with driver.session() as session:
        records = _srv._data_bounded(
            session,
            """
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            WHERE ($own IS NULL OR (size(mth.profile) > 0
                   AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super,
                   coalesce(mod.repo_url, mod.repo) AS repo, mod.edition AS edition
            ORDER BY mth.module
            """,
            label=f"override chain {model}.{method} (Odoo {v})",
            method=method, model=model, v=v, **_srv._scope(None),
        )

    if not records:
        next_line = format_next_step([
            f"model_inspect(model='{model}', method='methods', odoo_version='{v}')"
            " to find the actual method name",
        ])
        return (
            f"find_override_point({model!r}, {method!r}, {v})\n"
            f"├─ method not found on model {model!r} in Odoo {v}\n"
            + next_line
        )

    convention_kind = records[0]["ck"] or "private"
    super_safety = records[0]["ss"] or "usually"
    return_required = bool(records[0]["rr"])
    super_count = sum(1 for r in records if r["has_super"])
    super_ratio = f"{super_count}/{len(records)}"
    anti_patterns = _anti_patterns_for_convention(convention_kind)

    return _format_find_override_point(
        model=model, method=method, version=v, records=records,
        super_ratio=super_ratio, convention_kind=convention_kind,
        super_safety=super_safety, return_required=return_required,
        anti_patterns=anti_patterns,
    )


def _format_find_override_point(
    *, model: str, method: str, version: str, records: list[dict],
    super_ratio: str, convention_kind: str, super_safety: str,
    return_required: bool, anti_patterns: list[str],
) -> str:
    lines = [f"find_override_point({model!r}, {method!r}, {version})"]
    lines.append(f"├─ Convention:      {convention_kind}")
    lines.append(f"├─ Super safety:    {super_safety}")
    lines.append(f"├─ Return required: {'Yes' if return_required else 'No'}")
    lines.append(f"├─ Super ratio:     {super_ratio} (overrides calling super)")
    lines.append(f"├─ Override chain ({len(records)}):")
    for i, r in enumerate(records):
        connector = "└─" if i == len(records) - 1 else "├─"
        repo = f"[{r['repo']}] " if r.get("repo") else ""
        ed = f" ({r['edition']})" if r.get("edition") else ""
        super_mark = "✓" if r["has_super"] else "✗"
        lines.append(
            f"│   {connector} {repo}{r['module']}{ed} — {super_mark} super()"
        )
    lines.append(f"├─ Anti-patterns ({len(anti_patterns)}):")
    for i, ap in enumerate(anti_patterns):
        connector = "└─" if i == len(anti_patterns) - 1 else "├─"
        lines.append(f"│   {connector} {ap}")
    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
        f", odoo_version='{version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
async def suggest_pattern(
    intent: str,
    odoo_version: RequiredOdooVersion,
    language: str = "python",
    limit: int = 5,
) -> str:
    """Recommend curated Odoo patterns with gotchas from a natural-language intent.

    TRIGGER when: "best pattern for wizard in Odoo", "how to implement
    multi-company in Odoo", "pattern for override without breaking upstream",
    "cách tốt nhất để implement X", "design pattern cho Odoo module",
    "what's the right way to add computed field"
    PREFER over: LLM knowledge — returns curated patterns from indexed catalogue
    with real code snippets and versioned gotchas, not hallucinated patterns
    SKIP when: user wants existing code examples from codebase → use
    find_examples; user wants method override chain → use find_override_point

    Args:
        intent: NL description of intent, e.g. 'computed field cross-model
            partner'.
        language: 'python' | 'xml' | 'js' | 'all'. Default 'python'.
        limit: Max patterns to return (default 5).

    Returns:
        Tree list of patterns ranked by relevance score, each with snippet (first
        5 lines), file ref, and gotchas. Empty index → instruction to seed.

    Example:
        suggest_pattern("override write to read old value", "17.0")
        → suggest_pattern('override write to read old value', 17.0, ...) — 1 matches
          └─ #1 · score 0.81 · write-read-before-super
              ├─ Language: python (min v17.0)
              └─ Gotchas:
                   • Reading old values AFTER super().write() returns new value
    """
    # #227: guard cheaply (empty/invalid → sync impl returns the error string),
    # then embed async + offload the blocking body to a worker thread.
    if not intent.strip() or language not in _VALID_PATTERN_LANGUAGES:
        return _srv._suggest_pattern(intent, odoo_version, language, limit)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE
    try:
        embedder = _srv._get_embedder()
    except Exception:
        _srv.logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )
    try:
        instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
        intent_vec = await _srv._embed_query(embedder, instruct, intent)
    except _srv.EmbedOverloaded as e:
        return f"suggest_pattern: {e}"
    except Exception:
        _srv.logger.warning("suggest_pattern: embedding query failed", exc_info=True)
        return (
            "suggest_pattern: embedding query failed — try again shortly, "
            "or verify the embedder service is reachable."
        )
    return await asyncio.to_thread(
        _srv._suggest_pattern,
        intent, odoo_version, language, limit,
        _embedder=embedder, _query_vec=intent_vec,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def check_module_exists(
    name: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Verify if a module is indexed and flag EE-confusion for Viindoo stack.

    TRIGGER when: "does module sale_management exist in Odoo 17", "is
    viin_sale available", "check if feature X is in standard Odoo", "module X
    có trong OCA không", "Odoo 17 có tính năng X chưa", "is helpdesk an EE
    module"
    PREFER over: searching manually — instant cross-version, cross-repo module
    existence check with Enterprise edition detection and Viindoo equivalent
    SKIP when: caller needs the module's contents (models, views, JS) — use
    describe_module instead, which returns a full architecture overview in
    one round-trip. user wants module field/method details → use model_inspect;
    user wants code examples from a module → use find_examples

    Args:
        name: Module technical name (e.g. 'sale', 'helpdesk', 'viin_helpdesk').
        profile_name: Optional inheritance-resolved profile filter. When set,
            narrows the check to modules visible in this profile (including
            parent profiles via the ancestor chain). Default None checks all.

    Returns:
        Tree text: Indexed yes/no, edition, EE-confusion flag, Viindoo
        equivalent (if any), and WARNING when name is an EE-only module.

    Example:
        check_module_exists('helpdesk', '17.0')
        → check_module_exists('helpdesk', 17.0)
          ├─ Indexed:         No
          ├─ Is EE confusion: Yes
          ├─ Viindoo equiv:   viin_helpdesk
          └─ ⚠ WARNING: this is an Odoo Enterprise module (legacy hardcoded dict).
    """
    return _srv._check_module_exists(name, odoo_version, profile_name=profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def find_override_point(
    model: str, method: str, odoo_version: RequiredOdooVersion, to_version: str = "",
) -> str:
    """Show override chain + super-call convention + anti-patterns for a method.

    TRIGGER when: "where should I override action_confirm in sale.order", "best
    override point for partner creation", "how to extend method X without
    breaking OCA", "override field X ở đâu là đúng", "điểm override phù hợp
    cho method Y", "is super() required for write override"
    PREFER over: model_inspect(method='method') — adds super() safety guidance
    and anti-patterns, not just the chain listing
    SKIP when: full override chain only → model_inspect(method='method');
    design pattern guidance → suggest_pattern

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        method: Method name (e.g. 'action_confirm', '_compute_amount').
        odoo_version: From-version when in diff mode (see field schema for
            the required-version contract).
        to_version: Optional. When set, activates cross-version diff mode
            (e.g. '18.0' to diff 17.0 → 18.0). Default '' = single-version.

    Returns:
        Single-version: convention_kind, super_safety, return_required,
        super_ratio, override chain, and anti-patterns.
        Cross-version diff: presence, decorator changes, signature diff,
        convention and super safety change.

    Example:
        find_override_point('sale.order', 'action_confirm', '17.0')
        → find_override_point('sale.order', 'action_confirm', 17.0)
          ├─ Convention:      action
          ├─ Super safety:    always
          ├─ Return required: Yes
          ├─ Super ratio:     7/7
          └─ Anti-patterns (3): ...
    """
    return _srv._find_override_point(model, method, odoo_version, to_version=to_version)


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. That restores the
# pre-refactor bare-name behaviour: the impl bodies read the hub through
# `_srv.<name>` at call time so monkeypatch.setattr(srv, "_get_driver", ...),
# monkeypatch.setattr(srv, "_get_embedder", ...) and friends still take
# effect, and the pop + re-import fixtures see the impls re-exported on the fresh
# generation.
_srv = sys.modules["src.mcp.server"]
