# CSRF Phase 1J: closure re-audit report

## Conclusion

Phase 1 is closed for browser session CSRF coverage.

Ordinary authenticated browser write routes are now guarded by `require_same_origin_state_change`. The remaining unguarded state-changing routes are explicitly non-session-CSRF surfaces: payment provider callbacks, fake payment test callbacks with a production gate, and internal service callbacks protected by internal-key / loopback boundaries.

## Completed Scope

| Area | Final treatment |
| --- | --- |
| Admin settings / admin disk / admin support / admin traffic / admin jobs / pricing | Same-origin guard on state-changing admin routes |
| Auth/session/account writes | Same-origin guard on register, login, logout, password change, bind-email |
| Upload and job writes | Same-origin guard on upload, create/delete/rename, job subresource POST, job proxy PUT/PATCH/DELETE |
| Voice/user-visible writes | Same-origin guard on user voice routes, voice selection, notifications, support visitor-cookie writes |
| Billing order creation | Same-origin guard on `POST /api/billing/orders` |
| Fake payment | Production-disabled by default via `AVT_ENV=production` + `AVT_ENABLE_FAKE_PAYMENT` gate |
| Remaining route inventory | Locked by `tests/test_csrf_remaining_route_inventory.py` |

## Remaining Explicit Exceptions

| Endpoint | Classification | Reason |
| --- | --- | --- |
| `POST /api/billing/fake-pay/{order_id}` | Fake provider callback | Not a normal session write API; production default is disabled, explicit smoke-test opt-in only |
| `POST /api/billing/webhooks/{provider_name}` | Payment provider webhook | Must use provider signature, payload validation, event idempotency; same-origin would reject legitimate provider calls |
| `POST /api/internal/user-voices/*` | Internal service API | Protected by internal key / loopback / deployment ingress boundary |
| `POST /internal/notifications/dispatch` | Internal service API | Protected by `X-Internal-Key` |
| `POST /job-api/jobs/{job_id}/source-metadata` | Pipeline callback | Protected by `_require_internal_access` |
| `POST /job-api/jobs/{job_id}/metering` | Pipeline callback | Protected by `_require_internal_access` |

## Regression Locks

- `tests/test_csrf_remaining_route_inventory.py` asserts the only unguarded state-changing routes are the explicit exceptions above.
- `tests/test_user_csrf_guard_wiring.py` asserts direct browser-facing writes carry the same-origin dependency and that callback/internal routers stay exempt from session CSRF.
- `tests/test_admin_csrf_guard_wiring.py` asserts admin write routers remain guarded.
- `tests/test_support_csrf_guard_wiring.py` asserts support visitor-cookie write routes remain guarded.
- `tests/test_billing.py` asserts billing order CSRF guard and fake payment production gating.
- Internal callback tests assert the `X-Internal-Key` boundary and pipeline callback header forwarding.

## Residual Risk Moved To Phase 2

1. Payment webhook deep audit: provider signature coverage, replay/idempotency, provider-specific payload validation, and production observability.
2. Internal callback ingress audit: confirm public Caddy/Cloudflare routing still blocks `/api/internal/*` and internal-only callback paths in deployed topology.
3. GET side-effect prevention guardrail: keep fake payment documented as the explicit exception and avoid adding new state-changing GET routes.

## Closure Criteria

Phase 1 closure is valid while all of the following remain true:

- `tests/test_csrf_remaining_route_inventory.py` passes.
- Fake payment remains disabled by default in production.
- Payment webhooks remain provider-authenticated rather than session-CSRF guarded.
- Internal callbacks remain protected by internal-key / loopback / ingress controls.
