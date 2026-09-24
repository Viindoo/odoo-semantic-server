# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-side module lifecycle and freshness (ADR-0056) for check_module_exists / describe_module.

Two sources are combined:

* the Neo4j graph, which holds only LIVE modules: the owned-node existence
  predicate, freshness stamps (``last_seen_sha`` / ``last_seen_at`` /
  ``repos``), the reverse ``old_technical_name`` lookup, dependency stubs and
  the explicit per-version presence list;
* the Postgres ``module_presence`` ledger, which also keeps history: why a name
  is not indexed (retired / excluded / pending), the removing commit and the
  recorded successor.

Every read is tenant-scoped. Graph reads go through the ADR-0034 choke
(``_scope`` / ``_scope_pred``); ledger reads always pass an explicit profile
list, never the unscoped ``None`` (admin callers get the list of every existing
profile). A ledger failure degrades to the single line
:data:`LEDGER_UNAVAILABLE` and never turns into a tool error.

The helpers take the MCP server hub (``srv``) as an explicit argument instead
of binding it at import time, so the callers' ``_srv`` generation (and any test
monkeypatch on it) is the one used.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.constants import REL_DEPENDS_ON
from src.db.module_presence import ModulePresenceStore
from src.indexer.lifecycle import (
    BLOCKED_ERROR_PREFIX,
    BLOCKED_GATE_PREFIX,
    BLOCKED_NO_RETIRE,
    BLOCKED_SKIPPED_RECENT,
    BLOCKED_UNDECIDABLE_PREFIX,
    GATE_MANIFEST_UNPARSEABLE,
    GATE_MASS_RETIRE,
    GATE_SCAN_INCOMPLETE,
    GATE_SCAN_UNTRUSTED,
    GATE_TOTAL_WIPE,
)

logger = logging.getLogger(__name__)

LEDGER_UNAVAILABLE = "Lifecycle: unavailable (ledger unreachable)"

# Dependents named inline on the dependency-stub line; the total is always shown.
STUB_DEPENDENT_PREVIEW = 5

_EXCLUSION_TEXT = {
    "installable_false": "installable: False",
    "license_skip": "skipped by the license policy",
    "unparseable": "manifest could not be parsed",
}
_KNOWN_GATES = frozenset({
    GATE_SCAN_INCOMPLETE, GATE_SCAN_UNTRUSTED, GATE_MASS_RETIRE, GATE_TOTAL_WIPE,
    GATE_MANIFEST_UNPARSEABLE,
})
_BLOCKED_CLASS_TEXT = {
    "no_retire": "no_retire (--no-retire run)",
    "undecidable": "undecidable (another repository that may ship it is not synced yet)",
    "error": "error (retirement failed; retried by the next run)",
    "skipped_recent": "skipped_recent (re-written by a concurrent run)",
    "gate": "gate",
}
_SUCCESSOR_SOURCE_TEXT = {
    "git_rename": "git rename",
    "old_technical_name": "manifest old_technical_name",
}

_VERSION_ORDER = (
    "CASE WHEN {v} =~ '[0-9]+[.][0-9]+' THEN toInteger(split({v}, '.')[0]) END, "
    "CASE WHEN {v} =~ '[0-9]+[.][0-9]+' THEN toInteger(split({v}, '.')[1]) END, "
    "{v}"
)


def version_order_by(expr: str) -> str:
    """Cypher ORDER BY terms sorting Odoo version strings numerically, oldest first.

    ``major.minor`` compares as two integers (so ``9.0 < 17.0 < 17.1 < 18.0``);
    anything else sorts after, lexicographically.
    """
    return _VERSION_ORDER.format(v=expr)


def owned_pred(srv, alias: str) -> str:
    """A module node owned by at least one profile AND visible in the caller's scope.

    Dependency stubs (created by a ``depends`` reference, never owned) have no
    profile, so they fail this predicate for every caller, admin included (M7).
    """
    return f"size(coalesce({alias}.profile, [])) > 0 AND {srv._scope_pred(alias)}"


def one_line(text: str | None) -> str | None:
    """Collapse every whitespace run (newlines included) into one space."""
    if text is None:
        return None
    flat = " ".join(str(text).split())
    return flat or None


def short_sha(sha: str | None, length: int = 7) -> str | None:
    return sha[:length] if sha else None


def format_date(value) -> str | None:
    """ISO ``YYYY-MM-DD`` (UTC) of a Neo4j DateTime, a datetime, a date or an ISO string."""
    if value is None:
        return None
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        value = to_native()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10] if text else None


def seen_text(sha: str | None, at) -> str:
    """``HEAD <sha7> on <date>`` with graceful halves when either part is missing."""
    day = format_date(at)
    if sha and day:
        return f"HEAD {short_sha(sha)} on {day}"
    if sha:
        return f"HEAD {short_sha(sha)}"
    if day:
        return f"{day} (branch HEAD not stamped yet)"
    return "not stamped yet (indexed before lifecycle tracking)"


@dataclass(frozen=True)
class ModuleLifecycle:
    """What the index knows about a name that is NOT indexed at one version."""

    name: str
    version: str
    ledger_available: bool
    ledger_rows: tuple[dict, ...] = ()
    predecessor_rows: tuple[dict, ...] = ()
    manifest_successors: tuple[str, ...] = ()
    other_versions: tuple[tuple[str, tuple[str, ...]], ...] = ()
    stub_dependent_count: int = 0
    stub_dependent_names: tuple[str, ...] = ()
    # Admin caller: pending-retirement reasons are rendered in full (they may
    # name other tenants' repos and carry exception text).
    blocker_detail: bool = False

    @property
    def successors(self) -> tuple[str, ...]:
        """Recorded successors (ledger first, then manifest reverse lookup), de-duplicated."""
        seen: dict[str, None] = {}
        for row in self.ledger_rows:
            for s in row.get("successor_names") or ():
                if s and s != self.name:
                    seen.setdefault(s, None)
        for s in self.manifest_successors:
            if s != self.name:
                seen.setdefault(s, None)
        return tuple(seen)


def ledger_allowed_profiles(srv, conn, profile_name: str | None) -> list[str]:
    """The caller's profile list for a ledger read - never ``None``.

    A scoped tenant gets its flat ``own + shared`` list (narrowed by
    *profile_name*); an admin caller gets every existing profile name (narrowed
    the same way). Ledger rows of a deleted profile are therefore visible to
    nobody through the MCP tier.
    """
    allowed = srv._effective_allowed(profile_name)
    if allowed is not None:
        return list(allowed)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM profiles ORDER BY name")
        return [r[0] for r in cur.fetchall()]


def read_ledger_rows(
    srv, name: str, version: str | None, profile_name: str | None,
) -> list[dict] | None:
    """Scoped ledger rows about *name* (``relation`` self/predecessor); None when unreachable."""
    try:
        with srv._checkout_pg() as conn:
            allowed = ledger_allowed_profiles(srv, conn, profile_name)
            # The store only touches its pool when no connection is supplied.
            store = ModulePresenceStore(None)  # type: ignore[arg-type]
            return store.lifecycle_rows(name, version, allowed, conn=conn)
    except Exception:
        logger.warning(
            "module lifecycle ledger read failed for %r @ %s", name, version,
            exc_info=True,
        )
        return None


def _visible_successors(srv, session, rows, version: str, scope) -> list[dict] | None:
    """Ledger rows whose manifest-declared successors are cut to the caller's scope.

    A ``git_rename`` successor comes from the retiring repo's own history; an
    ``old_technical_name`` successor is a module that declares the old name,
    which another tenant's private module may do - such a name is never shown
    unless an owned module of that name is visible in the caller's scope.
    """
    if not rows:
        return rows
    declared = sorted({
        s for r in rows
        if r.get("successor_source") == "old_technical_name"
        for s in (r.get("successor_names") or []) if s
    })
    if not declared:
        return rows
    visible = {
        r["name"] for r in srv._data_bounded(
            session,
            f"""
            MATCH (s:Module {{odoo_version: $v}})
            WHERE s.name IN $names AND {owned_pred(srv, 's')}
            RETURN DISTINCT s.name AS name
            ORDER BY name
            """,
            f"recorded successors visible at Odoo {version}",
            names=declared, v=version, **scope,
        )
        if r.get("name")
    }
    out: list[dict] = []
    for r in rows:
        if r.get("successor_source") == "old_technical_name":
            kept = [s for s in (r.get("successor_names") or []) if s in visible]
            r = {**r, "successor_names": kept}
            if not kept:
                r["successor_source"] = None
        out.append(r)
    return out


def read_module_lifecycle(
    srv, session, name: str, version: str, profile_name: str | None,
) -> ModuleLifecycle:
    """Collect the NOT-indexed facts for *name* at *version* in the caller's scope."""
    scope = srv._scope(profile_name)
    stub = srv._single_bounded(
        session,
        f"""
        MATCH (m:Module {{name: $n, odoo_version: $v}})
        WHERE size(coalesce(m.profile, [])) = 0
        OPTIONAL MATCH (d:Module {{odoo_version: $v}})-[:{REL_DEPENDS_ON}]->(m)
        WHERE {owned_pred(srv, 'd')}
        WITH DISTINCT d
        ORDER BY d.name
        RETURN count(d) AS c, collect(d.name) AS names
        """,
        f"dependency references to '{name}' (Odoo {version})",
        n=name, v=version, **scope,
    )
    successors = srv._data_bounded(
        session,
        f"""
        MATCH (s:Module {{odoo_version: $v}})
        WHERE s.old_technical_name = $n AND s.name <> $n AND {owned_pred(srv, 's')}
        RETURN DISTINCT s.name AS name
        ORDER BY name
        """,
        f"modules renamed from '{name}' (Odoo {version})",
        n=name, v=version, **scope,
    )
    others = srv._data_bounded(
        session,
        f"""
        MATCH (m:Module {{name: $n}})
        WHERE m.odoo_version <> $v AND {owned_pred(srv, 'm')}
        RETURN m.odoo_version AS version,
               coalesce(m.repos, CASE WHEN m.repo IS NULL THEN [] ELSE [m.repo] END)
                   AS repos
        ORDER BY {version_order_by('m.odoo_version')}
        """,
        f"other indexed versions of '{name}'",
        n=name, v=version, **scope,
    )
    rows = _visible_successors(
        srv, session, read_ledger_rows(srv, name, version, profile_name),
        version, scope,
    )
    stub_count = int(stub["c"]) if stub else 0
    stub_names = tuple(n for n in (stub["names"] if stub else []) if n)
    return ModuleLifecycle(
        name=name,
        version=version,
        ledger_available=rows is not None,
        ledger_rows=tuple(r for r in rows or () if r.get("relation") == "self"),
        predecessor_rows=tuple(
            r for r in rows or () if r.get("relation") == "predecessor"
        ),
        manifest_successors=tuple(r["name"] for r in successors if r.get("name")),
        other_versions=tuple(
            (r["version"], tuple(sorted({x for x in (r.get("repos") or []) if x})))
            for r in others
        ),
        stub_dependent_count=stub_count,
        stub_dependent_names=stub_names,
        blocker_detail=srv._get_allowed_profiles() is None,
    )


def blocked_by_text(value: str | None, *, detail: bool) -> str | None:
    """Operator reason a pending retirement waits for, fit for the caller.

    ``retire_blocked_by`` is written for operators: an ``undecidable`` reason
    names the unsynced repos of ANY tenant that may ship the module, an
    ``error`` reason carries raw exception text. A scoped (non-admin) caller
    gets only the reason class (``gate`` with its gate ids, ``no_retire``,
    ``undecidable``, ``error``, ``skipped_recent``) - never a repo label or
    exception text; an admin caller (*detail*) gets the full reason.
    """
    if not value:
        return None
    if detail:
        return one_line(value)
    if value.startswith(BLOCKED_GATE_PREFIX):
        gates = [g for g in value[len(BLOCKED_GATE_PREFIX):].split(",") if g]
        if gates and all(g in _KNOWN_GATES for g in gates):
            return f"gate: {', '.join(gates)}"
        return _BLOCKED_CLASS_TEXT["gate"]
    if value == BLOCKED_NO_RETIRE:
        return _BLOCKED_CLASS_TEXT["no_retire"]
    if value == BLOCKED_SKIPPED_RECENT:
        return _BLOCKED_CLASS_TEXT["skipped_recent"]
    if value.startswith(BLOCKED_UNDECIDABLE_PREFIX):
        return _BLOCKED_CLASS_TEXT["undecidable"]
    if value.startswith(BLOCKED_ERROR_PREFIX):
        return _BLOCKED_CLASS_TEXT["error"]
    return "blocked"


def _state_text(row: Mapping) -> str:
    branch = row.get("repo_branch") or row.get("odoo_version")
    state = row.get("state")
    if state == "excluded":
        reason = _EXCLUSION_TEXT.get(
            row.get("exclusion_reason"), row.get("exclusion_reason") or "excluded",
        )
        return (
            f"on branch {branch} at {seen_text(row.get('last_seen_sha'), row.get('last_seen_at'))}"
            f" but not indexed: {reason}"
        )
    if state == "retired":
        reason = row.get("retire_reason")
        if reason == "repo_removed":
            text = "retired - the repository was removed from the profile"
        elif reason == "orphan_sweep":
            text = "retired - no repository ships it any more (orphan sweep)"
        else:
            text = f"retired - removed from branch {branch}"
        sha = short_sha(row.get("state_changed_sha"))
        day = format_date(row.get("state_changed_at"))
        if sha and day:
            text += f" (recorded at HEAD {sha} on {day})"
        elif sha:
            text += f" (recorded at HEAD {sha})"
        elif day:
            text += f" (recorded on {day})"
        return text
    return (
        f"present on branch {branch} at "
        f"{seen_text(row.get('last_seen_sha'), row.get('last_seen_at'))},"
        " but no module node is visible in this scope"
    )


def _ledger_row_lines(row: Mapping, *, blocker_detail: bool = False) -> list[str]:
    header = (
        f"├─ Lifecycle ({row.get('repo_basename')}, profile {row.get('profile_name')},"
        f" branch {row.get('repo_branch') or row.get('odoo_version')}):"
    )
    children = [f"State: {_state_text(row)}"]
    if row.get("retire_pending"):
        pending = f"Retirement pending: {row.get('retire_pending_reason') or 'absent'}"
        blocked = blocked_by_text(row.get("retire_blocked_by"), detail=blocker_detail)
        if blocked:
            pending += f" (blocked by {blocked})"
        children.append(pending)
    if row.get("state") == "retired":
        children.append(
            f"Last seen: {seen_text(row.get('last_seen_sha'), row.get('last_seen_at'))}"
        )
    if row.get("removing_commit_sha") or row.get("removing_commit_subject"):
        parts = [p for p in (
            short_sha(row.get("removing_commit_sha"), 10),
            format_date(row.get("removing_commit_date")),
        ) if p]
        subject = one_line(row.get("removing_commit_subject"))
        if subject:
            parts.append(f'"{subject}"')
        children.append("Removing commit: " + " ".join(parts))
    successors = [s for s in (row.get("successor_names") or []) if s]
    if successors:
        source = _SUCCESSOR_SOURCE_TEXT.get(
            row.get("successor_source"), row.get("successor_source") or "recorded",
        )
        children.append(f"Renamed to: {', '.join(successors)} ({source})")
    if row.get("version_mismatch") and row.get("version_raw"):
        children.append(
            f"Manifest version: {row['version_raw']} (does not match the branch version)"
        )
    lines = [header]
    last = len(children) - 1
    for i, child in enumerate(children):
        lines.append(f"│   {'└─' if i == last else '├─'} {child}")
    return lines


def lifecycle_lines(lc: ModuleLifecycle) -> list[str]:
    """Tree lines (all ``├─``-level, never the terminal line) for a NOT-indexed name.

    Order: dependency stub, ledger block(s) or the unavailable line, renamed
    from, renamed to (manifest reverse lookup), present at other versions.
    """
    lines: list[str] = []
    if lc.stub_dependent_count:
        preview = ", ".join(lc.stub_dependent_names[:STUB_DEPENDENT_PREVIEW])
        more = lc.stub_dependent_count - min(
            len(lc.stub_dependent_names), STUB_DEPENDENT_PREVIEW,
        )
        if more > 0:
            preview += f", ... and {more} more"
        lines.append(
            f"├─ Dependency stub: listed in 'depends' of {lc.stub_dependent_count}"
            f" indexed module(s) at {lc.version} ({preview}), not itself indexed"
        )
    if not lc.ledger_available:
        lines.append(f"├─ {LEDGER_UNAVAILABLE}")
    else:
        for row in lc.ledger_rows:
            lines.extend(_ledger_row_lines(row, blocker_detail=lc.blocker_detail))
        if lc.predecessor_rows:
            olds = ", ".join(
                f"{r.get('name')} ({r.get('repo_basename')}, {r.get('state')})"
                for r in lc.predecessor_rows
            )
            lines.append(f"├─ Renamed from:    {olds}")
    ledger_named = {
        s for r in lc.ledger_rows for s in (r.get("successor_names") or [])
    }
    manifest_only = [s for s in lc.manifest_successors if s not in ledger_named]
    if manifest_only:
        lines.append(
            f"├─ Renamed to:      {', '.join(manifest_only)}"
            f" (declares old_technical_name '{lc.name}' at {lc.version})"
        )
    if lc.other_versions:
        entries = [
            f"{v} [{', '.join(repos)}]" if repos else v
            for v, repos in lc.other_versions
        ]
        lines.append(f"├─ Present at other versions: {', '.join(entries)}")
    return lines


def owner_freshness_lines(
    *,
    repos: Iterable[str],
    last_seen_sha: str | None,
    last_seen_at,
    ledger_rows: list[dict] | None,
) -> list[str]:
    """YES-branch freshness lines.

    One owning repo: ``├─ Last seen:       HEAD <sha7> on <date> [repo]`` from
    the graph stamp. Several owning repos: a ``Repos`` line plus one ``Last
    seen`` row per repo from that repo's own ledger row (its HEAD, not another
    owner's); without the ledger, the graph stamp is shown as the latest across
    the repos.
    """
    owners = sorted({r for r in repos if r})
    if len(owners) <= 1:
        suffix = f" [{owners[0]}]" if owners else ""
        return [f"├─ Last seen:       {seen_text(last_seen_sha, last_seen_at)}{suffix}"]
    lines = [f"├─ Repos:           {', '.join(owners)}"]
    if ledger_rows is None:
        lines.append(
            f"├─ Last seen:       {seen_text(last_seen_sha, last_seen_at)}"
            f" (latest across repos; per-repo detail unavailable - ledger unreachable)"
        )
        return lines
    by_repo: dict[str, dict] = {}
    for row in ledger_rows:
        if row.get("relation", "self") != "self" or row.get("state") == "retired":
            continue
        by_repo.setdefault(row.get("repo_basename"), row)
    lines.append("├─ Last seen (per repo):")
    last = len(owners) - 1
    for i, repo in enumerate(owners):
        conn = "└─" if i == last else "├─"
        row = by_repo.get(repo)
        if row is None:
            lines.append(f"│   {conn} [{repo}] not tracked in the ledger yet")
            continue
        text = f"[{repo}] {seen_text(row.get('last_seen_sha'), row.get('last_seen_at'))}"
        if row.get("state") == "excluded":
            reason = _EXCLUSION_TEXT.get(row.get("exclusion_reason"), "excluded")
            text += f" (not indexed from this repo: {reason})"
        if row.get("retire_pending"):
            text += " (retirement pending)"
        lines.append(f"│   {conn} {text}")
    return lines
