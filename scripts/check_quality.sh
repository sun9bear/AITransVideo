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

echo "==> [4/4] changed-files ruff (阻断口径，决定退出码)"
if have ruff; then
  git fetch -q origin "${BASE#origin/}" --depth=1 2>/dev/null || true
  FILES="$(git diff --name-only "${BASE}...HEAD" -- '*.py' 2>/dev/null | tr '\n' ' ')"
  if [ -n "${FILES// /}" ]; then
    echo "  改动 .py 文件: $FILES"
    ruff check $FILES --output-format=concise || fail=1
    ruff format --check $FILES || fail=1
  else
    echo "  无改动的 .py 文件，跳过 changed-files 阻断检查"
  fi
else
  echo "  [skip] 未安装 ruff，无法做 changed-files 阻断检查"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "==> 结果: changed-files 存在 ruff/format 问题 ✗"
  exit 1
fi
echo "==> 结果: changed-files 干净 ✓ （全仓 ruff / 窄域 mypy / 守卫为报告项）"
