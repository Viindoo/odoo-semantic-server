# SPDX-License-Identifier: AGPL-3.0-or-later
"""``restore`` subcommand — restore PostgreSQL (+ Neo4j) from a dump or bundle.

Shared, patch-sensitive helpers (``_get_pg_dsn``, ``_dsn_to_pg_args_and_env``,
``_resolve_postgres_tool``, ``_export_neo4j_online``, ``_restore_neo4j_cypher``,
``_neo4j_unreachable_reason``) are reached through the ``src.cli`` module object
so ``patch("src.cli.<name>")`` test targets continue to intercept the call.
"""
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


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
        return _restore_sql_or_dump(path, args)


def _restore_sql_or_dump(path: Path, args) -> int:
    """Restore PostgreSQL from a dump file — plain SQL (.sql) or custom format (.dump).

    Dispatch logic:
    - .dump extension → pg_restore (custom/directory/tar format, supports parallel restore)
    - .sql extension  → psql (legacy plain-text SQL, backwards-compatible path)

    Both paths reuse _resolve_postgres_tool() so docker exec wrapping works the same way.
    """
    # Lazy import avoids the src.cli <-> src.cli_commands.restore circular import;
    # patch-sensitive helpers stay reachable as patch("src.cli.<name>").
    from src import cli

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

    env = {**os.environ, **env_overrides}

    if path.suffix.lower() == ".dump":
        # Custom-format dump produced by pg_dump -F custom; restored via pg_restore.
        # pg_restore reads the file directly (not via stdin), so no stdin redirect needed.
        cmd = cli._resolve_postgres_tool("pg_restore") + [*pg_args, str(path)]
        result = subprocess.run(cmd, capture_output=True, env=env)
    else:
        # Legacy plain-text SQL: feed via stdin to psql.
        # No text=True: consistent with pg_dump bytes mode to avoid decode errors on
        # non-UTF-8 SQL comments or BYTEA literals.
        cmd = cli._resolve_postgres_tool("psql") + [*pg_args]
        with path.open("rb") as f:
            result = subprocess.run(cmd, stdin=f, capture_output=True, env=env)

    if result.returncode != 0:
        tool = "pg_restore" if path.suffix.lower() == ".dump" else "psql"
        print(f"{tool} failed: {result.stderr.decode(errors='replace')}", file=sys.stderr)
        return result.returncode
    print(f"Restore complete from: {path}")
    return 0


def _restore_bundle(path: Path, args) -> int:
    """Extract and restore a tar.gz bundle produced by the backup command.

    Security requirements enforced:
    - tarfile.extractall(filter='data') blocks path traversal, symlinks,
      absolute paths, and special device files (PEP 706, Python 3.12+).
    - Pre-restore safety backup is written before any destructive operation.
    - manifest.json must be present to confirm this is a valid bundle.
    """
    # Lazy import avoids the src.cli <-> src.cli_commands.restore circular import;
    # patch-sensitive helpers stay reachable as patch("src.cli.<name>").
    from src import cli

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

        # --- Locate postgres dump: new bundles use .dump, legacy bundles use .sql ---
        pg_dump = tmpdir / "postgres.dump"
        if not pg_dump.exists():
            pg_dump = tmpdir / "postgres.sql"  # legacy bundle fallback
        if not pg_dump.exists():
            print(
                "ERROR: Bundle missing postgres dump (expected postgres.dump or postgres.sql).",
                file=sys.stderr,
            )
            return 1

        # --- Pre-restore safety backup (MUST succeed before any destructive op) ---
        backup_dir = Path(os.getenv("BACKUP_DIR", "~/backup")).expanduser()
        backup_dir.mkdir(parents=True, exist_ok=True)
        safety_path = backup_dir / f"pre-restore-{int(time.time())}.dump"
        env = {**os.environ, **env_overrides}
        safety_cmd = cli._resolve_postgres_tool("pg_dump") + [*pg_args, "-F", "custom", "-Z", "6"]
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
        # Dispatch on extension: .dump → pg_restore (custom format); .sql → psql (legacy).
        if pg_dump.suffix.lower() == ".dump":
            pg_restore_cmd = cli._resolve_postgres_tool("pg_restore") + [*pg_args, str(pg_dump)]
            pg_result = subprocess.run(pg_restore_cmd, capture_output=True, env=env)
        else:
            # Legacy plain-text SQL: feed via stdin (no text=True — bytes mode for
            # BYTEA / non-UTF-8 SQL comment safety).
            psql_cmd = cli._resolve_postgres_tool("psql") + [*pg_args]
            with pg_dump.open("rb") as f:
                pg_result = subprocess.run(
                    psql_cmd, stdin=f, capture_output=True, env=env
                )
        if pg_result.returncode != 0:
            pg_tool = "pg_restore" if pg_dump.suffix.lower() == ".dump" else "psql"
            print(
                f"ERROR: {pg_tool} restore failed: {pg_result.stderr.decode(errors='replace')}",
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
            snap_ok, snap_msg = cli._export_neo4j_online(neo4j_safety_path)
            if snap_ok:
                print(f"Pre-restore Neo4j safety snapshot: {neo4j_safety_path}")
            elif cli._neo4j_unreachable_reason(snap_msg):
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
            neo4j_ok, neo4j_msg = cli._restore_neo4j_cypher(neo4j_cypher)
            if neo4j_ok:
                log.info("Neo4j restore complete: %s", neo4j_msg)
                print(f"  Neo4j: {neo4j_msg}")
            else:
                neo4j_restore_failed = True
                log.warning("Neo4j restore failed: %s", neo4j_msg)
                print(f"ERROR: Neo4j restore failed: {neo4j_msg}", file=sys.stderr)
                print(
                    "  The postgres restore is complete. Fix Neo4j manually and re-run "
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
            # Legacy bundle: .dump format produced by old offline neo4j-admin dump.
            # Postgres has been restored; Neo4j has NOT been restored — exit
            # non-zero so DR automation does not mark this as a successful
            # restore and the operator knows manual neo4j-admin load is needed.
            log.error(
                "Legacy neo4j.dump found at %s — manual neo4j-admin restore required. "
                "Postgres restored; Neo4j NOT restored. See docs/deploy.md §Backup.",
                neo4j_dump,
            )
            print(
                f"ERROR: Legacy neo4j.dump present at {neo4j_dump} — "
                "manual neo4j-admin load required (see docs/deploy.md §Backup). "
                "Postgres has been restored; Neo4j has NOT.",
                file=sys.stderr,
            )
            neo4j_restore_failed = True

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
