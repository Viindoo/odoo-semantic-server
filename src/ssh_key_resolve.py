# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared SSOT for resolving a repo's SSH private key (fetch/clone paths).

Single source of truth for the "given a repo row, produce the private-key PEM to
hand to git_utils.clone_repo / refresh_repo" logic. Both the on-demand cloner
(src/cloner/__main__.py) and the nightly pre-scan refresh
(src/indexer/pipeline_repo.py) call this so the two paths cannot diverge on how
they decide between an SSH-credentialed fetch and a keyless HTTPS fetch.

Layering: this is a leaf module. It imports only is_ssh_url (src.git_utils),
auth_store (src.db.pg) and decrypt_private_key (src.crypto) - none of which is
src.web_ui. That is what lets the indexer layer use it without violating the
one-way pipeline rule (src/indexer must not import src.web_ui), enforced by
tests/test_pipeline_import_discipline.py.

Policy (decide by URL SCHEME, matching the cloner):
  - HTTPS url (is_ssh_url False) -> None (no credential needed).
  - SSH url with a usable key row -> decrypt_private_key(private_key_encrypted).
  - SSH url but no ssh_key_id, or the ssh_key_id row is missing -> raise
    SshKeyUnavailable. An SSH fetch with no credential would authenticate as
    nobody and fail; surfacing it as a distinct, catchable error lets each caller
    apply its OWN policy (the cloner marks clone_status='error'; the nightly
    refresh logs a WARNING and indexes the on-disk state) instead of silently
    running a doomed keyless fetch.

Decrypt / DB errors (RuntimeError from a missing FERNET_KEY, InvalidToken, a DB
error from auth_store) are NOT caught here - they propagate as-is so the caller
treats them as a genuine failure, distinct from the benign SshKeyUnavailable.
"""
from src.crypto import decrypt_private_key
from src.git_utils import is_ssh_url


class SshKeyUnavailable(Exception):
    """An SSH-scheme repo has no usable SSH key (no ssh_key_id or missing row).

    Surfaced (not swallowed) so a keyless SSH fetch is never attempted silently.
    """


def resolve_ssh_key_pem(repo: dict) -> bytes | None:
    """Return the decrypted private-key PEM for *repo*, or None for HTTPS.

    Args:
        repo: a repos row dict carrying at least ``url`` and ``ssh_key_id``.

    Returns:
        ``None`` when the repo URL is not an SSH URL (HTTPS needs no credential),
        else the decrypted PEM bytes.

    Raises:
        SshKeyUnavailable: URL is SSH-scheme but there is no usable key (either
            ``ssh_key_id`` is None or the ssh_key_pairs row does not exist).
        RuntimeError / cryptography.fernet.InvalidToken: propagated from
            ``decrypt_private_key`` (e.g. FERNET_KEY absent / bad ciphertext).
    """
    from src.db.pg import auth_store

    url = repo.get("url") or ""
    if not is_ssh_url(url):
        return None

    ssh_key_id = repo.get("ssh_key_id")
    if ssh_key_id is None:
        raise SshKeyUnavailable(
            f"SSH URL {url!r} but no ssh_key_id set on repo id={repo.get('id')}"
        )

    key_row = auth_store().get_ssh_key_by_id(ssh_key_id)
    if key_row is None:
        raise SshKeyUnavailable(
            f"ssh_key_id={ssh_key_id} not found for repo id={repo.get('id')}"
        )

    return decrypt_private_key(key_row["private_key_encrypted"])
