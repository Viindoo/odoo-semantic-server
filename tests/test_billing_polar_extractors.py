# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Polar webhook payload extractors (no DB, no network).

These cover the contract-hardening additions in ``src/web_ui/routes/webhooks.py``:

  * ``_extract_buyer_email`` — tolerant of the nested ``customer.email``, the
    flat ``customer_email``, and the ``user.email`` fallback; never crashes when
    email is absent (it is optional/nullable for some Polar events).
  * ``_extract_cancel_at_period_end`` — reads ``data.cancel_at_period_end`` so a
    ``subscription.uncanceled`` (flag=False) reconciles a locally-scheduled
    cancel; ``None`` when the vendor omitted the field (leave local flag alone).

The extractors are pure functions; the route module imports without a DB, so
these stay hermetic (NO pytest.mark.postgres / neo4j).  See
01-polar-contract-verification.md.
"""

from __future__ import annotations

from src.web_ui.routes.webhooks import (
    _extract_buyer_email,
    _extract_cancel_at_period_end,
)


class TestExtractBuyerEmail:
    def test_nested_customer_email(self):
        assert (
            _extract_buyer_email({"customer": {"email": "nested@example.com"}})
            == "nested@example.com"
        )

    def test_flat_customer_email_fallback(self):
        assert (
            _extract_buyer_email({"customer_email": "flat@example.com"})
            == "flat@example.com"
        )

    def test_user_email_fallback(self):
        assert (
            _extract_buyer_email({"user": {"email": "user@example.com"}})
            == "user@example.com"
        )

    def test_nested_customer_wins_over_flat(self):
        assert (
            _extract_buyer_email(
                {
                    "customer": {"email": "nested@example.com"},
                    "customer_email": "flat@example.com",
                }
            )
            == "nested@example.com"
        )

    def test_absent_email_returns_none_no_crash(self):
        assert _extract_buyer_email({}) is None
        assert _extract_buyer_email({"customer": "not-a-dict"}) is None
        assert _extract_buyer_email({"customer": {}}) is None
        assert _extract_buyer_email({"user": {}}) is None


class TestExtractCancelAtPeriodEnd:
    def test_true_when_scheduled(self):
        assert _extract_cancel_at_period_end({"cancel_at_period_end": True}) is True

    def test_false_when_reactivated(self):
        """subscription.uncanceled carries cancel_at_period_end=False → reconcile."""
        assert _extract_cancel_at_period_end({"cancel_at_period_end": False}) is False

    def test_none_when_absent_leaves_flag_untouched(self):
        assert _extract_cancel_at_period_end({}) is None

    def test_truthy_coerced_to_bool(self):
        assert _extract_cancel_at_period_end({"cancel_at_period_end": 1}) is True
        assert _extract_cancel_at_period_end({"cancel_at_period_end": 0}) is False
