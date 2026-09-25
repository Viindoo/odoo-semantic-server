# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web UI job tracking contract of the indexer CLI (#381 F2) - pure unit, no DB.

The Web UI starts every indexer run through ``spawn_indexer_subcommand``,
which passes ``--job-id N`` so the child can report running / done / error on
its ``indexer_jobs`` row. A subcommand that does not declare ``--job-id``
dies on argparse exit 2 before indexing anything and its job stays
``queued`` forever - the Web UI "index core" button did exactly that.

* every subcommand the Web UI spawns parses with ``--job-id 1``;
* ``index-core --job-id`` reports running, then done (or error on failure);
* the spawn helper never hands ``--job-id`` to a subcommand that rejects it.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import signal
from unittest.mock import MagicMock

import pytest

import src.indexer.__main__ as main_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WEB_UI_DIR = _REPO_ROOT / "src" / "web_ui"


def _web_ui_spawned_subcommands() -> set[str]:
    """Subcommand names the Web UI passes to ``spawn_indexer_subcommand``.

    Every call site builds a local list whose first element is the literal
    subcommand (``argv = ["index-core", ...]``) in the function that calls the
    helper; collect those first elements per calling function.
    """
    found: set[str] = set()
    for path in sorted(_WEB_UI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [
                n for n in ast.walk(func)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None))
                == "spawn_indexer_subcommand"
            ]
            if not calls:
                continue
            heads = {
                n.value.elts[0].value
                for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.List)
                and n.value.elts
                and isinstance(n.value.elts[0], ast.Constant)
                and isinstance(n.value.elts[0].value, str)
            }
            assert heads, f"{path.name}:{func.name} spawns an indexer run from no literal argv"
            found |= heads
    return found


def _minimal_argv(subcommand: str) -> list[str]:
    """``[subcommand, <every required option filled>]`` from the real parser."""
    parser = main_mod._build_parser()
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    sub = sub_action.choices[subcommand]
    argv = [subcommand]
    for action in sub._actions:
        if action.required and action.option_strings:
            argv += [action.option_strings[0], "x"]
    for group in sub._mutually_exclusive_groups:
        if group.required:
            first = group._group_actions[0]
            argv.append(first.option_strings[0])
            if first.nargs != 0:
                argv.append("x")
    return argv


def test_the_web_ui_spawns_the_known_subcommands():
    # GUARD: the AST scan finds the call sites (an empty set would make the
    # contract below vacuous).
    assert {"index-repo", "index-core", "seed-patterns"} <= _web_ui_spawned_subcommands()


@pytest.mark.parametrize("subcommand", sorted(_web_ui_spawned_subcommands()))
def test_every_web_ui_subcommand_accepts_job_id(subcommand):
    argv = _minimal_argv(subcommand) + ["--job-id", "1"]
    args = main_mod._build_parser().parse_args(argv)
    assert args.job_id == 1


@pytest.fixture
def core_run(monkeypatch):
    """Patch the Neo4j run and the PG connection; record every job update."""
    state = {"fail": None, "runs": 0}

    def fake_run_index_core(**_kw):
        state["runs"] += 1
        if state["fail"] is not None:
            raise state["fail"]

    monkeypatch.setattr(main_mod, "_run_index_core", fake_run_index_core)
    monkeypatch.setattr(main_mod, "open_production_pg", lambda: MagicMock())
    updates: list[dict] = []
    monkeypatch.setattr(
        main_mod.job_registry, "update_job",
        lambda _pg, job_id, **kw: updates.append({"job_id": job_id, **kw}),
    )
    previous = signal.getsignal(signal.SIGTERM)
    yield state, updates
    signal.signal(signal.SIGTERM, previous)  # main() installs its own


_CORE_ARGV = ["index-core", "--source", "/src", "--version", "17.0"]


def test_index_core_with_job_id_reports_running_then_done(core_run):
    state, updates = core_run

    rc = main_mod.main([*_CORE_ARGV, "--job-id", "5"])

    assert rc == 0
    assert state["runs"] == 1
    assert [u["status"] for u in updates] == ["running", "done"]
    assert {u["job_id"] for u in updates} == {5}
    assert updates[0]["pid"] and updates[0]["started_at"]
    assert updates[1]["finished_at"]


def test_a_failed_index_core_leaves_its_job_in_error(core_run):
    state, updates = core_run
    state["fail"] = RuntimeError("neo4j unreachable")

    with pytest.raises(RuntimeError):
        main_mod.main([*_CORE_ARGV, "--job-id", "5"])

    assert [u["status"] for u in updates] == ["running", "error"]
    assert "neo4j unreachable" in updates[-1]["error_msg"]


def test_index_core_without_job_id_touches_no_job(core_run):
    state, updates = core_run
    assert main_mod.main(_CORE_ARGV) == 0
    assert state["runs"] == 1
    assert updates == []


class _FakeJobStore:
    def __init__(self):
        self.created: list[str] = []
        self.updates: list[dict] = []

    def create_job(self, label):
        self.created.append(label)
        return 42

    def update_job(self, job_id, **kw):
        self.updates.append({"job_id": job_id, **kw})


@pytest.fixture
def spawn(monkeypatch):
    from src.web_ui.helpers import subprocess_runner

    store = _FakeJobStore()
    popen_calls: list[list[str]] = []

    class _Proc:
        pid = 31337

        def wait(self):
            return 0

    def fake_popen(argv, **_kw):
        popen_calls.append(argv)
        return _Proc()

    monkeypatch.setattr(subprocess_runner, "job_store", lambda: store)
    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", fake_popen)
    return subprocess_runner, store, popen_calls


def test_spawn_passes_job_id_to_index_core(spawn):
    runner, store, popen_calls = spawn

    job_id = runner.spawn_indexer_subcommand(
        ["index-core", "--source", "/src", "--version", "17.0"], job_label="core:17.0",
    )

    assert job_id == 42
    (argv,) = popen_calls
    assert argv[-2:] == ["--job-id", "42"]
    # The child's own argv parses (no argparse exit 2 in the child).
    main_mod._build_parser().parse_args(argv[argv.index("index-core"):])


def test_spawn_refuses_a_subcommand_that_cannot_report_its_job(spawn):
    runner, store, popen_calls = spawn

    with pytest.raises(ValueError, match="--job-id"):
        runner.spawn_indexer_subcommand(
            ["audit-repo", "--profile", "p", "--output", "/tmp/x.json"], job_label="p",
        )

    assert store.created == [], "no job row may be left queued for a run that cannot report"
    assert popen_calls == []


def test_index_core_without_job_id_keeps_the_default_sigterm(core_run):
    """Nothing to record, so no handler: SIGTERM keeps killing the process
    (exit 143) as before, like a plain CLI run."""
    before = signal.getsignal(signal.SIGTERM)
    main_mod.main(_CORE_ARGV)
    assert signal.getsignal(signal.SIGTERM) is before


def test_index_core_with_job_id_installs_a_sigterm_handler(core_run):
    before = signal.getsignal(signal.SIGTERM)
    main_mod.main([*_CORE_ARGV, "--job-id", "5"])
    assert signal.getsignal(signal.SIGTERM) is not before
