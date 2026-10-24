# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-repo dependency propagation re-writes the dependent MODULES, not whole repos (no DB).

Business rules protected here (ADR-0007 W14 note 3):

- A change to an upstream module flags, in the lifecycle ledger, only the
  modules of other repos that depend on it - directly or transitively - for a
  rewrite on their repo's next run. The other modules of those repos are not
  touched, and no repo loses its ``head_sha`` (that forced a full re-index
  with a full re-embed of every module of the repo).
- A transitive dependent is reached even through an unchanged module of the
  repo that was just indexed, or through a third repo.
- The repo that was just indexed never flags its own modules.
- A dependent repo without ledger rows at the version cannot be flagged per
  module: its ``head_sha`` is reset, as before the ledger existed. The same
  reset is the fallback when the run has no ledger or the flag write fails,
  and it covers the repos of EVERY dependent, transitive ones included.
- The dependency walk ends on a dependency cycle.
- Every run mode propagates what it changed: the incremental diff, the sync
  path's one-shot seeds, a full run's changes since its baseline.
- A propagation that cannot be recorded in full keeps the repo's head_sha
  (the next run repeats it), is named in ``lifecycle_attention``, and is an
  alert only when it was not recorded in the previous run either.
"""
from __future__ import annotations

import logging

import pytest

from src.indexer import cross_repo
from src.indexer.cross_repo import find_dependent_modules, propagate_to_dependents

V = "99.0"


class _Graph:
    """``DEPENDS_ON`` edges at one version: module -> (repo basename, depends)."""

    def __init__(self, modules: dict[str, tuple[str, list[str]]]) -> None:
        self.modules = modules
        self.queries = 0

    def direct_dependents(self, frontier: list[str], seen: list[str]) -> list[dict]:
        self.queries += 1
        return [
            {"name": name, "repo": repo}
            for name, (repo, deps) in sorted(self.modules.items())
            if name not in seen and any(d in frontier for d in deps)
        ]


class _Driver:
    def __init__(self, graph: _Graph) -> None:
        self.graph = graph

    def session(self):
        graph = self.graph

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def data(self):
                return self._rows

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, query, *, version, frontier, seen):
                assert query == cross_repo._CYPHER_DIRECT_DEPENDENT_MODULES
                assert version == V
                return _Result(graph.direct_dependents(frontier, seen))

        return _Session()


class _Ledger:
    """``module_presence`` rows: (repo_id, name, state) at version V."""

    def __init__(self, rows: list[tuple[int, str, str]], *, fail: bool = False) -> None:
        self.rows = rows
        self.flagged: set[tuple[int, str]] = set()
        self.fail = fail

    def mark_dependents_needs_rewrite(self, odoo_version, names, *, exclude_repo_ids=()):
        if self.fail:
            raise RuntimeError("ledger lock retire:99.0 not acquired within 30s")
        assert odoo_version == V
        hit = sorted(
            (rid, name) for rid, name, state in self.rows
            if name in names and state == "present" and rid not in exclude_repo_ids
        )
        self.flagged.update(hit)
        return hit

    def repo_ids_without_rows(self, repo_ids, odoo_version):
        have = {rid for rid, _name, _state in self.rows}
        return [i for i in sorted(repo_ids) if i not in have]


class _Repos:
    """``repos`` rows: basename -> id at version V, and the head_sha resets."""

    def __init__(self, ids_by_basename: dict[str, int]) -> None:
        self.ids_by_basename = ids_by_basename
        self.reset: list[int] = []

    def get_repo_ids_by_local_path_basenames(self, basenames, odoo_version):
        assert odoo_version == V
        return [self.ids_by_basename[b] for b in basenames if b in self.ids_by_basename]

    def reset_head_sha(self, repo_ids):
        self.reset.extend(repo_ids)
        return len(repo_ids)


# The observed case (2026-10-24): tvtmaaddons changed to_git; one module of
# saas-infrastructure depends on it, a second one depends on that one, and the
# rest of saas-infrastructure has nothing to do with it.
GRAPH = {
    "to_git": ("tvtmaaddons", []),
    "to_base": ("tvtmaaddons", []),
    "to_git_ui": ("tvtmaaddons", ["to_git"]),
    "to_server_ssh_git": ("saas-infrastructure", ["to_git"]),
    "to_odoo_saas": ("saas-infrastructure", ["to_server_ssh_git"]),
    "to_dns": ("saas-infrastructure", ["to_base"]),
    "viin_saas_portal": ("erponline-enterprise", ["to_git_ui"]),
}
LEDGER_ROWS = [
    (31, "to_git", "present"), (31, "to_base", "present"), (31, "to_git_ui", "present"),
    (42, "to_server_ssh_git", "present"), (42, "to_odoo_saas", "present"),
    (42, "to_dns", "present"),
    (32, "viin_saas_portal", "present"),
]
REPOS = {"tvtmaaddons": 31, "saas-infrastructure": 42, "erponline-enterprise": 32}


def _propagate(changed, *, ledger=None, repos=None, graph=GRAPH):
    repos = repos if repos is not None else _Repos(REPOS)
    result = propagate_to_dependents(
        _Driver(_Graph(graph)), V, set(changed),
        exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
        presence=ledger, repo_store=repos,
    )
    return result, repos


def test_an_upstream_change_flags_only_the_dependent_modules_of_another_repo():
    ledger = _Ledger(LEDGER_ROWS)

    result, repos = _propagate({"to_git"}, ledger=ledger)

    assert result.marked[42] == ["to_odoo_saas", "to_server_ssh_git"]
    assert (42, "to_dns") not in ledger.flagged
    assert repos.reset == [], "no repo may lose its head_sha (full re-index)"


def test_a_transitive_dependent_is_reached_through_an_unchanged_module_of_the_indexed_repo():
    ledger = _Ledger(LEDGER_ROWS)

    result, _repos = _propagate({"to_git"}, ledger=ledger)

    # erponline's viin_saas_portal depends on tvtmaaddons' unchanged to_git_ui.
    assert result.marked[32] == ["viin_saas_portal"]


def test_the_indexed_repo_never_flags_its_own_modules():
    ledger = _Ledger(LEDGER_ROWS)

    result, _repos = _propagate({"to_git"}, ledger=ledger)

    assert 31 not in result.marked
    assert (31, "to_git_ui") not in ledger.flagged


def test_a_dependent_repo_without_ledger_rows_is_reset_instead():
    ledger = _Ledger([r for r in LEDGER_ROWS if r[0] != 42])

    result, repos = _propagate({"to_git"}, ledger=ledger)

    assert repos.reset == [42]
    assert result.reset_repo_ids == [42]
    assert result.marked == {32: ["viin_saas_portal"]}


def test_without_a_ledger_the_repos_of_every_dependent_are_reset():
    result, repos = _propagate({"to_git"}, ledger=None)

    # saas-infrastructure ships a DIRECT dependent; erponline only a transitive
    # one, which a one-hop reset never reaches (a ledger-less full re-index
    # does not propagate further).
    assert repos.reset == [32, 42]
    assert result.marked == {}


def test_a_failed_flag_write_resets_the_repos_of_every_dependent_transitive_included(caplog):
    """erponline-enterprise ships only a depth-2 dependent: a reset of the
    direct dependents' repos left it with stale edges while the propagation
    counted as recorded."""
    ledger = _Ledger(LEDGER_ROWS, fail=True)

    with caplog.at_level(logging.WARNING, logger="src.indexer.cross_repo"):
        result, repos = _propagate({"to_git"}, ledger=ledger)

    assert repos.reset == [32, 42]
    assert result.reset_repo_ids == [32, 42]
    assert result.marked == {}
    assert result.complete is True
    assert "could not flag" in caplog.text


def test_a_change_nothing_depends_on_flags_and_resets_nothing():
    ledger = _Ledger(LEDGER_ROWS)

    result, repos = _propagate({"viin_saas_portal"}, ledger=ledger)

    assert result.dependents == 0
    assert ledger.flagged == set()
    assert repos.reset == []


def test_the_dependency_walk_ends_on_a_cycle():
    graph = {
        "a": ("r1", ["c"]),
        "b": ("r2", ["a"]),
        "c": ("r2", ["b"]),
    }

    found = find_dependent_modules(_Driver(_Graph(graph)), V, {"a"})

    assert found == {"b": ("r2", 1), "c": ("r2", 2)}


@pytest.mark.parametrize("changed", [set(), {"to_git"}])
def test_the_walk_reports_each_dependent_once_with_its_shortest_depth(changed):
    found = find_dependent_modules(_Driver(_Graph(GRAPH)), V, changed)

    if not changed:
        assert found == {}
        return
    assert found == {
        "to_git_ui": ("tvtmaaddons", 1),
        "to_server_ssh_git": ("saas-infrastructure", 1),
        "to_odoo_saas": ("saas-infrastructure", 2),
        "viin_saas_portal": ("erponline-enterprise", 2),
    }


def test_an_incremental_run_propagates_and_names_it_in_the_warning_log(tmp_path, caplog):
    """The wiring in ``_index_repo``: after an incremental write the dependents
    of the written modules are propagated to (the repo itself excluded), and
    one WARNING line - visible in the WARNING-level nightly log - names it."""
    import contextlib
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    from src.indexer.pipeline import _index_repo
    from tests.test_pipeline_refresh import (
        _add_module,
        _head,
        _install_inmemory_parsers,
        _make_repo,
    )

    repo = _make_repo(tmp_path / "upstream_repo", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    _add_module(repo, "mod_new")
    repo_row = {
        "id": 7, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://upstream", "branch": "17.0", "ssh_key_id": None,
    }
    fake_store = MagicMock()
    fake_store.get_repo_head_sha.return_value = sha_a
    fake_store.get_repo_ids_by_local_path_basenames.return_value = [99]
    fake_store.reset_head_sha.return_value = 1
    dependents = {
        "dep_mod": ("downstream_repo", 1),
        "own_dep": ("upstream_repo", 1),
    }

    with contextlib.ExitStack() as stack, caplog.at_level(logging.WARNING):
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        walk = stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_modules", return_value=dependents,
        ))
        _install_inmemory_parsers(stack, {})
        _index_repo(repo_row, MagicMock(), pg_conn=object(), refresh=False)

    assert walk.call_args.args[1:] == ("17.0", {"mod_new"})
    # No ledger in this run: the downstream repo of the direct dependent is
    # reset; the repo that was just indexed is not a candidate.
    fake_store.get_repo_ids_by_local_path_basenames.assert_called_once_with(
        ["downstream_repo"], "17.0",
    )
    fake_store.reset_head_sha.assert_called_once_with([99])
    line = next(r for r in caplog.records if "cross-repo dep propagation" in r.getMessage())
    assert line.levelno == logging.WARNING
    assert "mod_new" in line.getMessage() and "head_sha reset on 1 repo(s)" in line.getMessage()


# ---------------------------------------------------------------------------
# Robustness: a failed walk, a failed fallback, a stop (finding 2)
# ---------------------------------------------------------------------------


class _WalkFailsDriver:
    """The ``DEPENDS_ON`` closure walk raises (a Neo4j error); the one-hop
    ``find_dependent_repos`` query answers *direct_repos*, or raises too."""

    def __init__(self, direct_repos: list[str] | None) -> None:
        self.direct_repos = direct_repos

    def session(self):
        direct_repos = self.direct_repos

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def data(self):
                return self._rows

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, query, **_params):
                if query == cross_repo._CYPHER_DIRECT_DEPENDENT_MODULES:
                    raise RuntimeError("Neo4j TransientError: deadlock")
                assert query == cross_repo._CYPHER_FIND_DEPENDENT_REPOS
                if direct_repos is None:
                    raise RuntimeError("Neo4j ServiceUnavailable")
                return _Result([{"repo": r} for r in direct_repos])

        return _Session()


def test_a_failed_dependency_walk_resets_the_direct_dependents_repos(caplog):
    ledger = _Ledger(LEDGER_ROWS)
    repos = _Repos(REPOS)

    with caplog.at_level(logging.WARNING, logger="src.indexer.cross_repo"):
        result = propagate_to_dependents(
            _WalkFailsDriver(["saas-infrastructure", "tvtmaaddons"]), V, {"to_git"},
            exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
            presence=ledger, repo_store=repos,
        )

    assert result.walk_failed is True
    assert repos.reset == [42], "the indexed repo itself is never reset"
    assert result.reset_repo_ids == [42]
    assert ledger.flagged == set()
    # The transitive dependents are unknown: the caller must repeat the walk.
    assert result.complete is False
    assert "dependency walk failed" in caplog.text


def test_a_failed_walk_and_fallback_report_an_incomplete_propagation():
    repos = _Repos(REPOS)

    result = propagate_to_dependents(
        _WalkFailsDriver(None), V, {"to_git"},
        exclude_repo_id=31, exclude_basenames={"tvtmaaddons"},
        presence=_Ledger(LEDGER_ROWS), repo_store=repos,
    )

    assert result.complete is False
    assert repos.reset == []


def _run_index_repo(
    tmp_path, *, walk=None, bootstrap=(), meta_only=False, stack_extra=None, store=None,
    attention=None, stored_head="a", full_reindex=False, ledger_store=False,
    presence_head=None,
):
    """``_index_repo`` on a real two-commit git repo (in-memory parsers, no DB).

    Commit A ships ``mod_old``; commit B adds ``mod_new`` (or, with
    *meta_only*, only touches a top-level file). ``repo_store`` reports
    head_sha A, so the run is incremental A..B. *walk* replaces
    ``cross_repo.find_dependent_modules`` (a callable side effect or a return
    value); *bootstrap* are the names the shared-module bootstrap re-parses.
    *store* (a MagicMock) stands for ``repo_store()``; pass one to inspect
    it after a raising run. *attention* is the repo row's
    ``lifecycle_attention`` from the previous run. *stored_head*: the
    ``repos.head_sha`` the store reports, ``"a"`` (commit A), ``"b"`` (HEAD:
    nothing moved) or None (reset). *ledger_store*: ``_presence_store``
    returns a fake ledger whose ``presence_head_sha`` is *presence_head*
    (same labels) while this repo's lifecycle is off (no ledger commit), so
    the attention is stored and read back like with a ledger. Returns
    ``(fake_store, walk_mock, parsed_module_names, counters)``.
    """
    import contextlib
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    import src.indexer.pipeline_repo as _pr
    from src.indexer.pipeline import _index_repo
    from tests.test_pipeline_refresh import (
        _add_module,
        _git,
        _head,
        _install_inmemory_parsers,
        _make_repo,
    )

    repo = _make_repo(tmp_path / "upstream_repo", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    if meta_only:
        (repo / "README.md").write_text("docs only\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "docs")
    else:
        _add_module(repo, "mod_new")
    repo_row = {
        "id": 7, "local_path": str(repo), "odoo_version": "17.0",
        "url": "file://upstream", "branch": "17.0", "ssh_key_id": None,
        "lifecycle_attention": attention,
    }
    fake_store = store if store is not None else MagicMock()
    fake_store.get_repo_head_sha.return_value = {
        "a": sha_a, "b": _head(repo), None: None,
    }[stored_head]
    fake_store.get_repo_ids_by_local_path_basenames.return_value = [99]
    fake_store.reset_head_sha.return_value = 1
    parsed: list[str] = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(_pipeline, "repo_store", return_value=fake_store))
        walk_kw = (
            {"side_effect": walk} if callable(walk) or isinstance(walk, BaseException)
            else {"return_value": walk if walk is not None else {}}
        )
        walk_mock = stack.enter_context(
            patch("src.indexer.cross_repo.find_dependent_modules", **walk_kw)
        )
        _install_inmemory_parsers(stack, {})
        real_parse = _pr.parser_python.parse_module

        def record_parse(info, **kw):
            parsed.append(info.name)
            return real_parse(info, **kw)

        stack.enter_context(
            patch.object(_pr.parser_python, "parse_module", side_effect=record_parse)
        )
        stack.enter_context(
            patch.object(_pr, "shared_parse_bootstrap", return_value=list(bootstrap))
        )
        if ledger_store:
            _install_ledger_store_without_lifecycle(
                stack, {"a": sha_a, "b": _head(repo), None: None}[presence_head],
            )
        if stack_extra is not None:
            stack_extra(stack)
        counters = _index_repo(
            repo_row, MagicMock(), pg_conn=object(), refresh=False,
            full_reindex=full_reindex,
        )
    return fake_store, walk_mock, parsed, counters


def test_propagation_runs_before_head_sha_advances_so_a_stop_loses_nothing(tmp_path):
    """A stop (SIGTERM) while the propagation runs must leave head_sha where it
    was: the next run repeats the diff and the propagation. With head_sha
    advanced first, the dependents were never flagged."""
    from unittest.mock import MagicMock

    from src.indexer.stop import IndexRunStopped

    store = MagicMock()
    with pytest.raises(IndexRunStopped):
        _run_index_repo(
            tmp_path, walk=IndexRunStopped("index run stop requested"), store=store,
        )

    store.update_repo_head_sha.assert_not_called()


def test_head_sha_is_still_old_while_the_dependents_are_flagged(tmp_path):
    from unittest.mock import MagicMock

    store = MagicMock()
    seen_head_updates: list[int] = []

    def walk(_driver, _version, _seed):
        seen_head_updates.append(store.update_repo_head_sha.call_count)
        return {"dep_mod": ("downstream_repo", 1)}

    fake_store, _walk, _parsed, _c = _run_index_repo(tmp_path, walk=walk, store=store)

    assert seen_head_updates == [0], "head_sha advanced before the propagation ran"
    fake_store.update_repo_head_sha.assert_called_once()
    fake_store.reset_head_sha.assert_called_once_with([99])


def test_a_walk_error_resets_the_direct_dependents_and_keeps_head_sha(
    tmp_path, caplog,
):
    """The direct dependents' repos are reset at once, but the walk is what
    finds the transitive ones: head_sha is kept so the next run repeats it,
    and the repo is not reported failed."""
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos", return_value=["downstream_repo"],
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.reset_head_sha.assert_called_once_with([99])
    fake_store.update_repo_head_sha.assert_not_called()
    assert "dependency walk failed" in caplog.text
    assert counters["propagation"]["unrecorded"]


def test_a_propagation_that_cannot_be_recorded_keeps_head_sha_and_does_not_fail(
    tmp_path, caplog,
):
    """Walk and fallback both fail (Neo4j down after the writes), or a reset
    write raises: the repo is not reported failed, and head_sha stays so the
    next run repeats it."""
    def stack_extra(stack):
        from unittest.mock import patch
        stack.enter_context(patch(
            "src.indexer.cross_repo.find_dependent_repos",
            side_effect=RuntimeError("Neo4j ServiceUnavailable"),
        ))

    with caplog.at_level(logging.WARNING):
        fake_store, _walk, _parsed, _c = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"), stack_extra=stack_extra,
        )

    fake_store.update_repo_head_sha.assert_not_called()
    assert "head_sha is not advanced" in caplog.text


def test_a_failed_reset_write_keeps_head_sha_and_does_not_fail(tmp_path, caplog):
    from unittest.mock import MagicMock

    store = MagicMock()
    store.reset_head_sha.side_effect = RuntimeError("PostgreSQL connection reset")
    with caplog.at_level(logging.WARNING):
        _store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, store=store,
        )

    store.update_repo_head_sha.assert_not_called()
    assert "could not be recorded" in caplog.text
    assert counters["propagation"]["unrecorded"]


# ---------------------------------------------------------------------------
# The seed: only modules whose graph this run can have changed (finding 3)
# ---------------------------------------------------------------------------


def test_a_module_re_written_only_for_a_rewrite_reason_does_not_propagate(tmp_path):
    """mod_old is re-parsed for the shared-module bootstrap (its source did not
    change), mod_new is new in git: only mod_new seeds the propagation."""
    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, bootstrap=["mod_old"],
    )

    assert "mod_old" in parsed, "precondition: mod_old was re-written"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_new"}


def test_a_run_that_only_re_writes_unchanged_modules_propagates_nothing(tmp_path):
    """The ping-pong guard: a repo re-writing its dependents (flagged by another
    repo) or retrying their embeddings must not flag anything back."""
    store, walk, parsed, _c = _run_index_repo(tmp_path, bootstrap=["mod_old"], meta_only=True)

    assert parsed == ["mod_old"], "precondition: mod_old was re-written"
    walk.assert_not_called()
    store.reset_head_sha.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


@pytest.mark.parametrize(
    ("reason", "propagates"),
    [("no_node", True), ("path_drift", True), ("repo_drift", False)],
)
def test_self_heal_seeds_only_when_the_graph_can_change(reason, propagates):
    from src.indexer.pipeline_repo import propagation_seed

    seed = propagation_seed(
        set(), heal={"healed": {"reason": reason}}, degraded_changed=set(),
        gap_changed=set(), written={"healed"},
    )

    assert (seed == {"healed"}) is propagates


def test_the_seed_keeps_changed_sources_and_drops_what_was_not_written():
    from src.indexer.pipeline_repo import propagation_seed

    seed = propagation_seed(
        {"git_changed", "not_written"}, heal={}, degraded_changed={"files_fixed"},
        gap_changed={"gap_reevaluated"},
        written={"git_changed", "files_fixed", "gap_reevaluated", "rewrite_only"},
    )

    assert seed == {"git_changed", "files_fixed", "gap_reevaluated"}


# ---------------------------------------------------------------------------
# Every run mode propagates what it changed (round 2, item 2)
# ---------------------------------------------------------------------------


def test_the_sync_path_propagates_its_one_shot_seeds(tmp_path):
    """HEAD did not move, but a degraded module's failing files changed on
    disk: the sync path re-parses it, and its dependents must hear of it (the
    sync path used to never propagate, so the one-shot seed was lost)."""
    def stack_extra(stack):
        from unittest.mock import patch

        import src.indexer.pipeline_repo as _pr
        stack.enter_context(patch.object(_pr, "_degraded_state", return_value=({}, {"mod_old"})))

    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head="b",
        stack_extra=stack_extra,
    )

    assert parsed == ["mod_old"], "precondition: the sync path re-wrote only mod_old"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_old"}


def test_a_full_run_propagates_the_modules_changed_since_its_baseline(tmp_path):
    """``--full`` re-writes every module; only mod_new changed since the
    stored head_sha, so only mod_new seeds (full runs used to never propagate)."""
    _store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, full_reindex=True,
    )

    assert sorted(parsed) == ["mod_new", "mod_old"], "precondition: a full run"
    walk.assert_called_once()
    assert walk.call_args.args[2] == {"mod_new"}


def test_a_full_run_without_baseline_or_ledger_does_not_propagate(tmp_path):
    """head_sha reset and no ledger: a first index cannot be told from a reset,
    and seeding every module after each reset would bounce full re-indexes
    between ledger-less repos that depend on each other."""
    store, walk, parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head=None,
    )

    assert sorted(parsed) == ["mod_new", "mod_old"]
    walk.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


@pytest.mark.parametrize(
    ("mode", "since", "ledger", "expected"),
    [
        ("incremental", None, True, {"changed"}),
        ("sync", None, True, set()),
        ("full", {"moved"}, True, {"moved"}),
        ("full", set(), True, set()),
        ("full", None, True, {"changed"}),
        ("full", None, False, set()),
    ],
    ids=["incremental", "sync", "full-diff", "full-no-change", "full-first-index",
         "full-no-baseline-no-ledger"],
)
def test_source_changed_names_per_run_mode(mode, since, ledger, expected):
    from src.indexer import pipeline_repo as pr

    mode_value = {"incremental": pr.RUN_INCREMENTAL, "sync": pr.RUN_SYNC, "full": pr.RUN_FULL}
    got = pr.source_changed_names(mode_value[mode], {"changed"}, ledger=ledger, since=since)

    assert got == expected


def test_a_full_run_baseline_falls_back_to_the_ledger_head_and_survives_a_force_push(
    tmp_path,
):
    """head_sha NULL (reset) leaves the ledger's presence head as the baseline;
    a force-pushed old head still in the object store is diffed too; a head
    the checkout does not have is skipped."""
    from src.indexer.pipeline_repo import full_run_changed_names
    from src.indexer.registry import build_registry_scan
    from tests.test_pipeline_refresh import _add_module, _git, _head, _make_repo

    repo = _make_repo(tmp_path / "addons", branch="17.0")
    _add_module(repo, "mod_old")
    sha_a = _head(repo)
    _add_module(repo, "mod_gone")
    sha_b = _head(repo)
    _git(repo, "reset", "--hard", sha_a)  # force-push: B is no longer an ancestor
    _add_module(repo, "mod_new")
    head = _head(repo)
    scan = build_registry_scan(str(repo), "17.0", branch="17.0")

    assert full_run_changed_names(repo, scan, head, [None, sha_a]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, [sha_b, None]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, ["0" * 40, sha_a]) == {"mod_new"}
    assert full_run_changed_names(repo, scan, head, [head, sha_a]) == set()
    assert full_run_changed_names(repo, scan, head, [None, "0" * 40]) is None


# ---------------------------------------------------------------------------
# An unrecorded propagation is visible (round 2, item 4)
# ---------------------------------------------------------------------------


def _walk_and_fallback_fail(stack):
    from unittest.mock import patch
    stack.enter_context(patch(
        "src.indexer.cross_repo.find_dependent_repos",
        side_effect=RuntimeError("Neo4j ServiceUnavailable"),
    ))


def test_an_unrecorded_propagation_is_named_with_the_kept_head(tmp_path, caplog):
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    with caplog.at_level(logging.WARNING):
        store, _walk, _parsed, counters = _run_index_repo(
            tmp_path, walk=RuntimeError("Neo4j TransientError"),
            stack_extra=_walk_and_fallback_fail,
        )

    stuck = store.get_repo_head_sha.return_value[:12]
    line = counters["propagation"]["unrecorded"]
    assert PROPAGATION_UNRECORDED_MARK in line and stuck in line and "mod_new" in line
    assert counters["propagation"]["persistent"] is False
    # Without a ledger the attention lines are logged (WARNING) instead of stored.
    assert any(PROPAGATION_UNRECORDED_MARK in r.getMessage() for r in caplog.records)


def test_the_second_unrecorded_run_in_a_row_is_persistent(tmp_path):
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK

    _store, _walk, _parsed, counters = _run_index_repo(
        tmp_path, walk=RuntimeError("Neo4j TransientError"),
        stack_extra=_walk_and_fallback_fail,
        attention=f"{PROPAGATION_UNRECORDED_MARK} for 1 module(s) (mod_new) at HEAD x",
    )

    assert counters["propagation"]["persistent"] is True
    assert "two consecutive runs" in counters["propagation"]["unrecorded"]


def test_once_is_a_warning_twice_in_a_row_is_an_alert(capsys):
    """One unrecorded propagation retries by itself (head_sha kept): printed,
    exit 0. The same repo again in the next run is exit 3 (OnFailure= mail)."""
    from src.indexer import pipeline as pipeline_mod
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    def summary(persistent: bool) -> dict:
        lc = pipeline_mod._empty_lifecycle()
        pipeline_mod._absorb_repo_lifecycle(lc, {"id": 7, "url": "file://up"}, {
            "propagation": {"unrecorded": "cross-repo propagation not recorded ...",
                            "persistent": persistent},
        })
        return pipeline_mod._finish_lifecycle(lc)

    once = summary(False)
    assert once["propagation_unrecorded"] and once["stale_propagation"] == []
    assert once["needs_attention"] is False
    assert _lifecycle_exit_code(once) == 0
    assert "Cross-repo propagation not recorded" in capsys.readouterr().err

    twice = summary(True)
    assert twice["needs_attention"] is True
    assert _lifecycle_exit_code(twice) == EXIT_LIFECYCLE_ATTENTION
    err = capsys.readouterr().err
    assert "stale_propagation: repo id=7" in err
    # Printed once, and never as "retried by the next run": it was not.
    assert err.count("repo id=7 (file://up)") == 1
    assert "retried by the next run" not in err


# ---------------------------------------------------------------------------
# An unrecorded propagation is retried in every run mode (round 3, MAJOR 1)
# ---------------------------------------------------------------------------


def _with_stored_attention(stack):
    """The harness has no ledger store; the retry mark is read back from the
    repo's ``lifecycle_attention`` as with one."""
    from unittest.mock import patch

    import src.indexer.pipeline_repo as _pr
    real = _pr.propagation_retry
    stack.enter_context(patch.object(
        _pr, "propagation_retry", side_effect=lambda repo, ledger: real(repo, ledger=True),
    ))


def _stacked(*extras):
    def apply(stack):
        for extra in extras:
            extra(stack)
    return apply


def _degraded(name):
    def apply(stack):
        from unittest.mock import patch

        import src.indexer.pipeline_repo as _pr
        stack.enter_context(patch.object(_pr, "_degraded_state", return_value=({}, {name})))
    return apply


def _two_runs(tmp_path, *, first: dict, second: dict, second_fails: bool):
    """Run 1 fails to record its propagation; run 2 starts from run 1's
    attention. Returns run 2's ``(store, walk, parsed, counters)``."""
    _s1, _w1, _p1, c1 = _run_index_repo(
        tmp_path / "run1", walk=RuntimeError("Neo4j TransientError"),
        stack_extra=_stacked(_walk_and_fallback_fail, *first.pop("extra", ())), **first,
    )
    line = c1["propagation"]["unrecorded"]
    assert line and c1["propagation"]["persistent"] is False
    walk2 = RuntimeError("Neo4j TransientError") if second_fails else {
        "dep_mod": ("downstream_repo", 1),
    }
    extras = [_with_stored_attention, *second.pop("extra", ())]
    if second_fails:
        extras.append(_walk_and_fallback_fail)
    return _run_index_repo(
        tmp_path / "run2", walk=walk2, attention=line, stack_extra=_stacked(*extras),
        **second,
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Full run after a head_sha reset, diffed against the ledger's presence
        # head: in run 2 that baseline holds nothing (presence at HEAD).
        ({"stored_head": None, "ledger_store": True, "presence_head": "a"},
         {"stored_head": None, "ledger_store": True, "presence_head": "b"}),
        # Sync path at an unchanged HEAD: the degraded one-shot seed is gone in
        # run 2, which would skip.
        ({"stored_head": "b", "extra": (_degraded("mod_old"),)}, {"stored_head": "b"}),
        # Incremental run whose only seed was one-shot (a degraded module; the
        # diff touched no module): run 2 repeats the diff but not the seed.
        ({"meta_only": True, "extra": (_degraded("mod_old"),)}, {"meta_only": True}),
    ],
    ids=["full", "sync", "incremental-one-shot"],
)
def test_the_next_run_retries_an_unrecorded_propagation(tmp_path, first, second):
    _store, walk, _parsed, counters = _two_runs(
        tmp_path, first=dict(first), second=dict(second), second_fails=False,
    )

    walk.assert_called_once()
    assert "mod_old" in walk.call_args.args[2], "the retry re-seeds every present module"
    assert counters["propagation"] == {}, "recorded this time: no attention line"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"stored_head": None, "ledger_store": True, "presence_head": "a"},
         {"stored_head": None, "ledger_store": True, "presence_head": "b"}),
        ({"stored_head": "b", "extra": (_degraded("mod_old"),)}, {"stored_head": "b"}),
        ({"meta_only": True, "extra": (_degraded("mod_old"),)}, {"meta_only": True}),
    ],
    ids=["full", "sync", "incremental-one-shot"],
)
def test_a_second_unrecorded_run_is_exit_3_in_every_mode(tmp_path, first, second):
    from src.indexer import pipeline as pipeline_mod
    from src.indexer.__main__ import EXIT_LIFECYCLE_ATTENTION, _lifecycle_exit_code

    store, _walk, _parsed, counters = _two_runs(
        tmp_path, first=dict(first), second=dict(second), second_fails=True,
    )

    assert counters["propagation"]["persistent"] is True
    store.update_repo_head_sha.assert_not_called()
    lc = pipeline_mod._empty_lifecycle()
    pipeline_mod._absorb_repo_lifecycle(lc, {"id": 7, "url": "file://upstream"}, counters)
    assert _lifecycle_exit_code(pipeline_mod._finish_lifecycle(lc)) == EXIT_LIFECYCLE_ATTENTION


def test_a_retry_mark_forces_a_run_at_an_unchanged_head():
    from pathlib import Path

    from src.indexer import pipeline_repo as pr

    skip = pr.plan_repo_run(Path("/x"), "h", "h", "h", [], full_reindex=False, ledger=True)
    retry = pr.plan_repo_run(
        Path("/x"), "h", "h", "h", [], full_reindex=False, ledger=True,
        retry_propagation=True,
    )

    assert skip.mode == pr.RUN_SKIP
    assert retry.mode == pr.RUN_SYNC


def test_the_retry_mark_needs_a_ledger_and_the_previous_runs_line():
    from src.indexer.pipeline_repo import PROPAGATION_UNRECORDED_MARK, propagation_retry

    marked = {"lifecycle_attention": f"x; {PROPAGATION_UNRECORDED_MARK} for 2 module(s)"}
    assert propagation_retry(marked, ledger=True) is True
    assert propagation_retry(marked, ledger=False) is False
    assert propagation_retry({"lifecycle_attention": "pre-scan fetch failed"}, ledger=True) is False
    assert propagation_retry({}, ledger=True) is False


def _commit(tmp_path, *, hold_presence: bool, pending: bool = False):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.indexer.pipeline_repo import _commit_lifecycle
    from src.indexer.registry import build_registry_scan
    from tests.test_pipeline_refresh import _add_module, _head, _make_repo

    repo = _make_repo(tmp_path / "addons", branch="17.0")
    _add_module(repo, "mod_a")
    head = _head(repo)
    scan = build_registry_scan(str(repo), "17.0", branch="17.0")
    presence = MagicMock()
    presence.present_owner_basenames.return_value = {}
    presence.mark_retire_pending.return_value = ["gone_mod"] if pending else []
    writer = MagicMock()
    writer.stamp_module_presence.return_value = len(scan.present_names())
    gates = SimpleNamespace(kept_names=set(), retire_allowed=True, tripped=[])
    transitions = SimpleNamespace(
        retired=(SimpleNamespace(name="gone_mod", path="gone_mod"),) if pending else (),
    )
    counters = _commit_lifecycle(
        presence, writer, {"id": 7, "local_path": str(repo), "url": "file://addons"},
        scan=scan, rows=[], transitions=transitions, gates=gates, current_head=head,
        diff_base=None, owning_profile="p", retire=True, written=set(), rewrite_names=[],
        attention=[], hold_presence=hold_presence,
    )
    return presence, counters


def test_an_unrecorded_propagation_holds_the_ledger_presence_head(tmp_path):
    """The presence head is the "index reflects HEAD" claim the next run's skip
    and a full run's baseline read: it must not advance (nor be handed to the
    reconcile) while other repos' dependents were not flagged."""
    presence, counters = _commit(tmp_path, hold_presence=True)

    presence.mark_presence_synced.assert_not_called()
    assert counters["presence_synced"] is False
    assert counters["presence_deferred_head"] is None
    presence.commit_observed.assert_called_once()  # the rows themselves are committed


def test_a_recorded_propagation_still_syncs_the_ledger(tmp_path):
    presence, counters = _commit(tmp_path, hold_presence=False)

    presence.mark_presence_synced.assert_called_once()
    assert counters["presence_synced"] is True


def test_a_repo_whose_lifecycle_is_off_never_seeds_every_module_on_a_reset(tmp_path):
    """MINOR 1: a ledger store exists but this repo's lifecycle is off (no git
    HEAD / branch), so it never gets a presence head: a reset must not seed
    every module, or two such repos depending on each other bounce."""
    store, walk, _parsed, _c = _run_index_repo(
        tmp_path, walk={"dep_mod": ("downstream_repo", 1)}, stored_head=None,
        ledger_store=True,
    )

    walk.assert_not_called()
    store.update_repo_head_sha.assert_called_once()


def _install_ledger_store_without_lifecycle(stack, presence_head):
    """``_presence_store`` returns a store, but observe_lifecycle reports the
    lifecycle off for this repo (as for a checkout without a branch)."""
    from dataclasses import replace
    from unittest.mock import MagicMock, patch

    import src.indexer.pipeline as _pipeline
    import src.indexer.pipeline_repo as _pr

    fake = MagicMock()
    fake.presence_head_sha.return_value = presence_head
    fake.needs_rewrite_names.return_value = []
    fake.mark_dependents_needs_rewrite.return_value = []
    fake.repo_ids_without_rows.return_value = []
    stack.enter_context(patch.object(_pipeline, "_presence_store", return_value=fake))
    real = _pr.observe_lifecycle

    def off(repo, scan, presence, current_head, **kw):
        return replace(real(repo, scan, None, current_head, **kw), lifecycle_on=False)

    stack.enter_context(patch.object(_pr, "observe_lifecycle", side_effect=off))
