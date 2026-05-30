# SPDX-License-Identifier: AGPL-3.0-or-later
"""Billing package — vendor-agnostic entitlement activation (M10B P1, ADR-0039).

The package top level re-exports only the **vendor-neutral** activation surface
(the single Activation contract + provisioning).  Vendor adapters are imported
namespaced so a second adapter never collides at the package top level:

    from src.billing.polar import verify_signature, parse_event, resolve_plan_id, EVENT_STATUS_MAP
    from src.billing.webhook_pipeline import WebhookAdapter, run_webhook_pipeline
    from src.billing._db import slug_to_plan_id
"""

from src.billing.activation import (
    EntitlementGrant,
    grant_entitlement,
    revoke_entitlement,
    update_entitlement,
)
from src.billing.provisioning import (
    claim_subscription_for_user,
    provision_or_upgrade,
)

__all__ = [
    "EntitlementGrant",
    "claim_subscription_for_user",
    "grant_entitlement",
    "provision_or_upgrade",
    "revoke_entitlement",
    "update_entitlement",
]
