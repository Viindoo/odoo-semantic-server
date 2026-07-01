# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/pipeline_repo.py
"""Per-repo indexing stage (B6 split from pipeline.py — no behavior change).

Houses the per-repo scan -> parse -> write -> embed unit that ``index_profile``
drives (sequentially or via its ThreadPoolExecutor worker):

    _owning_profiles(repo, profile_name, repo_root_name) -> list[str]
    _index_repo(repo, writer, ...) -> per-repo counter dict

The orchestrator (``index_profile`` / ``index_all`` / ``index_core``), the lock
infrastructure and the production connection helpers stay in ``pipeline.py``.
``pipeline.py`` re-exports ``_owning_profiles`` and ``_index_repo`` at the bottom
of its body so existing call sites and test patch targets
(``src.indexer.pipeline._index_repo`` / ``_owning_profiles``) keep working.

Patch-visibility contract (why some names are referenced through ``pipeline``):
The test suite monkeypatches several collaborators on the *parent* module
namespace and then calls ``_index_repo`` — e.g.
``patch("src.indexer.pipeline.build_registry", ...)``,
``...topological_sort``, ``...repo_store``. These three are *function* bindings:
a ``from ... import build_registry`` binding in THIS module would NOT see a patch
applied to the ``pipeline`` namespace. So ``_index_repo`` resolves
``build_registry`` / ``topological_sort`` / ``repo_store`` through the ``pipeline``
module object at call time (a deferred, cold-import-safe ``from . import
pipeline``). By contrast ``_incremental`` and the ``parser_*`` submodules are
*module objects* shared by identity across both namespaces, so patching
``pipeline.parser_python.parse_module`` is visible here regardless of the binding
path — those stay as ordinary module-level imports.
"""
import contextlib
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
from src.indexer.models import AssetParseResult, StylesheetInfo, ViewParseResult
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
) -> dict:
    """Index a single repo dict (from get_repos_for_profile).

    Returns per-repo counters: {modules, views, qweb, embeddings}.
    Pass pg_conn + embedder to also write semantic embeddings to pgvector.
    Set progress=True to show tqdm progress bar during module iteration.
    profile_name is stamped on every EmbeddingChunk written so re-indexing
    one profile does not erase another profile's chunks for the same module.

    core_rng_root: Path to <odoo_core_root>/odoo/addons/base/rng/ (or the
        openerp/ equivalent for v8/v9).  When the repo itself contains the RNG
        directory it is used directly; *core_rng_root* is the fallback for
        addon-only repos whose views still need version-exact RNG validation.
        None → RelaxNG validation is silently skipped (no false positives).

    Incremental behaviour (M6 W2-4):
    - Compares current git HEAD to repos.head_sha (stored from last run).
    - Equal → zero-cost skip.
    - Force-push detected (stored sha not ancestor of HEAD) → full reindex.
    - Otherwise → diff-filter scan results to changed modules only.
    - head_sha advanced to current HEAD ONLY after all writes succeed.
    - full_reindex=True bypasses the skip + diff filter (use to clean stale nodes).

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
    # Resolve the parent orchestrator module at call time. ``build_registry``,
    # ``topological_sort`` and ``repo_store`` are referenced through it so that
    # test patches applied to ``src.indexer.pipeline.<name>`` are honoured (see
    # the module docstring). Deferred (function-local) import keeps a cold
    # ``import src.indexer.pipeline_repo`` cycle-free.
    from src.indexer import pipeline as _pipeline

    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]

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

    # === Incremental check (W2-4) ===
    current_head = _incremental.get_repo_head(repo_path)
    last_head: str | None = None

    if current_head is None:
        _logger.warning(
            "Cannot determine HEAD for repo %s — full reindex without head_sha tracking",
            repo["url"],
        )

    if not full_reindex and pg_conn is not None:
        last_head = _pipeline.repo_store().get_repo_head_sha(repo["id"])

        if current_head and last_head and current_head == last_head:
            _logger.info(
                "Repo %s unchanged (HEAD %s) — skipping reindex",
                repo.get("url", local_path), current_head[:8],
            )
            return {
                "modules": 0,
                "views": 0,
                "qweb": 0,
                "embeddings": 0,
                "js_patches": 0,
                "owl_comps": 0,
            }

        if last_head and current_head and not _incremental.is_ancestor(
            repo_path, last_head, current_head
        ):
            _logger.warning(
                "Repo %s: force-push or history rewrite detected "
                "(stored %s not ancestor of HEAD %s) — falling back to full reindex",
                repo.get("url", local_path),
                last_head[:8],
                current_head[:8],
            )
            last_head = None  # force full reindex below
    elif full_reindex:
        last_head = None  # ensure diff filter is skipped
    # === End incremental check ===

    # build_registry expects list[tuple[repo_path, odoo_version]].
    # Pass repo_url + repo_id for A2c provenance stamping on every ModuleInfo.
    registry = _pipeline.build_registry(
        [(local_path, odoo_version)],
        repo_url=repo.get("url"),
        repo_id=repo.get("id"),
    )
    # registry: {odoo_version: {module_name: ModuleInfo}}
    modules_by_version = registry  # alias for clarity

    # Collect live_paths (all module paths found on disk) BEFORE incremental filter.
    # GC compares these against Neo4j Module nodes to detect stale (renamed/removed) modules.
    # Must use the FULL scan (not the incremental-filtered subset) so GC sees ALL live dirs.
    # ADR-0037: relativize to repo root so live_paths matches the relative form now
    # stored in Module.path — a mismatch would mark every node stale and delete the graph.
    live_paths: set[str] = {
        info.relative_path(info.path)
        for mods in registry.values()
        for info in mods.values()
    }
    # Live module NAMES per version (registry keys) — used by gc_stale_test_nodes,
    # whose stale predicate is `tm.module IN $live_modules` (module names, not paths,
    # since TestClass/TestMethod nodes carry module NAME not relative path). Computed
    # from the FULL scan (pre-incremental) so a --full GC sees every live module.
    live_module_names_by_version: dict[str, list[str]] = {
        ver: list(mods.keys()) for ver, mods in registry.items()
    }
    # Repo dir name (m.repo in Neo4j) — derived the same way registry.py does it.
    repo_root_name: str = Path(local_path).name

    # F4 — single source of truth for this repo's OWNING profile. Compute ONCE
    # here and feed BOTH the Neo4j writer (`profiles=`) AND the pgvector write
    # (`profile_name=`) from it, so the two stores can never diverge by
    # construction (Neo4j↔pgvector owner split-brain). Previously Neo4j stamped
    # _owning_profiles(repo,...) while pgvector stamped the run `profile_name`
    # directly — equal today (get_repos_for_profile returns no `profile_name`
    # column) but would silently diverge if a future caller fed repos carrying
    # their own `profile_name` (e.g. get_ancestor_repos). _owning_profiles raises
    # on a falsy owner (F2), so `owning_profile` below is always a real name.
    _profiles_arr: list[str] = _owning_profiles(repo, profile_name, repo_root_name)
    owning_profile: str = _profiles_arr[0]

    # === Incremental filter (W2-4) ===
    if last_head and current_head and not full_reindex:
        changed_rel_paths = _incremental.compute_changed_module_paths(
            repo_path, last_head, current_head,
        )
        # convert relative paths to absolute to match ModuleInfo.path
        changed_abs_paths = {str(repo_path / rel) for rel in changed_rel_paths}

        filtered_by_version: dict[str, dict] = {}
        total_before = sum(len(mods) for mods in modules_by_version.values())
        for ver, mods in modules_by_version.items():
            filtered_by_version[ver] = _incremental.filter_modules_by_changed(
                mods, changed_abs_paths,
            )
        total_after = sum(len(mods) for mods in filtered_by_version.values())

        _logger.info(
            "Repo %s: incremental — %d/%d modules changed",
            repo.get("url", local_path), total_after, total_before,
        )

        if total_after == 0:
            _logger.info(
                "Repo %s: no module dirs changed (only meta files) — "
                "head_sha will still be advanced",
                repo.get("url", local_path),
            )
            if current_head and pg_conn is not None:
                _pipeline.repo_store().update_repo_head_sha(repo["id"], current_head)
            return {
                "modules": 0,
                "views": 0,
                "qweb": 0,
                "embeddings": 0,
                "js_patches": 0,
                "owl_comps": 0,
            }

        modules_by_version = filtered_by_version
    # === End incremental filter ===

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
                # (owning_profile == _profiles_arr[0]), not the run profile_name
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
    # would be hidden by the choke's all(). `_profiles_arr` is the SAME list
    # computed once near the top of the function (F4 single source of truth) and
    # used for the pgvector write above, so the two stores cannot diverge. See
    # _owning_profiles() for the full rationale + the F-2/F-6 non-empty guard.
    writer.write_results(py_results, profiles=_profiles_arr)
    # WI-D: write :AssetBundle nodes BEFORE views/qweb so the legacy
    # <template inherit_id="web.assets_backend"> extenders (written in the qweb
    # pass) resolve against the AssetBundle base nodes via EXTENDS_ASSET_BUNDLE
    # instead of emitting an unresolved warning (the ~13 A2 warnings, ADR-0052).
    writer.write_asset_results(asset_results, profiles=_profiles_arr)
    writer.write_view_results(view_results, profiles=_profiles_arr)
    # WI-1: write test surface nodes (TestClass/TestMethod) alongside model nodes.
    # test_results collected per-module inside the main loop (see below) then written here.
    # Era-gated dispatch is handled by parser_test.parse_module internally.
    if test_results:
        writer.write_test_results(test_results, profiles=_profiles_arr)
    # WI-3: write JsTestSuite nodes for frontend test files (Hoot/QUnit/tour).
    # js_test_suites accumulated per-module in the loop above.
    if js_test_suites:
        writer.write_js_test_results(js_test_suites, profiles=_profiles_arr)
    # WI-E (M11): write RelaxNG LintViolation nodes after View nodes exist.
    # ADR-0037: pass repo_root so file_path (a MERGE-key component) is stored
    # repo-relative — keeps it consistent with Stylesheet + the cleanup cypher.
    all_lint_violations = [v for vr in view_results for v in vr.lint_violations]
    writer.write_lint_violations(
        all_lint_violations, profiles=_profiles_arr, repo_root=repo_path,
    )
    writer.write_js_graph_results(js_graph_results, profiles=_profiles_arr)
    # WI-A1: write Stylesheet nodes (CSS + SCSS) after module writes.
    # ADR-0037: pass repo_root so Stylesheet.file_path + @import targets are
    # stored repo-relative (all stylesheets in this repo share one repo_root).
    # Pass repo_id so the :IMPORTS target MATCH is repo-scoped — without it two
    # repos at the same version sharing a relative path would cross-link.
    writer.write_stylesheets(
        all_stylesheet_infos, profiles=_profiles_arr, repo_root=repo_path,
        repo_id=repo.get("id"),
    )

    # === Module GC (M7 C4): delete stale Module nodes after successful writes ===
    # Risk gate: only run when scanner found ≥1 module to avoid data loss when
    # scanner fails silently (e.g. filesystem permission error, empty repo).
    if gc:
        if len(live_paths) >= 1:
            gc_deleted = writer.gc_stale_modules(repo_root_name, odoo_version, live_paths)
            if gc_deleted > 0:
                _logger.info(
                    "Module GC: deleted %d stale Module nodes for repo %s version %s",
                    gc_deleted, repo_root_name, odoo_version,
                )
            else:
                _logger.info(
                    "Module GC: no stale Module nodes found for repo %s version %s",
                    repo_root_name, odoo_version,
                )
        else:
            _logger.warning(
                "Module GC requested but scanner returned 0 modules — "
                "skipping to avoid data loss (repo %s version %s)",
                repo.get("url", local_path), odoo_version,
            )

        # Placeholder GC (ADR-0007 §D5 extension): delete inert __unresolved__
        # placeholder nodes that have accumulated in the graph.  Safe at any time
        # (server.py filters them at read time); running after module writes
        # maximises the chance that newly indexed parents already resolved some
        # of the pending placeholders so they will be absent from the graph.
        writer.gc_unresolved_placeholders(odoo_version)

        # AssetBundle orphan GC (graph MED-1 / integration LOW): AssetBundle is
        # version-global so it is NOT in the per-module delete cascade; reclaim
        # genuinely-unreferenced bundles here. Gated to --full only: on an
        # incremental run a bundle's sole contributor module may simply be absent
        # from the diff (not re-written), so it would look orphaned but is live.
        # On --full every live contribution is re-written first, so survivors are
        # real orphans.
        if full_reindex:
            writer.gc_orphan_asset_bundles(odoo_version)

        # Test-node GC (WI-1, MISSED-2): remove TestClass/TestMethod nodes whose
        # owning module no longer exists on disk (module-level) OR whose test FILE
        # was deleted inside a still-present module (file-level, M6). Risk-gated
        # identically to module GC above (only with >=1 live module) so a silent
        # empty scan never wipes the test surface. Same odoo_version scope.
        if len(live_paths) >= 1:
            # Live test FILE paths the parser actually emitted this run, grouped by
            # version (only from the changed-module subset on incremental runs).
            # A TestClass whose file_path is absent here (but module IS live and
            # was re-parsed) had its file deleted -> file-level prune removes orphan.
            live_test_files_by_version: dict[str, set[str]] = {}
            # Defect I fix: live_modules_for_file_gc = ONLY modules actually re-parsed
            # this run (test_results is the changed-module subset on incremental).
            # Using the full live_module_names would mark unchanged modules' test files
            # as absent (they were never re-emitted) and delete valid nodes.
            live_modules_for_file_gc_by_ver: dict[str, set[str]] = {}
            for _tr in test_results:
                _ver = _tr.module.odoo_version
                _bucket = live_test_files_by_version.setdefault(_ver, set())
                live_modules_for_file_gc_by_ver.setdefault(_ver, set()).add(
                    _tr.module.name
                )
                for _tc in _tr.test_classes:
                    if _tc.file_path:
                        _bucket.add(_tc.file_path)
            for _ver, _live_names in live_module_names_by_version.items():
                writer.gc_stale_test_nodes(
                    _ver, _live_names,
                    live_file_paths=sorted(live_test_files_by_version.get(_ver, set())),
                    # Defect H fix: scope both prune queries to this repo so
                    # another repo's same-named modules are never touched.
                    repo=repo_root_name,
                    # Defect I fix: file-level prune restricted to re-parsed modules.
                    live_modules_for_file_gc=sorted(
                        live_modules_for_file_gc_by_ver.get(_ver, set())
                    ),
                )
    # === End Module GC ===

    # NOTE: reconcile_same_name_inherits was moved from here to index_profile
    # (called once per version AFTER all repos are indexed) to avoid R redundant
    # full :Model label scans per profile run.  See PERF comment in index_profile.

    # Observability summary log (M7 C5) - one line per repo, readable by admins.
    _logger.info(
        "Indexer run: %d modules, %d embed calls, %d rows written",
        total_modules,
        total_embed_calls,
        total_embeddings,
    )

    # === On full success (W2-4): advance head_sha AFTER all writes ===
    # Must be the last statement — any exception above prevents this,
    # preserving last_head so next run retries the same diff (or full reindex).
    if current_head and pg_conn is not None:
        _pipeline.repo_store().update_repo_head_sha(repo["id"], current_head)
    # =====================================================================

    # === Cross-repo dep propagation (M7 W14) ===
    # Only on incremental runs (diff-based): collect the changed module names,
    # query Neo4j for modules in OTHER repos that DEPENDS_ON those modules, and
    # NULL their repos.head_sha so they are re-indexed on the next run.
    # Full reindex skips this — it already re-evaluates everything.
    _is_incremental = (
        last_head is not None
        and current_head is not None
        and not full_reindex
    )
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
                )
                # Warn if more IDs than basenames: two repos share a basename
                # (e.g. /srv/odoo and /home/a/odoo both have basename 'odoo').
                # Both get reset — over-eager but safe. See ADR-0007 W14 note.
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
    }
