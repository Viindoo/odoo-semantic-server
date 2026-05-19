"""Pydantic v2 DTOs for dual-channel MCP responses (M10.5 Wave B, WI-B2).

Two layers:

1. **``*Ref`` types** — composite-key identifiers that uniquely address a node
   in Neo4j and can round-trip across the MCP boundary without context loss.
   Each ``*Ref`` carries ``odoo_version`` so the client can re-issue a drill-down
   call without remembering the version from the outer conversation.

   Composite keys follow ADR-0013 (Model/Field/Method) and the corresponding
   parser schemas:
   - ``ModelRef``      → (module, name, odoo_version)   — matches Model node MERGE key
   - ``FieldRef``      → (model, name, module, odoo_version)
   - ``MethodRef``     → (model, name, module, odoo_version)
   - ``ViewRef``       → (xmlid, model, odoo_version)    — model may be None for QWeb
   - ``ModuleRef``     → (name, odoo_version, profile)   — profile = ADR-0016 array
   - ``PatternRef``    → (pattern_id, odoo_version_range)
   - ``CoreSymbolRef`` → (symbol, kind, odoo_version)

2. **``*Output`` types** — top-level response schemas for the 7 priority tools.
   Each ``*Output`` carries ``next_step_hint: str`` (per ADR-0023 §4) that
   mirrors the trailing ``└─ Next: ...`` footer in the text channel.

Wave B3 (WI-B3) will wire ``ToolResult(structured_content=...)`` using these
types. This module does NOT import from ``server.py`` — it is a pure data layer.
"""

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# *Ref types — composite-key identifiers (7 total)
# ---------------------------------------------------------------------------


class ModelRef(BaseModel):
    """Composite key per ADR-0013 — uniquely identifies a Model node in Neo4j.

    ``module`` is the *defining* module (the winner of the 5-tier ranking
    heuristic), not an extension wrapper module.  ``name`` is the dotted
    technical name (e.g. ``sale.order``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Dotted technical model name, e.g. 'sale.order'")
    module: str = Field(description="Defining module name, e.g. 'sale'")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")


class FieldRef(BaseModel):
    """Composite key for a Field node — (model, name, module, odoo_version).

    ``ref`` carries the opaque short ID minted by ``mint_refs()`` (e.g. ``'f3'``).
    It is ``None`` when the FieldRef is constructed outside a list_fields call
    (e.g. in resolve_field's declared_in list or in Wave-B tests).
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Parent model dotted name, e.g. 'sale.order'")
    name: str = Field(description="Field technical name, e.g. 'amount_total'")
    module: str = Field(description="Declaring module name")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")
    ref: str | None = Field(
        default=None,
        description=(
            "Opaque ref ID minted by list_fields (e.g. 'f3'). "
            "Pass as target= to resolve_field for a frictionless drill-down. "
            "None when this FieldRef was not produced by list_fields."
        ),
    )


class MethodRef(BaseModel):
    """Composite key for a Method node — (model, name, module, odoo_version).

    ``ref`` carries the opaque short ID minted by ``mint_refs()`` (e.g. ``'m2'``).
    It is ``None`` when the MethodRef is constructed outside a list_methods call.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Parent model dotted name")
    name: str = Field(description="Method name, e.g. 'action_confirm'")
    module: str = Field(description="Declaring module name")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")
    ref: str | None = Field(
        default=None,
        description=(
            "Opaque ref ID minted by list_methods (e.g. 'm2'). "
            "Pass as target= to resolve_method for a frictionless drill-down. "
            "None when this MethodRef was not produced by list_methods."
        ),
    )


class ViewRef(BaseModel):
    """Composite key for a View node — (xmlid, model, odoo_version).

    ``model`` is ``None`` for pure QWeb templates that are not tied to
    a specific Odoo model.
    """

    model_config = ConfigDict(extra="forbid")

    xmlid: str = Field(
        description="Full XML ID including module prefix, e.g. 'sale.view_order_form'"
    )
    model: str | None = Field(
        default=None,
        description="Target model dotted name; None for QWeb-only templates",
    )
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")


class ModuleRef(BaseModel):
    """Composite key for a Module node — (name, odoo_version) + ADR-0016 profile array."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Technical module name, e.g. 'sale'")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")
    profile: list[str] | None = Field(
        default=None,
        description="ADR-0016 profile array; None when module is not profile-scoped",
    )


class PatternRef(BaseModel):
    """Identifier for a pattern in the PatternExample catalogue (ADR-0003)."""

    model_config = ConfigDict(extra="forbid")

    pattern_id: str = Field(
        description="Stable pattern identifier, e.g. 'compute-stored-field'"
    )
    odoo_version_range: str = Field(
        description="Version range the pattern applies to, e.g. 'v14-v17'"
    )


class CoreSymbolRef(BaseModel):
    """Identifier for a CoreSymbol node (ADR-0005 core coverage index)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(
        description="Fully qualified symbol name, e.g. 'odoo.models.BaseModel'"
    )
    kind: str = Field(
        description="Symbol kind: 'class' | 'method' | 'function' | 'constant'"
    )
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")


# ---------------------------------------------------------------------------
# *Output types — top-level response schemas for 7 priority tools
# ---------------------------------------------------------------------------


class ResolveModelOutput(BaseModel):
    """Structured response for ``resolve_model`` (layer-0 model overview).

    ``extended_by`` is ordered per INHERITS edge ``order`` property — the
    first entry is the lowest-level extension (typically the base module).
    ``next_step_hint`` mirrors the ``└─ Next: ...`` footer in the text channel.
    """

    model_config = ConfigDict(extra="forbid")

    ref: ModelRef = Field(description="Composite key identifying this model")
    is_definition: bool = Field(
        description="True when the defining module is authoritative (ADR-0013 T1 flag)"
    )
    defined_in: ModuleRef = Field(
        description="Winning module per 5-tier ranking heuristic (ADR-0013)"
    )
    extended_by: list[ModuleRef] = Field(
        default_factory=list,
        description="Modules that extend this model, ordered by extension ranking",
    )
    inherits_from: list[str] = Field(
        default_factory=list,
        description="Parent model names this model inherits from (via _inherit)",
    )
    field_count: int = Field(description="Total number of fields across all modules")
    method_count: int = Field(description="Total number of methods across all modules")
    next_step_hint: str = Field(
        description=(
            "Next-step footer mirroring text channel, e.g. "
            "'└─ Next: list_fields(...) | list_methods(...)'"
        )
    )


class ResolveFieldOutput(BaseModel):
    """Structured response for ``resolve_field`` (field declaration chain)."""

    model_config = ConfigDict(extra="forbid")

    ref: FieldRef = Field(description="Composite key identifying this field")
    ttype: str = Field(description="Field type, e.g. 'monetary', 'many2one'")
    computed: bool = Field(description="True when the field has a compute method")
    compute_method: str | None = Field(
        default=None,
        description="Name of the compute method if computed is True",
    )
    stored: bool = Field(
        default=True,
        description="True when the computed field is stored in DB",
    )
    required: bool = Field(default=False, description="True when field is required")
    related: str | None = Field(
        default=None,
        description="Related field path if this is a related field",
    )
    declared_in: list[FieldRef] = Field(
        description="All modules declaring this field, ordered by ranking heuristic"
    )
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )


class ResolveMethodOutput(BaseModel):
    """Structured response for ``resolve_method`` (method override chain)."""

    model_config = ConfigDict(extra="forbid")

    ref: MethodRef = Field(description="Composite key identifying this method")
    override_chain: list[MethodRef] = Field(
        description="All overrides ordered by ranking heuristic (first = authoritative)"
    )
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )


class ResolveViewOutput(BaseModel):
    """Structured response for ``resolve_view`` (XML view + XPath chain)."""

    model_config = ConfigDict(extra="forbid")

    ref: ViewRef = Field(description="Composite key identifying this view")
    view_type: str = Field(description="View type: 'form' | 'tree' | 'kanban' | ...")
    module: str = Field(description="Defining module name")
    mode: str | None = Field(
        default=None,
        description="'extension' when the view inherits; None for base views",
    )
    inherits_from: str | None = Field(
        default=None,
        description="Parent view xmlid when mode is 'extension'",
    )
    xpath_count: int = Field(
        default=0,
        description="Number of XPath modifications declared by this view",
    )
    extended_by: list[ViewRef] = Field(
        default_factory=list,
        description="Child views that inherit from this view",
    )
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )


class DescribeModuleOutput(BaseModel):
    """Structured response for ``describe_module`` (module architecture overview)."""

    model_config = ConfigDict(extra="forbid")

    ref: ModuleRef = Field(description="Composite key identifying this module")
    edition: str = Field(
        description="Module edition: 'community' | 'enterprise' | 'viindoo' | 'oca' | 'custom'"
    )
    version_raw: str | None = Field(
        default=None,
        description="Raw version string from __manifest__, e.g. '17.0.1.0.0'",
    )
    depends: list[str] = Field(
        default_factory=list,
        description="Module names listed in depends (from manifest)",
    )
    defines_models: list[str] = Field(
        default_factory=list,
        description="Model names for which this module is the authoritative definition",
    )
    extends_models: list[str] = Field(
        default_factory=list,
        description="Model names this module extends (is_definition=False)",
    )
    view_total: int = Field(default=0, description="Total view count in this module")
    js_patch_count: int = Field(default=0, description="JS patch count in this module")
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )


class ListFieldsOutput(BaseModel):
    """Structured response for ``list_fields`` (field enumeration by module)."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Model technical name the fields belong to")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")
    total: int = Field(
        description="True total count before any truncation (from count query)"
    )
    shown: int = Field(description="Number of FieldRef entries actually returned")
    fields: list[FieldRef] = Field(
        default_factory=list,
        description="Field references, ordered by (edition_rank, module, name)",
    )
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )


class ListMethodsOutput(BaseModel):
    """Structured response for ``list_methods`` (method enumeration by module)."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Model technical name the methods belong to")
    odoo_version: str = Field(description="Odoo version string, e.g. '17.0'")
    total: int = Field(
        description="True total count before any truncation (from count query)"
    )
    shown: int = Field(description="Number of MethodRef entries actually returned")
    methods: list[MethodRef] = Field(
        default_factory=list,
        description="Method references, ordered by (edition_rank, module, name)",
    )
    override_names: list[str] = Field(
        default_factory=list,
        description="Method names that appear in ≥2 modules (override-points per ADR-0023 §5.3)",
    )
    next_step_hint: str = Field(
        description="Next-step footer mirroring text channel"
    )
