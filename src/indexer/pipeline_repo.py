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
import logging
import subprocess
import sys
from pathlib import Path

from src.indexer import incremental as _incremental
from src.indexer import (
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
      concurrent retire or a failed write).
    - ``path_drift``: the node was last written by this repo but its ``path`` is
      not the registry winner. ``kind``: ``shadowed`` (a same-name loser dir,
      the posbox ``point_of_sale`` stub, F15), ``untracked`` (a git-untracked
      copy such as ``.odoo-ai/...``, F7) or ``stale``; with ``indexed_path`` and
      ``winner_path`` (repo-relative).
    - ``repo_drift``: the node names another repo but only this repo's profile
      owns it and the ledger shows no other present owner (``indexed_repo``).

    The index path re-writes these on a plain sync / incremental run, so drift
    left by older code heals without ``--full``; the re-parse re-stamps the
    children and the entity prune drops those only the old path produced.
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
            out[name] = {"reason": REWRITE_NO_NODE}
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
) -> tuple[dict, list]:
    """Parse the given modules and write every node + embedding they produce.

    Returns ``(counters, test_results)``; ``test_results`` feeds the test-node GC.
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

    # Pre-flight: check whether embedding is possible (once, not per module).
    embed_enabled = pg_conn is not None and embedder is not None
    if embed_enabled:
        from src.db.migrate import _vector_extension_available
        embed_enabled = _vector_extension_available(pg_conn)
    if embed_enabled:
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

            # XML views (ir.ui.view records) — rng_root enables version-exact
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

            # JS graph extraction — patches and OWL components
            js_graph = parser_js.parse_module_graph(info)
            js_graph_results.append(js_graph)
            total_js_patches += len(js_graph.patches)
            total_owl_comps += len(js_graph.components)

            # CSS/SCSS/LESS parsing — stylesheet nodes + embeddings (WI-A1, ADR-0025; RP WI-3)
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

            # Semantic embeddings — optional, skipped when pg_conn/embedder absent,
            # pgvector extension is not installed, or version could not be resolved.
            if embed_enabled and version != "unknown":
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
                # directly — single source of truth, no split-brain.
                embed_calls = write_module_embeddings(
                    mod_name, version, chunks, embedder,
                    profile_name=owning_profile,
                )
                total_embeddings += len(chunks)
                total_embed_calls += embed_calls

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
    and reconciles the scan with the ``module_presence`` ledger; nothing is
    ever deleted here - retirement is decided by ``reconcile.reconcile_version``
    after the run's repos were indexed.

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
    - Signals the operator must see (version rule, gate trips, git refusing
      the repo, missing ``origin/<branch>`` ref, stamp shortfall) replace
      ``repos.lifecycle_attention``; a clean run clears it.

    ``lifecycle`` counters: ``odoo_version`` (ledger key version),
    ``gates_tripped`` (gate ids), ``pending`` (names flagged this run),
    ``attention`` (messages), ``presence_synced`` (bool),
    ``presence_deferred_head`` (sha or None).

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
    from src.git_utils import head_matches_remote_branch, remote_branch_ref_exists
    from src.indexer import lifecycle
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

    head_unchanged = bool(current_head) and current_head == last_head
    if not full_reindex and head_unchanged and (
        presence is None or (presence_head == current_head and not rewrite_names)
    ):
        _logger.info(
            "Repo %s unchanged (HEAD %s) - skipping reindex", url, current_head[:8],
        )
        return dict(_EMPTY_COUNTERS)

    diff_base: str | None = None if full_reindex else last_head
    if diff_base and current_head and not head_unchanged and not _incremental.is_ancestor(
        repo_path, diff_base, current_head
    ):
        _logger.warning(
            "Repo %s: force-push or history rewrite detected "
            "(stored %s not ancestor of HEAD %s) - falling back to full reindex",
            url, diff_base[:8], current_head[:8],
        )
        diff_base = None
    sync_only = not full_reindex and head_unchanged
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
    branch: str | None = repo.get("branch")
    attention: list[str] = list(scan.attention)
    trusted = head_matches_remote_branch(repo_path, branch)
    if scan.tracked_paths is None:
        attention.append(
            "git tracking unavailable (not a git work tree, no commit yet, or git "
            "refused the repository, e.g. safe.directory ownership): scan untrusted, "
            "no module retired"
        )
    elif branch and not remote_branch_ref_exists(repo_path, branch):
        attention.append(
            f"no origin/{branch} ref: scan trust falls back to the checked-out branch name"
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
    gates = lifecycle.apply_gates(
        transitions, scan, trusted=trusted, allow_mass_retire=allow_mass_retire,
    )
    attention.extend(gates.reasons)
    for reason in gates.reasons:
        _logger.warning("Repo %s: lifecycle gate: %s", url, reason)

    heal_names: set[str] = set()
    if lifecycle_on and present_names and diff_base is not None:
        heal = modules_needing_rewrite(writer, presence, repo, scan, owning_profile)
        heal_names = set(heal)
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
    if diff_base is None:
        write_names = set(present_names)
    elif sync_only:
        write_names = set()
    else:
        changed_rel_paths = _incremental.compute_changed_module_paths(
            repo_path, diff_base, current_head,
        )
        # convert relative paths to absolute to match ModuleInfo.path
        changed_abs_paths = {str(repo_path / rel) for rel in changed_rel_paths}
        write_names = {
            name
            for mods in scan.modules.values()
            for name in _incremental.filter_modules_by_changed(mods, changed_abs_paths)
        }
        _logger.info(
            "Repo %s: incremental - %d/%d modules changed",
            url, len(write_names), total_before,
        )
    write_names |= (set(rewrite_names) | heal_names) & present_names
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
    if modules_by_version:
        counters, test_results = _parse_and_write(
            modules_by_version,
            writer=writer, repo=repo, repo_path=repo_path, rng_root=rng_root,
            pg_conn=pg_conn, embedder=embedder, progress=progress,
            profiles_arr=_profiles_arr, owning_profile=owning_profile,
        )

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
            written=write_names, rewrite_names=rewrite_names, attention=attention,
        )
    if presence is not None:
        try:
            if attention:
                presence.set_lifecycle_attention(repo["id"], "; ".join(attention))
            else:
                presence.clear_lifecycle_attention(repo["id"])
        except Exception:  # noqa: BLE001 - never fail a repo over the attention column
            _logger.exception("Repo %s: could not write lifecycle_attention", url)
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
