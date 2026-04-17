#!/bin/bash
# Post-deploy verification for migration-debt batch (T1-T8 + fix commits).
# Checks each task's key symbol is actually present in the running containers.
set -u

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAILED=1; }

FAILED=0

echo "=== Gateway container symbol checks ==="
docker exec aivideotrans-gateway python -c "from startup_checks import validate_internal_api_key" 2>/dev/null \
    && pass "T4: startup_checks.validate_internal_api_key present" \
    || fail "T4: validate_internal_api_key MISSING"

docker exec aivideotrans-gateway python -c "from startup_checks import validate_production_safety" 2>/dev/null \
    && pass "T6: startup_checks.validate_production_safety present" \
    || fail "T6: validate_production_safety MISSING"

docker exec aivideotrans-gateway python -c "from config import resolve_database_url" 2>/dev/null \
    && pass "T3: config.resolve_database_url present" \
    || fail "T3: resolve_database_url MISSING"

docker exec aivideotrans-gateway python -c "from database import init_db" 2>/dev/null \
    && pass "T3: database.init_db present" \
    || fail "T3: init_db MISSING"

docker exec aivideotrans-gateway python -c "from job_intercept import _continue_with_gateway_lock" 2>/dev/null \
    && pass "T2: job_intercept._continue_with_gateway_lock present" \
    || fail "T2: _continue_with_gateway_lock MISSING"

docker exec aivideotrans-gateway python -c "import inspect; from credits_service import get_user_buckets; exit(0 if 'for_update' in inspect.getsource(get_user_buckets) else 1)" 2>/dev/null \
    && pass "T1: get_user_buckets has for_update param" \
    || fail "T1: for_update param MISSING"

echo
echo "=== App container symbol checks ==="
docker exec aivideotrans-app python -c "import inspect; from services.jobs.api import _build_job_api_handler; exit(0 if '_send_sanitized_error' in inspect.getsource(_build_job_api_handler) else 1)" 2>/dev/null \
    && pass "T5: _send_sanitized_error present in Job API" \
    || fail "T5: _send_sanitized_error MISSING"

docker exec aivideotrans-app python -c "import dataclasses; from services.tts.tts_generator import TTSResult; exit(0 if 'fallback_used_provider' in [f.name for f in dataclasses.fields(TTSResult)] else 1)" 2>/dev/null \
    && pass "T7: TTSResult.fallback_used_provider present" \
    || fail "T7: fallback_used_provider MISSING"

docker exec aivideotrans-app python -c "import dataclasses; from services.gemini.translator import DubbingSegment; exit(0 if 'fallback_used_provider' in [f.name for f in dataclasses.fields(DubbingSegment)] else 1)" 2>/dev/null \
    && pass "T7: DubbingSegment.fallback_used_provider present" \
    || fail "T7: DubbingSegment field MISSING"

echo
echo "=== Caddy config check ==="
docker exec aivideotrans-caddy grep -q '@internal_block' /etc/caddy/Caddyfile \
    && pass "T4: Caddy @internal_block rule present" \
    || fail "T4: Caddy rule MISSING"

echo
echo "=== Gateway auth.py cookie samesite ==="
docker exec aivideotrans-gateway grep -q 'samesite="strict"' /app/auth.py \
    && pass "T8: cookie samesite=strict" \
    || fail "T8: samesite not strict"

echo
echo "=== Container status ==="
docker ps --format "  {{.Names}}  {{.Status}}" | grep aivideotrans

echo
if [ "$FAILED" = "1" ]; then
    echo "=== DEPLOY VERIFICATION FAILED ==="
    exit 1
else
    echo "=== ALL CHECKS PASSED ==="
fi
