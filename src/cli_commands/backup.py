# SPDX-License-Identifier: AGPL-3.0-or-later
"""``backup`` subcommand — create a complete backup bundle (.tar.gz).

Shared, patch-sensitive helpers (``_get_pg_dsn``, ``_is_pg_container_running``,
``_dsn_to_pg_args_and_env``, ``_export_neo4j_online``, ``_resolve_postgres_tool``)
are reached through the ``src.cli`` module object so ``patch("src.cli.<name>")``
test targets continue to intercept the call. Backup-private helpers live here.
"""
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_logger = logging.getLogger(__name__)


@contextmanager
def _backup_advisory_lock(conn):
    """Attempt to acquire a Postgres advisory lock for backup.

    Yields True if the lock was acquired, False if another backup is running.
    The caller must check the yielded value and abort if False.
    Always releases the lock on exit if it was acquired.
    """
    LOCK_ID = 0xBA17C9  # arbitrary unique ID for backup lock
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))
        acquired = cur.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def _encrypt_with_passphrase(plaintext: str, passphrase: str) -> bytes:
    """Encrypt plaintext string with a passphrase-derived Fernet key via PBKDF2.

    Uses PBKDF2-HMAC-SHA256 with 100 000 iterations to derive a Fernet key
    from the passphrase + a random 16-byte salt. Output format:
        salt (16 bytes) || fernet_token (variable length)
    Both parts are concatenated raw — the restore process splits at byte 16.
    """
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    key_bytes = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000, dklen=32)
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    token = Fernet(fernet_key).encrypt(plaintext.encode())
    return salt + token


def _get_latest_migration_version() -> str:
    """Return the highest migration file number as schema_version string.

    Scans migrations/ directory for *.sql files, returns the numeric prefix
    of the highest-numbered file (e.g. '0003' for 0003_profile_hierarchy.sql).
    Returns 'unknown' if directory is empty or not found.
    """
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    if not migrations_dir.is_dir():
        return "unknown"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        return "unknown"
    return sql_files[-1].stem.split("_")[0]


_DEFAULT_KEEP_BUNDLES = 14


def _prune_old_bundles(
    backup_dir: Path,
    keep_n: int,
    *,
    current_bundle: Path,
) -> tuple[list[Path], int]:
    """Delete old backup bundles beyond the newest *keep_n*, never deleting *current_bundle*.

    Safety guarantees:
    - Only deletes regular files (no symlinks, no directories) inside *backup_dir*.
    - Never deletes *current_bundle* regardless of its mtime ranking.
    - Sorted by mtime descending so the newest bundles are always kept.

    Returns a tuple of (deleted_paths, total_bytes_reclaimed).
    """
    if keep_n <= 0:
        # Operator opted out via --keep-bundles 0 (or OSM_BACKUP_KEEP=0).
        # Disable pruning entirely — no bundles deleted, no scan.
        return [], 0

    bundles = [
        f for f in backup_dir.glob("*.tar.gz")
        if f.is_file() and not f.is_symlink()
        and f.resolve() != current_bundle.resolve()
    ]
    # Also include current_bundle so it counts toward the keep window.
    all_bundles = sorted(
        bundles + [current_bundle.resolve()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    to_keep = set(p.resolve() for p in all_bundles[:keep_n])
    # current_bundle is always kept regardless.
    to_keep.add(current_bundle.resolve())

    deleted: list[Path] = []
    reclaimed = 0
    for bundle in all_bundles[keep_n:]:
        resolved = bundle.resolve()
        if resolved == current_bundle.resolve():
            continue  # safety: never delete current
        try:
            file_size = resolved.stat().st_size
            resolved.unlink()
            deleted.append(resolved)
            reclaimed += file_size
            _logger.debug("Pruned old bundle: %s (%d bytes)", resolved, file_size)
        except OSError as exc:
            _logger.warning("Failed to prune bundle %s: %s", resolved, exc)

    return deleted, reclaimed


def _cmd_backup(args) -> int:
    """Create complete backup bundle: PG dump + Neo4j Cypher export + FERNET key + manifest.

    Output: <output>.tar.gz containing:
      - postgres.dump      (pg_dump custom format, -Z 6 compressed)
      - neo4j.cypher       (online Cypher export via Bolt driver — no DB shutdown needed)
      - fernet.enc         (FERNET_KEY encrypted with --bundle-passphrase-env passphrase)
      - manifest.json      (timestamps, schema_version, component checksums)

    Neo4j export uses _export_neo4j_online() which streams all nodes and
    relationships over the Bolt protocol — no APOC plugin and no database
    shutdown are required (Community edition compatible).

    FERNET_KEY is read via ``src.crypto.get_fernet_key()`` which checks
    ``$CREDENTIALS_DIRECTORY/FERNET_KEY`` (systemd LoadCredential) first, then
    the ``$FERNET_KEY`` environment variable as a fallback.
    """
    # Import lazily (not at module top) to avoid a circular import: src.cli
    # imports this module, and the patch-sensitive helpers are looked up via the
    # src.cli module object so patch("src.cli.<name>") still intercepts.
    from src import cli

    output_path = Path(args.output)

    # Validate output path
    if not str(args.output).endswith(".tar.gz"):
        print("ERROR: --output must end with .tar.gz", file=sys.stderr)
        return 1

    backup_dir = os.getenv("BACKUP_DIR", str(Path.home() / "backup"))
    resolved_output = output_path.resolve()
    resolved_backup_dir = Path(backup_dir).resolve()
    if not str(resolved_output).startswith(str(resolved_backup_dir)):
        print(
            f"ERROR: --output must be under BACKUP_DIR={backup_dir}",
            file=sys.stderr,
        )
        return 1

    # Resolve keep_n: CLI flag > OSM_BACKUP_KEEP env > built-in default (14).
    # Use getattr so synthetic Namespaces in tests (no argparse) don't AttributeError.
    cli_keep = getattr(args, "keep_bundles", None)
    if cli_keep is not None:
        keep_n = cli_keep
    else:
        env_keep = os.getenv("OSM_BACKUP_KEEP")
        if env_keep is not None:
            try:
                keep_n = int(env_keep)
            except ValueError:
                _logger.warning(
                    "OSM_BACKUP_KEEP=%r is not a valid integer — using default %d",
                    env_keep,
                    _DEFAULT_KEEP_BUNDLES,
                )
                keep_n = _DEFAULT_KEEP_BUNDLES
        else:
            keep_n = _DEFAULT_KEEP_BUNDLES

    # Ensure output directory exists
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    dsn = cli._get_pg_dsn()
    if not dsn:
        from src import config
        print(config.dsn_missing_hint(), file=sys.stderr)
        return 1
    try:
        pg_args, env_overrides = cli._dsn_to_pg_args_and_env(dsn)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Pre-check: skip-gracefully when the PG container is known-not-running.
    # During the May 2026 incident the nightly backup unit ran while postgres
    # was Exited (127), producing a misleading "psycopg2.OperationalError"
    # which marked the systemd unit `failed` and noisy-paged with no signal
    # about the real upstream cause. Now we exit 0 with a WARNING line that
    # log scrapers can route to a different channel.
    container_running = cli._is_pg_container_running()
    if container_running is False:
        container_name = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
        _logger.warning(
            "Backup skipped: postgres container %r is not running."
            " Start the DB tier (e.g. `make recreate-db`) and re-run.",
            container_name,
        )
        print(
            f"SKIPPED: postgres container {container_name!r} is not running — backup not taken.",
            file=sys.stderr,
        )
        return 0
    # container_running is True OR None (docker absent / container unknown):
    # try the direct connection so split-tier deploys still work.

    import psycopg2

    from src.constants import PG_CONNECT_TIMEOUT_SECONDS
    try:
        conn = psycopg2.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT_SECONDS)
    except psycopg2.OperationalError as e:
        # `Connection refused`, `timeout expired`, etc. Same skip-gracefully
        # path as container-not-running so the nightly unit does not page.
        _logger.warning("Backup skipped: PG connection failed — %s", str(e)[:300])
        print(
            f"SKIPPED: PG connection failed — backup not taken. Cause: {str(e)[:300]}",
            file=sys.stderr,
        )
        return 0
    try:
        with _backup_advisory_lock(conn) as lock_acquired:
            if not lock_acquired:
                print(
                    "ERROR: Another backup is in progress (advisory lock held)",
                    file=sys.stderr,
                )
                return 1
            with tempfile.TemporaryDirectory() as tmpdir_str:
                tmpdir = Path(tmpdir_str)
                components: list[dict] = []

                # 1. pg_dump → postgres.dump (custom format, zstd level 6)
                # Use stdout redirect (not -f) so docker exec pipes output back to host.
                # With -f the file would be written inside the container, causing
                # FileNotFoundError on subsequent pg_out.read_bytes().
                # -F custom -Z 6: pg_restore-compatible binary format, ~40% smaller
                # than plain SQL; restore-side auto-detects .dump vs legacy .sql.
                pg_out = tmpdir / "postgres.dump"
                env = {**os.environ, **env_overrides}
                pg_cmd = cli._resolve_postgres_tool("pg_dump") + [
                    *pg_args, "-F", "custom", "-Z", "6"
                ]
                with pg_out.open("wb") as pg_out_handle:
                    result = subprocess.run(
                        pg_cmd,
                        stdout=pg_out_handle,
                        stderr=subprocess.PIPE,
                        env=env,
                        shell=False,
                    )
                if result.returncode != 0:
                    stderr_msg = result.stderr.decode(errors="replace")
                    print(f"pg_dump failed: {stderr_msg}", file=sys.stderr)
                    return result.returncode
                pg_sha = hashlib.sha256(pg_out.read_bytes()).hexdigest()
                components.append({"file": "postgres.dump", "sha256": pg_sha})
                print(f"  postgres.dump: {pg_out.stat().st_size} bytes")

                # 2. neo4j.cypher — online export via Bolt driver (no shutdown needed)
                # _export_neo4j_online() streams all nodes + relationships over
                # the Bolt protocol and writes CREATE statements to neo4j.cypher.
                # This replaces the old stop-dump-start flow which required
                # ~30 s downtime. No APOC plugin required (Community compatible).
                # NEO4J_PASSWORD must be set; if absent the step is skipped with
                # a warning (non-fatal — postgres.dump is still captured).
                neo4j_out = tmpdir / "neo4j.cypher"
                skipped_components: list[dict] = []
                neo4j_ok, neo4j_msg = cli._export_neo4j_online(neo4j_out)
                if neo4j_ok and neo4j_out.exists():
                    neo4j_sha = hashlib.sha256(neo4j_out.read_bytes()).hexdigest()
                    components.append({"file": "neo4j.cypher", "sha256": neo4j_sha})
                    print(f"  neo4j.cypher: {neo4j_out.stat().st_size} bytes ({neo4j_msg})")
                else:
                    _logger.warning(
                        "Neo4j online export skipped — bundle missing neo4j.cypher: %s",
                        neo4j_msg,
                    )
                    # Record the skip in the manifest so restore-side and DR
                    # automation can detect a Postgres-only bundle (see ADR-0018).
                    skipped_components.append({"file": "neo4j.cypher", "reason": neo4j_msg})

                # 3. Encrypt FERNET key with passphrase (REQUIRED if flag provided)
                if args.bundle_passphrase_env:
                    pp = os.getenv(args.bundle_passphrase_env)
                    if pp:
                        from src.crypto import get_fernet_key as _gfk
                        fernet_key = _gfk() or ""
                        encrypted_key = _encrypt_with_passphrase(fernet_key, pp)
                        fernet_out = tmpdir / "fernet.enc"
                        fernet_out.write_bytes(encrypted_key)
                        fernet_sha = hashlib.sha256(encrypted_key).hexdigest()
                        components.append({"file": "fernet.enc", "sha256": fernet_sha})
                        print(f"  fernet.enc: {len(encrypted_key)} bytes (encrypted)")
                    else:
                        _logger.warning(
                            "FERNET_KEY passphrase env var %r not set — skipping fernet.enc",
                            args.bundle_passphrase_env,
                        )

                # 4. manifest.json
                manifest = {
                    "created_at": datetime.now(UTC).isoformat(),
                    "schema_version": _get_latest_migration_version(),
                    "components": components,
                    "skipped_components": skipped_components,
                }
                manifest_path = tmpdir / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2))

                # 5. Bundle into tar.gz
                with tarfile.open(str(resolved_output), "w:gz") as tar:
                    for f in sorted(tmpdir.iterdir()):
                        tar.add(str(f), arcname=f.name)

    finally:
        conn.close()

    size = resolved_output.stat().st_size
    print(f"Backup written: {resolved_output} ({size} bytes)")

    # Retention pruning — keep newest keep_n bundles, never delete current bundle.
    # keep_n was resolved earlier (CLI > OSM_BACKUP_KEEP env > default 14).
    pruned, reclaimed = _prune_old_bundles(
        resolved_backup_dir, keep_n, current_bundle=resolved_output
    )
    if pruned:
        _logger.info(
            "Retention pruning: kept newest %d bundles, removed %d old bundle(s),"
            " reclaimed %d bytes",
            keep_n,
            len(pruned),
            reclaimed,
        )
        for p in pruned:
            print(f"  pruned: {p}")
        print(f"  total reclaimed: {reclaimed} bytes")

    return 0
