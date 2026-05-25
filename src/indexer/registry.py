# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/registry.py
import ast
import logging
import re
from pathlib import Path
from typing import Protocol

from src.constants import LEGACY_ERA_MAX_MAJOR, license_policy_action

from .models import ModuleInfo
from .parser_python import (
    _derive_copyright_owner,
    _detect_module_edition,
    _detect_viindoo_equivalent,
    _resolve_effective_license,
)
from .scanner import get_git_branch, get_module_commit_sha, is_odoo_version_branch

_logger = logging.getLogger(__name__)

# --- ManifestFinder Protocol (M4.5 WI1.1, per ADR-0002) --------------------
# Odoo v8/v9 use __openerp__.py instead of __manifest__.py.
# Pluggable finder keeps the rest of the pipeline version-agnostic.

class ManifestFinder(Protocol):
    def find(self, repo_path: str) -> list[str]: ...


def _scan(repo_path: str, filename: str) -> list[str]:
    results = []
    for p in Path(repo_path).rglob(filename):
        parts = p.parts
        if '.git' in parts or 'node_modules' in parts:
            continue
        results.append(str(p))
    return results


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
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return ModernManifestFinder()
    if major <= LEGACY_ERA_MAX_MAJOR:
        return LegacyManifestFinder()
    if major == 10:
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
    """
    try:
        source = Path(manifest_path).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return {}

    try:
        tree = ast.parse(source)
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
                return ast.literal_eval(stmt.value)
    except (SyntaxError, ValueError):
        # Python 2-only syntax outside the dict — try regex.
        return _regex_extract_manifest(source)
    except Exception:
        return {}
    return {}


def resolve_odoo_version(manifest_version: str, repo_path: str) -> str:
    """
    Resolve Odoo version from a manifest version string.
    Priority 1: long format "17.0.x.x.x" → take first two parts.
    Priority 2: git branch of the repo → must be Odoo version format.
    Fallback: "unknown".
    """
    # Long format: "17.0.1.0.0" — Odoo version is always X.0 prefix with at least 4 parts
    m = re.match(r'^(\d+\.0)\.\d+\.\d+', manifest_version)
    if m:
        return m.group(1)

    branch = get_git_branch(repo_path)
    if branch and is_odoo_version_branch(branch):
        return branch

    return "unknown"


def _find_manifests(repo_path: str, odoo_version: str = "") -> list[str]:
    """Find manifest files in repo, dispatching by version (v8/v9 → __openerp__.py)."""
    return get_manifest_finder(odoo_version).find(repo_path)


def build_registry(
    repo_version_pairs: list[tuple[str, str]],
    repo_url: str | None = None,
    repo_id: int | None = None,
) -> dict[str, dict[str, ModuleInfo]]:
    """
    Build module registry from a list of (repo_path, odoo_version) pairs.
    Returns {odoo_version: {module_name: ModuleInfo}}.

    Conflict resolution: when the same module name appears in the same version,
    prefer the entry with a long-format manifest version.

    Args:
        repo_version_pairs: List of (repo_path, odoo_version) tuples.
        repo_url:  Optional repo URL for A2c provenance (set on every ModuleInfo).
        repo_id:   Optional repo DB id for A2c provenance (set on every ModuleInfo).
    """
    registry: dict[str, dict[str, ModuleInfo]] = {}

    for repo_path, repo_version in repo_version_pairs:
        repo_root = Path(repo_path)
        for manifest_path in _find_manifests(repo_path, repo_version):
            module_dir = Path(manifest_path).parent
            module_name = module_dir.name

            manifest = parse_manifest(manifest_path)
            if not manifest:
                continue
            if not manifest.get('installable', True):
                continue

            version_raw = manifest.get('version', '')
            odoo_version = resolve_odoo_version(version_raw, repo_path)
            if odoo_version == "unknown":
                odoo_version = repo_version  # fallback to version from scanner
            if odoo_version == "unknown":
                continue

            # Compute commit_sha: relative path from repo root to module directory
            try:
                module_relpath = module_dir.relative_to(repo_root)
            except ValueError:
                # module_dir is not under repo_root (shouldn't happen, but graceful)
                module_relpath = module_dir
            commit_sha = get_module_commit_sha(repo_root, module_relpath)

            # --- ADR-0036: License detection (D1) ---
            try:
                major = int(odoo_version.split(".")[0])
            except (ValueError, IndexError):
                major = 10  # default to v10+ era for unknown versions
            effective_license = _resolve_effective_license(manifest, major)
            copyright_owner = _derive_copyright_owner(manifest, effective_license)

            # --- ADR-0036: Policy chokepoint (D2) — single location, config-driven ---
            action = license_policy_action(effective_license)
            if action == "skip":
                _logger.warning(
                    "License policy: skipping module '%s' (license=%s, action=skip)."
                    " To enable, flip LICENSE_POLICY['%s'] in src/constants.py.",
                    module_name, effective_license, effective_license,
                )
                continue  # do NOT insert into registry

            # Build the license_notice for restricted (ingest_flagged) modules.
            # 'serve' modules have no notice (None = silent-OK).
            license_notice: str | None = None
            if action == "ingest_flagged":
                license_notice = (
                    f"Module '{module_name}' license {effective_license}:"
                    f" ingest_flagged per license policy."
                    f" Content is indexed but withheld from normal results pending review."
                )

            # A2b — manifest enrichment fields
            # auto_install may be bool OR list of trigger module names → coerce to bool
            _auto_install_raw = manifest.get('auto_install', False)
            _auto_install: bool = bool(_auto_install_raw)

            _application: bool = bool(manifest.get('application', False))
            _category: str | None = manifest.get('category') or None
            _summary: str | None = manifest.get('summary') or None

            _ext_deps = manifest.get('external_dependencies') or {}
            _external_python: list[str] = list(_ext_deps.get('python') or [])
            _external_bin: list[str] = list(_ext_deps.get('bin') or [])

            info = ModuleInfo(
                name=module_name,
                odoo_version=odoo_version,
                repo=repo_root.name,
                path=str(module_dir),
                depends=manifest.get('depends', []),
                version_raw=version_raw,
                edition=_detect_module_edition(
                    manifest, module_name, str(module_dir),
                ),
                viindoo_equivalent_qname=_detect_viindoo_equivalent(module_name),
                commit_sha=commit_sha,
                license=effective_license,
                copyright_owner=copyright_owner,
                license_notice=license_notice,
                # A2b — manifest enrichment
                auto_install=_auto_install,
                application=_application,
                category=_category,
                summary=_summary,
                external_python=_external_python,
                external_bin=_external_bin,
                # A2c — repo provenance
                repo_url=repo_url,
                repo_id=repo_id,
                # ADR-0037 — repo checkout root for path relativization at write time.
                repo_root=repo_root,
            )

            if odoo_version not in registry:
                registry[odoo_version] = {}

            existing = registry[odoo_version].get(module_name)
            if existing:
                # Keep module with long-format version (contains Odoo version prefix)
                if re.match(r'^\d+\.\d+\.\d+', version_raw):
                    registry[odoo_version][module_name] = info
                # else: keep existing
            else:
                registry[odoo_version][module_name] = info

    return registry
