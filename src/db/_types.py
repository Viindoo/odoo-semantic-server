# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared type aliases for src/db/* modules."""
import psycopg2.extensions

type PgConn = psycopg2.extensions.connection
