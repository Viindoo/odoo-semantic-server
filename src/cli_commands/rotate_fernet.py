# SPDX-License-Identifier: AGPL-3.0-or-later
"""``rotate-fernet`` subcommand — re-encrypt SSH keys + TOTP secrets atomically.

The shared, patch-sensitive ``_get_pg_dsn`` helper is reached through the
``src.cli`` module object so ``patch("src.cli._get_pg_dsn")`` test targets
continue to intercept the call. ``_key_fingerprint`` is private to this module.
"""
import hashlib
import logging
import os
import sys

log = logging.getLogger(__name__)


def _key_fingerprint(key_bytes: bytes) -> str:
    """Return a short SHA-256 fingerprint for identifying a FERNET key.

    Hashes only the first 8 characters of the base64-encoded key — non-revealing
    identifier suitable for audit logs.
    """
    digest = hashlib.sha256(key_bytes[:8]).hexdigest()
    return digest[:16]


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

    # Lazy import avoids the src.cli <-> src.cli_commands.rotate_fernet circular
    # import; _get_pg_dsn stays reachable as patch("src.cli._get_pg_dsn").
    from src import cli

    dsn = cli._get_pg_dsn()
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
