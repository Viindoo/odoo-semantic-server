# SPDX-License-Identifier: AGPL-3.0-or-later
# src/ops/__init__.py
"""Host operations helpers run by systemd units (alert hook, #383).

Not part of the indexing pipeline: modules here may import the leaf layers
(src.config, src.mailer) but never src.web_ui or src.mcp.
"""
