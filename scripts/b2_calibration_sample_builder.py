#!/usr/bin/env python3
"""B2 Calibration Sample Builder — generate uniform TTS samples for offline voice profiling.

Usage:
    python scripts/b2_calibration_sample_builder.py --output-dir data/b2_calibration_samples
    python scripts/b2_calibration_sample_builder.py --voices longanyang,longanhuan --dry-run
    python scripts/b2_calibration_sample_builder.py --output-dir /tmp/samples --voices longanyang
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Calibration scripts — emotionally neutral, phonetically varied, 50-80 chars
# ---------------------------------------------------------------------------

PRIMARY_SCRIPT: Final[str] = (
    "今天天气不错，我打算去公园散散步，顺便买点水果回来。"
    "你觉得这个周末我们一起去爬山怎么样？"
)

SECONDARY_SCRIPT: Final[str] = (
    "嗯，我觉得这个方案挺好的。"
    "不过有几个细节可能需要再讨论一下，你看什么时候方便？"
)

# ---------------------------------------------------------------------------
# B2.1 high-value candidate set
# ---------------------------------------------------------------------------

# B1 anchor voices (from _BASE_MAP + _STYLE_OVERRIDES)
_B1_ANCHORS: Final[list[str]] = [
    "longanyang", "longanhuan", "longhuhu_v3",
    "longlaobo_v3", "longanzhi_v3", "longyingjing_v3",
    "longlaoyi_v3", "longjielidou_v3", "longanwen_v3",
    "longxiaoxia_v3", "longanyun_v3", "longcheng_v3",
]

# Category representatives (1-2 per functional category not in B1 anchors)
_CATEGORY_REPS: Final[list[str]] = [
    "longshuo_v3",      # 新闻播报 male
    "loongbella_v3",    # 新闻播报 female
    "longsanshu_v3",    # 有声书 male
    "longmiao_v3",      # 有声书 female
    "longyingling_v3",  # 客服 female
    "longxiaochun_v3",  # 语音助手 female
    "longanlang_v3",    # 语音助手 male
    "longjiqi_v3",      # 短视频配音
    "longanran_v3",     # 直播带货 female
    "longfei_v3",       # 诗词朗诵 male
]

# Child voices (all matchable child voices)
_CHILD_VOICES: Final[list[str]] = [
    "longhuhu_v3", "longpaopao_v3", "longjielidou_v3",
    "longxian_v3", "longling_v3", "longshanshan_v3", "longniuniu_v3",
]

# Deduplicated union
B2_DEFAULT_CANDIDATES: Final[list[str]] = sorted(set(
    _B1_ANCHORS + _CATEGORY_REPS + _CHILD_VOICES
))

DEFAULT_MODEL: Final[str] = "cosyvoice-v3-flash"


def build_samples(
    *,
    output_dir: Path,
    voices: list[str],
    model: str = DEFAULT_MODEL,
    helper_script: Path | None = None,
    dry_run: bool = False,
    endpoint_mode: str = "",
) -> dict[str, dict]:
    """Generate calibration samples for each voice.

    Returns a manifest dict: {voice_id: {"primary": path|None, "secondary": path|None, "error": str|None}}
    """
    if helper_script is None:
        helper_script = Path(__file__).parent / "cosyvoice_tts_helper.py"

    manifest: dict[str, dict] = {}
    scripts = [("primary", PRIMARY_SCRIPT), ("secondary", SECONDARY_SCRIPT)]

    for voice_id in voices:
        voice_dir = output_dir / voice_id
        entry: dict[str, object] = {"primary": None, "secondary": None, "error": None}

        if dry_run:
            entry["primary"] = str(voice_dir / "primary.wav")
            entry["secondary"] = str(voice_dir / "secondary.wav")
            entry["dry_run"] = True
            manifest[voice_id] = entry
            print(f"[dry-run] {voice_id}: would generate 2 samples in {voice_dir}")
            continue

        voice_dir.mkdir(parents=True, exist_ok=True)

        for label, text in scripts:
            out_path = voice_dir / f"{label}.wav"
            if out_path.exists() and out_path.stat().st_size > 1000:
                entry[label] = str(out_path)
                print(f"[skip] {voice_id}/{label}.wav already exists")
                continue

            request_data = {
                "text": text,
                "voice": voice_id,
                "model": model,
                "output_path": str(out_path),
            }
            if endpoint_mode:
                request_data["endpoint_mode"] = endpoint_mode
            request_path = voice_dir / f"{label}_request.json"
            request_path.write_text(json.dumps(request_data, ensure_ascii=False), encoding="utf-8")

            try:
                result = subprocess.run(
                    [sys.executable, "-u", str(helper_script), str(request_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                response = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
                if response.get("ok"):
                    entry[label] = str(out_path)
                    print(f"[ok] {voice_id}/{label}.wav ({response.get('bytes', '?')} bytes)")
                else:
                    err = response.get("error", result.stderr[:200])
                    entry["error"] = f"{label}: {err}"
                    print(f"[fail] {voice_id}/{label}: {err}")
            except Exception as exc:
                entry["error"] = f"{label}: {type(exc).__name__}: {exc}"
                print(f"[error] {voice_id}/{label}: {exc}")
            finally:
                request_path.unlink(missing_ok=True)

            time.sleep(0.5)  # Brief pause between API calls

        manifest[voice_id] = entry

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="B2 Calibration Sample Builder")
    parser.add_argument("--output-dir", type=Path, default=Path("data/b2_calibration_samples"))
    parser.add_argument("--voices", type=str, default=None, help="Comma-separated voice IDs (default: B2.1 candidates)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true", help="Preview without generating")
    parser.add_argument("--helper", type=Path, default=None, help="Path to cosyvoice_tts_helper.py")
    parser.add_argument("--endpoint-mode", type=str, default="", help="Endpoint mode: international or mainland (default: offline config)")
    args = parser.parse_args()

    endpoint_mode = args.endpoint_mode
    if not endpoint_mode:
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent / "src"))
            from services.tts.cosyvoice_endpoint_config import get_offline_endpoint_mode
            endpoint_mode = get_offline_endpoint_mode()
        except ImportError:
            endpoint_mode = "mainland"

    voices = args.voices.split(",") if args.voices else B2_DEFAULT_CANDIDATES
    print(f"B2 Calibration Sample Builder")
    print(f"  voices: {len(voices)}")
    print(f"  output: {args.output_dir}")
    print(f"  model: {args.model}")
    print(f"  endpoint_mode: {endpoint_mode}")
    print(f"  dry_run: {args.dry_run}")
    print()

    manifest = build_samples(
        output_dir=args.output_dir,
        voices=voices,
        model=args.model,
        helper_script=args.helper,
        endpoint_mode=endpoint_mode,
        dry_run=args.dry_run,
    )

    manifest_path = args.output_dir / "manifest.json"
    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nManifest written to {manifest_path}")

    ok = sum(1 for v in manifest.values() if v.get("primary") and not v.get("error"))
    fail = sum(1 for v in manifest.values() if v.get("error"))
    print(f"Summary: {ok} ok, {fail} failed, {len(manifest)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
