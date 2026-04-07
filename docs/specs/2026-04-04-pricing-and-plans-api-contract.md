# Pricing & Plans API Contract (v2 — frozen)

**Status:** frozen — updated after H1/H2 decision (2026-04-06)
**Source of truth:** `gateway/plan_catalog.py`
**Consumers:** `frontend-next/src/lib/billing/*`, marketing / pricing / trial / billing pages

This document defines the frontend ↔ gateway contract for plan catalog discovery.
The gateway is the sole authoritative source of truth. The frontend only consumes
these facts — it must not redefine prices, trial rules, or plan capabilities.

---

## 1. Purpose

- Give anonymous marketing / pricing pages a single, public endpoint to discover the
  currently-offered plan tiers and their prices.
- Provide frozen Trial configuration so the frontend can render concrete trial copy
  (days, minutes, Studio inclusion) without hardcoding values.
- Eliminate any drift between gateway truth and frontend display.

---

## 2. Endpoint

### `GET /api/plans`

| Property | Value |
|----------|-------|
| Method | `GET` |
| Path | `/api/plans` |
| Authentication | **None.** Public endpoint. Do not add `Depends(require_auth)`. |
| Request body | *(none)* |
| Query parameters | *(none)* |
| Response content-type | `application/json` |
| Caching | Safe to cache client-side for a short TTL (e.g. 5 minutes). Not sensitive. |

---

## 3. Response shape

```json
{
  "plans": [
    {
      "code": "free",
      "display_name": "Free",
      "max_duration_minutes": 10,
      "max_concurrent_jobs": 1,
      "allowed_service_modes": ["express"],
      "self_serve": false,
      "price_cny_fen": null,
      "free_quota_total": 5
    },
    {
      "code": "plus",
      "display_name": "Plus",
      "max_duration_minutes": 45,
      "max_concurrent_jobs": 3,
      "allowed_service_modes": ["express", "studio"],
      "self_serve": true,
      "price_cny_fen": {
        "monthly": 9900,
        "quarterly": 26900,
        "annual": 99900
      }
    },
    {
      "code": "pro",
      "display_name": "Pro",
      "max_duration_minutes": 180,
      "max_concurrent_jobs": 5,
      "allowed_service_modes": ["express", "studio"],
      "self_serve": true,
      "price_cny_fen": {
        "monthly": 29900,
        "quarterly": 79900,
        "annual": 299900
      }
    }
  ],
  "trial": {
    "frozen": true,
    "days": 7,
    "source_minutes": 20,
    "includes_studio": true,
    "phone_required": true,
    "auto_charge": false,
    "fallback_plan": "free",
    "notes": "Trial facts frozen 2026-04-06. 7 days, 20 source minutes, Studio included. ..."
  }
}
```

Prices are expressed in **CNY fen (分)**. Divide by 100 for display.

---

## 4. Field taxonomy

Every field below is labelled as either **display** (safe to show to end users) or
**business** (used for client-side gating or upgrade decisions) or both.

### `plans[].code` — **business + display**
Machine-readable plan identifier (`"free"`, `"plus"`, `"pro"`). Used as the key for
any per-plan logic on the client and for building upgrade requests.

### `plans[].display_name` — **display**
Human-readable name. Safe to show verbatim in UI.

### `plans[].max_duration_minutes` — **display + business**
Maximum video length per job. Drives "Up to N minutes" copy on pricing cards and
can be used to preemptively warn users before they upload a too-long video.
Final enforcement is still done server-side by `job_intercept.py`.

### `plans[].max_concurrent_jobs` — **display + business**
Maximum simultaneously-active jobs. Drives "Run N jobs in parallel" copy.
Final enforcement is server-side.

### `plans[].allowed_service_modes` — **display + business**
Subset of `["express", "studio"]`. Drives "Studio mode included" copy and the
"upgrade to unlock Studio" CTA.

### `plans[].self_serve` — **business**
`true` if the tier can be purchased via in-app checkout. `false` for tiers that
require manual handling (currently only `"free"`). The client should hide upgrade
CTAs for tiers where this is `false`.

### `plans[].price_cny_fen` — **display + business**
Price map in CNY fen, keyed by billing period. `null` for free-tier plans.
Individual period fields may also be `null` if that billing period is not offered.

Divide by 100 to format as yuan. Never treat a `null` period as a zero price.

### `plans[].free_quota_total` — **display + business**
Only present on the free tier. Total number of complimentary jobs granted to a
new account. Used as the numerator/denominator of the "X / Y free jobs used"
counter in the app shell.

### `trial.frozen` — **business**
Boolean gate. **Currently `true`** (frozen by H1 decision 2026-04-06). When
`frozen === true`, the numeric trial fields below are present and authoritative.
The client may render trial-specific copy (days, minutes, Studio inclusion) by
reading these fields.

### `trial.days` — **display + business**
Integer. Length of the trial in days. Currently `7`.

### `trial.source_minutes` — **display + business**
Integer. Source-video minute budget granted to trial users. Currently `20`.

### `trial.includes_studio` — **display**
Boolean. Whether the trial includes Studio mode. Currently `true`.

### `trial.phone_required` — **business**
Boolean. Whether phone verification is required to receive the trial. Currently `true`.

### `trial.auto_charge` — **display + business**
Boolean. Whether the trial auto-charges at expiry. Currently `false`. The frontend
should show "试用结束不会自动扣费" when this is false.

### `trial.fallback_plan` — **business**
String. The plan code the user falls back to after trial expires. Currently `"free"`.

### `trial.notes` — **display** (dev / contract readers only)
Human-readable summary of current frozen trial rules. Not intended for end users.

---

## 5. Boundary: what this endpoint does NOT do

- **No authentication.** This endpoint is explicitly public. Do not guard it.
- **No per-user state.** It returns catalog facts only. Current plan, trial status,
  and remaining quota for the logged-in user come from `/api/me/entitlements` and
  `/api/me/subscription`, which are separate, authenticated endpoints.
- **No subscription / invoice / payment data.** Those belong to the billing truth
  layer (`Task 4+`) and have their own contracts.
- **No pricing override per user / campaign.** If promotional pricing is ever
  needed, it must go through a new field or a new endpoint, not a silent override
  of `price_cny_fen`.

---

## 6. Versioning

- The URL path is unversioned (`/api/plans`). Breaking changes will require a
  new path (`/api/v2/plans`) or a migration window with both available.
- Additive changes (new optional fields) are allowed without a path change.
- Changes to frozen pricing or trial values must be accompanied by an update to
  this document and explicit project-owner re-approval.

---

## 7. Test coverage

- `tests/test_plan_catalog.py` asserts the module-level `PLANS` table, the helper
  functions, the `_build_plans_response` payload shape, and that
  `billing.PLAN_PRICES_CNY` / `job_intercept.PLAN_CATALOG` are derived from
  `plan_catalog` (no hardcoded fork). Trial frozen facts are asserted explicitly.
- `tests/test_billing.py` continues to exercise the checkout flow against the
  derived price table.
- `tests/test_gateway_create_job.py` / `tests/test_gateway_job_policy.py` continue
  to exercise the derived `PLAN_CATALOG` gate dict.
