# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stamp taken at the run's start instant is never "older" than the run (F52).

Every lifecycle guard orders a server stamp (``Module.last_seen_at``, a child's
``written_at``) against the start of the deleting run:

* ``retire_modules``: a Module stamped at or after the run start was re-seen by
  some run after this one began (a concurrent owner, a ledger sync stamp) -
  it and its children are NOT retired (H2);
* the entity prune (``module_children_census`` / ``prune_module_children``): a
  child or relationship another run wrote at or after this run's start is not
  stale, whoever wrote it (28da41b); in the shared-module cutoff mode a child
  written at the cutoff instant is not stale (F49);
* the addon ``TestHelper`` projection stays while a ``TestClass`` of its name
  was written at or after the run start.

Real case (F52): Cypher orders two DateTimes of the SAME instant by their zone.
The server stamps ``datetime()`` with offset ``Z``, while a run start read back
through the driver (``server_now()``) arrives as zone id ``UTC``; with the
statement clock's millisecond resolution a stamp written in the run's first
millisecond compared as EARLIER than the start, and ``retire_modules`` deleted
a module a concurrent sync had just protected (data loss, 103-231 wrong
retirements per 1000 tight-loop iterations on this box).

These tests do not rely on timing luck: they construct the stamp and the run
start as the SAME instant explicitly - the stamp in the server's own form (and
in a named zone), the run start in every representation a caller can hand the
writer (a driver DateTime read back from the graph, a Python datetime with a
``ZoneInfo`` zone or a fixed offset) - and add a one-millisecond-earlier stamp
as the positive control that the guard still lets a genuinely older node go.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
M = "viin_ai_rag"

# 2026-09-24T04:48:20.123Z - a millisecond-resolution instant like the server's
# statement clock produces.
T_MS = 1_790_225_300_123


@pytest.fixture
def writer(clean_neo4j):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _py(ms: int, tz) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz)


def _readback(driver, ms: int):
    """The instant as the driver hands a server DateTime back (what server_now sees)."""
    with driver.session() as s:
        return s.run("RETURN datetime({epochMillis: $ms}) AS t", ms=ms).single()["t"]


# Every representation of the SAME instant a caller can pass as the run start.
RUN_START_FORMS = {
    "driver_datetime_readback": lambda driver, ms: _readback(driver, ms),
    "driver_readback_to_native": lambda driver, ms: _readback(driver, ms).to_native(),
    "zoneinfo_utc": lambda _d, ms: _py(ms, ZoneInfo("UTC")),
    "fixed_offset_utc": lambda _d, ms: _py(ms, UTC),
    "fixed_offset_plus_7": lambda _d, ms: _py(ms, timezone(timedelta(hours=7))),
}

# How the stamp itself may be stored: the server's own ``datetime()`` form
# (offset Z) or a value stamped from a zoned parameter (named zone).
STAMP_FORMS = {
    "server_offset_z": "datetime({epochMillis: $ms})",
    "named_zone": "datetime({epochMillis: $ms, timezone: 'Europe/Paris'})",
}


def _module(driver, name: str, stamp_expr: str | None, ms: int | None) -> None:
    """Module *name* with one Model child; ``last_seen_at`` from *stamp_expr*."""
    set_stamp = f"SET m.last_seen_at = {stamp_expr}" if stamp_expr else ""
    with driver.session() as s:
        s.run(
            f"""
            MERGE (m:Module {{name: $n, odoo_version: $v}})
            {set_stamp}
            MERGE (md:Model {{name: $model, module: $n, odoo_version: $v}})
            MERGE (md)-[:DEFINED_IN]->(m)
            """,
            n=name, v=V, ms=ms, model=f"{name}.record",
        ).consume()


def _exists(driver, cypher: str, **params) -> int:
    with driver.session() as s:
        return s.run(cypher, v=V, **params).single()["n"]


def _module_count(driver, name: str) -> int:
    return _exists(driver, "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
                   n=name)


def _model_count(driver, name: str) -> int:
    return _exists(driver, "MATCH (x:Model {module: $n, odoo_version: $v}) RETURN count(x) AS n",
                   n=name)


# ---------------------------------------------------------------------------
# retire_modules - H2 race guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stamp_form", sorted(STAMP_FORMS))
@pytest.mark.parametrize("start_form", sorted(RUN_START_FORMS))
def test_module_stamped_at_the_run_start_instant_is_never_retired_by_that_run(
    writer, clean_neo4j, start_form, stamp_form,
):
    """viin_ai_rag was stamped present in the same millisecond the retiring run
    started (a concurrent sync of another owner). Whatever zone the stamp and the
    run start carry, the run must report it ``skipped_recent`` and keep the
    Module and its children; a module stamped one millisecond BEFORE the start in
    the same call is still retired (the guard is not a blanket keep)."""
    driver = clean_neo4j
    _module(driver, M, STAMP_FORMS[stamp_form], T_MS)
    _module(driver, "viin_ai_rag_older", STAMP_FORMS[stamp_form].replace("$ms", "$ms - 1"),
            T_MS)
    run_at = RUN_START_FORMS[start_form](driver, T_MS)

    result = writer.retire_modules(V, [M, "viin_ai_rag_older"], run_started_at=run_at)

    assert result["skipped_recent"] == [M], result
    assert _module_count(driver, M) == 1, "a module stamped at the run start was retired"
    assert _model_count(driver, M) == 1, "its children were retired"
    assert result["retired"] == ["viin_ai_rag_older"]
    assert _module_count(driver, "viin_ai_rag_older") == 0
    assert _model_count(driver, "viin_ai_rag_older") == 0


# ---------------------------------------------------------------------------
# Entity prune - token mode (a concurrent run's writes) and cutoff mode (F49)
# ---------------------------------------------------------------------------

def _prune_fixture(driver, stamp_form: str) -> None:
    """Module M with, each written by ANOTHER run: a Field and a DEPENDS_ON edge
    stamped at T (the run start instant) and a Field and an edge stamped at T-1ms."""
    at = STAMP_FORMS[stamp_form]
    before = at.replace("$ms", "$ms - 1")
    with driver.session() as s:
        s.run(
            f"""
            MERGE (m:Module {{name: $n, odoo_version: $v}})
            MERGE (dep_now:Module {{name: 'dep_now', odoo_version: $v}})
            MERGE (dep_old:Module {{name: 'dep_old', odoo_version: $v}})
            MERGE (f_now:Field {{name: 'rag_note', model: 'ai.rag.source', module: $n,
                                 odoo_version: $v}})
            SET f_now.written_run = 'concurrent_run', f_now.written_at = {at}
            MERGE (f_old:Field {{name: 'rag_count', model: 'ai.rag.source', module: $n,
                                 odoo_version: $v}})
            SET f_old.written_run = 'earlier_run', f_old.written_at = {before}
            MERGE (m)-[r_now:DEPENDS_ON]->(dep_now)
            SET r_now.written_run = 'concurrent_run', r_now.written_at = {at}
            MERGE (m)-[r_old:DEPENDS_ON]->(dep_old)
            SET r_old.written_run = 'earlier_run', r_old.written_at = {before}
            """,
            n=M, v=V, ms=T_MS,
        ).consume()


def _field(driver, name: str) -> int:
    return _exists(
        driver, "MATCH (f:Field {name: $f, module: $n, odoo_version: $v}) RETURN count(f) AS n",
        f=name, n=M,
    )


def _dep(driver, target: str) -> int:
    return _exists(
        driver,
        "MATCH (:Module {name: $n, odoo_version: $v})-[r:DEPENDS_ON]->"
        "(:Module {name: $t, odoo_version: $v}) RETURN count(r) AS n",
        n=M, t=target,
    )


@pytest.mark.parametrize("stamp_form", sorted(STAMP_FORMS))
@pytest.mark.parametrize("start_form", sorted(RUN_START_FORMS))
def test_child_another_run_wrote_at_the_run_start_instant_is_not_pruned(
    writer, clean_neo4j, start_form, stamp_form,
):
    """This run re-parsed viin_ai_rag without rag_note; a concurrent run of
    another profile wrote rag_note (and a DEPENDS_ON edge) in the millisecond the
    run started. Neither is stale for this run: the census counts only the
    T-1ms node and edge, and the prune deletes exactly those."""
    driver = clean_neo4j
    _prune_fixture(driver, stamp_form)
    token = writer.begin_run("this_run", started_at=RUN_START_FORMS[start_form](driver, T_MS))

    census = writer.module_children_census(V, M, run_id=token)
    assert census["by_label"]["Field"] == {"total": 2, "stale": 1}, census
    assert census["rels_by_label"]["Module"] == {"total": 2, "stale": 1}, census

    result = writer.prune_module_children(V, M, run_id=token)

    assert _field(driver, "rag_note") == 1, "a child written at the run start was pruned"
    assert _dep(driver, "dep_now") == 1, "a relationship written at the run start was pruned"
    assert _field(driver, "rag_count") == 0
    assert _dep(driver, "dep_old") == 0
    assert (result["deleted"], result["rels_deleted"]) == (1, 1), result


@pytest.mark.parametrize("stamp_form", sorted(STAMP_FORMS))
@pytest.mark.parametrize("start_form", sorted(RUN_START_FORMS))
def test_child_written_at_the_shared_prune_cutoff_instant_is_not_stale(
    writer, clean_neo4j, start_form, stamp_form,
):
    """Shared-module mode: the cutoff is the oldest owner's complete-parse start.
    A child written at exactly that instant was produced by that parse (or
    later) and is kept; the T-1ms child is what no copy re-produced."""
    driver = clean_neo4j
    _prune_fixture(driver, stamp_form)
    cutoff = RUN_START_FORMS[start_form](driver, T_MS)

    census = writer.module_children_census(V, M, written_before=cutoff)
    assert census["by_label"]["Field"] == {"total": 2, "stale": 1}, census

    writer.prune_module_children(V, M, written_before=cutoff)

    assert _field(driver, "rag_note") == 1
    assert _dep(driver, "dep_now") == 1
    assert _field(driver, "rag_count") == 0
    assert _dep(driver, "dep_old") == 0


# ---------------------------------------------------------------------------
# TestHelper projection liveness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stamp_form", sorted(STAMP_FORMS))
@pytest.mark.parametrize("start_form", sorted(RUN_START_FORMS))
def test_test_helper_whose_test_class_was_written_at_the_run_start_instant_stays(
    writer, clean_neo4j, start_form, stamp_form,
):
    """The addon TestHelper ``RagCommon`` is a projection of its TestClass. A
    concurrent run wrote that TestClass in the millisecond this run started, so
    the helper is live; ``RagOld`` whose TestClass predates the start by 1 ms
    is pruned with it."""
    driver = clean_neo4j
    at = STAMP_FORMS[stamp_form]
    before = at.replace("$ms", "$ms - 1")
    with driver.session() as s:
        s.run(
            f"""
            MERGE (m:Module {{name: $n, odoo_version: $v}})
            MERGE (tc:TestClass {{name: 'RagCommon', module: $n, odoo_version: $v}})
            SET tc.written_run = 'concurrent_run', tc.written_at = {at}
            MERGE (th:TestHelper {{name: 'RagCommon', module: $n, odoo_version: $v}})
            MERGE (tc_old:TestClass {{name: 'RagOld', module: $n, odoo_version: $v}})
            SET tc_old.written_run = 'earlier_run', tc_old.written_at = {before}
            MERGE (th_old:TestHelper {{name: 'RagOld', module: $n, odoo_version: $v}})
            """,
            n=M, v=V, ms=T_MS,
        ).consume()
    token = writer.begin_run("this_run", started_at=RUN_START_FORMS[start_form](driver, T_MS))

    writer.prune_module_children(V, M, run_id=token)

    def helpers(name: str) -> int:
        return _exists(
            driver,
            "MATCH (h:TestHelper {name: $h, module: $n, odoo_version: $v}) RETURN count(h) AS n",
            h=name, n=M,
        )

    assert helpers("RagCommon") == 1, "a live helper projection was pruned"
    assert helpers("RagOld") == 0
