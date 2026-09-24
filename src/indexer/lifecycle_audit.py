# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/lifecycle_audit.py
"""Dry-run module lifecycle audit (ADR-0056, CLI ``lifecycle-audit``).

Answers "what would the next ``index-repo`` run retire, and what is wrong in the
index today" without writing anything to Neo4j or to the ledger.

It runs the SAME decision code as an index run, never a copy of it:

1. Per repo: ``pipeline_repo.plan_repo_run`` (skip / sync / incremental /
   full), ``build_registry_scan`` (B3 scan truth), ``observe_lifecycle``
   (trust + B5 classification and gates) and ``_commit_lifecycle`` (ledger
   rows, pending names with removing commit and successor, presence decision).
2. Per version: ``reconcile.reconcile_version(dry_run=True)`` (B7: retire /
   drop owner / undecidable, orphan sweep, embedding orphans).

Isolation, so the reuse cannot write:

- The ledger half runs against a SESSION-PRIVATE copy of ``module_presence`` and
  ``repos`` (``CREATE TEMP TABLE``, which shadows the real tables for the
  unqualified names the ledger store uses). The copy is taken in one
  REPEATABLE READ snapshot; after it the session is READ ONLY, so Postgres
  itself refuses any write outside ``pg_temp``. The copy vanishes with the
  connection. The simulation takes no ledger lock, so it never delays an index
  run.
- Neo4j is reached only through :class:`ReadOnlyWriter`, which exposes the read
  methods and answers ``stamp_module_presence`` as a successful run's stamp
  would (every present module matched) without writing.
- Every graph read carries a ``neo4j.Query`` timeout
  (``LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS``).

On top of the simulated run it reports rollout checks: Module nodes with a real
path and an empty profile list (F24, the read side answers "No" for them) and
modules whose node differs from the scan (``modules_needing_rewrite``: F15
posbox stub path, F7 untracked ``.odoo-ai`` copy, repo drift, a present module
whose node lost this profile). Every next run but the unchanged skip re-writes
those (``would_rewrite``, the run's own ``self_heal_rewrites`` set, on sync,
incremental and full alike; a module the run writes as new is not listed); a
repo whose next run is the unchanged skip keeps them (``wrong_paths``, a
finding). Entity prunes the soft gate holds (recorded on the Module node by the
index run, which exits 3 for them on every re-parse) are the ``held_prunes``
finding. An indexed module whose tracked manifest was read but does not parse
is kept as it is until it parses again (``unparseable_kept``, a finding; the
index run exits 3 for it); a manifest that cannot be read at all makes the scan
incomplete (gate G-A). The version reconcile's shared-module prune (a module two or more repos
ship loses what no present owner's latest complete parse defines) is predicted
from the ledger's complete-parse records as they stand: ``shared_prunes``
(``would_prune`` or ``held``) is a finding, ``shared_prune_waiting`` names the
modules an unsynced repo holds, ``shared_prune_rewrites`` the owners the reconcile
sends back to re-parse a module. Each repo entry's ``shared_parse_backlog``
(``remaining``, ``next_run``) is the bootstrap still to do: copies of shared
modules without a complete-parse record, re-parsed at most
``SHARED_PARSE_BOOTSTRAP_PER_RUN`` per repo per run (not a finding: it drains
by itself) (schema /3).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from src.db.module_presence import ModulePresenceStore
from src.indexer import incremental as _incremental
from src.indexer.lifecycle import reconcile_may_retry
from src.indexer.pipeline_repo import (
    REWRITE_NO_NODE,
    RUN_SKIP,
    _commit_lifecycle,
    _ConnBoundStore,
    _degraded_state,
    _owning_profiles,
    modules_needing_rewrite,
    observe_lifecycle,
    plan_repo_run,
    regular_write_names,
    self_heal_rewrites,
    shared_parse_backlog,
    shared_parse_bootstrap,
)
from src.indexer.reconcile import reconcile_version
from src.indexer.registry import build_registry_scan, resolve_repo_version

_logger = logging.getLogger(__name__)

AUDIT_SCHEMA = "osm.lifecycle-audit/3"

# Finding categories, in report order. Any non-zero count is a finding
# (``--fail-on-findings``).
FINDING_KEYS: tuple[str, ...] = (
    "would_retire",
    "would_drop_owner",
    "undecidable",
    "blocked",
    "held_prunes",
    "shared_prunes",
    "orphan_modules",
    "child_orphans",
    "embedding_orphans",
    "modules_without_profile",
    "would_rewrite",
    "wrong_paths",
    "unapplied_changes",
    "unparseable_kept",
    "errors",
)

_SIMULATED_TABLES = ("module_presence", "repos")


class ReadOnlyWriter:
    """Neo4j writer facade for the audit: reads only, the stamp is simulated.

    Any other writer attribute raises ``AttributeError`` so a write reached by
    the reused index / reconcile code fails loudly instead of touching the graph.
    """

    READS = frozenset({
        "orphan_module_names",
        "module_profiles",
        "module_identity",
        "modules_by_old_technical_name",
        "repo_module_baseline",
        "orphan_child_keys",
        "modules_without_profile",
        "parse_degraded_modules",
        "prune_held_modules",
        "prune_deferred_modules",
        "module_children_census",
        "shared_prune_states",
        "module_unattributed_latest",
    })

    def __init__(self, writer) -> None:
        self._writer = writer

    def __getattr__(self, name: str):
        if name in ReadOnlyWriter.READS:
            return getattr(self._writer, name)
        raise AttributeError(
            f"lifecycle-audit is read-only: writer.{name} is not available"
        )

    def stamp_module_presence(self, odoo_version, rows, head, now=None) -> int:
        """What the stamp of a successful run matches: every present module.

        The run writes each present module that has no node for its profile
        (changed modules, needs_rewrite, self-heal) before it stamps, so only a
        concurrent retire can make the real stamp fall short. No write here.
        """
        return len(list(rows))


class _SimulationStore(ModulePresenceStore):
    """Ledger store over the session-private copy: no ledger lock is taken.

    The copy is invisible to every other session, so there is nothing to
    serialize with; waiting for the real ``retire:<version>`` lock would only
    delay (or be delayed by) a concurrent index run.
    """

    def __init__(self) -> None:
        super().__init__(pool=None)

    @contextmanager
    def _hold_versions(self, conn, versions):
        yield


def open_simulation(dsn: str):
    """Open the audit's session with private copies of the ledger tables.

    Returns an autocommit psycopg2 connection whose unqualified
    ``module_presence`` / ``repos`` resolve to ``pg_temp`` copies of the real
    tables (one REPEATABLE READ snapshot) and whose transactions are READ ONLY
    (writes to real tables are refused by Postgres). ``app.allowed_profiles``
    is ``'*'`` so row-level security never hides ledger rows or embeddings.
    Raises RuntimeError when the shadowing cannot be verified.
    """
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ

    conn = psycopg2.connect(dsn, application_name="osm-lifecycle-audit")
    try:
        conn.autocommit = False
        conn.set_session(isolation_level=ISOLATION_LEVEL_REPEATABLE_READ)
        with conn.cursor() as cur:
            cur.execute("SET app.allowed_profiles = '*'")
            real: dict[str, sql.Composed] = {}
            for table in _SIMULATED_TABLES:
                cur.execute(
                    "SELECT n.nspname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.oid = to_regclass(%s)",
                    (table,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"table {table!r} not found (migrations not applied?)")
                real[table] = sql.Identifier(row[0], table)
            cur.execute(sql.SQL(
                "CREATE TEMP TABLE module_presence "
                "(LIKE {} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)"
            ).format(real["module_presence"]))
            cur.execute("CREATE TEMP SEQUENCE lifecycle_audit_module_presence_id")
            cur.execute(
                "ALTER TABLE pg_temp.module_presence ALTER COLUMN id "
                "SET DEFAULT nextval('pg_temp.lifecycle_audit_module_presence_id')"
            )
            cur.execute(sql.SQL(
                "INSERT INTO pg_temp.module_presence SELECT * FROM {}"
            ).format(real["module_presence"]))
            cur.execute(
                "SELECT setval('pg_temp.lifecycle_audit_module_presence_id', "
                "coalesce(max(id), 0) + 1, false) FROM pg_temp.module_presence"
            )
            cur.execute(sql.SQL(
                "CREATE TEMP TABLE repos AS SELECT * FROM {}"
            ).format(real["repos"]))
        conn.commit()
        with conn.cursor() as cur:
            for table in _SIMULATED_TABLES:
                cur.execute(
                    "SELECT to_regclass(%s) = to_regclass(%s)",
                    (table, f"pg_temp.{table}"),
                )
                if not cur.fetchone()[0]:
                    raise RuntimeError(
                        f"lifecycle-audit: {table!r} does not resolve to the session "
                        "copy; refusing to simulate against the real table"
                    )
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.commit()
        conn.autocommit = True
    except BaseException:
        conn.close()
        raise
    return conn


def _sim_repo_head(conn, repo_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE id = %s", (repo_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _sim_advance_head(conn, repo_id: int, head: str) -> None:
    # The index run advances repos.head_sha after its writes; the simulation
    # does the same on the session copy (pg_temp, verified in open_simulation).
    with conn.cursor() as cur:
        cur.execute("UPDATE pg_temp.repos SET head_sha = %s WHERE id = %s", (head, repo_id))


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _gate_dict(gates) -> dict:
    return {
        "scan_ok": gates.scan_ok,
        "mass_ok": gates.mass_ok,
        "retire_allowed": gates.retire_allowed,
        "tripped": list(gates.tripped),
        "bypassed": list(gates.bypassed),
        "reasons": list(gates.reasons),
        "n_soft_drop": gates.n_soft_drop,
        "n_present_before": gates.n_present_before,
        "baseline": gates.baseline,
    }


def _new_repo_entry(repo: dict) -> dict:
    local_path = repo.get("local_path") or ""
    return {
        "repo_id": repo["id"],
        "profile": repo.get("profile_name"),
        "url": repo.get("url"),
        "basename": Path(local_path).name,
        "branch": repo.get("branch"),
        "local_path": local_path,
        "profile_version": repo.get("odoo_version"),
        "odoo_version": None,
        "head": None,
        "head_sha": None,
        "presence_head_sha": None,
        "next_run": None,
        "scan": None,
        "scan_attention": [],
        "attention": [],
        "gates": None,
        "transitions": {},
        "pending": [],
        "unapplied_changes": {},
        "would_rewrite": [],
        "wrong_paths": [],
        "would_retire": [],
        "would_drop_owner": [],
        "undecidable": [],
        "blocked": [],
        "held_prunes": [],
        "unparseable_kept": [],
        "shared_parse_backlog": {"remaining": 0, "next_run": []},
        "error": None,
    }


def _audit_repo(repo: dict, *, conn, store, writer: ReadOnlyWriter) -> tuple[dict, str | None]:
    """Simulate one repo's index run on the session copy of the ledger.

    Returns the repo entry and the presence head the reconcile may advance
    (the index path's ``presence_deferred_head``), or None.
    """
    entry = _new_repo_entry(repo)
    local_path = entry["local_path"]
    repo_path = Path(local_path)
    if not local_path or not repo_path.is_dir():
        entry["next_run"] = "not_cloned"
        entry["attention"].append(
            "no checkout on disk: the index run fails this repo, nothing of it is "
            "observed"
        )
        return entry, None

    bound = _ConnBoundStore(store, conn)
    current_head = _incremental.get_repo_head(repo_path)
    last_head = _sim_repo_head(conn, repo["id"])
    presence_head = bound.presence_head_sha(repo["id"])
    rewrite_names = bound.needs_rewrite_names(repo["id"])
    entry.update(head=current_head, head_sha=last_head, presence_head_sha=presence_head)
    # B14: a degraded module whose failing files changed on disk makes the
    # real run re-parse at an unchanged HEAD; predict the same mode.
    _records, degraded_changed = _degraded_state(writer, repo, repo_path)
    # B14 shared-module bootstrap: the real run re-parses this batch like
    # needs_rewrite names (sync path at an unchanged HEAD).
    remaining, _batch = shared_parse_backlog(bound, repo, _records)
    bootstrap = shared_parse_bootstrap(bound, repo, _records)
    entry["shared_parse_backlog"] = {"remaining": remaining, "next_run": bootstrap}
    rewrite_names = sorted(set(rewrite_names) | set(bootstrap))
    plan = plan_repo_run(
        repo_path, current_head, last_head, presence_head, rewrite_names,
        full_reindex=False, ledger=True, degraded_changed=bool(degraded_changed),
    )
    entry["next_run"] = plan.mode

    scan = build_registry_scan(
        local_path, repo.get("odoo_version"),
        branch=repo.get("branch"), repo_url=repo.get("url"), repo_id=repo["id"],
    )
    entry["odoo_version"] = scan.odoo_version
    owning_profile = _owning_profiles(repo, repo.get("profile_name"), repo_path.name)[0]
    observation = observe_lifecycle(
        repo, scan, bound, current_head, writer=writer, owning_profile=owning_profile,
    )
    present = scan.present_names()
    entry["scan"] = {
        "complete": scan.complete,
        "trusted": observation.trusted,
        "tracked_available": scan.tracked_paths is not None,
        "present": len(present),
        "excluded": sum(1 for n in scan.excluded if n not in present),
        "shadowed": len(scan.shadowed),
        "untracked_manifests": len(scan.untracked),
        "missing_manifests": len(scan.missing),
        "unreadable_manifests": len(scan.unreadable),
    }
    entry["scan_attention"] = list(scan.attention)
    entry["gates"] = _gate_dict(observation.gates)
    entry["unparseable_kept"] = list(observation.unparseable_kept)
    changed = observation.transitions.changed
    kinds: dict[str, list[str]] = {}
    for t in changed:
        kinds.setdefault(t.kind, []).append(t.name)
    entry["transitions"] = {k: len(v) for k, v in sorted(kinds.items())}
    # A soft-gated entity prune stays held (the index run exits 3 on every
    # re-parse) until a run prunes the module, e.g. with --allow-mass-retire.
    entry["held_prunes"] = list(writer.prune_held_modules(repo["id"]))

    deferred_head: str | None = None
    attention = observation.attention
    if observation.lifecycle_on and present and plan.mode == RUN_SKIP:
        # A module without a node for this profile is reported as
        # modules_without_profile (F24) or is simply not indexed yet.
        drift = [
            {"name": name, **detail}
            for name, detail in sorted(
                modules_needing_rewrite(writer, bound, repo, scan, owning_profile).items()
            )
            if detail["reason"] != REWRITE_NO_NODE
        ]
        entry["wrong_paths"] = drift
        if drift:
            attention.append(
                f"{len(drift)} module node(s) differ from the scan at the unchanged "
                "HEAD; the next run skips this repo, so they stay until the module "
                "changes or index-repo --full"
            )
    elif observation.lifecycle_on and present:
        # The run's own heal set; a module the run writes as NEW (no node at
        # all, and in its regular write set) is not a rewrite.
        heal = self_heal_rewrites(
            writer, bound, repo, scan, owning_profile, plan=plan, lifecycle_on=True,
        )
        regular = regular_write_names(repo_path, scan, plan, current_head) if heal else set()
        entry["would_rewrite"] = [
            {"name": name, **detail}
            for name, detail in sorted(heal.items())
            if not (
                detail["reason"] == REWRITE_NO_NODE and not detail.get("has_node")
                and name in regular
            )
        ]
    reparse = sorted((set(rewrite_names) | degraded_changed) & present)
    if reparse and plan.mode != RUN_SKIP:
        # The audit does not parse, so it cannot count what an entity prune
        # (B14) would remove; it names the modules whose held, deferred or
        # degraded prune the next run re-evaluates. Nothing is pruned here.
        attention.append(
            f"{len(reparse)} module(s) are re-parsed by the next run without a source "
            "change (ledger needs_rewrite, shared-module bootstrap or changed "
            "degraded-parse files), which "
            "re-evaluates their entity prune: " + ", ".join(reparse[:10])
            + (f", ... {len(reparse) - 10} more" if len(reparse) > 10 else "")
        )
    if plan.mode == RUN_SKIP:
        if kinds:
            entry["unapplied_changes"] = {k: sorted(v) for k, v in sorted(kinds.items())}
            attention.append(
                f"the scan differs from the ledger at the unchanged HEAD ({len(changed)} "
                "change(s)); the next run skips this repo, only index-repo --full "
                "applies them"
            )
    elif observation.lifecycle_on:
        lc = _commit_lifecycle(
            bound, writer, repo,
            scan=scan, rows=observation.rows, transitions=observation.transitions,
            gates=observation.gates, current_head=current_head,
            diff_base=plan.diff_base, owning_profile=owning_profile, retire=True,
            written=set(rewrite_names) & present, rewrite_names=rewrite_names,
            attention=attention,
        )
        entry["pending"] = list(lc["pending"])
        deferred_head = lc["presence_deferred_head"]
        _sim_advance_head(conn, repo["id"], current_head)
    scan_attention = set(scan.attention)
    entry["attention"] = [a for a in attention if a not in scan_attention]
    return entry, deferred_head


def _pending_evidence(rows: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        commit = None
        if r.get("removing_commit_sha"):
            date = r.get("removing_commit_date")
            commit = {
                "sha": r["removing_commit_sha"],
                "date": date.isoformat() if hasattr(date, "isoformat") else date,
                "subject": r.get("removing_commit_subject"),
            }
        successor = None
        if r.get("successor_names"):
            successor = {
                "names": list(r["successor_names"]), "source": r.get("successor_source"),
            }
        out.setdefault(r["name"], []).append({
            "repo_id": r.get("repo_id"),
            "repo": r.get("repo_basename"),
            "profile": r.get("profile_name"),
            "path": r.get("path"),
            "removing_commit": commit,
            "successor": successor,
            "blocked_by": r.get("retire_blocked_by"),
        })
    return out


def _audit_version(
    version: str, *, conn, store, writer: ReadOnlyWriter,
    advance: dict[int, str], entries_by_id: dict[int, dict],
) -> dict:
    report = reconcile_version(
        version, writer=writer, run_started_at=None, store=store,
        advance_presence=advance, global_gc=False, sweep=True, dry_run=True, conn=conn,
    )
    evidence = _pending_evidence(store.pending_retirements(version, conn=conn))

    other_pending: list[dict] = []

    def _place(bucket: str, name: str, extra: dict) -> None:
        # A pending row blocked by its repo's gate (or --no-retire) is only
        # re-evaluated by that repo's next scan; the reconcile decided the
        # name over the other (eligible) rows.
        held = bucket == "blocked"
        for ev in evidence.get(name, []):
            if reconcile_may_retry(ev["blocked_by"]) == held:
                continue
            item = {"name": name, **ev, **extra}
            entry = entries_by_id.get(ev["repo_id"])
            if entry is not None:
                entry[bucket].append(item)
            else:
                other_pending.append({"outcome": bucket, **item})

    for name in report.retired:
        _place("would_retire", name, {})
    for name in report.owner_dropped:
        _place("would_drop_owner", name, {"kept_by": report.owners_kept.get(name, [])})
    for name in sorted(report.undecidable):
        _place("undecidable", name, {"waits_for": report.undecidable[name]})
    for name in sorted(report.blocked):
        _place("blocked", name, {"reason": report.blocked[name]})

    advanced = set(report.presence_advanced)
    unsynced = []
    for r in store.repo_sync_state(version, conn=conn):
        if r["synced"] or r["repo_id"] in advanced:
            continue
        local = r.get("local_path")
        if not local or not Path(local).is_dir():
            why = "no checkout"
        elif r.get("presence_head_sha") is None:
            why = "never synced"
        else:
            why = "ledger behind HEAD"
        unsynced.append({
            "repo_id": r["repo_id"], "repo": r["repo_basename"],
            "profile": r["profile_name"], "why": why,
        })

    orphans = sorted(set(report.orphan_candidates) | set(report.orphans_deferred))
    return {
        "odoo_version": version,
        "would_retire": report.retired,
        "would_drop_owner": report.owner_dropped,
        "would_drop_excluded_owner": {
            k: {"excluded_by": report.excluded_owner_dropped[k],
                "kept_by": report.owners_kept.get(k, [])}
            for k in sorted(report.excluded_owner_dropped)
        },
        "undecidable": {k: report.undecidable[k] for k in sorted(report.undecidable)},
        "blocked": {k: report.blocked[k] for k in sorted(report.blocked)},
        "other_pending": other_pending,
        "orphan_modules": [
            {
                "name": name,
                "deferred_for": report.orphans_deferred.get(name, []),
                "evidence": report.orphan_evidence.get(name),
            }
            for name in orphans
        ],
        "child_orphans": report.child_orphan_candidates,
        "embedding_orphans": [
            {"module": m, "profile": p, "rows": n} for m, p, n in report.embedding_orphans
        ],
        "gates_tripped": report.gates_tripped,
        "orphans_unparseable": report.orphans_unparseable,
        "prune_rewrites": report.prune_rewrites,
        "shared_prunes": [
            {"name": name, "outcome": "would_prune", **report.shared_pruned[name]}
            for name in sorted(report.shared_pruned)
        ] + [
            {"name": name, "outcome": "held", **report.shared_prune_held[name]}
            for name in sorted(report.shared_prune_held)
        ],
        "shared_prune_waiting": {
            k: report.shared_prune_waiting[k] for k in sorted(report.shared_prune_waiting)
        },
        "excluded_owner_waiting": {
            k: report.excluded_owner_waiting[k]
            for k in sorted(report.excluded_owner_waiting)
        },
        "shared_prune_rewrites": {
            k: report.shared_prune_rewrites[k] for k in sorted(report.shared_prune_rewrites)
        },
        "unsynced_repos": unsynced,
        "modules_without_profile": writer.modules_without_profile(version),
        "errors": {k: report.errors[k] for k in sorted(report.errors)},
    }


def _findings(repos: list[dict], versions: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(FINDING_KEYS, 0)
    for v in versions:
        if "error" in v:
            counts["errors"] += 1
            continue
        counts["would_retire"] += len(v["would_retire"])
        counts["would_drop_owner"] += len(v["would_drop_owner"])
        counts["would_drop_owner"] += len(v.get("would_drop_excluded_owner") or {})
        counts["undecidable"] += len(v["undecidable"])
        counts["blocked"] += len(v["blocked"])
        counts["shared_prunes"] += len(v["shared_prunes"])
        counts["orphan_modules"] += len(v["orphan_modules"])
        counts["child_orphans"] += len(v["child_orphans"])
        counts["embedding_orphans"] += len(v["embedding_orphans"])
        counts["modules_without_profile"] += len(v["modules_without_profile"])
        counts["errors"] += len(v["errors"])
    for r in repos:
        counts["would_rewrite"] += len(r["would_rewrite"])
        counts["held_prunes"] += len(r["held_prunes"])
        counts["wrong_paths"] += len(r["wrong_paths"])
        counts["unapplied_changes"] += sum(len(n) for n in r["unapplied_changes"].values())
        counts["unparseable_kept"] += len(r.get("unparseable_kept") or [])
        counts["errors"] += 1 if r["error"] else 0
    return counts


def audit_lifecycle(
    repos: list[dict],
    *,
    writer,
    conn,
    version: str | None = None,
    scope: dict | None = None,
) -> dict:
    """Audit *repos* (dicts as ``get_repos_for_profile`` returns them, plus
    ``profile_name``) against the graph behind *writer* and the session copy of
    the ledger on *conn* (see :func:`open_simulation`). Writes nothing.

    *version* narrows the audit to repos keyed at that Odoo version (version
    rule: standard branch, then profile version) and to that version's
    reconcile. Returns the report dict (schema ``AUDIT_SCHEMA``).
    """
    started = time.monotonic()
    ro = writer if isinstance(writer, ReadOnlyWriter) else ReadOnlyWriter(writer)
    store = _SimulationStore()

    selected: list[dict] = []
    for repo in sorted(repos, key=lambda r: (r.get("profile_name") or "", r["id"])):
        if version is not None:
            local_path = repo.get("local_path") or ""
            key = repo.get("odoo_version")
            if local_path and Path(local_path).is_dir():
                key = resolve_repo_version(
                    local_path, branch=repo.get("branch"),
                    profile_version=repo.get("odoo_version"),
                ).odoo_version or key
            if key != version:
                continue
        selected.append(repo)

    entries: list[dict] = []
    advance: dict[str, dict[int, str]] = {}
    versions: set[str] = set()
    for repo in selected:
        try:
            entry, deferred = _audit_repo(repo, conn=conn, store=store, writer=ro)
        except Exception as exc:  # noqa: BLE001 - one repo never aborts the audit
            _logger.exception("lifecycle-audit: repo id=%s failed", repo.get("id"))
            entry, deferred = _new_repo_entry(repo), None
            entry["error"] = _error_text(exc)
        entries.append(entry)
        for v in (entry["odoo_version"], repo.get("odoo_version")):
            if v and v != "unknown":
                versions.add(v)
        if deferred and entry["odoo_version"]:
            advance.setdefault(entry["odoo_version"], {})[repo["id"]] = deferred
    if version is not None:
        versions = {version}

    entries_by_id = {e["repo_id"]: e for e in entries}
    version_reports: list[dict] = []
    for v in sorted(versions, key=_version_key):
        try:
            version_reports.append(_audit_version(
                v, conn=conn, store=store, writer=ro,
                advance=advance.get(v, {}), entries_by_id=entries_by_id,
            ))
        except Exception as exc:  # noqa: BLE001 - one version never aborts the audit
            _logger.exception("lifecycle-audit: version %s failed", v)
            version_reports.append({"odoo_version": v, "error": _error_text(exc)})

    findings = _findings(entries, version_reports)
    return {
        "schema": AUDIT_SCHEMA,
        "dry_run": True,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - started, 2),
        "scope": scope or {},
        "repos": entries,
        "versions": version_reports,
        "findings": findings,
        "has_findings": any(findings.values()),
    }


def _version_key(v: str) -> tuple:
    parts = v.split(".")
    try:
        return (0, *(int(p) for p in parts))
    except ValueError:
        return (1, v)


def run_lifecycle_audit(
    *, profile: str | None = None, all_profiles: bool = False, version: str | None = None,
) -> dict:
    """Production entry of ``lifecycle-audit``: registered repos, real graph,
    session copy of the ledger. Raises ValueError for an unknown profile."""
    from src.constants import LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS
    from src.db.pg import repo_store
    from src.indexer.pipeline import _neo4j_creds, open_production_pg, production_pg_dsn
    from src.indexer.writer_neo4j import Neo4jWriter

    pg = open_production_pg()
    try:
        if all_profiles:
            names = [p["name"] for p in repo_store().list_profiles()]
        else:
            names = [profile]
        repos: list[dict] = []
        for name in names:
            for repo in repo_store().get_repos_for_profile(name):
                repos.append({**repo, "profile_name": name})
        if profile is not None and not repos:
            raise ValueError(f"no repos registered for profile {profile!r}")
    finally:
        pg.close()

    uri, user, password = _neo4j_creds()
    writer = Neo4jWriter(uri, user, password)
    writer.read_timeout_s = LIFECYCLE_AUDIT_QUERY_TIMEOUT_SECONDS
    conn = open_simulation(production_pg_dsn())
    try:
        return audit_lifecycle(
            repos, writer=writer, conn=conn, version=version,
            scope={"profile": profile, "all": all_profiles, "version": version},
        )
    finally:
        conn.close()
        writer.close()


# --- human output -------------------------------------------------------------

def _commit_text(commit: dict | None) -> str:
    if not commit:
        return "removing commit unknown"
    subject = commit.get("subject") or ""
    return f"removed by {str(commit['sha'])[:10]} {str(commit.get('date') or '')[:10]} {subject!r}"


def _successor_text(successor: dict | None) -> str:
    if not successor:
        return "no successor"
    return f"successor {', '.join(successor['names'])} [{successor['source']}]"


def _drift_text(item: dict) -> str:
    if item["reason"] == "path_drift":
        return (
            f"path drift [{item['kind']}]: indexed at {item['indexed_path']}, registry "
            f"winner {item['winner_path']}"
        )
    if item["reason"] == "repo_drift":
        return f"repo drift: node names repo {item['indexed_repo']}"
    return "no graph node for this repo's profile"


def render_text(report: dict) -> str:
    """Human-readable report (English, ASCII), one block per repo then per version."""
    lines: list[str] = []
    scope = report.get("scope") or {}
    target = "all profiles" if scope.get("all") else f"profile {scope.get('profile')}"
    lines.append(
        f"lifecycle-audit (dry run, nothing written): {target}, "
        f"version {scope.get('version') or 'all'}"
    )
    for r in report["repos"]:
        lines.append("")
        lines.append(
            f"repo id={r['repo_id']} {r['basename']} (profile {r['profile']}, branch "
            f"{r['branch']}, version {r['odoo_version'] or r['profile_version']}): "
            f"next run {r['next_run'] or 'unknown'}"
        )
        if r["error"]:
            lines.append(f"  error: {r['error']}")
            continue
        s = r["scan"]
        if s is not None:
            verdict = "complete" if s["complete"] else "INCOMPLETE"
            trust = "trusted" if s["trusted"] else "UNTRUSTED"
            lines.append(
                f"  scan: {verdict}, {trust}; {s['present']} present, {s['excluded']} "
                f"excluded, {s['shadowed']} shadowed, {s['untracked_manifests']} "
                f"untracked manifest(s), {s['missing_manifests']} missing"
            )
        for text in r["scan_attention"]:
            lines.append(f"  version rule / scan: {text}")
        for text in r["attention"]:
            lines.append(f"  attention: {text}")
        g = r["gates"]
        if g is not None:
            if g["tripped"]:
                lines.append(
                    f"  gates: TRIPPED {', '.join(g['tripped'])} ({g['n_soft_drop']} of "
                    f"{g['n_present_before']} present would drop)"
                )
            else:
                lines.append("  gates: pass")
        for item in r["would_retire"]:
            lines.append(
                f"  would retire: {item['name']} ({_commit_text(item['removing_commit'])}; "
                f"{_successor_text(item['successor'])})"
            )
        for item in r["would_drop_owner"]:
            lines.append(
                f"  would drop owner: {item['name']} (kept by {', '.join(item['kept_by'])})"
            )
        for item in r["undecidable"]:
            lines.append(
                f"  undecidable: {item['name']} (waits for {', '.join(item['waits_for'])})"
            )
        for item in r["blocked"]:
            lines.append(f"  blocked: {item['name']} ({item['reason']})")
        for item in r["held_prunes"]:
            lines.append(
                f"  entity prune held: {item['name']}@{item['odoo_version']} "
                f"({item['stale']} of {item['total']} node(s) and {item['rels_stale']} of "
                f"{item['rels_total']} relationship(s) no longer produced by its parse; "
                "check the parse, then index with --allow-mass-retire)"
            )
        if r.get("unparseable_kept"):
            lines.append(
                "  manifest does not parse, module kept until it does: "
                + ", ".join(r["unparseable_kept"])
            )
        backlog = r.get("shared_parse_backlog") or {}
        if backlog.get("remaining"):
            lines.append(
                f"  shared-module bootstrap: {backlog['remaining']} shared module(s) without "
                f"a complete-parse record; the next run re-parses "
                f"{len(backlog['next_run'])}: {', '.join(backlog['next_run'][:10])}"
            )
        for kind, names in r["unapplied_changes"].items():
            lines.append(f"  unapplied at unchanged HEAD ({kind}): {', '.join(names)}")
        for label, items in (
            ("would rewrite", r["would_rewrite"]),
            ("not healed at unchanged HEAD", r["wrong_paths"]),
        ):
            for item in items:
                lines.append(f"  {label}: {item['name']} ({_drift_text(item)})")
    for v in report["versions"]:
        lines.append("")
        lines.append(f"version {v['odoo_version']}")
        if "error" in v:
            lines.append(f"  error: {v['error']}")
            continue
        for item in v["other_pending"]:
            lines.append(
                f"  pending in repo outside the audit ({item['repo'] or 'deleted repo'}): "
                f"{item['name']} -> {item['outcome']}"
            )
        for gate in v["gates_tripped"]:
            lines.append(f"  gate tripped: {gate}")
        for name, item in (v.get("would_drop_excluded_owner") or {}).items():
            lines.append(
                f"  would drop excluding owner(s) of {name}: {', '.join(item['excluded_by'])} "
                f"(kept by {', '.join(item['kept_by'])})"
            )
        for item in v.get("shared_prunes", []):
            verdict = (
                "would prune" if item["outcome"] == "would_prune"
                else "HELD (mass drop; index with --allow-mass-retire)"
            )
            lines.append(
                f"  shared module entity prune {verdict}: {item['name']} ({item['stale']} "
                f"of {item['total']} node(s) and {item['rels_stale']} of "
                f"{item['rels_total']} relationship(s)"
                + (
                    f" and {item['embeddings_stale']} embedding row(s)"
                    if item.get("embeddings_stale") is not None else ""
                )
                + f" produced by no present owner's latest parse; owners "
                f"{', '.join(item['owners'])})"
            )
        for name, owners in (v.get("shared_prune_rewrites") or {}).items():
            lines.append(
                f"  shared module re-parsed next run to decide what no owner's latest parse "
                f"accounts for: {name} (by {', '.join(owners)})"
            )
        for name, waits in (v.get("shared_prune_waiting") or {}).items():
            lines.append(
                f"  shared module entity prune waits: {name} (for {', '.join(waits)})"
            )
        for name, waits in (v.get("excluded_owner_waiting") or {}).items():
            lines.append(
                f"  excluding co-owner drop waits: {name} (for {', '.join(waits)})"
            )
        if v.get("prune_rewrites"):
            lines.append(
                "  deferred entity prune now decidable (owner re-parses and prunes): "
                + ", ".join(v["prune_rewrites"])
            )
        for item in v["orphan_modules"]:
            if item["deferred_for"]:
                how = f"kept until profile(s) {', '.join(item['deferred_for'])} sync"
            else:
                how = "would be swept"
            ev = item["evidence"]
            proof = (
                f"; {_commit_text(ev['removing_commit'])}; {_successor_text(ev['successor'])}"
                if ev else ""
            )
            lines.append(f"  orphan module: {item['name']} ({how}{proof})")
        if v["child_orphans"]:
            lines.append(f"  module-less children of: {', '.join(v['child_orphans'])}")
        for item in v["embedding_orphans"]:
            lines.append(
                f"  embedding orphans: {item['module']} / {item['profile']} ({item['rows']} rows)"
            )
        for item in v["modules_without_profile"]:
            lines.append(
                f"  Module without profile (reads as not indexed): {item['name']} at "
                f"{item['path']} (repo {item['repo']}, {item['children']} children)"
            )
        for item in v["unsynced_repos"]:
            lines.append(
                f"  unsynced after the run: {item['repo']} (repo id={item['repo_id']}, "
                f"profile {item['profile']}): {item['why']}"
            )
        for key, text in v["errors"].items():
            lines.append(f"  error: {key}: {text}")
    lines.append("")
    f = report["findings"]
    listed = ", ".join(f"{k}={n}" for k, n in f.items() if n)
    lines.append(f"findings: {listed or 'none'} ({report['duration_s']}s)")
    return "\n".join(lines)
