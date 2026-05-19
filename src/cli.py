"""Admin CLI for odoo-semantic-mcp.

Usage:
    python -m src.cli backup --output backup/dump.tar.gz [--bundle-passphrase-env ENV_NAME]
    python -m src.cli restore <bundle.tar.gz | dump.sql>
    python -m src.cli rotate-fernet --old-key-env OLD_FERNET_KEY --new-key-env NEW_FERNET_KEY
    python -m src.cli diagnose [--json]
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from psycopg2 import extensions

log = logging.getLogger(__name__)


def _get_pg_dsn() -> str:
    """Return PG_DSN from env or INI config. Empty string if not configured."""
    from src import config
    return config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback="") or ""


def _resolve_postgres_tool(tool: str) -> list[str]:
    """Return command prefix: local binary if available on PATH, else docker exec wrapper."""
    if shutil.which(tool):
        return [tool]
    container = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
    # -e PGPASSWORD forwards the host env var by name into the container (no value = forward by ref)
    return ["docker", "exec", "-i", "-e", "PGPASSWORD", container, tool]


def _resolve_neo4j_tool(tool: str) -> list[str]:
    """Return command prefix for a Neo4j tool: local binary if on PATH, else docker exec.

    Parallel to _resolve_postgres_tool. When neo4j-admin is only available inside
    the Neo4j container (typical Docker-Compose deployments), docker exec is used
    so the dump is written to a path mounted or accessible inside the container.
    Set NEO4J_CONTAINER to override the default container name.

    NOTE: For `neo4j-admin database dump` specifically, the database must be
    OFFLINE. The backup CLI uses _backup_neo4j_via_compose() instead of this
    resolver. This helper remains available for online tools (e.g., cypher-shell).
    """
    if shutil.which(tool):
        return [tool]
    container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    return ["docker", "exec", "-i", container, tool]


def _is_pg_container_running() -> bool | None:
    """Return True if the PG container reports `State.Running=true`, False if it
    explicitly reports `false`, None if `docker` is unavailable, the container
    is unknown, OR the output is ambiguous.

    Used as a pre-check by the backup command so a nightly run reports
    "skipped — PG container is not running" (exit 0, log WARNING) instead
    of crashing with `psycopg2.OperationalError: Connection refused` and
    being marked `failed` in systemd. Honours $POSTGRES_CONTAINER override
    so split-tier deploys can swap the container name.

    Returning None on ambiguous output (anything other than "true"/"false")
    is deliberate: callers should fall through to the direct connection
    attempt rather than incorrectly skipping. Past mocks in the test suite
    return generic MagicMock objects for `docker inspect`, which must be
    treated as "unknown", not "container down".
    """
    container = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True, text=True, shell=False,
        )
    except FileNotFoundError:
        # No docker in PATH — caller can fall back to a direct PG_DSN attempt.
        return None
    if r.returncode != 0:
        # Container does not exist (e.g. split-tier where PG is not in compose).
        return None
    stdout = r.stdout
    if not isinstance(stdout, str):
        # Mocked subprocess returning MagicMock — treat as unknown.
        return None
    out = stdout.strip()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _wait_neo4j_healthy(timeout_seconds: int = 60) -> bool:
    """Poll the Neo4j container's docker Health.Status until 'healthy' or timeout."""
    container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
                capture_output=True, text=True, shell=False,
            )
        except FileNotFoundError:
            return False
        if r.returncode == 0 and r.stdout.strip() == "healthy":
            return True
        time.sleep(2)
    log.warning("Neo4j did not return healthy within %ds after dump", timeout_seconds)
    return False


def _backup_neo4j_via_compose(host_tmpdir: Path) -> subprocess.CompletedProcess:
    """Dump Neo4j using stop-dump-start against a docker-compose deployment.

    Required because `neo4j-admin database dump` cannot run against a serving
    Neo4j 5.x database — the previous `docker exec` approach always failed with
    exit 1 ("database in use"). We stop the running container, run a one-off
    `docker compose run` container that mounts the same neo4j_data volume plus
    a bind-mounted /backups directory, then ALWAYS restart the service.

    ~30 s downtime per invocation. The neo4j service is expected to be running
    when this is called; the ``finally`` clause restores it even on dump failure.
    Returns the CompletedProcess from the dump step (returncode + stderr).

    Two operational details that matter on first deploy:

    - ``chmod 0o777`` on the host tmpdir. ``tempfile.TemporaryDirectory``
      defaults to mode 0700 owned by the calling user (UID 1000 in our prod
      setup). The neo4j image's default ``USER neo4j`` (UID 7474) cannot
      write to a 0700 tmpdir owned by 1000, so the bind-mounted /backups
      target needs world-write before the dump container starts. The
      directory is in /tmp and gets unlinked on context exit, so the
      window is bounded.
    - ``--verbose`` on neo4j-admin so the next failure shows the real cause
      instead of the generic "Dump failed for databases" one-liner.
    """
    try:
        os.chmod(host_tmpdir, 0o777)
    except OSError as e:
        log.warning(
            "chmod 0o777 on %s failed: %s — dump may fail if container UID differs",
            host_tmpdir, e,
        )

    subprocess.run(
        ["docker", "compose", "stop", "neo4j"],
        capture_output=True, shell=False,
    )
    try:
        return subprocess.run(
            [
                "docker", "compose", "run", "--rm", "-T",
                "-v", f"{host_tmpdir}:/backups",
                "neo4j",
                "neo4j-admin", "database", "dump",
                "--verbose",
                "--to-path=/backups",
                "neo4j",
            ],
            capture_output=True, shell=False,
        )
    finally:
        subprocess.run(
            ["docker", "compose", "start", "neo4j"],
            capture_output=True, shell=False,
        )
        _wait_neo4j_healthy(timeout_seconds=60)


def _dsn_to_pg_args_and_env(dsn: str) -> tuple[list[str], dict[str, str]]:
    """Parse PostgreSQL DSN into safe pg_dump/psql arguments and environment overrides.

    Extracts password from DSN and passes it via PGPASSWORD env var to avoid
    leaking credentials in /proc/<pid>/cmdline (visible to all users).

    Supports both URL form (postgresql://user:pass@host:5432/db) and
    keyword form (host=H port=P user=U password=P dbname=D).

    Args:
        dsn: PostgreSQL connection string (URL or keyword form)

    Returns:
        (argv_flags, env_overrides) where:
        - argv_flags: list of ['--host', 'H', '--port', 'P', '--username', 'U', '--dbname', 'D']
        - env_overrides: {'PGPASSWORD': '...'} if password present, else {}
    """
    env_overrides = {}
    argv_flags = []

    # Try URL form first (postgresql://...)
    if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
        try:
            parsed = urllib.parse.urlparse(dsn)

            if parsed.hostname:
                argv_flags.extend(["--host", parsed.hostname])

            if parsed.port:
                argv_flags.extend(["--port", str(parsed.port)])

            if parsed.username:
                argv_flags.extend(["--username", urllib.parse.unquote(parsed.username)])

            if parsed.password:
                # URL-decode password (e.g., %40 -> @)
                env_overrides["PGPASSWORD"] = urllib.parse.unquote(parsed.password)

            # Extract database name from path (e.g., /mydb -> mydb)
            if parsed.path and parsed.path != "/":
                dbname = parsed.path.lstrip("/")
                argv_flags.extend(["--dbname", dbname])

            return argv_flags, env_overrides
        except Exception as e:
            raise ValueError(f"Failed to parse PostgreSQL URL DSN: {e}")

    # Try keyword form (host=... port=... user=... password=... dbname=...)
    if "=" in dsn:
        try:
            # Use psycopg2's parser for robustness
            parsed_kw = extensions.parse_dsn(dsn)

            if parsed_kw.get("host"):
                argv_flags.extend(["--host", parsed_kw["host"]])

            if parsed_kw.get("port"):
                argv_flags.extend(["--port", str(parsed_kw["port"])])

            if parsed_kw.get("user"):
                argv_flags.extend(["--username", parsed_kw["user"]])

            if parsed_kw.get("password"):
                env_overrides["PGPASSWORD"] = parsed_kw["password"]

            if parsed_kw.get("dbname"):
                argv_flags.extend(["--dbname", parsed_kw["dbname"]])

            return argv_flags, env_overrides
        except Exception as e:
            raise ValueError(f"Failed to parse PostgreSQL keyword DSN: {e}")

    raise ValueError(f"Unrecognized PostgreSQL DSN format: {dsn}")


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
    migrations_dir = Path(__file__).parent.parent / "migrations"
    if not migrations_dir.is_dir():
        return "unknown"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        return "unknown"
    return sql_files[-1].stem.split("_")[0]


def _cmd_backup(args) -> int:
    """Create complete backup bundle: PG dump + Neo4j dump + FERNET key + manifest.

    Output: <output>.tar.gz containing:
      - postgres.sql       (pg_dump plain SQL output)
      - neo4j.dump         (neo4j-admin database dump, if neo4j-admin is in PATH)
      - fernet.enc         (FERNET_KEY encrypted with --bundle-passphrase-env passphrase)
      - manifest.json      (timestamps, schema_version, component checksums)
    """
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

    # Ensure output directory exists
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    dsn = _get_pg_dsn()
    if not dsn:
        print(
            "ERROR: PG_DSN not configured. Set [database] pg_dsn in config or PG_DSN env var.",
            file=sys.stderr,
        )
        return 1
    try:
        pg_args, env_overrides = _dsn_to_pg_args_and_env(dsn)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Pre-check: skip-gracefully when the PG container is known-not-running.
    # During the May 2026 incident the nightly backup unit ran while postgres
    # was Exited (127), producing a misleading "psycopg2.OperationalError"
    # which marked the systemd unit `failed` and noisy-paged with no signal
    # about the real upstream cause. Now we exit 0 with a WARNING line that
    # log scrapers can route to a different channel.
    container_running = _is_pg_container_running()
    if container_running is False:
        container_name = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
        log.warning(
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
        log.warning("Backup skipped: PG connection failed — %s", str(e)[:300])
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

                # 1. pg_dump → postgres.sql
                # Use stdout redirect (not -f) so docker exec pipes output back to host.
                # With -f the file would be written inside the container, causing
                # FileNotFoundError on subsequent pg_out.read_bytes().
                pg_out = tmpdir / "postgres.sql"
                env = {**os.environ, **env_overrides}
                pg_cmd = _resolve_postgres_tool("pg_dump") + [
                    *pg_args, "-F", "plain"
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
                components.append({"file": "postgres.sql", "sha256": pg_sha})
                print(f"  postgres.sql: {pg_out.stat().st_size} bytes")

                # 2. neo4j.dump (docker-compose stop-dump-start)
                # neo4j-admin database dump requires the database OFFLINE
                # (Neo4j 5.x). _backup_neo4j_via_compose stops the service,
                # runs a one-off dump container with a bind-mounted /backups
                # dir so the dump lands on the host, and ALWAYS restarts.
                neo4j_out = tmpdir / "neo4j.dump"
                try:
                    neo4j_result = _backup_neo4j_via_compose(tmpdir)
                    if neo4j_result.returncode == 0 and neo4j_out.exists():
                        neo4j_sha = hashlib.sha256(neo4j_out.read_bytes()).hexdigest()
                        components.append({"file": "neo4j.dump", "sha256": neo4j_sha})
                        print(f"  neo4j.dump: {neo4j_out.stat().st_size} bytes")
                    else:
                        stderr = neo4j_result.stderr
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode(errors="replace")
                        stdout = neo4j_result.stdout
                        if isinstance(stdout, bytes):
                            stdout = stdout.decode(errors="replace")
                        # neo4j-admin writes the --verbose detail to either
                        # stderr or stdout depending on the failure stage,
                        # so surface both up to 2 KB each.
                        log.warning(
                            "neo4j dump failed (exit %d) stderr=%s stdout=%s"
                            " — bundle missing neo4j.dump",
                            neo4j_result.returncode,
                            (stderr or "")[:2000],
                            (stdout or "")[:2000],
                        )
                except FileNotFoundError:
                    log.warning("docker not found in PATH — skipping Neo4j backup")

                # 3. Encrypt FERNET key with passphrase (REQUIRED if flag provided)
                if args.bundle_passphrase_env:
                    pp = os.getenv(args.bundle_passphrase_env)
                    if pp:
                        fernet_key = os.getenv("FERNET_KEY", "")
                        encrypted_key = _encrypt_with_passphrase(fernet_key, pp)
                        fernet_out = tmpdir / "fernet.enc"
                        fernet_out.write_bytes(encrypted_key)
                        fernet_sha = hashlib.sha256(encrypted_key).hexdigest()
                        components.append({"file": "fernet.enc", "sha256": fernet_sha})
                        print(f"  fernet.enc: {len(encrypted_key)} bytes (encrypted)")
                    else:
                        log.warning(
                            "FERNET_KEY passphrase env var %r not set — skipping fernet.enc",
                            args.bundle_passphrase_env,
                        )

                # 4. manifest.json
                manifest = {
                    "created_at": datetime.now(UTC).isoformat(),
                    "schema_version": _get_latest_migration_version(),
                    "components": components,
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
    return 0


def _cmd_restore(args) -> int:
    """Restore PostgreSQL from a SQL dump file OR a .tar.gz bundle (W-BK format)."""
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        return 1

    fname = path.name.lower()
    if fname.endswith(".tar.gz") or fname.endswith(".tgz"):
        return _restore_bundle(path, args)
    else:
        return _restore_sql_plaintext(path, args)


def _restore_sql_plaintext(path: Path, args) -> int:
    """Restore PostgreSQL from a SQL plaintext dump file."""
    dsn = _get_pg_dsn()
    if not dsn:
        print("ERROR: PG_DSN not configured.", file=sys.stderr)
        return 1
    try:
        pg_args, env_overrides = _dsn_to_pg_args_and_env(dsn)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    env = {**os.environ, **env_overrides}
    cmd = _resolve_postgres_tool("psql") + [*pg_args]

    # stdin from file opened in bytes mode; capture_output=True gives bytes stdout/stderr.
    # No text=True: consistent with pg_dump (bytes mode) to avoid decode errors on
    # non-UTF-8 SQL comments or BYTEA literals.
    with path.open("rb") as f:
        result = subprocess.run(cmd, stdin=f, capture_output=True, env=env)
    if result.returncode != 0:
        print(f"psql failed: {result.stderr.decode(errors='replace')}", file=sys.stderr)
        return result.returncode
    print(f"Restore complete from: {path}")
    return 0


def _key_fingerprint(key_bytes: bytes) -> str:
    """Return a short SHA-256 fingerprint for identifying a FERNET key.

    Hashes only the first 8 characters of the base64-encoded key — non-revealing
    identifier suitable for audit logs.
    """
    digest = hashlib.sha256(key_bytes[:8]).hexdigest()
    return digest[:16]


def _restore_bundle(path: Path, args) -> int:
    """Extract and restore a tar.gz bundle produced by the backup command.

    Security requirements enforced:
    - tarfile.extractall(filter='data') blocks path traversal, symlinks,
      absolute paths, and special device files (PEP 706, Python 3.12+).
    - Pre-restore safety backup is written before any destructive operation.
    - manifest.json must be present to confirm this is a valid bundle.
    """
    dsn = _get_pg_dsn()
    if not dsn:
        print("ERROR: PG_DSN not configured.", file=sys.stderr)
        return 1
    try:
        pg_args, env_overrides = _dsn_to_pg_args_and_env(dsn)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as _tmpdir:
        tmpdir = Path(_tmpdir)

        # --- OWASP Guard: tarfile filter='data' (PEP 706) ---
        # Blocks: absolute paths, path-traversal ('..'), symlinks outside dest,
        # hardlinks outside dest, and special device files.
        try:
            with tarfile.open(path, "r:gz") as tar:
                try:
                    tar.extractall(tmpdir, filter="data")
                except (
                    tarfile.AbsoluteLinkError,
                    tarfile.OutsideDestinationError,
                    tarfile.LinkOutsideDestinationError,
                    tarfile.SpecialFileError,
                ) as e:
                    print(f"ERROR: Rejected malicious tar member: {e}", file=sys.stderr)
                    return 1
        except tarfile.TarError as e:
            print(f"ERROR: Failed to open bundle: {e}", file=sys.stderr)
            return 1

        # --- Verify manifest ---
        manifest_path = tmpdir / "manifest.json"
        if not manifest_path.exists():
            print(
                "ERROR: Bundle missing manifest.json — not a valid backup bundle.",
                file=sys.stderr,
            )
            return 1
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception as e:
            print(f"ERROR: Invalid manifest.json: {e}", file=sys.stderr)
            return 1
        log.info("Bundle manifest: %s", manifest)

        # --- Verify postgres.sql presence ---
        pg_dump = tmpdir / "postgres.sql"
        if not pg_dump.exists():
            print("ERROR: Bundle missing postgres.sql.", file=sys.stderr)
            return 1

        # --- Pre-restore safety backup (MUST succeed before any destructive op) ---
        backup_dir = Path(os.getenv("BACKUP_DIR", "~/backup")).expanduser()
        backup_dir.mkdir(parents=True, exist_ok=True)
        safety_path = backup_dir / f"pre-restore-{int(time.time())}.sql"
        env = {**os.environ, **env_overrides}
        safety_cmd = _resolve_postgres_tool("pg_dump") + [*pg_args, "-F", "plain"]
        log.info("Writing pre-restore safety backup to: %s", safety_path)
        try:
            with safety_path.open("wb") as sf:
                safety_result = subprocess.run(
                    safety_cmd,
                    stdout=sf,
                    stderr=subprocess.PIPE,
                    env=env,
                )
            if safety_result.returncode != 0:
                print(
                    f"ERROR: Pre-restore safety backup failed: "
                    f"{safety_result.stderr.decode(errors='replace')}",
                    file=sys.stderr,
                )
                safety_path.unlink(missing_ok=True)
                return 1
        except Exception as e:
            print(f"ERROR: Pre-restore safety backup failed: {e}", file=sys.stderr)
            return 1
        log.info("Safety backup written: %s", safety_path)
        print(f"Pre-restore safety backup: {safety_path}")

        # --- Restore PostgreSQL ---
        # Bytes mode (no text=True) — consistent with pg_dump bytes mode to avoid
        # decode errors on non-UTF-8 SQL content.
        psql_cmd = _resolve_postgres_tool("psql") + [*pg_args]
        with pg_dump.open("rb") as f:
            pg_result = subprocess.run(
                psql_cmd, stdin=f, capture_output=True, env=env
            )
        if pg_result.returncode != 0:
            print(
                f"ERROR: psql restore failed: {pg_result.stderr.decode(errors='replace')}",
                file=sys.stderr,
            )
            print(f"  Safety backup preserved at: {safety_path}", file=sys.stderr)
            return 1

        # --- Restore Neo4j dump if present ---
        neo4j_dump = tmpdir / "neo4j.dump"
        if neo4j_dump.exists():
            log.info(
                "Neo4j dump found at %s — manual neo4j-admin restore required. "
                "See docs/deploy.md §Backup.",
                neo4j_dump,
            )
            print(
                f"Note: Neo4j dump present at {neo4j_dump} — "
                "manual neo4j-admin restore required (see docs/deploy.md §Backup)."
            )

        # --- Restore fernet.enc if present and passphrase provided ---
        fernet_enc = tmpdir / "fernet.enc"
        if fernet_enc.exists():
            passphrase_env = getattr(args, "bundle_passphrase_env", None)
            if passphrase_env:
                passphrase = os.getenv(passphrase_env)
                if passphrase:
                    log.info(
                        "fernet.enc present — passphrase provided via env, "
                        "decryption skipped (not implemented)."
                    )
                    print(
                        "Note: fernet.enc decryption not implemented "
                        "— set FERNET_KEY manually."
                    )
                else:
                    print(
                        f"Note: fernet.enc present but env var "
                        f"{passphrase_env!r} is unset — skipping."
                    )
            else:
                print(
                    "Note: fernet.enc present but --bundle-passphrase-env "
                    "not specified — skipping."
                )

        print(f"Restore complete from bundle: {path}")
        return 0


def _cmd_rotate_fernet(args) -> int:
    """Re-encrypt SSH private keys in ssh_key_pairs with a new FERNET_KEY.

    Keys must be delivered via environment variables (not CLI flags) to avoid
    leaking secrets via /proc/<pid>/cmdline. Legacy --old-key/--new-key flags
    are still accepted for backward compatibility but emit a deprecation warning.

    The rotation is fully atomic: if any row fails to decrypt with the old key,
    the entire transaction is rolled back (no partial state). A successful rotation
    writes an audit row to ``key_rotation_log``.
    """
    # Resolve keys: legacy flags (deprecated) or env var names (preferred).
    old_key_str: str | None = None
    new_key_str: str | None = None

    if args.old_key or args.new_key:
        log.warning(
            "--old-key/--new-key flags leak secrets via /proc/PID/cmdline. "
            "Use --old-key-env/--new-key-env instead. These flags will be removed in M10."
        )
        old_key_str = args.old_key
        new_key_str = args.new_key

    # Env var names take precedence when --old-key-env/--new-key-env are used.
    if not old_key_str:
        old_key_str = os.getenv(args.old_key_env)
    if not new_key_str:
        new_key_str = os.getenv(args.new_key_env)

    if not old_key_str or not new_key_str:
        print(
            f"ERROR: Missing FERNET keys. "
            f"Set {args.old_key_env} and {args.new_key_env} environment variables "
            f"or use --old-key-env/--new-key-env to specify different env var names.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if old_key_str == new_key_str:
        print("ERROR: old key and new key must differ.", file=sys.stderr)
        return 1

    from cryptography.fernet import Fernet, InvalidToken

    try:
        old_key_bytes = old_key_str.encode()
        new_key_bytes = new_key_str.encode()
        old_f = Fernet(old_key_bytes)
        new_f = Fernet(new_key_bytes)
    except Exception as e:
        print(f"ERROR: Invalid key: {e}", file=sys.stderr)
        return 1

    import psycopg2

    dsn = _get_pg_dsn()
    if not dsn:
        print("ERROR: PG_DSN not configured.", file=sys.stderr)
        return 1

    actor = os.getenv("USER") or os.getenv("LOGNAME") or "unknown"
    old_fp = _key_fingerprint(old_key_bytes)
    new_fp = _key_fingerprint(new_key_bytes)

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(
                "SELECT id, private_key_encrypted FROM ssh_key_pairs "
                "WHERE private_key_encrypted IS NOT NULL FOR UPDATE"
            )
            rows = cur.fetchall()
            failures = []
            updated = 0
            for row_id, encrypted in rows:
                try:
                    plaintext = old_f.decrypt(
                        encrypted.encode() if isinstance(encrypted, str) else encrypted
                    )
                    new_encrypted = new_f.encrypt(plaintext)
                    cur.execute(
                        "UPDATE ssh_key_pairs "
                        "SET private_key_encrypted = %s, "
                        "key_version = COALESCE(key_version, 0) + 1 "
                        "WHERE id = %s",
                        (new_encrypted.decode(), row_id),
                    )
                    updated += 1
                except InvalidToken:
                    failures.append(row_id)

            if failures:
                conn.rollback()
                log.error(
                    "Rotation aborted: %d row(s) failed to decrypt with old key: %s",
                    len(failures),
                    failures,
                )
                print(
                    f"ERROR: Rotation aborted — {len(failures)} row(s) could not be decrypted "
                    f"with the old key: {failures}. No rows were changed.",
                    file=sys.stderr,
                )
                raise SystemExit(2)

            # All rows re-encrypted successfully — write audit entry then commit.
            cur.execute(
                "INSERT INTO key_rotation_log "
                "(rotated_at, actor, row_count, old_key_id, new_key_id) "
                "VALUES (NOW(), %s, %s, %s, %s)",
                (actor, updated, old_fp, new_fp),
            )
            conn.commit()
            log.info("Rotated %d row(s) successfully.", updated)
            print(f"Rotated {updated} key(s).")
        except SystemExit:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        conn.close()
    return 0


def _diagnose_initdb_dir() -> Path:
    """Resolve `docker/initdb.d` against `src/cli.py`'s location (NOT runtime cwd).

    Same pattern as `src/db/migrate.py`'s `_MIGRATIONS_DIR`: anchor to
    `__file__` so the check works under systemd (`WorkingDirectory=/`), cron,
    or any caller. Exposed as a function (rather than a module constant) so
    tests can monkeypatch it cleanly.
    """
    return Path(__file__).resolve().parent.parent / "docker" / "initdb.d"


def _cmd_diagnose(args) -> int:
    """Cross-tier health diagnostic. Reports PG container, Neo4j container,
    MCP /health endpoint, and bind-mount source types declared in compose.

    Output: human-readable text by default; `--json` emits a single object
    suitable for piping into a remote alert pipeline.

    Exit codes:
        0  all checks passed (or all checks skipped because docker absent)
        1  at least one check FAILED — see output for which
    """
    import json as _json
    import urllib.error
    import urllib.request

    checks: list[dict] = []

    # Check 1: PG container running
    pg_container = os.getenv("POSTGRES_CONTAINER", "odoo-semantic-mcp-postgres-1")
    pg_running = _is_pg_container_running()
    if pg_running is None:
        checks.append({"check": "pg_container_running", "status": "skipped",
                       "detail": "docker not available or container unknown"})
    elif pg_running:
        checks.append({"check": "pg_container_running", "status": "ok",
                       "detail": pg_container})
    else:
        checks.append({"check": "pg_container_running", "status": "fail",
                       "detail": f"{pg_container} not running"})

    # Check 2: Neo4j container healthy
    neo4j_container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", neo4j_container],
            capture_output=True, text=True, shell=False,
        )
        if r.returncode != 0:
            checks.append({"check": "neo4j_container_healthy", "status": "skipped",
                           "detail": f"{neo4j_container} not found"})
        elif r.stdout.strip() == "healthy":
            checks.append({"check": "neo4j_container_healthy", "status": "ok",
                           "detail": neo4j_container})
        else:
            checks.append({"check": "neo4j_container_healthy", "status": "fail",
                           "detail": f"{neo4j_container} state={r.stdout.strip() or 'unknown'}"})
    except FileNotFoundError:
        checks.append({"check": "neo4j_container_healthy", "status": "skipped",
                       "detail": "docker not in PATH"})

    # Check 3: MCP /health endpoint reachable
    from src.constants import MCP_HEALTH_PROBE_TIMEOUT_SECONDS
    mcp_url = os.getenv("MCP_HEALTH_URL", "http://127.0.0.1:8002/health")
    try:
        with urllib.request.urlopen(
            mcp_url, timeout=MCP_HEALTH_PROBE_TIMEOUT_SECONDS,
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = _json.loads(body)
                health_status = parsed.get("status", "unknown")
            except _json.JSONDecodeError:
                health_status = "unparseable"
            if resp.status == 200 and health_status == "ok":
                checks.append({"check": "mcp_health", "status": "ok",
                               "detail": f"HTTP {resp.status} status={health_status}"})
            else:
                checks.append({"check": "mcp_health", "status": "fail",
                               "detail": f"HTTP {resp.status} status={health_status}"})
    except urllib.error.URLError as e:
        checks.append({"check": "mcp_health", "status": "fail",
                       "detail": f"unreachable: {str(e)[:200]}"})
    except Exception as e:
        checks.append({"check": "mcp_health", "status": "fail",
                       "detail": f"unexpected: {str(e)[:200]}"})

    # Check 4: bind-mount source declared in compose is a directory
    # (regression guard for the May 2026 incident — Docker daemon auto-creates
    # an empty *file* as a directory if it doesn't exist before the next `up`,
    # so we verify the declared source path is intact and the correct type).
    init_dir = _diagnose_initdb_dir()
    if init_dir.exists():
        if init_dir.is_dir():
            checks.append({"check": "compose_initdb_mount_type", "status": "ok",
                           "detail": f"{init_dir} is a directory"})
        else:
            checks.append({"check": "compose_initdb_mount_type", "status": "fail",
                           "detail": f"{init_dir} exists but is NOT a directory — fix immediately"})
    else:
        checks.append({"check": "compose_initdb_mount_type", "status": "skipped",
                       "detail": f"{init_dir} missing (repo not deployed here?)"})

    failures = [c for c in checks if c["status"] == "fail"]

    if getattr(args, "json", False):
        print(_json.dumps({"checks": checks, "failures": len(failures)}, indent=2))
    else:
        print("=== osm diagnose ===")
        for c in checks:
            symbol = {"ok": "✓", "fail": "✗", "skipped": "~"}[c["status"]]
            print(f"  {symbol} {c['check']:<30} {c['detail']}")
        print(f"\n{len(failures)} failure(s) of {len(checks)} checks")

    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bak = sub.add_parser("backup", help="Create complete backup bundle (.tar.gz).")
    bak.add_argument(
        "--output",
        required=True,
        help="Output path ending in .tar.gz (must be under BACKUP_DIR env var).",
    )
    bak.add_argument(
        "--bundle-passphrase-env",
        default="",
        metavar="ENV_VAR_NAME",
        help=(
            "Name of environment variable holding the passphrase used to encrypt "
            "FERNET_KEY in fernet.enc. The passphrase is never logged. "
            "Example: --bundle-passphrase-env BUNDLE_PASSPHRASE"
        ),
    )

    rst = sub.add_parser("restore", help="Restore PostgreSQL from dump or .tar.gz bundle.")
    rst.add_argument("file", help="SQL dump file or .tar.gz bundle to restore.")
    rst.add_argument(
        "--bundle-passphrase-env",
        default=None,
        help="Env var name containing passphrase for fernet.enc in bundle (optional).",
    )

    rot = sub.add_parser(
        "rotate-fernet", help="Re-encrypt SSH private keys with new FERNET_KEY."
    )
    rot.add_argument(
        "--old-key-env",
        default="OLD_FERNET_KEY",
        help="Name of env var holding the current FERNET key (default: OLD_FERNET_KEY).",
    )
    rot.add_argument(
        "--new-key-env",
        default="NEW_FERNET_KEY",
        help="Name of env var holding the new FERNET key (default: NEW_FERNET_KEY).",
    )
    # Deprecated: use --old-key-env/--new-key-env instead.  Kept for backward
    # compatibility until M10.  Hidden from --help to discourage use.
    rot.add_argument("--old-key", help=argparse.SUPPRESS)
    rot.add_argument("--new-key", help=argparse.SUPPRESS)

    diag = sub.add_parser(
        "diagnose",
        help="Cross-tier health check (PG, Neo4j, MCP /health, bind-mount types).",
    )
    diag.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text (for alert pipelines).",
    )

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "backup":
        return _cmd_backup(args)
    elif args.cmd == "restore":
        return _cmd_restore(args)
    elif args.cmd == "rotate-fernet":
        return _cmd_rotate_fernet(args)
    elif args.cmd == "diagnose":
        return _cmd_diagnose(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
