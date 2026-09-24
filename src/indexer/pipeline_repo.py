# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/pipeline_repo.py
"""Per-repo indexing stage (B6 split from pipeline.py).

Houses the per-repo scan -> ledger -> parse -> write -> embed unit that
``index_profile`` drives (sequentially or via its ThreadPoolExecutor worker):

    _owning_profiles(repo, profile_name, repo_root_name) -> list[str]
    _index_repo(repo, writer, ...) -> per-repo counter dict

The per-repo path OBSERVES module lifecycle (ADR-0056: ledger rows, pending
retirements, presence stamp) but never deletes a module; deletion happens only
in ``reconcile.reconcile_version``. The orchestrator (``index_profile`` /
``index_all`` / ``index_core``), the lock infrastructure and the production
connection helpers stay in ``pipeline.py``. ``pipeline.py`` re-exports
``_owning_profiles`` and ``_index_repo`` at the bottom of its body so existing
call sites and test patch targets (``src.indexer.pipeline._index_repo`` /
``_owning_profiles``) keep working.

Patch-visibility contract (why some names are referenced through ``pipeline``):
The test suite monkeypatches several collaborators on the *parent* module
namespace and then calls ``_index_repo`` — e.g.
``patch("src.indexer.pipeline.build_registry_scan", ...)``,
``...topological_sort``, ``...repo_store``, ``..._presence_store``. These are
*function* bindings: a ``from ... import`` binding in THIS module would NOT see
a patch applied to the ``pipeline`` namespace. So ``_index_repo`` resolves them
through the ``pipeline`` module object at call time (a deferred,
cold-import-safe ``from . import pipeline``). By contrast ``_incremental`` and
the ``parser_*`` submodules are *module objects* shared by identity across both
namespaces, so patching ``pipeline.parser_python.parse_module`` is visible here
regardless of the binding path - those stay as ordinary module-level imports.
"""
import contextlib
import functools
import hashlib
import logging
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src import constants as _constants
from src.indexer import incremental as _incremental
from src.indexer import (
    parse_health,
    parser_assets,
    parser_css,
    parser_js,
    parser_js_test,
    parser_less,
    parser_python,
    parser_qweb,
    parser_scss,
    parser_test,
    parser_xml,
)
from src.indexer.models import (
    EXCLUSION_UNPARSEABLE,
    AssetParseResult,
    StylesheetInfo,
    ViewParseResult,
)
from src.indexer.protocols import IndexWriterProtocol
from src.indexer.version_registry import less_active, scss_active

# Log under the parent "src.indexer.pipeline" name (NOT __name__) so every
# per-repo, admin-facing log line (the M7 C5 "Indexer run" summary, the W2-4
# incremental "%d/%d modules changed" line, GC lines) stays on the SAME logger it
# was emitted from before the B6 split. Operators (and tests) that scope log
# filters to "src.indexer.pipeline" keep seeing them — the split is observability-
# transparent.
_logger = logging.getLogger("src.indexer.pipeline")


def _owning_profiles(
    repo: dict,
    profile_name: str | None,
    repo_root_name: str,
) -> list[str]:
    """Return the single-element ``profile[]`` to stamp on every node from *repo*.

    ADR-0034 single-owner provenance (supersedes the ADR-0016 Option-Y "stamp the
    full ancestor chain" behaviour for the WRITE-time provenance array):

    A node's ``profile[]`` must reflect the profile that OWNS the repo the node
    physically came from — NOT the descendant profile the indexer happens to be
    running under. ``index_profile`` indexes only the repos *directly registered*
    under ``profile_name`` (``get_repos_for_profile`` joins ``r.profile_id =
    p.id``), so the owning profile of every repo in a run is exactly
    ``profile_name``. The repo row may also carry its own ``profile_name`` column
    (e.g. ``get_ancestor_repos``); prefer that when present so the helper is
    correct even if a future caller mixes repos from several profiles.

    Why single-owner (not the ancestor chain): inheritance is a READ-time concept
    resolved through the ``$own``/``$shared`` scope arrays at the ADR-0034 choke,
    NOT a write-time provenance concept. Stamping the descendant chain unions
    tenant-private profile names onto shared-core nodes (e.g. ``base`` gaining
    ``viindoo_internal_17``), which the ``all()`` choke then correctly DENIES to
    callers not allowed on every one of those names — hiding shared core modules.
    Stamping only the owning profile makes Neo4j's array predicate structurally
    equivalent to pgvector's already-secure single-scalar ``profile_name``
    membership (``write_module_embeddings`` already stamps the leaf), closing the
    Neo4j↔pgvector split-brain by construction.

    F-6 guard: the result is ALWAYS a non-empty single-element list. An empty
    ``profile=[]`` would make the choke's ``all(__p IN [] ...)`` vacuously TRUE
    (fail-OPEN). Falls back to ``repo_root_name`` only when neither the repo's own
    ``profile_name`` nor the run ``profile_name`` is available (direct callers /
    unit tests / CLI without a profile).

    F2: a FALSY owner (all three candidates empty/``None`` — e.g.
    ``Path('/').name == ''``) is a hard error, never an empty/``['']`` stamp. A
    ``['']`` array is *truthy* so the downstream ``if not _profiles_arr`` guard
    would miss it, and the ADR-0034 ``all()`` choke would then deny that node to
    every scoped tenant (a silent fail-closed black hole). Raise so the run fails
    loudly instead of writing un-servable nodes.
    """
    owner = repo.get("profile_name") or profile_name or repo_root_name
    if not owner:
        raise ValueError(
            "_owning_profiles: cannot determine an owning profile for repo "
            f"{repo.get('url', repo.get('local_path', '<unknown>'))!r} — "
            "all of repo['profile_name'], profile_name, and repo_root_name are "
            "empty. Every indexed node MUST carry a real owning profile name "
            "(an empty owner becomes a fail-closed black hole at the ADR-0034 "
            "choke). Pass a profile_name or ensure local_path has a basename."
        )
    return [owner]


def refresh_before_scan(repo: dict, pg_conn: object | None = None) -> None:
    """Fetch + reset the repo's local clone to its upstream branch tip.

    ROOT CAUSE this fixes (nightly-fetch): the incremental check in ``_index_repo``
    only reads the LOCAL clone (``git rev-parse HEAD`` / ``merge-base`` / ``diff``).
    The nightly reindex cron never ran ``git fetch``, so when an upstream branch
    advanced (a merged PR), local HEAD still equalled ``repos.head_sha`` and the
    repo was skipped - upstream merges were structurally invisible to the cron.
    Running a fetch + ``reset --hard origin/<branch>`` FIRST advances local HEAD to
    the real remote tip so the existing incremental diff (and force-push /
    is_ancestor handling) compose naturally on top of it.

    Reuses ``src.git_utils.refresh_repo`` (ADR-0035 SSH hardening: GIT_SSH_COMMAND,
    pinned known_hosts, StrictHostKeyChecking=yes, per-call 0o600 tempfile key) -
    no git/SSH logic is re-implemented here.

    Serialization: the mutating fetch/reset runs UNDER the per-repo Postgres
    advisory lock ``_repo_git_lock(pg_conn, repo_id)`` - the SAME lock the on-demand
    cloner uses (ADR-0035 D2) - so a scheduled fetch and a concurrent ``clone-all``
    for the same repo serialize instead of racing on ``.git/index.lock``. The lock
    is skipped only when ``pg_conn`` is None; only non-DB / unit-test callers hit
    that path - all production callers (cron, web routes) pass a pg connection, so
    the lock is always held in production.

    SSH key resolution shares ONE helper with the cloner:
    ``src.ssh_key_resolve.resolve_ssh_key_pem`` (decides by URL scheme, decrypts
    via the SSOT ``src.crypto.decrypt_private_key``). HTTPS repo -> None. SSH repo
    with no usable key -> ``SshKeyUnavailable`` is SURFACED (WARNING + skip fetch,
    index on-disk) rather than running a doomed keyless SSH fetch that would just
    fail auth and leave the clone stale forever. Decrypt / DB errors (e.g. missing
    FERNET_KEY) propagate out of the helper and land on the WARNING fail-safe path
    below - they are resolved BEFORE the lock, so they can NEVER be misread as
    benign lock contention.

    FAIL-SAFE: any git/SSH/network error (``CalledProcessError``,
    ``TimeoutExpired``, ``FileNotFoundError``, or any other unexpected error) is
    caught, logged as a WARNING, and swallowed - the caller then indexes whatever is
    on disk. Network reachability must NEVER become a hard dependency of the nightly
    job: a fetch failure must not abort the profile's reindex.

    Benign lock contention: if a concurrent ``clone-all`` already holds the per-repo
    advisory lock, ``_repo_git_lock`` raises ``RuntimeError`` at acquisition. That is
    an EXPECTED, non-error case (the other worker is refreshing the same repo), so it
    is logged at INFO - NOT WARNING - and indexing proceeds on the on-disk state.
    Because the key is resolved BEFORE the lock and ``refresh_repo`` never raises
    ``RuntimeError`` (it raises CalledProcessError/TimeoutExpired/FileNotFoundError),
    the ONLY RuntimeError reachable inside the ``with`` block is the lock acquisition
    -> the RuntimeError branch below unambiguously means contention.

    Self-healing note: a partial refresh (fetch OK but reset fails) needs no special
    handling - ``refresh_repo`` re-runs the reset after every fetch, so the next run
    completes it. A reset that keeps failing is an ops/timeout issue, not a
    silent-skip logic bug.
    """
    # resolve_ssh_key_pem is a leaf SSOT (is_ssh_url + auth_store + decrypt_private_key,
    # none of them src.web_ui) so this indexer-layer module honours the one-way
    # pipeline rule (src/indexer must not import src.web_ui). Deferred imports keep
    # module load cheap + avoid pulling crypto/DB deps when refresh is off.
    from src.git_utils import refresh_repo
    from src.indexer import pipeline as _pipeline
    from src.ssh_key_resolve import SshKeyUnavailable, resolve_ssh_key_pem

    local_path = Path(repo["local_path"])
    url = repo.get("url", str(local_path))
    branch: str | None = repo.get("branch")
    if not branch:
        _logger.warning(
            "Repo %s has no branch recorded - skipping pre-scan fetch "
            "(cannot reset --hard origin/<branch> without a branch name)",
            url,
        )
        return

    # Resolve the SSH key BEFORE acquiring the lock. This keeps the ONLY RuntimeError
    # that can fire inside the `with` block below the lock-acquisition one (contention).
    try:
        private_key_pem: bytes | None = resolve_ssh_key_pem(repo)
    except SshKeyUnavailable as exc:
        # SSH URL but no usable key. Surface it (WARNING) and skip - do NOT run a
        # keyless SSH fetch that would fail auth and leave the clone stale silently.
        _logger.warning(
            "Repo %s: %s - skipping pre-scan fetch, indexing on-disk state",
            url, exc,
        )
        return
    except Exception as exc:  # noqa: BLE001 - decrypt/DB error (e.g. FERNET_KEY absent)
        # A genuine config/crypto failure resolving the key. Non-fatal for the
        # nightly job, but a real error -> WARNING (not the INFO contention line).
        _logger.warning(
            "Repo %s: pre-scan fetch failed resolving SSH key (%s: %s) - "
            "indexing on-disk state instead",
            url, type(exc).__name__, exc,
        )
        return

    # ADR-0035 D2: serialize the mutating fetch/reset behind the per-repo advisory
    # lock (same lock id the cloner uses) so a scheduled fetch and an on-demand
    # clone-all for the same repo never race on .git/index.lock. nullcontext keeps
    # ONE refresh_repo call site for both the locked (pg_conn) and unlocked paths.
    lock_cm = (
        _pipeline._repo_git_lock(pg_conn, repo["id"])
        if pg_conn is not None
        else contextlib.nullcontext()
    )
    try:
        with lock_cm:
            refresh_repo(local_path, branch, private_key_pem=private_key_pem)
    except RuntimeError:
        # _repo_git_lock raises RuntimeError ONLY at acquisition (contention);
        # refresh_repo raises CalledProcessError/TimeoutExpired/FileNotFoundError,
        # never RuntimeError. So a RuntimeError here == a concurrent clone-all holds
        # the lock. Benign, EXPECTED - INFO (not WARNING) so operators are not
        # alarmed; index the on-disk state.
        _logger.info(
            "Repo %s: another git op in progress for repo %s, skipping pre-scan "
            "fetch; indexing on-disk state",
            url, repo.get("id"),
        )
        return
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        # Expected git failures: network blip, revoked/rejected key, unreachable
        # host, missing/damaged clone. Non-fatal - index on-disk state.
        _logger.warning(
            "Repo %s: pre-scan fetch failed (%s: %s) - indexing on-disk state instead",
            url, type(exc).__name__, exc,
        )
        return
    except Exception as exc:  # noqa: BLE001 - defensive: any git error is non-fatal
        # Any other unexpected error (e.g. OSError from the subprocess machinery)
        # must ALSO not abort the profile reindex.
        _logger.warning(
            "Repo %s: pre-scan fetch failed unexpectedly (%s: %s) - "
            "indexing on-disk state instead",
            url, type(exc).__name__, exc,
        )
        return

    _logger.info(
        "Repo %s: pre-scan refresh (fetch + reset --hard origin/%s) OK",
        url, branch,
    )




_EMPTY_COUNTERS = {
    "modules": 0,
    "views": 0,
    "qweb": 0,
    "embeddings": 0,
    "js_patches": 0,
    "owl_comps": 0,
}

# Test seam (ADR-0056 T09): when set, called with the repo dict after this
# repo's graph writes and BEFORE its ledger commit, so a test can interleave a
# concurrent reconcile deterministically. Always None in production.
_LIFECYCLE_TEST_BARRIER = None

_MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")


class _ConnBoundStore:
    """A ModulePresenceStore whose every call runs on one given connection."""

    def __init__(self, store, conn) -> None:
        self._store = store
        self._conn = conn

    def __getattr__(self, name: str):
        attr = getattr(self._store, name)
        if not callable(attr):
            return attr
        return functools.partial(attr, conn=self._conn)


def _manifest_file_for(scan, rel_dir: str) -> str:
    """File name of the manifest the scan used for the module directory *rel_dir*."""
    live = scan.finder_paths - scan.untracked
    for name in _MANIFEST_NAMES:
        if str(Path(rel_dir) / name) in live:
            return name
    return _MANIFEST_NAMES[0]


def _observed_modules(scan) -> list:
    """Map a RegistryScan onto the ledger's ObservedModule list (one per name)."""
    from src.db.module_presence import ObservedModule

    observed = []
    for name in sorted(scan.present_names()):
        info = scan.module(name)
        rel = info.relative_path(info.path)
        observed.append(ObservedModule(
            name=name,
            path=rel,
            manifest_file=_manifest_file_for(scan, rel),
            version_raw=info.version_raw or None,
            version_mismatch=info.version_mismatch,
            shadowed_paths=tuple(scan.shadowed.get(name, ())),
        ))
    for name in sorted(scan.excluded):
        if scan.module(name) is not None:
            continue
        ex = scan.excluded[name]
        observed.append(ObservedModule(
            name=name,
            path=ex.path,
            manifest_file=ex.manifest_file,
            state="excluded",
            exclusion_reason=ex.reason,
            version_raw=ex.version_raw or None,
            version_mismatch=ex.version_mismatch,
            shadowed_paths=tuple(scan.shadowed.get(name, ())),
        ))
    return observed


RUN_SKIP = "skip"
RUN_SYNC = "sync"
RUN_INCREMENTAL = "incremental"
RUN_FULL = "full"


@dataclass(frozen=True)
class RunPlan:
    """How ``_index_repo`` treats a repo this run (shared with ``lifecycle-audit``).

    ``mode``: ``skip`` (HEAD unchanged and the ledger reflects it), ``sync``
    (HEAD unchanged but the ledger is behind: scan + ledger, no re-parse),
    ``incremental`` (re-parse the modules changed since ``diff_base``) or
    ``full`` (re-parse everything: ``--full``, first index, force-push).
    """
    mode: str
    diff_base: str | None
    head_unchanged: bool
    force_push: bool


def plan_repo_run(
    repo_path: Path,
    current_head: str | None,
    last_head: str | None,
    presence_head: str | None,
    rewrite_names: list[str],
    *,
    full_reindex: bool,
    ledger: bool,
    degraded_changed: bool = False,
) -> RunPlan:
    """Decide skip / sync / incremental / full for one repo (ADR-0007 + ADR-0056).

    *ledger* False (no lifecycle ledger) keeps the pre-ledger rule: HEAD equal
    to ``repos.head_sha`` skips. *degraded_changed* (B14, ``_degraded_state``):
    the failing files of a degraded module changed on disk, so the repo is
    not skipped even at an unchanged HEAD (the sync path re-parses them).
    """
    head_unchanged = bool(current_head) and current_head == last_head
    if not full_reindex and head_unchanged and not degraded_changed and (
        not ledger or (presence_head == current_head and not rewrite_names)
    ):
        return RunPlan(RUN_SKIP, last_head, True, False)
    diff_base: str | None = None if full_reindex else last_head
    force_push = bool(
        diff_base and current_head and not head_unchanged
        and not _incremental.is_ancestor(repo_path, diff_base, current_head)
    )
    if force_push:
        diff_base = None
    if not full_reindex and head_unchanged:
        mode = RUN_SYNC
    elif diff_base is None:
        mode = RUN_FULL
    else:
        mode = RUN_INCREMENTAL
    return RunPlan(mode, diff_base, head_unchanged, force_push)


@dataclass
class LifecycleObservation:
    """The lifecycle verdict of one repo scan before anything is written.

    ``lifecycle_on``: the ledger is updated this run (a ledger, a git HEAD and
    a registered branch). ``rows`` are the repo's ledger rows (empty when
    lifecycle is off), ``transitions`` / ``gates`` the B5 classification and
    gate verdict, ``attention`` the operator messages collected so far
    (version rule, git trust, gate reasons).
    """
    trusted: bool
    lifecycle_on: bool
    rows: list[dict]
    transitions: object
    gates: object
    attention: list[str] = field(default_factory=list)
    # Indexed modules whose manifest does not parse, kept (lifecycle.unparseable_kept).
    unparseable_kept: list[str] = field(default_factory=list)


def _git_trust_attention(repo_path: Path, branch: str | None, tracked_available: bool) -> list[str]:
    """Operator messages about git tracking and the ``origin/<branch>`` ref."""
    from src.git_utils import remote_branch_ref_exists

    if not tracked_available:
        return [
            "git tracking unavailable (not a git work tree, no commit yet, or git "
            "refused the repository, e.g. safe.directory ownership): scan untrusted, "
            "no module retired"
        ]
    if branch and not remote_branch_ref_exists(repo_path, branch):
        return [f"no origin/{branch} ref: scan trust falls back to the checked-out branch name"]
    return []


def observe_lifecycle(
    repo: dict,
    scan,
    presence,
    current_head: str | None,
    *,
    allow_mass_retire: bool = False,
    writer: IndexWriterProtocol | None = None,
    owning_profile: str | None = None,
) -> LifecycleObservation:
    """Trust, classification and gates of one repo scan (ADR-0056, B5).

    Read-only: reads the repo's ledger rows through *presence* (None = no
    ledger), git and, through *writer*, the graph; writes nothing. The
    per-repo index path and the dry-run ``lifecycle-audit`` both decide
    through this function.

    While the ledger has never reflected the repo (``presence_head_sha``
    NULL: the first runs after the ledger was deployed, a new registration)
    G-B is evaluated against the graph baseline
    (``writer.repo_module_baseline`` for *owning_profile*) instead of the
    ledger rows, which cannot tell what the repo shipped before. The scan's
    unparseable names that have a graph node feed the ``manifest_unparseable``
    signal (``lifecycle.unparseable_kept``).
    """
    from src.git_utils import head_matches_remote_branch
    from src.indexer import lifecycle

    repo_path = Path(repo["local_path"])
    branch: str | None = repo.get("branch")
    attention: list[str] = list(scan.attention)
    trusted = head_matches_remote_branch(repo_path, branch)
    attention.extend(
        _git_trust_attention(repo_path, branch, scan.tracked_paths is not None)
    )
    lifecycle_on = presence is not None and bool(current_head) and bool(branch)
    if presence is not None and not lifecycle_on:
        attention.append(
            "lifecycle ledger not updated: "
            + ("no git HEAD" if not current_head else "no branch registered")
            + "; no module retired"
        )
    rows: list[dict] = presence.rows_for_repo(repo["id"]) if lifecycle_on else []
    transitions = lifecycle.classify(rows, scan)
    graph_baseline = None
    if (
        lifecycle_on and writer is not None
        and presence.presence_head_sha(repo["id"]) is None
    ):
        graph_baseline = writer.repo_module_baseline(
            repo.get("id"), repo_path.name,
            owning_profile or repo.get("profile_name") or "",
        )
    unparseable = sorted(
        n for n, ex in scan.excluded.items() if ex.reason == lifecycle.EXCLUSION_UNPARSEABLE
    )
    indexed_names: list[str] = []
    if lifecycle_on and writer is not None and unparseable:
        indexed_names = sorted(writer.module_identity(scan.odoo_version, unparseable))
    gates = lifecycle.apply_gates(
        transitions, scan, trusted=trusted, allow_mass_retire=allow_mass_retire,
        graph_baseline=graph_baseline, indexed_names=indexed_names,
    )
    attention.extend(gates.reasons)
    return LifecycleObservation(
        trusted=trusted, lifecycle_on=lifecycle_on, rows=rows,
        transitions=transitions, gates=gates, attention=attention,
        unparseable_kept=lifecycle.unparseable_kept(transitions, scan, indexed_names),
    )


REWRITE_NO_NODE = "no_node"
REWRITE_PATH_DRIFT = "path_drift"
REWRITE_REPO_DRIFT = "repo_drift"


def _repo_relative(path: str | None, local_path: str) -> str | None:
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(local_path))
        except ValueError:
            return str(p)
    return str(p)


def modules_needing_rewrite(
    writer: IndexWriterProtocol,
    presence,
    repo: dict,
    scan,
    owning_profile: str,
) -> dict[str, dict]:
    """Present modules whose graph node does not match the scan (self-heal, H2).

    ``{name: {reason, ...}}`` for every present module of the scan whose Module
    node, re-written by this repo, would differ from what it is now:

    - ``no_node``: no Module node carries this repo's owning profile (lost to a
      concurrent retire or a failed write; ``has_node`` True when a node exists
      without that profile, e.g. an empty profile list, F24).
    - ``path_drift``: the node was last written by this repo but its ``path`` is
      not the registry winner. ``kind``: ``shadowed`` (a same-name loser dir,
      the posbox ``point_of_sale`` stub, F15), ``untracked`` (a git-untracked
      copy such as ``.odoo-ai/...``, F7) or ``stale``; with ``indexed_path`` and
      ``winner_path`` (repo-relative).
    - ``repo_drift``: the node names another repo but only this repo's profile
      owns it and the ledger shows no other present owner (``indexed_repo``).

    Every index run that is not the unchanged skip re-writes these
    (``self_heal_rewrites``), so drift left by older code heals without
    ``--full``; the re-parse re-stamps the children and the entity prune drops
    those only the old path produced.
    Read-only (one Module lookup; one ledger read when *presence* is given).
    """
    local_path = repo["local_path"]
    basename = Path(local_path).name
    present = sorted(scan.present_names())
    if not present:
        return {}
    identity = writer.module_identity(scan.odoo_version, present)
    untracked_dirs = {str(Path(p).parent) for p in scan.untracked}
    owners = (
        presence.present_owner_basenames(scan.odoo_version, present)
        if presence is not None else {}
    )
    out: dict[str, dict] = {}
    for name in present:
        node = identity.get(name)
        if node is None or owning_profile not in node.get("profile", []):
            out[name] = {"reason": REWRITE_NO_NODE, "has_node": node is not None}
            continue
        ours = node.get("repo") == basename and (
            node.get("repo_id") is None or node["repo_id"] == repo["id"]
        )
        if not ours:
            if node.get("profile") == [owning_profile] and len(owners.get(name, [])) <= 1:
                out[name] = {"reason": REWRITE_REPO_DRIFT, "indexed_repo": node.get("repo")}
            continue
        info = scan.module(name)
        winner = info.relative_path(info.path)
        indexed = _repo_relative(node.get("path"), local_path)
        if not indexed or indexed == winner:
            continue
        if indexed in scan.shadowed.get(name, ()):
            kind = "shadowed"
        elif indexed in untracked_dirs:
            kind = "untracked"
        else:
            kind = "stale"
        out[name] = {
            "reason": REWRITE_PATH_DRIFT, "kind": kind,
            "indexed_path": indexed, "winner_path": winner,
        }
    return out


def self_heal_rewrites(
    writer: IndexWriterProtocol,
    presence,
    repo: dict,
    scan,
    owning_profile: str,
    *,
    plan: RunPlan,
    lifecycle_on: bool,
) -> dict[str, dict]:
    """The self-heal set of the planned run (``modules_needing_rewrite``), or {}.

    Computed for every mode but the unchanged skip, when the ledger is
    updated this run: on sync and incremental these modules are written in
    addition to the changed ones; on a full run they are written anyway, and
    the ``path_drift`` entries still hand their old directory to the entity
    prune. The index run and the dry-run audit both decide through this.
    """
    if plan.mode == RUN_SKIP or not lifecycle_on or not scan.present_names():
        return {}
    return modules_needing_rewrite(writer, presence, repo, scan, owning_profile)


def regular_write_names(repo_path: Path, scan, plan: RunPlan, current_head: str | None) -> set[str]:
    """Modules the planned run re-parses on its own, before ``needs_rewrite``,
    self-heal and degraded retries are added.

    Every present module on a full run, none on the sync path or the skip,
    the modules whose directory changed since ``plan.diff_base`` on an
    incremental run. Read-only (git diff).
    """
    if plan.mode == RUN_SKIP or plan.mode == RUN_SYNC:
        return set()
    if plan.diff_base is None:
        return set(scan.present_names())
    changed_rel_paths = _incremental.compute_changed_module_paths(
        repo_path, plan.diff_base, current_head,
    )
    # convert relative paths to absolute to match ModuleInfo.path
    changed_abs_paths = {str(repo_path / rel) for rel in changed_rel_paths}
    return {
        name
        for mods in scan.modules.values()
        for name in _incremental.filter_modules_by_changed(mods, changed_abs_paths)
    }


def _parse_and_write(
    modules_by_version: dict,
    *,
    writer: IndexWriterProtocol,
    repo: dict,
    repo_path: Path,
    rng_root: Path | None,
    pg_conn,
    embedder,
    progress: bool,
    profiles_arr: list[str],
    owning_profile: str,
    module_health: dict | None = None,
) -> tuple[dict, list]:
    """Parse the given modules and write every node + embedding they produce.

    Returns ``(counters, test_results)``; ``test_results`` feeds the test-node GC.
    *module_health*, when given, receives one
    :class:`~src.indexer.parse_health.ModuleParseHealth` per parsed module,
    keyed ``(version, name)`` - the input of the intra-module entity prune.
    """
    from src.indexer import pipeline as _pipeline

    py_results = []
    view_results: list[ViewParseResult] = []
    # WI-D: asset-bundle parse results (one per module; era-B v15+ populates,
    # era-A v8-14 yields empty since parser_qweb owns legacy <template> bundles).
    asset_results: list[AssetParseResult] = []
    js_graph_results = []
    # WI-1: test surface parse results (one per module)
    test_results = []
    # WI-3: JS test suites (JsTestSuiteInfo, collected per module)
    js_test_suites = []
    # CSS/SCSS (WI-A1, ADR-0025)
    all_stylesheet_infos: list[StylesheetInfo] = []

    total_modules = 0
    total_views = 0
    total_qweb = 0
    total_reports = 0
    total_asset_bundles = 0
    total_embeddings = 0
    total_js_patches = 0
    total_owl_comps = 0
    total_stylesheets = 0
    total_embed_calls = 0
    total_js_test_suites = 0

    # Pre-flight (once, not per module): the embeddings table is usable
    # (chunks_enabled), and new vectors can be computed (embed_enabled). With
    # --no-embed the chunk KEYS are still built: the entity prune deletes the
    # rows of entities the parse no longer produces, which needs no embedder.
    # Without an embedder the probe needs a real connection (callers that
    # pass a stand-in for the embed path never reach the embeddings table).
    chunks_enabled = pg_conn is not None and (
        embedder is not None or callable(getattr(pg_conn, "cursor", None))
    )
    if chunks_enabled:
        from src.db.migrate import _vector_extension_available
        chunks_enabled = _vector_extension_available(pg_conn)
    embed_enabled = chunks_enabled and embedder is not None
    if chunks_enabled:
        from src.indexer.writer_pgvector import make_chunks, write_module_embeddings

    for version, modules in modules_by_version.items():
        sorted_names = _pipeline.topological_sort(modules)

        # Try to import tqdm for progress bar; graceful fallback if not installed.
        try:
            from tqdm import tqdm as _tqdm
        except ImportError:
            _tqdm = None

        # Wrap iteration with tqdm if progress enabled, tqdm available, and stdout is a TTY.
        iterable = sorted_names
        if progress and _tqdm is not None and sys.stdout.isatty():
            iterable = _tqdm(sorted_names, desc=f"[{version}]", unit="mod", leave=True)

        for mod_name in iterable:
            # Parse completeness (ADR-0056 B14): parsers report every file they
            # could not read or parse into this module's health record.
            with parse_health.track(mod_name, version) as health:
                info = modules[mod_name]
                total_modules += 1

                # Python models
                py_result = parser_python.parse_module(info)
                py_results.append(py_result)

                # WI-1: test surface extraction (era-gated internally by parse_module)
                test_result = parser_test.parse_module(info)
                test_results.append(test_result)

                # WI-3: JS frontend test extraction (Hoot/QUnit/tour from static/tests/)
                js_suites = parser_js_test.parse_module_js_tests(info)
                js_test_suites.extend(js_suites)
                total_js_test_suites += len(js_suites)

                # XML views (ir.ui.view records) - rng_root enables version-exact
                # RelaxNG validation; None when no Odoo source RNG dir is available.
                xml_result = parser_xml.parse_module(info, rng_root=rng_root)
                total_views += len(xml_result.views)
                total_reports += len(xml_result.reports)

                # QWeb templates
                qweb_result = parser_qweb.parse_module(info)
                total_qweb += len(qweb_result.qweb)

                # WI-D asset bundles (ADR-0052): version-aware dispatch. Era B (v15+)
                # parses the __manifest__.py 'assets' dict; era A (v8-14) returns empty
                # (legacy XML <template> bundles already captured by parser_qweb above).
                asset_result = parser_assets.parse_assets(info)
                asset_results.append(asset_result)
                total_asset_bundles += len(asset_result.contributions)

                # Merge both view parsers into one ViewParseResult per module.
                # writer.write_view_results handles both .views and .qweb in one call.
                # lint_violations from xml_result (RelaxNG v15+) are preserved.
                merged = ViewParseResult(
                    module=info,
                    views=xml_result.views,
                    qweb=qweb_result.qweb,
                    # GAP-2/GAP-5: report actions parsed alongside views in parser_xml.
                    # Written by write_view_results AFTER models (write_results) and
                    # templates (this same qweb pass) exist, so REPORTS_ON/USES_TEMPLATE
                    # resolve. write order in _index_repo: write_results -> ... ->
                    # write_view_results, and within _write_view_parse_result the qweb
                    # loop runs before the report loop.
                    reports=xml_result.reports,
                    lint_violations=xml_result.lint_violations,
                )
                view_results.append(merged)

                # JS graph extraction - patches and OWL components
                js_graph = parser_js.parse_module_graph(info)
                js_graph_results.append(js_graph)
                total_js_patches += len(js_graph.patches)
                total_owl_comps += len(js_graph.components)

                # CSS/SCSS/LESS parsing - stylesheet nodes + embeddings (WI-A1, ADR-0025; RP WI-3)
                # Era gate (osm-audit-views GAP-3): LESS is the v9-v11 stylesheet
                # language, SCSS is v12+. Plain CSS spans every era (always parsed).
                # Gating off-era parsers is harmless (they no-op without files) but
                # enforces + documents the boundary via the version registry (ADR-0032).
                css_chunks_mod, css_infos = parser_css.parse_module(info)
                if scss_active(version):
                    scss_chunks_mod, scss_infos = parser_scss.parse_module(info)
                else:
                    scss_chunks_mod, scss_infos = [], []
                if less_active(version):
                    less_chunks_mod, less_infos = parser_less.parse_module(info)
                else:
                    less_chunks_mod, less_infos = [], []
                all_stylesheet_infos.extend(css_infos)
                all_stylesheet_infos.extend(scss_infos)
                all_stylesheet_infos.extend(less_infos)
                total_stylesheets += len(css_infos) + len(scss_infos) + len(less_infos)

                # Semantic embeddings - skipped when pg_conn is absent, the pgvector
                # extension is not installed, or the version could not be resolved;
                # without an embedder only the chunk keys are kept (see above).
                if chunks_enabled and version != "unknown":
                    from src.indexer.writer_pgvector import (  # noqa: PLC0415
                        make_css_chunks,
                        make_less_chunks,
                        make_scss_chunks,
                    )
                    js_chunks = parser_js.parse_module(info)
                    chunks = make_chunks(mod_name, version, py_result, merged, js_chunks)
                    # Append CSS, SCSS, and LESS embedding chunks.
                    # Pass `info` (ModuleInfo) so chunks carry repo/repo_id provenance
                    # and file_path is relativized to repo root (ADR-0037, WS-C).
                    chunks.extend(make_css_chunks(css_chunks_mod, info))
                    chunks.extend(make_scss_chunks(scss_chunks_mod, info))
                    chunks.extend(make_less_chunks(less_chunks_mod, info))
                    # WI-1/WI-3 (C2): append test + JS-test chunks so find_test_examples
                    # (AC5) has test_method/test_class/js_test chunks to retrieve. Without
                    # these the test-chunk makers exist but are never called -> the tool
                    # returns nothing. test_result / js_suites are in scope from this loop.
                    from src.indexer.writer_pgvector import (  # noqa: PLC0415
                        make_js_test_chunks,
                        make_test_chunks,
                    )
                    chunks.extend(make_test_chunks(mod_name, version, test_result))
                    chunks.extend(make_js_test_chunks(
                        js_suites, mod_name, version,
                        repo=info.repo, repo_id=info.repo_id,
                    ))
                    # F4: pgvector stamps the SAME single owning profile as Neo4j
                    # (owning_profile == profiles_arr[0]), not the run profile_name
                    # directly - single source of truth, no split-brain.
                    # Upsert only (B14): the rows of chunks this parse did not
                    # produce are deleted by the entity prune once it decided
                    # the module may lose them, so embeddings and graph agree.
                    health.embedded_keys = {
                        (c.chunk_type, c.entity_name, c.file_path, c.chunk_idx)
                        for c in chunks
                    }
                    if embed_enabled:
                        embed_calls = write_module_embeddings(
                            mod_name, version, chunks, embedder,
                            profile_name=owning_profile, replace=False,
                        )
                        health.embeddings_written = True
                        total_embeddings += len(chunks)
                        total_embed_calls += embed_calls
            if module_health is not None:
                module_health[(version, mod_name)] = health

    # ADR-0034 single-owner provenance (supersedes ADR-0016 Option-Y full-chain
    # stamping for the WRITE-time provenance array): stamp every node with the
    # OWNING profile of THIS repo — never the descendant ancestor chain. Foreign
    # tenant-private names accumulated onto shared-core nodes (`base`, `sale`, …)
    # would be hidden by the choke's all(). `profiles_arr` is the SAME list
    # computed once near the top of the function (F4 single source of truth) and
    # used for the pgvector write above, so the two stores cannot diverge. See
    # _owning_profiles() for the full rationale + the F-2/F-6 non-empty guard.
    writer.write_results(py_results, profiles=profiles_arr)
    # WI-D: write :AssetBundle nodes BEFORE views/qweb so the legacy
    # <template inherit_id="web.assets_backend"> extenders (written in the qweb
    # pass) resolve against the AssetBundle base nodes via EXTENDS_ASSET_BUNDLE
    # instead of emitting an unresolved warning (the ~13 A2 warnings, ADR-0052).
    writer.write_asset_results(asset_results, profiles=profiles_arr)
    writer.write_view_results(view_results, profiles=profiles_arr)
    # WI-1: write test surface nodes (TestClass/TestMethod) alongside model nodes.
    # test_results collected per-module inside the main loop (see below) then written here.
    # Era-gated dispatch is handled by parser_test.parse_module internally.
    if test_results:
        writer.write_test_results(test_results, profiles=profiles_arr)
    # WI-3: write JsTestSuite nodes for frontend test files (Hoot/QUnit/tour).
    # js_test_suites accumulated per-module in the loop above.
    if js_test_suites:
        writer.write_js_test_results(js_test_suites, profiles=profiles_arr)
    # WI-E (M11): write RelaxNG LintViolation nodes after View nodes exist.
    # ADR-0037: pass repo_root so file_path (a MERGE-key component) is stored
    # repo-relative — keeps it consistent with Stylesheet + the cleanup cypher.
    all_lint_violations = [v for vr in view_results for v in vr.lint_violations]
    writer.write_lint_violations(
        all_lint_violations, profiles=profiles_arr, repo_root=repo_path,
    )
    writer.write_js_graph_results(js_graph_results, profiles=profiles_arr)
    # WI-A1: write Stylesheet nodes (CSS + SCSS) after module writes.
    # ADR-0037: pass repo_root so Stylesheet.file_path + @import targets are
    # stored repo-relative (all stylesheets in this repo share one repo_root).
    # Pass repo_id so the :IMPORTS target MATCH is repo-scoped — without it two
    # repos at the same version sharing a relative path would cross-link.
    writer.write_stylesheets(
        all_stylesheet_infos, profiles=profiles_arr, repo_root=repo_path,
        repo_id=repo.get("id"),
    )

    return {
        "modules": total_modules,
        "views": total_views,
        "qweb": total_qweb,
        "reports": total_reports,
        "asset_bundles": total_asset_bundles,
        "embeddings": total_embeddings,
        "embed_calls": total_embed_calls,
        "js_patches": total_js_patches,
        "owl_comps": total_owl_comps,
        "stylesheets": total_stylesheets,
    }, test_results


# Why a re-parsed module was not pruned (``entity_prune.skipped`` values).
PRUNE_SKIP_NO_RUN = "no_run"  # the writer carries no run token
PRUNE_SKIP_NO_LEDGER = "no_ledger"  # ownership cannot be checked without the ledger
PRUNE_SKIP_NO_RETIRE = "no_retire"  # --no-retire deletes nothing
PRUNE_SKIP_SCAN_UNTRUSTED = "scan_untrusted"  # gate G-A: scan incomplete or untrusted
PRUNE_SKIP_DEGRADED = "degraded"  # a file of the module could not be read or parsed
PRUNE_SKIP_SHARED = "shared"  # another repo still ships the module (ledger)
# A never-observed repo's checkout tracks the module: it becomes a ledger
# owner at its first sync ("shared" from then on).
PRUNE_SKIP_SHARED_UNSYNCED = "shared_unsynced"
# An unsynced repo may ship it and nothing tells whether it does (its last
# ledger observation had it, or git cannot read its checkout).
PRUNE_SKIP_UNDECIDABLE = "undecidable_owner"
PRUNE_SKIP_NO_MODULE = "no_module"  # no Module node after the write
PRUNE_SKIP_SOFT_GATE = "soft_gate"  # the prune would remove a mass of the module

# Skips whose cause can clear without the module changing: the module is
# flagged needs_rewrite so the next run re-parses it and prunes then.
# "shared" and "shared_unsynced" are not retried: the reconcile flags the
# survivor itself (M5) once the other owner is gone.
# "degraded" is decided per source state by _track_degraded_parses (retried
# once after a transient failure, then only when the failing files change).
_PRUNE_RETRY_REASONS = frozenset({
    PRUNE_SKIP_NO_RETIRE, PRUNE_SKIP_SCAN_UNTRUSTED, PRUNE_SKIP_SOFT_GATE,
    PRUNE_SKIP_UNDECIDABLE,
})

# Skips that mean the prune machinery is unavailable (not that the prune was
# unsafe): the module's embeddings keep the pre-B14 behaviour and are replaced
# by what this parse produced.
_PRUNE_UNAVAILABLE_REASONS = frozenset({PRUNE_SKIP_NO_RUN, PRUNE_SKIP_NO_LEDGER})

PRUNE_GATE_PREFIX = "entity_prune:"


def _writer_run_id(writer: IndexWriterProtocol) -> str | None:
    """The run token module children are stamped with, beginning one if needed.

    ``index_profile`` begins one run per profile run (scope ``"run"``); a
    direct ``_index_repo`` call without one gets a fresh per-call token.
    None when the writer cannot carry a token.
    """
    run_id = getattr(writer, "run_id", None)
    if isinstance(run_id, str) and run_id and getattr(writer, "run_scope", None) == "run":
        return run_id
    begin = getattr(writer, "begin_run", None)
    if not callable(begin):
        return None
    run_id = begin(scope="repo")
    return run_id if isinstance(run_id, str) and run_id else None


def _module_file_prefixes(
    info, repo_path: Path, previous_path: str | None = None,
) -> list[str]:
    """Directory prefixes (``/``-terminated) under which the module's files are stored.

    *previous_path* (repo-relative) is the directory the module was indexed
    from before a ``path_drift`` self-heal (``modules_needing_rewrite``): the
    children that old directory alone produced (lint violations are selected
    by file prefix) belong to the module too, so the prune must see them.
    """
    from src.indexer.models import to_repo_relative

    abs_dir = str(Path(info.path))
    candidates = {abs_dir, to_repo_relative(abs_dir, repo_path), info.relative_path(abs_dir)}
    if previous_path:
        candidates |= {previous_path, str(repo_path / previous_path)}
    return sorted(c.rstrip("/") + "/" for c in candidates if c)


def _mass_drop(stale: int, total: int) -> bool:
    """Soft-drop gate G-B applied to one module's children or relationships."""
    from src.indexer.lifecycle import MASS_RETIRE_FLOOR, MASS_RETIRE_FRACTION

    return stale > MASS_RETIRE_FRACTION * total and stale >= MASS_RETIRE_FLOOR


def _prune_reparsed_modules(
    writer: IndexWriterProtocol,
    presence,
    repo: dict,
    *,
    modules_by_version: dict,
    module_health: dict,
    run_id: str | None,
    owning_profile: str,
    repo_path: Path,
    pg_conn,
    lifecycle_on: bool,
    retire: bool,
    scan_ok: bool,
    allow_mass_retire: bool,
    previous_paths: dict[str, str] | None = None,
) -> dict:
    """Intra-module entity prune (ADR-0056 B14, review M13/G4).

    For every module re-parsed this run, delete the children (fields,
    methods, models, views, templates, reports, JS patches, OWL components,
    stylesheets, JS test suites, test classes and methods, lint violations,
    addon test-helper projections) and the relationships
    (``MODULE_CHILD_REL_TYPES``: dependencies, inheritance, view extension,
    template use, ...) that the parse no longer produced - they carry an
    older run token, or none. Constraints, per module M at version v:

    (a) only modules re-parsed this run (``modules_by_version``);
    (b) skipped when any file of M could not be read or parsed
        (``parse_health``); node families the parse did not look at this run
        (``unobserved_labels``, e.g. lint violations without RelaxNG schemas)
        are left out of the prune;
    (c) skipped when, per the LEDGER, another live repo has M ``present``
        (``shared``), or a repo that is not synced may still ship M, decided
        per module by ``reconcile.PotentialOwners``: a never-observed repo
        whose checkout tracks a manifest of M (``shared_unsynced``: it
        becomes an owner at its first sync; this also covers a new second
        owner that has written M but not committed its ledger row yet), or
        one whose last ledger observation had M or whose checkout git cannot
        read (``undecidable_owner``). An unsynced repo that does not ship M
        does not hold M's prune;
    (d) skipped when the prune would remove more than MASS_RETIRE_FRACTION of
        M's children, or of M's relationships, and at least
        MASS_RETIRE_FLOOR of them (bypassed by ``allow_mass_retire``):
        ``lifecycle_attention`` names it, the gate id
        ``entity_prune:<M>@<v>`` makes the CLI exit 3, and the Module node
        records the hold (``record_module_prune_held``, read by
        ``lifecycle-audit`` as the ``held_prunes`` finding) until a run
        prunes M.

    Nothing is pruned under ``--no-retire`` or when gate G-A failed (the
    parse may be of the wrong tree). Skips whose cause can clear without the
    module changing (``no_retire``, ``scan_untrusted``, ``soft_gate``,
    ``undecidable_owner``) are returned in ``retry`` so the caller flags the
    module ``needs_rewrite`` and the next run re-parses and prunes it without
    a source change; ``shared`` is not retried (the reconcile flags the
    survivor once the other owner is gone, M5; so is ``shared_unsynced``).
    What no owner of a shared module defines any more is pruned by the
    version reconcile once every owner recorded a complete parse of its copy
    (``_record_full_parses``, ``reconcile._Reconciler.prune_shared``).
    ``degraded`` is retried per
    source state by the caller (``_track_degraded_parses``: once after a
    transient failure, then only when the failing files change), which also
    names the files in ``lifecycle_attention``. An undecidable skip names
    the unsynced repos there.

    Nodes and relationships written before B14 carry no token. A module's
    first re-parse after deploy re-stamps everything it still defines, so
    one still without this run's token after a complete (b), single-owner
    (c), non-mass (d) re-parse is one the source no longer defines - the same
    rule as for an older token. Ownership comes from the ledger only, never
    from ``Module.profile``, so pre-ledger profile arrays cannot block it.

    *previous_paths* (``{name: repo-relative dir}``): modules re-written by
    the ``path_drift`` self-heal (``modules_needing_rewrite``). Their old
    directory's file prefixes join the selection, so the children only the
    old path produced (stylesheets, tests, lint violations) are pruned too.

    Embeddings follow the graph. The write path upserts a module's chunks
    without deleting (``_parse_and_write``); here, when M was pruned (or the
    prune machinery is unavailable: ``no_run`` / ``no_ledger``, the pre-B14
    replace), the owning profile's rows of M whose chunk key this parse did
    not produce are deleted. For every other skip the old rows stay, exactly
    like the graph nodes they describe.

    A ``shared_unsynced`` skip is recorded on the Module node with the
    siblings it waits for (``_track_prune_deferrals``); the reconcile sends
    the owner back to prune once those synced without present-owning M.

    Returns ``{"run_id", "pruned": {name: {"deleted", "by_label",
    "rels_deleted", "rels_by_label"}}, "skipped": {name: reason},
    "embeddings_deleted": int, "gates_tripped": [ids], "attention":
    [messages], "retry": [names], "deferred_for": {name: [repo ids]}}``.
    """
    from src.indexer.lifecycle import MASS_RETIRE_FLOOR, MASS_RETIRE_FRACTION
    from src.indexer.reconcile import PotentialOwners

    report: dict = {
        "run_id": run_id,
        "pruned": {},
        "skipped": {},
        "embeddings_deleted": 0,
        "gates_tripped": [],
        "attention": [],
        "retry": [],
        "deferred_for": {},
    }
    url = repo.get("url", repo.get("local_path"))
    reparsed = [
        (version, name, info)
        for version, mods in sorted(modules_by_version.items())
        for name, info in sorted(mods.items())
    ]
    if not reparsed:
        return report

    blanket: str | None = None
    if not run_id:
        blanket = PRUNE_SKIP_NO_RUN
    elif not lifecycle_on or presence is None:
        blanket = PRUNE_SKIP_NO_LEDGER
    elif not retire:
        blanket = PRUNE_SKIP_NO_RETIRE
    elif not scan_ok:
        blanket = PRUNE_SKIP_SCAN_UNTRUSTED

    shared: dict[str, set[str]] = {}
    owners = PotentialOwners(presence) if blanket is None else None
    released: dict[str, list[str]] = {}
    if blanket is None:
        for version in sorted(modules_by_version):
            shared[version] = presence.names_owned_elsewhere(
                version, modules_by_version[version], repo["id"],
            )

    for version, name, info in reparsed:
        health = module_health.get((version, name))
        reason = blanket or _prune_skip_reason(
            owners, repo, version, name, health, shared[version], report, url,
        )
        pruned = False
        if reason is None:
            prefixes = _module_file_prefixes(
                info, repo_path, (previous_paths or {}).get(name),
            )
            skip_labels = sorted(health.unobserved_labels)
            census = writer.module_children_census(
                version, name, run_id=run_id,
                file_prefixes=prefixes, skip_labels=skip_labels,
            )
            stale, total = census["stale"], census["total"]
            rels_stale, rels_total = census["rels_stale"], census["rels_total"]
            if not census["module_exists"]:
                reason = PRUNE_SKIP_NO_MODULE
            elif not allow_mass_retire and (
                _mass_drop(stale, total) or _mass_drop(rels_stale, rels_total)
            ):
                reason = PRUNE_SKIP_SOFT_GATE
                report["gates_tripped"].append(f"{PRUNE_GATE_PREFIX}{name}@{version}")
                report["attention"].append(
                    f"entity prune of {name}@{version} held: {stale} of {total} indexed "
                    f"node(s) and {rels_stale} of {rels_total} relationship(s) are no "
                    "longer produced by its parse (more than "
                    f"{int(MASS_RETIRE_FRACTION * 100)}% and at least {MASS_RETIRE_FLOOR}); "
                    "graph and embeddings kept; check the parse, then re-run with "
                    "--allow-mass-retire"
                )
                _logger.warning(
                    "Repo %s: entity prune held for %s@%s (%d of %d nodes, %d of %d "
                    "relationships stale)", url, name, version, stale, total,
                    rels_stale, rels_total,
                )
                record_held = getattr(writer, "record_module_prune_held", None)
                if callable(record_held):
                    record_held(
                        version, name, repo_id=repo.get("id"), stale=stale, total=total,
                        rels_stale=rels_stale, rels_total=rels_total,
                    )
            else:
                pruned = True
                released.setdefault(version, []).append(name)
                if stale or rels_stale:
                    report["pruned"][name] = writer.prune_module_children(
                        version, name, run_id=run_id,
                        file_prefixes=prefixes, skip_labels=skip_labels,
                    )
                else:
                    report["pruned"][name] = {
                        "deleted": 0, "by_label": {}, "rels_deleted": 0, "rels_by_label": {},
                    }
        if reason is not None:
            report["skipped"][name] = reason
        if pruned or reason in _PRUNE_UNAVAILABLE_REASONS:
            _reconcile_module_embeddings(
                report, health, name, version, owning_profile, pg_conn,
            )

    clear_held = getattr(writer, "clear_module_prune_held", None)
    if callable(clear_held):
        for version, names in sorted(released.items()):
            clear_held(version, names)
    _track_prune_deferrals(writer, repo, reparsed, report)
    report["retry"] = sorted(
        n for n, reason in report["skipped"].items() if reason in _PRUNE_RETRY_REASONS
    )
    deleted = sum(r["deleted"] for r in report["pruned"].values())
    rels_deleted = sum(r["rels_deleted"] for r in report["pruned"].values())
    if deleted or rels_deleted or report["embeddings_deleted"]:
        _logger.info(
            "Repo %s: entity prune removed %d node(s), %d relationship(s) and %d "
            "embedding row(s) that the re-parsed modules no longer define",
            url, deleted, rels_deleted, report["embeddings_deleted"],
        )
    return report


def _track_prune_deferrals(writer, repo: dict, reparsed: list, report: dict) -> None:
    """Record each ``shared_unsynced`` skip on its Module node; forget the
    deferral of a module that was pruned or is now ``shared``.

    The reconcile reads the record back (``prune_deferred_modules``) once the
    siblings it waits for synced, so a sibling that turns out not to
    present-own the module (excluded, ``installable: False``, absent) sends
    the owner back to prune it (``needs_rewrite``) instead of deferring for
    ever. Any other skip keeps the record: that reason owns its own retry.
    """
    record = getattr(writer, "record_module_prune_deferred", None)
    clear = getattr(writer, "clear_module_prune_deferred", None)
    settled: dict[str, list[str]] = {}
    for version, name, _info in reparsed:
        reason = report["skipped"].get(name)
        waits_for = report["deferred_for"].get(name)
        if waits_for and reason == PRUNE_SKIP_SHARED_UNSYNCED:
            if callable(record):
                record(version, name, repo_id=repo.get("id"), waits_for=waits_for)
        elif name in report["pruned"] or reason == PRUNE_SKIP_SHARED:
            settled.setdefault(version, []).append(name)
    if callable(clear):
        for version, names in sorted(settled.items()):
            clear(version, names)


def _prune_skip_reason(
    owners, repo: dict, version: str, name: str, health, shared: set[str],
    report: dict, url,
) -> str | None:
    """Per-module constraints (b) and (c); appends operator messages to *report*."""
    if health is None or health.degraded:
        # Retry and operator message: _track_degraded_parses.
        _logger.warning(
            "Repo %s: entity prune skipped for %s@%s - parse degraded", url, name, version,
        )
        return PRUNE_SKIP_DEGRADED
    if name in shared:
        return PRUNE_SKIP_SHARED
    from src.indexer.reconcile import POTENTIAL_OWNER_SHIPS_NAME

    blockers = owners.blockers(name, version, [repo["id"]])
    if not blockers:
        return None
    unknown = [b for b in blockers if b["why"] != POTENTIAL_OWNER_SHIPS_NAME]
    if not unknown:
        _logger.info(
            "Repo %s: entity prune skipped for %s@%s - shipped by not yet synced "
            "repo(s) %s", url, name, version,
            ", ".join(sorted(b.get("repo_basename") or "?" for b in blockers)),
        )
        report["deferred_for"][name] = sorted({b["repo_id"] for b in blockers})
        return PRUNE_SKIP_SHARED_UNSYNCED
    labels = ", ".join(
        f"{b.get('repo_basename') or '?'} (repo id={b.get('repo_id')}) [{b['why']}]"
        for b in unknown
    )
    report["attention"].append(
        f"entity prune of {name}@{version} deferred: repo(s) {labels} not synced "
        "may still ship it"
    )
    return PRUNE_SKIP_UNDECIDABLE


def _reconcile_module_embeddings(
    report: dict, health, name: str, version: str, owning_profile: str, pg_conn,
) -> None:
    """Delete the owning profile's rows of *name* this parse did not produce (B14).

    Runs on the run's own *pg_conn* when it is autocommit (the DELETE is then
    its own committed transaction), like every other lifecycle write of the
    run; only a non-autocommit or absent run connection falls back to a
    short pool checkout. Without a pool the preceding upsert wrote nothing
    either, so there is nothing to reconcile.

    A parse that wrote no rows (``--no-embed``) still removes the rows of the
    entities it no longer produces - deleting needs no embedder - matched by
    ``(chunk_type, entity_name)`` so a live entity's older row is kept; the
    rows are counted first and the count is the delete's ``expected``.
    """
    if health is None or health.degraded or health.embedded_keys is None or pg_conn is None:
        return
    from src.indexer.writer_pgvector import delete_module_embeddings_except

    by_entity = not health.embeddings_written

    def reconcile(conn) -> int:
        expected = None
        if by_entity:
            expected = delete_module_embeddings_except(
                conn, name, version, owning_profile, health.embedded_keys,
                by_entity=True, delete=False,
            )
            if not expected:
                return 0
        return delete_module_embeddings_except(
            conn, name, version, owning_profile, health.embedded_keys,
            by_entity=by_entity, expected=expected,
        )

    if getattr(pg_conn, "autocommit", None) is True:
        report["embeddings_deleted"] += reconcile(pg_conn)
        return
    from src.db.exceptions import PoolNotInitializedError
    from src.db.pg import get_pool

    try:
        pool = get_pool()
    except PoolNotInitializedError:
        return
    with pool.checkout() as conn:
        report["embeddings_deleted"] += reconcile(conn)


def _failure_fingerprint(repo_path: Path, paths: Iterable[str]) -> str:
    """Identity of the on-disk state of a degraded module's failing files.

    A digest of each file's (path, mode, size, mtime) - or its absence -
    so it changes when the file is edited, replaced, deleted or has its
    permissions fixed, and stays equal while nothing about it changed.
    """
    digest = hashlib.sha1()
    for rel in sorted(set(paths)):
        target = Path(rel) if Path(rel).is_absolute() else repo_path / rel
        try:
            st = target.stat()
            state = (rel, st.st_mode, st.st_size, st.st_mtime_ns)
        except OSError as exc:
            state = (rel, "missing", type(exc).__name__)
        digest.update(repr(state).encode())
    return digest.hexdigest()


def _degraded_message(name: str, version: str, problems: list[str], retry: bool) -> str:
    shown = "; ".join(problems[:5]) + (
        f"; ... {len(problems) - 5} more" if len(problems) > 5 else ""
    )
    when = (
        "the module is re-parsed once next run (transient read failure)" if retry
        else "the module is re-parsed when these files change"
    )
    return (
        f"entity prune of {name}@{version} skipped: parse degraded, "
        f"{len(problems)} file problem(s): {shown}; {when}"
    )


def _standing_attention(repo: dict, repo_path: Path, degraded_records: dict) -> list[str]:
    """The ``lifecycle_attention`` of a repo the run skips at its unchanged HEAD (F38).

    The skip happens only when the ledger reflects HEAD and no module is
    flagged ``needs_rewrite``, so no gate is tripped, no name is pending and
    no entity prune is held or deferred; the signals of the run that synced
    the ledger that no longer hold (a bypassed gate, an entity prune since
    applied) must not linger. What still stands at that HEAD: the version
    rule, git tracking and the ``origin/<branch>`` ref, and the modules whose
    parse is still degraded (their files did not change, or the repo would
    not be skipped).
    """
    from src.git_utils import list_tracked_manifests
    from src.indexer.registry import resolve_repo_version

    branch = repo.get("branch")
    attention = list(resolve_repo_version(
        str(repo_path), branch=branch, profile_version=repo.get("odoo_version"),
    ).attention)
    attention.extend(_git_trust_attention(
        repo_path, branch, list_tracked_manifests(repo_path) is not None,
    ))
    for (version, name), record in sorted(degraded_records.items()):
        attention.append(_degraded_message(name, version, list(record["problems"]), False))
    return attention


def _write_attention(presence, repo: dict, attention: list[str], url) -> None:
    """Replace ``repos.lifecycle_attention`` with *attention* (cleared when empty).

    No write when the repo row already carries exactly that text.
    """
    text = "; ".join(attention) or None
    if "lifecycle_attention" in repo and repo["lifecycle_attention"] == text:
        return
    try:
        if text:
            presence.set_lifecycle_attention(repo["id"], text)
        else:
            presence.clear_lifecycle_attention(repo["id"])
    except Exception:  # noqa: BLE001 - never fail a repo over the attention column
        _logger.exception("Repo %s: could not write lifecycle_attention", url)


def _degraded_state(writer: IndexWriterProtocol, repo: dict, repo_path: Path):
    """``(records, changed)``: the repo's degraded-parse records and the names
    whose failing files changed on disk since (re-parse them this run)."""
    read = getattr(writer, "parse_degraded_modules", None)
    if not callable(read) or repo.get("id") is None:
        return {}, set()
    records: dict = {}
    changed: set[str] = set()
    for row in read(repo["id"]) or []:
        records[(row["odoo_version"], row["name"])] = row
        if _failure_fingerprint(repo_path, row["paths"]) != row["fingerprint"]:
            changed.add(row["name"])
    return records, changed


def _track_degraded_parses(
    writer: IndexWriterProtocol,
    repo: dict,
    *,
    module_health: dict,
    records: dict,
    present_names: set[str],
    repo_path: Path,
) -> tuple[set[str], list[str]]:
    """Retry a degraded module once per source state, never on every run (B14).

    For every module parsed this run: a complete parse clears its record; a
    degraded parse records ``_failure_fingerprint`` of its failing files and
    is retried (``needs_rewrite``) only when the failure is transient
    (an OSError: unreadable, permission, IO) AND this fingerprint was not
    already recorded - i.e. once per state. A content failure (syntax) is
    never retried: the module is re-parsed when a commit changes it, or
    when ``_degraded_state`` sees its failing files change on disk. Modules
    still degraded but not parsed this run keep their operator message.

    Returns ``(retry_names, attention_messages)``.
    """
    from src.indexer.models import to_repo_relative

    root = str(repo_path).rstrip("/") + "/"
    retry: set[str] = set()
    attention: list[str] = []
    clear: dict[str, list[str]] = {}
    for (version, name), health in sorted(module_health.items()):
        prev = records.get((version, name))
        if not health.degraded:
            if prev is not None:
                clear.setdefault(version, []).append(name)
            continue
        paths = sorted(
            to_repo_relative(p, repo_path) or p for p in health.failure_paths
        )
        fingerprint = _failure_fingerprint(repo_path, paths)
        again = health.transient and (prev is None or prev["fingerprint"] != fingerprint)
        problems = [f.replace(root, "") for f in health.failures]
        record = getattr(writer, "record_module_parse_degraded", None)
        if callable(record):
            record(
                version, name, repo_id=repo.get("id"), fingerprint=fingerprint,
                paths=paths, problems=problems,
            )
        if again:
            retry.add(name)
        attention.append(_degraded_message(name, version, problems, again))
    for (version, name), prev in sorted(records.items()):
        if (version, name) in module_health or name not in present_names:
            continue
        attention.append(_degraded_message(name, version, list(prev["problems"]), False))
    clear_fn = getattr(writer, "clear_module_parse_degraded", None)
    if callable(clear_fn):
        for version, names in sorted(clear.items()):
            clear_fn(version, names)
    return retry, attention


def _record_full_parses(
    presence,
    writer: IndexWriterProtocol,
    repo: dict,
    *,
    module_health: dict,
    run_id: str | None,
    scan_ok: bool,
    head: str | None,
    present_names: set[str],
    embedded_at=None,
) -> None:
    """Ledger record of the copies this run parsed completely (B14, shared modules).

    A present module parsed this run with every file read, under a run token
    and a trusted scan, gets ``last_full_parse_at`` = the run start on the
    Neo4j clock (every child the parse wrote is stamped at or after it) plus
    the families it did not observe; any other parse of it (degraded, no
    token, untrusted scan) forgets the record, since its children may not
    all carry the stamp. A parse that also (re-)embedded every chunk of the
    module records *embedded_at* (PostgreSQL time taken before the parse, see
    ``_pg_clock``). The version post-pass prunes a module several repos
    ship only while every present owner has a record
    (``reconcile._Reconciler.prune_shared``).
    """
    record = getattr(presence, "record_full_parses", None)
    forget = getattr(presence, "clear_full_parses", None)
    if not module_health or not callable(record) or not callable(forget):
        return
    started = getattr(writer, "run_started_at", None)
    complete_run = bool(run_id) and scan_ok and started is not None
    full: dict[str, dict[str, list[str]]] = {}
    embedded: dict[str, list[str]] = {}
    partial: dict[str, list[str]] = {}
    for (version, name), health in sorted(module_health.items()):
        if name not in present_names:
            continue
        if complete_run and health is not None and not health.degraded:
            full.setdefault(version, {})[name] = sorted(health.unobserved_labels)
            if health.embeddings_written and not health.embeddings_incomplete:
                embedded.setdefault(version, []).append(name)
        else:
            partial.setdefault(version, []).append(name)
    for version, names in sorted(partial.items()):
        forget(repo["id"], version, names)
    for version, parses in sorted(full.items()):
        record(
            repo["id"], version, parses, parsed_at=started, head_sha=head,
            embedded=embedded.get(version, ()), embedded_at=embedded_at, run_token=run_id,
        )


def shared_parse_backlog(presence, repo: dict, degraded_records: dict) -> tuple[int, list[str]]:
    """``(remaining, batch)`` of the repo's shared-module bootstrap (B14).

    The repo's present copies of modules another live repo also ships that
    have no complete-parse record, minus the modules whose last parse by this
    repo is recorded degraded (their re-parse waits for the failing files,
    ``_degraded_state``); *batch* is at most
    ``constants.shared_parse_bootstrap_per_run()`` of them, the ones whose other owners
    are all recorded first. ``(0, [])`` without a ledger.
    """
    read = getattr(presence, "shared_parse_backlog", None)
    if not callable(read) or repo.get("id") is None:
        return 0, []
    degraded = {name for (_v, name) in degraded_records}
    return read(repo["id"], limit=_constants.shared_parse_bootstrap_per_run(), exclude=degraded)


def shared_parse_bootstrap(presence, repo: dict, degraded_records: dict) -> list[str]:
    """The names this run re-parses for the shared-module bootstrap (see
    :func:`shared_parse_backlog`)."""
    if _constants.shared_parse_bootstrap_per_run() <= 0:
        return []
    return shared_parse_backlog(presence, repo, degraded_records)[1]


def _pg_clock(pg_conn):
    """PostgreSQL's current time on the run connection, or None without one.

    Taken before the parse: every embedding upsert of the parse runs in a
    later transaction, so its ``indexed_at = NOW()`` is at or after it.
    """
    if getattr(pg_conn, "autocommit", None) is not True:
        return None
    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT clock_timestamp()")
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 - no clock only disables the shared embedding prune
        _logger.warning("could not read the PostgreSQL clock", exc_info=True)
        return None


def _gc_stale_test_nodes(
    writer: IndexWriterProtocol,
    live_names_by_version: dict[str, list[str]],
    test_results: list,
    repo_root_name: str,
) -> None:
    """Test-node GC (M12, explicit and not behind any flag).

    Removes this repo's TestClass/TestMethod nodes whose module is no longer
    in the scan (module level) or whose test FILE was deleted inside a module
    re-parsed this run (file level). Repo-scoped (Defect H) and restricted to
    the re-parsed modules for the file level (Defect I).
    """
    live_test_files_by_version: dict[str, set[str]] = {}
    reparsed_by_version: dict[str, set[str]] = {}
    for tr in test_results:
        ver = tr.module.odoo_version
        bucket = live_test_files_by_version.setdefault(ver, set())
        reparsed_by_version.setdefault(ver, set()).add(tr.module.name)
        for tc in tr.test_classes:
            if tc.file_path:
                bucket.add(tc.file_path)
    for ver, live_names in live_names_by_version.items():
        writer.gc_stale_test_nodes(
            ver, live_names,
            live_file_paths=sorted(live_test_files_by_version.get(ver, set())),
            repo=repo_root_name,
            live_modules_for_file_gc=sorted(reparsed_by_version.get(ver, set())),
        )


def _index_repo(
    repo: dict,
    writer: IndexWriterProtocol,
    pg_conn=None,
    embedder=None,
    progress: bool = False,
    full_reindex: bool = False,
    gc: bool = False,
    profile_name: str | None = None,
    core_rng_root: Path | None = None,
    refresh: bool = True,
    *,
    retire: bool = True,
    allow_mass_retire: bool = False,
) -> dict:
    """Index a single repo dict (from get_repos_for_profile) and observe its
    module lifecycle (ADR-0056).

    Returns per-repo counters: {modules, views, qweb, embeddings, ...}; a run
    that reached the ledger also carries ``lifecycle`` (see below).
    Pass pg_conn + embedder to also write semantic embeddings to pgvector.
    Set progress=True to show tqdm progress bar during module iteration.
    profile_name is stamped on every EmbeddingChunk written so re-indexing
    one profile does not erase another profile's chunks for the same module.

    core_rng_root: Path to <odoo_core_root>/odoo/addons/base/rng/ (or the
        openerp/ equivalent for v8/v9).  When the repo itself contains the RNG
        directory it is used directly; *core_rng_root* is the fallback for
        addon-only repos whose views still need version-exact RelaxNG validation.
        None → RelaxNG validation is silently skipped (no false positives).

    Every run scans the repo (``build_registry_scan``: git-tracked manifests)
    and reconciles the scan with the ``module_presence`` ledger; no module is
    ever deleted here - retirement is decided by ``reconcile.reconcile_version``
    after the run's repos were indexed. What IS deleted here: the test-node GC
    and the intra-module entity prune (B14) - the children of a re-parsed
    module that its parse no longer produced (see ``_prune_reparsed_modules``).

    - Unchanged skip (zero cost) only when HEAD == ``repos.head_sha`` AND the
      ledger reflects that HEAD (``repos.presence_head_sha``) AND no module of
      the repo is flagged ``needs_rewrite``.
    - Sync path: HEAD unchanged but the ledger is behind (first run after the
      ledger shipped, a gate trip, a pending retirement): scan + ledger + stamp,
      no re-parse except the modules that must be (re)written.
    - Incremental: only modules whose directory changed since ``head_sha`` are
      re-parsed. Force-push (stored sha not an ancestor) or ``full_reindex``
      re-parse everything.
    - Always (re)written as well: ``needs_rewrite`` names (another repo stopped
      shipping a module this repo still ships, M5) and self-heal names (present
      in the scan but without a graph node carrying this repo's profile, H2).
    - After the writes: ``commit_observed`` (present/excluded rows), names the
      scan no longer contains are flagged ``retire_pending`` with the removing
      commit and successor as evidence, blocked by the repo's gates (G-A scan
      complete + trusted, G-B mass drop) or by ``retire=False``; the Module
      nodes are stamped with the HEAD and their ledger owners (L6).
    - ``head_sha`` advances after all writes succeed. ``presence_head_sha``
      advances only when no gate tripped, the stamp matched every present
      module, and nothing is pending (H1); when the only reason is pending
      names, ``lifecycle.presence_deferred_head`` hands the HEAD to the
      reconcile, which advances it once those names are retired.
    - Before the ledger commit, the entity prune runs over the re-parsed
      modules (children, relationships, embeddings); a module it had to skip
      for a reason that can clear on its own (soft gate, an unsynced repo
      that may ship it (``undecidable_owner``), ``--no-retire``, untrusted
      scan) is flagged ``needs_rewrite`` so
      the next run re-parses and prunes it. A degraded parse is recorded on
      the Module node with a fingerprint of its failing files and retried
      once after a transient (IO) failure; after that it is re-parsed only
      when a commit changes it or its failing files change on disk (checked
      before the unchanged skip), and its files stay named in
      ``lifecycle_attention`` while it remains degraded.
    - Signals the operator must see (version rule, gate trips, git refusing
      the repo, missing ``origin/<branch>`` ref, stamp shortfall, a held,
      degraded or deferred entity prune) replace ``repos.lifecycle_attention``;
      a clean run clears it. The unchanged skip rewrites it too, with what
      still stands at that HEAD (``_standing_attention``: version rule, git
      trust, still-degraded parses), so a signal that no longer holds (e.g.
      a gate bypassed by ``--allow-mass-retire``) never outlives its run.

    ``lifecycle`` counters: ``odoo_version`` (ledger key version),
    ``gates_tripped`` (gate ids, including ``entity_prune:<name>@<version>``),
    ``pending`` (names flagged this run), ``attention`` (messages),
    ``presence_synced`` (bool), ``presence_deferred_head`` (sha or None),
    ``entity_prune`` (present when modules were re-parsed: ``run_id``,
    ``pruned``, ``skipped``, ``embeddings_deleted``, ``gates_tripped``,
    ``retry``).

    ``gc`` is accepted for backward compatibility and ignored (retirement and
    test-node GC always run). ``retire=False`` (CLI ``--no-retire``) scans and
    writes but deletes nothing: absent names stay pending (``no_retire``) and
    the test-node GC is skipped. ``allow_mass_retire`` bypasses gate G-B.

    refresh (nightly-fetch): when True (default), do a ``git fetch`` +
        ``reset --hard origin/<branch>`` on the local clone BEFORE the incremental
        check, so upstream merges become visible to the cron (the incremental
        check only reads the local clone, so without a fetch an advanced upstream
        branch left local HEAD == repos.head_sha and the repo was skipped). The
        fetch runs under the per-repo advisory lock and is FAIL-SAFE: a fetch
        error is logged and indexing proceeds on the on-disk state (network
        reachability is never a hard dependency of the nightly job). Set False
        (CLI ``--no-fetch``) to preserve the old local-only behaviour. See
        ``refresh_before_scan``.
    """
    # Resolve the parent orchestrator module at call time. ``build_registry_scan``,
    # ``topological_sort``, ``repo_store`` and ``_presence_store`` are referenced
    # through it so that test patches applied to ``src.indexer.pipeline.<name>``
    # are honoured (see the module docstring). Deferred (function-local) import
    # keeps a cold ``import src.indexer.pipeline_repo`` cycle-free.
    from src.indexer import pipeline as _pipeline

    del gc  # deprecated: retirement is no longer opt-in (ADR-0056)

    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]
    url = repo.get("url", local_path)

    if not Path(local_path).is_dir():
        raise FileNotFoundError(f"local_path does not exist: {local_path!r}")

    # Resolve the RNG directory for version-exact RelaxNG validation (WI-E rework).
    # Prefer the RNG dir within THIS repo's local_path (covers the main Odoo core
    # repo where addons live alongside the rng/ dir).  Fall back to core_rng_root
    # (resolved once per profile in index_profile) for addon-only repos.
    # If neither exists → rng_root=None → validation silently skipped.
    repo_path = Path(local_path)
    _rng_candidates = [
        repo_path / "odoo" / "addons" / "base" / "rng",
        repo_path / "openerp" / "addons" / "base" / "rng",
    ]
    rng_root: Path | None = next(
        (p for p in _rng_candidates if p.is_dir()), core_rng_root
    )

    # === Pre-scan refresh (nightly-fetch) ===
    # Fetch + reset --hard origin/<branch> BEFORE reading HEAD, so an advanced
    # upstream branch (e.g. a merged PR) is picked up by the incremental check
    # below instead of being invisible (local HEAD == repos.head_sha -> skip).
    # FAIL-SAFE inside refresh_before_scan: a fetch error is logged and we index
    # whatever is on disk. Gated by `refresh` (CLI --no-fetch turns it off).
    if refresh:
        refresh_before_scan(repo, pg_conn)
    # === End pre-scan refresh ===

    # === Incremental / sync decision (W2-4 + ADR-0056) ===
    current_head = _incremental.get_repo_head(repo_path)
    if current_head is None:
        _logger.warning(
            "Cannot determine HEAD for repo %s - full reindex without head_sha tracking",
            url,
        )

    presence = _pipeline._presence_store() if pg_conn is not None else None
    if presence is not None and getattr(pg_conn, "autocommit", None) is True:
        # Ledger calls run on this worker's own connection, never the shared
        # pool: waiting for the per-version ledger lock (up to
        # RETIRE_LOCK_WAIT_SECONDS while another process reconciles) must not
        # pin pool connections that embedding writes and repo_store() need.
        presence = _ConnBoundStore(presence, pg_conn)
    last_head: str | None = None
    presence_head: str | None = None
    rewrite_names: list[str] = []
    if pg_conn is not None:
        last_head = _pipeline.repo_store().get_repo_head_sha(repo["id"])
    if presence is not None:
        presence_head = presence.presence_head_sha(repo["id"])
        rewrite_names = presence.needs_rewrite_names(repo["id"])

    # Degraded-parse records (B14): a module whose failing files changed on
    # disk since its degraded parse (edited, deleted, permission fixed) is
    # re-parsed now even when HEAD did not move.
    degraded_records, degraded_changed = _degraded_state(writer, repo, repo_path)
    # Shared-module bootstrap (B14): a bounded batch of this repo's copies of
    # shared modules without a complete-parse record is re-parsed like a
    # needs_rewrite name (sync path when HEAD did not move).
    bootstrap = shared_parse_bootstrap(presence, repo, degraded_records)
    if bootstrap:
        _logger.info(
            "Repo %s: re-parsing %d shared module(s) without a complete-parse record "
            "(bootstrap, budget %d per run): %s", url, len(bootstrap),
            _constants.shared_parse_bootstrap_per_run(), ", ".join(bootstrap[:10]),
        )
        rewrite_names = sorted(set(rewrite_names) | set(bootstrap))

    plan = plan_repo_run(
        repo_path, current_head, last_head, presence_head, rewrite_names,
        full_reindex=full_reindex, ledger=presence is not None,
        degraded_changed=bool(degraded_changed),
    )
    if plan.mode == RUN_SKIP:
        _logger.info(
            "Repo %s unchanged (HEAD %s) - skipping reindex", url, current_head[:8],
        )
        if presence is not None:
            _write_attention(
                presence, repo, _standing_attention(repo, repo_path, degraded_records), url,
            )
        return dict(_EMPTY_COUNTERS)

    diff_base: str | None = plan.diff_base
    if plan.force_push:
        _logger.warning(
            "Repo %s: force-push or history rewrite detected "
            "(stored %s not ancestor of HEAD %s) - falling back to full reindex",
            url, last_head[:8], current_head[:8],
        )
    sync_only = plan.mode == RUN_SYNC
    if sync_only:
        _logger.info(
            "Repo %s: HEAD %s unchanged but the lifecycle ledger is behind it "
            "(presence %s, %d needs_rewrite) - sync path, no re-parse of unchanged modules",
            url, current_head[:8], (presence_head or "none")[:8], len(rewrite_names),
        )
    # === End incremental / sync decision ===

    # Scan truth for this repo (ADR-0056): git-tracked manifests, version rule
    # from the registered branch + profile version, present vs excluded.
    scan = _pipeline.build_registry_scan(
        local_path, odoo_version,
        branch=repo.get("branch"),
        repo_url=repo.get("url"),
        repo_id=repo.get("id"),
    )
    present_names: set[str] = scan.present_names()
    live_module_names_by_version: dict[str, list[str]] = {
        ver: sorted(mods) for ver, mods in scan.modules.items()
    }
    # Repo dir name (m.repo in Neo4j) - derived the same way registry.py does it.
    repo_root_name: str = Path(local_path).name

    # F4 - single source of truth for this repo's OWNING profile. Compute ONCE
    # here and feed BOTH the Neo4j writer (`profiles=`) AND the pgvector write
    # (`profile_name=`) from it, so the two stores can never diverge by
    # construction (Neo4j↔pgvector owner split-brain). _owning_profiles raises
    # on a falsy owner (F2), so `owning_profile` below is always a real name.
    _profiles_arr: list[str] = _owning_profiles(repo, profile_name, repo_root_name)
    owning_profile: str = _profiles_arr[0]

    # === Lifecycle pre-write: trust, gates, self-heal (ADR-0056) ===
    observation = observe_lifecycle(
        repo, scan, presence, current_head, allow_mass_retire=allow_mass_retire,
        writer=writer, owning_profile=owning_profile,
    )
    attention: list[str] = observation.attention
    lifecycle_on = observation.lifecycle_on
    rows = observation.rows
    transitions = observation.transitions
    gates = observation.gates
    for reason in gates.reasons:
        _logger.warning("Repo %s: lifecycle gate: %s", url, reason)

    heal = self_heal_rewrites(
        writer, presence, repo, scan, owning_profile, plan=plan, lifecycle_on=lifecycle_on,
    )
    heal_names: set[str] = set(heal)
    if heal:
        for reason in (REWRITE_NO_NODE, REWRITE_PATH_DRIFT, REWRITE_REPO_DRIFT):
            names = sorted(n for n, d in heal.items() if d["reason"] == reason)
            if names:
                _logger.info(
                    "Repo %s: %d present module(s) re-written (self-heal, %s): %s",
                    url, len(names), reason, ", ".join(names[:10]),
                )
    # === End lifecycle pre-write ===

    # === Write set ===
    total_before = sum(len(mods) for mods in scan.modules.values())
    write_names = regular_write_names(repo_path, scan, plan, current_head)
    if plan.mode == RUN_INCREMENTAL:
        _logger.info(
            "Repo %s: incremental - %d/%d modules changed",
            url, len(write_names), total_before,
        )
    write_names |= (set(rewrite_names) | heal_names | degraded_changed) & present_names
    modules_by_version: dict[str, dict] = {
        ver: {n: info for n, info in mods.items() if n in write_names}
        for ver, mods in scan.modules.items()
    }
    modules_by_version = {v: m for v, m in modules_by_version.items() if m}
    if not modules_by_version and diff_base is not None:
        _logger.info(
            "Repo %s: no module dirs changed (only meta files) - head_sha will "
            "still be advanced", url,
        )
    # === End write set ===

    counters = dict(_EMPTY_COUNTERS)
    test_results: list = []
    prune_report: dict | None = None
    if modules_by_version:
        run_id = _writer_run_id(writer)
        module_health: dict = {}
        embedded_at = _pg_clock(pg_conn) if lifecycle_on else None
        counters, test_results = _parse_and_write(
            modules_by_version,
            writer=writer, repo=repo, repo_path=repo_path, rng_root=rng_root,
            pg_conn=pg_conn, embedder=embedder, progress=progress,
            profiles_arr=_profiles_arr, owning_profile=owning_profile,
            module_health=module_health,
        )
        # Intra-module entity prune (B14): children the re-parse no longer
        # produced. Before the ledger commit, like every other graph write.
        prune_report = _prune_reparsed_modules(
            writer, presence, repo,
            modules_by_version=modules_by_version, module_health=module_health,
            run_id=run_id, owning_profile=owning_profile, repo_path=repo_path,
            pg_conn=pg_conn, lifecycle_on=lifecycle_on, retire=retire,
            scan_ok=gates.scan_ok, allow_mass_retire=allow_mass_retire,
            previous_paths={
                n: d["indexed_path"] for n, d in heal.items()
                if d["reason"] == REWRITE_PATH_DRIFT and d.get("indexed_path")
            },
        )
        attention.extend(prune_report["attention"])
    prune_retry: set[str] = set(prune_report["retry"]) if prune_report else set()
    degraded_retry, degraded_attention = _track_degraded_parses(
        writer, repo,
        module_health=module_health if modules_by_version else {},
        records=degraded_records, present_names=present_names, repo_path=repo_path,
    )
    attention.extend(degraded_attention)
    prune_retry |= degraded_retry
    if prune_report is not None:
        prune_report["retry"] = sorted(prune_retry)

    # NOTE: reconcile_same_name_inherits runs once per version in the post-pass
    # (index_profile / reconcile_version), not per repo.

    if _LIFECYCLE_TEST_BARRIER is not None:
        _LIFECYCLE_TEST_BARRIER(repo)

    # === Lifecycle post-write: ledger, pending, stamp (ADR-0056, C1/H1/H2) ===
    lifecycle_counters: dict | None = None
    if lifecycle_on:
        lifecycle_counters = _commit_lifecycle(
            presence, writer, repo,
            scan=scan, rows=rows, transitions=transitions, gates=gates,
            current_head=current_head, diff_base=diff_base,
            owning_profile=owning_profile, retire=retire,
            written=write_names - prune_retry, rewrite_names=rewrite_names,
            attention=attention,
        )
        for name in sorted(prune_retry & present_names):
            presence.mark_needs_rewrite(repo["id"], name)
        _record_full_parses(
            presence, writer, repo,
            module_health=module_health if modules_by_version else {},
            run_id=run_id if modules_by_version else None,
            scan_ok=gates.scan_ok, head=current_head, present_names=present_names,
            embedded_at=embedded_at if modules_by_version else None,
        )
        if prune_report is not None:
            lifecycle_counters["gates_tripped"].extend(prune_report["gates_tripped"])
            lifecycle_counters["entity_prune"] = {
                k: v for k, v in prune_report.items() if k != "attention"
            }
    if presence is not None:
        _write_attention(presence, repo, attention, url)
    elif attention:
        for message in attention:
            _logger.warning("Repo %s: lifecycle: %s", url, message)
    # === End lifecycle post-write ===

    # Test-node GC (M12): explicit, not behind any flag; needs a trusted,
    # complete scan (a degraded scan would drop live modules' tests) and is
    # skipped under --no-retire (which deletes nothing).
    if retire and gates.scan_ok and present_names:
        # A module whose manifest does not parse is kept as it is (E2E-D1):
        # its test nodes are live too.
        test_live = {v: set(names) for v, names in live_module_names_by_version.items()}
        test_live.setdefault(scan.odoo_version, set()).update(
            n for n, ex in scan.excluded.items()
            if ex.reason == EXCLUSION_UNPARSEABLE
        )
        _gc_stale_test_nodes(
            writer, {v: sorted(n) for v, n in test_live.items()}, test_results, repo_root_name,
        )

    # Observability summary log (M7 C5) - one line per repo, readable by admins.
    _logger.info(
        "Indexer run: %d modules, %d embed calls, %d rows written",
        counters["modules"],
        counters.get("embed_calls", 0),
        counters["embeddings"],
    )

    # === On full success (W2-4): advance head_sha AFTER all writes ===
    # Any exception above prevents this, preserving last_head so the next run
    # retries the same diff (or full reindex).
    if current_head and pg_conn is not None:
        _pipeline.repo_store().update_repo_head_sha(repo["id"], current_head)
    # =====================================================================

    # === Cross-repo dep propagation (M7 W14) ===
    # Only on incremental runs (diff-based): query Neo4j for modules in OTHER
    # repos that DEPENDS_ON the re-written modules and NULL their
    # repos.head_sha so they are re-indexed on the next run. Full reindex skips
    # this - it already re-evaluates everything. Dependents of RETIRED modules
    # are reset by the reconcile, before the delete removes the edges.
    _is_incremental = diff_base is not None and not sync_only
    if _is_incremental and pg_conn is not None:
        changed_module_names: set[str] = {
            mod_name
            for mods in modules_by_version.values()
            for mod_name in mods
        }
        if changed_module_names:
            from src.indexer.cross_repo import find_dependent_repos
            dep_repo_basenames = find_dependent_repos(
                writer.driver, odoo_version, changed_module_names,
            )
            # Exclude the repo we just indexed (its head_sha was just updated).
            dep_repo_basenames = [b for b in dep_repo_basenames if b != repo_root_name]
            if dep_repo_basenames:
                dep_repo_ids = _pipeline.repo_store().get_repo_ids_by_local_path_basenames(
                    dep_repo_basenames,
                    odoo_version,
                )
                # Warn if more IDs than basenames: two repos share a basename
                # AT THE SAME odoo_version (e.g. /srv/odoo and /home/a/odoo both
                # basename 'odoo' in the same version's profiles). Both get reset
                # - over-eager but safe. The cross-VERSION leak (same basename at
                # a different version) is now filtered out by the odoo_version
                # predicate. See ADR-0007 W14 note.
                if len(dep_repo_ids) > len(dep_repo_basenames):
                    _logger.warning(
                        "Cross-repo dep propagation: basename collision detected — "
                        "%d repo IDs returned for %d basenames (%s). "
                        "All matching repos will be reset (safe, over-eager). "
                        "See ADR-0007 W14 for fix path.",
                        len(dep_repo_ids),
                        len(dep_repo_basenames),
                        ", ".join(sorted(dep_repo_basenames)),
                    )
                if dep_repo_ids:
                    n_reset = _pipeline.repo_store().reset_head_sha(dep_repo_ids)
                    _logger.info(
                        "Cross-repo dep propagation: reset head_sha on %d dependent repo(s) "
                        "(changed modules: %s)",
                        n_reset,
                        ", ".join(sorted(changed_module_names)),
                    )
    # === End cross-repo dep propagation ===

    if lifecycle_counters is not None:
        counters["lifecycle"] = lifecycle_counters
    return counters


def _commit_lifecycle(
    presence,
    writer: IndexWriterProtocol,
    repo: dict,
    *,
    scan,
    rows: list[dict],
    transitions,
    gates,
    current_head: str,
    diff_base: str | None,
    owning_profile: str,
    retire: bool,
    written: set[str],
    rewrite_names: list[str],
    attention: list[str],
) -> dict:
    """Ledger half of ``_index_repo`` (after the graph writes succeeded).

    Commits the observed rows, flags absent names pending with their evidence,
    stamps the Module nodes, and decides whether ``presence_head_sha`` may
    advance. Appends operator messages to *attention*. Returns the
    ``lifecycle`` counters.
    """
    from src.db.module_presence import RetireEvidence
    from src.db.module_presence import Successor as LedgerSuccessor
    from src.indexer import lifecycle

    repo_id = repo["id"]
    local_path = repo["local_path"]
    scan_version = scan.odoo_version
    observed = _observed_modules(scan)
    observed_names = {o.name for o in observed}
    presence.commit_observed(
        repo_id,
        profile_name=owning_profile,
        odoo_version=scan_version,
        head_sha=current_head,
        observed=observed,
    )
    done_rewrites = sorted(set(rewrite_names) & written)
    if done_rewrites:
        presence.clear_needs_rewrite(repo_id, done_rewrites)

    absent = transitions.retired
    pending: list[str] = []
    if absent:
        changes = (
            _incremental.compute_manifest_changes(Path(local_path), diff_base, current_head)
            if diff_base and diff_base != current_head else []
        )
        successors = lifecycle.pick_successors(transitions, changes, scan)
        rows_by_name = {r["name"]: r for r in rows}
        evidence: dict = {}
        ledger_successors: dict = {}
        for t in absent:
            row = rows_by_name.get(t.name, {})
            found, git_successor = lifecycle.removal_evidence(
                local_path, t.path or row.get("path") or t.name,
                [row.get("manifest_file") or _MANIFEST_NAMES[0]],
                observed_names,
            )
            if found is not None:
                evidence[t.name] = RetireEvidence(found.sha, found.date, found.subject)
            chosen = successors.get(t.name) or git_successor
            if chosen is not None:
                ledger_successors[t.name] = LedgerSuccessor(chosen.names, chosen.source)
        if not gates.retire_allowed:
            blocked_by = lifecycle.BLOCKED_GATE_PREFIX + ",".join(gates.tripped)
        elif not retire:
            blocked_by = lifecycle.BLOCKED_NO_RETIRE
        else:
            blocked_by = None
        pending = presence.mark_retire_pending(
            repo_id, [t.name for t in absent], "absent",
            evidence=evidence, successors=ledger_successors, blocked_by=blocked_by,
        )
        _logger.info(
            "Repo %s: %d module(s) no longer shipped, pending retirement%s: %s",
            repo.get("url", local_path), len(pending),
            f" (blocked: {blocked_by})" if blocked_by else "",
            ", ".join(pending),
        )

    present = sorted(scan.present_names())
    stamp_short = False
    if present:
        owners = presence.present_owner_basenames(scan_version, present)
        matched = writer.stamp_module_presence(
            scan_version,
            [
                {
                    "name": n,
                    "repos": owners.get(n),
                    "version_mismatch": scan.module(n).version_mismatch,
                    "version_raw": scan.module(n).version_raw or None,
                }
                for n in present
            ],
            current_head,
        )
        if matched < len(present):
            stamp_short = True
            attention.append(
                f"{len(present) - matched} present module(s) have no graph node after "
                "the write (concurrent retire?); ledger not marked synced, the next run "
                "re-writes them"
            )

    held = (not gates.retire_allowed) or stamp_short or (bool(pending) and not retire)
    synced = False
    deferred_head: str | None = None
    if not held and not pending:
        presence.mark_presence_synced(repo_id, current_head)
        synced = True
    elif not held:
        deferred_head = current_head
    return {
        "odoo_version": scan_version,
        "gates_tripped": list(gates.tripped),
        "pending": pending,
        "attention": list(attention),
        "presence_synced": synced,
        "presence_deferred_head": deferred_head,
    }
