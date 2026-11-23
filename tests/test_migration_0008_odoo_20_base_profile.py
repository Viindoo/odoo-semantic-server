# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0008: every supported Odoo series has its shared base profile.

- a fresh install gets odoo_20 (20.0, shared: tenant_id NULL) next to the
  odoo_8..odoo_19 rows of 0001;
- a deployment that already created odoo_20 by hand keeps its row untouched
  when the migration body runs (description, tenant, parent links).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.constants import SURVEYED_MAJORS
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

_MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0008_odoo_20_base_profile.sql"


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_fresh_install_has_a_shared_base_profile_per_surveyed_series(clean_pg):
    run_migrations(clean_pg)
    rows = dict(_rows(
        clean_pg,
        "SELECT odoo_version, name FROM profiles "
        "WHERE name LIKE 'odoo\\_%%' AND tenant_id IS NULL",
    ))
    assert rows == {f"{m}.0": f"odoo_{m}" for m in SURVEYED_MAJORS}


def test_existing_odoo_20_profile_is_left_untouched(clean_pg):
    run_migrations(clean_pg)
    clean_pg.autocommit = True
    with clean_pg.cursor() as cur:
        cur.execute("UPDATE profiles SET description = 'curated by admin' "
                    "WHERE name = 'odoo_20'")
        cur.execute("INSERT INTO profiles (name, odoo_version, parent_profile_id) "
                    "SELECT 'child_20', '20.0', id FROM profiles WHERE name = 'odoo_20'")
        cur.execute(_MIGRATION.read_text())

    assert _rows(clean_pg, "SELECT description FROM profiles WHERE name = 'odoo_20'") == [
        ("curated by admin",)
    ]
    assert _rows(
        clean_pg,
        "SELECT p.name FROM profiles c JOIN profiles p ON p.id = c.parent_profile_id "
        "WHERE c.name = 'child_20'",
    ) == [("odoo_20",)]
    assert _rows(clean_pg, "SELECT count(*) FROM profiles WHERE odoo_version = '20.0' "
                           "AND name = 'odoo_20'") == [(1,)]
