# SPDX-License-Identifier: AGPL-3.0-or-later
"""#248 Phase C — set_active_version / set_active_profile emit HONEST receipts.

Before the fix, both tools returned a cheerful "Active … set" receipt even when
``set_active_*_db`` silently skipped the write (non-numeric api_key_id) — so an
HTTP client that lost api_key_id propagation was told the pin succeeded while it
had not. This guards the new contract:

  - persist succeeded            → success receipt (teaches "pass odoo_version='auto'")
  - persist skipped + HTTP key   → LOUD error receipt (do not lie)
  - persist skipped + no HTTP key→ gentle stdio/CLI note (legit no-op)

The neo4j "version indexed" check and the DB write are mocked, so this is a fast
unit test of the receipt branch logic (no DB).

NOTE: the server module is resolved LIVE from ``sys.modules`` inside each call and
patched via ``patch.object`` on that exact object. Other tests re-import
``src.mcp.server`` (``_import_server_module`` does ``sys.modules.pop`` + re-import),
so a top-level ``import`` binding would point at a STALE module while a string
``patch("src.mcp.server.…")`` targets the live one — the patch would silently miss
and the tool would hit the real Neo4j. Resolving the live module keeps them aligned.
"""
import asyncio
from unittest.mock import MagicMock, patch


def _live_server():
    """Return the server module object currently in sys.modules (post any reload)."""
    import importlib
    import sys
    return sys.modules.get("src.mcp.server") or importlib.import_module("src.mcp.server")


def _call_set_active_version(*, persisted: bool, has_api_key: bool) -> str:
    srv = _live_server()
    # MagicMock driver → .session().__enter__().run().data() is truthy → version "indexed".
    with patch.object(srv, "_get_driver", return_value=MagicMock()), \
         patch("src.mcp.session.set_active_version_db", return_value=persisted), \
         patch.object(srv, "_http_request_has_api_key", return_value=has_api_key):
        result = asyncio.run(srv.set_active_version(odoo_version="17.0"))
    return result.content[0].text


def _call_set_active_profile(*, persisted: bool, has_api_key: bool) -> str:
    srv = _live_server()
    # profile_name=None skips the existence/authz checks → straight to persist.
    with patch("src.mcp.session.set_active_profile_db", return_value=persisted), \
         patch.object(srv, "_http_request_has_api_key", return_value=has_api_key):
        result = asyncio.run(srv.set_active_profile(profile_name=None))
    return result.content[0].text


class TestVersionReceiptHonesty:
    def test_persisted_success_receipt(self):
        text = _call_set_active_version(persisted=True, has_api_key=True)
        assert "Active version set to '17.0'" in text
        # Post ADR-0029 amendment (odoo_version required) + #248 fix: the receipt
        # must teach the still-valid path (pass 'auto'), NOT the obsolete "omit"
        # contract the CEO flagged on odoo-mcp-client PR #38.
        assert "omit odoo_version" not in text, (
            "Receipt must not advertise omitting odoo_version — it is now a "
            "required parameter on the 19 version-bearing tools"
        )
        assert "auto" in text

    def test_skipped_on_http_is_loud_error_not_a_lie(self):
        text = _call_set_active_version(persisted=False, has_api_key=True)
        assert "could not persist" in text.lower()
        assert "Active version set" not in text  # must NOT claim success
        assert "explicit odoo_version" in text

    def test_skipped_on_stdio_is_gentle_note(self):
        text = _call_set_active_version(persisted=False, has_api_key=False)
        assert "not persisted" in text.lower()
        assert "stdio" in text.lower()
        assert "Active version set" not in text


class TestProfileReceiptHonesty:
    def test_persisted_success_receipt(self):
        text = _call_set_active_profile(persisted=True, has_api_key=True)
        assert "cleared" in text.lower()  # profile_name=None → clear receipt

    def test_skipped_on_http_is_loud_error_not_a_lie(self):
        text = _call_set_active_profile(persisted=False, has_api_key=True)
        assert "could not persist" in text.lower()
        assert "cleared" not in text.lower()
        assert "explicit profile_name" in text

    def test_skipped_on_stdio_is_gentle_note(self):
        text = _call_set_active_profile(persisted=False, has_api_key=False)
        assert "not persisted" in text.lower()
        assert "stdio" in text.lower()
