"""ORM-validation MCP tool wrappers (split out of src/mcp/server.py, Phase 1).

Thin wrappers over the static ORM checks implemented in ``src/mcp/orm.py``.
Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.  The impl
functions are imported directly from ``src.mcp.orm`` (the same source server.py
uses), so this module does not need a late import of ``server`` for them.
"""

from src.mcp.orm import (
    _resolve_orm_chain,
    _validate_depends,
    _validate_domain,
    _validate_relation,
)
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_bounded,
)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_bounded
def resolve_orm_chain(
    model: str,
    dotted_path: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Walk a dotted ORM field path and return the terminal field type.

    Traverses 'partner_id.country_id.code' hop by hop across the indexed Field
    graph (following many2one/one2many/many2many comodels), reporting the
    terminal type or the exact hop where the path breaks.

    Inherited fields resolve depth-first: a field on a nearer ancestor (mixin)
    shadows the same field name on a farther one.

    TRIGGER when: "what type is sale.order.partner_id.country_id.code", "does
    this dotted path resolve", "trace a field path", "field nào ở cuối chain",
    "kiểm tra đường dẫn field a.b.c có hợp lệ không"
    PREFER over: entity_lookup(kind='field') when you have a multi-hop dotted
    path rather than a single field.
    SKIP when: validating a whole domain or @api.depends — use validate_domain
    / validate_depends (they call this primitive per term).

    Args:
        model: Root dotted model name, e.g. 'sale.order'.
        dotted_path: Dotted field path, e.g. 'partner_id.country_id.code'.
        profile_name: Optional profile filter.

    Returns:
        Tree: one line per resolved hop (field : type -> comodel), terminal
        tagged, or a BROKEN line naming the first unresolved hop.
    """
    return _resolve_orm_chain(model, dotted_path, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_bounded
def validate_domain(
    model: str,
    domain: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Validate a search domain's field-paths and operators against the graph.

    Parses the domain literal and checks each (field_path, operator, value)
    term: every field-path hop must resolve in the Field graph, and the operator
    must be valid for the version ('any'/'not any' only exist from v17). Catches
    hallucinated fields before they reach a user.

    Inherited fields resolve depth-first: a field on a nearer ancestor (mixin)
    shadows the same field name on a farther one.

    TRIGGER when: "is this domain valid", "check domain [('x','=',1)]", "validate
    search domain for sale.order", "domain này có field sai không", "kiểm tra
    domain trước khi dùng"
    PREFER over: resolve_orm_chain when you have a full domain (multiple terms).
    SKIP when: validating @api.depends — use validate_depends.

    Args:
        model: Dotted model the domain runs on, e.g. 'sale.order'.
        domain: Domain literal, e.g. "[('partner_id.country_id', '=', 'VN')]".
        profile_name: Optional profile filter.

    Returns:
        Tree: per-term OK / ERROR (bad field-path or invalid operator) with a
        verdict header. Logical connectors (&, |, !) are skipped.
    """
    return _validate_domain(model, domain, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_bounded
def validate_depends(
    model: str,
    method: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Validate a compute method's @api.depends paths against the Field graph.

    Reads the indexed @api.depends('a.b', ...) arguments of the method and
    checks each dependency path resolves; flags depends on 'id' (Odoo forbids
    it) and suggests the closest field name for typos.

    Inherited fields resolve depth-first: a field on a nearer ancestor (mixin)
    shadows the same field name on a farther one.

    TRIGGER when: "are the @api.depends on _compute_x correct", "validate depends
    of this compute method", "check compute dependencies", "depends của method
    này có field sai không", "kiểm tra @api.depends"
    PREFER over: resolve_orm_chain when checking an existing method's declared
    dependencies (not an ad-hoc path).
    SKIP when: the path is in a domain, not a depends — use validate_domain.

    Args:
        model: Dotted model name, e.g. 'sale.order'.
        method: Compute method name, e.g. '_compute_amount_total'.
        profile_name: Optional profile filter.

    Returns:
        Tree: per-dependency OK / ERROR (missing field, depends-on-id, typo
        suggestion). Note line when the method has no @api.depends (or era1).
    """
    return _validate_depends(model, method, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_bounded
def validate_relation(
    model: str,
    field: str,
    target_model: str,
    odoo_version: RequiredOdooVersion,
    profile_name: str | None = None,
) -> str:
    """Assert a relational field points at an expected comodel.

    Checks that model.field is a many2one/one2many/many2many whose comodel is
    target_model (or a subtype of it via inheritance). Reports the actual
    comodel on mismatch and suggests the closest field name when missing.

    Inherited fields resolve depth-first: a field on a nearer ancestor (mixin)
    shadows the same field name on a farther one.

    TRIGGER when: "does sale.order.partner_id point to res.partner", "is this
    field a many2one to res.users", "check relation target", "field X có trỏ
    đúng model Y không", "kiểm tra quan hệ field"
    PREFER over: entity_lookup(kind='field') when you specifically want to assert
    the comodel rather than read all field detail.
    SKIP when: tracing a multi-hop path — use resolve_orm_chain.

    Args:
        model: Dotted model name, e.g. 'sale.order'.
        field: Relational field name, e.g. 'partner_id'.
        target_model: Expected comodel, e.g. 'res.partner'.
        profile_name: Optional profile filter.

    Returns:
        Tree: OK (field -> comodel) or MISMATCH (actual vs expected) or ERROR
        (field not found / not relational), with a Next-step footer.
    """
    return _validate_relation(model, field, target_model, odoo_version, profile_name)
