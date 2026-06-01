# SPDX-License-Identifier: AGPL-3.0-or-later
# Qwen3-Embedding uses asymmetric instruction: only queries get the prefix.
# Documents (indexed code) have NO prefix — raw content only.
# Reference: https://huggingface.co/Qwen/Qwen3-Embedding#usage
#
# Backend-specific: this prefix is the *Qwen* query instruction and is wired in
# as ``Qwen3Embedder.query_instruction``. OpenAI-compatible backends
# (OpenAICompatEmbedder) use NO instruction prefix (query_instruction="").
INSTRUCT_NL_TO_CODE = (
    "Instruct: Given a natural language description, retrieve the most relevant Odoo code snippet\n"
    "Query: "
)
