# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_rls_guc_unit.py
"""Pure unit tests for the _allowed_to_guc helper (no DB dependency).

This file intentionally has NO pytestmark = pytest.mark.postgres so the test
runs in the unit-test lane (make test, no Docker required) and is never skipped
by the postgres fixture guard.  It must ALWAYS pass unconditionally.

Business rule under test: _allowed_to_guc maps an allowed-profile list to the
GUC string consumed by the embeddings RLS policy USING clause:
  - None      → '*'     (admin sentinel: policy returns TRUE — unrestricted)
  - []        → ''      (deny-all: ANY({''}) = FALSE for any real profile_name)
  - ['a']     → 'a'     (single profile)
  - ['a','b'] → 'a,b'   (comma-separated, order preserved)
"""


def test_allowed_to_guc_mapping():
    """_allowed_to_guc maps allowed list to correct GUC string.

    Pure function, no I/O.  Covers all four semantic cases:
    - None  → '*'    (admin sentinel: unrestricted)
    - []    → ''     (deny-all: tenant with no profiles)
    - ['a'] → 'a'    (single profile)
    - ['a', 'b'] → 'a,b'  (comma-separated, order preserved)
    """
    from src.mcp.server import _allowed_to_guc

    # Admin sentinel: None → '*'
    assert _allowed_to_guc(None) == "*", (
        "_allowed_to_guc(None) must return '*' (admin sentinel — policy returns TRUE)."
    )

    # Deny-all: empty list → empty string
    assert _allowed_to_guc([]) == "", (
        "_allowed_to_guc([]) must return '' (deny-all: ANY({}) = FALSE for any row)."
    )

    # Single profile
    assert _allowed_to_guc(["acme_profile"]) == "acme_profile", (
        "_allowed_to_guc(['acme_profile']) must return 'acme_profile'."
    )

    # Two profiles: comma-separated
    result = _allowed_to_guc(["profile_a", "profile_b"])
    assert result == "profile_a,profile_b", (
        f"_allowed_to_guc(['profile_a', 'profile_b']) must return 'profile_a,profile_b', "
        f"got {result!r}."
    )
