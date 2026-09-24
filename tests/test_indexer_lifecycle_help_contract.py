# SPDX-License-Identifier: AGPL-3.0-or-later
"""The indexer's --help tells operators what the lifecycle flags do NOW (G3, ADR-0056).

Operators decide from ``--help`` whether to schedule ``--full``, whether
``--no-embed`` leaves stale rows, what ``--no-retire`` suspends and what the
weekly ``lifecycle-audit --fail-on-findings`` timer alerts on. Before G3 the
texts described the pre-ADR-0056 world ("use --full periodically to clean up
stale Module nodes"; a hand-written finding list that missed ``held_prunes``
successors such as ``shared_prunes``). Assertions are derived from the contract
sources - ``lifecycle_audit.FINDING_KEYS`` and the exit-code constants - and
from the semantic claims, not from frozen prose.
"""
from __future__ import annotations

import re

import pytest

from src.indexer.__main__ import (
    EXIT_AUDIT_FINDINGS,
    EXIT_LIFECYCLE_ATTENTION,
    _build_parser,
)
from src.indexer.lifecycle_audit import FINDING_KEYS


def _help(subcommand: str, option: str) -> str:
    parser = _build_parser()
    sub = next(
        a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
    ).choices[subcommand]
    action = next(a for a in sub._actions if option in a.option_strings)
    return " ".join((action.help or "").split())


def test_fail_on_findings_help_names_every_finding_the_report_counts():
    """The weekly detector's help lists exactly the report's finding keys (one
    source: FINDING_KEYS) and its exit code, and says the bootstrap backlog is
    reported but not a finding."""
    text = _help("lifecycle-audit", "--fail-on-findings")

    missing = [k for k in FINDING_KEYS if k not in text]
    assert missing == [], f"--fail-on-findings help omits findings: {missing}"
    assert f"Exit {EXIT_AUDIT_FINDINGS}" in text
    assert "backlog" in text and "not a finding" in text


def test_full_help_does_not_sell_full_reindex_as_the_cleanup_step():
    """Retirement, the entity prune and the orphan sweep run on every index
    run, so --full is not a periodic cleanup: its help must not tell the
    operator to schedule it for cleanup, and must say cleanup happens on every
    run."""
    text = _help("index-repo", "--full")

    assert not re.search(r"periodic", text, re.I), text
    assert not re.search(r"\bto clean ?up\b", text, re.I), text
    assert "every index run" in text, text


@pytest.mark.parametrize(
    ("option", "claims"),
    [
        ("--no-embed", ("prune", "deleted")),
        ("--no-retire", ("entity prune",)),
        ("--allow-mass-retire", ("entity prune", f"exit {EXIT_LIFECYCLE_ATTENTION}")),
    ],
)
def test_lifecycle_flag_help_states_its_effect_on_the_entity_prune(option, claims):
    """--no-embed still deletes the rows of pruned entities; --no-retire also
    suspends the entity prune; --allow-mass-retire also opens the entity-prune
    gate, whose trip makes the run exit 3."""
    text = _help("index-repo", option).lower()

    for claim in claims:
        assert claim.lower() in text, f"{option} help does not state: {claim!r} ({text})"
