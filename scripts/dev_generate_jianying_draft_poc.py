"""Phase 0 spike: pyJianYingDraft -> 剪映草稿生成 PoC.

This script is a research-only experiment. It does NOT enter the production
pipeline. Output is meant to be opened manually in 剪映专业版 (Jianying Pro)
to verify whether pyJianYingDraft 0.2.6 produces an openable draft.

Usage:
    # Minimal SRT-only mode (uses bundled sample SRT):
    python scripts/dev_generate_jianying_draft_poc.py \\
        --output-dir D:/tmp/jy_spike \\
        --draft-name phase_1a_min

    # With phase 1a SRT output:
    python scripts/dev_generate_jianying_draft_poc.py \\
        --output-dir D:/tmp/jy_spike \\
        --draft-name phase_1a_srt \\
        --srt path/to/output/subtitles_zh.srt

    # Full three-track (requires ffprobe-readable video and audio files):
    python scripts/dev_generate_jianying_draft_poc.py \\
        --output-dir D:/tmp/jy_spike \\
        --draft-name phase_1a_full \\
        --video path/to/source.mp4 \\
        --audio path/to/dubbed.wav \\
        --srt path/to/subtitles_zh.srt

After generation, copy the entire draft folder into 剪映 草稿目录 and open
Jianying to verify the draft is editable.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md (Phase 0)
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pyJianYingDraft as pjy


DEFAULT_SRT = """1
00:00:01,000 --> 00:00:03,444
今天我们来看第一个问题。

2
00:00:03,444 --> 00:00:06,000
这个问题涉及 LLM 推理成本。
"""


def _try_add_video(script: pjy.ScriptFile, path: Path) -> bool:
    """Try to add video track. Return True on success, False on failure
    (e.g. ffprobe / mediainfo unavailable)."""
    try:
        material = pjy.VideoMaterial(str(path))
        export = material.export_json()
        duration = export.get("duration", 0)
        if duration <= 0:
            print(f"[video] WARN: duration=0 for {path.name}, skipping")
            return False
        script.add_track(pjy.TrackType.video, track_name="video_main")
        seg = pjy.VideoSegment(
            material=material,
            target_timerange=pjy.Timerange(0, duration),
        )
        script.add_segment(seg, track_name="video_main")
        print(f"[video] added {path.name} duration={duration / pjy.SEC:.2f}s")
        return True
    except Exception as exc:
        print(f"[video] FAILED to add {path.name}: {type(exc).__name__}: {exc}")
        print("[video] hint: install ffmpeg or pymediainfo backend")
        return False


def _try_add_audio(script: pjy.ScriptFile, path: Path) -> bool:
    """Try to add audio track. Return True on success."""
    try:
        material = pjy.AudioMaterial(str(path))
        export = material.export_json()
        duration = export.get("duration", 0)
        if duration <= 0:
            print(f"[audio] WARN: duration=0 for {path.name}, skipping")
            return False
        script.add_track(pjy.TrackType.audio, track_name="dubbed_audio")
        seg = pjy.AudioSegment(
            material=material,
            target_timerange=pjy.Timerange(0, duration),
        )
        script.add_segment(seg, track_name="dubbed_audio")
        print(f"[audio] added {path.name} duration={duration / pjy.SEC:.2f}s")
        return True
    except Exception as exc:
        print(f"[audio] FAILED to add {path.name}: {type(exc).__name__}: {exc}")
        return False


def _add_subtitle(script: pjy.ScriptFile, srt_path: Path | None) -> Path:
    """Add text track + import SRT. Returns the path that was actually used
    (caller may need to clean it up if it was a tempfile)."""
    script.add_track(pjy.TrackType.text, track_name="zh_subtitle")

    if srt_path and srt_path.exists():
        script.import_srt(str(srt_path), track_name="zh_subtitle")
        print(f"[srt] imported {srt_path.name}")
        return srt_path

    # Use bundled minimal SRT
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", delete=False, encoding="utf-8",
    ) as f:
        f.write(DEFAULT_SRT)
        tmp_path = Path(f.name)
    script.import_srt(str(tmp_path), track_name="zh_subtitle")
    print(
        f"[srt] imported default minimal SRT "
        f"({DEFAULT_SRT.count('-->')} cues, {tmp_path.name})"
    )
    return tmp_path


def _inspect_draft(draft_dir: Path) -> None:
    """Print a brief structure summary of the saved draft."""
    content_path = draft_dir / "draft_content.json"
    meta_path = draft_dir / "draft_meta_info.json"

    print()
    print("=== draft_content.json ===")
    if not content_path.exists():
        print(f"  MISSING: {content_path}")
        return

    content = json.loads(content_path.read_text(encoding="utf-8"))
    print(f"  duration_us: {content.get('duration')}  "
          f"(= {content.get('duration', 0) / pjy.SEC:.2f}s)")
    print(f"  fps: {content.get('fps')}")
    canvas = content.get("canvas_config", {})
    print(f"  canvas: {canvas.get('width')}x{canvas.get('height')}")
    print(f"  draft_id: {content.get('id')}")
    print(f"  platform: {content.get('platform', {}).get('app_version', '-')}")

    for tr in content.get("tracks", []):
        print(f"  track[type={tr.get('type')!r}, "
              f"name={tr.get('name', '-')!r}]: "
              f"{len(tr.get('segments', []))} segments")

    materials = content.get("materials", {})
    for k in ("videos", "audios", "texts"):
        entries = materials.get(k, [])
        if entries:
            print(f"  materials.{k}: {len(entries)} entries")

    print()
    print("=== draft_meta_info.json ===")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"  draft_name: {meta.get('draft_name', '-')}")
        print(f"  draft_id: {meta.get('draft_id', '-')}")
        print(f"  draft_root_path: {meta.get('draft_root_path', '-')}")
        print(f"  tm_draft_create: {meta.get('tm_draft_create', '-')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pyJianYingDraft phase 0 spike — generate a sample draft",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="Drafts will be written here (acts as Jianying's draft root).",
    )
    parser.add_argument(
        "--draft-name", required=True,
        help="Subfolder name under output-dir for this draft.",
    )
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument("--audio", type=Path, default=None)
    parser.add_argument(
        "--srt", type=Path, default=None,
        help="Path to a .srt file. Defaults to a bundled 2-cue sample.",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    folder = pjy.DraftFolder(str(args.output_dir))
    if folder.has_draft(args.draft_name):
        folder.remove(args.draft_name)
        print(f"[clean] existing draft {args.draft_name!r} removed")

    script = folder.create_draft(
        draft_name=args.draft_name,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )

    video_added = False
    audio_added = False

    if args.video:
        video_added = _try_add_video(script, args.video)

    if args.audio:
        audio_added = _try_add_audio(script, args.audio)

    used_srt = _add_subtitle(script, args.srt)

    script.save()

    draft_dir = args.output_dir / args.draft_name

    if used_srt and used_srt != args.srt and used_srt.exists():
        # Clean tempfile we created
        used_srt.unlink(missing_ok=True)

    print()
    print(f"[done] draft saved to {draft_dir}")
    print(f"  video_track: {video_added}")
    print(f"  audio_track: {audio_added}")
    print(f"  text_track:  True")

    _inspect_draft(draft_dir)

    print()
    print("Next steps for manual verification:")
    print(f"  1. Open Jianying / 剪映专业版.")
    print(r"  2. Copy the folder " + str(draft_dir))
    print(r"     into the Jianying drafts directory:")
    print(r"     %LocalAppData%\JianyingPro\User Data\Projects\com.lveditor.draft\\")
    print(r"  3. Restart Jianying or refresh the drafts list.")
    print(r"  4. Open the draft and verify all tracks show up + are editable.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
