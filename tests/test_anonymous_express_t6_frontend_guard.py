"""APF 匿名 Express T6 — 前端 lane 三态 / 失败重试 / 智能版 CTA 静态守卫.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §G/T6：

* /limits 增返 active_lane / master_open（后端 T1 已测）；前端按其渲染
  free/express/关闭三态，两 lane 都关显示「暂未开放」。
* failed 态显示「重试」按钮：复用 preview_id 重新 create，不重新上传。
* 注册/智能版 CTA 带 returnTo（repo 既有约定是 /auth?from=<内部路径>，
  resolvePostAuthRedirect 读 ``from`` 参数）。

repo 无 JS test runner——Python 静态扫描（同 admin sync guard 模式），
类型正确性由 `npx tsc --noEmit` 把关。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_TS = REPO_ROOT / "frontend-next" / "src" / "lib" / "api" / "anonymousPreview.ts"
PANEL_TSX = (
    REPO_ROOT / "frontend-next" / "src" / "components" / "marketing"
    / "anonymous-trial-panel.tsx"
)
# UI-03g：面板可见文案迁入 next-intl 字典（marketing.anonymousTrial.*）。原先钉在
# panel 源里的 zh 文案断言改钉字典（zh 值的字节一致另由 zh-snapshot.mjs 守卫）。
MARKETING_ZH = REPO_ROOT / "frontend-next" / "messages" / "zh" / "marketing.json"


def _api_src() -> str:
    return API_TS.read_text(encoding="utf-8")


def _panel_src() -> str:
    return PANEL_TSX.read_text(encoding="utf-8")


def _marketing_zh() -> str:
    return MARKETING_ZH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. API client：lane 三态字段
# ---------------------------------------------------------------------------


def test_api_limits_carries_lane_fields():
    src = _api_src()
    assert "active_lane: ActiveLane" in src
    assert "master_open: boolean" in src
    assert "express_clone_available: boolean" in src
    # 三态解析：express / free / 关闭（null）
    assert "'express'" in src and "'free'" in src


def test_api_default_limits_fail_open():
    """拉取失败兜底必须 fail-open（free/开）——保持既有 UX，服务端才是
    真正的 gate；fail-closed 会让网络抖动直接关停入口。"""
    src = _api_src()
    default_block = src.split("export const DEFAULT_PREVIEW_LIMITS")[1].split("}")[0]
    assert "active_lane: 'free'" in default_block
    assert "master_open: true" in default_block
    assert "express_clone_available: false" in default_block


# ---------------------------------------------------------------------------
# 2. 面板：三态渲染 + 失败重试 + CTA
# ---------------------------------------------------------------------------


def test_panel_renders_closed_state():
    src = _panel_src()
    assert "master_open" in src
    assert "暂未开放" in src


def test_panel_renders_express_lane_copy():
    src = _panel_src()
    assert "active_lane === 'express'" in src
    # UI-03g：文案迁入字典（marketing.anonymousTrial.footerExpressSuffix）。
    assert "快捷版真实管线" in _marketing_zh()


def test_panel_gates_clone_opt_in_on_backend_availability():
    src = _panel_src()
    assert "limits.active_lane === 'express' && limits.express_clone_available" in src
    for marker in (
        "async function handleCreate",
        "async function handleRetry",
    ):
        body = src.split(marker)[1].split(
            "setState((s) => ({ ...s, step: 'processing'"
        )[0]
        assert "limits.express_clone_available" in body


def test_panel_failed_step_retries_with_same_preview_id():
    """failed 态「重试」必须复用 preview_id 重新 create（不重新上传）。"""
    src = _panel_src()
    assert "'failed'" in src
    assert "handleRetry" in src
    # handleRetry 体内调 createPreview（previewId）而非 upload
    retry_body = src.split("async function handleRetry")[1].split(
        "// ── Polling ──"
    )[0]
    assert "createPreview(previewId" in retry_body
    assert "uploadPreviewVideo" not in retry_body
    # failed 渲染分支带「重试」按钮 + 不重新上传文案
    assert "重试" in src
    assert "无需重新上传" in src


def test_panel_poll_failed_goes_to_failed_step_not_error():
    src = _panel_src()
    assert "step: 'failed'" in src


def test_panel_cta_carries_return_to():
    """注册/智能版 CTA 带 returnTo（repo 约定 /auth?from=<path>）。"""
    src = _panel_src()
    assert "/auth?from=" in src
    # UI-03g：智能版 CTA 文案迁入字典（marketing.anonymousTrial.*）。
    assert "智能版" in _marketing_zh()


def test_panel_lifecycle_not_blocked_by_closed_lane():
    """三态关停只拦 idle（新 intake 入口）——进行中/已就绪的预览
    （processing/ready/failed 步）不受 lane 开关影响（§A 生命周期不变量
    的前端镜像）。"""
    src = _panel_src()
    assert "!limits.master_open && step === 'idle'" in src
