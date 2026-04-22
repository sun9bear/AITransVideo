#!/usr/bin/env bash
# Phase 0 探针 ②: R2 原生域名下载稳定性
# 对齐方案 § 15.3
#
# 用法:
#   export PRESIGNED_URL='<项目方用 generate_download_url.py 签好的 URL>'
#   bash probe2_r2_download.sh <运营商名>
# 例:
#   bash probe2_r2_download.sh 电信

set -uo pipefail

OPERATOR="${1:-unknown}"
URL="${PRESIGNED_URL:-}"
ROUNDS="${ROUNDS:-5}"

if [ "$OPERATOR" = "unknown" ]; then
    echo "用法: bash probe2_r2_download.sh <运营商名>"
    echo "     如: bash probe2_r2_download.sh 电信"
    exit 1
fi

if [ -z "$URL" ]; then
    echo "错误: 需要设置 PRESIGNED_URL 环境变量"
    echo ""
    echo "项目方用 generate_download_url.py 签好一个 URL 发给你, 然后:"
    echo "  export PRESIGNED_URL='https://<account>.r2.cloudflarestorage.com/...'"
    echo "  bash probe2_r2_download.sh $OPERATOR"
    exit 1
fi

echo "==== Phase 0 探针 ② R2 原生域名下载稳定性 ===="
echo "运营商: $OPERATOR"
echo "URL   : $(echo "$URL" | cut -c1-80)..."
echo "次数  : $ROUNDS"
echo "日期  : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

SPEEDS=()
SUCCESS=0
RST_COUNT=0
HTTP_FAIL=0

for i in $(seq 1 "$ROUNDS"); do
    echo "--- 第 $i / $ROUNDS 次 ---"
    # 执行 curl, 分别提取 http_code 和 speed_download
    RESULT=$(curl -sL -o /dev/null \
        -w "HTTPCODE=%{http_code} SPEED=%{speed_download} TIME=%{time_total}" \
        --max-time 300 \
        "$URL" 2>&1 || true)

    CODE=$(echo "$RESULT" | grep -oE 'HTTPCODE=[0-9]+' | cut -d= -f2)
    SPEED=$(echo "$RESULT" | grep -oE 'SPEED=[0-9.]+' | cut -d= -f2)
    TIME=$(echo "$RESULT" | grep -oE 'TIME=[0-9.]+' | cut -d= -f2)

    CODE="${CODE:-000}"
    SPEED="${SPEED:-0}"
    SPEED_MB=$(awk -v v="$SPEED" 'BEGIN{printf "%.2f", v/1024/1024}')

    if [ "$CODE" = "200" ] || [ "$CODE" = "206" ]; then
        SUCCESS=$((SUCCESS + 1))
        SPEEDS+=("$SPEED_MB")
        printf "  ✓ HTTP %s, 速度 %s MB/s, 耗时 %ss\n" "$CODE" "$SPEED_MB" "$TIME"
    elif [ "$CODE" = "000" ]; then
        RST_COUNT=$((RST_COUNT + 1))
        echo "  ✗ 连接超时 / RST (CODE=000)"
    else
        HTTP_FAIL=$((HTTP_FAIL + 1))
        printf "  ✗ HTTP %s (可能是 URL 过期)\n" "$CODE"
    fi

    [ "$i" -lt "$ROUNDS" ] && sleep 2
done

# ---------- 统计 ----------
if [ ${#SPEEDS[@]} -gt 0 ]; then
    AVG=$(printf '%s\n' "${SPEEDS[@]}" | awk '{s+=$1}END{printf "%.2f", s/NR}')
    MIN=$(printf '%s\n' "${SPEEDS[@]}" | sort -n | head -1)
    MAX=$(printf '%s\n' "${SPEEDS[@]}" | sort -n | tail -1)
else
    AVG="0.00" ; MIN="0.00" ; MAX="0.00"
fi

NOTES=""
if [ "$RST_COUNT" -gt 0 ]; then NOTES="${NOTES}RST×${RST_COUNT} "; fi
if [ "$HTTP_FAIL" -gt 0 ]; then NOTES="${NOTES}HTTP 失败×${HTTP_FAIL} "; fi
[ -z "$NOTES" ] && NOTES="正常"

echo ""
echo "--- 汇总 ---"
echo "  成功: ${SUCCESS} / ${ROUNDS}"
echo "  平均速度: ${AVG} MB/s  (min ${MIN}, max ${MAX})"
echo "  RST: ${RST_COUNT}"
echo ""

# ---------- 输出结果行 ----------
echo "=========================================================="
echo "【贴到方案 § 15.3 表的这一行】"
echo ""
echo "| 中国${OPERATOR} | ${AVG} | ${MIN} | ${MAX} | ${SUCCESS}/${ROUNDS} | ${RST_COUNT} | ${NOTES} |"
echo ""
echo "=========================================================="

# ---------- 放行判据提醒 ----------
if [ "$(awk -v a="$AVG" 'BEGIN{print (a < 1)}')" = "1" ] || [ "$SUCCESS" -lt $(( (ROUNDS * 9 + 9) / 10 )) ]; then
    echo "⚠ 此运营商数据不达标(平均 < 1 MB/s 或 成功率 < 90%)"
    echo "  按方案 D39,MVP 上线后需启动 Phase 2b 备胎方案"
else
    echo "✓ 此运营商数据达标"
fi
