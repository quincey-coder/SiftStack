"""Shim for Ty's Deal Room `reisift_auth` module.

The ported Ty agents (obituary_opportunity, buyer_sweep, post_walkthrough,
sms_agent, ...) do `from reisift_auth import get_headers`, expecting the
Deal Room clients directory on Ty's machine (DEALROOM_API_PATH). This shim
provides the same interface on top of OUR read-only DataSiftAPIClient JWT
auth (cached refresh token, else DATASIFT_EMAIL/DATASIFT_PASSWORD from .env).

Differences from Ty's original, on purpose:
  - REISIFT_ACCOUNT is ignored — this box has exactly one DataSift account.
    Ty's "datasift-apikey" account name resolves to it.
  - No Api-Key support: the internal API needs custom fields, which the Open
    API key does not expose, so everything rides the minted JWT.

Read-only policy note: this shim only hands out headers; the callers that use
it (the ranker etc.) issue GETs and method-override GETs. It grants no write
capability beyond what the caller already chooses to do.
"""

from __future__ import annotations

import base64
import json
import time

_client = None


def _access_expiry(token: str) -> float:
    """Best-effort exp claim from the JWT; 0 if undecodable (forces refresh)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


def _get_client():
    global _client
    if _client is None:
        from datasift_api_client import DataSiftAPIClient
        _client = DataSiftAPIClient.from_env()
    # Long pulls outlive the access token; refresh with a 90s safety margin.
    if _access_expiry(_client._access) - time.time() < 90 and _client._refresh:
        _client._do_refresh()
    return _client


def get_headers(content_type: str | None = None, method_override: str | None = None) -> dict:
    """Headers for one apiv2.reisift.io request, matching Ty's signature."""
    c = _get_client()
    hdrs = dict(c._session.headers)
    if content_type:
        hdrs["Content-Type"] = content_type
    if method_override:
        hdrs["X-HTTP-Method-Override"] = method_override
    return hdrs
