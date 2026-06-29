"""D7 前端转完整接线守卫（CodeX P3）.

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §6.5 / D7 前端。

端到端漏斗最后一块：登录认领后，创建页用认领的完整原视频作源走正常付费流程。
项目无 JS test runner → Python 静态扫描锁住关键接线（沿用
test_anonymous_preview_claim_admin_sync_guard.py 约定）：
- claim.ts：认领成功写 convert-ready key（带 TTL）+ 三件套 helper。
- jobs.ts：submitTranslationJob 仅在有认领预览时注入 reuse_anonymous_preview_id。
- TranslationForm.tsx：mount 读 convert-ready → banner 替代上传 + sourceValidationError
  短路 + handleSubmit 注入字段 + 双击 ref 守卫 + 转完整失败清模式。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FE = REPO_ROOT / "frontend-next" / "src"
CLAIM_TS = FE / "lib" / "api" / "claim.ts"
JOBS_TS = FE / "lib" / "api" / "jobs.ts"
TYPES_TS = FE / "types" / "jobs.ts"
FORM_TSX = FE / "components" / "workspace" / "TranslationForm.tsx"
APP_SHELL_TSX = FE / "components" / "app-shell.tsx"
SETTINGS_TSX = FE / "app" / "[locale]" / "(app)" / "settings" / "page.tsx"
POST_AUTH_TS = FE / "lib" / "auth" / "post-auth-redirect.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"前端文件不存在: {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. claim.ts — convert-ready key（带 TTL）+ 认领成功写入
# ---------------------------------------------------------------------------


def test_claim_ts_convert_ready_helpers_and_ttl():
    src = _read(CLAIM_TS)
    assert 'CONVERT_READY_KEY = "avt_anon_convert_ready"' in src, "convert-ready key"
    for fn in ("setAnonConvertReady", "getAnonConvertReady", "clearAnonConvertReady"):
        assert f"export function {fn}" in src, f"缺 helper {fn}"
    # TTL（CodeX P2）：存 {previewId, ts}，读时超期/损坏即清。
    assert "CONVERT_READY_TTL_MS" in src, "convert-ready 必须带 TTL（防跨账号残留 banner）"
    assert "JSON.stringify(payload)" in src, "应存 {previewId, ts, userId}"
    assert re.search(r"payload:\s*\{\s*previewId:\s*string;\s*ts:\s*number;\s*userId:\s*string", src), \
        "convert-ready payload 必须带 userId"


def test_claim_ts_writes_convert_ready_on_claim_success():
    src = _read(CLAIM_TS)
    m = re.search(
        r"export async function maybeClaimAnonPreviewAfterLogin[\s\S]*?\n\}", src
    )
    assert m, "找不到 maybeClaimAnonPreviewAfterLogin"
    body = m.group(0)
    # 认领成功（claimed + preview_ids[0]）→ setAnonConvertReady 供创建页转完整。
    assert "outcome.claimed" in body and "preview_ids" in body
    assert "setAnonConvertReady(" in body, "认领成功必须写 convert-ready 供创建页"


def test_claim_ts_uses_hinted_preview_for_convert_ready():
    src = _read(CLAIM_TS)
    m = re.search(
        r"export async function maybeClaimAnonPreviewAfterLogin[\s\S]*?\n\}", src
    )
    assert m, "找不到 maybeClaimAnonPreviewAfterLogin"
    body = m.group(0)
    # CodeX P2: claim may bind multiple previews with no ordering. The
    # convert-ready preview must be the same preview that set the local hint,
    # otherwise the create page can preselect the wrong original video.
    assert "const hintedPreviewId = getAnonClaimHint()" in body
    assert re.search(r"\.includes\(\s*hintedPreviewId\s*\)", body)
    assert re.search(r"setAnonConvertReady\(\s*hintedPreviewId\s*,\s*userId\s*\)", body)
    assert "setAnonConvertReady(outcome.preview_ids[0])" not in body


def test_convert_ready_is_scoped_to_authenticated_user():
    claim_src = _read(CLAIM_TS)
    form_src = _read(FORM_TSX)
    app_shell_src = _read(APP_SHELL_TSX)
    settings_src = _read(SETTINGS_TSX)
    post_auth_src = _read(POST_AUTH_TS)

    # CodeX P2: browser-wide localStorage must not put a second account into
    # the first account's stale convert flow.
    assert re.search(r"setAnonConvertReady\(previewId: string,\s*userId", claim_src)
    assert re.search(r"getAnonConvertReady\(userId", claim_src)
    assert "if (!userId) return null" in claim_src
    assert "scopeAnonConvertReadyToUser" in claim_src
    assert "storedUserId !== userId" in claim_src

    assert "useSession" in form_src
    assert re.search(r"getAnonConvertReady\(\s*user\?\.id", form_src)
    assert "scopeAnonConvertReadyToUser(user.id)" in app_shell_src
    assert "maybeClaimAnonPreviewAfterLogin(user.id)" in app_shell_src

    assert re.search(r"waitForSessionReady\(\): Promise<string \| null>", post_auth_src)
    assert "maybeClaimAnonPreviewAfterLogin(userId)" in post_auth_src

    assert "clearAnonConvertReady()" in app_shell_src
    assert "clearAnonConvertReady()" in settings_src


# ---------------------------------------------------------------------------
# 2. types/jobs.ts + jobs.ts — reuseAnonPreviewId 透传
# ---------------------------------------------------------------------------


def test_types_has_reuse_field():
    src = _read(TYPES_TS)
    assert re.search(r"reuseAnonPreviewId\?\s*:\s*string", src), "CreateTranslationJobInput 缺字段"


def test_jobs_submit_injects_reuse_field_conditionally():
    src = _read(JOBS_TS)
    # 仅在有认领预览时注入 reuse_anonymous_preview_id（普通创建 byte-identical）。
    assert re.search(
        r"if\s*\(\s*input\.reuseAnonPreviewId\s*\)\s*\{[\s\S]*?reuse_anonymous_preview_id\s*=\s*input\.reuseAnonPreviewId",
        src,
    ), "submitTranslationJob 必须条件注入 requestBody.reuse_anonymous_preview_id"


# ---------------------------------------------------------------------------
# 3. TranslationForm.tsx — 读 convert-ready / banner / 短路 / 注入 / 守卫
# ---------------------------------------------------------------------------


def test_form_reads_convert_ready_and_state():
    src = _read(FORM_TSX)
    assert "getAnonConvertReady(" in src, "mount 须读 convert-ready"
    assert re.search(r"reuseAnonPreviewId.*useState", src) or "setReuseAnonPreviewId" in src, "reuseAnonPreviewId state"


def test_form_handles_late_convert_ready_writes():
    claim_src = _read(CLAIM_TS)
    form_src = _read(FORM_TSX)
    # CodeX P2: AppShell retry can claim after the create page has mounted.
    # Same-tab localStorage writes do not emit a native storage event, so the
    # claim helper must notify the mounted form explicitly.
    assert "ANON_CONVERT_READY_EVENT" in claim_src
    assert "window.dispatchEvent" in claim_src
    assert "subscribeAnonConvertReady" in claim_src
    assert "subscribeAnonConvertReady" in form_src
    assert re.search(r"setReuseAnonPreviewId\(\s*getAnonConvertReady\(\s*user\?\.id\s*\)\s*\)", form_src)


def test_form_source_validation_short_circuits_in_convert_mode():
    src = _read(FORM_TSX)
    # sourceValidationError 在 reuseAnonPreviewId 时短路为 null（否则表单永远不可提交）。
    assert re.search(
        r"sourceValidationError\s*=\s*reuseAnonPreviewId\s*\n?\s*\?\s*null", src
    ), "sourceValidationError 必须在转完整模式短路为 null"


def test_form_injects_reuse_field_and_clears_on_success():
    src = _read(FORM_TSX)
    assert re.search(r"reuseAnonPreviewId\s*:\s*reuseAnonPreviewId\s*\?\?\s*undefined", src), \
        "handleSubmit 须把 reuseAnonPreviewId 传给 submitTranslationJob"
    # 成功后清 convert-ready key（避免返回创建页重复进入转完整模式）。
    assert "clearAnonConvertReady()" in src


def test_form_has_double_submit_guard():
    src = _read(FORM_TSX)
    # 双击守卫（CodeX P2）：ref 同步拦截。
    assert "submittingRef" in src, "缺双击 ref 守卫"
    assert re.search(r"if\s*\(\s*submittingRef\.current\s*\)\s*return", src), \
        "handleSubmit 开头须 if (submittingRef.current) return"


def test_form_clears_convert_mode_on_reuse_rejection():
    src = _read(FORM_TSX)
    # 转完整失败（anon_preview_* 不可复用）→ 清模式提示重传（CodeX P2）。
    assert "isAnonConvertRejected" in src, "缺转完整失败检测"
    assert re.search(r'startsWith\(\s*"anon_preview"', src), \
        "isAnonConvertRejected 应识别 anon_preview_* 错误码"


def test_form_hides_smart_preview_entry_in_convert_mode():
    src = _read(FORM_TSX)
    # 转完整模式隐藏 smart 3min 预览试用入口（已预览过 + 源非 fresh upload）。
    assert re.search(
        r"smartPreviewEntryEnabled\s*&&\s*!reuseAnonPreviewId", src
    ), "转完整模式应隐藏 smart 预览试用入口"


# ---------------------------------------------------------------------------
# 4. A 方案 pre-flight 时长闸前端 CTA（plan 2026-06-16 转化漏斗 UX）
# ---------------------------------------------------------------------------


def test_form_maps_duration_block_two_tier_cta():
    src = _read(FORM_TSX)
    # 识别两档可区分 reason（body.error）：可升级 + 超过最高自助套餐（CodeX P1）。
    assert "duration_upgrade_required" in src, "缺可升级错误码识别"
    assert "duration_over_max_plan" in src, "缺『超过最高套餐』错误码识别（CodeX P1）"
    assert "readDurationBlockReason" in src, "缺两档 reason 检测 helper"
    # 命中 → 持久 banner（state，带 canUpgrade）而非死路 toast。
    assert "durationBlock" in src, "缺时长 banner state"
    assert "setDurationBlock(" in src, "命中须设置 banner"
    # 升级 CTA 仅在 canUpgrade 时给 /pricing（over_max 升级也没用 → 不给 /pricing）。
    assert re.search(r"durationBlock\.canUpgrade\s*\?[\s\S]*?href=\"/pricing\"", src), \
        "/pricing CTA 须由 durationBlock.canUpgrade 守门（CodeX P1：升无可升不给升级口）"
    # 保留转完整模式：源有效，升级 / 换更短视频后可重试 → duration 分支不得清 reuseAnonPreviewId。
    block = re.search(r"const durationReason = readDurationBlockReason\(error\)[\s\S]*?\n      \}", src)
    assert block, "找不到 duration block catch 分支"
    assert "setReuseAnonPreviewId(null)" not in block.group(0), \
        "时长超限不应清转完整模式（源有效，升级 / 换视频后可重试）"


def test_form_clears_duration_banner_on_switch_video():
    src = _read(FORM_TSX)
    # CodeX P3：点「改用其它视频」清 reuseAnonPreviewId 时，须一并清旧的时长 banner。
    switch = re.search(
        r"setReuseAnonPreviewId\(null\)\s*\n\s*clearAnonConvertReady\(\)[\s\S]*?setDurationBlock\(null\)",
        src,
    )
    assert switch, "『改用其它视频』须 setDurationBlock(null) 清残留 banner（CodeX P3）"
