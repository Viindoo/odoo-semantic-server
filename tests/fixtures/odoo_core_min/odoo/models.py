# SPDX-License-Identifier: AGPL-3.0-or-later
# Minimal stub modelling Odoo's models.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=orm_class/orm_method.
"""Odoo ORM base model stubs.

Provides the BaseModel class and its three public subclasses: Model for
persistent records, TransientModel for wizard-style temporary records, and
AbstractModel for mixin classes with no backing table.
"""


class BaseModel:
    """Root of the Odoo ORM class hierarchy.

    All Odoo model classes ultimately inherit from BaseModel, which provides
    the core record-set API: search, browse, create, write, unlink, and the
    helper methods used throughout the framework.

    Class attributes (defined by subclasses):
        _name:        Technical model name, e.g. 'sale.order'.
        _description: Human-readable model description shown in UI.
        _inherit:     Parent model(s) to extend.
        _order:       Default sort order for search results.
        _rec_name:    Field name used as the record's display name.
    """

    _name = None
    _description = None
    _inherit = None
    _order = "id"
    _rec_name = "name"

    def search(self, domain, limit=None, offset=0, order=None):
        """Search for records matching *domain*.

        Args:
            domain: List of (field, operator, value) triples (Odoo domain syntax).
            limit: Maximum number of records to return; None means no limit.
            offset: Number of records to skip (for pagination).
            order: Override the model's default sort order.

        Returns:
            RecordSet of matching records.
        """
        raise NotImplementedError

    def browse(self, ids):
        """Return a RecordSet for the given database IDs.

        Args:
            ids: Single integer ID or list of integer IDs.

        Returns:
            RecordSet wrapping the given IDs (records may not exist).
        """
        raise NotImplementedError

    def create(self, vals):
        """Create a new record with the given field values.

        Args:
            vals: Dict mapping field names to their new values.

        Returns:
            RecordSet containing the newly created record.
        """
        raise NotImplementedError

    def write(self, vals):
        """Update the records in this RecordSet with the given field values.

        Args:
            vals: Dict mapping field names to their new values.

        Returns:
            True on success.
        """
        raise NotImplementedError

    def unlink(self):
        """Delete the records in this RecordSet from the database.

        Returns:
            True on success.
        """
        raise NotImplementedError

    def copy(self, default=None):
        """Duplicate the record, optionally overriding some field values.

        Args:
            default: Dict of field values to set on the copy instead of
                     copying from the original.

        Returns:
            RecordSet containing the new duplicate record.
        """
        raise NotImplementedError

    def exists(self):
        """Filter this RecordSet to records that actually exist in the DB.

        Returns:
            RecordSet containing only the records that still exist.
        """
        raise NotImplementedError

    def ensure_one(self):
        """Assert this RecordSet contains exactly one record.

        Raises:
            ValueError: If the RecordSet has 0 or more than 1 records.

        Returns:
            Self (the single-record RecordSet).
        """
        if len(self) != 1:
            raise ValueError(f"Expected singleton: {self!r}")
        return self

    def mapped(self, func):
        """Apply *func* to each record, returning a list or RecordSet.

        Args:
            func: A callable or dot-separated field path string.

        Returns:
            List of results, or a merged RecordSet when func returns RecordSets.
        """
        raise NotImplementedError

    def filtered(self, func):
        """Filter this RecordSet to records for which *func* returns truthy.

        Args:
            func: A callable taking a single record, or a field name string.

        Returns:
            RecordSet containing only the records that pass the filter.
        """
        raise NotImplementedError

    def sorted(self, key=None, reverse=False):
        """Return a new RecordSet sorted by *key*.

        Args:
            key: Field name string or callable taking a record; None uses _order.
            reverse: If True, sort in descending order.

        Returns:
            New RecordSet in sorted order.
        """
        raise NotImplementedError

    def with_context(self, *args, **kwargs):
        """Return a new RecordSet bound to an updated execution context.

        Args:
            *args: Optional dict to merge into the current context.
            **kwargs: Additional context key-value pairs.

        Returns:
            New RecordSet with the merged context.
        """
        raise NotImplementedError

    def with_company(self, company):
        """Return a new RecordSet with the given company as the active company.

        Args:
            company: A res.company record or integer company ID.

        Returns:
            New RecordSet operating under the specified company.
        """
        raise NotImplementedError


class Model(BaseModel):
    """Persistent model with a backing PostgreSQL table.

    Subclass this for models whose records should be stored in the database
    and visible across sessions. This is the most common base class.

    Class attribute _auto defaults to True (table is auto-created).
    """

    _auto = True
    _abstract = False
    _transient = False


class TransientModel(BaseModel):
    """Temporary model backed by a PostgreSQL table, auto-vacuumed.

    Use this for wizards and dialogs whose records should be discarded after
    the user closes the form. The scheduler periodically deletes old records.

    Class attribute _transient is True; records are auto-cleaned after 1 hour
    by default (configurable via transient_age_limit system parameter).
    """

    _auto = True
    _abstract = False
    _transient = True


class AbstractModel(BaseModel):
    """Mixin model with no backing table — used for shared behaviour.

    Subclass this to define reusable methods and fields that other models
    can inherit via _inherit. No table is created for AbstractModel subclasses.

    Class attribute _abstract is True; _auto is False.
    """

    _auto = False
    _abstract = True
    _transient = False
