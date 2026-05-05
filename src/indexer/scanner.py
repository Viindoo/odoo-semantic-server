# src/indexer/scanner.py
import re
import subprocess
from pathlib import Path


def get_git_branch(repo_path: str) -> str | None:
    """Trả về tên branch hiện tại của git repo. Trả về None nếu không phải git repo."""
    try:
        # Use symbolic-ref to handle unborn branches (newly created repos)
        result = subprocess.run(
            ["git", "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        return branch if branch else None
    except Exception:
        return None


def is_odoo_version_branch(branch: str) -> bool:
    """Kiểm tra xem branch có phải là Odoo version format (e.g. 17.0, 8.0, 16.0)."""
    if not branch:
        return False
    return bool(re.match(r'^\d+\.\d+$', branch))


def scan_repos(base_dirs: list[str]) -> list[tuple[str, str]]:
    """
    Quét các base_dirs để tìm git repos có Odoo version branch.
    Trả về danh sách (repo_path, odoo_version).
    """
    results = []
    for base_dir in base_dirs:
        base_path = Path(base_dir)
        if not base_path.exists():
            continue

        # Check if base_dir itself is a git repo with Odoo version branch
        branch = get_git_branch(str(base_path))
        if branch and is_odoo_version_branch(branch):
            results.append((str(base_path), branch))
            continue

        # Check subdirectories
        for subdir in base_path.iterdir():
            if not subdir.is_dir():
                continue
            branch = get_git_branch(str(subdir))
            if branch and is_odoo_version_branch(branch):
                results.append((str(subdir), branch))

    return results
