# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parse_health.py
"""Per-module parse completeness (ADR-0056 B14).

The intra-module entity prune deletes every node of a re-parsed module that
the parse did not produce. That is only safe when the parse saw every file of
the module. Parsers skip an unreadable or unparseable file and carry on, so
their return values cannot tell "the file defines nothing" from "the file
could not be read". This module carries that signal out of the parsers
without changing their return types:

* the pipeline opens :func:`track` around ONE module's parse;
* a parser calls :func:`note_failure` wherever it drops a file (or part of
  one) because it could not read or parse it, and :func:`note_unobserved`
  when a whole node family was not looked at this run;
* the pipeline reads the resulting :class:`ModuleParseHealth`: ``degraded``
  modules are never pruned, ``unobserved_labels`` are left out of the prune.

Outside :func:`track` both note functions are no-ops, so parsers stay
callable on their own. The state is a ``ContextVar``: each indexer thread
tracks its own module.
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class ModuleParseHealth:
    """What one module's parse could not see."""

    module: str
    odoo_version: str
    failures: list[str] = field(default_factory=list)
    # The files behind ``failures`` (as reported, usually absolute).
    failure_paths: set[str] = field(default_factory=set)
    # True when at least one failure is of a class that can clear without the
    # file changing (an OSError: unreadable file, permission, IO); False when
    # every failure is about the file's content (a syntax error), which only
    # a source change can fix.
    transient: bool = False
    unobserved_labels: set[str] = field(default_factory=set)
    # Keys ``(chunk_type, entity_name, file_path, chunk_idx)`` of the embedding
    # chunks this parse produces for the module (written, or with no embedder
    # only computed, so the prune can still delete the rows of removed
    # entities); None when pgvector is unavailable.
    embedded_keys: set[tuple] | None = None
    # True when the chunks were (re-)embedded and upserted this run.
    embeddings_written: bool = False
    # True when a chunk of the module could not be embedded this run: its row
    # keeps an older ``indexed_at``, so the rows cannot tell what this parse
    # produced (the shared-module prune then leaves the embeddings alone).
    embeddings_incomplete: bool = False

    @property
    def degraded(self) -> bool:
        """True when at least one file of the module was not fully parsed."""
        return bool(self.failures)


_CURRENT: contextvars.ContextVar[ModuleParseHealth | None] = contextvars.ContextVar(
    "osm_module_parse_health", default=None,
)


@contextmanager
def track(module: str, odoo_version: str) -> Iterator[ModuleParseHealth]:
    """Collect the parse failures of *module* raised inside the block."""
    health = ModuleParseHealth(module=module, odoo_version=odoo_version)
    token = _CURRENT.set(health)
    try:
        yield health
    finally:
        _CURRENT.reset(token)


def note_failure(path: object, reason: str, *, transient: bool = False) -> None:
    """Record that *path* (or part of it) was dropped: *reason* says why.

    *transient* marks a failure class that can clear with the file unchanged
    (an OSError reading it); the pipeline retries such a module once.
    """
    health = _CURRENT.get()
    if health is not None:
        health.failure_paths.add(str(path))
        health.transient = health.transient or transient
        entry = f"{path}: {reason}"
        if entry not in health.failures:
            health.failures.append(entry)


def empty_source(path: object) -> bool:
    """True when *path* holds nothing but whitespace (a 0-byte file included).

    An empty file defines nothing: a parser that rejects it (lxml: "Document
    is empty") must return no nodes WITHOUT a failure, or the module would be
    marked degraded - never pruned, attention on every run - for valid input
    (real case: odoo 16.0 ``mass_mailing/data/mass_mailing_data.xml``).
    Unreadable files are not empty (False).
    """
    try:
        with open(str(path), "rb") as fh:
            head = fh.read(65536)
            return not head.strip() and not fh.read(1)
    except OSError:
        return False


def note_unobserved(label: str) -> None:
    """Record that the parse did not look for *label* nodes at all this run."""
    health = _CURRENT.get()
    if health is not None:
        health.unobserved_labels.add(label)


def note_embeddings_incomplete() -> None:
    """Record that some chunk of the tracked module was not (re-)embedded."""
    health = _CURRENT.get()
    if health is not None:
        health.embeddings_incomplete = True
