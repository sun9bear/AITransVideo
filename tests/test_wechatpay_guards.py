"""Contract guards for the WeChat Pay Native integration (plan 2026-05-22 T10).

(a) merchant credentials never tracked by git;
(b) out_trade_no carries the AVT_ project prefix within the 32-char cap;
(c) the default notify_url points at this project's domain + webhook path
    (per-request injection invariant — the merchant-portal default is shared
    with AiPlay.video and must never be relied on);
(d) no merchant identifier / credential literal leaks into the frontend.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_gateway_dir = str(_REPO_ROOT / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from payment_provider_wechat import (  # noqa: E402
    DEFAULT_NOTIFY_URL,
    DEFAULT_ORDER_PREFIX,
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def test_no_wechat_credentials_tracked_by_git():
    tracked = _tracked_files()
    offenders = [
        f
        for f in tracked
        if f.startswith("config/wechatpay/")
        or "apiclient_key" in f
        or "apiclient_cert" in f
        or f.endswith(".p12")
    ]
    assert offenders == [], f"merchant credentials must never be committed: {offenders}"


def test_out_trade_no_prefix_and_cap():
    assert DEFAULT_ORDER_PREFIX == "AVT_"
    # AVT_ (4) + 28 uuid-hex chars == 32 == WeChat's hard cap.
    from payment_provider_wechat import _OUT_TRADE_NO_HEX_CHARS

    assert len(DEFAULT_ORDER_PREFIX) + _OUT_TRADE_NO_HEX_CHARS == 32


def test_default_notify_url_is_project_owned():
    assert DEFAULT_NOTIFY_URL.startswith("https://")
    assert "aitrans.video" in DEFAULT_NOTIFY_URL
    assert DEFAULT_NOTIFY_URL.endswith("/api/billing/webhooks/wechatpay")


def test_frontend_has_no_wechat_merchant_leakage():
    frontend_src = _REPO_ROOT / "frontend-next" / "src"
    forbidden = ("1745928268", "apiclient_key", "WECHATPAY_APIV3", "WECHATPAY_MCHID")
    offenders: list[str] = []
    for path in frontend_src.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path}:{token}")
    assert offenders == [], offenders
