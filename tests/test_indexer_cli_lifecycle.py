# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI contract of module retirement (ADR-0056 B9, review H4) - pure unit, no DB.

* Retirement is not behind a flag: a plain ``index-repo`` asks for it.
* ``--gc`` is still accepted (existing timers and the Web UI still pass it) but
  is a deprecated no-op: it logs a deprecation and changes nothing in the run.
* ``--no-retire`` / ``--allow-mass-retire`` reach the index run.
* A run whose lifecycle needs attention (tripped gate, undecidable name,
  reconcile error) exits 3 so the systemd ``OnFailure=`` alert fires, while the
  job itself is still marked done (the index WAS written).
"""
import logging
from unittest.mock import MagicMock

import pytest

import src.indexer.__main__ as main_mod


def _lifecycle(**over) -> dict:
    lc = {
        "versions": ["17.0"], "gates_tripped": [], "undecidable": [], "errors": [],
        "deferred_presence": {}, "reports": [], "needs_attention": False,
    }
    lc.update(over)
    return lc


@pytest.fixture
def captured(monkeypatch):
    """Patch the DB + index entry points; record every index call's kwargs."""
    calls: dict[str, list[dict]] = {"profile": [], "all": []}
    state = {"lifecycle": _lifecycle()}

    def fake_profile(pg, **kw):
        calls["profile"].append(kw)
        return {"modules": 1, "views": 0, "qweb": 0, "lifecycle": dict(state["lifecycle"])}

    def fake_all(pg, **kw):
        calls["all"].append(kw)
        return {"profiles_ok": 1, "modules": 1, "lifecycle": dict(state["lifecycle"])}

    monkeypatch.setattr(main_mod, "open_production_pg", lambda: MagicMock())
    monkeypatch.setattr(main_mod, "index_profile", fake_profile)
    monkeypatch.setattr(main_mod, "index_all", fake_all)
    job_updates: list[dict] = []
    monkeypatch.setattr(
        main_mod.job_registry, "update_job",
        lambda _pg, _id, **kw: job_updates.append(kw), raising=False,
    )
    return calls, state, job_updates


def test_plain_index_repo_retires_without_any_flag(captured):
    calls, _state, _jobs = captured

    rc = main_mod.main(["index-repo", "--profile", "p1", "--no-embed"])

    assert rc == 0
    (kw,) = calls["profile"]
    assert kw.get("retire") is True
    assert kw.get("allow_mass_retire") is False


@pytest.mark.parametrize("scope", [["--profile", "p1"], ["--all"]], ids=["profile", "all"])
def test_gc_is_accepted_logs_a_deprecation_and_changes_nothing(captured, caplog, scope):
    calls, _state, _jobs = captured
    key = "all" if scope == ["--all"] else "profile"

    main_mod.main(["index-repo", *scope, "--no-embed"])
    with caplog.at_level(logging.WARNING):
        rc = main_mod.main(["index-repo", *scope, "--no-embed", "--gc"])

    assert rc == 0
    plain, with_gc = calls[key]
    assert with_gc == plain, "--gc must not change how the run is driven"
    assert any(
        "--gc" in r.getMessage() and "deprecated" in r.getMessage()
        for r in caplog.records if r.levelno == logging.WARNING
    ), [r.getMessage() for r in caplog.records]


def test_lifecycle_flags_reach_the_run(captured):
    calls, _state, _jobs = captured

    main_mod.main(["index-repo", "--all", "--no-embed", "--no-retire"])
    main_mod.main(["index-repo", "--all", "--no-embed", "--allow-mass-retire"])

    no_retire, allow = calls["all"]
    assert (no_retire["retire"], no_retire["allow_mass_retire"]) == (False, False)
    assert (allow["retire"], allow["allow_mass_retire"]) == (True, True)


@pytest.mark.parametrize("trouble", [
    {"gates_tripped": ["17.0: orphan_sweep:mass_retire"]},
    {"undecidable": ["17.0: shared_mod"]},
    {"errors": ["17.0: reconcile: LifecycleLockTimeout: waited"]},
], ids=["gate", "undecidable", "error"])
def test_lifecycle_trouble_exits_3_but_the_job_is_done(captured, capsys, trouble):
    _calls, state, jobs = captured
    state["lifecycle"] = _lifecycle(needs_attention=True, **trouble)

    rc = main_mod.main(["index-repo", "--profile", "p1", "--no-embed", "--job-id", "7"])

    assert rc == 3
    assert jobs[-1]["status"] == "done", "the index was written; only the alert fires"
    detail = next(iter(trouble.values()))[0]
    assert detail in capsys.readouterr().err


# GUARD: pre-existing behaviour (a clean run exits 0)
def test_clean_lifecycle_exits_0(captured):
    assert main_mod.main(["index-repo", "--profile", "p1", "--no-embed"]) == 0


# GUARD: pre-existing behaviour (argv --gc accepted, L8)
def test_web_ui_gc_argv_still_parses():
    """The Web UI index route still forwards ``--gc``; the CLI must keep parsing it."""
    args = main_mod._build_parser().parse_args(["index-repo", "--profile", "p", "--gc"])
    assert args.gc is True
