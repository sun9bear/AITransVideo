"""Phase 2a Task 8 (gate #8) — free-tier video watermark (ffmpeg drawtext).

Mock-only — never runs ffmpeg. Tests the free->watermark policy, the drawtext
filter shape, and VideoRenderer._build_render_command (watermark forces a
re-encode + injects drawtext; paid modes keep the lossless -c:v copy mux), plus
static guards for the process.py / dispatcher wiring.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from utils.free_watermark import (  # noqa: E402
    FREE_WATERMARK_TEXT,
    build_drawtext_filter,
    free_watermark_text_for,
)
from modules.output.output_models import OutputRequest  # noqa: E402
from modules.output.publish.publish_models import PublishRequest  # noqa: E402
from modules.output.publish.video_renderer import VideoRenderer  # noqa: E402


# --- policy: only free is watermarked ---

def test_free_mode_gets_watermark_text():
    assert free_watermark_text_for("free") == FREE_WATERMARK_TEXT
    assert FREE_WATERMARK_TEXT  # non-empty default


def test_paid_modes_get_no_watermark():
    for mode in ("express", "studio", "smart", "", None):
        assert free_watermark_text_for(mode) is None


# --- drawtext filter shape ---

def test_drawtext_filter_has_text_and_bottom_right_position():
    f = build_drawtext_filter("BrandX")
    assert f.startswith("drawtext=")
    assert "text='BrandX'" in f
    assert "x=w-tw-20" in f and "y=h-th-20" in f  # bottom-right, padded
    assert "fontfile=" not in f  # none given


def test_drawtext_filter_embeds_fontfile_when_given():
    f = build_drawtext_filter("BrandX", fontfile="/fonts/x.ttf")
    assert "fontfile='/fonts/x.ttf'" in f


# --- command builder: watermark forces re-encode, paid keeps copy ---

def _cmd(**kw):
    renderer = VideoRenderer(command_runner=lambda cmd: None)
    defaults = dict(
        original_video_path=Path("/v.mp4"),
        dubbed_audio_path=Path("/a.wav"),
        ambient_audio_path=None,
        output_path=Path("/out.mp4"),
        watermark_text=None,
        ambient_volume_db=-12.0,
    )
    defaults.update(kw)
    return renderer._build_render_command(**defaults)


def test_no_watermark_two_track_copies_video():
    cmd = " ".join(_cmd())
    assert "-c:v copy" in cmd
    assert "drawtext" not in cmd
    assert "libx264" not in cmd


def test_watermark_two_track_reencodes_with_drawtext():
    cmd = " ".join(_cmd(watermark_text="BrandX"))
    assert "drawtext=" in cmd
    assert "libx264" in cmd
    assert "-c:v copy" not in cmd
    assert "[vout]" in cmd  # video routed through the filter output


def test_no_watermark_three_track_copies_video():
    cmd = " ".join(_cmd(ambient_audio_path=Path("/amb.wav")))
    assert "-c:v copy" in cmd
    assert "amix=inputs=2" in cmd  # ambient mix preserved
    assert "drawtext" not in cmd


def test_watermark_three_track_reencodes_and_keeps_ambient():
    cmd = " ".join(_cmd(watermark_text="BrandX", ambient_audio_path=Path("/amb.wav")))
    assert "drawtext=" in cmd
    assert "libx264" in cmd
    assert "-c:v copy" not in cmd
    assert "amix=inputs=2" in cmd  # ambient mix still there
    assert "[0:v]drawtext" in cmd  # drawtext applied to the video chain


# --- model fields carry the watermark ---

def test_request_models_carry_watermark_text():
    pr = PublishRequest(
        project_id="p", original_video_path="v", dubbed_audio_path="a",
        output_dir="o", watermark_text="WM",
    )
    assert pr.watermark_text == "WM"
    assert OutputRequest(watermark_text="WM").watermark_text == "WM"
    # default is clean (no watermark)
    assert OutputRequest().watermark_text is None


# --- static wiring guards ---

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_process_wires_free_watermark_at_publish():
    """plan 2026-06-12 §C ①：水印档位经 effective_policy_mode——匿名预览
    （含匿名 express）恒水印；非匿名任务仍按 job_service_mode。
    P3e-3：智能版 3min 预览（job_smart_preview）也经同一 helper 恒水印。"""
    src = _read("src/pipeline/process.py")
    compact = src.replace("\n", "").replace(" ", "")
    assert (
        "free_watermark_text_for(effective_policy_mode(job_service_mode,job_anonymous_preview,smart_preview=job_smart_preview,))"
        in compact
    ), "run() publish must derive the watermark via effective_policy_mode（含 P3e-3 smart_preview）"


def test_dispatcher_forwards_watermark_to_publish_request():
    src = _read("src/modules/output/output_dispatcher.py")
    assert "watermark_text=request.watermark_text" in src
