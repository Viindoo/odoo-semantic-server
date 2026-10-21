# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_admin_lifecycle_page.py
"""Admin module lifecycle page API (#380 item 4) - no database, no graph.

Business rules protected here:

- Only a global admin sees the page's data. Admin-ness is read from the
  database by ``is_admin_session`` on every request (ADR-0011 / ADR-0026); a
  non-admin gets 403 and NO store or audit is touched; no session -> 401.
- The overview tells an admin, per repo, whether the ledger reflects the last
  indexed HEAD (``synced``), whether a checkout exists on disk, and the full
  attention text; per version the latest retirements with their removing
  commit and successor, newest version first. A broken retirement read never
  hides the repo view.
- The findings come from ``lifecycle-audit`` itself, run as the weekly timer
  runs it narrowed to one version, as a child process the page can kill;
  a report WITH findings is still a report (no ``--fail-on-findings``).
- Bounded: one audit at a time per process (a second version is told
  ``busy``), a version's report is reused for the TTL, ``force`` re-runs, a
  failed run is shown and keeps the previous report.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import pytest

from src.web_ui.helpers import lifecycle_audit_runner as runner_mod
from src.web_ui.helpers.lifecycle_audit_runner import (
    START_BUSY,
    START_FRESH,
    START_RUNNING,
    AuditFailed,
    LifecycleAuditCache,
)
from src.web_ui.routes import admin_lifecycle

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-admin-lifecycle-page-32b!!")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _GatedRunner:
    """An audit runner whose runs finish only when the test releases them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.fail: dict[str, str] = {}

    async def __call__(self, version: str, *, timeout: float) -> dict:
        self.calls.append(version)
        gate = self.gates.setdefault(version, asyncio.Event())
        await gate.wait()
        gate.clear()
        if version in self.fail:
            raise AuditFailed(self.fail.pop(version))
        return {"schema": "osm.lifecycle-audit/5", "scope": {"version": version},
                "run": len(self.calls), "findings": {"parser_gaps": 1}}

    def release(self, version: str) -> None:
        self.gates.setdefault(version, asyncio.Event()).set()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def page_cache(monkeypatch):
    """Every test runs against a fake audit runner: never the real audit."""
    runner = _GatedRunner()
    clock = _Clock()
    cache = LifecycleAuditCache(runner, ttl=600, timeout=30, clock=clock)
    monkeypatch.setattr(admin_lifecycle, "_CACHE", cache)
    return cache, runner, clock


@pytest.fixture
def admin(monkeypatch):
    """Toggle what the DB-sourced admin check answers; record its calls."""
    import src.web_ui.auth as auth_mod

    state = {"is_admin": True, "calls": 0}

    def fake_is_admin_session(request):
        state["calls"] += 1
        return state["is_admin"]

    monkeypatch.setattr(auth_mod, "is_admin_session", fake_is_admin_session)
    return state


class _FakeRepoStore:
    def __init__(self, repos):
        self.repos = repos
        self.calls = 0

    def list_repos(self):
        self.calls += 1
        return [dict(r) for r in self.repos]


class _FakePresenceStore:
    counts: dict = {}
    retirements: dict | Exception = {}
    calls: list = []

    def __init__(self, pool):
        pass

    def lifecycle_counts(self, repo_ids):
        _FakePresenceStore.calls.append("lifecycle_counts")
        return {rid: self.counts.get(rid, {"present": 0}) for rid in repo_ids}

    def recent_retirements(self, *, per_version):
        _FakePresenceStore.calls.append(("recent_retirements", per_version))
        if isinstance(self.retirements, Exception):
            raise self.retirements
        return self.retirements


@pytest.fixture
def stores(monkeypatch, tmp_path):
    import src.db.module_presence as mp_mod
    import src.db.pg as pg_mod

    checkout = tmp_path / "odoo_17" / "odoo"
    checkout.mkdir(parents=True)
    repos = [
        {"id": 1, "profile_name": "odoo_17", "odoo_version": "17.0",
         "url": "git@github.com:odoo/odoo.git", "branch": "17.0",
         "local_path": str(checkout), "clone_status": "done", "clone_error_msg": None,
         "status": "done", "error_msg": None, "last_indexed_at": None,
         "head_sha": "a" * 40, "presence_head_sha": "a" * 40,
         "lifecycle_attention": None, "lifecycle_attention_at": None},
        {"id": 2, "profile_name": "viindoo_17", "odoo_version": "17.0",
         "url": "git@github.com:Viindoo/erponline-enterprise.git", "branch": "17.0",
         "local_path": str(tmp_path / "missing" / "erponline-enterprise"),
         "clone_status": "error", "clone_error_msg": "Permission denied (publickey)",
         "status": "error", "error_msg": "clone failed", "last_indexed_at": None,
         "head_sha": "b" * 40, "presence_head_sha": "c" * 40,
         "lifecycle_attention": "checkout unavailable: clone failed; the next index "
                                "run retries (repo erponline-enterprise of tenant X)",
         "lifecycle_attention_at": None},
        {"id": 3, "profile_name": "odoo_16", "odoo_version": "16.0",
         "url": "git@github.com:odoo/odoo.git", "branch": "16.0",
         "local_path": str(checkout), "clone_status": "manual", "clone_error_msg": None,
         "status": "pending", "error_msg": None, "last_indexed_at": None,
         "head_sha": None, "presence_head_sha": None,
         "lifecycle_attention": None, "lifecycle_attention_at": None},
    ]
    repo_store = _FakeRepoStore(repos)
    _FakePresenceStore.counts = {1: {"present": 600, "excluded": 3, "retired": 2,
                                     "retire_pending": 0, "needs_rewrite": 0}}
    _FakePresenceStore.retirements = {
        "17.0": {"total": 7, "rows": [
            {"name": "viin_ai_rag", "odoo_version": "17.0", "repo_basename": "tvtmaaddons",
             "retire_reason": "absent", "removing_commit_sha": "676cf82",
             "removing_commit_subject": "[MERGE] viin_ai_rag: merge into viin_ai",
             "successor_names": ["viin_ai"], "successor_source": "git_merge"},
        ]},
        "8.0": {"total": 1, "rows": []},
    }
    _FakePresenceStore.calls = []
    monkeypatch.setattr(pg_mod, "repo_store", lambda: repo_store)
    monkeypatch.setattr(pg_mod, "get_pool", lambda: None)
    monkeypatch.setattr(mp_mod, "ModulePresenceStore", _FakePresenceStore)
    return repo_store


def _client():
    from src.web_ui.app import create_app

    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")


# ---------------------------------------------------------------------------
# Access: global admin only, DB-sourced
# ---------------------------------------------------------------------------

@pytest.mark.http
class TestAdminOnly:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/admin/lifecycle"),
        ("GET", "/api/admin/lifecycle/audit/17.0"),
        ("POST", "/api/admin/lifecycle/audit/17.0"),
    ])
    async def test_a_non_admin_gets_403_and_nothing_is_read_or_started(
            self, admin, stores, page_cache, method, path):
        cache, runner, _clock = page_cache
        admin["is_admin"] = False
        async with _client() as c:
            r = await c.request(method, path)
        assert r.status_code == 403
        assert "repos" not in r.text and "report" not in r.text
        assert admin["calls"] == 1, "the admin check must come from is_admin_session"
        assert stores.calls == 0 and _FakePresenceStore.calls == []
        await _settle()
        assert runner.calls == [] and cache.running_version is None

    async def test_without_a_session_the_answer_is_401(self, admin, stores, monkeypatch):
        import src.web_ui.auth as auth_mod

        monkeypatch.setattr(auth_mod, "current_user_id", lambda request: None)
        async with _client() as c:
            r = await c.get("/api/admin/lifecycle")
        assert r.status_code == 401
        assert stores.calls == 0

    async def test_the_admin_check_is_asked_on_every_request(self, admin, stores):
        """Revoking admin in the database takes effect on the next request."""
        async with _client() as c:
            assert (await c.get("/api/admin/lifecycle")).status_code == 200
            admin["is_admin"] = False
            assert (await c.get("/api/admin/lifecycle")).status_code == 403
        assert admin["calls"] == 2


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@pytest.mark.http
class TestOverview:
    async def _overview(self):
        async with _client() as c:
            r = await c.get("/api/admin/lifecycle")
        assert r.status_code == 200
        return r.json()

    async def test_each_repo_says_whether_the_ledger_reflects_its_last_indexed_head(
            self, admin, stores):
        body = await self._overview()
        by_id = {r["id"]: r for r in body["repos"]}
        assert by_id[1]["synced"] is True
        assert by_id[2]["synced"] is False, "ledger HEAD c... behind indexed HEAD b..."
        assert by_id[3]["synced"] is False, "never indexed is not synced"
        assert (by_id[2]["head_sha"], by_id[2]["presence_head_sha"]) == ("b" * 40, "c" * 40)

    async def test_a_repo_without_a_checkout_on_disk_is_marked_unavailable(
            self, admin, stores):
        by_id = {r["id"]: r for r in (await self._overview())["repos"]}
        assert by_id[1]["checkout_on_disk"] is True
        assert by_id[2]["checkout_on_disk"] is False
        assert by_id[2]["clone_status"] == "error"
        assert by_id[2]["clone_error_msg"] == "Permission denied (publickey)"

    async def test_the_admin_sees_the_full_attention_text_and_ledger_counts(
            self, admin, stores):
        by_id = {r["id"]: r for r in (await self._overview())["repos"]}
        assert by_id[2]["lifecycle_attention"].startswith("checkout unavailable")
        assert by_id[1]["lifecycle_counts"]["present"] == 600
        assert by_id[1]["basename"] == "odoo"

    async def test_versions_newest_first_with_their_latest_retirements(self, admin, stores):
        body = await self._overview()
        assert [v["odoo_version"] for v in body["versions"]] == ["17.0", "16.0", "8.0"]
        v17 = body["versions"][0]
        assert v17["repo_ids"] == [1, 2]
        assert v17["retired_total"] == 7
        row = v17["recent_retirements"][0]
        assert (row["name"], row["removing_commit_sha"], row["successor_names"],
                row["successor_source"]) == ("viin_ai_rag", "676cf82", ["viin_ai"], "git_merge")
        assert body["versions"][2]["repo_ids"] == [], "a version only the ledger knows"
        assert ("recent_retirements", admin_lifecycle.RECENT_RETIREMENTS_PER_VERSION) in (
            _FakePresenceStore.calls)

    async def test_a_broken_retirement_read_still_shows_the_repos(self, admin, stores):
        _FakePresenceStore.retirements = RuntimeError("relation does not exist")
        body = await self._overview()
        assert len(body["repos"]) == 3
        assert body["retirements_error"]
        assert all(v["recent_retirements"] == [] for v in body["versions"])

    async def test_the_overview_never_starts_an_audit_and_carries_no_report(
            self, admin, stores, page_cache):
        _cache, runner, _clock = page_cache
        body = await self._overview()
        await _settle()
        assert runner.calls == []
        assert body["versions"][0]["audit"]["state"] == "idle"
        assert "report" not in body["versions"][0]["audit"]


# ---------------------------------------------------------------------------
# Audit: bounded, cached, the audit's own report
# ---------------------------------------------------------------------------

class TestAuditCache:
    async def test_one_audit_at_a_time_and_a_second_request_joins_the_running_one(
            self, page_cache):
        cache, runner, _clock = page_cache
        assert cache.start("17.0") == START_RUNNING
        assert cache.start("17.0") == START_RUNNING
        assert cache.start("16.0") == START_BUSY
        await _settle()
        assert runner.calls == ["17.0"]
        assert cache.snapshot("17.0")["state"] == "running"
        assert cache.snapshot("16.0")["running_version"] == "17.0"

        runner.release("17.0")
        await _settle()
        snap = cache.snapshot("17.0")
        assert snap["state"] == "ready" and snap["fresh"] is True
        assert snap["report"]["scope"] == {"version": "17.0"}
        assert cache.running_version is None
        assert cache.start("16.0") == START_RUNNING, "the slot is free again"

    async def test_a_fresh_report_is_reused_until_the_ttl_then_rerun(self, page_cache):
        cache, runner, clock = page_cache
        cache.start("17.0")
        runner.release("17.0")
        await _settle()
        clock.now += 599
        assert cache.start("17.0") == START_FRESH
        await _settle()
        assert runner.calls == ["17.0"]
        clock.now += 2
        assert cache.snapshot("17.0")["fresh"] is False
        assert cache.start("17.0") == START_RUNNING
        await _settle()
        assert runner.calls == ["17.0", "17.0"]

    async def test_force_reruns_a_fresh_report(self, page_cache):
        cache, runner, _clock = page_cache
        cache.start("17.0")
        runner.release("17.0")
        await _settle()
        assert cache.start("17.0", force=True) == START_RUNNING
        runner.release("17.0")
        await _settle()
        assert runner.calls == ["17.0", "17.0"]
        assert cache.snapshot("17.0")["report"]["run"] == 2

    async def test_a_failed_run_is_shown_and_keeps_the_previous_report(self, page_cache):
        cache, runner, _clock = page_cache
        runner.fail["17.0"] = "the audit exited with code 1: Neo4j unreachable"
        cache.start("17.0")
        runner.release("17.0")
        await _settle()
        snap = cache.snapshot("17.0")
        assert snap["state"] == "error" and snap["report"] is None
        assert "Neo4j unreachable" in snap["error"]

        cache.start("17.0")
        runner.release("17.0")
        await _settle()
        assert cache.snapshot("17.0")["state"] == "ready"
        assert cache.snapshot("17.0")["error"] is None

        runner.fail["17.0"] = "the audit did not finish within 30s and was stopped"
        cache.start("17.0", force=True)
        runner.release("17.0")
        await _settle()
        snap = cache.snapshot("17.0")
        assert snap["state"] == "ready", "the previous report stays visible"
        assert snap["report"]["run"] == 2
        assert "did not finish" in snap["error"]


@pytest.mark.http
class TestAuditRoutes:
    async def test_post_starts_get_polls_then_a_fresh_report_answers_200(
            self, admin, page_cache):
        _cache, runner, _clock = page_cache
        async with _client() as c:
            r = await c.post("/api/admin/lifecycle/audit/17.0")
            assert r.status_code == 202 and r.json()["state"] == "running"
            assert (await c.get("/api/admin/lifecycle/audit/17.0")).json()["state"] == "running"
            runner.release("17.0")
            await _settle()
            got = (await c.get("/api/admin/lifecycle/audit/17.0")).json()
            assert got["state"] == "ready"
            assert got["report"]["findings"] == {"parser_gaps": 1}
            again = await c.post("/api/admin/lifecycle/audit/17.0")
            assert again.status_code == 200 and again.json()["state"] == "ready"
        assert runner.calls == ["17.0"]

    async def test_another_version_while_one_runs_is_409_busy(self, admin, page_cache):
        _cache, runner, _clock = page_cache
        async with _client() as c:
            assert (await c.post("/api/admin/lifecycle/audit/17.0")).status_code == 202
            r = await c.post("/api/admin/lifecycle/audit/16.0")
        assert r.status_code == 409
        assert r.json()["running_version"] == "17.0" and "17.0" in r.json()["busy"]
        await _settle()
        assert runner.calls == ["17.0"]
        runner.release("17.0")
        await _settle()

    async def test_get_never_starts_an_audit(self, admin, page_cache):
        _cache, runner, _clock = page_cache
        async with _client() as c:
            r = await c.get("/api/admin/lifecycle/audit/17.0")
        await _settle()
        assert r.json()["state"] == "idle" and runner.calls == []

    @pytest.mark.parametrize("bad", ["17", "latest", "17.0;rm", "1000.0", "17.0%0A"])
    async def test_a_malformed_version_is_refused_before_anything_runs(
            self, admin, page_cache, bad):
        _cache, runner, _clock = page_cache
        async with _client() as c:
            r = await c.post(f"/api/admin/lifecycle/audit/{bad}")
        await _settle()
        assert r.status_code == 422
        assert runner.calls == []


# ---------------------------------------------------------------------------
# The child process: the timer's audit, one version, killable
# ---------------------------------------------------------------------------

class TestAuditProcess:
    def test_the_page_runs_the_weekly_audit_narrowed_to_the_version(self):
        argv = runner_mod.audit_argv("17.0")
        assert argv[:3] == [sys.executable, "-m", "src.indexer"]
        assert argv[3] == "lifecycle-audit"
        assert {"--all", "--json"} <= set(argv)
        assert argv[argv.index("--version") + 1] == "17.0"
        # A report with findings is what the page shows; --fail-on-findings
        # would turn it into exit 4, i.e. an error with no report.
        assert "--fail-on-findings" not in argv

    @staticmethod
    def _fake_audit(monkeypatch, code: str) -> None:
        monkeypatch.setattr(
            runner_mod, "audit_argv", lambda version: [sys.executable, "-c", code],
        )

    async def test_the_report_printed_by_the_audit_is_returned(self, monkeypatch):
        from src.indexer.lifecycle_audit import AUDIT_SCHEMA

        report = {"schema": AUDIT_SCHEMA, "repos": [], "versions": [], "findings": {}}
        self._fake_audit(monkeypatch, f"print({json.dumps(json.dumps(report))})")
        assert await runner_mod.run_audit_process("17.0", timeout=30) == report

    async def test_a_failing_audit_reports_its_exit_code_and_last_error_line(
            self, monkeypatch):
        self._fake_audit(
            monkeypatch,
            "import sys; print('warning: x', file=sys.stderr); "
            "print('Neo4j unreachable', file=sys.stderr); sys.exit(1)",
        )
        with pytest.raises(AuditFailed, match="code 1: Neo4j unreachable"):
            await runner_mod.run_audit_process("17.0", timeout=30)

    async def test_an_audit_past_its_timeout_is_killed(self, monkeypatch, tmp_path):
        marker = tmp_path / "pid"
        self._fake_audit(
            monkeypatch,
            f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(60)",
        )
        started = time.monotonic()
        with pytest.raises(AuditFailed, match="did not finish within"):
            await runner_mod.run_audit_process("17.0", timeout=1.5)
        assert time.monotonic() - started < 20
        pid = int(marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_a_timed_out_audit_takes_its_own_subprocesses_down(
            self, monkeypatch, tmp_path):
        """The audit runs git; a timeout must not leave those children running."""
        marker = tmp_path / "grandchild"
        self._fake_audit(
            monkeypatch,
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(marker)!r}, 'w').write(str(p.pid)); time.sleep(60)",
        )
        with pytest.raises(AuditFailed, match="did not finish within"):
            await runner_mod.run_audit_process("17.0", timeout=1.5)
        pid = int(marker.read_text())

        def alive() -> bool:
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                return False

        deadline = time.monotonic() + 5
        while alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        try:
            assert not alive(), "the audit's subprocess survived the timeout"
        finally:
            if alive():
                os.kill(pid, 9)

    async def test_a_report_of_another_schema_is_refused(self, monkeypatch):
        self._fake_audit(monkeypatch, "print('{\"schema\": \"osm.lifecycle-audit/3\"}')")
        with pytest.raises(AuditFailed, match="restart the Web UI"):
            await runner_mod.run_audit_process("17.0", timeout=30)

    async def test_output_that_is_not_json_is_an_error(self, monkeypatch):
        self._fake_audit(monkeypatch, "print('lifecycle-audit (dry run, ...)')")
        with pytest.raises(AuditFailed, match="JSON report"):
            await runner_mod.run_audit_process("17.0", timeout=30)
