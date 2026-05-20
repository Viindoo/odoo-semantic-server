# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/login_attempts.py
"""Postgres-backed login attempt tracking and rate limiting (M9 W-AC — F2, F3).

Replaces in-process _LOGIN_FAILURES dict with a shared Postgres table so that
all uvicorn workers see the same counters (multi-worker safe).

Public API:
    record_login_attempt(...)   — INSERT row into login_attempts
    check_rate_limit(...)       — return True if identifier or IP is over threshold
    get_client_ip(request)      — resolve real client IP, honoring TRUSTED_PROXY_CIDRS
"""

import ipaddress
import logging
import os

from starlette.requests import Request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit thresholds
# ---------------------------------------------------------------------------
_RATE_WINDOW_MINUTES = 15
_RATE_MAX_FAILURES_PER_USER = 5
_RATE_MAX_FAILURES_PER_IP = 20

# ---------------------------------------------------------------------------
# Trusted proxy CIDR list (lazy-parsed from TRUSTED_PROXY_CIDRS env var)
# ---------------------------------------------------------------------------
_TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None


def _get_trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse TRUSTED_PROXY_CIDRS env var once and cache the result.

    Returns an empty list if the env var is unset or blank (safe default:
    no X-Forwarded-For headers are trusted).
    """
    global _TRUSTED_PROXIES
    if _TRUSTED_PROXIES is None:
        raw = os.getenv("TRUSTED_PROXY_CIDRS", "")
        proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for cidr in raw.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                proxies.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                logger.warning("TRUSTED_PROXY_CIDRS: invalid CIDR %r — ignored", cidr)
        _TRUSTED_PROXIES = proxies
    return _TRUSTED_PROXIES


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP address for the request.

    If the direct peer (request.client.host) belongs to a trusted proxy CIDR
    (TRUSTED_PROXY_CIDRS env var), the first hop from X-Forwarded-For is used.
    Otherwise the direct peer address is returned as-is.

    Default: TRUSTED_PROXY_CIDRS is empty → X-FF headers are never trusted,
    preventing spoofing in bare-metal deployments without a known proxy.
    """
    peer_str = request.client.host if request.client else "unknown"
    proxies = _get_trusted_proxies()
    if proxies:
        try:
            peer_addr = ipaddress.ip_address(peer_str)
        except ValueError:
            return peer_str
        if any(peer_addr in net for net in proxies):
            xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if xff:
                return xff
    return peer_str


def record_login_attempt(
    *,
    identifier: str,
    success: bool,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """INSERT a row into login_attempts.

    Args:
        identifier: Username or IP address being tracked.
        success: True if login succeeded, False if failed.
        ip_address: Client IP string (stored as INET). May be None.
        user_agent: HTTP User-Agent header value. May be None.

    Never raises — failure is logged as a warning (best-effort audit trail).
    """
    try:
        from src.db.pg import auth_store

        with auth_store()._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO login_attempts (identifier, success, ip_address, user_agent)"
                    " VALUES (%s, %s, %s::inet, %s)",
                    (identifier, success, ip_address, user_agent),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("record_login_attempt failed (non-fatal): %s", exc)


def check_rate_limit(identifier: str, ip_address: str | None = None) -> bool:
    """Return True if the identifier or IP is currently rate-limited.

    Rate-limit window: last 15 minutes.
    Thresholds:
        - Per-username:  5 failures → locked
        - Per-IP:        20 failures → locked

    Only rows with success=FALSE are counted; successful logins are not cleared
    (audit-friendly) but do not count toward the threshold.

    Returns False (not limited) if the DB query fails — fail-open to avoid
    blocking legitimate users when the DB is temporarily unavailable.
    """
    try:
        from src.db.pg import auth_store

        pool = auth_store()._pool
        with pool.checkout() as conn:
            # Per-identifier (username) threshold
            row = pool.fetch_one(
                conn,
                "SELECT COUNT(*) AS cnt FROM login_attempts"
                " WHERE identifier = %s"
                "   AND attempted_at > NOW() - INTERVAL '%s minutes'"
                "   AND success = FALSE",
                (identifier, _RATE_WINDOW_MINUTES),
            )
            if row and row["cnt"] >= _RATE_MAX_FAILURES_PER_USER:
                return True

            # Per-IP threshold (only when ip_address provided)
            if ip_address:
                row_ip = pool.fetch_one(
                    conn,
                    "SELECT COUNT(*) AS cnt FROM login_attempts"
                    " WHERE ip_address = %s::inet"
                    "   AND attempted_at > NOW() - INTERVAL '%s minutes'"
                    "   AND success = FALSE",
                    (ip_address, _RATE_WINDOW_MINUTES),
                )
                if row_ip and row_ip["cnt"] >= _RATE_MAX_FAILURES_PER_IP:
                    return True

        return False
    except Exception as exc:
        logger.warning("check_rate_limit DB error (fail-open): %s", exc)
        return False
