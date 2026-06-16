"""智能版预览转完整 pre-flight 时长闸前端守卫（2026-06-16 对抗审查 finding #2）.

消除与 D7 匿名转完整（test_d7_frontend_convert_guard.py §4）的不对称：smart 预览转完整
路径（smartPreviewClone.ts + SmartPreviewResultCard.tsx）也须识别两档可区分 reason
（后端 body.error = duration_upgrade_required / duration_over_max_plan）并渲染对应 CTA：

- duration_upgrade_required（≤ 最高自助套餐，升级可解决）→ /pricing 升级 CTA。
- duration_over_max_plan（超过最高自助套餐，升级也没用）→ 仅提示用更短视频 / 联系客服，
  **不**给 /pricing、**不**给转完整按钮。

项目无 JS test runner → Python 静态扫描（沿用 test_d7_frontend_convert_guard.py 约定）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FE = REPO_ROOT / "frontend-next" / "src"
CLONE_TS = FE / "lib" / "api" / "smartPreviewClone.ts"
CARD_TSX = FE / "components" / "workspace" / "SmartPreviewResultCard.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"前端文件不存在: {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. smartPreviewClone.ts — mapper 识别两档 reason（后端 body.error 契约）
# ---------------------------------------------------------------------------


def test_clone_ts_maps_two_tier_duration_reasons():
    src = _read(CLONE_TS)
    # 后端两档 error code（与 D7 共享同一字面量契约）。
    assert "duration_upgrade_required" in src, "缺可升级错误码识别"
    assert "duration_over_max_plan" in src, "缺『超过最高套餐』错误码识别（CodeX P1）"
    # union 新增两个 reason。
    assert "'duration_upgrade'" in src and "'duration_over_max'" in src, "reason 枚举缺新成员"
    # mapper：后端 code → 前端 reason（可升级 / 升无可升）。
    assert re.search(
        r"code === 'duration_upgrade_required'[\s\S]*?reason: 'duration_upgrade'", src
    ), "duration_upgrade_required 须映射为 duration_upgrade"
    assert re.search(
        r"code === 'duration_over_max_plan'[\s\S]*?reason: 'duration_over_max'", src
    ), "duration_over_max_plan 须映射为 duration_over_max"


# ---------------------------------------------------------------------------
# 2. SmartPreviewResultCard.tsx — 两档 CTA（升级 → /pricing；升无可升 → 仅提示）
# ---------------------------------------------------------------------------


def test_card_handles_duration_two_tier_state():
    src = _read(CARD_TSX)
    assert "durationOverMax" in src, "缺『超最高套餐』state"
    assert "setDurationOverMax(" in src, "命中须设置 over-max state"
    # duration_upgrade 并入 /pricing 升级 CTA（与 smart entitlement upgrade_required 同 UX）。
    assert re.search(r'mapped\.reason === "duration_upgrade"', src), \
        "duration_upgrade 须并入升级 CTA（给 /pricing）"
    # duration_over_max 走「升无可升」分支。
    assert re.search(r'mapped\.reason === "duration_over_max"', src), \
        "duration_over_max 须设 over-max 分支（不给 /pricing）"


def test_card_over_max_branch_has_no_pricing_link_or_button():
    src = _read(CARD_TSX)
    # over-max 三元分支体内：升无可升 + 重试同源必再失败 → 既不给 /pricing，也不给转完整
    # 按钮（仅提示用更短视频 / 联系客服）。除 href 外一并断言无 <Link / <Button / handleConvert
    # 绑定（CodeX LOW：仅查 href 不够，<Button onClick 同样是死路重试入口）。
    m = re.search(r"\{durationOverMax \?([\s\S]*?)\) : upgradeRequired \?", src)
    assert m, "找不到 durationOverMax CTA 三元分支"
    branch = m.group(1)
    assert 'href="/pricing"' not in branch, "超最高套餐分支不得给 /pricing（升无可升，CodeX P1）"
    assert "<Link" not in branch and "<Button" not in branch, "over-max 分支不得有任何 CTA 按钮/链接"
    assert "onClick={handleConvert}" not in branch, "over-max 分支不得给转完整按钮（死路重试）"
    # /pricing 仍存在于其后的 upgradeRequired 升级分支（可升级路径不受影响）。
    assert 'href="/pricing"' in src, "可升级分支仍须给 /pricing"


def test_card_convert_button_only_in_default_branch():
    src = _read(CARD_TSX)
    # 转完整按钮（onClick={handleConvert}）只能在最后的 else 分支出现一次——两个被拦分支
    # （durationOverMax / upgradeRequired）都不得含它，否则会重现 doomed-retry 循环
    # （hoist / 复制按钮到被拦分支的回归，结构扫描挡不住，故按出现次数 + 升级分支不含来锁）。
    assert src.count("onClick={handleConvert}") == 1, "转完整按钮只应绑定一次（仅 else 分支）"
    up = re.search(r"\) : upgradeRequired \?([\s\S]*?)\) : \(", src)
    assert up, "找不到 upgradeRequired 三元分支"
    assert "onClick={handleConvert}" not in up.group(1), "升级分支不得给转完整按钮（应只给 /pricing）"


def test_card_resets_duration_state_on_retry():
    src = _read(CARD_TSX)
    # 每次 handleConvert 重置两个 banner state（避免上次失败残留）。
    assert "setUpgradeRequired(false)" in src and "setDurationOverMax(false)" in src, \
        "handleConvert 须重置 upgradeRequired + durationOverMax"
