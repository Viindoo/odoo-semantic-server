# SPDX-License-Identifier: AGPL-3.0-or-later
"""The owner-facing indexing error category matches the real cause (F34, #237).

A tenant member sees ``sanitize_job_error(raw)`` instead of the raw indexer
text. The output is always one fixed category string, never the raw text, and
the category must not be picked by an accidental substring: "boom" is not an
out-of-memory kill, "spool" is not the connection pool, "last." is not a parse
error, and a server path that happens to contain "ssh" or "git" is not a
repository access / git failure.

Raw texts below are the shapes the producers really write: ``str(e)`` of a
``subprocess.CalledProcessError`` from the cloner / git refresh, psycopg2 and
neo4j driver errors, ``OSError`` errno 28, the kernel OOM kill of a child
process, a ``SyntaxError`` from the Python parser.
"""
from __future__ import annotations

import pytest

from src.web_ui.routes.jobs import (
    _JOB_ERROR_CATEGORIES,
    _JOB_ERROR_DEFAULT,
    sanitize_job_error,
)

AUTH, GIT, TIMEOUT, BACKEND, RESOURCE, PARSE = (s for _p, s in _JOB_ERROR_CATEGORIES)
DEFAULT = _JOB_ERROR_DEFAULT


@pytest.mark.parametrize("raw", [
    "boom",
    "boom at /srv/osm/indexer/pipeline.py line 12",
    "room full",
    "zoom spool",
    "last. value",
    "/home/x/git/odoo/addons broke",
    "/srv/osm/sshfs-cache/addons: unexpected state",
    "secret-error /private/path",
])
def test_an_unrecognised_error_is_reported_as_the_generic_internal_error(raw):
    assert sanitize_job_error(raw) == DEFAULT


@pytest.mark.parametrize(("raw", "expected"), [
    ("fatal: could not read from remote repository.\n\nPlease make sure you have the "
     "correct access rights (permission denied)", AUTH),
    ("git@github.com: Permission denied (publickey).", AUTH),
    ("SSH URL but no ssh_key_id configured for repo id=7", AUTH),
    ("Command '['git', '-C', '/srv/osm/clones/p/odoo', 'fetch', 'origin']' returned "
     "non-zero exit status 128.", GIT),
    ("fatal: not a git repository (or any of the parent directories): .git", GIT),
    ("connection to server at \"127.0.0.1\", port 5432 failed: Connection refused",
     BACKEND),
    ("Couldn't connect to localhost:7687 (resolved to ('127.0.0.1:7687',))", BACKEND),
    ("psycopg2.pool.PoolError: connection pool exhausted", BACKEND),
    ("[Errno 28] No space left on device: '/var/lib/osm/tmp/chunk'", RESOURCE),
    ("Command '['node', 'build.js']' returned non-zero exit status -9.", RESOURCE),
    ("MemoryError", RESOURCE),
    ("invalid syntax (models.py, line 3)", PARSE),
    ("Failed to parse /srv/osm/clones/p/addons/sale_x/__manifest__.py", PARSE),
])
def test_a_real_error_is_reported_under_its_cause(raw, expected):
    assert sanitize_job_error(raw) == expected


@pytest.mark.parametrize("raw", [
    "boom at /srv/osm/secret/path.py",
    "fatal: could not read from remote repository git@internal.example:tenant-b/x.git",
    "[Errno 28] No space left on device: '/var/lib/osm/tmp/chunk'",
])
def test_the_raw_text_never_reaches_the_owner(raw):
    out = sanitize_job_error(raw)
    assert out in {s for _p, s in _JOB_ERROR_CATEGORIES} | {DEFAULT}
    assert raw not in out


@pytest.mark.parametrize("raw", [None, ""])
def test_no_error_means_no_summary(raw):  # GUARD: pre-existing behaviour
    assert sanitize_job_error(raw) is None
