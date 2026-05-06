# src/indexer/registry.py
import ast
import re
from pathlib import Path

from .models import ModuleInfo
from .scanner import get_git_branch, is_odoo_version_branch


def parse_manifest(manifest_path: str) -> dict:
    """Read __manifest__.py and return the manifest dict. Returns {} on error.

    Only iterates tree.body (top-level statements) instead of ast.walk,
    to avoid catching nested dicts like 'external_dependencies', 'assets', etc.
    """
    try:
        source = Path(manifest_path).read_text(encoding='utf-8', errors='ignore')
        tree = ast.parse(source)
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
                return ast.literal_eval(stmt.value)
    except Exception:
        pass
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


def _find_manifests(repo_path: str) -> list[str]:
    results = []
    for p in Path(repo_path).rglob('__manifest__.py'):
        parts = p.parts
        if '.git' in parts or 'node_modules' in parts:
            continue
        results.append(str(p))
    return results


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
        for manifest_path in _find_manifests(repo_path):
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

            info = ModuleInfo(
                name=module_name,
                odoo_version=odoo_version,
                repo=Path(repo_path).name,
                path=str(module_dir),
                depends=manifest.get('depends', []),
                version_raw=version_raw,
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
