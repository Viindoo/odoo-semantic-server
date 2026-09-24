# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_ee_guard_since_version.py
"""check_module_exists applies an EE guard row only from its since_version on.

Business rules protected (lane-mcpfix defect 1):

- E1  A guard row with ``since_version`` answers "Is EE confusion: No" at an
      Odoo version before it and "Yes" from that version on. Real case: the
      Knowledge app first shipped with Odoo 16.0 Enterprise, so an admin who
      records ``knowledge`` since 16.0 must not see Odoo 15.0 flagged.
- E2  The comparison is numeric (``9.0`` is before ``16.0``, not after it as a
      string compare would say).
- E3  A row with no ``since_version`` still warns at every version and says no
      first version is recorded (an unknown window never hides a warning).
- E4  GUARD: an indexed module whose edition is ``enterprise`` is flagged even
      before the guard row's window opens (the indexed edition wins).
- E5  (round 2, C4) The indexed edition also wins the other way: a guarded
      name indexed at V as community / viindoo / oca / custom answers "Is EE
      confusion: No" there, even with the guard row open. Real case: OCA's
      ``knowledge`` module (OCA/knowledge, AGPL-3) shares its name with the
      Enterprise Knowledge app. The guard only fills gaps: at a version where
      the name is not indexed (or indexed with no edition) the guard's
      since_version window still decides.

The guard row is written into the real ``ee_modules`` table (the admin route's
store) and read back through ``get_ee_modules``; the version-keyed asks are
read-only on the graph except E4, which writes at TEST_VERSION only.
"""
from __future__ import annotations

import pytest

from tests.conftest import TEST_VERSION

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


@pytest.fixture
def guard_table(clean_pg):
    """The migrated ee_modules table with the seeded 16 rows; cache cleared around."""
    from src.data.ee_modules import get_ee_modules, invalidate_ee_modules_cache
    from src.db.migrate import run_migrations

    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ee_modules CASCADE")
    run_migrations(clean_pg)
    invalidate_ee_modules_cache()
    # Precondition: the tool reads THIS table (not the static fallback).
    assert any(r["name"] == "knowledge" for r in get_ee_modules(force_refresh=True))

    touched: set[str] = set()

    def set_since(name: str, since: str | None) -> None:
        touched.add(name)
        with clean_pg.cursor() as cur:
            cur.execute("UPDATE ee_modules SET since_version = %s WHERE name = %s",
                        (since, name))
            assert cur.rowcount == 1
        clean_pg.commit()
        invalidate_ee_modules_cache()
        rows = {r["name"]: r for r in get_ee_modules(force_refresh=True)}
        assert rows[name]["since_version"] == since  # the DB row is what the tool reads

    yield set_since
    # Leave the seeded windows (all NULL) for every later test reading this table.
    with clean_pg.cursor() as cur:
        cur.execute("UPDATE ee_modules SET since_version = NULL WHERE name = ANY(%s)",
                    (sorted(touched),))
    clean_pg.commit()
    invalidate_ee_modules_cache()


def _check(driver, name: str, version: str) -> str:
    from src.mcp.server import _check_module_exists
    return _check_module_exists(name, odoo_version=version, _driver=driver)


def _flag(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "Is EE confusion:" in ln]
    assert len(lines) == 1, out
    return lines[0].split("Is EE confusion:")[1].strip()


def test_knowledge_is_not_flagged_before_its_first_enterprise_version(
        guard_table, clean_neo4j):
    """E1 (FIX): since 16.0 -> 15.0 answers No, and says nothing about EE."""
    guard_table("knowledge", "16.0")
    out = _check(clean_neo4j, "knowledge", "15.0")
    assert "Indexed:         No" in out, out
    assert _flag(out) == "No", out
    assert "WARNING" not in out, out


@pytest.mark.parametrize("version", ["16.0", "17.0", "18.0"])
def test_knowledge_is_flagged_from_its_first_enterprise_version_on(
        guard_table, clean_neo4j, version):
    """E1 (FIX): since 16.0 -> 16.0 and later answer Yes, naming the window."""
    guard_table("knowledge", "16.0")
    out = _check(clean_neo4j, "knowledge", version)
    assert _flag(out) == "Yes", out
    assert "Odoo Enterprise module (EE guard list, since 16.0)" in out, out


def test_version_window_compares_numerically_not_as_text(guard_table, clean_neo4j):
    """E2 (FIX): "9.0" sorts after "16.0" as text; numerically it is earlier."""
    guard_table("knowledge", "16.0")
    assert _flag(_check(clean_neo4j, "knowledge", "9.0")) == "No"
    guard_table("knowledge", "9.0")
    assert _flag(_check(clean_neo4j, "knowledge", "16.0")) == "Yes"


@pytest.mark.parametrize("version", ["8.0", "15.0", "19.0"])
def test_row_without_since_version_still_warns_at_every_version(
        guard_table, clean_neo4j, version):
    """E3 (FIX for the label; the warning itself is pre-existing)."""
    guard_table("knowledge", None)
    out = _check(clean_neo4j, "knowledge", version)
    assert _flag(out) == "Yes", out
    assert "(EE guard list, no first version recorded)" in out, out
    assert "WARNING" in out and "Do NOT" in out, out


def test_other_rows_keep_their_own_window(guard_table, clean_neo4j):
    """E1 GUARD: one row's since_version never moves another row's window."""
    # GUARD: pre-existing behaviour (pre-fix every row applied at every version)
    guard_table("knowledge", "16.0")
    out = _check(clean_neo4j, "helpdesk", "15.0")
    assert _flag(out) == "Yes", out
    assert "viin_helpdesk" in out, out


def test_indexed_enterprise_module_is_flagged_before_the_guard_window(
        guard_table, clean_neo4j):
    """E4 GUARD: pre-existing behaviour - the indexed edition decides first."""
    # GUARD: pre-existing behaviour
    guard_table("knowledge", "100.0")  # window opens after TEST_VERSION (99.0)
    with clean_neo4j.session() as s:
        s.run("MERGE (m:Module {name: 'knowledge', odoo_version: $v}) "
              "SET m.profile = ['ee_guard_std'], m.edition = 'enterprise', "
              "m.license = 'OEEL-1', m.repo = 'enterprise'", v=TEST_VERSION)
    out = _check(clean_neo4j, "knowledge", TEST_VERSION)
    assert "Indexed:         Yes" in out, out
    assert _flag(out) == "Yes", out
    assert "(license=OEEL-1)" in out, out


def _index(driver, name: str, *, edition: str | None, license_val: str | None) -> None:
    with driver.session() as s:
        s.run("MERGE (m:Module {name: $n, odoo_version: $v}) "
              "SET m.profile = ['ee_guard_std'], m.edition = $e, m.license = $l, "
              "m.repo = 'knowledge'",
              n=name, v=TEST_VERSION, e=edition, l=license_val)


@pytest.mark.parametrize(("edition", "license_val"), [
    ("oca", "AGPL-3"),
    ("community", "LGPL-3"),
    ("viindoo", "OPL-1"),
    ("custom", "Other proprietary"),
])
@pytest.mark.parametrize("since", [None, "16.0"])
def test_indexed_non_enterprise_module_is_not_flagged_by_the_guard(
        guard_table, clean_neo4j, edition, license_val, since):
    """E5 (FIX): OCA ``knowledge`` indexed at 99.0 is not the Enterprise app,
    whatever the guard row says (open everywhere, or open since 16.0)."""
    guard_table("knowledge", since)
    _index(clean_neo4j, "knowledge", edition=edition, license_val=license_val)
    out = _check(clean_neo4j, "knowledge", TEST_VERSION)
    assert "Indexed:         Yes" in out, out
    assert _flag(out) == "No", out
    assert "EE guard list" not in out and "WARNING" not in out, out


def test_guard_still_answers_where_the_name_is_not_indexed(guard_table, clean_neo4j):
    """E5 (GUARD for the gap-filling half): OCA ``knowledge`` indexed at 99.0
    does not clear the guard at versions where nothing named knowledge is
    indexed - there the since_version window decides (15.0: No, 16.0: Yes)."""
    # GUARD: pre-existing behaviour (round-1 since_version rules)
    guard_table("knowledge", "16.0")
    _index(clean_neo4j, "knowledge", edition="oca", license_val="AGPL-3")
    assert _flag(_check(clean_neo4j, "knowledge", "15.0")) == "No"
    out = _check(clean_neo4j, "knowledge", "16.0")
    assert "Indexed:         No" in out, out
    assert _flag(out) == "Yes", out
    assert "(EE guard list, since 16.0)" in out, out


def test_guard_answers_for_a_module_indexed_without_an_edition(guard_table, clean_neo4j):
    """E5 (GUARD): indexed with no edition recorded is a gap - the guard row
    (no window recorded) still flags it."""
    # GUARD: pre-existing behaviour
    guard_table("knowledge", None)
    _index(clean_neo4j, "knowledge", edition=None, license_val=None)
    out = _check(clean_neo4j, "knowledge", TEST_VERSION)
    assert _flag(out) == "Yes", out
    assert "EE guard list" in out, out
