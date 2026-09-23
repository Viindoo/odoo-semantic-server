# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/module_presence.py
"""Module lifecycle ledger store - SSOT of module presence per repo (ADR-0056).

One ``module_presence`` row per ``(repo_id, name)`` records whether that repo
currently ships the module (``present``), ships a manifest that is not indexed
(``excluded``: ``installable_false`` / ``license_skip`` / ``unparseable``), or
no longer ships it (``retired``: ``absent`` / ``repo_removed`` /
``orphan_sweep``). Retired rows are history and are never deleted.

Two-phase contract (review amendment C1)
----------------------------------------
Observation and retirement are committed separately:

1. :meth:`ModulePresenceStore.diff` is read-only. It classifies a scan against
   the ledger and writes nothing.
2. :meth:`ModulePresenceStore.commit_observed` upserts the ``present`` /
   ``excluded`` rows of a scan. It never retires anything.
3. Names the scan no longer sees are only flagged with
   :meth:`ModulePresenceStore.mark_retire_pending`; the row stays
   ``present``/``excluded`` and the flag survives across runs until the
   retirement is executed.
4. :meth:`ModulePresenceStore.commit_retired` flips a row to ``retired`` and
   must be called only AFTER the graph and embedding delete (or ownership drop)
   for that name succeeded. A gate trip, ``--no-retire`` or a crash therefore
   leaves the row pending, and the next run re-evaluates it.

Allowed row transitions::

    (no row)          -> present | excluded        commit_observed
    present           -> excluded                  commit_observed
    excluded          -> present | excluded        commit_observed (reason may change)
    present|excluded  -> retire_pending=true       mark_retire_pending / mark_repo_removed
    retire_pending    -> retire_pending=false      commit_observed (name seen again)
    present|excluded  -> retired                   commit_retired (pending or not)
    retired           -> present | excluded        commit_observed (resurrection:
                                                   resurrection_count += 1, removing
                                                   commit / successor / reasons cleared)
    retired           -> retired                   no-op (commit_retired returns False)

Locking (review amendment H2b)
------------------------------
Every ledger write for an Odoo version runs under the session-level advisory
lock ``retire:<odoo_version>`` (:func:`retire_lock_id`), waited for up to
``RETIRE_LOCK_WAIT_SECONDS``. The per-version reconcile holds it through
:meth:`ModulePresenceStore.version_lock` and MUST pass the yielded connection
as ``conn=`` to every store call it makes while holding it: the lock is
re-entrant only within the same database session, so a call that checks out a
different connection would wait on the caller's own lock until it times out.

Connections
-----------
Every method takes an optional ``conn``. Omitted, a pooled connection is
checked out and the write runs in its own transaction. Given an autocommit
connection, the method runs its own transaction on it. Given a connection in
manual-transaction mode (``autocommit=False``), the method joins the caller's
transaction and does not commit; such a caller should hold
:meth:`ModulePresenceStore.version_lock` around its whole transaction, because a
method's own lock hold ends when the method returns, before the caller commits.

This module is part of the DB layer: it must not import ``src.indexer`` or
``src.mcp``. The indexer maps its registry scan onto :class:`ObservedModule`.
"""
from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from psycopg2.extras import RealDictCursor, execute_values

from src.constants import RETIRE_LOCK_WAIT_SECONDS
from src.db._types import PgConn
from src.db.exceptions import LifecycleLockTimeout, RepoNotFoundError
from src.db.pg import PgPool, advisory_lock, advisory_lock_id

ObservedState = Literal["present", "excluded"]
ExclusionReason = Literal["installable_false", "license_skip", "unparseable"]
RetireReason = Literal["absent", "repo_removed", "orphan_sweep"]
SuccessorSource = Literal["git_rename", "old_technical_name"]

EXCLUSION_REASONS: frozenset[str] = frozenset(
    {"installable_false", "license_skip", "unparseable"}
)
RETIRE_REASONS: frozenset[str] = frozenset({"absent", "repo_removed", "orphan_sweep"})
SUCCESSOR_SOURCES: frozenset[str] = frozenset({"git_rename", "old_technical_name"})

_RETIRE_LOCK_NAMESPACE = "osm-retire:"


def retire_lock_id(odoo_version: str) -> int:
    """Advisory lock id of the per-version ledger lock ``retire:<odoo_version>``."""
    return advisory_lock_id(f"{_RETIRE_LOCK_NAMESPACE}{odoo_version}")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservedModule:
    """One module directory as the registry resolved it for a repo scan.

    ``name`` must be unique within one scan: the registry picks the winner
    among same-name manifests inside the repo and passes the losers'
    repo-relative module paths as ``shadowed_paths`` (the posbox
    ``point_of_sale`` stub beside the real module, odoo12-18).
    """

    name: str
    path: str
    manifest_file: str
    state: ObservedState = "present"
    exclusion_reason: ExclusionReason | None = None
    version_raw: str | None = None
    version_mismatch: bool = False
    shadowed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ObservedModule.name must be non-empty")
        if self.state == "present":
            if self.exclusion_reason is not None:
                raise ValueError(
                    f"{self.name}: a present module carries no exclusion_reason"
                )
        elif self.state == "excluded":
            if self.exclusion_reason not in EXCLUSION_REASONS:
                raise ValueError(
                    f"{self.name}: excluded needs exclusion_reason in "
                    f"{sorted(EXCLUSION_REASONS)}, got {self.exclusion_reason!r}"
                )
        else:
            raise ValueError(
                f"{self.name}: observed state must be 'present' or 'excluded', "
                f"got {self.state!r}"
            )
        object.__setattr__(self, "shadowed_paths", tuple(self.shadowed_paths))


@dataclass(frozen=True)
class RetireEvidence:
    """The commit that removed a module's manifest (``git log -1 --diff-filter=D``)."""

    sha: str | None = None
    date: datetime | str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class Successor:
    """Where a retired module went. Never inferred from similarity or subjects."""

    names: tuple[str, ...]
    source: SuccessorSource

    def __post_init__(self) -> None:
        names = tuple(n for n in self.names if n)
        if not names:
            raise ValueError("Successor.names must contain at least one name")
        if self.source not in SUCCESSOR_SOURCES:
            raise ValueError(
                f"Successor.source must be one of {sorted(SUCCESSOR_SOURCES)}, "
                f"got {self.source!r}"
            )
        object.__setattr__(self, "names", names)


@dataclass(frozen=True)
class PresenceDiff:
    """Read-only classification of one repo scan against the ledger.

    Every observed name lands in exactly one of ``added``, ``resurrected``,
    ``became_present``, ``became_excluded``, ``reason_changed`` or
    ``unchanged``. Every non-retired ledger row the scan did not observe lands
    in ``absent`` (mapped to its current state), whether or not it is
    already ``retire_pending``.
    """

    repo_id: int
    presence_head_sha: str | None
    never_synced: bool
    n_present_before: int
    added: tuple[str, ...] = ()
    resurrected: tuple[str, ...] = ()
    became_present: tuple[str, ...] = ()
    became_excluded: Mapping[str, str] = field(default_factory=dict)
    reason_changed: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    unchanged: tuple[str, ...] = ()
    moved: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    absent: Mapping[str, str] = field(default_factory=dict)
    already_pending: tuple[str, ...] = ()
    pending_cleared: tuple[str, ...] = ()

    @property
    def absent_from_present(self) -> tuple[str, ...]:
        """Names that were ``present`` and are gone from the scan (G-B numerator)."""
        return tuple(sorted(n for n, s in self.absent.items() if s == "present"))

    @property
    def unparseable_from_present(self) -> tuple[str, ...]:
        """``present -> excluded(unparseable)`` names; G-B counts them (M3)."""
        return tuple(sorted(
            n for n, r in self.became_excluded.items() if r == "unparseable"
        ))


@dataclass(frozen=True)
class CommitObservedResult:
    inserted: tuple[str, ...]
    resurrected: tuple[str, ...]
    state_changed: tuple[str, ...]
    pending_cleared: tuple[str, ...]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_UPSERT_OBSERVED_SQL = """
INSERT INTO module_presence (
    repo_id, repo_url, repo_basename, repo_branch, profile_name, odoo_version,
    name, path, manifest_file, shadowed_paths, state, exclusion_reason,
    version_raw, version_mismatch,
    first_seen_sha, first_seen_at, last_seen_sha, last_seen_at,
    state_changed_sha, state_changed_at, updated_at
) VALUES %s
ON CONFLICT (repo_id, name) DO UPDATE SET
    repo_url         = EXCLUDED.repo_url,
    repo_basename    = EXCLUDED.repo_basename,
    repo_branch      = EXCLUDED.repo_branch,
    profile_name     = EXCLUDED.profile_name,
    odoo_version     = EXCLUDED.odoo_version,
    path             = EXCLUDED.path,
    manifest_file    = EXCLUDED.manifest_file,
    shadowed_paths   = EXCLUDED.shadowed_paths,
    version_raw      = EXCLUDED.version_raw,
    version_mismatch = EXCLUDED.version_mismatch,
    last_seen_sha    = EXCLUDED.last_seen_sha,
    last_seen_at     = EXCLUDED.last_seen_at,
    state_changed_sha = CASE
        WHEN module_presence.state IS DISTINCT FROM EXCLUDED.state
          OR module_presence.exclusion_reason IS DISTINCT FROM EXCLUDED.exclusion_reason
        THEN EXCLUDED.state_changed_sha ELSE module_presence.state_changed_sha END,
    state_changed_at = CASE
        WHEN module_presence.state IS DISTINCT FROM EXCLUDED.state
          OR module_presence.exclusion_reason IS DISTINCT FROM EXCLUDED.exclusion_reason
        THEN EXCLUDED.state_changed_at ELSE module_presence.state_changed_at END,
    resurrection_count = module_presence.resurrection_count
        + CASE WHEN module_presence.state = 'retired' THEN 1 ELSE 0 END,
    state                   = EXCLUDED.state,
    exclusion_reason        = EXCLUDED.exclusion_reason,
    retire_reason           = NULL,
    retire_pending          = FALSE,
    retire_pending_reason   = NULL,
    retire_pending_at       = NULL,
    retire_blocked_by       = NULL,
    removing_commit_sha     = NULL,
    removing_commit_date    = NULL,
    removing_commit_subject = NULL,
    successor_names         = NULL,
    successor_source        = NULL,
    needs_rewrite = CASE WHEN EXCLUDED.state = 'present'
                         THEN module_presence.needs_rewrite ELSE FALSE END,
    updated_at              = EXCLUDED.updated_at
"""

_LIFECYCLE_ROWS_SQL = """
SELECT CASE WHEN mp.name = %(name)s THEN 'self' ELSE 'predecessor' END AS relation,
       mp.*
FROM module_presence mp
WHERE (mp.name = %(name)s OR %(name)s = ANY (mp.successor_names))
  AND (%(v)s::text IS NULL OR mp.odoo_version = %(v)s::text)
  AND (%(scoped)s = FALSE OR mp.profile_name = ANY (%(allowed)s))
ORDER BY CASE WHEN mp.odoo_version ~ '^[0-9]+([.][0-9]+)?$'
              THEN mp.odoo_version::numeric END DESC NULLS LAST,
         mp.odoo_version, relation DESC, mp.repo_basename, mp.name, mp.id
"""


class ModulePresenceStore:
    """Two-phase ledger API over ``module_presence`` + the repos lifecycle columns."""

    def __init__(
        self, pool: PgPool, *, lock_wait_seconds: float = RETIRE_LOCK_WAIT_SECONDS,
    ) -> None:
        self._pool = pool
        self._lock_wait_seconds = lock_wait_seconds

    # ------------------------------------------------------------------
    # Connection / transaction / lock plumbing
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self, conn: PgConn | None) -> Generator[PgConn, None, None]:
        if conn is not None:
            yield conn
            return
        with self._pool.checkout() as c:
            yield c

    @staticmethod
    @contextmanager
    def _tx(conn: PgConn) -> Generator[PgConn, None, None]:
        if not conn.autocommit:
            yield conn
            return
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    @contextmanager
    def _hold_versions(
        self, conn: PgConn, versions: Iterable[str],
    ) -> Generator[None, None, None]:
        # Sorted acquisition keeps two multi-version writers deadlock-free.
        with ExitStack() as stack:
            for version in sorted(set(versions)):
                acquired = stack.enter_context(advisory_lock(
                    conn, retire_lock_id(version),
                    wait_seconds=self._lock_wait_seconds,
                ))
                if not acquired:
                    raise LifecycleLockTimeout(
                        f"ledger lock retire:{version} not acquired within "
                        f"{self._lock_wait_seconds:g}s"
                    )
            yield

    @contextmanager
    def _write(
        self, conn: PgConn | None, versions: Iterable[str],
    ) -> Generator[PgConn, None, None]:
        with self._conn(conn) as c, self._hold_versions(c, versions), self._tx(c):
            yield c

    @contextmanager
    def version_lock(
        self, odoo_version: str, *, conn: PgConn | None = None,
    ) -> Generator[PgConn, None, None]:
        """Hold ``retire:<odoo_version>`` and yield the connection that holds it.

        Pass the yielded connection as ``conn=`` to every store call made while
        holding the lock. Raises :class:`LifecycleLockTimeout` when the lock is
        not acquired within the wait budget.
        """
        with self._conn(conn) as c, self._hold_versions(c, [odoo_version]):
            yield c

    @staticmethod
    def _fetch_all(conn: PgConn, sql: str, params: tuple | dict = ()) -> list[dict]:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @classmethod
    def _fetch_one(cls, conn: PgConn, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = cls._fetch_all(conn, sql, params)
        return rows[0] if rows else None

    @classmethod
    def _repo_row(cls, conn: PgConn, repo_id: int) -> dict:
        row = cls._fetch_one(
            conn,
            "SELECT r.id, r.url, r.branch, r.local_path, r.head_sha, "
            "r.presence_head_sha, "
            "regexp_replace(r.local_path, '/+$', '') AS local_path_trimmed "
            "FROM repos r WHERE r.id = %s",
            (repo_id,),
        )
        if row is None:
            raise RepoNotFoundError(f"repo id={repo_id} not found")
        row["basename"] = row["local_path_trimmed"].rsplit("/", 1)[-1]
        return row

    @classmethod
    def _repo_versions(cls, conn: PgConn, repo_id: int) -> list[str]:
        rows = cls._fetch_all(
            conn,
            "SELECT DISTINCT odoo_version FROM module_presence WHERE repo_id = %s",
            (repo_id,),
        )
        return [r["odoo_version"] for r in rows]

    # ------------------------------------------------------------------
    # Phase 1: diff (read-only) + observed commit
    # ------------------------------------------------------------------

    @staticmethod
    def _index_observed(observed: Iterable[ObservedModule]) -> dict[str, ObservedModule]:
        by_name: dict[str, ObservedModule] = {}
        for obs in observed:
            if obs.name in by_name:
                raise ValueError(
                    f"module {obs.name!r} observed twice in one scan; the registry "
                    "must pass one winner per name and the rest as shadowed_paths"
                )
            by_name[obs.name] = obs
        return by_name

    def diff(
        self,
        repo_id: int,
        observed: Iterable[ObservedModule],
        *,
        conn: PgConn | None = None,
    ) -> PresenceDiff:
        """Classify a scan of *repo_id* against its ledger rows. Writes nothing."""
        by_name = self._index_observed(observed)
        with self._conn(conn) as c:
            repo = self._repo_row(c, repo_id)
            rows = self._fetch_all(
                c,
                "SELECT name, state, exclusion_reason, path, retire_pending "
                "FROM module_presence WHERE repo_id = %s",
                (repo_id,),
            )
        existing = {r["name"]: r for r in rows}

        added: list[str] = []
        resurrected: list[str] = []
        became_present: list[str] = []
        became_excluded: dict[str, str] = {}
        reason_changed: dict[str, tuple[str, str]] = {}
        unchanged: list[str] = []
        moved: dict[str, tuple[str, str]] = {}
        pending_cleared: list[str] = []

        for name in sorted(by_name):
            obs = by_name[name]
            row = existing.get(name)
            if row is None:
                added.append(name)
                continue
            if row["retire_pending"]:
                pending_cleared.append(name)
            if row["path"] != obs.path:
                moved[name] = (row["path"], obs.path)
            prev = row["state"]
            if prev == "retired":
                resurrected.append(name)
            elif prev == "present" and obs.state == "excluded":
                became_excluded[name] = obs.exclusion_reason  # type: ignore[assignment]
            elif prev == "excluded" and obs.state == "present":
                became_present.append(name)
            elif prev == "excluded" and row["exclusion_reason"] != obs.exclusion_reason:
                reason_changed[name] = (row["exclusion_reason"], obs.exclusion_reason)  # type: ignore[assignment]
            else:
                unchanged.append(name)

        absent = {
            n: r["state"]
            for n, r in sorted(existing.items())
            if r["state"] != "retired" and n not in by_name
        }
        already_pending = tuple(
            n for n in absent if existing[n]["retire_pending"]
        )
        return PresenceDiff(
            repo_id=repo_id,
            presence_head_sha=repo["presence_head_sha"],
            never_synced=repo["presence_head_sha"] is None and not existing,
            n_present_before=sum(1 for r in rows if r["state"] == "present"),
            added=tuple(added),
            resurrected=tuple(resurrected),
            became_present=tuple(became_present),
            became_excluded=became_excluded,
            reason_changed=reason_changed,
            unchanged=tuple(unchanged),
            moved=moved,
            absent=absent,
            already_pending=already_pending,
            pending_cleared=tuple(pending_cleared),
        )

    def commit_observed(
        self,
        repo_id: int,
        *,
        profile_name: str,
        odoo_version: str,
        head_sha: str,
        observed: Iterable[ObservedModule],
        observed_at: datetime | None = None,
        conn: PgConn | None = None,
    ) -> CommitObservedResult:
        """Upsert the ``present``/``excluded`` rows of one repo scan at *head_sha*.

        Never retires: names missing from *observed* keep their rows untouched
        (flag them with :meth:`mark_retire_pending`). An observed name that was
        ``retire_pending`` is un-flagged; one that was ``retired`` is resurrected
        (``resurrection_count`` += 1; removing commit, successor and reasons are
        cleared, L5). ``first_seen_*`` is set once and kept forever;
        ``state_changed_*`` moves only when the state or exclusion reason changes.
        ``needs_rewrite`` survives the upsert while the row stays ``present``.
        """
        by_name = self._index_observed(observed)
        if not profile_name:
            raise ValueError("profile_name is required")
        if not by_name:
            return CommitObservedResult((), (), (), ())
        now = observed_at or datetime.now(UTC)
        with self._conn(conn) as c:
            versions = {odoo_version, *self._repo_versions(c, repo_id)}
            with self._hold_versions(c, versions), self._tx(c):
                repo = self._repo_row(c, repo_id)
                prev = {
                    r["name"]: r
                    for r in self._fetch_all(
                        c,
                        "SELECT name, state, exclusion_reason, retire_pending "
                        "FROM module_presence WHERE repo_id = %s AND name = ANY(%s) "
                        "FOR UPDATE",
                        (repo_id, list(by_name)),
                    )
                }
                values = [
                    (
                        repo_id, repo["url"], repo["basename"], repo["branch"],
                        profile_name, odoo_version,
                        obs.name, obs.path, obs.manifest_file, list(obs.shadowed_paths),
                        obs.state, obs.exclusion_reason,
                        obs.version_raw, obs.version_mismatch,
                        head_sha, now, head_sha, now,
                        head_sha, now, now,
                    )
                    for obs in (by_name[n] for n in sorted(by_name))
                ]
                with c.cursor() as cur:
                    execute_values(cur, _UPSERT_OBSERVED_SQL, values)

        inserted, resurrected, changed, cleared = [], [], [], []
        for name in sorted(by_name):
            row = prev.get(name)
            obs = by_name[name]
            if row is None:
                inserted.append(name)
                continue
            if row["retire_pending"]:
                cleared.append(name)
            if row["state"] == "retired":
                resurrected.append(name)
            elif (row["state"], row["exclusion_reason"]) != (obs.state, obs.exclusion_reason):
                changed.append(name)
        return CommitObservedResult(
            tuple(inserted), tuple(resurrected), tuple(changed), tuple(cleared),
        )

    # ------------------------------------------------------------------
    # Phase 2: pending -> retired
    # ------------------------------------------------------------------

    def mark_retire_pending(
        self,
        repo_id: int,
        names: Iterable[str],
        reason: RetireReason = "absent",
        *,
        evidence: Mapping[str, RetireEvidence] | None = None,
        successors: Mapping[str, Successor] | None = None,
        blocked_by: str | None = None,
        conn: PgConn | None = None,
    ) -> list[str]:
        """Flag non-retired rows of *repo_id* as decided-to-retire. Returns the names flagged.

        The row keeps its ``present``/``excluded`` state (C1). The first flag
        time is kept across re-flags; the reason and ``blocked_by`` (why the
        last evaluation did not retire, e.g. a gate name) are overwritten.
        Evidence and successors, when given, are stored on the row so the
        reconcile can hand them to :meth:`commit_retired` without git access.
        Unknown or already-retired names are ignored.
        """
        if reason not in RETIRE_REASONS:
            raise ValueError(f"reason must be one of {sorted(RETIRE_REASONS)}, got {reason!r}")
        wanted = sorted(set(names))
        if not wanted:
            return []
        evidence = evidence or {}
        successors = successors or {}
        with self._conn(conn) as c:
            with self._write(c, self._repo_versions(c, repo_id)):
                rows = self._fetch_all(
                    c,
                    "UPDATE module_presence SET "
                    "retire_pending = TRUE, retire_pending_reason = %s, "
                    "retire_pending_at = COALESCE(retire_pending_at, now()), "
                    "retire_blocked_by = %s, updated_at = now() "
                    "WHERE repo_id = %s AND name = ANY(%s) AND state <> 'retired' "
                    "RETURNING name",
                    (reason, blocked_by, repo_id, wanted),
                )
                flagged = sorted(r["name"] for r in rows)
                with c.cursor() as cur:
                    for name in flagged:
                        self._store_evidence(
                            cur, repo_id, name, evidence.get(name), successors.get(name),
                        )
        return flagged

    @staticmethod
    def _store_evidence(
        cur, repo_id: int, name: str,
        evidence: RetireEvidence | None, successor: Successor | None,
    ) -> None:
        if evidence is not None:
            cur.execute(
                "UPDATE module_presence SET "
                "removing_commit_sha = COALESCE(%s, removing_commit_sha), "
                "removing_commit_date = COALESCE(%s::timestamptz, removing_commit_date), "
                "removing_commit_subject = COALESCE(%s, removing_commit_subject) "
                "WHERE repo_id = %s AND name = %s",
                (evidence.sha, evidence.date, evidence.subject, repo_id, name),
            )
        if successor is not None:
            cur.execute(
                "UPDATE module_presence SET successor_names = %s, successor_source = %s "
                "WHERE repo_id = %s AND name = %s",
                (list(successor.names), successor.source, repo_id, name),
            )

    def mark_retire_blocked(
        self, row_ids: Iterable[int], blocked_by: str | None, *, conn: PgConn | None = None,
    ) -> int:
        """Record why pending rows were not retired this run (by ledger row id)."""
        ids = sorted(set(row_ids))
        if not ids:
            return 0
        with self._conn(conn) as c:
            versions = [
                r["odoo_version"] for r in self._fetch_all(
                    c,
                    "SELECT DISTINCT odoo_version FROM module_presence WHERE id = ANY(%s)",
                    (ids,),
                )
            ]
            with self._write(c, versions):
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE module_presence SET retire_blocked_by = %s, updated_at = now() "
                        "WHERE id = ANY(%s) AND retire_pending",
                        (blocked_by, ids),
                    )
                    return cur.rowcount

    def commit_retired(
        self,
        repo_id: int | None,
        name: str,
        *,
        reason: RetireReason | None = None,
        evidence: RetireEvidence | None = None,
        successor: Successor | None = None,
        head_sha: str | None = None,
        row_id: int | None = None,
        conn: PgConn | None = None,
    ) -> bool:
        """Flip one row to ``retired``. Call ONLY after its graph/embedding delete succeeded.

        Identity is ``(repo_id, name)``; a row whose repo was deleted
        (``repo_id`` NULL) is addressed by ``row_id`` with ``repo_id=None``.
        ``reason`` defaults to the row's pending reason, else ``absent``.
        Evidence/successor given here win; otherwise the values stored by
        :meth:`mark_retire_pending` are kept. ``head_sha`` is recorded as the
        sha at which the state was entered. Returns False when there is no such
        row or it is already retired (idempotent; history is never overwritten).
        """
        if reason is not None and reason not in RETIRE_REASONS:
            raise ValueError(f"reason must be one of {sorted(RETIRE_REASONS)}, got {reason!r}")
        if repo_id is None and row_id is None:
            raise ValueError("commit_retired needs repo_id or row_id")
        with self._conn(conn) as c:
            if repo_id is not None:
                target = self._fetch_one(
                    c,
                    "SELECT id, odoo_version FROM module_presence "
                    "WHERE repo_id = %s AND name = %s",
                    (repo_id, name),
                )
            else:
                target = self._fetch_one(
                    c,
                    "SELECT id, odoo_version FROM module_presence "
                    "WHERE id = %s AND name = %s",
                    (row_id, name),
                )
            if target is None:
                return False
            with self._write(c, [target["odoo_version"]]):
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE module_presence SET "
                        "state = 'retired', "
                        "retire_reason = COALESCE(%s, retire_pending_reason, 'absent'), "
                        "exclusion_reason = NULL, "
                        "retire_pending = FALSE, retire_pending_reason = NULL, "
                        "retire_pending_at = NULL, retire_blocked_by = NULL, "
                        "needs_rewrite = FALSE, "
                        "state_changed_sha = %s, state_changed_at = now(), "
                        "updated_at = now() "
                        "WHERE id = %s AND state <> 'retired'",
                        (reason, head_sha, target["id"]),
                    )
                    if cur.rowcount == 0:
                        return False
                    if evidence is not None:
                        cur.execute(
                            "UPDATE module_presence SET "
                            "removing_commit_sha = COALESCE(%s, removing_commit_sha), "
                            "removing_commit_date = "
                            "COALESCE(%s::timestamptz, removing_commit_date), "
                            "removing_commit_subject = "
                            "COALESCE(%s, removing_commit_subject) "
                            "WHERE id = %s",
                            (evidence.sha, evidence.date, evidence.subject, target["id"]),
                        )
                    if successor is not None:
                        cur.execute(
                            "UPDATE module_presence SET successor_names = %s, "
                            "successor_source = %s WHERE id = %s",
                            (list(successor.names), successor.source, target["id"]),
                        )
        return True

    def mark_repo_removed(
        self, repo_id: int, *, conn: PgConn | None = None,
    ) -> list[dict]:
        """Flag every non-retired row of *repo_id* ``retire_pending`` (``repo_removed``).

        Run BEFORE the repos row is deleted (H3): the reconcile then drops this
        repo's ownership / retires each name and calls :meth:`commit_retired`
        with ``reason='repo_removed'``. Rows stay readable after the repo
        delete (``repo_id`` becomes NULL). Returns ``[{id, name, odoo_version}]``.
        """
        with self._conn(conn) as c:
            with self._write(c, self._repo_versions(c, repo_id)):
                rows = self._fetch_all(
                    c,
                    "UPDATE module_presence SET "
                    "retire_pending = TRUE, retire_pending_reason = 'repo_removed', "
                    "retire_pending_at = COALESCE(retire_pending_at, now()), "
                    "retire_blocked_by = NULL, needs_rewrite = FALSE, updated_at = now() "
                    "WHERE repo_id = %s AND state <> 'retired' "
                    "RETURNING id, name, odoo_version",
                    (repo_id,),
                )
        return sorted(rows, key=lambda r: (r["odoo_version"], r["name"]))

    def pending_retirements(
        self, odoo_version: str, *, conn: PgConn | None = None,
    ) -> list[dict]:
        """Every ``retire_pending`` row at *odoo_version*, including rows of deleted repos."""
        with self._conn(conn) as c:
            return self._fetch_all(
                c,
                "SELECT * FROM module_presence "
                "WHERE odoo_version = %s AND retire_pending "
                "ORDER BY name, repo_basename, id",
                (odoo_version,),
            )

    # ------------------------------------------------------------------
    # Ownership (per-name guards, H5)
    # ------------------------------------------------------------------

    def other_present_owners(
        self,
        name: str,
        odoo_version: str,
        exclude_repo_id: int | None = None,
        *,
        conn: PgConn | None = None,
    ) -> list[dict]:
        """Live repos (other than *exclude_repo_id*) with a ``present`` row for the name.

        A row already ``retire_pending`` still counts: its repo has not stopped
        owning the node until its own retirement is committed.
        """
        with self._conn(conn) as c:
            return self._fetch_all(
                c,
                "SELECT mp.id, mp.repo_id, mp.repo_url, mp.repo_basename, mp.repo_branch, "
                "mp.profile_name, mp.path, mp.last_seen_sha, mp.last_seen_at, "
                "mp.retire_pending "
                "FROM module_presence mp "
                "WHERE mp.name = %s AND mp.odoo_version = %s AND mp.state = 'present' "
                "AND mp.repo_id IS NOT NULL "
                "AND (%s::integer IS NULL OR mp.repo_id <> %s::integer) "
                "ORDER BY mp.repo_id",
                (name, odoo_version, exclude_repo_id, exclude_repo_id),
            )

    def potential_owners_unsynced(
        self,
        name: str,
        odoo_version: str,
        exclude_repo_id: int | None = None,
        *,
        conn: PgConn | None = None,
    ) -> list[dict]:
        """Unsynced repos at the version that might still ship *name* (H5a).

        A repo is unsynced when ``presence_head_sha`` is NULL or differs from
        ``head_sha``. It is a potential owner when its last ledger observation
        at this version had the name ``present``/``excluded``
        (``why='had_name'``), or when it has never been synced at all
        (``why='never_synced'``: no ledger rows and no presence head).
        A non-empty result makes the name undecidable for this run.
        """
        with self._conn(conn) as c:
            return self._fetch_all(
                c,
                """
                SELECT r.id AS repo_id, r.url AS repo_url,
                       regexp_replace(regexp_replace(r.local_path, '/+$', ''), '^.*/', '')
                           AS repo_basename,
                       p.name AS profile_name, r.head_sha, r.presence_head_sha,
                       CASE WHEN mp.id IS NOT NULL THEN 'had_name' ELSE 'never_synced' END
                           AS why
                FROM repos r
                JOIN profiles p ON p.id = r.profile_id
                LEFT JOIN module_presence mp
                       ON mp.repo_id = r.id AND mp.name = %(name)s
                      AND mp.odoo_version = %(v)s
                      AND mp.state IN ('present', 'excluded')
                WHERE (p.odoo_version = %(v)s
                       OR EXISTS (SELECT 1 FROM module_presence x
                                   WHERE x.repo_id = r.id AND x.odoo_version = %(v)s))
                  AND (r.presence_head_sha IS NULL OR r.head_sha IS NULL
                       OR r.presence_head_sha <> r.head_sha)
                  AND (%(ex)s::integer IS NULL OR r.id <> %(ex)s::integer)
                  AND (mp.id IS NOT NULL
                       OR (r.presence_head_sha IS NULL
                           AND NOT EXISTS (SELECT 1 FROM module_presence y
                                            WHERE y.repo_id = r.id)))
                ORDER BY r.id
                """,
                {"name": name, "v": odoo_version, "ex": exclude_repo_id},
            )

    def present_names(
        self, odoo_version: str, *, conn: PgConn | None = None,
    ) -> set[str]:
        """Names with at least one ``present`` row at the version (orphan-sweep input)."""
        with self._conn(conn) as c:
            rows = self._fetch_all(
                c,
                "SELECT DISTINCT name FROM module_presence "
                "WHERE odoo_version = %s AND state = 'present'",
                (odoo_version,),
            )
        return {r["name"] for r in rows}

    def repo_sync_state(
        self, odoo_version: str, *, conn: PgConn | None = None,
    ) -> list[dict]:
        """Registered repos at the version with their sync verdict (orphan attribution, H5b).

        ``synced`` is True only when ``presence_head_sha`` equals ``head_sha``.
        """
        with self._conn(conn) as c:
            return self._fetch_all(
                c,
                """
                SELECT r.id AS repo_id, p.name AS profile_name, r.url AS repo_url,
                       regexp_replace(regexp_replace(r.local_path, '/+$', ''), '^.*/', '')
                           AS repo_basename,
                       r.head_sha, r.presence_head_sha,
                       (r.presence_head_sha IS NOT NULL
                        AND r.presence_head_sha = r.head_sha) AS synced
                FROM repos r JOIN profiles p ON p.id = r.profile_id
                WHERE p.odoo_version = %(v)s
                   OR EXISTS (SELECT 1 FROM module_presence x
                               WHERE x.repo_id = r.id AND x.odoo_version = %(v)s)
                ORDER BY p.name, r.id
                """,
                {"v": odoo_version},
            )

    # ------------------------------------------------------------------
    # needs_rewrite (M5)
    # ------------------------------------------------------------------

    def mark_needs_rewrite(
        self, repo_id: int, name: str, *, conn: PgConn | None = None,
    ) -> bool:
        """Force *name* into the surviving owner's next write set. Only ``present`` rows."""
        with self._conn(conn) as c:
            with self._write(c, self._repo_versions(c, repo_id)):
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE module_presence SET needs_rewrite = TRUE, updated_at = now() "
                        "WHERE repo_id = %s AND name = %s AND state = 'present'",
                        (repo_id, name),
                    )
                    return cur.rowcount > 0

    def needs_rewrite_names(
        self, repo_id: int, *, conn: PgConn | None = None,
    ) -> list[str]:
        """Peek the names flagged ``needs_rewrite`` for *repo_id* (sorted, no clear)."""
        with self._conn(conn) as c:
            rows = self._fetch_all(
                c,
                "SELECT name FROM module_presence "
                "WHERE repo_id = %s AND needs_rewrite ORDER BY name",
                (repo_id,),
            )
        return [r["name"] for r in rows]

    def clear_needs_rewrite(
        self, repo_id: int, names: Iterable[str], *, conn: PgConn | None = None,
    ) -> int:
        """Clear ``needs_rewrite`` for names whose rewrite succeeded."""
        wanted = sorted(set(names))
        if not wanted:
            return 0
        with self._conn(conn) as c:
            with self._write(c, self._repo_versions(c, repo_id)):
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE module_presence SET needs_rewrite = FALSE, updated_at = now() "
                        "WHERE repo_id = %s AND name = ANY(%s) AND needs_rewrite",
                        (repo_id, wanted),
                    )
                    return cur.rowcount

    def consume_needs_rewrite(
        self, repo_id: int, *, conn: PgConn | None = None,
    ) -> list[str]:
        """Atomically read AND clear the ``needs_rewrite`` names of *repo_id*.

        A crash between this call and the rewrite loses the flags; callers that
        need crash safety use :meth:`needs_rewrite_names` then
        :meth:`clear_needs_rewrite` after the write succeeded.
        """
        with self._conn(conn) as c:
            with self._write(c, self._repo_versions(c, repo_id)):
                rows = self._fetch_all(
                    c,
                    "UPDATE module_presence SET needs_rewrite = FALSE, updated_at = now() "
                    "WHERE repo_id = %s AND needs_rewrite RETURNING name",
                    (repo_id,),
                )
        return sorted(r["name"] for r in rows)

    # ------------------------------------------------------------------
    # repos lifecycle columns (H1, H4)
    # ------------------------------------------------------------------

    def _update_repo(self, conn: PgConn | None, repo_id: int, sql: str, params: tuple) -> None:
        with self._conn(conn) as c, self._tx(c):
            with c.cursor() as cur:
                cur.execute(sql, params)
                if cur.rowcount == 0:
                    raise RepoNotFoundError(f"repo id={repo_id} not found")

    def mark_presence_synced(
        self, repo_id: int, head_sha: str, *, conn: PgConn | None = None,
    ) -> None:
        """Advance ``repos.presence_head_sha``: the ledger fully reflects *head_sha*.

        Callers advance it only when no gate tripped and nothing is pending (H1).
        """
        if not head_sha:
            raise ValueError("head_sha is required")
        self._update_repo(
            conn, repo_id,
            "UPDATE repos SET presence_head_sha = %s WHERE id = %s",
            (head_sha, repo_id),
        )

    def set_lifecycle_attention(
        self, repo_id: int, text: str, *, conn: PgConn | None = None,
    ) -> None:
        """Persist a lifecycle problem on the repo (gate trip, undecidable name).

        Separate from ``repos.status``, which ``update_repo_status`` overwrites.
        """
        if not text:
            raise ValueError("attention text is required")
        self._update_repo(
            conn, repo_id,
            "UPDATE repos SET lifecycle_attention = %s, lifecycle_attention_at = now() "
            "WHERE id = %s",
            (text, repo_id),
        )

    def clear_lifecycle_attention(
        self, repo_id: int, *, conn: PgConn | None = None,
    ) -> None:
        self._update_repo(
            conn, repo_id,
            "UPDATE repos SET lifecycle_attention = NULL, lifecycle_attention_at = NULL "
            "WHERE id = %s",
            (repo_id,),
        )

    # ------------------------------------------------------------------
    # Profile rename (L7)
    # ------------------------------------------------------------------

    def rename_profile(
        self, old_name: str, new_name: str, *, conn: PgConn | None = None,
    ) -> int:
        """Rewrite the denormalized ``profile_name`` of every ledger row (history included).

        Pass the connection of the profile-rename transaction (autocommit off)
        so both commit or neither does. Returns the number of rows rewritten.
        """
        if not old_name or not new_name:
            raise ValueError("old_name and new_name are required")
        if old_name == new_name:
            return 0
        with self._conn(conn) as c:
            versions = [
                r["odoo_version"] for r in self._fetch_all(
                    c,
                    "SELECT DISTINCT odoo_version FROM module_presence WHERE profile_name = %s",
                    (old_name,),
                )
            ]
            with self._write(c, versions):
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE module_presence SET profile_name = %s, updated_at = now() "
                        "WHERE profile_name = %s",
                        (new_name, old_name),
                    )
                    return cur.rowcount

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def lifecycle_rows(
        self,
        name: str,
        odoo_version: str | None,
        allowed_profiles: list[str] | None,
        *,
        conn: PgConn | None = None,
    ) -> list[dict]:
        """Ledger rows about *name* visible to the caller's tenant scope.

        Returns the rows OF the name (``relation='self'``) and the rows of
        modules that recorded the name as a successor (``relation='predecessor'``,
        i.e. "renamed from" data), at *odoo_version* or at every version when
        it is None. ``allowed_profiles`` None means unscoped (admin); a list is
        enforced both by an explicit ``profile_name`` filter (fail-closed even
        for the table owner, which bypasses RLS) and by ``app.allowed_profiles``
        for the RLS policy; an empty list returns nothing. Ordered by version
        (numeric, newest first), relation, repo, name.
        """
        if allowed_profiles is not None and not allowed_profiles:
            return []
        guc = "*" if allowed_profiles is None else ",".join(allowed_profiles)
        params = {
            "name": name,
            "v": odoo_version,
            "scoped": allowed_profiles is not None,
            "allowed": list(allowed_profiles or []),
        }
        with self._conn(conn) as c, self._tx(c):
            with c.cursor() as cur:
                cur.execute("SET LOCAL app.allowed_profiles = %s", (guc,))
            return self._fetch_all(c, _LIFECYCLE_ROWS_SQL, params)

    def rows_for_repo(
        self, repo_id: int, *, conn: PgConn | None = None,
    ) -> list[dict]:
        """Every ledger row of *repo_id* (audit / ops), ordered by name."""
        with self._conn(conn) as c:
            return self._fetch_all(
                c,
                "SELECT * FROM module_presence WHERE repo_id = %s ORDER BY name",
                (repo_id,),
            )

    def lifecycle_counts(
        self, repo_ids: Iterable[int], *, conn: PgConn | None = None,
    ) -> dict[int, dict[str, int]]:
        """Per-repo ``{present, excluded, retired, retire_pending, needs_rewrite}`` counts.

        Every requested repo id is in the result (zeros when it has no rows).
        """
        ids = sorted(set(repo_ids))
        out = {
            rid: {"present": 0, "excluded": 0, "retired": 0,
                  "retire_pending": 0, "needs_rewrite": 0}
            for rid in ids
        }
        if not ids:
            return out
        with self._conn(conn) as c:
            rows = self._fetch_all(
                c,
                "SELECT repo_id, "
                "count(*) FILTER (WHERE state = 'present') AS present, "
                "count(*) FILTER (WHERE state = 'excluded') AS excluded, "
                "count(*) FILTER (WHERE state = 'retired') AS retired, "
                "count(*) FILTER (WHERE retire_pending) AS retire_pending, "
                "count(*) FILTER (WHERE needs_rewrite) AS needs_rewrite "
                "FROM module_presence WHERE repo_id = ANY(%s) GROUP BY repo_id",
                (ids,),
            )
        for r in rows:
            out[r["repo_id"]] = {k: int(r[k]) for k in out[r["repo_id"]]}
        return out
