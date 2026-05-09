"""Admin CLI for odoo-semantic-mcp.

Usage:
    python -m src.cli backup --output dump.sql
    python -m src.cli restore dump.sql
    python -m src.cli rotate-fernet --old-key OLD --new-key NEW
"""
import argparse
import os
import subprocess
import sys


def _get_pg_dsn() -> str:
    """Return PG_DSN from env or INI config. Empty string if not configured."""
    # Try env var first (production override), then INI config
    val = os.getenv("PG_DSN", "")
    if val:
        return val
    try:
        from src import config
        dsn = config.get("database", "pg_dsn", fallback=None)
        return dsn or ""
    except (ImportError, KeyError, AttributeError):
        return ""


def _cmd_backup(args) -> int:
    """Dump PostgreSQL database to a SQL file."""
    dsn = _get_pg_dsn()
    if not dsn:
        print(
            "ERROR: PG_DSN not configured. Set [database] pg_dsn in config or PG_DSN env var.",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        ["pg_dump", dsn, "-F", "plain", "-f", args.output],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"pg_dump failed: {result.stderr}", file=sys.stderr)
        return result.returncode
    print(f"Backup complete: {args.output}")
    print(
        "Note: Neo4j backup requires neo4j-admin dump "
        "(manual step — see docs/deploy.md §Backup)."
    )
    return 0


def _cmd_restore(args) -> int:
    """Restore PostgreSQL from a SQL dump file."""
    if not os.path.exists(args.file):
        print(f"ERROR: File not found: {args.file}", file=sys.stderr)
        return 1
    dsn = _get_pg_dsn()
    if not dsn:
        print("ERROR: PG_DSN not configured.", file=sys.stderr)
        return 1
    with open(args.file, "rb") as f:
        result = subprocess.run(["psql", dsn], stdin=f, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"psql failed: {result.stderr}", file=sys.stderr)
        return result.returncode
    print(f"Restore complete from: {args.file}")
    return 0


def _cmd_rotate_fernet(args) -> int:
    """Re-encrypt SSH private keys in ssh_key_pairs with a new FERNET_KEY."""
    if args.old_key == args.new_key:
        print("ERROR: --old-key and --new-key must differ.", file=sys.stderr)
        return 1

    from cryptography.fernet import Fernet, InvalidToken

    try:
        old_f = Fernet(args.old_key.encode())
        new_f = Fernet(args.new_key.encode())
    except Exception as e:
        print(f"ERROR: Invalid key: {e}", file=sys.stderr)
        return 1

    import psycopg2

    dsn = _get_pg_dsn()
    if not dsn:
        print("ERROR: PG_DSN not configured.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, private_key_encrypted FROM ssh_key_pairs "
                "WHERE private_key_encrypted IS NOT NULL"
            )
            rows = cur.fetchall()
            count = 0
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
                    count += 1
                except InvalidToken:
                    print(
                        f"WARNING: Row {row_id} could not be decrypted with old key — skipped.",
                        file=sys.stderr,
                    )
        conn.commit()
        print(f"Rotated {count} key(s).")
    finally:
        conn.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bak = sub.add_parser("backup", help="Dump PostgreSQL to file.")
    bak.add_argument("--output", required=True, help="Output SQL file path.")

    rst = sub.add_parser("restore", help="Restore PostgreSQL from dump.")
    rst.add_argument("file", help="SQL dump file to restore.")

    rot = sub.add_parser(
        "rotate-fernet", help="Re-encrypt SSH private keys with new FERNET_KEY."
    )
    rot.add_argument("--old-key", required=True, help="Current FERNET_KEY (base64).")
    rot.add_argument("--new-key", required=True, help="New FERNET_KEY (base64).")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
