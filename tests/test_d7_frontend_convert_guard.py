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
    assert re.search(r"JSON\.stringify\(\s*\{\s*previewId", src), "应存 {previewId, ts}"


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
