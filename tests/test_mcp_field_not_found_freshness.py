# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for issue #341 (secondary): differentiated not-found messages.

When a field or method lookup misses, the response must distinguish:
  (A) model IS indexed at the requested version but the member is absent
      -> consumer should verify on-disk source before concluding it is absent.
  (B) model is NOT indexed at the requested version (unknown model/version)
      -> consumer can conclude there is no index entry to fall back to.

Both messages must still contain "not found" so existing loose snapshot tests
(e.g. test_mcp_resources_full.py) continue to pass.

The test seeds minimal Model nodes into a dedicated TEST_VERSION namespace
(cleaned before/after by the clean_neo4j fixture) and calls the resolver
implementations directly, matching the precedent in
test_resolve_view_unresolved_leak.py.
"""
import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

_MODEL_INDEXED = "test.partner.freshness"     # seeded below
_MODEL_UNKNOWN = "test.ghost.freshness"       # never seeded
_MODULE = "test_freshness_module"


@pytest.fixture(autouse=True)
def _seed_model(clean_neo4j):
    """Seed one non-stub Model node for _MODEL_INDEXED in TEST_VERSION."""
    driver = clean_neo4j
    with driver.session() as session:
        session.run(
            """
            CREATE (m:Model {
                name: $name,
                odoo_version: $ver,
                module: $module,
                unresolved: false,
                profile: []
            })
            """,
            name=_MODEL_INDEXED,
            ver=TEST_VERSION,
            module=_MODULE,
        )
    # clean_neo4j handles teardown; nothing to yield here.


# ---------------------------------------------------------------------------
# _resolve_field freshness note
# ---------------------------------------------------------------------------

class TestFieldNotFoundFreshness:
    """AC5 gate: _resolve_field not-found message carries correct freshness note."""

    def test_field_not_found_on_indexed_model_contains_not_found(self):
        """Message must still contain 'not found' (backward compat for snapshot tests)."""
        from src.mcp.server import _resolve_field

        out = _resolve_field(_MODEL_INDEXED, "no_such_field", TEST_VERSION)
        assert "not found" in out.lower(), (
            f"Expected 'not found' in output for indexed model; got: {out!r}"
        )

    def test_field_not_found_on_indexed_model_has_checkout_note(self):
        """Model IS indexed -> note should tell consumer to verify on-disk source."""
        from src.mcp.server import _resolve_field

        out = _resolve_field(_MODEL_INDEXED, "no_such_field", TEST_VERSION)
        assert "verify against the checkout" in out, (
            f"Expected checkout-verify note for indexed model; got: {out!r}"
        )

    def test_field_not_found_on_unknown_model_contains_not_found(self):
        """Message must still contain 'not found' even for an unindexed model."""
        from src.mcp.server import _resolve_field

        out = _resolve_field(_MODEL_UNKNOWN, "no_such_field", TEST_VERSION)
        assert "not found" in out.lower(), (
            f"Expected 'not found' in output for unknown model; got: {out!r}"
        )

    def test_field_not_found_on_unknown_model_has_not_indexed_note(self):
        """Model NOT indexed -> note should say 'not indexed at this version'."""
        from src.mcp.server import _resolve_field

        out = _resolve_field(_MODEL_UNKNOWN, "no_such_field", TEST_VERSION)
        assert "not indexed at this version" in out, (
            f"Expected not-indexed note for unknown model; got: {out!r}"
        )

    def test_indexed_and_unknown_notes_are_different(self):
        """The two branches must produce distinguishable messages (AC5 core)."""
        from src.mcp.server import _resolve_field

        indexed_out = _resolve_field(_MODEL_INDEXED, "no_such_field", TEST_VERSION)
        unknown_out = _resolve_field(_MODEL_UNKNOWN, "no_such_field", TEST_VERSION)
        assert indexed_out != unknown_out, (
            "Indexed-model and unknown-model not-found messages must differ."
        )


# ---------------------------------------------------------------------------
# _resolve_method freshness note
# ---------------------------------------------------------------------------

class TestMethodNotFoundFreshness:
    """AC5 gate: _resolve_method not-found message carries correct freshness note."""

    def test_method_not_found_on_indexed_model_contains_not_found(self):
        """Message must still contain 'not found' (backward compat)."""
        from src.mcp.server import _resolve_method

        out = _resolve_method(_MODEL_INDEXED, "no_such_method", TEST_VERSION)
        assert "not found" in out.lower(), (
            f"Expected 'not found' for indexed model method miss; got: {out!r}"
        )

    def test_method_not_found_on_indexed_model_has_checkout_note(self):
        """Model IS indexed -> note should tell consumer to verify on-disk source."""
        from src.mcp.server import _resolve_method

        out = _resolve_method(_MODEL_INDEXED, "no_such_method", TEST_VERSION)
        assert "verify against the checkout" in out, (
            f"Expected checkout-verify note for indexed model; got: {out!r}"
        )

    def test_method_not_found_on_unknown_model_contains_not_found(self):
        """Message must still contain 'not found' for unindexed model."""
        from src.mcp.server import _resolve_method

        out = _resolve_method(_MODEL_UNKNOWN, "no_such_method", TEST_VERSION)
        assert "not found" in out.lower(), (
            f"Expected 'not found' for unknown model method miss; got: {out!r}"
        )

    def test_method_not_found_on_unknown_model_has_not_indexed_note(self):
        """Model NOT indexed -> note should say 'not indexed at this version'."""
        from src.mcp.server import _resolve_method

        out = _resolve_method(_MODEL_UNKNOWN, "no_such_method", TEST_VERSION)
        assert "not indexed at this version" in out, (
            f"Expected not-indexed note for unknown model; got: {out!r}"
        )

    def test_indexed_and_unknown_method_notes_are_different(self):
        """The two branches must produce distinguishable messages (AC5 core)."""
        from src.mcp.server import _resolve_method

        indexed_out = _resolve_method(_MODEL_INDEXED, "no_such_method", TEST_VERSION)
        unknown_out = _resolve_method(_MODEL_UNKNOWN, "no_such_method", TEST_VERSION)
        assert indexed_out != unknown_out, (
            "Indexed-model and unknown-model not-found method messages must differ."
        )
