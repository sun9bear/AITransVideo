"""P3e-3b — 智能版 3min 预览 teaser 裁剪（consumer 侧 / 钱-关键耦合）.

plan 2026-06-14-p3e2-preview-lane-design.md §4 + docs/plans/2026-06-14-p3e3-HANDOFF.md §4。

把登录智能版预览任务（``job_smart_preview=True``）的源**音频**在 separation/ASR/
clone/TTS 等付费阶段**之前**裁剪到 ~180s teaser。音频 teaser 同时驱动两条边界：
①付费 AI 阶段读 teaser 音频（分离→ASR→克隆→TTS 全部有界 3 分钟）；
②``actual_duration_ms = _ffprobe_duration_ms(source_audio_path)`` → 经
EditorPackageWriter 的 base-silence 时长 + render ``-shortest`` 把成片有界到 3 分钟。
**视频不裁**（原 original.mp4 保留 + render 经 -shortest 收口）——避免污染 artifact
index 的 ``source.original_video``（供 P3e-3c-2 转完整复用原视频）。

⚠️ 钱-关键耦合：本步**仍扣分钟**（create reserve 不变 → settle 照常 = 无漏收），
只把工作/产物限到 3 分钟。后续 P3e-3c 跳分钟**必须**在本步之后，否则 pipeline 跑
完整视频却不收分钟 = 免费完整任务（漏收全部分钟点 ≫ 克隆 600）。

**fail-closed**：任何裁剪失败 → 抛 ``SmartPreviewTeaserError`` 使任务 terminal
failed，**绝不**退回处理完整源。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from utils.smart_preview_teaser import (  # noqa: E402
    SMART_PREVIEW_TEASER_SECONDS,
    SmartPreviewTeaserError,
    apply_smart_preview_teaser,
    build_teaser_ffmpeg_cmd,
    smart_preview_gemini_url_unbounded,
    trim_to_teaser,
)

_PROCESS_PY = _SRC / "pipeline" / "process.py"


# ---------------------------------------------------------------------------
# Fakes — 注入 runner/prober，绝不真跑 ffmpeg/ffprobe。
# ---------------------------------------------------------------------------


def _runner_creates_dest(rc: int = 0):
    """成功桩：rc=0 且在 cmd[-1]（dest）落一个占位文件。"""

    def _run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"teaser-bytes")
        return subprocess.CompletedProcess(cmd, rc)

    return _run


def _runner_rc(rc: int):
    """退出码桩：返回指定 rc，不落文件。"""

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc)

    return _run


def _runner_raises(exc: BaseException):
    def _run(cmd, **kwargs):
        raise exc

    return _run


def _prober_const(value):
    return lambda _path: value


# ---------------------------------------------------------------------------
# 常量：smart 预览复用匿名 180s teaser
# ---------------------------------------------------------------------------


def test_teaser_seconds_mirrors_anonymous_180():
    """🔥 smart 预览 teaser 复用匿名 anonymous_preview_max_seconds 默认 180s。"""
    assert SMART_PREVIEW_TEASER_SECONDS == 180.0


# ---------------------------------------------------------------------------
# build_teaser_ffmpeg_cmd（纯函数）：-t 有界时长（audio-only）
# ---------------------------------------------------------------------------


def test_audio_cmd_bounds_duration(tmp_path):
    """🔥 audio teaser 命令带 -t <cap>（有界时长）且丢弃视频流（-vn）。"""
    cmd = build_teaser_ffmpeg_cmd(
        tmp_path / "original.wav",
        tmp_path / "preview_teaser.wav",
        max_seconds=180.0,
    )
    assert "-t" in cmd
    ti = cmd.index("-t")
    assert cmd[ti + 1] == "180.0"
    assert "-vn" in cmd
    # dest 始终是最后一个参数（runner 桩据此定位产物）
    assert cmd[-1] == str(tmp_path / "preview_teaser.wav")


# ---------------------------------------------------------------------------
# trim_to_teaser fail-closed 矩阵（钱-correctness 核心）
# ---------------------------------------------------------------------------


def test_trim_fail_closed_on_nonzero_exit(tmp_path):
    """🔥 ffmpeg 非零退出 → fail-closed（绝不当成功放行完整源）。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_rc(1),
            prober=_prober_const(10.0),
        )


def test_trim_fail_closed_on_ffmpeg_missing(tmp_path):
    """🔥 ffmpeg 不在 PATH（FileNotFoundError）→ fail-closed。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_raises(FileNotFoundError()),
            prober=_prober_const(10.0),
        )


def test_trim_fail_closed_on_timeout(tmp_path):
    """🔥 ffmpeg 超时 → fail-closed。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_raises(subprocess.TimeoutExpired("ffmpeg", 600.0)),
            prober=_prober_const(10.0),
        )


def test_trim_fail_closed_when_dest_missing(tmp_path):
    """🔥 ffmpeg rc=0 但产物未生成 → fail-closed（不当成功）。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_rc(0),  # 不落文件
            prober=_prober_const(10.0),
        )


def test_trim_fail_closed_when_duration_over_cap(tmp_path):
    """🔥🔥 ffmpeg "成功"但产物时长越界（-t 未生效/源未被裁）→ fail-closed。
    这是最关键的钱-防线：teaser 必须真的有界，否则就是未裁剪的完整源。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_creates_dest(0),
            prober=_prober_const(999.0),  # 远超 180+容差
        )


def test_trim_fail_closed_when_duration_unprobeable(tmp_path):
    """🔥 产物时长探测失败（prober 返回 None）→ fail-closed（不可信即拒）。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            tmp_path / "preview_teaser.wav",
            max_seconds=180.0,
            runner=_runner_creates_dest(0),
            prober=_prober_const(None),
        )


def test_trim_fail_closed_when_src_equals_dest(tmp_path):
    """🔥 src == dest → fail-closed（防 ffmpeg -y clobber 原文件；CodeX P3）。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    with pytest.raises(SmartPreviewTeaserError):
        trim_to_teaser(
            src,
            src,  # 同路径
            max_seconds=180.0,
            runner=_runner_creates_dest(0),
            prober=_prober_const(170.0),
        )


def test_trim_success_returns_actual_duration(tmp_path):
    """成功：产物存在且时长 ≤ cap+容差 → 返回真实时长。"""
    src = tmp_path / "original.wav"
    src.write_bytes(b"x")
    dur = trim_to_teaser(
        src,
        tmp_path / "preview_teaser.wav",
        max_seconds=180.0,
        runner=_runner_creates_dest(0),
        prober=_prober_const(178.5),
    )
    assert dur == pytest.approx(178.5)
    assert (tmp_path / "preview_teaser.wav").exists()


# ---------------------------------------------------------------------------
# apply_smart_preview_teaser：只裁音频 + 保留原文件 + fail-closed
# ---------------------------------------------------------------------------


def _seed_project(tmp_path, *, with_video=True, with_audio=True):
    proj = tmp_path / "proj"
    (proj / "video").mkdir(parents=True)
    (proj / "audio").mkdir(parents=True)
    video = proj / "video" / "original.mp4"
    audio = proj / "audio" / "original.wav"
    if with_video:
        video.write_bytes(b"full-video")
    if with_audio:
        audio.write_bytes(b"full-audio")
    return proj, video, audio


def test_apply_returns_teaser_audio_distinct_from_source(tmp_path):
    """🔥🔥 钱-关键：返回的 teaser 音频路径与源不同（新文件），源 original.wav 保留。"""
    proj, _video, audio = _seed_project(tmp_path)
    teaser_audio = apply_smart_preview_teaser(
        source_audio_path=audio,
        project_dir=proj,
        runner=_runner_creates_dest(0),
        prober=_prober_const(176.0),
    )
    assert teaser_audio != audio
    assert teaser_audio.exists()
    # 原文件原封不动
    assert audio.exists() and audio.read_bytes() == b"full-audio"


def test_apply_does_not_touch_video(tmp_path):
    """🔥 只裁音频：原 original.mp4 不被读/裁/改（视频留原供 source.original_video
    + render -shortest 收口），runner 只被调一次（audio）。"""
    proj, video, audio = _seed_project(tmp_path)
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"t")
        return subprocess.CompletedProcess(cmd, 0)

    apply_smart_preview_teaser(
        source_audio_path=audio,
        project_dir=proj,
        runner=_run,
        prober=_prober_const(175.0),
    )
    assert len(calls) == 1  # 只裁音频
    assert "-vn" in calls[0]
    # 原视频原封不动
    assert video.exists() and video.read_bytes() == b"full-video"
    # 没有 preview_teaser.mp4 被创建
    assert not (proj / "video" / "preview_teaser.mp4").exists()


def test_apply_writes_canonical_teaser_audio_name(tmp_path):
    """teaser 音频落盘到 audio/preview_teaser.wav（resume 路径据此回找）。"""
    proj, _video, audio = _seed_project(tmp_path)
    teaser_audio = apply_smart_preview_teaser(
        source_audio_path=audio,
        project_dir=proj,
        runner=_runner_creates_dest(0),
        prober=_prober_const(177.0),
    )
    assert teaser_audio == (proj / "audio" / "preview_teaser.wav")


def test_apply_fail_closed_when_audio_trim_fails(tmp_path):
    """🔥🔥 音频裁剪失败 → fail-closed（音频边界=AI 工作量 + base-silence 边界）。"""
    proj, _video, audio = _seed_project(tmp_path)
    with pytest.raises(SmartPreviewTeaserError):
        apply_smart_preview_teaser(
            source_audio_path=audio,
            project_dir=proj,
            runner=_runner_rc(1),
            prober=_prober_const(10.0),
        )


def test_apply_fail_closed_when_audio_missing(tmp_path):
    """🔥 源音频缺失 → fail-closed（无法有界 ASR/TTS/base-silence）。"""
    proj, _video, audio = _seed_project(tmp_path, with_audio=False)
    with pytest.raises(SmartPreviewTeaserError):
        apply_smart_preview_teaser(
            source_audio_path=audio,
            project_dir=proj,
            runner=_runner_creates_dest(0),
            prober=_prober_const(10.0),
        )


# ---------------------------------------------------------------------------
# smart_preview_gemini_url_unbounded（纯守卫谓词）：gemini-on-URL 无法被本地
# teaser 有界 → caller fail-closed。
# ---------------------------------------------------------------------------


def test_gemini_url_is_unbounded():
    """🔥 gemini + 非空 URL → True（本地 teaser 无法有界，需 fail-closed）。"""
    assert smart_preview_gemini_url_unbounded("gemini", "https://youtu.be/abc") is True


def test_gemini_local_upload_is_bounded():
    """gemini + 空 URL（本地上传 normalized_url="")→ False（实际走 assemblyai/本地）。"""
    assert smart_preview_gemini_url_unbounded("gemini", "") is False
    assert smart_preview_gemini_url_unbounded("gemini", "   ") is False


def test_assemblyai_is_bounded():
    """assemblyai（读本地 speech_audio_path）→ False（teaser 已有界）。"""
    assert smart_preview_gemini_url_unbounded("assemblyai", "https://youtu.be/abc") is False
    assert smart_preview_gemini_url_unbounded(None, "https://youtu.be/abc") is False


# ---------------------------------------------------------------------------
# process.py source-scan：teaser 接线 + 钱-关键放置 + gemini 守卫 + resume 有界
# ---------------------------------------------------------------------------


def _proc_src() -> str:
    return _PROCESS_PY.read_text(encoding="utf-8")


def test_pipeline_applies_audio_teaser_under_smart_preview_guard():
    """pipeline 在 if job_smart_preview 下调 apply_smart_preview_teaser 并 repoint
    source_audio_path（只裁音频；video_path 不动 → source.original_video 不被污染）。
    默认 inert：非预览任务不触发。"""
    flat = " ".join(_proc_src().split())
    assert "if job_smart_preview:" in flat
    assert "source_audio_path = apply_smart_preview_teaser(" in flat


def test_pipeline_does_not_repoint_video_path_for_teaser():
    """🔥 video_path 绝不被 teaser repoint（保 artifact index source.original_video
    指向真原视频，供 P3e-3c-2 转完整复用）。"""
    flat = " ".join(_proc_src().split())
    # 不得出现把 apply 返回赋给 video_path 的写法
    assert "video_path, source_audio_path = apply_smart_preview_teaser(" not in flat
    assert "video_path = apply_smart_preview_teaser(" not in flat


def test_teaser_trim_before_separation():
    """🔥🔥 钱-关键放置：teaser 裁剪必须在 _ensure_separated_audio_assets（→ASR→
    TTS）**之前**——否则付费工作已发生，有界无意义。"""
    src = _proc_src()
    teaser_idx = src.find("apply_smart_preview_teaser(")
    sep_idx = src.find("_ensure_separated_audio_assets(")
    assert teaser_idx != -1 and sep_idx != -1
    assert teaser_idx < sep_idx, "teaser 裁剪必须在音频分离/ASR 之前"


def test_gemini_on_url_fail_closed_guard_wired():
    """smart 预览 + gemini-on-URL → fail-closed 守卫已接线（本地 teaser 无法有界
    URL 转录）。"""
    flat = " ".join(_proc_src().split())
    assert "smart_preview_gemini_url_unbounded(" in flat


def test_resume_publish_prefers_teaser_audio():
    """🔥🔥 resume/publish-only（Studio 编辑-提交）必须优先用 preview_teaser.wav 的
    时长造 base-silence——否则预览任务被编辑提交会用完整时长出满长片 = 完整任务
    （漏收分钟点，CodeX/对抗性 P1）。"""
    flat = " ".join(_proc_src().split())
    assert '"audio" / "preview_teaser.wav"' in flat or "'audio' / 'preview_teaser.wav'" in flat
