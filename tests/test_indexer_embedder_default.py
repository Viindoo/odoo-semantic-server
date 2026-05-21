# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify Qwen3Embedder default model matches config + README."""


def test_default_model_is_qwen3_q5km():
    from src.indexer.embedder import Qwen3Embedder
    # Construct without model arg to verify the default
    inst = Qwen3Embedder()
    assert inst._model == "qwen3-embedding-q5km"
