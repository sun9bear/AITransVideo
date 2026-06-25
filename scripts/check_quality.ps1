<#
.SYNOPSIS
  TU-03 quality scaffold — 一键本地质量门（Windows PowerShell）。
.DESCRIPTION
  见 docs/plans/code-quality-tasks/TU-03-quality-scaffold.md §7 / 母方案 §10。
  判定口径（与 CI python-lint 对齐，首周一律不把历史债变红）：
    - changed-files ruff/format  → 阻断（决定本脚本退出码）
    - 核心架构守卫测试 / 全仓 ruff / 窄域 mypy → 仅报告（不影响退出码）
.PARAMETER Base
  diff 基准引用，默认 origin/main。
.EXAMPLE
  pwsh scripts/check_quality.ps1
  pwsh scripts/check_quality.ps1 -Base origin/main
#>
param([string]$Base = "origin/main")

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$fail = 0
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

Write-Host "==> [1/4] 核心架构守卫测试 (report-only)"
if (Have "python") {
  python -m pytest -q -p no:cacheprovider `
    tests/test_gateway_startup_checks.py `
    tests/test_phase1_guards.py `
    tests/test_legacy_cleanup_guards.py
  if ($LASTEXITCODE -ne 0) { Write-Host "  [warn] 守卫测试有失败/环境缺依赖（本地仅报告）" }
} else { Write-Host "  [skip] 未找到 python" }

Write-Host "==> [2/4] ruff 全仓 (report-only, --exit-zero)"
if (Have "ruff") {
  ruff check src/ gateway/ --exit-zero --output-format=concise
} else { Write-Host "  [skip] 未安装 ruff（pip install ruff）" }

Write-Host "==> [3/4] 窄域 mypy (report-only, 首周不阻断)"
if (Have "mypy") {
  mypy src/core/ src/utils/ src/services/llm/ gateway/storage/ `
    --ignore-missing-imports --check-untyped-defs
  if ($LASTEXITCODE -ne 0) { Write-Host "  [warn] mypy 报告若干问题（首周不阻断，债务清理见 TU-07）" }
} else { Write-Host "  [skip] 未安装 mypy（pip install mypy）" }

Write-Host "==> [4/4] changed-files ruff (阻断口径，决定退出码)"
if (Have "ruff") {
  git fetch -q origin ($Base -replace '^origin/', '') --depth=1 2>$null
  # --diff-filter=ACMR 去掉 Deleted，避免 ruff 对被删文件 E902（与 CI python-lint 对齐）。
  $files = @(git diff --name-only --diff-filter=ACMR "$Base...HEAD" -- '*.py' 2>$null | Where-Object { $_ })
  if ($files.Count -gt 0) {
    Write-Host ("  改动 .py 文件: " + ($files -join " "))
    ruff check @files --output-format=concise; if ($LASTEXITCODE -ne 0) { $fail = 1 }
    ruff format --check @files; if ($LASTEXITCODE -ne 0) { $fail = 1 }
  } else {
    Write-Host "  无改动的 .py 文件，跳过 changed-files 阻断检查"
  }
} else { Write-Host "  [skip] 未安装 ruff，无法做 changed-files 阻断检查" }

Write-Host ""
if ($fail -ne 0) {
  Write-Host "==> 结果: changed-files 存在 ruff/format 问题 ✗"
  exit 1
}
Write-Host "==> 结果: changed-files 干净 ✓ （全仓 ruff / 窄域 mypy / 守卫为报告项）"
exit 0
