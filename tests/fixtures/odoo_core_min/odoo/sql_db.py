# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal stub modelling Odoo's sql_db.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=cursor_method.
"""Odoo database connection and cursor stubs.

Provides Cursor, ConnectionPool, and helper utilities that the Odoo server
uses to manage PostgreSQL connections across multiple worker processes.
"""


class Cursor:
    """Thin wrapper around psycopg2 cursor providing Odoo-specific helpers.

    The Cursor class adds logging, savepoint management, and debug helpers
    on top of a bare psycopg2 cursor. One Cursor is created per HTTP request
    and is shared throughout the request's ORM calls.

    Args:
        cnx: The underlying psycopg2 connection.
        dbname: The database name, used in log messages.
        dsn: The data source name for diagnostic output.
    """

    def __init__(self, cnx, dbname, dsn):
        self._cnx = cnx
        self.dbname = dbname
        self._dsn = dsn

    def execute(self, query, params=None):
        """Execute a SQL query, logging it in debug mode.

        Args:
            query: SQL string, may contain %s placeholders.
            params: Sequence of parameter values substituted into the query.

        Returns:
            None. Fetch results with fetchone() / fetchall() / dictfetchall().
        """
        self._cnx.cursor().execute(query, params)

    def fetchone(self):
        """Return the next row from the last query as a tuple.

        Returns:
            Tuple of column values, or None when no more rows exist.
        """
        return self._cnx.cursor().fetchone()

    def fetchall(self):
        """Return all remaining rows from the last query.

        Returns:
            List of tuples; empty list when no rows remain.
        """
        return self._cnx.cursor().fetchall()

    def dictfetchall(self):
        """Return all remaining rows as a list of dicts keyed by column name.

        Returns:
            List of dicts; empty list when no rows remain.
        """
        cursor = self._cnx.cursor()
        columns = [desc[0] for desc in cursor.description or []]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def commit(self):
        """Commit the current transaction.

        Should only be called at the end of a successful request. The ORM
        normally handles commit automatically via the request lifecycle.
        """
        self._cnx.commit()

    def rollback(self):
        """Roll back the current transaction.

        Called automatically on uncaught exceptions during a request.
        """
        self._cnx.rollback()

    def savepoint(self, name):
        """Create a database savepoint for partial rollback support.

        Args:
            name: Alphanumeric savepoint identifier (must be a valid SQL identifier).
        """
        self.execute(f"SAVEPOINT {name}")

    def close(self):
        """Close this cursor and return the connection to the pool.

        After calling close() the cursor must not be used again.
        """
        self._cnx.close()


class ConnectionPool:
    """Pool of psycopg2 connections shared across ORM requests.

    Manages a bounded set of database connections that are checked out
    per-request and returned when the request completes. Prevents the
    overhead of opening a new connection on every HTTP request.

    Args:
        dsn: PostgreSQL connection string.
        minconn: Minimum connections to keep open.
        maxconn: Maximum connections allowed in the pool.
    """

    def __init__(self, dsn, minconn=1, maxconn=64):
        self._dsn = dsn
        self._minconn = minconn
        self._maxconn = maxconn
        self._connections = []

    def borrow(self, dbname):
        """Check out a connection from the pool, creating one if needed.

        Args:
            dbname: Database name for logging purposes.

        Returns:
            A Cursor wrapping the checked-out connection.
        """
        raise NotImplementedError

    def give_back(self, connection, keep_in_pool=True):
        """Return a connection to the pool after a request completes.

        Args:
            connection: The psycopg2 connection to return.
            keep_in_pool: If False, the connection is closed rather than pooled.
        """
        raise NotImplementedError
