# src/indexer/registry.py
import ast
import re
from pathlib import Path
from typing import Protocol

from src.constants import LEGACY_ERA_MAX_MAJOR

from .models import ModuleInfo
from .parser_python import _detect_module_edition, _detect_viindoo_equivalent
from .scanner import get_git_branch, get_module_commit_sha, is_odoo_version_branch

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


def get_manifest_finder(odoo_version: str) -> ManifestFinder:
    """Dispatch finder by Odoo major version. Defaults to Modern when unknown."""
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return ModernManifestFinder()
    return LegacyManifestFinder() if major <= LEGACY_ERA_MAX_MAJOR else ModernManifestFinder()


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
) -> dict[str, dict[str, ModuleInfo]]:
    """
    Build module registry from a list of (repo_path, odoo_version) pairs.
    Returns {odoo_version: {module_name: ModuleInfo}}.

    Conflict resolution: when the same module name appears in the same version,
    prefer the entry with a long-format manifest version.
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
