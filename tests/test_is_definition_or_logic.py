# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_is_definition_or_logic.py
"""Unit tests for is_definition OR-semantics in writer_neo4j.py.

These tests verify the formula used in ON MATCH SET without Neo4j — by
directly exercising the Python-side evaluation that the Cypher query mirrors.

The invariant: is_definition is "sticky TRUE" — once set to TRUE it can
never be overwritten back to FALSE by an extension class (no _name).

Formula under test (Cypher):
    m.is_definition =
        coalesce(m.is_definition, false) OR ($had_explicit_name AND NOT $name IN $inherit_list)

Equivalently in Python:
    new_val = coalesce(existing, False) OR (had_explicit_name AND name not in inherit_list)
"""


def _compute_is_definition(
    existing: bool | None,
    had_explicit_name: bool,
    name: str,
    inherit_list: list[str],
) -> bool:
    """Python equivalent of the ON MATCH SET formula in writer_neo4j.py line 72.

    coalesce(m.is_definition, false) OR ($had_explicit_name AND NOT $name IN $inherit_list)
    """
    stored = existing if existing is not None else False
    incoming = had_explicit_name and (name not in inherit_list)
    return stored or incoming


def _compute_had_explicit_name(
    existing: bool | None,
    incoming: bool,
) -> bool:
    """Python equivalent of the ON MATCH SET formula in writer_neo4j.py line 71.

    coalesce(m.had_explicit_name, false) OR $had_explicit_name
    """
    stored = existing if existing is not None else False
    return stored or incoming


# ---------------------------------------------------------------------------
# is_definition OR-semantics
# ---------------------------------------------------------------------------

class TestIsDefinitionOrLogic:
    """All scenarios for the is_definition sticky-TRUE invariant."""

    def test_first_write_true_stored_as_true(self):
        """ON CREATE path (existing=None): definition class → TRUE."""
        result = _compute_is_definition(
            existing=None,
            had_explicit_name=True,
            name="sale.order",
            inherit_list=["portal.mixin"],
        )
        assert result is True

    def test_second_write_false_does_not_overwrite_true(self):
        """ON MATCH path: extension class (no _name) cannot clobber existing TRUE.

        Mirrors the real scenario:
          1st write: models/sale_order.py → _name='sale.order' → is_definition=TRUE
          2nd write: populate/sale_order.py → only _inherit='sale.order'
            → is_definition=FALSE incoming
          Expected: node stays TRUE.
        """
        result = _compute_is_definition(
            existing=True,
            had_explicit_name=False,   # populate file: no _name
            name="sale.order",
            inherit_list=["sale.order"],
        )
        assert result is True

    def test_first_write_false_then_true_becomes_true(self):
        """If extension class indexed before definition class, TRUE wins on second write."""
        # 1st write: populate file (no _name) → FALSE
        val_after_first = _compute_is_definition(
            existing=None,
            had_explicit_name=False,
            name="sale.order",
            inherit_list=["sale.order"],
        )
        assert val_after_first is False

        # 2nd write: definition file (_name='sale.order') → should flip to TRUE
        val_after_second = _compute_is_definition(
            existing=val_after_first,
            had_explicit_name=True,
            name="sale.order",
            inherit_list=["portal.mixin"],
        )
        assert val_after_second is True

    def test_self_extension_pattern_stays_false(self):
        """Self-extension pattern: _name='X' AND _inherit=['X'] → is_definition=FALSE.

        This is CORRECT per the diagnose report note:
        'The 366 nodes with had_explicit_name=TRUE, is_definition=FALSE are
         CORRECT behavior (self-extension pattern).'
        """
        result = _compute_is_definition(
            existing=None,
            had_explicit_name=True,
            name="sale.order",
            inherit_list=["sale.order"],  # name IS in inherit_list → not a definition
        )
        assert result is False

    def test_self_extension_after_definition_stays_false(self):
        """Self-extension class cannot clobber a TRUE written by a true definition class.

        This covers a subtle case:
          1st write: definition class (had_explicit_name=True, name not in inherit_list) → TRUE
          2nd write: self-extension (_name='X', _inherit=['X']) → incoming=FALSE
          Expected: stays TRUE (OR semantics preserve the first TRUE).
        """
        result = _compute_is_definition(
            existing=True,             # set by the true definition write
            had_explicit_name=True,    # self-ext has _name
            name="sale.order",
            inherit_list=["sale.order"],  # _inherit=['sale.order'] → incoming FALSE
        )
        assert result is True

    def test_null_existing_treated_as_false(self):
        """coalesce(NULL, false) → extension class yields FALSE when no prior write."""
        result = _compute_is_definition(
            existing=None,
            had_explicit_name=False,
            name="sale.order",
            inherit_list=["sale.order"],
        )
        assert result is False

    def test_multi_inherit_not_self(self):
        """Model with _name='X' and _inherit=['Y', 'Z'] (no self) → is_definition=TRUE."""
        result = _compute_is_definition(
            existing=None,
            had_explicit_name=True,
            name="sale.order",
            inherit_list=["portal.mixin", "mail.thread", "mail.activity.mixin"],
        )
        assert result is True


# ---------------------------------------------------------------------------
# had_explicit_name OR-semantics
# ---------------------------------------------------------------------------

class TestHadExplicitNameOrLogic:
    """All scenarios for had_explicit_name sticky-TRUE invariant."""

    def test_first_write_true_stored_as_true(self):
        result = _compute_had_explicit_name(existing=None, incoming=True)
        assert result is True

    def test_second_write_false_does_not_overwrite_true(self):
        """Extension class (no _name) must not clobber TRUE from definition class."""
        result = _compute_had_explicit_name(existing=True, incoming=False)
        assert result is True

    def test_first_write_false_then_true_becomes_true(self):
        val_first = _compute_had_explicit_name(existing=None, incoming=False)
        assert val_first is False
        val_second = _compute_had_explicit_name(existing=val_first, incoming=True)
        assert val_second is True

    def test_null_existing_with_false_incoming(self):
        result = _compute_had_explicit_name(existing=None, incoming=False)
        assert result is False

    def test_both_true_stays_true(self):
        result = _compute_had_explicit_name(existing=True, incoming=True)
        assert result is True
