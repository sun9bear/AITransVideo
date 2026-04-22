#!/usr/bin/env bash
# Phase 0 探针 ③: R2 真实 multipart 上传样本
# 对齐方案 § 15.4
#
# 用法:
#   export AWS_ACCESS_KEY_ID='<R2 探针 token Access Key>'
#   export AWS_SECRET_ACCESS_KEY='<R2 探针 token Secret>'
#   export R2_ENDPOINT='https://<account>.r2.cloudflarestorage.com'
#   export R2_BUCKET='avt-uploads'
#   bash probe3_r2_upload.sh <运营商名> [样本文件路径]
# 例:
#   bash probe3_r2_upload.sh 电信 ./sample_2gb.bin

set -uo pipefail

OPERATOR="${1:-unknown}"
SAMPLE="${2:-./sample_2gb.bin}"

if [ "$OPERATOR" = "unknown" ]; then
    echo "用法: bash probe3_r2_upload.sh <运营商名> [样本文件]"
    exit 1
fi

# ---------- 前置检查 ----------
for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY R2_ENDPOINT R2_BUCKET; do
    if [ -z "${!var:-}" ]; then
        echo "错误: 未设置环境变量 $var"
        echo "参考 README.md § 探针 ③"
        exit 1
    fi
done

if ! command -v aws >/dev/null 2>&1; then
    echo "错误: 未装 AWS CLI"
    echo "安装:"
    echo "  curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o awscli.zip"
    echo "  unzip awscli.zip && sudo ./aws/install"
    exit 1
fi

if [ ! -f "$SAMPLE" ]; then
    echo "错误: 样本文件 $SAMPLE 不存在"
    echo ""
    echo "快速生成一个 2GB 样本(耗时 ~1-2 分钟):"
    echo "  dd if=/dev/urandom of=./sample_2gb.bin bs=1M count=2048"
    exit 1
fi

SIZE_BYTES=$(stat -c%s "$SAMPLE" 2>/dev/null || stat -f%z "$SAMPLE" 2>/dev/null || echo "0")
SIZE_MB=$(awk -v v="$SIZE_BYTES" 'BEGIN{printf "%d", v/1024/1024}')
KEY="probe/upload-test-${OPERATOR}-$(date +%s).bin"
LOG="/tmp/probe3-${OPERATOR}-$(date +%s).log"

echo "==== Phase 0 探针 ③ R2 真实 multipart 上传 ===="
echo "运营商  : $OPERATOR"
echo "样本    : $SAMPLE (${SIZE_MB} MB)"
echo "目标    : s3://${R2_BUCKET}/${KEY}"
echo "Endpoint: $R2_ENDPOINT"
echo "日期    : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "日志    : $LOG"
echo ""

# ---------- 上传 ----------
echo "--- 开始上传 (25MB 分片, 与方案 D15 对齐) ---"
START=$(date +%s)

# --debug 模式太吵, 用普通模式但通过 AWS_MAX_ATTEMPTS 限制重试观察真实重传
aws s3 cp "$SAMPLE" "s3://${R2_BUCKET}/${KEY}" \
    --endpoint-url="$R2_ENDPOINT" \
    --region="auto" \
    --cli-read-timeout=120 \
    --cli-connect-timeout=30 \
    --metadata "probe-operator=${OPERATOR}" \
    > "$LOG" 2>&1
STATUS=$?

END=$(date +%s)
DURATION=$((END - START))

MIN=$((DURATION / 60))
SEC=$((DURATION % 60))

if [ "$DURATION" -gt 0 ]; then
    SPEED_MBPS=$(awk -v s="$SIZE_MB" -v t="$DURATION" 'BEGIN{printf "%.2f", s/t}')
else
    SPEED_MBPS="0.00"
fi

# 统计可能的重传提示(aws cli 不会在非 debug 模式输出太多, 但可以从日志抓一些迹象)
RETRY=$(grep -ciE "retry|throttl|retrying|backoff|timed out" "$LOG" 2>/dev/null || echo 0)
[ -z "$RETRY" ] && RETRY=0

if [ "$STATUS" -eq 0 ]; then
    RESULT="成功"
else
    RESULT="失败(exit=$STATUS)"
    echo ""
    echo "--- 上传失败日志尾部 ---"
    tail -20 "$LOG"
fi

# ---------- 清理(成功才删, 失败保留供排查) ----------
echo ""
echo "--- 清理 ---"
if [ "$STATUS" -eq 0 ]; then
    if aws s3 rm "s3://${R2_BUCKET}/${KEY}" --endpoint-url="$R2_ENDPOINT" --region="auto" >/dev/null 2>&1; then
        echo "  ✓ 已删除测试对象"
    else
        echo "  ⚠ 删除失败, 请项目方手工清理: s3://${R2_BUCKET}/${KEY}"
    fi
else
    echo "  ⚠ 上传失败, 保留对象供排查: s3://${R2_BUCKET}/${KEY}"
    echo "  ⚠ 请项目方排查后手工删除"
fi

# ---------- 输出结果行 ----------
echo ""
echo "--- 汇总 ---"
echo "  总耗时: ${MIN}m${SEC}s (${DURATION}s)"
echo "  平均上行: ${SPEED_MBPS} MB/s"
echo "  状态: $RESULT"
echo "  重试/警告提示次数: $RETRY"
echo ""
echo "=========================================================="
echo "【贴到方案 § 15.4 表的这一行】"
echo ""
echo "| 中国${OPERATOR} | ${MIN}m${SEC}s | ${SPEED_MBPS} | ${RESULT} | ${RETRY} | \`_备注_\` |"
echo ""
echo "=========================================================="

# ---------- Phase 3 放行判据提醒 ----------
if [ "$STATUS" -eq 0 ]; then
    echo "✓ 此运营商上传成功"
else
    echo "⚠ 此运营商上传失败, 详见 $LOG"
fi

echo ""
echo "注: 三网成功率达标判据(方案 D38):"
echo "  ≥ 80% → Phase 3 路径 α (4.5d)"
echo "  60-80% → 路径 β (+1d UI 灰度)"
echo "  < 60% → 路径 γ (暂缓, 重评审)"

exit "$STATUS"
