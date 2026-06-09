"""Guard: the frontend must not embed Paddle server secrets or internal price ids.

Plan 2026-06-08 §8 / §12. The ONLY Paddle value allowed in the frontend is the
client-side token via NEXT_PUBLIC_PADDLE_CLIENT_TOKEN (Paddle designs it to be
public/browser-safe). The server API key, webhook signing secret, and the
internal price ids must never reach the client bundle — the backend maps
(plan, period) -> price id server-side.

Mirrors the R2 leakage guard in test_phase2_download_backend.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend-next" / "src"
_SCANNED_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs"}

# Literal substrings that must never appear in frontend source.
_FORBIDDEN_SUBSTRINGS: list[tuple[str, str]] = [
    ("pdl_live_", "Paddle live API key prefix"),
    ("pdl_sdbx_", "Paddle sandbox API key prefix"),
    ("pdl_ntfset_", "Paddle webhook signing secret prefix"),
    ("AVT_PADDLE_API_KEY", "server API key env name"),
    ("AVT_PADDLE_WEBHOOK_SECRET", "webhook secret env name"),
]

# Hardcoded Paddle price ids (pri_<long id>). The frontend must never carry
# these; only the gateway knows the (plan, period) -> price id mapping.
_PRICE_ID_RE = re.compile(r"pri_[0-9A-Za-z]{20,}")


def _frontend_files() -> list[Path]:
    if not _FRONTEND_SRC.exists():
        pytest.skip("frontend-next/src not present")
    return [p for p in _FRONTEND_SRC.rglob("*") if p.suffix in _SCANNED_SUFFIXES]


def test_frontend_has_no_paddle_secret_or_price_leakage():
    offenders: list[str] = []
    for path in _frontend_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle, label in _FORBIDDEN_SUBSTRINGS:
            if needle in text:
                offenders.append(f"{path}: contains {label} ({needle!r})")
        for match in _PRICE_ID_RE.finditer(text):
            offenders.append(f"{path}: hardcoded Paddle price id {match.group(0)!r}")
    assert not offenders, (
        "Paddle secret / price-id leakage in frontend (only "
        "NEXT_PUBLIC_PADDLE_CLIENT_TOKEN is allowed):\n" + "\n".join(offenders)
    )
