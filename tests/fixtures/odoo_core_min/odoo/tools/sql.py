# Minimal stub modelling Odoo's tools/sql.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=function (top-level).
"""SQL DDL helper functions stub.

Provides utilities for schema inspection and DDL operations used by the
Odoo ORM during module installation and database migration steps.
"""


def column_exists(cr, table, column):
    """Check whether *column* exists in *table*.

    Args:
        cr: Odoo Cursor instance.
        table: Table name (no schema prefix).
        column: Column name to check.

    Returns:
        True if the column exists, False otherwise.
    """
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return bool(cr.fetchone())


def table_exists(cr, table):
    """Check whether *table* exists in the public schema.

    Args:
        cr: Odoo Cursor instance.
        table: Table name to check.

    Returns:
        True if the table exists, False otherwise.
    """
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name=%s AND table_schema='public'",
        (table,),
    )
    return bool(cr.fetchone())


def drop_view_if_exists(cr, viewname):
    """Drop *viewname* if it exists, without error if absent.

    Args:
        cr: Odoo Cursor instance.
        viewname: Name of the SQL VIEW to drop.
    """
    cr.execute(f'DROP VIEW IF EXISTS "{viewname}"')


def pg_varchar(size=None):
    """Return a VARCHAR column type string.

    Args:
        size: Optional integer maximum length; None returns unbounded VARCHAR.

    Returns:
        SQL type string suitable for use in ALTER TABLE ... ADD COLUMN.
    """
    if size:
        return f"VARCHAR({size})"
    return "VARCHAR"


def index_exists(cr, indexname):
    """Check whether a database index named *indexname* exists.

    Args:
        cr: Odoo Cursor instance.
        indexname: Name of the index to look up in pg_indexes.

    Returns:
        True if the index exists, False otherwise.
    """
    cr.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname=%s",
        (indexname,),
    )
    return bool(cr.fetchone())
