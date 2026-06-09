#!/usr/bin/env python3
"""Read-only Paddle P0 readiness verifier (plan 2026-06-08, P0 gate).

Confirms the Paddle dashboard configuration the integration depends on,
WITHOUT making any write call and WITHOUT touching any paid API. Paddle
Billing API reads (GET /prices, GET /notification-settings) are free and,
in the recommended sandbox mode, hit test data only.

What this script verifies against the Paddle Billing API:
  - the 6 one-time CNY prices exist and their amounts match plan_catalog
    (the single source of truth — gateway/plan_catalog.py)
  - a notification (webhook) destination points at the gateway webhook URL,
    is active, subscribes to the settlement event, and has a signing secret

What the Billing API does NOT expose (printed as a manual dashboard
checklist instead of being silently passed):
  - Alipay / WeChat Pay enablement
  - approved Paddle.js / default-payment-link domain
  - the Paddle.js client-side token
  - transaction.write scope on the API key (a read-only verifier must not
    create a transaction to probe it)

This is an operator tool. It is intentionally NOT imported by the gateway
and NOT wired into any startup / sweeper / fallback path — run it by hand.

Usage:
    # put AVT_PADDLE_API_KEY (sandbox key, pdl_sdbx_...) in .env, then:
    python scripts/paddle_p0_verify.py

Environment (read from process env first, then repo-root .env):
    AVT_PADDLE_API_KEY    required. Sandbox key recommended for P0 (R9).
    AVT_PADDLE_ENV        sandbox | production. Default: sandbox.
    AVT_PADDLE_NOTIFY_URL optional. Webhook URL to look for. Default is the
                          production gateway webhook URL.
    AVT_PADDLE_PRICE_{PLUS,PRO}_{M,Q,A}  optional. If set, the matching price
                          is verified by id; if unset, it is discovered by
                          amount and the id is printed for you to paste into
                          config (this bootstraps the P1 price mapping).

Exit code: 0 iff every API-verifiable item passed. Manual-check items never
fail the exit code (the API cannot confirm them) but are always listed.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NOTIFY_URL = "https://aitrans.video/api/billing/webhooks/paddle"
SANDBOX_BASE = "https://sandbox-api.paddle.com"
PRODUCTION_BASE = "https://api.paddle.com"

# The gateway settles on either of these (plan §7.3). At least one MUST be
# subscribed or webhook-driven settlement can never fire.
SETTLEMENT_EVENTS = ("transaction.completed", "transaction.paid")
# Recommended but non-blocking. NOTE: Paddle Billing models refunds as
# *adjustments* (adjustment.created), NOT a "transaction.refunded" event —
# the plan's §7.4 wording predates this and must be corrected in P1 (R7).
RECOMMENDED_EVENTS = ("transaction.updated", "adjustment.created")

# (plan_code, billing_period) -> env var suffix letter, per plan §9.
_PERIOD_LETTER = {"monthly": "M", "quarterly": "Q", "annual": "A"}

# Used only if plan_catalog cannot be imported standalone. Mirrors the frozen
# table in gateway/plan_catalog.py; a warning is printed if this path is hit so
# any drift from the live table stays visible.
_FROZEN_EXPECTED: dict[tuple[str, str], int] = {
    ("plus", "monthly"): 9900,
    ("plus", "quarterly"): 26900,
    ("plus", "annual"): 99900,
    ("pro", "monthly"): 29900,
    ("pro", "quarterly"): 79900,
    ("pro", "annual"): 299900,
}


@dataclass(frozen=True)
class PriceRow:
    status: str  # PASS | FAIL | WARN
    plan: str
    period: str
    fen: int
    price_id: str
    detail: str


# --- env loading ---------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    """Merge KEY=VALUE lines from ``path`` into os.environ without overriding.

    Tiny zero-dependency parser so the operator can drop the key in .env
    instead of exporting it. Real process env always wins.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _load_expected_prices() -> dict[tuple[str, str], int]:
    """6 priced (plan, period) -> CNY fen, from plan_catalog (truth source)."""
    sys.path.insert(0, str(REPO_ROOT / "gateway"))
    try:
        from plan_catalog import get_legacy_price_table

        return dict(get_legacy_price_table())
    except Exception as exc:  # standalone import may miss fastapi etc.
        print(
            f"  WARN: 无法导入 plan_catalog ({exc}); 改用冻结快照核对",
            file=sys.stderr,
        )
        return dict(_FROZEN_EXPECTED)


def _price_env_var(plan: str, period: str) -> str:
    return f"AVT_PADDLE_PRICE_{plan.upper()}_{_PERIOD_LETTER[period]}"


# --- Paddle HTTP (read-only) ---------------------------------------------


def _make_client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(20.0),
    )


def _get(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    resp = client.get(url, params=params)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"认证失败（{resp.status_code}）：API key 无效或缺少 read 权限"
            "（需 price.read + notification_setting.read），或 key 与"
            " AVT_PADDLE_ENV 不匹配。"
        )
    resp.raise_for_status()
    return resp.json()


def _list_active_prices(client: httpx.Client) -> list[dict]:
    """All active prices, following meta.pagination.next (capped)."""
    prices: list[dict] = []
    url: str = "/prices"
    params: dict | None = {"status": "active", "per_page": "200"}
    for _ in range(20):  # 20 * 200 = 4000 prices, far beyond 6
        body = _get(client, url, params=params)
        prices.extend(body.get("data", []))
        pagination = (body.get("meta") or {}).get("pagination") or {}
        if not pagination.get("has_more"):
            break
        url = pagination.get("next") or ""
        params = None  # the next URL already carries the cursor
        if not url:
            break
    return prices


def _fetch_price_by_id(client: httpx.Client, price_id: str) -> dict | None:
    resp = client.get(f"/prices/{price_id}")
    if resp.status_code == 404:
        return None
    if resp.status_code in (401, 403):
        raise SystemExit(f"认证失败（{resp.status_code}）：核对 {price_id} 时 key 无权限")
    resp.raise_for_status()
    return resp.json().get("data")


# --- checks --------------------------------------------------------------


def _verify_price_by_id(
    client: httpx.Client, plan: str, period: str, fen: int, price_id: str
) -> PriceRow:
    price = _fetch_price_by_id(client, price_id)
    if price is None:
        return PriceRow("FAIL", plan, period, fen, price_id, f"{price_id} 不存在")
    unit = price.get("unit_price") or {}
    problems: list[str] = []
    if price.get("status") != "active":
        problems.append(f"status={price.get('status')}")
    if unit.get("currency_code") != "CNY":
        problems.append(f"currency={unit.get('currency_code')}")
    if price.get("billing_cycle") is not None:
        problems.append("recurring（应为 one-time）")
    if unit.get("amount") != str(fen):
        problems.append(f"amount={unit.get('amount')} 应为 {fen}")
    if problems:
        return PriceRow("FAIL", plan, period, fen, price_id, "; ".join(problems))
    return PriceRow("PASS", plan, period, fen, price_id, "verified by id")


def _discover_price(
    plan: str, period: str, fen: int, matches: list[dict]
) -> PriceRow:
    if not matches:
        return PriceRow(
            "FAIL", plan, period, fen, "", f"未找到金额={fen} 的一次性 CNY price"
        )
    if len(matches) > 1:
        ids = ",".join(m.get("id", "") for m in matches)
        return PriceRow(
            "WARN", plan, period, fen, matches[0].get("id", ""),
            f"金额={fen} 命中多个 price: {ids}",
        )
    return PriceRow(
        "PASS", plan, period, fen, matches[0].get("id", ""), "discovered by amount"
    )


def _check_prices(
    client: httpx.Client, expected: dict[tuple[str, str], int]
) -> tuple[list[PriceRow], dict[str, str], list[dict]]:
    all_prices = _list_active_prices(client)
    cny_one_time = [
        p
        for p in all_prices
        if (p.get("unit_price") or {}).get("currency_code") == "CNY"
        and p.get("billing_cycle") is None
    ]
    cny_recurring = [
        p
        for p in all_prices
        if (p.get("unit_price") or {}).get("currency_code") == "CNY"
        and p.get("billing_cycle") is not None
    ]
    by_amount: dict[str, list[dict]] = {}
    for price in cny_one_time:
        amount = (price.get("unit_price") or {}).get("amount", "")
        by_amount.setdefault(amount, []).append(price)

    rows: list[PriceRow] = []
    discovered_env: dict[str, str] = {}
    for (plan, period), fen in sorted(expected.items()):
        env_var = _price_env_var(plan, period)
        configured_id = os.environ.get(env_var, "").strip()
        if configured_id:
            row = _verify_price_by_id(client, plan, period, fen, configured_id)
        else:
            row = _discover_price(plan, period, fen, by_amount.get(str(fen), []))
            if row.status == "PASS":
                discovered_env[env_var] = row.price_id
        rows.append(row)
    return rows, discovered_env, cny_recurring


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _check_webhook(client: httpx.Client, notify_url: str) -> tuple[str, list[str]]:
    body = _get(client, "/notification-settings")
    url_settings = [s for s in body.get("data", []) if s.get("type") == "url"]
    target = _norm_url(notify_url)
    match = next(
        (s for s in url_settings if _norm_url(s.get("destination", "")) == target),
        None,
    )
    if match is None:
        existing = ", ".join(s.get("destination", "") for s in url_settings)
        return "FAIL", [
            f"FAIL: 无 url destination 命中 {notify_url}",
            f"  现有 url destinations: {existing or '(无)'}",
        ]

    notes: list[str] = []
    hard_fail = False
    events = {e.get("name") for e in match.get("subscribed_events", [])}
    notes.append(f"命中 destination; events={sorted(e for e in events if e)}")
    if not match.get("active"):
        hard_fail = True
        notes.append("FAIL: destination active=false (未启用投递)")
    if not (set(SETTLEMENT_EVENTS) & events):
        hard_fail = True
        notes.append("FAIL: 未订阅 transaction.completed / transaction.paid (结算事件,必须)")
    for event in RECOMMENDED_EVENTS:
        if event not in events:
            notes.append(f"WARN: 未订阅 {event} (建议; adjustment.created 用于退款 R7)")
    if match.get("endpoint_secret_key"):
        notes.append("OK: secret 已配置 (去后台复制到 AVT_PADDLE_WEBHOOK_SECRET)")
    else:
        hard_fail = True
        notes.append("FAIL: 无 endpoint_secret_key")
    return ("FAIL" if hard_fail else "PASS"), notes


# --- reporting -----------------------------------------------------------

_MARK = {"PASS": "[OK]", "FAIL": "[FAIL]", "WARN": "[!]"}


def _print_prices(rows: list[PriceRow], cny_recurring: list[dict]) -> None:
    print("\n[价格] 6 个一次性 CNY price (真源 gateway/plan_catalog.py)")
    for r in rows:
        mark = _MARK.get(r.status, "[?]")
        print(
            f"  {mark:<6} {r.status:<4} {r.plan}/{r.period:<9} "
            f"{r.fen / 100:>5.0f}元 {r.fen:<7} {r.price_id or '-':<34} {r.detail}"
        )
    if cny_recurring:
        ids = ", ".join(p.get("id", "") for p in cny_recurring)
        print(f"  [!] 发现 CNY 循环订阅 price (D5 要求一次性,请核对): {ids}")


def _print_webhook(status: str, notes: list[str], notify_url: str) -> None:
    print(f"\n[Webhook] notification destination -> {notify_url}")
    print(f"  {_MARK.get(status, '?')} {status}")
    for note in notes:
        print(f"     {note}")


def _print_manual() -> None:
    print("\n[需在 Paddle 后台人工确认 — Billing API 不暴露这些]")
    items = [
        ("Alipay 已 approved 并开启", "Paddle > Checkout settings > Payment methods"),
        ("WeChat Pay 已开启", "同上"),
        (
            "payment-link / 默认结账域名 aitrans.video 已 approved",
            "Paddle.js > Approved domains（或 Checkout > Default payment link）",
        ),
        (
            "API key 具备 transaction read+write",
            "只读脚本仅验证了 read；write 留到 P1 创建交易时验证",
        ),
        (
            "Paddle.js client-side token（AVT_PADDLE_CLIENT_TOKEN）已取",
            "Developer tools > Authentication > Client-side tokens",
        ),
    ]
    for label, where in items:
        print(f"  [ ] {label}\n       -> {where}")


def _print_discovered_env(discovered: dict[str, str]) -> None:
    if not discovered:
        return
    print("\n[发现的 price id — 可粘进 config/.env（P1 价格映射）]")
    for env_var in sorted(discovered):
        print(f"  {env_var}={discovered[env_var]}")


# --- main ----------------------------------------------------------------


def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")

    api_key = os.environ.get("AVT_PADDLE_API_KEY", "").strip()
    if not api_key:
        print(
            "FAIL: 缺少 AVT_PADDLE_API_KEY。把 Sandbox key (pdl_sdbx_... 开头) 写进"
            f" {REPO_ROOT / '.env'} 或 export 后重跑。",
            file=sys.stderr,
        )
        return 2

    env = (os.environ.get("AVT_PADDLE_ENV", "sandbox") or "sandbox").strip().lower()
    base_url = PRODUCTION_BASE if env == "production" else SANDBOX_BASE
    notify_url = os.environ.get("AVT_PADDLE_NOTIFY_URL", "").strip() or DEFAULT_NOTIFY_URL

    # Common footgun: key/env mismatch. Warn loudly but let the API be the judge.
    if env == "sandbox" and api_key.startswith("pdl_live_"):
        print("  WARN: AVT_PADDLE_ENV=sandbox 但 key 是 live key, 大概率会 403。", file=sys.stderr)
    if env == "production" and api_key.startswith("pdl_sdbx_"):
        print("  WARN: AVT_PADDLE_ENV=production 但 key 是 sandbox key, 大概率会 403。", file=sys.stderr)

    print(f"==== Paddle P0 只读核对（env={env}, base={base_url}）====")

    expected = _load_expected_prices()
    with _make_client(base_url, api_key) as client:
        price_rows, discovered_env, cny_recurring = _check_prices(client, expected)
        webhook_status, webhook_notes = _check_webhook(client, notify_url)

    _print_prices(price_rows, cny_recurring)
    _print_webhook(webhook_status, webhook_notes, notify_url)
    _print_manual()
    _print_discovered_env(discovered_env)

    price_fail = sum(1 for r in price_rows if r.status == "FAIL")
    price_warn = sum(1 for r in price_rows if r.status == "WARN")
    api_ok = price_fail == 0 and webhook_status != "FAIL"

    print("\n==== 结论 ====")
    print(
        f"  价格: {len(price_rows) - price_fail - price_warn}/{len(price_rows)} PASS"
        f"（FAIL {price_fail}, WARN {price_warn}）；Webhook: {webhook_status}"
    )
    if api_ok:
        print("  [OK] API 可验证项全部通过。仍需在后台逐项确认上面的人工核对项后, P0 才算闭环。")
    else:
        print("  [FAIL] 有 API 可验证项未通过, 见上。修好后重跑本脚本。")
    return 0 if api_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
