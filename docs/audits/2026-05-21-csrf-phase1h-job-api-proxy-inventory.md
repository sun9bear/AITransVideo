# CSRF Phase 1H: `/job-api/{path:path}` proxy inventory

## Conclusion

The non-jobs Job API catch-all is now covered by `require_same_origin_state_change`.

The helper is intentionally attached at route level because it is a no-op for `GET`, `HEAD`, and `OPTIONS`; it only blocks cross-site `PUT`, `PATCH`, and `DELETE` requests.

## Inventory

Observed callers and references:

- `GET /job-api/voice-library` remains the only explicit non-jobs global proxy use found in route coverage and Job API tests.
- Frontend `apiClient` only exposes `get` and `post`; its default `/job-api` writes are jobs-scoped.
- Frontend `DELETE /job-api/jobs/{id}` is handled by the dedicated job delete intercept route, not the catch-all.
- User voice-library writes now go through `/gateway/user-voices`, not `/job-api/voice-library`.
- Pipeline callbacks use `/job-api/jobs/{id}/source-metadata` and `/metering`; both remain internal-key routes and are not part of the catch-all.

## Decision

Apply the same-origin helper to `/job-api/{path:path}` instead of splitting the route now.

This keeps the existing transparent proxy behavior for reads and preflight while closing the remaining session-authenticated browser write surface.

## Regression lock

- `tests/test_user_csrf_guard_wiring.py` now asserts `PUT`, `PATCH`, and `DELETE /job-api/{path:path}` carry the CSRF dependency.
- `tests/test_csrf_remaining_route_inventory.py` no longer lists the catch-all as an unguarded state-changing route.
