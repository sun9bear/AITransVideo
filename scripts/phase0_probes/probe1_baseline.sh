#!/usr/bin/env bash
# Phase 0 探针 ①: 当前 US 直连基线(无 Cloudflare Tunnel 前)
# 对齐方案 § 15.2
#
# 用法:
#   bash probe1_baseline.sh <运营商名>
# 例:
#   bash probe1_baseline.sh 电信
#
# 可覆盖环境变量:
#   APP_URL       默认 https://aitrans.video
#   SAMPLE_URL    默认 ${APP_URL}/probe/sample_100mb.bin
#   HEALTH_URL    默认 ${APP_URL}/gateway/health

set -uo pipefail

OPERATOR="${1:-unknown}"
APP_URL="${APP_URL:-https://aitrans.video}"
SAMPLE_URL="${SAMPLE_URL:-${APP_URL}/probe/sample_100mb.bin}"
HEALTH_URL="${HEALTH_URL:-${APP_URL}/gateway/health}"

if [ "$OPERATOR" = "unknown" ]; then
    echo "用法: bash probe1_baseline.sh <运营商名>"
    echo "     如: bash probe1_baseline.sh 电信"
    exit 1
fi

echo "==== Phase 0 探针 ① 当前 US 直连基线 ===="
echo "运营商: $OPERATOR"
echo "目标  : $APP_URL"
echo "样本  : $SAMPLE_URL"
echo "Health: $HEALTH_URL"
echo "日期  : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# ---------- 1. 首屏 HTML TTFB ----------
echo "--- 1. 首屏 HTML TTFB (5 次取最快) ---"
TTFB_VALUES=()
for i in $(seq 1 5); do
    V=$(curl -sL -o /dev/null -w "%{time_starttransfer}" --max-time 30 "$APP_URL" 2>/dev/null || echo "99")
    TTFB_VALUES+=("$V")
    printf "  第 %d 次: %ss\n" "$i" "$V"
done
TTFB_MIN=$(printf '%s\n' "${TTFB_VALUES[@]}" | sort -n | head -1)
TTFB_MS=$(awk -v v="$TTFB_MIN" 'BEGIN{printf "%d", v*1000}')
echo "  → 最快 TTFB: ${TTFB_MS} ms"

# ---------- 2. API P50 ----------
echo ""
echo "--- 2. API /gateway/health P50 (10 次) ---"
API_VALUES=()
for i in $(seq 1 10); do
    V=$(curl -sL -o /dev/null -w "%{time_total}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo "99")
    API_VALUES+=("$V")
    printf "  第 %d 次: %ss\n" "$i" "$V"
done
# 取排序后的第 5 位作为中位数(简化版 P50)
P50=$(printf '%s\n' "${API_VALUES[@]}" | sort -n | awk 'NR==5{print; exit}')
P50_MS=$(awk -v v="$P50" 'BEGIN{printf "%d", v*1000}')
echo "  → P50: ${P50_MS} ms"

# ---------- 3. 下载 100MB 速度 ----------
echo ""
echo "--- 3. 下载 100MB 样本速度 (1 次) ---"
DL=$(curl -sL -o /dev/null -w "%{speed_download}" --max-time 600 "$SAMPLE_URL" 2>/dev/null || echo "0")
DL_CODE=$(curl -sLI -o /dev/null -w "%{http_code}" --max-time 30 "$SAMPLE_URL" 2>/dev/null || echo "000")
DL_MB=$(awk -v v="$DL" 'BEGIN{printf "%.2f", v/1024/1024}')
echo "  → HTTP: $DL_CODE, 平均速度: ${DL_MB} MB/s"
if [ "$DL_CODE" != "200" ] && [ "$DL_CODE" != "206" ]; then
    echo "  ⚠ 样本 URL 不可达; 请项目方确认 $SAMPLE_URL 已放好"
fi

# ---------- 4. 可达性 ----------
echo ""
echo "--- 4. 访问可达性 ---"
STATUS=$(curl -sL -o /dev/null -w "%{http_code}" --max-time 10 "$APP_URL" 2>/dev/null || echo "000")
if [ "$STATUS" = "200" ] || [ "$STATUS" = "302" ] || [ "$STATUS" = "301" ]; then
    PROXY_STATUS="否 (直连可达 HTTP $STATUS)"
elif [ "$STATUS" = "000" ]; then
    PROXY_STATUS="是 (直连超时)"
else
    PROXY_STATUS="异常 (HTTP $STATUS)"
fi
echo "  → $PROXY_STATUS"

# ---------- 输出结果行 ----------
echo ""
echo "=========================================================="
echo "【贴到方案 § 15.2 表的这一行】"
echo ""
echo "| 中国${OPERATOR} | \`_填你的城市_\` | ${TTFB_MS} | ${P50_MS} | ${DL_MB} | ${PROXY_STATUS} |"
echo ""
echo "=========================================================="
