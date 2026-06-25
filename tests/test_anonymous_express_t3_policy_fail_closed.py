"""APF 匿名 Express T3 — 策略点 fail-closed（effective_policy_mode）守卫.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §C：

新增 ``effective_policy_mode(service_mode, anonymous_preview)``——
``anonymous_preview=True`` 恒返回 ``"anonymous_preview"``（最严档）。
八点全清单：① 水印 ② 下载 key ③ stream kinds ④ Job API 下载门
⑤ Job API stream 门 ⑥ Gateway R2 redirect（download+stream）
⑦ Job API artifacts 列表 ⑧ R2 sweeper is_anonymous_preview 短路（既有）。

验收：匿名 express 成片有水印、零下载 key、仅 stream video、不进 R2
redirect；AST 守卫钉死 §C 文件不得新增绕过 helper 的 service_mode 字面量比较。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.r2_publisher_lib.downloadable_keys import (  # noqa: E402
    download_keys_for,
    eager_push_keys_for,
    effective_policy_mode,
    stream_kinds_for,
)
from utils.free_watermark import free_watermark_text_for  # noqa: E402


# ---------------------------------------------------------------------------
# A. effective_policy_mode 语义
# ---------------------------------------------------------------------------


class TestEffectivePolicyMode:
    @pytest.mark.parametrize(
        "service_mode", ["express", "free", "studio", "smart", None, ""]
    )
    def test_anonymous_always_wins(self, service_mode):
        assert effective_policy_mode(service_mode, True) == "anonymous_preview"

    @pytest.mark.parametrize(
        "service_mode", ["express", "free", "studio", None]
    )
    def test_non_anonymous_passthrough(self, service_mode):
        assert effective_policy_mode(service_mode, False) == service_mode

    def test_truthy_flag_normalized(self):
        # JobRecord 字段可能是非 bool 真值（JSON 反序列化），按 truthiness 判
        assert effective_policy_mode("express", 1) == "anonymous_preview"
        assert effective_policy_mode("express", None) == "express"


# ---------------------------------------------------------------------------
# B. 验收四件套：匿名 express 有水印 / 零下载 / 仅 stream video / 不进 R2
# ---------------------------------------------------------------------------


class TestAnonymousExpressPolicyChain:
    MODE = effective_policy_mode("express", True)

    def test_watermark_forced(self):
        assert free_watermark_text_for(self.MODE), (
            "匿名 express 成片必须有水印（最高指导原则：真实管线效果 + "
            "防白嫖干净成片）"
        )

    def test_zero_download_keys(self):
        assert download_keys_for(self.MODE) == frozenset()

    def test_stream_video_only(self):
        assert stream_kinds_for(self.MODE) == frozenset({"video"})

    def test_no_eager_r2_push(self):
        assert eager_push_keys_for(self.MODE) == frozenset()

    def test_paid_express_unchanged(self):
        """登录 express 行为零变化：无水印、可下载成片。"""
        mode = effective_policy_mode("express", False)
        assert free_watermark_text_for(mode) is None
        assert "publish.dubbed_video" in download_keys_for(mode)

    def test_free_lane_watermark_unchanged(self):
        assert free_watermark_text_for(effective_policy_mode("free", False))


# ---------------------------------------------------------------------------
# C. Gateway R2 redirect：匿名任务不 redirect（download + stream 两处）
# ---------------------------------------------------------------------------


class TestGatewayR2RedirectGates:
    def _fake_db_with_job(self, **overrides):
        job = MagicMock()
        job.service_mode = overrides.get("service_mode", "express")
        job.is_anonymous_preview = overrides.get("is_anonymous_preview", True)
        job.project_dir = "/opt/aivideotrans/app/projects/p1"
        job.edit_generation = 0
        job.r2_artifacts = [
            {
                "artifact_key": "publish.dubbed_video",
                "edit_generation": 0,
                "state": "pushed",
                "r2_key": "jobs/j1/publish.dubbed_video.mp4",
                "filename": "v.mp4",
                "content_type": "video/mp4",
            }
        ]
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=job)
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.fixture(autouse=True)
    def _r2_enabled(self, monkeypatch):
        pytest.importorskip("boto3")
        import storage.backend_router as backend_router

        monkeypatch.setattr(backend_router, "is_r2_enabled", lambda: True)

    @pytest.mark.asyncio
    async def test_download_redirect_refused_for_anonymous(self):
        import job_intercept

        url, kind = await job_intercept._resolve_r2_redirect(
            self._fake_db_with_job(), "j1", artifact_key="publish.dubbed_video"
        )
        assert url is None and kind == ""

    @pytest.mark.asyncio
    async def test_stream_redirect_refused_for_anonymous(self):
        import job_intercept

        url, kind = await job_intercept._resolve_r2_stream_redirect(
            self._fake_db_with_job(), "j1", stream_kind="video"
        )
        assert url is None and kind == "", (
            "匿名任务的 video stream 也不进 R2 redirect——本地 stream-only "
            "（AD-6），由 Job API 直通字节流"
        )


# ---------------------------------------------------------------------------
# D. 源码级守卫：策略点经 helper + 不新增 service_mode 字面量比较
# ---------------------------------------------------------------------------


def _count_service_mode_literal_eq(path: Path) -> int:
    """统计 `X == "literal"`（或反向）中名为 service_mode 的直接比较。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _is_sm(node) -> bool:
        return (isinstance(node, ast.Name) and node.id == "service_mode") or (
            isinstance(node, ast.Attribute) and node.attr == "service_mode"
        )

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        sides = [node.left, *node.comparators]
        has_sm = any(_is_sm(s) for s in sides)
        has_lit = any(
            isinstance(s, ast.Constant) and isinstance(s.value, str)
            for s in sides
        )
        if has_sm and has_lit:
            count += 1
    return count


class TestPolicySourceGuards:
    # §C 文件的 service_mode 字面量直接比较基线——新增判断必须经
    # effective_policy_mode helper，任何升高即 red。
    _BASELINE = {
        # 策略函数本体（helper 的真源所在，合法分支）
        "src/services/r2_publisher_lib/downloadable_keys.py": 9,
        # _is_express_job 用 getattr(...) == "express"（Call 节点，不计入）；
        # 其余直接比较不得新增
        "src/services/jobs/api.py": 1,
        # 既有 create-intercept / post-edit 等业务逻辑（§C 范围外、含 i18n
        # PR-A 等分支累积的 service_mode 业务比较）；§C 两个 R2 redirect gate
        # 已改走 helper。任何升高 = 有人绕过 helper 新增**策略**判断。
        # 14 = 12 既有业务比较
        #    + P3e-2b 的 ``service_mode == "smart"`` reserve-gate（智能版预览克隆
        #      600 预扣触发判断）
        #    + P3e-4a 的 ``service_mode == "smart"`` 预览 lane **准入** gate
        #      （smart_preview_lane_exempt 一次性判定：免费/未获 smart entitlement 的
        #      登录用户能否进入受限智能版预览 lane）。
        # 后两者均为 **业务/准入/计费判断，非策略判断**：不决定下载 key / stream
        # kinds / 水印 / R2 redirect——这 8 个策略点仍单点经 effective_policy_mode
        # （准入放行后，受限预览的水印/stream-only/跳分钟全由下游 smart_preview_mode
        # → effective_policy_mode(..., smart_preview=...) 服务端强制）。准入 gate 问的是
        # "请求模式是否为 smart"，effective_policy_mode 解析的是"模式→策略档字符串"，
        # 二者语义不同；强行套 helper 在此为恒等空操作（smart_preview=False 时透传），
        # 只会把准入判断伪装成策略判断、并在 flag 变动时埋坑，故按既有 P3e-2b 同类
        # 处理：保留直接比较、计入基线、不绕 helper。
        "gateway/job_intercept.py": 14,
        "src/utils/free_watermark.py": 0,
    }

    @pytest.mark.parametrize("rel_path", sorted(_BASELINE))
    def test_no_new_direct_service_mode_comparisons(self, rel_path):
        path = _REPO / rel_path
        actual = _count_service_mode_literal_eq(path)
        assert actual <= self._BASELINE[rel_path], (
            f"{rel_path} 的 service_mode 字面量比较从基线 "
            f"{self._BASELINE[rel_path]} 升到 {actual}——新增策略判断必须经 "
            "effective_policy_mode（plan 2026-06-12 §C AST 守卫）"
        )

    def test_job_intercept_gates_use_helper(self):
        src = (_REPO / "gateway" / "job_intercept.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn_name in ("_resolve_r2_redirect", "_resolve_r2_stream_redirect"):
            fn = next(
                n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name
            )
            calls = {
                sub.func.id if isinstance(sub.func, ast.Name) else getattr(sub.func, "attr", "")
                for sub in ast.walk(fn)
                if isinstance(sub, ast.Call)
            }
            assert "effective_policy_mode" in calls, (
                f"{fn_name} 必须经 effective_policy_mode 解析策略档"
            )

    def test_jobs_api_gates_use_helper(self):
        src = (_REPO / "src" / "services" / "jobs" / "api.py").read_text(
            encoding="utf-8"
        )
        # 下载门 / tts-zip / stream 门走 _policy_mode_for 薄包装（def + 3
        # 调用点），artifacts 过滤直接调 effective_policy_mode（1）。
        assert src.count("_policy_mode_for(") + src.count(
            "effective_policy_mode("
        ) >= 5

    def test_process_watermark_call_site_uses_helper(self):
        src = (_REPO / "src" / "pipeline" / "process.py").read_text(
            encoding="utf-8"
        )
        assert "free_watermark_text_for(effective_policy_mode(" in src.replace(
            "\n", ""
        ).replace(" ", ""), (
            "process.py 水印调用点必须传 effective_policy_mode（匿名恒水印）"
        )

    def test_sweeper_anonymous_short_circuit_present(self):
        """§C 第八点：R2 sweeper 的 is_anonymous_preview 短路保持在位。"""
        src = (_REPO / "gateway" / "r2_artifact_sweeper.py").read_text(
            encoding="utf-8"
        )
        assert "is_anonymous_preview" in src
