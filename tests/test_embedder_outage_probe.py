# SPDX-License-Identifier: AGPL-3.0-or-later
"""``embedder_down`` means the embedder is unreachable, not "0 of N vectors" (no DB).

Since unchanged chunks reuse their stored vector, a quiet night can send the
embedder nothing but the one chunk a HEALTHY embedder keeps refusing (a module
flagged ``needs_rewrite`` every night for its incomplete embeddings). The old
rule "chunks sent and none returned" then declared the embedder down: exit 3
and an alert mail every night.

Business rules protected here:

- A run that sent chunks and got no vector asks the embedder for one vector
  at the end of the run; when it answers, the run reports no
  ``embedder_down`` outage (the refused chunks are reported per module and
  retried, never as an outage).
- An embedder that does not answer that probe either is down: the outage
  and the exit 3 stand.
- A run that got any vector, or sent nothing, never probes.
- ``index_all`` probes once for the whole run, not once per profile.
- The reused chunks are counted in the run summary (``embeddings_reused``),
  so the nightly log shows what the reuse saved.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.constants import TIMEOUT_EMBEDDER_READ_QUERY
from src.indexer import pipeline as pipeline_mod
from src.indexer.embedder import Qwen3Embedder


class _Embedder:
    """Records every embed call; answers or raises as told."""

    def __init__(self, *, up: bool) -> None:
        self.up = up
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if not self.up:
            raise RuntimeError("Qwen3Embedder failed after 3 attempts: ConnectError")
        return [[1.0, 0.0] for _ in texts]


def _counters(*, sent: int, ok: int, reused: int) -> dict:
    return {
        "modules": 1, "views": 0, "qweb": 0, "embeddings": sent + reused,
        "js_patches": 0, "owl_comps": 0,
        "embed_attempted": sent, "embed_ok": ok, "embed_reused": reused,
        "fetch": {"attempted": False, "failed": None, "persistent": False},
    }


def _mock_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)  # advisory lock acquired
    conn.cursor.return_value = cur
    return conn


def _run_profile(embedder, counters_by_repo: list[dict]) -> dict:
    repos = [
        {"id": i, "local_path": f"/fake/repo{i}", "odoo_version": "99.0",
         "url": f"file://r{i}", "branch": "99.0"}
        for i in range(1, len(counters_by_repo) + 1)
    ]
    store = MagicMock()
    store.get_repos_for_profile.return_value = repos
    store.get_ancestor_profile_names.return_value = ["p"]
    by_id = {r["id"]: c for r, c in zip(repos, counters_by_repo, strict=True)}
    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch(
            "src.indexer.pipeline._index_repo",
            side_effect=lambda repo, w, **kw: by_id[repo["id"]],
        ),
        # The auto-reseed must not embed: only the run's own calls are counted.
        patch("src.indexer.seed_patterns.run", return_value={"skipped": True}),
    ):
        return pipeline_mod.index_profile(_mock_conn(), profile_name="p", embedder=embedder)


def _outage_ids(lc: dict) -> list[str]:
    return [o.split(":")[0] for o in lc["outages"]]


def test_a_quiet_night_with_one_refused_chunk_on_a_healthy_embedder_is_no_outage():
    """The observed shape: every unchanged chunk reused, one chunk sent, refused."""
    embedder = _Embedder(up=True)

    summary = _run_profile(embedder, [_counters(sent=1, ok=0, reused=57)])

    lc = summary["lifecycle"]
    assert _outage_ids(lc) == []
    assert lc["needs_attention"] is False
    assert lc["embedder_answered"] is True
    assert embedder.calls == [[pipeline_mod.EMBEDDER_PROBE_TEXT]], "one probe, nothing else"


def test_an_embedder_that_does_not_answer_the_probe_is_down():
    embedder = _Embedder(up=False)

    summary = _run_profile(embedder, [_counters(sent=40, ok=0, reused=0)])

    lc = summary["lifecycle"]
    assert _outage_ids(lc) == ["embedder_down"]
    assert lc["needs_attention"] is True
    assert len(embedder.calls) == 1, "the probe was tried once"


@pytest.mark.parametrize(
    "counters",
    [_counters(sent=10, ok=9, reused=3), _counters(sent=0, ok=0, reused=12)],
    ids=["some-vectors", "nothing-sent"],
)
def test_a_run_that_got_a_vector_or_sent_nothing_never_probes(counters):
    embedder = _Embedder(up=False)

    summary = _run_profile(embedder, [counters])

    assert embedder.calls == []
    assert _outage_ids(summary["lifecycle"]) == []


def test_the_run_summary_counts_the_reused_chunks():
    summary = _run_profile(
        _Embedder(up=True),
        [_counters(sent=2, ok=2, reused=40), _counters(sent=0, ok=0, reused=17)],
    )

    assert summary["embeddings_reused"] == 57
    assert summary["lifecycle"]["embed_reused"] == 57


def test_index_all_probes_once_for_the_merged_run():
    embedder = _Embedder(up=True)
    profiles = [{"name": f"p{i}", "odoo_version": "99.0"} for i in (1, 2)]
    store = MagicMock()
    store.list_profiles.return_value = profiles
    store.get_repos_for_profile.side_effect = lambda name: [{
        "id": int(name[1:]), "local_path": f"/fake/{name}", "odoo_version": "99.0",
        "url": f"file://{name}", "branch": "99.0",
    }]
    store.get_ancestor_profile_names.side_effect = lambda name: [name]
    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch(
            "src.indexer.pipeline._index_repo",
            side_effect=lambda repo, w, **kw: _counters(sent=1, ok=0, reused=30),
        ),
        patch("src.indexer.seed_patterns.run", return_value={"skipped": True}),
        patch("src.indexer.pipeline.run_lifecycle_reconcile"),
    ):
        summary = pipeline_mod.index_all(_mock_conn(), embedder=embedder)

    assert embedder.calls == [[pipeline_mod.EMBEDDER_PROBE_TEXT]]
    assert _outage_ids(summary["lifecycle"]) == []
    assert summary["embeddings_reused"] == 60


def test_the_probe_uses_the_real_embedders_short_query_client():
    """The real HTTP embedder is probed through its query client (short read
    timeout), and a 200 with a vector counts as an answer."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"embeddings": [[0.6, 0.8]]})

    embedder = Qwen3Embedder(
        url="http://embedder.test", model="m", dim=2, transport=httpx.MockTransport(handler),
    )
    lc = pipeline_mod._empty_lifecycle()
    lc["embed_attempted"] = 1
    try:
        pipeline_mod._probe_silent_embedder(lc, embedder)
    finally:
        embedder.close()

    assert lc["embedder_answered"] is True
    (request,) = seen
    # Not the 20-minute batch read timeout: a hung embedder must not hold the run.
    assert request.extensions["timeout"]["read"] == TIMEOUT_EMBEDDER_READ_QUERY
    assert pipeline_mod.run_outages(lc) == []
