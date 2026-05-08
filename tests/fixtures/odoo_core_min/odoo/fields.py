# Minimal stub modelling Odoo's fields.py — written from scratch for parser smoke tests.
# DOES NOT contain real Odoo source. Purpose: exercise parse_odoo_core kind=field_type.
"""Odoo ORM field descriptor stubs.

Provides the base Field class and the concrete field descriptors used
to declare model attributes. Each field class maps to a column type
in the backing PostgreSQL database.
"""


class Field:
    """Base descriptor for all Odoo ORM field types.

    Subclass this to create a new field type. Concrete subclasses
    must set `type` and optionally override `convert` and `to_column_type`.

    Args:
        string: Human-readable label displayed in views.
        required: If True, the field must have a non-empty value.
        readonly: If True, the field cannot be written by users.
        store: If True (default), the value is persisted in the database.
        compute: Optional method name that computes the field value.
        related: Optional dot-separated path for related field access.
    """

    type = None

    def __init__(self, string=None, required=False, readonly=False,
                 store=True, compute=None, related=None, **kwargs):
        self.string = string
        self.required = required
        self.readonly = readonly
        self.store = store
        self.compute = compute
        self.related = related

    def convert(self, value):
        """Convert a raw value to the field's Python type.

        Args:
            value: Raw value from database or user input.

        Returns:
            Converted value in the field's Python representation.
        """
        return value

    def to_column_type(self):
        """Return the PostgreSQL column type for this field.

        Returns:
            String SQL type fragment, e.g. 'VARCHAR'.
        """
        raise NotImplementedError


class Char(Field):
    """Single-line text field stored as VARCHAR.

    Args:
        size: Optional maximum length; None means unlimited.
        translate: If True, the value can be translated per language.
    """

    type = "char"

    def __init__(self, string=None, size=None, translate=False, **kwargs):
        super().__init__(string=string, **kwargs)
        self.size = size
        self.translate = translate

    def to_column_type(self):
        """Return VARCHAR column type, respecting optional size constraint."""
        if self.size:
            return f"VARCHAR({self.size})"
        return "VARCHAR"


class Integer(Field):
    """Integer field stored as INTEGER.

    Args:
        group_operator: SQL aggregate function for group-by operations.
    """

    type = "integer"
    group_operator = "sum"

    def to_column_type(self):
        """Return INTEGER column type."""
        return "INTEGER"

    def convert(self, value):
        """Convert value to int, returning 0 for falsy non-zero inputs."""
        if value is None or value is False:
            return 0
        return int(value)


class Float(Field):
    """Floating-point field stored as NUMERIC.

    Args:
        digits: Tuple (precision, scale) for the NUMERIC column.
        group_operator: SQL aggregate function for group-by operations.
    """

    type = "float"
    group_operator = "sum"

    def __init__(self, string=None, digits=None, **kwargs):
        super().__init__(string=string, **kwargs)
        self.digits = digits

    def to_column_type(self):
        """Return NUMERIC column type with optional precision/scale."""
        if self.digits:
            return f"NUMERIC({self.digits[0]}, {self.digits[1]})"
        return "NUMERIC"


class Many2one(Field):
    """Many-to-one relational field stored as INTEGER (FK).

    Args:
        comodel_name: The Odoo model name this field points to.
        ondelete: PostgreSQL ON DELETE behaviour ('restrict', 'cascade', 'set null').
    """

    type = "many2one"

    def __init__(self, comodel_name, string=None, ondelete="set null", **kwargs):
        super().__init__(string=string, **kwargs)
        self.comodel_name = comodel_name
        self.ondelete = ondelete

    def to_column_type(self):
        """Return INTEGER column type (FK stored as integer)."""
        return "INTEGER"
