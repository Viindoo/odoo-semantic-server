# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/registry.py
import ast
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.constants import LEGACY_ERA_MAX_MAJOR, license_policy_action
from src.git_utils import list_tracked_manifests

from .models import (
    EXCLUSION_INSTALLABLE_FALSE,
    EXCLUSION_LICENSE_SKIP,
    EXCLUSION_UNPARSEABLE,
    ExcludedModule,
    ModuleInfo,
    RegistryScan,
)
from .parser_python import (
    _derive_copyright_owner,
    _detect_module_edition,
    _detect_viindoo_equivalent,
    _normalize_author,
    _resolve_effective_license,
)
from .parser_util import parse_external_source
from .scanner import get_git_branch, get_module_commit_sha

_logger = logging.getLogger(__name__)

# --- ManifestFinder Protocol (M4.5 WI1.1, per ADR-0002) --------------------
# Odoo v8/v9 use __openerp__.py instead of __manifest__.py.
# Pluggable finder keeps the rest of the pipeline version-agnostic.

class ManifestFinder(Protocol):
    def find(self, repo_path: str) -> list[str]: ...


def _scan(repo_path: str, filename: str) -> list[str]:
    root = Path(repo_path)
    results = []
    for p in root.rglob(filename):
        if _SKIP_DIRS & set(p.relative_to(root).parts[:-1]):
            continue
        results.append(str(p))
    return results


def _coerce_int(value) -> int | None:
    """Best-effort int coercion of a manifest value; None on anything non-numeric.

    Rejects bool explicitly: `isinstance(True, int)` is True in Python, but a
    manifest `'sequence': True` must NOT silently become 1. A malformed value
    (e.g. `'sequence': '1.5'`, a list, or junk text) returns None rather than
    raising - one odd third-party/marketplace manifest must never crash the
    whole index run (the per-module loop has no try/except around this).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _coerce_float(value) -> float | None:
    """Best-effort float coercion of a manifest value; None on anything non-numeric.

    Same defensive contract as _coerce_int: rejects bool (a manifest
    `'price': True` must NOT become 1.0) and swallows ValueError/TypeError so a
    malformed `'price'` (e.g. `'free'`, `'13,5'`, or a list) yields None instead
    of crashing the index run.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class ModernManifestFinder:
    """Locate __manifest__.py (Odoo v10+)."""

    def find(self, repo_path: str) -> list[str]:
        return _scan(repo_path, "__manifest__.py")


class LegacyManifestFinder:
    """Locate __openerp__.py (Odoo v8/v9)."""

    def find(self, repo_path: str) -> list[str]:
        return _scan(repo_path, "__openerp__.py")


class DualManifestFinder:
    """Locate both __manifest__.py and __openerp__.py (Odoo v10 transition era).

    Odoo v10 standardised on __manifest__.py, yet a handful of legacy l10n
    modules still ship only __openerp__.py (carried over from v9). Scanning
    just one filename silently drops the other group from the graph.

    Dedupe rule: a module directory is indexed once. When a directory holds
    BOTH files we prefer the modern __manifest__.py (do not double-index the
    same module, and never pick the legacy file when the modern one exists).
    Implementation: collect modern manifests first, record their parent
    directories, then add only those legacy manifests whose parent directory
    is not already covered by a modern manifest.
    """

    def find(self, repo_path: str) -> list[str]:
        modern = _scan(repo_path, "__manifest__.py")
        modern_dirs = {str(Path(p).parent) for p in modern}
        legacy = [
            p
            for p in _scan(repo_path, "__openerp__.py")
            if str(Path(p).parent) not in modern_dirs
        ]
        return modern + legacy


def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    """Dispatch finder by Odoo major version. Defaults to Modern when unknown.

    - major <= LEGACY_ERA_MAX_MAJOR (v8/v9) → Legacy (__openerp__.py only)
    - major == 10                            → Dual (both, dedupe to modern)
    - major >= 11                            → Modern (__manifest__.py only)
    - unknown / unparseable                  → Modern (safe default)
    """
    era = _manifest_era(odoo_version)
    if era == "legacy":
        return LegacyManifestFinder()
    if era == "dual":
        return DualManifestFinder()
    return ModernManifestFinder()


# --- Regex fallback for legacy __openerp__.py with Python 2 syntax ---------
_RE_NAME = re.compile(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_RE_VERSION = re.compile(r"['\"]version['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_RE_DEPENDS = re.compile(r"['\"]depends['\"]\s*:\s*\[([^\]]*)\]", re.DOTALL)
_RE_INSTALLABLE = re.compile(r"['\"]installable['\"]\s*:\s*(True|False)")


def _regex_extract_manifest(source: str) -> dict:
    """Best-effort regex extract for legacy manifests that fail ast.parse.
    Used only as fallback when Python 2 syntax outside the dict trips up Python 3 parser.
    """
    result: dict = {}
    if m := _RE_NAME.search(source):
        result['name'] = m.group(1)
    if m := _RE_VERSION.search(source):
        result['version'] = m.group(1)
    if m := _RE_DEPENDS.search(source):
        items = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
        result['depends'] = items
    if m := _RE_INSTALLABLE.search(source):
        result['installable'] = m.group(1) == 'True'
    return result


def parse_manifest(manifest_path: str) -> dict:
    """Read manifest file (__manifest__.py or __openerp__.py) → dict.

    Iterates tree.body (top-level statements) only, to avoid catching nested
    dicts like 'external_dependencies', 'assets', etc.
    Falls back to regex extraction when ast.parse fails (Python 2 v8/v9 syntax).
    A file that cannot be read returns ``{}`` like an unparseable one; the
    lifecycle scan tells the two apart (:func:`build_registry_scan`).
    """
    try:
        source = read_manifest_source(manifest_path)
    except OSError:
        return {}
    return parse_manifest_source(source, manifest_path)


def read_manifest_source(manifest_path: str) -> str:
    """Manifest text; raises OSError (permission, I/O) when it cannot be read."""
    return Path(manifest_path).read_text(encoding='utf-8', errors='ignore')


def parse_manifest_source(source: str, manifest_path: str) -> dict:
    """Manifest dict from its text (see :func:`parse_manifest`); ``{}`` when unparseable."""
    try:
        # External manifest source — scope away SyntaxWarning noise, pass the real
        # path so any diagnostic is attributable (not <unknown>). See parser_util.
        tree = parse_external_source(source, filename=manifest_path)
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
                return ast.literal_eval(stmt.value)
    except (SyntaxError, ValueError):
        # Python 2-only syntax outside the dict — try regex.
        return _regex_extract_manifest(source)
    except Exception:
        return {}
    return {}


# --- Version rule (owner decision, ADR-0056) -------------------------------
# The Odoo version of every module of a repo is the version of its STANDARD
# branch name (X.Y, e.g. 17.0 / 17.1 / 18.0; `saas-17.1` normalizes to 17.1).
# A non-standard branch (main, feature/x) falls back to the profile version and
# raises an attention signal; a standard branch that differs from the profile
# version also raises one (the branch still wins). The manifest `version` never
# decides the key: a long-form value (X.Y.a.b[.c]) whose X.Y differs from the
# branch version keeps the branch version, sets version_mismatch and keeps the
# raw string. Only when neither a standard branch nor a profile version exists
# does the long-form prefix place the module (legacy scan_repos callers).
_BRANCH_VERSION_RE = re.compile(r"^(?:saas-)?(\d+\.\d+)$")
_LONG_FORM_VERSION_RE = re.compile(r"^(\d+\.\d+)\.\d+\.\d+")
# Same-name conflict preference: a manifest version with at least three numeric
# parts (short X.Y.Z or long form) beats one without.
_SEMVER_PREFIX_RE = re.compile(r"^\d+\.\d+\.\d+")


def normalize_branch_version(branch: str | None) -> str | None:
    """Odoo version named by a git branch, or None when the branch is not standard.

    ``17.0`` -> ``17.0``; ``saas-17.1`` -> ``17.1``; ``refs/heads/18.0`` ->
    ``18.0``; ``main`` / ``17.0-fix`` / ``""`` / None -> None.
    """
    if not branch:
        return None
    candidate = branch.strip().removeprefix("refs/heads/")
    m = _BRANCH_VERSION_RE.match(candidate)
    return m.group(1) if m else None


def _real_version(version: str | None) -> str | None:
    """Treat empty / ``unknown`` profile versions as absent."""
    if not version or version == "unknown":
        return None
    return version


@dataclass(frozen=True)
class RepoVersion:
    """Repo-level outcome of the version rule.

    ``odoo_version`` is None only when there is neither a standard branch nor a
    profile version (manifests then place themselves by their long-form prefix).
    ``attention`` holds the operator-facing signals (non-standard branch, branch
    vs profile version mismatch); an empty tuple means the repo is consistent.
    """
    odoo_version: str | None
    branch: str | None
    branch_version: str | None
    attention: tuple[str, ...] = ()


def resolve_repo_version(
    repo_path: str,
    *,
    branch: str | None = None,
    profile_version: str | None = None,
) -> RepoVersion:
    """Apply the version rule for one repo.

    ``branch`` is the REGISTERED branch (``repos.branch``); when None the checked
    out branch of ``repo_path`` is used. ``profile_version`` is the owning
    profile's ``odoo_version``.
    """
    if branch is None:
        branch = get_git_branch(repo_path)
    profile_version = _real_version(profile_version)
    branch_version = normalize_branch_version(branch)
    if branch_version:
        attention: tuple[str, ...] = ()
        if profile_version and profile_version != branch_version:
            attention = (
                f"branch {branch!r} is Odoo {branch_version} but the profile "
                f"version is {profile_version}; modules are indexed at "
                f"{branch_version}",
            )
        return RepoVersion(branch_version, branch, branch_version, attention)
    if profile_version:
        label = repr(branch) if branch else "(none)"
        return RepoVersion(
            profile_version, branch, None,
            (
                f"branch {label} is not a standard Odoo version branch (X.Y); "
                f"modules are indexed at the profile version {profile_version}",
            ),
        )
    return RepoVersion(None, branch, None, ())


def _place_version(version_raw: str, repo_version: str | None) -> str:
    """Module version key: the repo version, else the long-form prefix, else
    ``unknown``."""
    if repo_version:
        return repo_version
    m = _LONG_FORM_VERSION_RE.match(version_raw or "")
    return m.group(1) if m else "unknown"


def manifest_version_mismatch(version_raw: str | None, odoo_version: str) -> bool:
    """True when *version_raw* is long-form and its X.Y prefix != *odoo_version*.

    Short versions (``1.0.0``, ``1.0``) never mismatch: Odoo prefixes them with
    the series at install time.
    """
    m = _LONG_FORM_VERSION_RE.match(str(version_raw or ""))
    return bool(m) and m.group(1) != odoo_version


def resolve_odoo_version(
    manifest_version: str,
    repo_path: str,
    *,
    branch: str | None = None,
    profile_version: str | None = None,
) -> str:
    """Resolve the Odoo version key of one manifest under the version rule.

    Priority: standard branch name (registered ``branch``, else the checked-out
    branch of ``repo_path``) > ``profile_version`` > long-form manifest prefix >
    ``"unknown"``. Use ``manifest_version_mismatch`` for the mismatch flag and
    ``resolve_repo_version`` for the attention signals.
    """
    repo = resolve_repo_version(
        repo_path, branch=branch, profile_version=profile_version,
    )
    return _place_version(str(manifest_version or ""), repo.odoo_version)


# --- Manifest dispatch shared by the finder and the tracked set -------------
_SKIP_DIRS = frozenset({".git", "node_modules"})


def _manifest_era(odoo_version: str) -> str:
    """``legacy`` (v8/v9), ``dual`` (v10) or ``modern`` (v11+, and unknown)."""
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return "modern"
    if major <= LEGACY_ERA_MAX_MAJOR:
        return "legacy"
    if major == 10:
        return "dual"
    return "modern"


def filter_manifest_paths(paths: set[str], odoo_version: str) -> set[str]:
    """Filter repo-relative manifest paths through the version dispatch.

    The SAME rule the finder applies on disk: legacy keeps ``__openerp__.py``,
    modern keeps ``__manifest__.py``, dual keeps one manifest per directory
    preferring ``__manifest__.py``. Paths under ``.git`` / ``node_modules`` are
    dropped, as the finder never looks there.
    """
    kept = {
        p for p in paths
        if not (_SKIP_DIRS & set(Path(p).parts[:-1]))
    }
    modern = {p for p in kept if Path(p).name == "__manifest__.py"}
    legacy = {p for p in kept if Path(p).name == "__openerp__.py"}
    era = _manifest_era(odoo_version)
    if era == "legacy":
        return legacy
    if era == "modern":
        return modern
    modern_dirs = {str(Path(p).parent) for p in modern}
    return modern | {p for p in legacy if str(Path(p).parent) not in modern_dirs}


def _find_manifests(repo_path: str, odoo_version: str = "") -> list[str]:
    """Find manifest files in repo, dispatching by version (v8/v9 → __openerp__.py)."""
    return get_manifest_finder(odoo_version).find(repo_path)


def _manifest_depth(rel_manifest: str) -> int:
    return len(Path(rel_manifest).parts)


def _build_module_info(
    manifest: dict,
    module_name: str,
    module_dir: Path,
    repo_root: Path,
    odoo_version: str,
    version_raw: str,
    effective_license: str | None,
    copyright_owner: str | None,
    license_notice: str | None,
    repo_url: str | None,
    repo_id: int | None,
) -> ModuleInfo:
    try:
        module_relpath = module_dir.relative_to(repo_root)
    except ValueError:
        module_relpath = module_dir
    commit_sha = get_module_commit_sha(repo_root, module_relpath)

    # A2b - manifest enrichment fields
    # auto_install may be bool OR list of trigger module names → coerce to bool
    _auto_install: bool = bool(manifest.get('auto_install', False))
    _application: bool = bool(manifest.get('application', False))
    _category: str | None = manifest.get('category') or None
    _summary: str | None = manifest.get('summary') or None
    # Issue #121 P2 - identity card raw fields. shortdesc = the human
    # display name (manifest 'name'); author coerced str|list -> str|None.
    _shortdesc: str | None = manifest.get('name') or None
    _author: str | None = _normalize_author(manifest)

    # sequence/price may be declared as str/int/junk in third-party
    # manifests - coerce defensively (None when absent, bool, or
    # non-numeric); never raise (would crash the whole index run).
    _ext_deps = manifest.get('external_dependencies') or {}

    return ModuleInfo(
        name=module_name,
        odoo_version=odoo_version,
        repo=repo_root.name,
        path=str(module_dir),
        depends=manifest.get('depends', []),
        version_raw=version_raw,
        version_mismatch=manifest_version_mismatch(version_raw, odoo_version),
        edition=_detect_module_edition(manifest, module_name, str(module_dir)),
        viindoo_equivalent_qname=_detect_viindoo_equivalent(module_name),
        commit_sha=commit_sha,
        license=effective_license,
        copyright_owner=copyright_owner,
        license_notice=license_notice,
        # A2b - manifest enrichment
        auto_install=_auto_install,
        application=_application,
        category=_category,
        summary=_summary,
        # Issue #121 P2 - identity card
        shortdesc=_shortdesc,
        author=_author,
        # Issue #121 (extended) - additional manifest metadata
        description=manifest.get('description') or None,
        website=manifest.get('website') or None,
        live_test_url=manifest.get('live_test_url') or None,
        demo_video_url=manifest.get('demo_video_url') or None,
        support=manifest.get('support') or None,
        sequence=_coerce_int(manifest.get('sequence')),
        old_technical_name=manifest.get('old_technical_name') or None,
        price=_coerce_float(manifest.get('price')),
        currency=manifest.get('currency') or None,
        external_python=list(_ext_deps.get('python') or []),
        external_bin=list(_ext_deps.get('bin') or []),
        # v17+ manifest `countries` key - module install-UI country filter.
        countries=list(manifest.get('countries') or []),
        # A2c - repo provenance
        repo_url=repo_url,
        repo_id=repo_id,
        # ADR-0037 - repo checkout root for path relativization at write time.
        repo_root=repo_root,
    )


def build_registry_scan(
    repo_path: str,
    profile_version: str | None = None,
    *,
    branch: str | None = None,
    repo_url: str | None = None,
    repo_id: int | None = None,
) -> RegistryScan:
    """Scan ONE repo checkout into its scan truth (ADR-0056).

    Truth is the set of git-TRACKED manifests, filtered through the same
    version dispatch as the finder (Legacy ``__openerp__.py`` v8-9, Dual v10 one
    per directory preferring ``__manifest__.py``, Modern ``__manifest__.py``
    v11+). Rules:

    - A manifest the finder sees but git does not track (ignored or untracked
      directories) is neither indexed nor counted live (``untracked``).
    - A tracked manifest the finder does not see makes the scan incomplete
      (``missing``); completeness compares PATHS, never names. So does a
      tracked manifest that exists but cannot be READ (permission or I/O
      error, ``unreadable``): the module is neither present nor excluded -
      a read fault says nothing about the module, unlike a manifest that was
      read and does not parse (``unparseable``).
    - A directory without a manifest is not a module; a module without
      ``__init__.py`` is still a module.
    - Each tracked manifest is present (indexable ModuleInfo) or excluded
      (``installable_false`` / ``license_skip`` / ``unparseable``).
    - Several copies of one name inside the repo resolve to ONE winner: a present
      copy beats an excluded one, then a manifest version with >= 3 numeric parts
      beats one without, then the shallower path, then the path order. Losers are
      recorded in ``shadowed``. (Real case: the posbox ``point_of_sale`` stub
      nested in core ``point_of_sale`` v12-v18.)
    - The version key follows the version rule (``resolve_repo_version``).

    When git tracking is unavailable (not a git repo, unborn HEAD) the working
    tree is the only truth: every finder manifest is used, ``tracked_paths`` is
    None and ``complete`` is True unless a manifest is unreadable.
    """
    repo_root = Path(repo_path)
    repo_ver = resolve_repo_version(
        repo_path, branch=branch, profile_version=profile_version,
    )
    dispatch_version = repo_ver.odoo_version or _real_version(profile_version) or ""
    for message in repo_ver.attention:
        _logger.warning("version rule %s: %s", repo_path, message)

    finder_paths = frozenset(
        str(Path(p).relative_to(repo_root))
        for p in _find_manifests(repo_path, dispatch_version)
    )
    tracked_raw = list_tracked_manifests(repo_root)
    tracked_paths: frozenset[str] | None = (
        None if tracked_raw is None
        else frozenset(filter_manifest_paths(tracked_raw, dispatch_version))
    )
    if tracked_paths is None:
        untracked: frozenset[str] = frozenset()
        missing: frozenset[str] = frozenset()
    else:
        untracked = finder_paths - tracked_paths
        missing = tracked_paths - finder_paths
    if untracked:
        _logger.warning(
            "tracked-manifest scan %s: %d untracked manifest(s) ignored (not indexed, "
            "not live), e.g. %s",
            repo_path, len(untracked), sorted(untracked)[:3],
        )
    if missing:
        _logger.warning(
            "tracked-manifest scan %s: %d tracked manifest(s) missing on disk - scan "
            "incomplete, e.g. %s",
            repo_path, len(missing), sorted(missing)[:3],
        )

    present_candidates: dict[str, list[tuple[str, ModuleInfo]]] = {}
    excluded_candidates: dict[str, list[ExcludedModule]] = {}
    counts = {
        EXCLUSION_INSTALLABLE_FALSE: 0,
        EXCLUSION_LICENSE_SKIP: 0,
        EXCLUSION_UNPARSEABLE: 0,
    }
    # No Odoo version could be placed (no standard branch, no profile version,
    # short manifest version). Recorded as `unparseable`, counted apart in the log.
    skipped_unknown_version = 0

    unreadable: set[str] = set()
    for rel_manifest in sorted(finder_paths - untracked):
        manifest_path = repo_root / rel_manifest
        module_dir = manifest_path.parent
        module_name = module_dir.name
        rel_dir = str(Path(rel_manifest).parent)

        def _exclude(reason: str, version_raw: str = "", mismatch: bool = False,
                     _name: str = module_name, _dir: str = rel_dir,
                     _file: str = manifest_path.name) -> None:
            counts[reason] += 1
            excluded_candidates.setdefault(_name, []).append(ExcludedModule(
                name=_name, reason=reason, path=_dir, manifest_file=_file,
                version_raw=version_raw, version_mismatch=mismatch,
            ))

        try:
            source = read_manifest_source(str(manifest_path))
        except OSError as exc:
            unreadable.add(rel_manifest)
            _logger.warning(
                "tracked-manifest scan %s: cannot read %s (%s: %s) - scan incomplete",
                repo_path, rel_manifest, type(exc).__name__, exc,
            )
            continue
        manifest = parse_manifest_source(source, str(manifest_path))
        if not manifest:
            _exclude(EXCLUSION_UNPARSEABLE)
            continue
        version_raw = str(manifest.get('version', '') or '')
        odoo_version = _place_version(version_raw, repo_ver.odoo_version)
        mismatch = odoo_version != "unknown" and manifest_version_mismatch(
            version_raw, odoo_version,
        )
        if not manifest.get('installable', True):
            _exclude(EXCLUSION_INSTALLABLE_FALSE, version_raw, mismatch)
            continue
        # NOTE: do NOT skip on `active: False`. `active` (legacy, pre-
        # `auto_install`) is an auto-activate-on-fresh-DB hint - it is NOT an
        # index-exclusion signal. `installable: True` is what gates indexing.
        # The only v10-v14 module carrying `active: False` is `account_test`
        # (installable:True), which ships the real, queryable model
        # `accounting.assert.test` + views + a report - exactly what OSM is
        # meant to index. Skipping it dropped that module/model from the index
        # (parser MED-1 review).
        if odoo_version == "unknown":
            skipped_unknown_version += 1
            _exclude(EXCLUSION_UNPARSEABLE, version_raw)
            continue

        # --- ADR-0036: License detection (D1) ---
        try:
            major = int(odoo_version.split(".")[0])
        except (ValueError, IndexError):
            major = 10  # default to v10+ era for unknown versions
        effective_license = _resolve_effective_license(manifest, major)
        copyright_owner = _derive_copyright_owner(manifest, effective_license)

        # --- ADR-0036: Policy chokepoint (D2) - single location, config-driven ---
        action = license_policy_action(effective_license)
        if action == "skip":
            # By-design policy exclusion (ADR-0036), not a problem: the
            # per-repo INFO summary below already reports the license count,
            # so the per-module line is DEBUG (zero-noise reindex). Indexer
            # log-level policy: see the module header in parser_python.py.
            _logger.debug(
                "License policy: skipping module '%s' (license=%s, action=skip)."
                " To enable, flip LICENSE_POLICY['%s'] in src/constants.py.",
                module_name, effective_license, effective_license,
            )
            _exclude(EXCLUSION_LICENSE_SKIP, version_raw, mismatch)
            continue

        # 'serve' modules have no notice (None = silent-OK).
        license_notice: str | None = None
        if action == "ingest_flagged":
            license_notice = (
                f"Module '{module_name}' license {effective_license}:"
                f" ingest_flagged per license policy."
                f" Content is indexed but withheld from normal results pending review."
            )

        info = _build_module_info(
            manifest, module_name, module_dir, repo_root, odoo_version,
            version_raw, effective_license, copyright_owner, license_notice,
            repo_url, repo_id,
        )
        present_candidates.setdefault(module_name, []).append((rel_manifest, info))

    modules: dict[str, dict[str, ModuleInfo]] = {}
    excluded: dict[str, ExcludedModule] = {}
    shadowed: dict[str, list[str]] = {}
    for name in sorted(set(present_candidates) | set(excluded_candidates)):
        present = present_candidates.get(name, [])
        losers: list[str] = [e.path for e in excluded_candidates.get(name, [])]
        if present:
            ranked = sorted(
                present,
                key=lambda c: (
                    not _SEMVER_PREFIX_RE.match(c[1].version_raw),
                    _manifest_depth(c[0]),
                    c[0],
                ),
            )
            winner = ranked[0][1]
            modules.setdefault(winner.odoo_version, {})[name] = winner
            losers += [str(Path(rel).parent) for rel, _ in ranked[1:]]
        else:
            ranked_ex = sorted(
                excluded_candidates[name],
                key=lambda e: (len(Path(e.path).parts), e.path),
            )
            excluded[name] = ranked_ex[0]
            losers = [e.path for e in ranked_ex[1:]]
        if losers:
            shadowed[name] = sorted(losers)

    n_present = sum(len(m) for m in modules.values())
    # One summary line per repo - NOT one per skipped module (no spam).
    _logger.info(
        "registry scan %s (v%s): %d manifests → %d registered "
        "(skipped: %d not-installable, %d license, %d unparseable, "
        "%d unknown-version, %d untracked; %d shadowed, %d tracked missing, "
        "%d unreadable)",
        repo_path,
        dispatch_version or profile_version,
        len(finder_paths),
        n_present,
        counts[EXCLUSION_INSTALLABLE_FALSE],
        counts[EXCLUSION_LICENSE_SKIP],
        counts[EXCLUSION_UNPARSEABLE] - skipped_unknown_version,
        skipped_unknown_version,
        len(untracked),
        sum(len(v) for v in shadowed.values()),
        len(missing),
        len(unreadable),
    )

    return RegistryScan(
        repo_path=str(repo_path),
        odoo_version=dispatch_version or "unknown",
        branch=repo_ver.branch,
        modules=modules,
        excluded=excluded,
        shadowed=shadowed,
        tracked_paths=tracked_paths,
        finder_paths=finder_paths,
        untracked=untracked,
        missing=missing,
        complete=not missing and not unreadable,
        attention=list(repo_ver.attention),
        unreadable=frozenset(unreadable),
    )


def build_registry(
    repo_version_pairs: list[tuple[str, str]],
    repo_url: str | None = None,
    repo_id: int | None = None,
    *,
    branch: str | None = None,
) -> dict[str, dict[str, ModuleInfo]]:
    """
    Build module registry from a list of (repo_path, odoo_version) pairs.
    Returns {odoo_version: {module_name: ModuleInfo}}.

    Thin wrapper over ``build_registry_scan`` (one scan per pair, ``odoo_version``
    = the profile version, ``branch`` = the registered branch applied to every
    pair - callers pass one pair per repo). Only the indexable modules are
    returned; excluded / untracked / shadowed manifests are in the scan object.

    Conflict resolution across pairs: when the same module name appears in the
    same version, a later entry whose manifest version has >= 3 numeric parts
    replaces the earlier one.

    Args:
        repo_version_pairs: List of (repo_path, odoo_version) tuples.
        repo_url:  Optional repo URL for A2c provenance (set on every ModuleInfo).
        repo_id:   Optional repo DB id for A2c provenance (set on every ModuleInfo).
        branch:    Optional registered branch (version rule); None = checked-out branch.
    """
    registry: dict[str, dict[str, ModuleInfo]] = {}
    for repo_path, repo_version in repo_version_pairs:
        scan = build_registry_scan(
            repo_path, repo_version,
            branch=branch, repo_url=repo_url, repo_id=repo_id,
        )
        for version, mods in scan.modules.items():
            bucket = registry.setdefault(version, {})
            for name, info in mods.items():
                if name not in bucket or _SEMVER_PREFIX_RE.match(info.version_raw):
                    bucket[name] = info
    return registry
