#!/usr/bin/env bash
# TU-03 quality scaffold — 一键本地质量门（Git Bash / CI Linux）。
# 见 docs/plans/code-quality-tasks/TU-03-quality-scaffold.md §7 / 母方案 §10。
#
# 判定口径（与 CI python-lint 对齐，首周一律不把历史债变红）：
#   - changed-files ruff/format  → 阻断（决定本脚本退出码）
#   - 核心架构守卫测试 / 全仓 ruff / 窄域 mypy → 仅报告（打印结果，不影响退出码）
#
# 用法：
#   bash scripts/check_quality.sh [BASE_REF]   # BASE_REF 默认 origin/main
set -uo pipefail

BASE="${1:-origin/main}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail=0
have() { command -v "$1" >/dev/null 2>&1; }

echo "==> [1/4] 核心架构守卫测试 (report-only)"
if have python; then
  python -m pytest -q -p no:cacheprovider \
    tests/test_gateway_startup_checks.py \
    tests/test_phase1_guards.py \
    tests/test_legacy_cleanup_guards.py \
    || echo "  [warn] 守卫测试有失败/环境缺依赖（见上，本地仅报告）"
else
  echo "  [skip] 未找到 python"
fi

echo "==> [2/4] ruff 全仓 (report-only, --exit-zero)"
if have ruff; then
  ruff check src/ gateway/ --exit-zero --output-format=concise
else
  echo "  [skip] 未安装 ruff（pip install ruff）"
fi

echo "==> [3/4] 窄域 mypy (report-only, 首周不阻断)"
if have mypy; then
  mypy src/core/ src/utils/ src/services/llm/ gateway/storage/ \
    --ignore-missing-imports --check-untyped-defs \
    || echo "  [warn] mypy 报告若干问题（首周不阻断，债务清理见 TU-07）"
else
  echo "  [skip] 未安装 mypy（pip install mypy）"
fi

echo "==> [4/4] 新增 .py 阻断 + 改动既有 .py report-only (决定退出码；与 CI python-lint 对齐)"
if have ruff; then
  git fetch -q origin "${BASE#origin/}" --depth=1 2>/dev/null || true
  # 本地是普通分支 checkout（非 PR merge ref），用三点 diff（merge-base）取 PR 净改动。
  # 只对【新增】文件阻断（无历史债）；【改动既有】文件仅报告，不拿历史债卡退出码。
  ADDED="$(git diff --name-only --diff-filter=A "${BASE}...HEAD" -- '*.py' 2>/dev/null | tr '\n' ' ')"
  EXISTING="$(git diff --name-only --diff-filter=MRC "${BASE}...HEAD" -- '*.py' 2>/dev/null | tr '\n' ' ')"
  if [ -n "${ADDED// /}" ]; then
    echo "  新增 .py（阻断）: $ADDED"
    ruff check $ADDED --output-format=concise || fail=1
    ruff format --check $ADDED || fail=1
  else
    echo "  无新增 .py 文件"
  fi
  if [ -n "${EXISTING// /}" ]; then
    echo "  改动既有 .py（report-only）: $EXISTING"
    ruff check $EXISTING --exit-zero --output-format=concise || true
  fi
else
  echo "  [skip] 未安装 ruff，无法做 changed-files 检查"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "==> 结果: 新增 .py 存在 ruff/format 问题 ✗"
  exit 1
fi
echo "==> 结果: 新增 .py 干净 ✓ （改动既有 .py / 全仓 ruff / 窄域 mypy / 守卫为报告项）"
