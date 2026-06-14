"""P3e-3d — 智能版预览 stream-only 策略门贯通（钱/安全：防白嫖干净成片）.

plan 2026-06-14-p3e2-preview-lane-design.md §4 / P3e-3a 残缺补全。

P3e-3a 把 ``smart_preview`` 加进 ``effective_policy_mode`` 但只用在 pipeline 水印
渲染点；Job API / Gateway 的下载/stream/artifact/materials 策略门只读
``service_mode + anonymous_preview``，**不读 smart_state.smart_preview_mode** →
登录智能版预览任务虽有烧录视频水印，但干净配音音频/字幕/素材/剪映 zip 仍可被
下载，违背 stream-only、损害「转完整」转化。

本切片把 smart_preview_mode 贯通到全部策略门（与 anonymous_preview 同档）：
- Job API：中心 _policy_mode_for 读 smart_state（→ download/stream/artifacts 门）。
- Gateway：job_intercept R2 redirect（download/stream）+ materials/background-task
  端点（**登录** smart 预览能到达，匿名登出到不了，故新增门）+ R2 sweeper 短路。
- 共享助手 gateway/preview_policy.py（gateway-safe）。

⚠️ logged-in 差异：匿名预览登出 → materials/background-task 被 auth 挡（401），
无门可镜像；smart 预览是登录免费用户 → 能到达 → **必须新增** stream-only 门。

默认 inert：非预览任务（无 smart_state.smart_preview_mode）字节级不变。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "gateway"), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub ``database`` so module-level ``from database import ...`` in sweeper /
# gateway modules doesn't build a real engine.
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

from services.r2_publisher_lib.downloadable_keys import (  # noqa: E402
    download_keys_for,
    eager_push_keys_for,
    effective_policy_mode,
    stream_kinds_for,
)
from utils.free_watermark import free_watermark_text_for  # noqa: E402


# ---------------------------------------------------------------------------
# A. smart_preview 策略链验收（镜像匿名四件套）：水印 / 零下载 / 仅 video / 不推 R2
# ---------------------------------------------------------------------------


class TestSmartPreviewPolicyChain:
    MODE = effective_policy_mode("smart", False, smart_preview=True)

    def test_smart_preview_maps_to_strictest(self):
        assert self.MODE == "anonymous_preview"

    def test_watermark_forced(self):
        assert free_watermark_text_for(self.MODE)

    def test_zero_download_keys(self):
        """🔥🔥 干净成片/音频/字幕/素材都不可下载（防白嫖）。"""
        assert download_keys_for(self.MODE) == frozenset()

    def test_stream_video_only(self):
        """🔥 仅 video stream；/stream/audio（干净配音）不可取。"""
        assert stream_kinds_for(self.MODE) == frozenset({"video"})

    def test_no_eager_r2_push(self):
        assert eager_push_keys_for(self.MODE) == frozenset()

    def test_non_preview_smart_unchanged(self):
        """🔥 inert：普通 smart（非预览）零变化——无水印、可下载成片。"""
        mode = effective_policy_mode("smart", False, smart_preview=False)
        assert free_watermark_text_for(mode) is None
        assert "publish.dubbed_video" in download_keys_for(mode)


# ---------------------------------------------------------------------------
# B. Gateway 共享助手 preview_policy（extract_smart_preview_flag / 策略门谓词）
# ---------------------------------------------------------------------------


class TestGatewayPreviewPolicyHelper:
    def test_extract_flag_true(self):
        from preview_policy import extract_smart_preview_flag

        assert extract_smart_preview_flag({"smart_preview_mode": True}) is True

    def test_extract_flag_false_variants(self):
        from preview_policy import extract_smart_preview_flag

        assert extract_smart_preview_flag(None) is False
        assert extract_smart_preview_flag({}) is False
        assert extract_smart_preview_flag({"smart_preview_mode": False}) is False
        # 非 dict（坏数据）→ False（fail-safe，不误判预览）
        assert extract_smart_preview_flag("smart_preview_mode") is False
        # strict is True：非布尔真值不算（防 JSON "false" 字符串误判）
        assert extract_smart_preview_flag({"smart_preview_mode": "true"}) is False

    def test_stream_only_predicate(self):
        from preview_policy import job_is_stream_only_preview

        smart_preview_job = SimpleNamespace(
            service_mode="smart",
            is_anonymous_preview=False,
            smart_state={"smart_preview_mode": True},
        )
        assert job_is_stream_only_preview(smart_preview_job) is True

        anon_job = SimpleNamespace(
            service_mode="express", is_anonymous_preview=True, smart_state=None
        )
        assert job_is_stream_only_preview(anon_job) is True

        normal_job = SimpleNamespace(
            service_mode="studio", is_anonymous_preview=False, smart_state=None
        )
        assert job_is_stream_only_preview(normal_job) is False

        # 普通 smart（非预览）→ 非 stream-only（可下载）
        normal_smart = SimpleNamespace(
            service_mode="smart",
            is_anonymous_preview=False,
            smart_state={"some_other_key": 1},
        )
        assert job_is_stream_only_preview(normal_smart) is False


# ---------------------------------------------------------------------------
# C. Job API 中心 _policy_mode_for 读 smart_state（→ 贯通 download/stream/artifacts）
# ---------------------------------------------------------------------------


class TestJobApiPolicyModeFor:
    def test_smart_preview_record_strictest(self):
        from services.jobs.api import _policy_mode_for

        record = SimpleNamespace(
            service_mode="smart",
            anonymous_preview=False,
            smart_state={"smart_preview_mode": True},
        )
        assert _policy_mode_for(record) == "anonymous_preview"

    def test_non_preview_record_passthrough(self):
        from services.jobs.api import _policy_mode_for

        record = SimpleNamespace(
            service_mode="smart", anonymous_preview=False, smart_state=None
        )
        assert _policy_mode_for(record) == "smart"

    def test_smart_state_without_preview_key_passthrough(self):
        from services.jobs.api import _policy_mode_for

        record = SimpleNamespace(
            service_mode="studio",
            anonymous_preview=False,
            smart_state={"smart_clone_reservation_id": "r1"},
        )
        assert _policy_mode_for(record) == "studio"


# ---------------------------------------------------------------------------
# D. Gateway R2 redirect：smart 预览任务不 redirect（download + stream）
# ---------------------------------------------------------------------------


class TestGatewayR2RedirectSmartPreview:
    def _fake_db_with_smart_preview_job(self):
        job = MagicMock()
        job.service_mode = "smart"
        job.is_anonymous_preview = False  # smart 预览是登录用户、非匿名
        job.smart_state = {"smart_preview_mode": True}
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
    async def test_download_redirect_refused_for_smart_preview(self):
        """🔥🔥 smart 预览 publish.dubbed_video 不进 R2 302（零下载 key）。"""
        import job_intercept

        url, kind = await job_intercept._resolve_r2_redirect(
            self._fake_db_with_smart_preview_job(),
            "j1",
            artifact_key="publish.dubbed_video",
        )
        assert url is None and kind == ""

    @pytest.mark.asyncio
    async def test_stream_redirect_refused_for_smart_preview(self):
        """🔥 smart 预览 video stream 也不进 R2 redirect（本地 stream-only）。"""
        import job_intercept

        url, kind = await job_intercept._resolve_r2_stream_redirect(
            self._fake_db_with_smart_preview_job(), "j1", stream_kind="video"
        )
        assert url is None and kind == ""


# ---------------------------------------------------------------------------
# E. R2 sweeper：smart 预览不 eager-push（镜像匿名短路）
# ---------------------------------------------------------------------------


def _json_rec(jianying=None):
    return SimpleNamespace(job_id="job_x", jianying_draft_zip_path=jianying)


class TestSweeperSmartPreviewSkip:
    def test_smart_preview_job_not_pushed(self):
        """🔥 smart 预览（never published, r2_artifacts=None）须短路 (False, None)。"""
        import r2_artifact_sweeper as sweeper

        should, push_keys = sweeper._classify_candidate(
            _json_rec(),
            SimpleNamespace(
                is_anonymous_preview=False,
                smart_state={"smart_preview_mode": True},
                r2_artifacts=None,
                edit_generation=0,
            ),
        )
        assert should is False and push_keys is None

    def test_non_preview_job_still_pushed(self):
        """inert：普通任务（无 smart_preview_mode）仍正常 full push。"""
        import r2_artifact_sweeper as sweeper

        should, push_keys = sweeper._classify_candidate(
            _json_rec(),
            SimpleNamespace(
                is_anonymous_preview=False,
                smart_state={"smart_clone_reservation_id": "r1"},
                r2_artifacts=None,
                edit_generation=0,
            ),
        )
        assert should is True and push_keys is None


# ---------------------------------------------------------------------------
# F. 源码级守卫：策略门接入 smart_preview / materials+task 端点新增 stream-only 门
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_job_intercept_redirects_thread_smart_preview():
    """两个 R2 redirect gate 都把 smart_preview 传进 effective_policy_mode。"""
    src = _read("gateway/job_intercept.py")
    assert src.count("smart_preview=") >= 2


def test_jobs_api_policy_mode_for_reads_smart_state():
    """_policy_mode_for 读 smart_state.smart_preview_mode + artifacts 门走中心 helper。"""
    src = _read("src/services/jobs/api.py")
    flat = " ".join(src.split())
    assert "smart_preview_mode" in flat
    # artifacts 门（原内联 effective_policy_mode）改走 _policy_mode_for → 中心化
    assert "_policy_mode_for(record) == \"anonymous_preview\"" in flat or \
        "_policy_mode_for(record)==\"anonymous_preview\"" in flat


def test_materials_api_gates_stream_only_preview():
    """materials-pack 端点新增 stream-only 预览门（登录 smart 预览可达，须挡）。"""
    src = _read("gateway/materials_api.py")
    assert "job_is_stream_only_preview(" in src


def test_background_task_api_gates_stream_only_preview():
    """background-task create + download 端点都新增 stream-only 预览门。"""
    src = _read("gateway/background_task_api.py")
    assert src.count("job_is_stream_only_preview(") >= 2


def test_sweeper_smart_preview_short_circuit_present():
    """R2 sweeper 短路保留 is_anonymous_preview 并新增 smart_preview_mode。"""
    src = _read("gateway/r2_artifact_sweeper.py")
    assert "is_anonymous_preview" in src
    assert "smart_preview_mode" in src


def test_gateway_create_stamps_smart_preview_mode_to_pg():
    """🔥🔥 对抗性/CodeX P0：gateway create 必须把 smart_preview_mode 落进 **PG
    Job.smart_state**（非只 request_data 转发给 Job API）——gateway 侧门读 PG 列，
    漏写会让预览成片在 mirror 跑前可下载/被 eager-push。smart_preview_mode 至少
    出现 2 次（request_data producer + PG Job insert）。"""
    src = _read("gateway/job_intercept.py")
    assert src.count('"smart_preview_mode"') >= 2


def test_generate_video_gated_for_stream_only_preview():
    """🔥 CodeX P1：generate-video（watermark_text=None 出无水印干净片）对预览任务
    走 _policy_mode_for 闸（绝不为预览产出无水印成片供 stream/video 取）。"""
    src = _read("src/services/jobs/api.py")
    flat = " ".join(src.split())
    # POST generate-video 处理器（"start async video mux" 注释唯一标识；区别于
    # generate-video status GET 端点），其内有 _policy_mode_for 闸。
    gv_idx = flat.find("generate-video: start async video mux")
    assert gv_idx != -1
    window = flat[gv_idx:gv_idx + 700]
    assert '_policy_mode_for(record) == "anonymous_preview"' in window


def test_draft_audio_gated_for_stream_only_preview():
    """🔥 草稿配音（干净 TTS 输出）对预览任务走 _policy_mode_for 闸（深度防御）。"""
    src = _read("src/services/jobs/api.py")
    flat = " ".join(src.split())
    da_idx = flat.find('path_parts[4] == "draft-audio"')
    assert da_idx != -1
    window = flat[da_idx:da_idx + 800]
    assert '_policy_mode_for(record) == "anonymous_preview"' in window
