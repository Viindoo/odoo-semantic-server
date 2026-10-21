# SPDX-License-Identifier: AGPL-3.0-or-later
"""apply-preset tells the operator what to fix when the profile is refused.

Business rule: RepoStore.add_profile refuses a preset for
different reasons - no owner, ``--tenant`` together with a preset
``shared: true``, a visibility violation, or a taken name. Only the last one
is fixed by "use a different name"; the others must point at the tenant /
shared choice. Every refusal stays fail-closed (exit 2, nothing written).
"""
from __future__ import annotations

import pytest

from src.db.exceptions import (
    ProfileInputError,
    ProfileTenantRequiredError,
    ProfileVisibilityError,
)
from src.manager import __main__ as manager


class _Store:
    def __init__(self, exc):
        self.exc = exc
        self.repos = []

    def add_profile(self, **_kw):
        raise self.exc

    def add_repo(self, **kw):  # pragma: no cover - must never be reached
        self.repos.append(kw)


@pytest.mark.parametrize(("exc", "hint", "not_hint"), [
    (ProfileTenantRequiredError("tenant_id is required"), "--tenant", "different name"),
    (ProfileInputError("pass either tenant_id or shared=true, not both"),
     "--tenant", "different name"),
    (ProfileVisibilityError("profile would be more visible than parent"),
     "--tenant", "different name"),
    (ValueError("Profile 'odoo_17' already exists"), "different name", "--tenant"),
])
def test_refusal_message_matches_the_cause(monkeypatch, capsys, exc, hint, not_hint):
    store = _Store(exc)
    monkeypatch.setattr(manager, "repo_store", lambda: store)
    rc = manager._cmd_apply_preset_write(
        None, profile_name="odoo_17", odoo_version="17.0", description="",
        resolved_repos=[{"url": "https://x/odoo.git", "branch": "17.0", "local_path": "/x"}],
        tenant_id=5, shared=True,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert hint in err and not_hint not in err, err
    assert store.repos == []
