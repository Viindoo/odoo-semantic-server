# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every Python datetime the indexer sends to Neo4j is at the fixed UTC offset.

Rules (finalfix round 3, item 1):

* ``writer_neo4j.utc_for_neo4j`` accepts any aware time - a named ``zoneinfo``
  zone, a pytz zone, a fixed offset, ``ZoneInfo("UTC")``, a driver
  ``neo4j.time.DateTime`` - and returns the SAME instant with ``tzinfo`` the
  fixed ``datetime.UTC``; a naive time is refused (it would reach Cypher as a
  LocalDateTime, which has no instant, and the guards would silently match
  nothing);
* a real index run - begin_run, presence stamps, retirement, the entity prune,
  the shared-module prune cutoffs, lifecycle ledger times read back from a
  PostgreSQL session in another zone - sends no datetime parameter to the
  driver that is not at the UTC offset, even when the caller hands it a run
  start in a named zone.

Real case: the neo4j 5.28 driver packs an aware datetime by calling
``tzinfo.utcoffset(<neo4j.time.DateTime>)``; CPython's C ``zoneinfo`` reads that
non-datetime as a datetime struct, so a NAMED zone intermittently SIGSEGVs the
whole process (reproduced on the pre-F52 tree: the retirement guard crashed
pytest). These tests NEVER hand the driver a named-zone value: the unit tests
make no driver call, and the index-run spy checks every parameter BEFORE the
driver sees it and refuses to forward an offending one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.indexer.writer_neo4j import utc_for_neo4j

HCM = ZoneInfo("Asia/Ho_Chi_Minh")
# 2026-09-24T11:48:20.123+07:00 == 04:48:20.123Z
LOCAL = datetime(2026, 9, 24, 11, 48, 20, 123000)
INSTANT = datetime(2026, 9, 24, 4, 48, 20, 123000, tzinfo=UTC)


def _pytz_hcm() -> datetime:
    pytz = pytest.importorskip("pytz")
    return pytz.timezone("Asia/Ho_Chi_Minh").localize(LOCAL)


def _neo4j_datetime() -> object:
    from neo4j.time import DateTime

    # A fixed offset: building / unpacking it never goes through C zoneinfo.
    return DateTime.from_native(LOCAL.replace(tzinfo=timezone(timedelta(hours=7))))


AWARE_FORMS = {
    "zoneinfo_named_zone": lambda: LOCAL.replace(tzinfo=HCM),
    "zoneinfo_utc": lambda: INSTANT.replace(tzinfo=ZoneInfo("UTC")),
    "pytz_named_zone": _pytz_hcm,
    "fixed_offset_plus_7": lambda: LOCAL.replace(tzinfo=timezone(timedelta(hours=7))),
    "fixed_utc": lambda: INSTANT,
    "neo4j_driver_datetime": _neo4j_datetime,
}


@pytest.mark.parametrize("form", sorted(AWARE_FORMS))
def test_any_aware_time_becomes_the_same_instant_at_the_fixed_utc_offset(form):
    """A run start in Asia/Ho_Chi_Minh (zoneinfo or pytz), a fixed +07:00, a
    ZoneInfo UTC or a driver DateTime is the instant 04:48:20.123Z, sent with the
    fixed ``datetime.UTC`` tzinfo - never a zoneinfo / pytz object."""
    value = AWARE_FORMS[form]()

    result = utc_for_neo4j(value, "run_started_at")

    assert isinstance(result, datetime)
    assert result == INSTANT, f"{form}: the instant changed ({result!r})"
    assert result.tzinfo is UTC, f"{form}: tzinfo {result.tzinfo!r} is not the fixed UTC offset"


def test_naive_time_is_refused_with_the_parameter_named():
    """A naive datetime has no instant; it is refused, naming the parameter."""
    with pytest.raises(ValueError, match="run_started_at"):
        utc_for_neo4j(LOCAL, "run_started_at")
    with pytest.raises(ValueError, match="since"):
        utc_for_neo4j("2026-09-24T04:48:20Z", "since")


# ---------------------------------------------------------------------------
# Guard: nothing the indexer sends to the driver is outside the UTC offset
# ---------------------------------------------------------------------------

def _datetimes(value):
    """Every datetime-like object nested in a parameter value."""
    from neo4j.time import DateTime as Neo4jDateTime

    if isinstance(value, datetime | Neo4jDateTime):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _datetimes(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for v in value:
            yield from _datetimes(v)


def _offending(value) -> bool:
    """A datetime the driver must not pack: aware and either not at offset 0 or
    carried by a zoneinfo zone (the C path, even for ZoneInfo('UTC'))."""
    tz = getattr(value, "tzinfo", None)
    if tz is None:
        return False
    if isinstance(tz, ZoneInfo):
        return True
    offset = value.utcoffset() if isinstance(value, datetime) else tz.utcoffset(
        value.to_native().replace(tzinfo=None)
    )
    return offset != timedelta(0)


class _ParamSpy:
    """Checks the parameters of every Session.run / Transaction.run before the
    driver packs them; an offending one is recorded and NOT forwarded."""

    def __init__(self) -> None:
        self.seen = 0
        self.violations: list[str] = []

    def check(self, query, parameters, kwargs) -> bool:
        params = {**(parameters or {}), **kwargs}
        bad = []
        for key, value in params.items():
            for dt in _datetimes(value):
                self.seen += 1
                if _offending(dt):
                    bad.append(f"${key}={dt!r}")
        if bad:
            text = str(getattr(query, "text", query)).strip().splitlines()[0][:120]
            self.violations.append(f"{', '.join(bad)} in: {text}")
        return not bad

    def wrap(self, original):
        spy = self

        def run(self_, query, parameters=None, **kwargs):
            if not spy.check(query, parameters, kwargs):
                raise AssertionError(
                    "a non-UTC datetime parameter was about to reach the Neo4j driver"
                )
            return original(self_, query, parameters, **kwargs)

        return run


def test_spy_flags_a_named_zone_parameter_without_calling_the_driver():
    """Positive control of the guard below: the spy catches a named-zone value
    and a +07:00 offset, and passes UTC (no driver involved)."""
    spy = _ParamSpy()
    assert spy.check("RETURN $t", {"t": LOCAL.replace(tzinfo=HCM)}, {}) is False
    plus_7 = LOCAL.replace(tzinfo=timezone(timedelta(hours=7)))
    assert spy.check("RETURN $t", None, {"t": [plus_7]}) is False
    assert spy.check("RETURN $t", {"t": INSTANT}, {}) is True
    assert spy.seen == 3 and len(spy.violations) == 2


@pytest.mark.postgres
@pytest.mark.neo4j
def test_index_runs_send_the_driver_only_utc_datetimes_even_from_a_named_zone_start(
    clean_pg, clean_neo4j, tmp_path, monkeypatch,
):
    """Two repos ship viin_ai_rag (shared) and repo a also ships rag_gone. Both
    copies drop rag_note and a deletes rag_gone. Every run gets its run start in
    Asia/Ho_Chi_Minh and the run's PostgreSQL session is in that zone too
    (ledger times come back at +07:00). Across the runs - begin_run, presence
    stamps, retirement of rag_gone, the shared-module prune of rag_note - the
    driver receives datetime parameters, and every one is at the UTC offset."""
    import os

    import neo4j._sync.work.session as session_mod
    import neo4j._sync.work.transaction as tx_mod

    from src.db.migrate import run_migrations
    from src.indexer.writer_neo4j import Neo4jWriter
    from tests._lifecycle_repo import GitRepo, register, run, write_module

    pg = clean_pg
    run_migrations(pg)
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        write_module(repo, "viin_ai_rag", extra_field="rag_note = fields.Text()")
        repo.commit("copy with rag_note")
    write_module(a, "rag_gone")
    a.commit("rag_gone")
    register("pa_99", a)
    register("pb_99", b)

    probe = Neo4jWriter(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                        os.environ["NEO4J_PASSWORD"])

    def hcm_start() -> datetime:
        # The server clock, handed over in a named zone (as a caller in +07 would).
        return probe.server_now().astimezone(HCM)

    spy = _ParamSpy()
    monkeypatch.setattr(session_mod.Session, "run", spy.wrap(session_mod.Session.run))
    monkeypatch.setattr(tx_mod.TransactionBase, "run", spy.wrap(tx_mod.TransactionBase.run))
    with pg.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Ho_Chi_Minh'")
    try:
        for profile in ("pa_99", "pb_99"):
            run(pg, profile, run_started_at=hcm_start())
        for repo in (a, b):
            write_module(repo, "viin_ai_rag")
            if repo is a:
                repo.rm("rag_gone")
            repo.commit("[REM] drop rag_note (and rag_gone in a)")
        for profile in ("pa_99", "pb_99", "pa_99"):
            run(pg, profile, run_started_at=hcm_start())
    finally:
        with pg.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        monkeypatch.undo()
        probe.close()

    assert spy.violations == [], spy.violations
    assert spy.seen > 0, "the spy saw no datetime parameter - it proves nothing"
    with clean_neo4j.session() as s:
        left = s.run(
            "MATCH (f:Field {name: 'rag_note', odoo_version: '99.0'}) RETURN count(f) AS n"
        ).single()["n"]
        gone = s.run(
            "MATCH (m:Module {name: 'rag_gone', odoo_version: '99.0'}) RETURN count(m) AS n"
        ).single()["n"]
    assert (left, gone) == (0, 0), "precondition: the prune and the retirement both ran"
