# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin CLI for odoo-semantic-mcp.

Usage:
    python -m src.cli backup --output backup/dump.tar.gz [--bundle-passphrase-env ENV_NAME]
    python -m src.cli restore <bundle.tar.gz | dump.sql>
    python -m src.cli rotate-fernet [--old-key-env OLD_FERNET_KEY] [--new-key-env NEW_FERNET_KEY]
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
    """
    if shutil.which(tool):
        return [tool]
    container = os.getenv("NEO4J_CONTAINER", "odoo-semantic-mcp-neo4j-1")
    return ["docker", "exec", "-i", container, tool]


def _get_neo4j_creds() -> tuple[str, str, str] | None:
    """Return (uri, user, password) from env / config, or None if password missing.

    Reads NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from environment (production
    layout) or config file via ``config.from_env_or_ini``.  Returns None when
    the password is absent so callers can skip the Neo4j step gracefully.
    """
    from src import config as _cfg
    uri = _cfg.from_env_or_ini(
        "NEO4J_URI", "database", "neo4j_uri", fallback="bolt://localhost:7687",
    )
    user = _cfg.from_env_or_ini(
        "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
    )
    password = _cfg.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
    )
    if not password:
        return None
    return uri, user, password


# Substrings that mark a Neo4j operation failure as "Neo4j is not reachable or
# not configured here" — as opposed to a genuine error against a live database.
# _export_neo4j_online and _restore_neo4j_cypher both phrase their early-exit
# (pre-wipe) errors with these markers, so a failed pre-restore SNAPSHOT carrying
# one of them means the subsequent restore will also bail out before its
# DETACH DELETE, leaving nothing to protect.
_NEO4J_UNREACHABLE_MARKERS = (
    "driver not installed",
    "not set",            # "NEO4J_PASSWORD not set ..."
    "connection failed",  # verify_connectivity() raised
)


def _neo4j_unreachable_reason(msg: str) -> bool:
    """Return True if *msg* indicates Neo4j is unreachable / unconfigured.

    Used to decide whether a failed pre-restore Neo4j safety snapshot is benign
    (Neo4j absent → the restore cannot wipe anything either) or fatal (live
    graph present but un-snapshottable → must abort before wiping).
    """
    low = (msg or "").lower()
    return any(marker in low for marker in _NEO4J_UNREACHABLE_MARKERS)


def _export_neo4j_online(out_path: Path) -> tuple[bool, str]:
    """Export Neo4j graph as Cypher CREATE statements using the Bolt driver (online).

    This is the online replacement for ``neo4j-admin database dump`` which
    requires the database to be OFFLINE (Neo4j 5.x Community).  No APOC plugin
    is required — the export is performed entirely over the Bolt protocol using
    standard Cypher queries.

    Required Neo4j config (already satisfied by default Community install):
      - No extra plugins needed (no APOC, no Enterprise licence).
      - Standard read access via NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD.

    Output format (``neo4j.cypher``):
      - Header comment with timestamp.
      - ``CREATE`` statements for every node (labels + properties).
      - ``MATCH`` + ``CREATE`` statements for every relationship (type + properties).
      - Suitable for replay via ``cypher-shell < neo4j.cypher`` or driver exec.

    Returns:
        (success, message) — (True, "") on success, (False, reason) on failure.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return False, "neo4j Python driver not installed — cannot export graph"

    creds = _get_neo4j_creds()
    if creds is None:
        return False, "NEO4J_PASSWORD not set — skipping Neo4j export"

    uri, user, password = creds
    # Single try/finally covers driver creation, verify_connectivity and use so
    # the driver is always closed — even when verify_connectivity() raises.
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        try:
            driver.verify_connectivity()
        except Exception as exc:
            return False, f"Neo4j connection failed: {exc}"

        node_count = 0
        rel_count = 0
        # E (memory): stream every statement straight to the file handle instead
        # of accumulating the whole graph in a `lines: list[str]` and join()-ing
        # at the end. ADR-0018 sizes the graph at ~1-2M nodes — holding all of
        # that Cypher in RAM is hundreds of MB; line-at-a-time keeps it flat.
        # F (consistency): read nodes AND relationships inside ONE explicit READ
        # transaction so a concurrent indexer write cannot land between the node
        # scan and the relationship scan and leave a dangling rel in the dump.
        with out_path.open("w", encoding="utf-8") as f, \
                driver.session() as session, \
                session.begin_transaction() as tx:
            f.write(f"// neo4j.cypher — exported {datetime.now(UTC).isoformat()}\n")
            f.write("// Replay: cypher-shell -u <user> -p <pass> < neo4j.cypher\n")
            f.write("// Or:     run each statement via neo4j Python driver\n")
            f.write("\n")

            # --- Export nodes ---
            # Retrieve all nodes with their elementId (stable surrogate key used
            # only within this export to wire up relationships).
            node_result = tx.run(
                "MATCH (n) RETURN elementId(n) AS eid, labels(n) AS lbls, properties(n) AS props"
            )
            for record in node_result:
                eid = record["eid"]
                lbls = record["lbls"]
                props = record["props"]
                label_str = ":" + ":".join(lbls) if lbls else ""
                props_cypher = _props_to_cypher(props)
                # Tag node with __eid__ so relationship MATCH can locate it.
                # Guard against a leading comma when the node has no serialisable
                # properties (props_cypher == "") — symmetric with the rel branch.
                inner = f"{props_cypher}, __eid__: {json.dumps(eid)}" if props_cypher \
                    else f"__eid__: {json.dumps(eid)}"
                f.write(f"CREATE (n{label_str} {{{inner}}});\n")
                node_count += 1

            # --- Export relationships ---
            rel_result = tx.run(
                "MATCH (a)-[r]->(b) "
                "RETURN elementId(a) AS aeid, elementId(b) AS beid, "
                "type(r) AS rtype, properties(r) AS props"
            )
            for record in rel_result:
                aeid = record["aeid"]
                beid = record["beid"]
                rtype = record["rtype"]
                props = record["props"]
                props_cypher = _props_to_cypher(props)
                rel_props = f" {{{props_cypher}}}" if props_cypher else ""
                f.write(
                    f"MATCH (a {{__eid__: {json.dumps(aeid)}}}), "
                    f"(b {{__eid__: {json.dumps(beid)}}}) "
                    f"CREATE (a)-[:{rtype}{rel_props}]->(b);\n"
                )
                rel_count += 1

            # --- Remove __eid__ helper property ---
            f.write("\n")
            f.write("// Cleanup: remove the temporary __eid__ routing property\n")
            f.write("MATCH (n) WHERE n.__eid__ IS NOT NULL REMOVE n.__eid__;\n")
            # Read-only export: never commit. Exiting the `begin_transaction()`
            # context manager without a commit rolls the (write-free) transaction
            # back. The snapshot stayed consistent across both scans above.

        msg = f"Exported {node_count} nodes, {rel_count} relationships"
        log.info("Neo4j online export complete: %s", msg)
        return True, msg
    except Exception as exc:
        return False, f"Neo4j export failed: {exc}"
    finally:
        driver.close()


def _props_to_cypher(props: dict) -> str:
    """Serialise a Neo4j property map to an inline Cypher key: value string.

    Values are encoded as JSON scalars (string → quoted, number → literal,
    bool → ``true``/``false``, None → skipped, list → Cypher list literal).
    The output is used directly inside ``{...}`` in CREATE/MATCH clauses.
    """
    parts: list[str] = []
    for key, value in props.items():
        safe_key = f"`{key}`" if not key.isidentifier() else key
        if value is None:
            continue
        if isinstance(value, bool):
            parts.append(f"{safe_key}: {str(value).lower()}")
        elif isinstance(value, (int, float)):
            parts.append(f"{safe_key}: {value}")
        elif isinstance(value, str):
            parts.append(f"{safe_key}: {json.dumps(value)}")
        elif isinstance(value, list):
            items = ", ".join(
                json.dumps(v) if isinstance(v, str) else str(v)
                for v in value
                if v is not None
            )
            parts.append(f"{safe_key}: [{items}]")
        else:
            # Fallback: JSON-encode unknown types (e.g. datetime, bytes)
            parts.append(f"{safe_key}: {json.dumps(str(value))}")
    return ", ".join(parts)


def _restore_neo4j_cypher(cypher_path: Path) -> tuple[bool, str]:
    """Restore Neo4j graph from a Cypher file produced by _export_neo4j_online.

    Executes each non-comment, non-empty statement in the file against Neo4j
    via the Bolt driver.  Statements are separated by semicolons at line end.

    Returns:
        (success, message) — (True, "") on success, (False, reason) on failure.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return False, "neo4j Python driver not installed"

    creds = _get_neo4j_creds()
    if creds is None:
        return False, "NEO4J_PASSWORD not set — cannot restore Neo4j"

    uri, user, password = creds
    # Single try/finally covers driver creation, verify_connectivity and use so
    # the driver is always closed — even when verify_connectivity() raises.
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        try:
            driver.verify_connectivity()
        except Exception as exc:
            return False, f"Neo4j connection failed: {exc}"

        content = cypher_path.read_text(encoding="utf-8")
        # Split on semicolon-terminated lines; skip comments and blanks.
        statements: list[str] = []
        current: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or not stripped:
                continue
            current.append(stripped.rstrip(";"))
            if stripped.endswith(";"):
                stmt = " ".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []

        # --- DR safety gate: validate BEFORE the destructive DETACH DELETE ---
        # A truncated / empty / corrupt dump must NOT wipe the live graph (a
        # wiped graph with nothing to restore is unrecoverable). Two checks:
        #   1. At least one parsed statement.
        #   2. The export completeness trailer is present. _export_neo4j_online
        #      ALWAYS writes "... REMOVE n.__eid__" as its final statement, even
        #      for an empty graph, so its absence proves the file was truncated
        #      mid-write or is not a genuine export.
        if not statements:
            return False, (
                "refusing to wipe Neo4j: cypher file has no executable statements "
                f"({cypher_path}) — empty or corrupt dump"
            )
        if not any("REMOVE n.__eid__" in stmt for stmt in statements):
            return False, (
                "refusing to wipe Neo4j: cypher file is missing the export "
                f"completeness trailer 'REMOVE n.__eid__' ({cypher_path}) — "
                "the dump was truncated or is not a valid _export_neo4j_online file"
            )

        executed = 0
        errors: list[str] = []
        with driver.session() as session:
            # Restore replays CREATE statements (not MERGE), so it is a
            # replace operation: wipe the existing graph first to avoid
            # duplicating every node/relationship onto a non-empty graph.
            # The legacy offline path (neo4j-admin database load) was
            # destructive by design; this preserves that contract.
            print("Wiping existing Neo4j graph before restore...")
            log.warning("Wiping existing Neo4j graph before Cypher restore")
            session.run("MATCH (n) DETACH DELETE n").consume()
            for stmt in statements:
                try:
                    session.run(stmt).consume()
                    executed += 1
                except Exception as exc:
                    errors.append(f"{stmt[:80]!r}: {exc}")

        if errors:
            return False, f"Restore finished with {len(errors)} errors: {errors[0]}"
        return True, f"Restored {executed} statements"
    except Exception as exc:
        return False, f"Neo4j restore failed: {exc}"
    finally:
        driver.close()


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
    """Create complete backup bundle: PG dump + Neo4j Cypher export + FERNET key + manifest.

    Output: <output>.tar.gz containing:
      - postgres.sql       (pg_dump plain SQL output)
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
        from src import config
        print(config.dsn_missing_hint(), file=sys.stderr)
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

                # 2. neo4j.cypher — online export via Bolt driver (no shutdown needed)
                # _export_neo4j_online() streams all nodes + relationships over
                # the Bolt protocol and writes CREATE statements to neo4j.cypher.
                # This replaces the old stop-dump-start flow which required
                # ~30 s downtime. No APOC plugin required (Community compatible).
                # NEO4J_PASSWORD must be set; if absent the step is skipped with
                # a warning (non-fatal — postgres.sql is still captured).
                neo4j_out = tmpdir / "neo4j.cypher"
                neo4j_ok, neo4j_msg = _export_neo4j_online(neo4j_out)
                if neo4j_ok and neo4j_out.exists():
                    neo4j_sha = hashlib.sha256(neo4j_out.read_bytes()).hexdigest()
                    components.append({"file": "neo4j.cypher", "sha256": neo4j_sha})
                    print(f"  neo4j.cypher: {neo4j_out.stat().st_size} bytes ({neo4j_msg})")
                else:
                    log.warning(
                        "Neo4j online export skipped — bundle missing neo4j.cypher: %s",
                        neo4j_msg,
                    )

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
        from src import config
        print(config.dsn_missing_hint(), file=sys.stderr)
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
        from src import config
        print(config.dsn_missing_hint(), file=sys.stderr)
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

        # --- Restore Neo4j from Cypher file if present ---
        # New bundles contain neo4j.cypher (online export); legacy bundles may
        # contain neo4j.dump (old offline format).  The .cypher file is loaded
        # automatically; the legacy .dump requires manual neo4j-admin load.
        #
        # A Neo4j restore failure is NOT silently swallowed: it sets
        # `neo4j_restore_failed` so the command exits non-zero. Postgres is
        # already restored at this point, so we report that success explicitly
        # while still signalling overall failure to any DR automation that keys
        # off the exit code (a partial/failed graph must not look like exit 0).
        neo4j_restore_failed = False
        neo4j_cypher = tmpdir / "neo4j.cypher"
        neo4j_dump = tmpdir / "neo4j.dump"
        if neo4j_cypher.exists():
            # Pre-restore safety SNAPSHOT of the live graph (parity with the
            # Postgres safety backup above). _restore_neo4j_cypher wipes the
            # graph; without this snapshot a failed restore would be
            # unrecoverable. Written next to the Postgres safety backup.
            neo4j_safety_path = backup_dir / f"pre-restore-{int(time.time())}-neo4j.cypher"
            log.info("Writing pre-restore Neo4j safety snapshot to: %s", neo4j_safety_path)
            snap_ok, snap_msg = _export_neo4j_online(neo4j_safety_path)
            if snap_ok:
                print(f"Pre-restore Neo4j safety snapshot: {neo4j_safety_path}")
            elif _neo4j_unreachable_reason(snap_msg):
                # Neo4j is not configured / not reachable here. The restore call
                # below will hit the same wall and return early WITHOUT wiping
                # (it never reaches DETACH DELETE), so there is no graph to
                # protect — proceed and let the restore report its own error.
                neo4j_safety_path.unlink(missing_ok=True)
                log.warning(
                    "Skipping Neo4j safety snapshot — Neo4j unreachable/unconfigured: %s",
                    snap_msg,
                )
            else:
                # Neo4j IS reachable but the snapshot failed for another reason.
                # Refuse to wipe a live graph we cannot first back up.
                neo4j_safety_path.unlink(missing_ok=True)
                print(
                    f"ERROR: Neo4j pre-restore safety snapshot failed: {snap_msg}",
                    file=sys.stderr,
                )
                print(
                    "  Postgres restore is complete, but the Neo4j graph was NOT "
                    "touched (no safety snapshot = no wipe). Fix Neo4j and re-run.",
                    file=sys.stderr,
                )
                return 1

            print("Restoring Neo4j graph from neo4j.cypher ...")
            neo4j_ok, neo4j_msg = _restore_neo4j_cypher(neo4j_cypher)
            if neo4j_ok:
                log.info("Neo4j restore complete: %s", neo4j_msg)
                print(f"  Neo4j: {neo4j_msg}")
            else:
                neo4j_restore_failed = True
                log.warning("Neo4j restore failed: %s", neo4j_msg)
                print(f"ERROR: Neo4j restore failed: {neo4j_msg}", file=sys.stderr)
                print(
                    "  The postgres.sql restore is complete. Fix Neo4j manually and re-run "
                    "the Neo4j portion, or restore from a fresh reindex.",
                    file=sys.stderr,
                )
                if snap_ok:
                    print(
                        f"  Pre-restore Neo4j safety snapshot preserved at: "
                        f"{neo4j_safety_path}",
                        file=sys.stderr,
                    )
        elif neo4j_dump.exists():
            # Legacy bundle: .dump format produced by old offline neo4j-admin dump
            log.info(
                "Legacy neo4j.dump found at %s — manual neo4j-admin restore required. "
                "See docs/deploy.md §Backup.",
                neo4j_dump,
            )
            print(
                f"Note: Legacy neo4j.dump present at {neo4j_dump} — "
                "manual neo4j-admin load required (see docs/deploy.md §Backup)."
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
                        "— set FERNET_KEY manually (via $FERNET_KEY env var or "
                        "systemd LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY)."
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

        if neo4j_restore_failed:
            print(
                f"Restore from bundle {path} FINISHED WITH ERRORS: Postgres "
                "restored, Neo4j restore failed (see above).",
                file=sys.stderr,
            )
            return 1
        print(f"Restore complete from bundle: {path}")
        return 0


def _cmd_rotate_fernet(args) -> int:
    """Re-encrypt FERNET-encrypted rows in ssh_key_pairs AND totp_secrets with a new FERNET_KEY.

    Keys must be delivered via environment variables (not CLI flags) to avoid
    leaking secrets via /proc/<pid>/cmdline.

    The rotation is fully atomic across both tables: if any row in either table
    fails to decrypt with the old key, the entire transaction is rolled back
    (no partial state). A successful rotation writes an audit row to
    ``key_rotation_log``.
    """
    # Resolve keys via env var names (--old-key-env / --new-key-env).
    old_key_str: str | None = os.getenv(args.old_key_env)
    new_key_str: str | None = os.getenv(args.new_key_env)

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
        from src import config
        print(config.dsn_missing_hint(), file=sys.stderr)
        return 1

    actor = os.getenv("USER") or os.getenv("LOGNAME") or "unknown"
    old_fp = _key_fingerprint(old_key_bytes)
    new_fp = _key_fingerprint(new_key_bytes)

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")

            # --- 1. Re-encrypt ssh_key_pairs.private_key_encrypted ---
            cur.execute(
                "SELECT id, private_key_encrypted FROM ssh_key_pairs "
                "WHERE private_key_encrypted IS NOT NULL FOR UPDATE"
            )
            ssh_rows = cur.fetchall()
            ssh_failures = []
            ssh_updated = 0
            for row_id, encrypted in ssh_rows:
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
                    ssh_updated += 1
                except InvalidToken:
                    ssh_failures.append(("ssh_key_pairs", row_id))

            # --- 2. Re-encrypt totp_secrets.secret_encrypted ---
            # Table may not exist on older deployments; skip gracefully if absent.
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'totp_secrets' AND table_schema = 'public')"
            )
            totp_table_exists = cur.fetchone()[0]

            totp_failures = []
            totp_updated = 0
            if totp_table_exists:
                cur.execute(
                    "SELECT user_id, secret_encrypted FROM totp_secrets "
                    "WHERE secret_encrypted IS NOT NULL FOR UPDATE"
                )
                totp_rows = cur.fetchall()
                for user_id, encrypted in totp_rows:
                    try:
                        plaintext = old_f.decrypt(
                            encrypted.encode() if isinstance(encrypted, str) else encrypted
                        )
                        new_encrypted = new_f.encrypt(plaintext)
                        cur.execute(
                            "UPDATE totp_secrets SET secret_encrypted = %s WHERE user_id = %s",
                            (new_encrypted.decode(), user_id),
                        )
                        totp_updated += 1
                    except InvalidToken:
                        totp_failures.append(("totp_secrets", user_id))

            # --- 3. Atomic check: any failure → rollback everything ---
            failures = ssh_failures + totp_failures
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

            # --- 4. All rows re-encrypted — write audit entry then commit ---
            total_updated = ssh_updated + totp_updated
            cur.execute(
                "INSERT INTO key_rotation_log "
                "(rotated_at, actor, row_count, old_key_id, new_key_id) "
                "VALUES (NOW(), %s, %s, %s, %s)",
                (actor, total_updated, old_fp, new_fp),
            )
            conn.commit()
            log.info(
                "Rotated %d ssh_key_pairs + %d totp_secrets row(s) successfully.",
                ssh_updated,
                totp_updated,
            )
            print(
                f"Rotated {ssh_updated} SSH key(s) + {totp_updated} TOTP secret(s). "
                f"Total: {total_updated} row(s)."
            )
            # Write admin_audit_log entry for fernet.rotate (ADR-0021 taxonomy).
            # Fire-and-forget; never raises — audit failure must not abort rotation.
            try:
                from src.db.audit import write_audit_log
                write_audit_log(
                    actor=f"cli:{actor}",
                    action="fernet.rotate",
                    target=f"old={old_fp},new={new_fp}",
                    success=True,
                    detail={
                        "ssh_rows": ssh_updated,
                        "totp_rows": totp_updated,
                        "total_rows": total_updated,
                        "old_key_fingerprint": old_fp,
                        "new_key_fingerprint": new_fp,
                    },
                )
            except Exception as _audit_exc:
                log.warning(
                    "admin_audit_log write for fernet.rotate failed (non-fatal): %s",
                    _audit_exc,
                )
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

    Delegates all check logic to ``src.diagnostics.run_diagnostics()`` (SSOT)
    so the HTTP endpoint can reuse the same checks without code duplication.

    Output: human-readable text by default; `--json` emits a single object
    suitable for piping into a remote alert pipeline.

    Exit codes:
        0  all checks passed (or all checks skipped because docker absent)
        1  at least one check FAILED — see output for which
    """
    import json as _json

    from src.diagnostics import run_diagnostics
    result = run_diagnostics()
    checks = result["checks"]

    # Map shared status names to CLI legacy names for human-readable output
    _status_symbol = {"ok": "✓", "error": "✗", "skipped": "~"}
    errors = [c for c in checks if c["status"] == "error"]

    if getattr(args, "json", False):
        # Emit JSON using the shared schema (name/status/detail) + failure count
        print(_json.dumps({"checks": checks, "failures": len(errors)}, indent=2))
    else:
        print("=== osm diagnose ===")
        for c in checks:
            symbol = _status_symbol.get(c["status"], "?")
            print(f"  {symbol} {c['name']:<30} {c['detail']}")
        print(f"\n{len(errors)} failure(s) of {len(checks)} checks")

    return 1 if errors else 0


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
        "rotate-fernet",
        help="Re-encrypt SSH keys + TOTP secrets with a new FERNET_KEY.",
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
    from src import config
    config.init_dotenv()
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
