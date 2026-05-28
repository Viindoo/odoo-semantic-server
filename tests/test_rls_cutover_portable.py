# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RLS cutover portability guard — unit tests (no Docker needed).

Asserts that ops/rls_create_osm_reader.sql uses psql variable substitution
(:"db_name") instead of a hardcoded database name, and that ops/rls_cutover.sh
passes -v db_name="$DB_NAME" to the psql call. Prevents silent failures when
deploying to environments whose database is not named 'odoo_semantic'.

Refs: Pha 2B drift D1 / runbook §5.14 portability gap.
"""
import re
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
SQL_FILE = OPS_DIR / "rls_create_osm_reader.sql"
CUTOVER_SH = OPS_DIR / "rls_cutover.sh"


def test_sql_uses_psql_variable_for_db_name():
    """GRANT CONNECT must use psql variable :\"db_name\" instead of a literal name."""
    content = SQL_FILE.read_text()
    assert ':"db_name"' in content, (
        f"{SQL_FILE.name} must use psql variable substitution ':{chr(34)}db_name{chr(34)}' "
        "in the GRANT CONNECT statement so deployments with a non-default DB name work correctly."
    )


def test_sql_has_no_hardcoded_odoo_semantic_outside_comments():
    """No literal 'odoo_semantic' should appear outside comment lines in the SQL file.

    Lines beginning with '--' (SQL comments) or '#' are excluded from the check
    so header documentation is allowed to reference the default name as an example.
    """
    content = SQL_FILE.read_text()
    non_comment_lines = [
        line
        for line in content.split("\n")
        if not line.strip().startswith("--") and not line.strip().startswith("#")
    ]
    offenders = [
        line for line in non_comment_lines if re.search(r"\bodoo_semantic\b", line)
    ]
    assert not offenders, (
        f"{SQL_FILE.name} contains hardcoded 'odoo_semantic' outside comment lines:\n"
        + "\n".join(f"  {line!r}" for line in offenders)
        + "\nUse psql variable substitution (e.g. :\"db_name\") instead."
    )


def test_cutover_sh_passes_db_name_variable():
    """The psql call in rls_cutover.sh must pass -v db_name=\"$DB_NAME\" to the SQL file."""
    content = CUTOVER_SH.read_text()
    assert '-v db_name="$DB_NAME"' in content, (
        f"{CUTOVER_SH.name} must include '-v db_name=\"$DB_NAME\"' in the psql invocation "
        "that runs rls_create_osm_reader.sql, so the :\"{chr(34)}db_name{chr(34)}\" variable "
        "is defined and the GRANT CONNECT statement targets the correct database."
    )
