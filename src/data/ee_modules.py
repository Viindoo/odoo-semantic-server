# SPDX-License-Identifier: AGPL-3.0-or-later
"""EE confusion list — Odoo Enterprise modules vắng trên stack Community/Viindoo.

Source: 2026-05-08 survey 16 modules verified absent từ
~/git/odoo{17,18,19}/odoo/addons/. Mapping → Viindoo equivalent
từ tvtmaaddons17 + erponline-enterprise17 surveyed addons.

DO NOT DEPEND on these modules in Viindoo Community stack — vi phạm
GPL/Enterprise license boundary (per CLAUDE.md §2 stack rule).
"""

_SOURCE_DATE = "2026-05-08"

EE_CONFUSION: dict[str, str | None] = {
    # module_name: viindoo_equivalent_qname (None = no equivalent)
    "knowledge": None,
    "documents": "viin_document",
    "helpdesk": "viin_helpdesk",
    "marketing_automation": None,
    "quality": "to_quality",
    "industry_fsm": None,
    "appointment": "viin_appointment",
    "planning": None,
    "sign": "viin_sign",
    "social": "viin_social",
    "voip": None,
    "whatsapp": None,
    "mrp_plm": "to_mrp_plm",
    "accountant": "to_account_accountant",
    "web_studio": None,
    "web_enterprise": None,
}
