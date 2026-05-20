# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git URL detection + SSH clone helper.

Per ADR-0008: project-local known_hosts, GIT_SSH_COMMAND env, tempfile 0o600,
full clone (no --depth=1).
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from src.constants import TIMEOUT_GIT_CLONE

_SSH_URL_RE = re.compile(r"^(git@|ssh://)")


def is_ssh_url(url: str) -> bool:
    """Return True if `url` is an SSH-style git URL (git@... or ssh://...).

    Per ADR-0008 D1.
    """
    return bool(_SSH_URL_RE.match(url.strip()))


def _clones_base_dir() -> Path:
    """Base directory for auto-cloned repos. Configurable via odoo-semantic.conf."""
    # Default: ~/.local/share/odoo-semantic-mcp/clones/
    # If src.config exposes a [clones] base_dir reader, use it; else fall back to default.
    base = Path.home() / ".local" / "share" / "odoo-semantic-mcp" / "clones"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_clone_dir(profile_name: str, url: str) -> Path:
    """Compute the default target dir for an auto-cloned repo.

    Format: <base>/<profile>/<repo_slug>/
    Where repo_slug = stem of URL path with .git stripped (e.g. 'odoo' from 'odoo/odoo.git').
    """
    # Parse URL to strip query and fragment
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme:
        # https://, ssh://, file:// etc. — urlparse strips query/fragment
        path = parsed.path
    else:
        # SCP-style SSH (git@host:org/repo.git) — urlparse returns empty scheme
        # Manually strip query and fragment
        path = cleaned.split("?", 1)[0].split("#", 1)[0]

    # Derive slug from cleaned path
    # SSH: git@github.com:org/repo.git → 'repo'
    # ssh://git@host/path/repo.git → 'repo'
    # https://...: same logic
    name = path.rstrip("/").split("/")[-1].split(":")[-1]
    slug = name[:-4] if name.endswith(".git") else name
    if not slug:
        slug = "repo"
    return _clones_base_dir() / profile_name / slug


def _known_hosts_path() -> Path:
    """Project-local known_hosts file (per ADR-0008 D4). Touched if missing."""
    base = Path.home() / ".local" / "share" / "odoo-semantic-mcp"
    base.mkdir(parents=True, exist_ok=True)
    kh = base / "known_hosts"
    if not kh.exists():
        kh.touch(mode=0o644)
    return kh


def clone_repo(
    url: str,
    branch: str,
    target_dir: Path,
    *,
    private_key_pem: bytes | None,
    timeout: int = TIMEOUT_GIT_CLONE,
) -> None:
    """Clone `url` at `branch` into `target_dir`.

    Per ADR-0008:
    - D2: GIT_SSH_COMMAND env var (key path NEVER in argv).
    - D3: tempfile.mkstemp(mode=0o600) + try/finally cleanup.
    - D4: project-local known_hosts.
    - D5: full clone (no --depth=1).

    `private_key_pem` is None for HTTPS URLs (no SSH key needed).
    Raises subprocess.CalledProcessError on git failure or TimeoutExpired.
    Raises FileExistsError if target_dir already non-empty.
    """
    target_dir = Path(target_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise FileExistsError(f"target dir non-empty: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ}
    tmp_path: str | None = None

    try:
        if private_key_pem is not None and is_ssh_url(url):
            fd, tmp_path = tempfile.mkstemp(prefix="osm-ssh-", suffix=".key")
            try:
                os.fchmod(fd, 0o600)  # belt-and-suspenders
                os.write(fd, private_key_pem)
                if not private_key_pem.endswith(b"\n"):
                    os.write(fd, b"\n")
            finally:
                os.close(fd)
            kh = _known_hosts_path()
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {tmp_path} "
                f"-o StrictHostKeyChecking=accept-new "
                f"-o UserKnownHostsFile={kh}"
            )

        # D5: full clone, no --depth=1
        cmd = ["git", "clone", "--branch", branch, "--single-branch", url, str(target_dir)]
        subprocess.run(cmd, env=env, check=True, timeout=timeout)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass  # best-effort cleanup
