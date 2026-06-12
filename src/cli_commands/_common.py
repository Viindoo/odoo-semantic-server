# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers used by more than one CLI subcommand.

These functions are re-exported from ``src/cli.py`` so existing callers
(``from src.cli import ...``) and tests (``patch("src.cli.<name>")``) keep
working unchanged. The subcommand handlers reach the patch-sensitive helpers
through the ``src.cli`` module object (``from src import cli`` →
``cli._get_pg_dsn()``) so monkeypatching ``src.cli.<name>`` still intercepts
the call — see the B1 refactor notes.
"""
import json
import logging
import os
import subprocess
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from psycopg2 import extensions

log = logging.getLogger(__name__)


def _get_pg_dsn() -> str:
    """Return PG_DSN from env or INI config. Empty string if not configured."""
    from src import config
    return config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback="") or ""


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
