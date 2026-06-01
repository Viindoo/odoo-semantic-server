# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify Qwen3Embedder default model matches config + README."""


def test_default_model_is_qwen3_q5km():
    from src.indexer.embedder import Qwen3Embedder
    # Construct without model arg to verify the default
    inst = Qwen3Embedder()
    assert inst._model == "qwen3-embedding-q5km"


def test_latest_tag_is_stripped_from_model_name():
    """An optional Ollama ':latest' tag must be normalized away at the
    _BaseHttpEmbedder choke-point so the dim/model guard compares bare names
    and does not falsely trip on a later reindex (fix/ready-public-probe)."""
    from src.indexer.embedder import Qwen3Embedder
    inst = Qwen3Embedder(model="qwen3-embedding-q5km:latest")
    # Both the guard/writer-read attr (.model) and the internal _model must be bare.
    assert inst.model == "qwen3-embedding-q5km"
    assert inst._model == "qwen3-embedding-q5km"


def test_bare_model_name_is_unchanged():
    """A model name without a ':latest' suffix passes through untouched."""
    from src.indexer.embedder import Qwen3Embedder
    inst = Qwen3Embedder(model="qwen3-embedding-q5km")
    assert inst.model == "qwen3-embedding-q5km"
    assert inst._model == "qwen3-embedding-q5km"
