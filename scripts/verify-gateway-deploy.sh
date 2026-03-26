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

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && echo "✅ All checks passed." || echo "❌ Some checks failed."
exit "$FAIL"
