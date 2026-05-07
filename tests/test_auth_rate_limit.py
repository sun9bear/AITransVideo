"""P1-10a-1 (audit 2026-05-07) regression: /auth/login rate limit +
X-Forwarded-For trusted-proxy boundary.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        S-HIGH-3 — /auth/login had no rate limit; credential stuffing
                   was wide open.
        S-HIGH-5 — _client_ip blindly trusted X-Forwarded-For from any
                   peer; attacker could spoof IP to bypass limits.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


# --------------------------------------------------------------------
# §1 — Login rate limit (in-memory state, no DB needed)
# --------------------------------------------------------------------


def _reset_rate_limit_state():
    """Clear in-memory state between tests so order doesn't matter."""
    import risk_control
    risk_control._login_ip_failures.clear()
    risk_control._login_account_failures.clear()


def test_check_login_allowed_passes_initially():
    _reset_rate_limit_state()
    from risk_control import check_login_allowed
    check_login_allowed("alice@example.com", "1.2.3.4")  # should not raise


def test_check_login_allowed_blocks_after_n_per_ip_failures():
    _reset_rate_limit_state()
    from risk_control import check_login_allowed, record_login_failure, RateLimitExceeded

    ip = "9.9.9.9"
    # Record 5 failures (limit) for the same IP across different accounts
    for i in range(5):
        record_login_failure(f"user{i}@example.com", ip)

    # Sixth attempt from same IP should be rate-limited even on a fresh account
    with pytest.raises(RateLimitExceeded) as exc_info:
        check_login_allowed("victim@example.com", ip)
    assert exc_info.value.scope == "login_ip"


def test_check_login_allowed_blocks_after_n_per_account_failures():
    _reset_rate_limit_state()
    from risk_control import check_login_allowed, record_login_failure, RateLimitExceeded

    account = "alice@example.com"
    # 5 failures from 5 different IPs all targeting the same account
    for i in range(5):
        record_login_failure(account, f"10.0.0.{i}")

    # Sixth attempt from a brand new IP should still be blocked per-account
    with pytest.raises(RateLimitExceeded) as exc_info:
        check_login_allowed(account, "10.0.0.99")
    assert exc_info.value.scope == "login_account"


# --------------------------------------------------------------------
# §2 — _client_ip trusted-proxy boundary
# --------------------------------------------------------------------


def _make_request(socket_host: str | None, headers: dict[str, str]):
    req = MagicMock()
    req.client = MagicMock()
    if socket_host is None:
        req.client = None
    else:
        req.client.host = socket_host
    # FastAPI Request.headers is a Headers object; MagicMock supports get()
    req.headers = headers
    return req


def test_client_ip_trusts_xff_when_peer_is_loopback():
    from auth_phone import _client_ip
    req = _make_request("127.0.0.1", {"x-forwarded-for": "203.0.113.5"})
    assert _client_ip(req) == "203.0.113.5"


def test_client_ip_ignores_xff_when_peer_is_not_loopback():
    """The bug we're fixing: previously this returned the spoofed XFF."""
    from auth_phone import _client_ip
    req = _make_request(
        "203.0.113.99",  # external attacker, not a trusted proxy
        {"x-forwarded-for": "1.2.3.4"},  # spoofed
    )
    assert _client_ip(req) == "203.0.113.99", (
        "P1-10a-1 regression: _client_ip honored X-Forwarded-For from an "
        "untrusted peer. This lets attackers bypass per-IP rate limit and "
        "IP-based trial eligibility checks."
    )


def test_client_ip_ignores_x_real_ip_when_peer_is_untrusted():
    from auth_phone import _client_ip
    req = _make_request("203.0.113.99", {"x-real-ip": "1.2.3.4"})
    assert _client_ip(req) == "203.0.113.99"


def test_client_ip_falls_back_to_socket_when_no_forwarded_header():
    from auth_phone import _client_ip
    req = _make_request("127.0.0.1", {})  # trusted peer, no XFF
    assert _client_ip(req) == "127.0.0.1"


def test_client_ip_handles_no_client():
    from auth_phone import _client_ip
    req = _make_request(None, {"x-forwarded-for": "1.2.3.4"})
    # No socket peer info, can't trust anything; XFF is ignored.
    assert _client_ip(req) is None
