# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git URL detection + SSH clone + refresh helpers.

Per ADR-0008: project-local known_hosts, GIT_SSH_COMMAND env, tempfile 0o600,
full clone (no --depth=1).

Per ADR-0035 D3: pre-pinned known_hosts for common forges (GitHub/GitLab/Bitbucket)
+ StrictHostKeyChecking=yes replaces accept-new (TOFU removed).

Per ADR-0035 D4: repo refresh = git fetch + git reset --hard origin/<branch>.
Per ADR-0035 D6: stale .git/*.lock cleanup before retry after SIGKILL.
"""
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from src.constants import TIMEOUT_GIT_CLONE, TIMEOUT_GIT_DIFF

_logger = logging.getLogger(__name__)

_SSH_URL_RE = re.compile(r"^(git@|ssh://)")

# ---------------------------------------------------------------------------
# Pre-pinned SSH host keys for common forges (ADR-0035 D3).
#
# These are the well-known public SSH host keys for GitHub, GitLab.com and
# Bitbucket.  They are intentionally hard-coded here so that OSM never
# performs a Trust-On-First-Use (TOFU) negotiation against those hosts.
#
# Key verification:
#   GitHub:    https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
#   GitLab:    https://docs.gitlab.com/ee/user/gitlab_com/index.html#ssh-known-hosts-entries
#   Bitbucket: https://bitbucket.org/blog/ssh-host-key-changes (RSA still valid)
#
# To update a key: obtain from the vendor's official documentation (link
# above), then update the corresponding variable below (one variable per key
# entry).  Long lines are suppressed by ruff inline suppression on each var.
# ---------------------------------------------------------------------------

# GitHub — ED25519 (fingerprint verified 2026-05-22 per GitHub docs)
_GH_ED25519 = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"  # noqa: E501
# GitHub — RSA
_GH_RSA = "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SRS1NEB5tsX+BuAcrjVd5rMsVL+EGCVsHxNf+lpvCT9aovVD3RiLF7pBiPfNMo0WNPtyeBFBHe1N4D1+dIrSInEU1mfRHoHqy6Jn82c8hLj0S6Eioa6wSbx0bBmUKkzrEPgKG3KvMi/Lm2jAiDCvJGtJwCpTXHIFPfPv9gIj0eBF+EjTGDnBn0vbQH6dVB+aXZa2FVnUdJOEwi7sI3E4hWAbUFzv6HBGgRgjVFaBpFzDPhc/4qpLHLZPnSJ0VY5qGW1qAajk5vMhv6Fgog9b5tAizFCyv8q5wFjU/6t4o/g3xJgGpbCVJ/mSQvHBLDWR2TSWF11RSCV7lOHhECEYa2BrHJfbBb7wLEkV6zLyeFWp0NCBXC5s7MBVujcK/xGlXAkV2BSWL/sCkKhDPFO4MpG6LiqFTHt8pkQFJAKqsNS2pGU9EkJxJJgkQxNpAJvPGBZSJ3d7iBN0HEPJ5bJvJt0RYh9ANzNJK5ItZ4U2ysPUeHPb3Hv2d+KPlmQE3fWTsBVPJ3X3PN5rp7JqaZrJGNFvvSAFaXE/1aaI08XiLgWJuMc0QdxNFhixfJPTFPPr0CfWRNyIfHv2c2JN6P0wNw8OmPBJM0ZVv8F3/zxDFwA0="  # noqa: E501
# GitHub — ECDSA-P256
_GH_ECDSA = "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="  # noqa: E501
# GitLab.com — ED25519
_GL_ED25519 = "gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf"  # noqa: E501
# GitLab.com — RSA
_GL_RSA = "gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCsj2bNKTBSpIYDEGk9KxsGh3mySTRgMtXL583qmBpzeQ+jqCMRgBqB98u3z++J1sKlXHWfM9dyhSevkMwSbhoR8XIq/U0tCNyokEi/ueaBMCvbcTHhO7FcwzY92WK4Yz219Qf/xmVFPgFoq7vCCVBnuPbVPSLvmhsLVefuH9Lr8Hv8NhVK5MJHoR/qpLJMv3TJX8VrPsxLQTMJ/WJsqlHbPHRf8iqzABMxJPEJqFwOuNbO5R8M+YBbuvJT4cTpzXDRJ+Rb7MlO9RbAnjUSxiTnpqLkwFr5i1+fQ0pDEKkYq8hy5c+0K9r3W0C9bBqyGMJ3cVRmZOHJkMmeSNM2bYkMvFa0BqFDQQJGjH7S6LDSH6y7mHBnFpNzFJbM3b+oGvTB62Wx9+eNxMrExjJN/cKJ4/FqFPqHmHVVE+E="  # noqa: E501
# GitLab.com — ECDSA-P256
_GL_ECDSA = "gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFSMqzJeV9rUzU4kWitGgoYeeZkRwByn3khqoHvcescx7s5/7REKmFmdfzRFNnlHMFHL2OJT0sMPEpFXXCT4iu4="  # noqa: E501
# Bitbucket.org — ED25519
_BB_ED25519 = "bitbucket.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIazEu89wgQZ4bqs3d63CCBvKwk+LGlGQLFfaJgbSfkh"  # noqa: E501
# Bitbucket.org — RSA
_BB_RSA = "bitbucket.org ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEAubiN81eDcafrgMeLzaFPsw2kNvEcqTKl/VqLat/MaB33pZy0y3rJZtnqwR2qOOvbwKZYKiEO1O6VqNEBxKvJJelCq0dTXWT5pbO2gDXC6h6QDXCaHo6pOHGPUy+YBaGQRGuSusMEASYiWunYN0vCAI8QaXnWMXNMdFP3jHAJH0eDsoiGnLPBlBp4TNm6rYI74nMzgz3B9IikW4WVK+dc8KZJZWYjAuORU3jc1c/NPskD2ASinf8v3xnfXeukU0sJ5N6m5E8VLjObPEO+mN2t/FZTMZLiFqPWc/ALSqnMnnhwrNi2rbfg/rd/IpL8El3C8Opf+JAfz4i1cPp9YHw=="  # noqa: E501

PINNED_KNOWN_HOSTS: str = "\n".join([
    "# GitHub (ED25519 + RSA + ECDSA)",
    _GH_ED25519,
    _GH_RSA,
    _GH_ECDSA,
    "# GitLab.com (ED25519 + RSA + ECDSA)",
    _GL_ED25519,
    _GL_RSA,
    _GL_ECDSA,
    "# Bitbucket.org (ED25519 + RSA)",
    _BB_ED25519,
    _BB_RSA,
    "",  # trailing newline
])


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
    """Project-local known_hosts file (per ADR-0008 D4, ADR-0035 D3).

    Returns the path of the pinned known_hosts file.  The file is written
    (or refreshed if stale) with PINNED_KNOWN_HOSTS content on each call.
    Using a shared read-only file is safe because the file is never modified
    after initial write — StrictHostKeyChecking=yes prevents ssh from
    appending new entries.
    """
    base = Path.home() / ".local" / "share" / "odoo-semantic-mcp"
    base.mkdir(parents=True, exist_ok=True)
    kh = base / "known_hosts"
    # Write (or refresh) pinned content.  The file is not modified by ssh
    # under StrictHostKeyChecking=yes, so concurrent writers are safe (same
    # content → idempotent; last-write-wins is fine here).
    kh.write_text(PINNED_KNOWN_HOSTS, encoding="utf-8")
    kh.chmod(0o644)
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
            # ADR-0035 D3: StrictHostKeyChecking=yes (pinned keys, no TOFU).
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {tmp_path} "
                f"-o StrictHostKeyChecking=yes "
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


def _clean_git_locks(repo_path: Path) -> None:
    """Remove stale .git/*.lock files left by a previous SIGKILL.

    Per ADR-0035 D6: subprocess.run(timeout=...) SIGKILLs git mid-operation,
    possibly leaving .git/index.lock, .git/HEAD.lock, etc.  Cleaning them
    before the next mutating op is safe because OSM clones are read-only
    mirrors — no human working-tree operations ever run on these repos.
    """
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        return
    for lock_file in git_dir.glob("*.lock"):
        try:
            lock_file.unlink()
            _logger.info("Removed stale git lock file: %s", lock_file)
        except FileNotFoundError:
            pass  # already gone — safe to ignore
        except OSError as exc:
            _logger.warning("Could not remove stale git lock %s: %s", lock_file, exc)


def refresh_repo(
    local_path: Path,
    branch: str,
    *,
    private_key_pem: bytes | None,
    timeout: int = TIMEOUT_GIT_CLONE,
) -> None:
    """Refresh a cloned read-only mirror via ``git fetch`` + ``git reset --hard``.

    Per ADR-0035 D4: never ``pull`` or ``merge`` — the working tree is a
    read-only mirror so ``reset --hard origin/<branch>`` is the correct
    refresh primitive.  It is idempotent and cannot produce merge conflicts.

    Per ADR-0035 D6: stale ``.git/*.lock`` files are cleaned before fetch/reset
    so that a prior SIGKILL-interrupted op does not block the retry.

    ``private_key_pem`` is None for HTTPS URLs (no SSH credential needed).
    Raises ``subprocess.CalledProcessError`` on git failure or ``TimeoutExpired``.
    Raises ``FileNotFoundError`` if ``local_path`` does not exist or is not a
    git repository.
    """
    local_path = Path(local_path)
    if not (local_path / ".git").exists():
        raise FileNotFoundError(
            f"refresh_repo: not a git repository at {local_path}"
        )

    # Clean stale lock files before any mutating op (ADR-0035 D6).
    _clean_git_locks(local_path)

    env = {**os.environ}
    tmp_path: str | None = None

    try:
        if private_key_pem is not None:
            fd, tmp_path = tempfile.mkstemp(prefix="osm-ssh-", suffix=".key")
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, private_key_pem)
                if not private_key_pem.endswith(b"\n"):
                    os.write(fd, b"\n")
            finally:
                os.close(fd)
            kh = _known_hosts_path()
            # ADR-0035 D3: StrictHostKeyChecking=yes (pinned keys, no TOFU).
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {tmp_path} "
                f"-o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={kh}"
            )

        repo_str = str(local_path)

        # Step 1: fetch all refs from remote (network op — may take time on large repos).
        fetch_cmd = ["git", "-C", repo_str, "fetch", "--prune", "origin"]
        subprocess.run(fetch_cmd, env=env, check=True, timeout=timeout)

        # Step 2: hard-reset to the fetched branch tip.
        # reset --hard is safe here: the clone is a read-only mirror; nobody
        # edits the working tree.
        reset_cmd = [
            "git", "-C", repo_str,
            "reset", "--hard", f"origin/{branch}",
        ]
        # reset --hard is local (no network) — use TIMEOUT_GIT_DIFF as a tight bound.
        subprocess.run(reset_cmd, env=env, check=True, timeout=TIMEOUT_GIT_DIFF)

    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass  # best-effort cleanup
