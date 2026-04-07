#!/usr/bin/env bash
# Post-deploy verification for Gateway container.
# Run on the remote host after deploying gateway files.
# Usage: bash verify-gateway-deploy.sh

set -euo pipefail

PASS=0
FAIL=0

check() {
  local desc="$1"
  local result="$2"
  if [ "$result" = "ok" ]; then
    echo "  ✅ $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $desc — $result"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Gateway Post-Deploy Verification ==="
echo ""

# 1. Container is running
STATUS=$(docker inspect -f '{{.State.Status}}' aivideotrans-gateway 2>/dev/null || echo "missing")
check "Container running" "$([ "$STATUS" = "running" ] && echo ok || echo "status=$STATUS")"

# 2. Live code path is /opt/gateway/
LIVE_PATH=$(docker inspect -f '{{.Config.WorkingDir}}' aivideotrans-gateway 2>/dev/null || echo "unknown")
check "Live code at /opt/gateway" "$([ "$LIVE_PATH" = "/opt/gateway" ] && echo ok || echo "workdir=$LIVE_PATH")"

# 3. main.py catch-all does NOT include POST or GET
CATCHALL_METHODS=$(docker exec aivideotrans-gateway python3 -c "
import sys; sys.path.insert(0,'/opt/gateway')
import main
for r in main.app.routes:
    p=getattr(r,'path','')
    if p=='/job-api/{path:path}':
        print(','.join(sorted(getattr(r,'methods',[]) or [])))
        break
" 2>/dev/null || echo "error")
CATCHALL_FAIL=""
echo "$CATCHALL_METHODS" | grep -q POST && CATCHALL_FAIL="POST in catch-all"
echo "$CATCHALL_METHODS" | grep -q GET && CATCHALL_FAIL="${CATCHALL_FAIL:+$CATCHALL_FAIL, }GET in catch-all"
check "catch-all excludes POST and GET" "$([ -z "$CATCHALL_FAIL" ] && echo ok || echo "FAIL: $CATCHALL_FAIL ($CATCHALL_METHODS)")"

# 4. POST /job-api/jobs -> intercept_create_job
CREATE_ENDPOINT=$(docker exec aivideotrans-gateway python3 -c "
import sys; sys.path.insert(0,'/opt/gateway')
import main
for r in main.app.routes:
    p=getattr(r,'path','')
    m=getattr(r,'methods',set()) or set()
    if p=='/job-api/jobs' and 'POST' in m:
        print(getattr(r.endpoint,'__name__','?'))
        break
" 2>/dev/null || echo "error")
check "POST /job-api/jobs -> intercept_create_job" "$([ "$CREATE_ENDPOINT" = "intercept_create_job" ] && echo ok || echo "endpoint=$CREATE_ENDPOINT")"

# 5. job_intercept.py has print diagnostics
PRINT_COUNT=$(docker exec aivideotrans-gateway grep -c 'GATEWAY' /opt/gateway/job_intercept.py 2>/dev/null || echo 0)
check "job_intercept.py has GATEWAY prints (>=10)" "$([ "$PRINT_COUNT" -ge 10 ] && echo ok || echo "count=$PRINT_COUNT")"

# 6. Gateway health check
HEALTH=$(curl -sf http://127.0.0.1:8880/gateway/health 2>/dev/null || echo "fail")
check "Health endpoint responds" "$(echo "$HEALTH" | grep -q ok && echo ok || echo "$HEALTH")"

# 7. admin_settings.py exists
ADMIN_EXISTS=$(docker exec aivideotrans-gateway test -f /opt/gateway/admin_settings.py && echo yes || echo no)
check "admin_settings.py present" "$([ "$ADMIN_EXISTS" = "yes" ] && echo ok || echo "missing")"

# -----------------------------------------------------------------------
# V3 Pilot Shadow Checks (added 2026-04-07)
# These verify the V3 shadow pilot routes and capabilities are deployed.
# V2 remains the production truth system — these checks are for shadow
# observability only.
# -----------------------------------------------------------------------
echo ""
echo "--- V3 Pilot Shadow Checks ---"

# 8. V3 credits routes registered
V3_ROUTES=$(docker exec aivideotrans-gateway python3 -c "
import sys; sys.path.insert(0,'/opt/gateway')
import main
routes = set()
for r in main.app.routes:
    p = getattr(r, 'path', '')
    if '/credits' in p or '/credit' in p:
        routes.add(p)
print('|'.join(sorted(routes)))
" 2>/dev/null || echo "error")

for expected_route in "/api/me/credits" "/api/me/credits-ledger" "/api/credits/estimate" "/api/admin/credits/summary"; do
    check "Route registered: $expected_route" "$(echo "$V3_ROUTES" | grep -qF "$expected_route" && echo ok || echo "not found in: $V3_ROUTES")"
done

# 9. V3 metering route registered
METERING_ROUTE=$(docker exec aivideotrans-gateway python3 -c "
import sys; sys.path.insert(0,'/opt/gateway')
import main
for r in main.app.routes:
    p = getattr(r, 'path', '')
    m = getattr(r, 'methods', set()) or set()
    if '/metering' in p and 'POST' in m:
        print(p)
        break
" 2>/dev/null || echo "none")
check "Route registered: POST .../metering" "$(echo "$METERING_ROUTE" | grep -q metering && echo ok || echo "not found")"

# 10. credits_observability.py present
CREDITS_OBS=$(docker exec aivideotrans-gateway test -f /opt/gateway/credits_observability.py && echo yes || echo no)
check "credits_observability.py present" "$([ "$CREDITS_OBS" = "yes" ] && echo ok || echo "missing")"

# 11. credits_service.py present
CREDITS_SVC=$(docker exec aivideotrans-gateway test -f /opt/gateway/credits_service.py && echo yes || echo no)
check "credits_service.py present" "$([ "$CREDITS_SVC" = "yes" ] && echo ok || echo "missing")"

# 12. Public estimate endpoint smoke check (no auth required)
ESTIMATE_RESP=$(curl -sf "http://127.0.0.1:8880/api/credits/estimate?minutes=1&service_mode=express&quality_tier=standard" 2>/dev/null || echo "fail")
ESTIMATE_OK=$(echo "$ESTIMATE_RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('ok' if 'estimated_credits' in d and d['estimated_credits'] == 10 else 'bad_value')
except: print('parse_error')
" 2>/dev/null || echo "parse_error")
check "GET /api/credits/estimate returns valid JSON" "$ESTIMATE_OK"

# 13. Protected routes return 401 without auth
for protected_route in "/api/me/credits" "/api/me/credits-ledger"; do
    HTTP_CODE=$(curl -sf -o /dev/null -w '%{http_code}' "http://127.0.0.1:8880${protected_route}" 2>/dev/null || echo "000")
    check "GET ${protected_route} returns 401 without auth" "$([ "$HTTP_CODE" = "401" ] && echo ok || echo "status=$HTTP_CODE")"
done

# 14. V3 DB tables exist (via Gateway container psql or Python)
V3_TABLES=$(docker exec aivideotrans-gateway python3 -c "
import sys; sys.path.insert(0,'/opt/gateway')
from models import CreditsBucket, CreditsLedger
print('credits_buckets=' + CreditsBucket.__tablename__)
print('credits_ledger=' + CreditsLedger.__tablename__)
" 2>/dev/null || echo "error")
check "V3 models importable (CreditsBucket, CreditsLedger)" "$(echo "$V3_TABLES" | grep -q credits_buckets && echo ok || echo "$V3_TABLES")"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && echo "✅ All checks passed." || echo "❌ Some checks failed."
exit "$FAIL"
