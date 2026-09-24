# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/reconcile.py
"""Per-version module lifecycle reconcile (ADR-0056) - the ONLY place a module
is retired from the index.

The per-repo index path (``pipeline_repo._index_repo``) only OBSERVES: it
commits the ``present`` / ``excluded`` ledger rows of its scan and flags the
names its scan no longer contains ``retire_pending``. :func:`reconcile_version`
runs once per Odoo version after the repos of a run were indexed (end of
``index_profile``; after every profile worker joined in ``index_all``), holds
the ledger lock ``retire:<version>`` for its whole duration, re-reads ledger
ownership under it and decides every pending name on its own (review H5):

1. **Another repo still ships the name** (a ``present`` row outside the
   retiring repos) and no unsynced repo may also ship it (case 2's rule) ->
   the node survives: ``drop_module_owner`` resets its
   ownership (Module and whole subtree) to exactly the surviving owners, the
   retiring profiles' embeddings are deleted, the survivors are marked
   ``needs_rewrite`` so their next run rewrites the node's identity (M5).
2. **A repo that may still ship it is not synced** (with or without another
   present owner; its
   ledger does not reflect its HEAD: it had the name at its last observation,
   or it was never observed and its checkout tracks a manifest of the name or
   cannot be read by git - :class:`PotentialOwners`, decided per name) ->
   undecidable this run: nothing is deleted, the row stays pending, the
   retiring repo gets ``lifecycle_attention`` and the run exits non-zero
   (never a silent freeze).
3. **Nobody ships it** -> dependents are collected first (their ``DEPENDS_ON``
   edges vanish with the node), then ``retire_modules`` (Module + subtree),
   the embeddings of every owning profile are deleted, and only then the
   ledger row becomes ``retired`` with the removing commit and successor the
   index path recorded (review C1). A name a concurrent run re-wrote after this
   run started (``skipped_recent``) stays pending.

Then the orphan sweep (review M6, H5b) removes what the ledger never saw
(nodes indexed before the ledger existed, debris of the old Module-only
``--gc``): Module nodes with no ``present`` row anywhere, attributed through
their own ``profile[]`` / ``repo_id`` and swept only when every repo of those
profiles at the version is synced; module-owned children whose Module is gone;
embedding groups no live owner accounts for. The sweep is under the mass gate
G-B. Finally the version-wide GCs run (``gc_orphan_asset_bundles``,
``gc_unresolved_placeholders``, and ``gc_null_repo_dep_stubs`` once every
profile of the version ran) and, whenever anything was deleted or re-owned
(with or without the sweep), the same-name INHERITS re-link
(``reconcile_same_name_inherits``: DETACH DELETE drops the extender-to-definer
edges of a retired definer, review L3).

``retire=False`` (CLI ``--no-retire``) and ``dry_run=True`` delete nothing and
write nothing; the report still lists what would happen.

:func:`reconcile_removed_repos` is the Web UI repo/profile delete variant: the
same per-name decision for the removed repos' names, plus their Module nodes
the ledger never recorded, without the version-wide sweep.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.db.module_presence import (
    ModulePresenceStore,
    RetireEvidence,
)
from src.db.module_presence import (
    Successor as LedgerSuccessor,
)
from src.indexer.cross_repo import find_dependent_repos
from src.indexer.lifecycle import (
    BLOCKED_ERROR_PREFIX,
    BLOCKED_SKIPPED_RECENT,
    BLOCKED_UNDECIDABLE_PREFIX,
    SUCCESSOR_OLD_TECHNICAL_NAME,
    mass_gate_trips,
    reconcile_may_retry,
    removal_evidence,
)
from src.indexer.models import ModuleOwner
from src.indexer.protocols import IndexWriterProtocol

_logger = logging.getLogger(__name__)

_MANIFEST_CANDIDATES = ("__manifest__.py", "__openerp__.py")
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 0.5


@dataclass
class ReconcileReport:
    """What one :func:`reconcile_version` call decided and did.

    Name lists are sorted. In a dry run the action lists (``retired``,
    ``owner_dropped``, ``orphans_swept``, ...) are what WOULD happen. With
    ``retire_enabled`` False every eligible pending name is in ``blocked`` as
    ``no_retire`` and no sweep runs (nor with ``sweep=False``). ``undecidable``
    maps a pending name to the repos that must sync before it can be decided;
    ``blocked`` maps a pending name to the reason it was not evaluated (a repo
    gate trip, ``no_retire``);
    ``orphans_deferred`` maps an orphan Module to the unsynced profiles that
    still claim it. ``modules_deleted`` / ``children_deleted`` count the Neo4j
    nodes the cascade removed (0 in a dry run). ``gates_tripped`` holds
    ``orphan_sweep:<gate>`` when the sweep was stopped by G-B.
    ``needs_attention`` is what makes the CLI exit 3.

    Also filled, executed or dry, for the operator and ``lifecycle-audit``:
    ``owners_kept`` maps an owner-dropped name to the repos that keep it;
    ``orphan_candidates`` / ``child_orphan_candidates`` are the orphans the
    sweep (when it runs) found decidable before the mass gate (swept unless
    G-B trips);
    ``orphan_evidence`` (dry run only) maps an orphan Module to the git proof of
    how it left its repo: ``{repo_id, removing_commit: {sha, date, subject},
    successor: {names, source} | None}``.
    """
    odoo_version: str
    dry_run: bool = False
    retire_enabled: bool = True
    retired: list[str] = field(default_factory=list)
    owner_dropped: list[str] = field(default_factory=list)
    undecidable: dict[str, list[str]] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)
    skipped_recent: list[str] = field(default_factory=list)
    orphans_swept: list[str] = field(default_factory=list)
    orphans_deferred: dict[str, list[str]] = field(default_factory=dict)
    child_orphans_swept: list[str] = field(default_factory=list)
    embedding_orphans: list[tuple[str, str, int]] = field(default_factory=list)
    embeddings_deleted: int = 0
    modules_deleted: int = 0
    children_deleted: int = 0
    dependents_reset: int = 0
    ledger_orphan_rows: int = 0
    gates_tripped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    gc: dict[str, int] = field(default_factory=dict)
    presence_advanced: list[int] = field(default_factory=list)
    owners_kept: dict[str, list[str]] = field(default_factory=dict)
    orphan_candidates: list[str] = field(default_factory=list)
    child_orphan_candidates: list[str] = field(default_factory=list)
    orphan_evidence: dict[str, dict] = field(default_factory=dict)
    # Excluded co-owner drops waiting for unsynced repos that may ship the
    # module: name -> repo labels (re-evaluated every run; the node is untouched).
    excluded_owner_waiting: dict[str, list[str]] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return bool(self.gates_tripped or self.undecidable or self.errors)

    @property
    def deleted_anything(self) -> bool:
        return bool(
            self.retired or self.owner_dropped or self.orphans_swept
            or self.child_orphans_swept or self.embedding_orphans
        )

    def as_dict(self) -> dict:
        out = asdict(self)
        out["needs_attention"] = self.needs_attention
        return out


def _is_transient(exc: BaseException) -> bool:
    try:
        from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
        if isinstance(exc, TransientError | ServiceUnavailable | SessionExpired):
            return True
    except ImportError:  # pragma: no cover - neo4j is a hard dependency
        pass
    try:
        import psycopg2
        from psycopg2 import errors as pg_errors
        if isinstance(
            exc,
            pg_errors.DeadlockDetected | pg_errors.SerializationFailure
            | psycopg2.OperationalError,
        ):
            return True
    except ImportError:  # pragma: no cover - psycopg2 is a hard dependency
        pass
    text = f"{getattr(exc, 'code', '')} {exc}"
    return "DeadlockDetected" in text


def _retrying[T](label: str, fn: Callable[[], T]) -> T:
    """Run *fn*, retrying transient Neo4j / Postgres errors with backoff."""
    delay = _RETRY_BASE_DELAY_S
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            _logger.warning(
                "reconcile %s: transient error (%s: %s), retry %d/%d in %.1fs",
                label, type(exc).__name__, exc, attempt, _RETRY_ATTEMPTS - 1, delay,
            )
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


POTENTIAL_OWNER_HAD_NAME = "had_name"
POTENTIAL_OWNER_SHIPS_NAME = "ships_name"
POTENTIAL_OWNER_UNREADABLE = "checkout_unreadable"
_NEVER_SYNCED = "never_synced"


class PotentialOwners:
    """Per-module answer to "could an unsynced repo still ship this name" (H5a, F48).

    The one rule the reconcile (pending names), the entity prune
    (``pipeline_repo._prune_reparsed_modules``) and, through them, the
    dry-run ``lifecycle-audit`` apply. A repo whose ledger does not reflect
    its HEAD (``ModulePresenceStore.potential_owners_unsynced``) blocks the
    name only when it may actually ship it:

    - it has a ledger observation: its last one had the name
      ``present``/``excluded`` (``why='had_name'``);
    - it was never observed and has a checkout: git tracks a manifest of the
      name in it, through the same version dispatch as the scan
      (``why='ships_name'``), or git tracking of the checkout is unavailable
      (not a git work tree, no commit, git refusing it), so the answer is
      unknown and the repo counts as an owner (``why='checkout_unreadable'``);
    - it was never observed and has no checkout on disk: it cannot ship
      anything until it is cloned, and then its modules are simply added, so
      it never blocks (a never-cloned registration must not hold a version's
      retirements hostage, lane-db concern 1).

    A never-observed repo whose version rule places its modules at another
    version ships nothing at this one. The tracked names of each checkout are
    read once per instance (one decision pass).
    """

    def __init__(self, store, *, conn=None) -> None:
        self._store = store
        self._conn = conn
        self._tracked: dict[int, set[str] | None] = {}

    def blockers(
        self,
        name: str,
        odoo_version: str,
        exclude_repo_ids: Iterable[int | None] = (),
    ) -> list[dict]:
        """The unsynced repos that may ship *name* at the version.

        Rows of ``potential_owners_unsynced`` (repo_id, repo_url,
        repo_basename, profile_name, head_sha, presence_head_sha, local_path,
        repo_branch, profile_version) with ``why`` one of
        ``POTENTIAL_OWNER_HAD_NAME``, ``POTENTIAL_OWNER_SHIPS_NAME``,
        ``POTENTIAL_OWNER_UNREADABLE``; repos in *exclude_repo_ids* are left out.
        """
        excluded = {r for r in exclude_repo_ids if r is not None}
        kwargs = {"conn": self._conn} if self._conn is not None else {}
        out: list[dict] = []
        for row in self._store.potential_owners_unsynced(name, odoo_version, **kwargs):
            if row["repo_id"] in excluded:
                continue
            if row["why"] != _NEVER_SYNCED:
                out.append(row)
                continue
            why = self._checkout_verdict(row, name, odoo_version)
            if why is not None:
                out.append({**row, "why": why})
        return out

    def _checkout_verdict(self, row: Mapping, name: str, odoo_version: str) -> str | None:
        local_path = row.get("local_path")
        if not local_path or not Path(local_path).is_dir():
            return None
        repo_id = row["repo_id"]
        if repo_id not in self._tracked:
            self._tracked[repo_id] = _tracked_module_names(row, odoo_version)
        names = self._tracked[repo_id]
        if names is None:
            return POTENTIAL_OWNER_UNREADABLE
        return POTENTIAL_OWNER_SHIPS_NAME if name in names else None


def _tracked_module_names(row: Mapping, odoo_version: str) -> set[str] | None:
    """Names of the modules git tracks in the repo's checkout at *odoo_version*.

    None when git tracking is unavailable. The manifests go through the scan's
    version dispatch (``registry.filter_manifest_paths``) for the version the
    repo's version rule gives (``registry.resolve_repo_version``); a repo
    placed at another version ships nothing here.
    """
    from src.git_utils import list_tracked_manifests
    from src.indexer.registry import filter_manifest_paths, resolve_repo_version

    local_path = str(row["local_path"])
    tracked = list_tracked_manifests(local_path)
    if tracked is None:
        return None
    repo_version = resolve_repo_version(
        local_path, branch=row.get("repo_branch"),
        profile_version=row.get("profile_version") or odoo_version,
    ).odoo_version
    if repo_version is not None and repo_version != odoo_version:
        return set()
    root_name = Path(local_path.rstrip("/")).name
    return {
        (Path(p).parent.name or root_name)
        for p in filter_manifest_paths(tracked, repo_version or odoo_version)
    }


def _repo_label(row: Mapping) -> str:
    return f"{row.get('repo_basename') or '?'} (repo id={row.get('repo_id')})"


class _Reconciler:
    def __init__(
        self,
        odoo_version: str,
        *,
        writer: IndexWriterProtocol,
        store: ModulePresenceStore,
        conn,
        run_started_at,
        retire: bool,
        allow_mass_retire: bool,
        dry_run: bool,
        report: ReconcileReport,
    ) -> None:
        self.v = odoo_version
        self.writer = writer
        self.store = store
        self.conn = conn
        self.run_started_at = run_started_at
        self.execute = retire and not dry_run
        self.allow_mass_retire = allow_mass_retire
        self.report = report
        self._attention: dict[int, list[str]] = {}
        self._owners = PotentialOwners(store, conn=conn)
        # Dry run: repos whose presence head WOULD advance (step 1b), treated
        # as synced by the sweep exactly like the executed mark_presence_synced.
        self._assumed_synced: set[int] = set()
        self._heads: dict[int, str | None] | None = None

    # --- helpers -------------------------------------------------------------

    def _attend(self, repo_id: int | None, text: str) -> None:
        if repo_id is None:
            return
        bucket = self._attention.setdefault(repo_id, [])
        if text not in bucket:
            bucket.append(text)

    def flush_attention(self) -> None:
        """Append this reconcile's messages to each repo's lifecycle_attention.

        The index path already replaced the column with its own scan signals
        in this run, so appending keeps both.
        """
        if self.report.dry_run:
            return
        from src.db.pg import repo_store
        for repo_id, messages in sorted(self._attention.items()):
            try:
                current = (repo_store().get_repo_by_id(repo_id) or {}).get(
                    "lifecycle_attention",
                ) or ""
                parts = [p for p in current.split("; ") if p]
                for m in messages:
                    if m not in parts:
                        parts.append(m)
                self.store.set_lifecycle_attention(
                    repo_id, "; ".join(parts), conn=self.conn,
                )
            except Exception:  # noqa: BLE001 - attention is best-effort, never fatal
                _logger.exception(
                    "reconcile %s: could not write lifecycle_attention for repo id=%s",
                    self.v, repo_id,
                )

    def _block(self, rows: Iterable[Mapping], reason: str) -> None:
        if not self.execute:
            return
        ids = [r["id"] for r in rows]
        if ids:
            self.store.mark_retire_blocked(ids, reason, conn=self.conn)

    def _repo_head(self, repo_id: int | None) -> str | None:
        """The registered repo's current HEAD (``repos.head_sha``), None when gone."""
        if repo_id is None:
            return None
        if self._heads is None:
            self._heads = {
                r["repo_id"]: r.get("head_sha")
                for r in self.store.repo_sync_state(self.v, conn=self.conn)
            }
        return self._heads.get(repo_id)

    def _commit_retired(self, rows: Iterable[Mapping]) -> None:
        """Record each row ``retired`` at its repo's HEAD (``state_changed_sha``)."""
        for r in rows:
            self.store.commit_retired(
                r["repo_id"], r["name"],
                row_id=r["id"] if r["repo_id"] is None else None,
                head_sha=self._repo_head(r["repo_id"]),
                conn=self.conn,
            )

    def _delete_embeddings(
        self, name: str, profiles: Iterable[str], *, expected: int | None = None,
    ) -> int:
        from src.indexer.writer_pgvector import delete_module_embeddings
        n = _retrying(
            f"embeddings[{name}]",
            lambda: delete_module_embeddings(
                self.conn, name, self.v, sorted(set(profiles)), expected=expected,
            ),
        )
        self.report.embeddings_deleted += n
        return n

    def _count_deleted(self, result: Mapping) -> None:
        self.report.modules_deleted += int(result.get("modules") or 0)
        self.report.children_deleted += int(result.get("children") or 0)

    def _reset_dependents(self, names: set[str], exclude_basenames: set[str]) -> None:
        if not names:
            return
        basenames = [
            b for b in find_dependent_repos(self.writer.driver, self.v, names)
            if b not in exclude_basenames
        ]
        if not basenames or not self.execute:
            return
        from src.db.pg import repo_store
        ids = repo_store().get_repo_ids_by_local_path_basenames(basenames, self.v)
        if ids:
            n = repo_store().reset_head_sha(ids)
            self.report.dependents_reset += n
            _logger.info(
                "reconcile %s: reset head_sha on %d repo(s) depending on retired "
                "module(s) %s", self.v, n, ", ".join(sorted(names)),
            )

    # --- 1. pending names ----------------------------------------------------

    def pending(self, only_names: set[str] | None = None) -> None:
        rows = self.store.pending_retirements(self.v, conn=self.conn)
        by_name: dict[str, list[dict]] = {}
        for r in rows:
            if only_names is not None and r["name"] not in only_names:
                continue
            by_name.setdefault(r["name"], []).append(r)

        to_retire: dict[str, list[dict]] = {}
        for name in sorted(by_name):
            name_rows = by_name[name]
            eligible = [r for r in name_rows if reconcile_may_retry(r["retire_blocked_by"])]
            held = sorted({
                r["retire_blocked_by"] for r in name_rows
                if not reconcile_may_retry(r["retire_blocked_by"])
            })
            if held:
                self.report.blocked[name] = ", ".join(held)
            if not eligible:
                continue
            if not self.report.retire_enabled:
                self.report.blocked.setdefault(name, "no_retire")
                continue
            retiring_ids = {r["repo_id"] for r in eligible if r["repo_id"] is not None}
            owners = [
                o for o in self.store.other_present_owners(name, self.v, conn=self.conn)
                if o["repo_id"] not in retiring_ids
            ]
            # One rule for both outcomes (F48): an unsynced repo that may ship
            # the name must be heard before its ownership is decided - a drop
            # would reset the node (and subtree) to the synced survivors only,
            # hiding it from the unsynced repo's profile until that repo re-ran.
            blockers = self._owners.blockers(
                name, self.v, retiring_ids | {o["repo_id"] for o in owners},
            )
            if owners and not blockers:
                self._drop_owner(name, eligible, owners)
                continue
            if blockers:
                labels = [f"{_repo_label(b)} [{b['why']}]" for b in blockers]
                self.report.undecidable[name] = labels
                text = (
                    f"{BLOCKED_UNDECIDABLE_PREFIX} {name}@{self.v} kept: repo(s) "
                    f"{', '.join(labels)} not synced"
                )
                _logger.warning("reconcile %s: %s", self.v, text)
                self._block(eligible, text)
                for r in eligible:
                    self._attend(r["repo_id"], text)
                continue
            to_retire[name] = eligible

        if to_retire:
            self._retire(to_retire)

    def _drop_owner(self, name: str, rows: list[dict], owners: list[dict]) -> None:
        survivor_profiles = {o["profile_name"] for o in owners}
        gone_profiles = {r["profile_name"] for r in rows} - survivor_profiles
        self.report.owners_kept[name] = sorted({_repo_label(o) for o in owners})
        if not self.execute:
            self.report.owner_dropped.append(name)
            return
        try:
            _retrying(
                f"drop_module_owner[{name}]",
                lambda: self.writer.drop_module_owner(self.v, name, [
                    ModuleOwner(
                        profile_name=o["profile_name"],
                        repo_basename=o["repo_basename"],
                        path=o["path"],
                        repo_id=o["repo_id"],
                        repo_url=o["repo_url"],
                    )
                    for o in owners
                ]),
            )
            if gone_profiles:
                self._delete_embeddings(name, gone_profiles)
            for repo_id in sorted({o["repo_id"] for o in owners}):
                self.store.mark_needs_rewrite(repo_id, name, conn=self.conn)
            self._commit_retired(rows)
        except Exception as exc:  # noqa: BLE001 - one name never aborts the others
            self._error(name, rows, exc)
            return
        self.report.owner_dropped.append(name)

    def _retire(self, to_retire: dict[str, list[dict]]) -> None:
        names = sorted(to_retire)
        if not self.execute:
            self.report.retired.extend(names)
            return
        retiring_basenames = {
            r["repo_basename"] for rows in to_retire.values() for r in rows
        }
        try:
            profiles_before = self.writer.module_profiles(self.v, names)
            self._reset_dependents(set(names), retiring_basenames)
            result = _retrying(
                "retire_modules",
                lambda: self.writer.retire_modules(
                    self.v, names, run_started_at=self.run_started_at,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            for name in names:
                self._error(name, to_retire[name], exc)
            return
        self._count_deleted(result)
        skipped = set(result.get("skipped_recent") or [])
        for name in names:
            rows = to_retire[name]
            if name in skipped:
                self.report.skipped_recent.append(name)
                self._block(rows, BLOCKED_SKIPPED_RECENT)
                continue
            try:
                profiles = set(profiles_before.get(name, [])) | {
                    r["profile_name"] for r in rows
                }
                self._delete_embeddings(name, profiles)
                self._commit_retired(rows)
            except Exception as exc:  # noqa: BLE001
                self._error(name, rows, exc)
                continue
            self.report.retired.append(name)

    def _error(self, name: str, rows: list[dict], exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"[:300]
        self.report.errors[name] = message
        _logger.error("reconcile %s: %s not retired: %s", self.v, name, message)
        text = f"{BLOCKED_ERROR_PREFIX} {name}@{self.v} not retired ({message})"
        try:
            self._block(rows, text)
        except Exception:  # noqa: BLE001
            _logger.exception("reconcile %s: could not record the error on %s", self.v, name)
        for r in rows:
            self._attend(r["repo_id"], text)

    # --- 1b. presence heads held back only by pending names -------------------

    def advance_presence(self, advance: Mapping[int, str]) -> None:
        if not advance or not (self.execute or self.report.dry_run):
            return
        pending_rows = self.store.pending_retirements(self.v, conn=self.conn)
        if self.execute:
            still_pending = {r["repo_id"] for r in pending_rows}
        else:
            # What step 1 would have resolved (retired / owner dropped) is no
            # longer pending after an executed run.
            resolved = set(self.report.retired) | set(self.report.owner_dropped)
            still_pending = {r["repo_id"] for r in pending_rows if r["name"] not in resolved}
        for repo_id, head in sorted(advance.items()):
            if repo_id in still_pending or not head:
                continue
            if self.execute:
                self.store.mark_presence_synced(repo_id, head, conn=self.conn)
            else:
                self._assumed_synced.add(repo_id)
            self.report.presence_advanced.append(repo_id)

    # --- 2. orphan sweep -----------------------------------------------------

    def sweep(self) -> bool:
        """Orphan sweep; returns True when every repo at the version is synced.

        Unlike a pending name (which only a repo that had it, or a never
        observed checkout that tracks it, can still claim - :class:`PotentialOwners`),
        an orphan node carries no ledger
        history, so EVERY unsynced repo blocks the orphans of its profile - a
        registered repo whose checkout vanished may have written them. The
        blocking repos get lifecycle_attention naming what waits for them.
        """
        sync_rows = self.store.repo_sync_state(self.v, conn=self.conn)
        blocking = [
            r for r in sync_rows
            if not r["synced"] and r["repo_id"] not in self._assumed_synced
        ]
        unsynced_profiles = {r["profile_name"] for r in blocking}
        unsynced_repo_ids = {r["repo_id"] for r in blocking}
        all_synced = not blocking
        repo_by_id = {r["repo_id"]: r for r in sync_rows}

        # Names step 1 retired: executed, their rows are retired and their
        # nodes gone (no-op subtraction); in a dry run both still exist and
        # must not be counted again as orphans.
        retired = set(self.report.retired)
        present = self.store.present_names(self.v, conn=self.conn) - retired
        orphans = [
            n for n in self.writer.orphan_module_names(self.v, present) if n not in retired
        ]
        identity = self.writer.module_identity(self.v, orphans)
        decidable: list[str] = []
        for name in orphans:
            ident = identity.get(name, {})
            claims = sorted(set(ident.get("profile") or []) & unsynced_profiles)
            if ident.get("repo_id") in unsynced_repo_ids:
                claims = sorted(set(claims) | {
                    repo_by_id[ident["repo_id"]]["profile_name"],
                })
            if claims:
                self.report.orphans_deferred[name] = claims
            else:
                decidable.append(name)
        if self.report.orphans_deferred:
            deferred_profiles = {
                p for claims in self.report.orphans_deferred.values() for p in claims
            }
            for r in blocking:
                if r["profile_name"] in deferred_profiles:
                    n = sum(
                        1 for claims in self.report.orphans_deferred.values()
                        if r["profile_name"] in claims
                    )
                    self._attend(
                        r["repo_id"],
                        f"orphan sweep at {self.v}: {n} orphan module(s) of profile "
                        f"{r['profile_name']} kept until this repo is synced",
                    )

        child_names: list[str] = []
        if all_synced:
            child_names = [
                n for n in self.writer.orphan_child_keys(self.v)
                if n not in present and n not in set(orphans)
            ]

        # Nodes of modules the ledger positively saw become installable False /
        # license-skipped are removals by design, not a mass event (real case:
        # 436 modules flipped installable False at tvtmaaddons 19.0).
        exempt = self.store.excluded_names(self.v, conn=self.conn)
        indexed = set(self.writer.module_profiles(self.v)) - retired
        self.report.orphan_candidates = sorted(decidable)
        self.report.child_orphan_candidates = sorted(child_names)
        if self.report.dry_run:
            for name in orphans:
                found = self._orphan_evidence(name, identity.get(name, {}), repo_by_id, present)
                if found is not None:
                    repo, _path, evidence, successor = found
                    self.report.orphan_evidence[name] = {
                        "repo_id": repo["repo_id"],
                        "removing_commit": {
                            "sha": evidence.sha, "date": evidence.date,
                            "subject": evidence.subject,
                        },
                        "successor": (
                            {"names": list(successor.names), "source": successor.source}
                            if successor else None
                        ),
                    }
        n_swept = len((set(decidable) | set(child_names)) - exempt)
        n_before = len((set(indexed) | set(child_names)) - exempt)
        # Total wipe here = the sweep would empty the version while the ledger
        # shows nothing present at it (every repo lost everything).
        gate = mass_gate_trips(n_swept, n_before, len(present)) if n_swept else None
        if gate is not None:
            message = (
                f"orphan sweep at {self.v}: {n_swept} of {n_before} module(s) "
                f"would be removed"
            )
            if self.allow_mass_retire:
                _logger.warning("reconcile %s: %s; applied (--allow-mass-retire)", self.v, message)
            else:
                self.report.gates_tripped.append(f"orphan_sweep:{gate}")
                text = f"{message}; nothing swept (use --allow-mass-retire)"
                _logger.warning("reconcile %s: %s", self.v, text)
                for name in decidable:
                    self._attend(identity.get(name, {}).get("repo_id"), text)
                return all_synced

        if not self.execute:
            self.report.orphans_swept.extend(decidable)
            self.report.child_orphans_swept.extend(child_names)
            self._embedding_orphans(present, unsynced_profiles, delete=False)
            return all_synced

        targets = sorted(set(decidable) | set(child_names))
        if targets:
            try:
                self._reset_dependents(set(decidable), set())
                result = _retrying(
                    "retire_modules[orphans]",
                    lambda: self.writer.retire_modules(
                        self.v, targets, run_started_at=self.run_started_at,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self.report.errors["orphan_sweep"] = f"{type(exc).__name__}: {exc}"[:300]
                _logger.error("reconcile %s: orphan sweep failed: %s", self.v, exc)
                return all_synced
            self._count_deleted(result)
            skipped = set(result.get("skipped_recent") or [])
            for name in decidable:
                if name in skipped:
                    continue
                profiles = identity.get(name, {}).get("profile") or []
                try:
                    if profiles:
                        self._delete_embeddings(name, profiles)
                    self._record_orphan(name, identity.get(name, {}), repo_by_id, present)
                except Exception as exc:  # noqa: BLE001
                    self.report.errors[name] = f"{type(exc).__name__}: {exc}"[:300]
                    _logger.exception("reconcile %s: orphan %s cleanup incomplete", self.v, name)
                    continue
                self.report.orphans_swept.append(name)
            self.report.child_orphans_swept.extend(
                n for n in child_names if n not in skipped
            )
            if self.report.orphans_swept or self.report.child_orphans_swept:
                _logger.info(
                    "reconcile %s: orphan sweep removed %d module(s) %s and the "
                    "children of %d module-less name(s) %s",
                    self.v, len(self.report.orphans_swept),
                    ", ".join(self.report.orphans_swept),
                    len(self.report.child_orphans_swept),
                    ", ".join(self.report.child_orphans_swept),
                )
        self._embedding_orphans(present, unsynced_profiles, delete=True)
        return all_synced

    def _orphan_evidence(
        self, name: str, ident: Mapping, repo_by_id: Mapping[int, Mapping],
        present: set[str],
    ):
        """Git proof of how an orphan Module left the repo that last wrote it.

        Returns ``(repo row, repo-relative path, RemovalEvidence, ledger
        Successor | None)``, or None when the node has no registered repo with a
        checkout, or git has no deleting commit with a parent for its manifest.
        """
        repo = repo_by_id.get(ident.get("repo_id"))
        path = ident.get("path")
        if repo is None or not path or not repo.get("local_path"):
            return None
        local_path = str(repo["local_path"])
        if Path(path).is_absolute():
            try:
                path = str(Path(path).relative_to(local_path))
            except ValueError:
                return None
        evidence, successor = removal_evidence(
            local_path, path, _MANIFEST_CANDIDATES, present,
        )
        if evidence is None or evidence.parent_sha is None:
            return None
        ledger_successor = (
            LedgerSuccessor(successor.names, successor.source) if successor else None
        )
        if ledger_successor is None:
            declared = self.writer.modules_by_old_technical_name(
                self.v, [name], profiles=[repo["profile_name"]],
            ).get(name)
            if declared:
                ledger_successor = LedgerSuccessor(tuple(declared), SUCCESSOR_OLD_TECHNICAL_NAME)
        return repo, path, evidence, ledger_successor

    def _record_orphan(
        self, name: str, ident: Mapping, repo_by_id: Mapping[int, Mapping],
        present: set[str],
    ) -> None:
        """Ledger history for a swept node, when git can prove how it left."""
        found = self._orphan_evidence(name, ident, repo_by_id, present)
        if found is None:
            return
        repo, path, evidence, ledger_successor = found
        inserted = self.store.record_orphan_retired(
            repo["repo_id"], name,
            profile_name=repo["profile_name"],
            odoo_version=self.v,
            path=path,
            manifest_file=evidence.manifest_file,
            last_seen_sha=evidence.parent_sha,
            last_seen_at=evidence.date,
            evidence=RetireEvidence(evidence.sha, evidence.date, evidence.subject),
            successor=ledger_successor,
            head_sha=repo.get("head_sha"),
            conn=self.conn,
        )
        if inserted:
            self.report.ledger_orphan_rows += 1

    def _embedding_orphans(
        self, present: set[str], unsynced_profiles: set[str], *, delete: bool,
    ) -> None:
        from src.indexer.writer_pgvector import orphan_embedding_keys
        live = {
            (m, p)
            for m, profiles in self.writer.module_profiles(self.v).items()
            for p in profiles
        } | self.store.present_pairs(self.v, conn=self.conn)
        with self._vec_conn() as conn:
            groups = [
                g for g in orphan_embedding_keys(conn, self.v, live)
                if g[1] not in unsynced_profiles
            ]
        self.report.embedding_orphans = groups
        if not delete:
            return
        for module, profile, n in groups:
            try:
                self._delete_embeddings(module, [profile], expected=n)
            except Exception as exc:  # noqa: BLE001
                self.report.errors[f"embeddings:{module}:{profile}"] = (
                    f"{type(exc).__name__}: {exc}"[:300]
                )
        if groups:
            _logger.info(
                "reconcile %s: deleted %d orphan embedding group(s)", self.v, len(groups),
            )

    @contextmanager
    def _vec_conn(self):
        if self.conn is not None:
            yield self.conn
            return
        from src.db.pg import get_pool
        with get_pool().checkout() as conn:
            yield conn

    # --- 2b. residue of removed repos ----------------------------------------

    def sweep_removed_repo_residue(
        self, basenames: set[str], removed_repo_ids: set[int],
        removed_profiles: set[str],
    ) -> None:
        """Retire Module nodes a removed repo wrote that the ledger never recorded.

        A repo indexed before the ledger existed (or whose rows were lost) left
        Module nodes with ``Module.repo`` = its basename and no ``present`` row
        anywhere. A basename is a directory name and collides across profiles
        (``<base>/<profile>/odoo``), so a node is residue of the removed repos
        only when it also belongs to them: one of its ``Module.profile`` entries
        is a removed repo's profile, or it has no profile and its ``repo_id`` is
        a removed repo. Another tenant's node is never examined, retired or
        reported. A residue node is retired when no repo that could still ship
        it is unsynced (the orphan-sweep attribution rule, with the removed
        repos themselves not counted as blockers); otherwise it is kept and
        listed in ``orphans_deferred``. No G-B gate applies: removing every
        module of the repo is what the operator asked for.
        """
        if not basenames or not (removed_profiles or removed_repo_ids):
            return
        sync_rows = self.store.repo_sync_state(self.v, conn=self.conn)
        blocking = [
            r for r in sync_rows
            if not r["synced"] and r["repo_id"] not in removed_repo_ids
        ]
        unsynced_profiles = {r["profile_name"] for r in blocking}
        blocking_by_id = {r["repo_id"]: r for r in blocking}

        present = self.store.present_names(self.v, conn=self.conn)
        orphans = sorted({
            name
            for basename in sorted(basenames)
            for name in self.writer.orphan_module_names(self.v, present, repo=basename)
        })
        if not orphans:
            return
        identity = self.writer.module_identity(self.v, orphans)

        def belongs(ident: Mapping) -> bool:
            profiles = set(ident.get("profile") or [])
            if profiles:
                return bool(profiles & removed_profiles)
            return ident.get("repo_id") in removed_repo_ids

        orphans = [n for n in orphans if belongs(identity.get(n, {}))]
        decidable: list[str] = []
        for name in orphans:
            ident = identity.get(name, {})
            claims = set(ident.get("profile") or []) & unsynced_profiles
            if ident.get("repo_id") in blocking_by_id:
                claims.add(blocking_by_id[ident["repo_id"]]["profile_name"])
            if claims:
                self.report.orphans_deferred[name] = sorted(claims)
            else:
                decidable.append(name)
        if not self.execute or not decidable:
            self.report.orphans_swept.extend(decidable)
            return
        try:
            self._reset_dependents(set(decidable), set())
            result = _retrying(
                "retire_modules[removed repo residue]",
                lambda: self.writer.retire_modules(
                    self.v, decidable, run_started_at=self.run_started_at,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.report.errors["removed_repo_residue"] = f"{type(exc).__name__}: {exc}"[:300]
            _logger.error("reconcile %s: removed-repo residue not retired: %s", self.v, exc)
            return
        self._count_deleted(result)
        skipped = set(result.get("skipped_recent") or [])
        for name in decidable:
            if name in skipped:
                self.report.skipped_recent.append(name)
                continue
            try:
                self._delete_embeddings(name, identity.get(name, {}).get("profile") or [])
            except Exception as exc:  # noqa: BLE001
                self.report.errors[name] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            self.report.orphans_swept.append(name)

    # --- 3. version-wide GCs ---------------------------------------------------

    def global_gcs(self, run_dep_stub_gc: bool) -> None:
        if not self.execute:
            return
        gc = self.report.gc
        gc["asset_bundles"] = _retrying(
            "gc_orphan_asset_bundles", lambda: self.writer.gc_orphan_asset_bundles(self.v),
        )
        placeholders = _retrying(
            "gc_unresolved_placeholders",
            lambda: self.writer.gc_unresolved_placeholders(self.v),
        )
        gc["unresolved_placeholders"] = sum((placeholders or {}).values())
        if run_dep_stub_gc:
            gc["dep_stubs"] = _retrying(
                "gc_null_repo_dep_stubs", lambda: self.writer.gc_null_repo_dep_stubs(self.v),
            )

    def relink_inherits(self) -> None:
        """Re-MERGE same-name INHERITS edges once anything was retired or re-owned.

        DETACH DELETE of a retired definer drops its extenders' edges (review
        L3); this runs whenever the reconcile deleted or re-owned a node,
        independent of the sweep, so a skip night that still decides pending
        names never leaves the topology broken.
        """
        if not self.execute or not self.report.deleted_anything:
            return
        self.report.gc["inherits_relinked"] = _retrying(
            "reconcile_same_name_inherits",
            lambda: self.writer.reconcile_same_name_inherits(self.v),
        )


def reconcile_version(
    odoo_version: str,
    *,
    writer: IndexWriterProtocol,
    run_started_at,
    store: ModulePresenceStore | None = None,
    retire: bool = True,
    allow_mass_retire: bool = False,
    advance_presence: Mapping[int, str] | None = None,
    global_gc: bool | None = None,
    sweep: bool = True,
    dry_run: bool = False,
    conn=None,
) -> ReconcileReport:
    """Decide and execute every pending module retirement at *odoo_version*.

    Args:
        odoo_version:      the module key version.
        writer:            open Neo4j writer.
        run_started_at:    aware start of the run on the Neo4j clock
                           (``writer.server_now()`` taken before the first repo
                           was indexed); a node re-written after it is never
                           deleted.
        store:             ledger store (default: on the shared pool).
        retire:            False (``--no-retire``) decides nothing and deletes
                           nothing; pending rows stay pending.
        allow_mass_retire: bypass G-B for the orphan sweep (never G-A, which is
                           a per-repo gate evaluated by the index path).
        advance_presence:  ``{repo_id: head}`` of repos the index path held
                           back ONLY because they had pending names; their
                           ``presence_head_sha`` advances to *head* once none of
                           their names is pending any more (review H1).
        global_gc:         run ``gc_null_repo_dep_stubs``. None = only when
                           every registered repo at the version is synced (a
                           single-profile run cannot know other profiles are
                           done); ``index_all`` passes True after all workers
                           joined.
        sweep:             run the orphan sweep and the version-wide GCs. The
                           callers pass False when no repo at the version was
                           scanned this run (every repo took the unchanged
                           skip): nothing new can have become an orphan, so the
                           skip night stays cheap (ADR-0007). Pending names are
                           decided either way, and the INHERITS re-link runs
                           whenever a node was deleted or re-owned.
        dry_run:           compute the report without the lock, deleting and
                           writing nothing. The decisions are those of an
                           executed run: names step 1 would retire are not
                           counted as orphans, and repos in *advance_presence*
                           whose pending names would all be resolved count as
                           synced for the sweep (``lifecycle-audit``).
        conn:              an autocommit connection the caller owns (not from
                           the shared pool) to hold the ledger lock and run
                           every ledger / embeddings statement on. None = one
                           pooled connection for the whole call. Either way the
                           lock holder holds exactly one connection; the only
                           other pool use is a few brief ``repo_store()`` calls.

    Returns a :class:`ReconcileReport`. Raises only when the ledger lock cannot
    be acquired (``LifecycleLockTimeout``) or the ledger is unreachable;
    per-name failures are reported in ``errors`` and left pending.
    """
    if store is None:
        from src.db.pg import get_pool
        store = ModulePresenceStore(get_pool())
    report = ReconcileReport(
        odoo_version=odoo_version, dry_run=dry_run, retire_enabled=retire,
    )
    lock = nullcontext(conn) if dry_run else store.version_lock(odoo_version, conn=conn)
    with lock as conn:
        rec = _Reconciler(
            odoo_version, writer=writer, store=store, conn=conn,
            run_started_at=run_started_at, retire=retire,
            allow_mass_retire=allow_mass_retire, dry_run=dry_run, report=report,
        )
        rec.pending()
        rec.advance_presence(advance_presence or {})
        if retire and sweep:
            all_synced = rec.sweep()
            rec.global_gcs(all_synced if global_gc is None else global_gc)
        rec.relink_inherits()
        rec.flush_attention()

    for key in ("retired", "owner_dropped", "skipped_recent", "orphans_swept",
                "child_orphans_swept"):
        setattr(report, key, sorted(set(getattr(report, key))))
    _logger.info(
        "reconcile %s: retired %d, owner dropped %d, undecidable %d, blocked %d, "
        "orphans swept %d (deferred %d), child orphans %d, embedding orphan groups %d%s",
        odoo_version, len(report.retired), len(report.owner_dropped),
        len(report.undecidable), len(report.blocked), len(report.orphans_swept),
        len(report.orphans_deferred), len(report.child_orphans_swept),
        len(report.embedding_orphans),
        " (dry run)" if dry_run else ("" if retire else " (--no-retire)"),
    )
    return report


def reconcile_removed_repos(
    odoo_version: str,
    *,
    writer: IndexWriterProtocol,
    store: ModulePresenceStore,
    conn,
    run_started_at,
    names: Iterable[str],
    basenames: Iterable[str],
    removed_repo_ids: Iterable[int],
    removed_profiles: Iterable[str],
) -> ReconcileReport:
    """Decide the names of repos that are being removed (Web UI repo/profile delete).

    The caller has flagged every ledger row of the removed repos
    ``retire_pending`` (``ModulePresenceStore.mark_repo_removed``), holds
    ``retire:<odoo_version>`` on *conn* (``store.version_lock``), and deletes the
    repos rows only after this returns (review H3), so ownership is read from
    the ledger while the rows still name their repo. Per name *names* (the
    flagged names at this version), exactly as :func:`reconcile_version`
    decides them: a name another repo still ships keeps its node with that
    repo as the only owner (``drop_module_owner``; only the departing profiles'
    embeddings are deleted); a name nobody else ships is retired with its
    subtree and embeddings; a name an unsynced repo may still ship stays
    pending (``undecidable``). Then :meth:`_Reconciler.sweep_removed_repo_residue`
    retires the removed repos' Module nodes the ledger never recorded
    (*basenames* = their ``Module.repo`` values, restricted to nodes of
    *removed_profiles* / *removed_repo_ids* - basenames collide across profiles).

    Unlike :func:`reconcile_version` there is no version-wide orphan sweep and
    no version-wide GC (the request must stay short; the next index run at the
    version does both); only the same-name INHERITS re-link runs when a node
    was deleted (review L3). Rows left pending (undecidable, error,
    ``skipped_recent``) keep their ``repo_removed`` reason and are decided by the
    next reconcile, also after the repos row is gone (``repo_id`` NULL).
    """
    report = ReconcileReport(odoo_version=odoo_version)
    with store.version_lock(odoo_version, conn=conn) as locked:
        rec = _Reconciler(
            odoo_version, writer=writer, store=store, conn=locked,
            run_started_at=run_started_at, retire=True, allow_mass_retire=False,
            dry_run=False, report=report,
        )
        rec.pending(only_names=set(names))
        rec.sweep_removed_repo_residue(
            set(basenames), set(removed_repo_ids), set(removed_profiles),
        )
        rec.relink_inherits()
        rec.flush_attention()

    for key in ("retired", "owner_dropped", "skipped_recent", "orphans_swept"):
        setattr(report, key, sorted(set(getattr(report, key))))
    _logger.info(
        "reconcile %s (repo removal %s): retired %d, owner dropped %d, undecidable %d, "
        "residue retired %d (deferred %d), errors %d",
        odoo_version, ", ".join(sorted(set(basenames))), len(report.retired),
        len(report.owner_dropped), len(report.undecidable), len(report.orphans_swept),
        len(report.orphans_deferred), len(report.errors),
    )
    return report
