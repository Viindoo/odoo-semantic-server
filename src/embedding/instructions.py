# SPDX-License-Identifier: AGPL-3.0-or-later
# Qwen3-Embedding uses asymmetric instruction: only queries get the prefix.
# Documents (indexed code) have NO prefix — raw content only.
# Reference: https://huggingface.co/Qwen/Qwen3-Embedding#usage
INSTRUCT_NL_TO_CODE = (
    "Instruct: Given a natural language description, retrieve the most relevant Odoo code snippet\n"
    "Query: "
)
