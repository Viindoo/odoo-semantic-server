# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic v2 ``*Ref`` DTOs — composite-key identifiers for MCP responses.

``*Ref`` types are composite-key identifiers that uniquely address a node in
Neo4j and can round-trip across the MCP boundary without context loss. Each
``*Ref`` carries ``odoo_version`` so the client can re-issue a drill-down call
without remembering the version from the outer conversation.

Composite keys follow ADR-0013 (Model/Field/Method) and the corresponding
parser schemas:
   - ``ModelRef``      → (module, name, odoo_version)   — matches Model node MERGE key
   - ``FieldRef``      → (model, name, module, odoo_version)
   - ``MethodRef``     → (model, name, module, odoo_version)
   - ``ViewRef``       → (xmlid, model, odoo_version)    — model may be None for QWeb
   - ``ModuleRef``     → (name, odoo_version, profile)   — profile = ADR-0016 array
   - ``PatternRef``    → (pattern_id, odoo_version_range)
   - ``CoreSymbolRef`` → (symbol, kind, odoo_version)

The retired ``*Output`` response DTOs (the dual-channel structured subsystem)
were physically removed when ADR-0028 made all tools text-only
(``output_schema=None``, no ``structuredContent``); see ADR-0028 / ADR-0048.
This module does NOT import from ``server.py`` — it is a pure data layer.
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

