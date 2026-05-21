# CSRF Phase 1I: fake payment production gate

## Conclusion

Fake payment remains a dev/test checkout loop, not a production payment surface.

When `AVT_ENV=production`, the fake provider is non-operational unless `AVT_ENABLE_FAKE_PAYMENT=true` is set explicitly. This keeps the local browser checkout loop intact while preventing order-id-only fake settlement from being exposed by default in production.

## Behavior

- `create_order(... provider="fake")` is rejected by the existing provider operational gate in production.
- `POST /api/billing/fake-pay/{order_id}` returns 403 when fake payment is disabled.
- `GET /api/billing/fake-pay/{order_id}` redirects to `/settings/billing?status=error&reason=fake_payment_disabled` when fake payment is disabled.
- Dev/test behavior is unchanged: fake checkout remains enabled unless `AVT_ENV=production`.

## Why Not Session CSRF

Fake payment is a provider-style callback / browser handoff path, not a normal authenticated session write API. Applying session same-origin CSRF would not address the main production risk, because the endpoint settles by order id. The correct Phase 1 control is production disablement with an explicit smoke-test override.

## Regression Lock

- `tests/test_billing.py` covers provider operational state in production, explicit opt-in, create-order rejection, and direct fake-pay GET/POST gating.
- `tests/test_csrf_remaining_route_inventory.py` keeps fake-pay classified as a non-session-CSRF exception.
