# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal stub modelling Odoo's tools/query.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=query_helper (class).
"""SQL query builder stubs.

Provides the Query class used by the ORM's search() implementation to
construct parameterized SELECT statements in a composable, injection-safe way.
"""


class Query:
    """Builder for parameterised SELECT queries.

    The ORM's search() method constructs a Query, applies domain filters
    via add_where(), joins related tables via add_join(), then renders the
    final SQL via as_sql().

    Args:
        table: The primary table alias for this query.
        order: Default ORDER BY expression.
    """

    def __init__(self, table="", order="id"):
        self._table = table
        self._order = order
        self._where_clauses = []
        self._params = []
        self._joins = []

    def add_where(self, clause, params=None):
        """Append a WHERE sub-clause.

        Args:
            clause: SQL fragment with %s placeholders, e.g. '"id" = %s'.
            params: List of parameter values corresponding to %s placeholders.
        """
        self._where_clauses.append(clause)
        if params:
            self._params.extend(params)

    def add_join(self, table, alias, condition):
        """Append a JOIN clause.

        Args:
            table: Table name to join.
            alias: Alias for the joined table in the query.
            condition: ON condition as SQL fragment.
        """
        self._joins.append((table, alias, condition))

    def as_sql(self):
        """Render the query to a (sql_string, params) tuple.

        Returns:
            Tuple of (sql, params) ready to pass to cursor.execute().
        """
        parts = [f'SELECT "{self._table}".id FROM "{self._table}"']
        for table, alias, cond in self._joins:
            parts.append(f'JOIN "{table}" AS "{alias}" ON {cond}')
        if self._where_clauses:
            parts.append("WHERE " + " AND ".join(self._where_clauses))
        parts.append(f"ORDER BY {self._order}")
        return " ".join(parts), self._params

    def select(self, *args):
        """Override the SELECT target columns (advanced use only).

        Args:
            *args: Column expressions to select instead of the default 'id'.

        Returns:
            Self, for fluent chaining.
        """
        self._select_cols = list(args)
        return self
