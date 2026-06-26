"""Guard: the frontend must not embed PayPal server secrets or call PayPal directly.

Plan 2026-06-26 §11/D6. Unlike Paddle (which has a browser-safe client token),
PayPal carries NO public credential — the frontend only ever redirects to the
gateway-returned payer-action URL (www.paypal.com). So the server Client ID /
Secret / webhook id, any NEXT_PUBLIC_PAYPAL_* var, the PayPal REST API hosts, and
the OAuth endpoint must never appear in the client bundle.

Mirrors the Paddle leakage guard (test_paddle_frontend_no_leakage.py). Does NOT
hardcode any real secret value (that would itself be a leak into git).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend-next" / "src"
_SCANNED_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs"}

_FORBIDDEN_SUBSTRINGS: list[tuple[str, str]] = [
    ("AVT_PAYPAL_CLIENT_ID", "server client-id env name"),
    ("AVT_PAYPAL_SECRET", "server secret env name"),
    ("AVT_PAYPAL_WEBHOOK_ID", "server webhook-id env name"),
    ("NEXT_PUBLIC_PAYPAL", "there is no browser-safe PayPal token (unlike Paddle)"),
    ("api-m.paypal.com", "PayPal REST API host — server-only"),
    ("api-m.sandbox.paypal.com", "PayPal sandbox API host — server-only"),
    ("/v1/oauth2/token", "PayPal OAuth endpoint — server-only"),
    ("/v2/checkout/orders", "PayPal Orders API — server-only"),
]


def _frontend_files() -> list[Path]:
    if not _FRONTEND_SRC.exists():
        pytest.skip("frontend-next/src not present")
    return [p for p in _FRONTEND_SRC.rglob("*") if p.suffix in _SCANNED_SUFFIXES]


def test_frontend_has_no_paypal_secret_or_api_leakage():
    offenders: list[str] = []
    for path in _frontend_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle, label in _FORBIDDEN_SUBSTRINGS:
            if needle in text:
                offenders.append(f"{path}: contains {label} ({needle!r})")
    assert not offenders, (
        "PayPal secret / API leakage in frontend (the frontend only redirects to "
        "the gateway-returned payer-action URL — no PayPal credential or API call "
        "belongs client-side):\n" + "\n".join(offenders)
    )
