#!/usr/bin/env python3
"""B2 Gemini Voice Profiler — batch-label calibration samples with Gemini multimodal.

Usage:
    python scripts/b2_gemini_voice_profiler.py --samples-dir data/b2_calibration_samples
    python scripts/b2_gemini_voice_profiler.py --samples-dir data/b2_calibration_samples --voice longanyang
    python scripts/b2_gemini_voice_profiler.py --samples-dir data/b2_calibration_samples --dry-run

Reads calibration WAV files, sends each to Gemini for voice-side profiling,
writes results to a JSON profile catalog.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Gemini prompt template for voice profiling
# ---------------------------------------------------------------------------

PROFILING_PROMPT: Final[str] = """你是一个语音音色分析专家。下面是同一个 CosyVoice TTS 音色合成的校准音频（可能有 1-2 段）。
请综合分析这些音频的声音特征，按以下维度给出评估。

## Primary Labels（主要特征，用于音色排序）
- pitch_level: 音高水平（low / mid / high）
- warmth: 温暖感（low / medium / high）
- authority: 权威感（low / medium / high）
- intimacy: 亲近感（low / medium / high）

## Secondary Labels（辅助特征，用于一致性校验）
- energy_level: 能量水平（low / medium / high）
- brightness: 声音明亮度（low / medium / high）
- maturity: 声音成熟度（child / young / adult / elder）
- delivery_style: 最接近的播报风格，从以下选一个：narration / assistant / customer_service / companion / explainer / storyteller
- texture_tags: 声音质地标签，从以下选 1-3 个：soft / crisp / magnetic / husky / airy / steady
- childlike: 是否具有明显的儿童声特征（true / false）

请只输出 JSON，不要输出其他内容。格式如下：
{"primary": {"pitch_level": "...", "warmth": "...", "authority": "...", "intimacy": "..."}, "secondary": {"energy_level": "...", "brightness": "...", "maturity": "...", "delivery_style": "...", "texture_tags": ["...", "..."], "childlike": false}}"""


def profile_voice_with_gemini(
    audio_paths: list[Path],
    *,
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
) -> dict | None:
    """Send calibration audio file(s) to Gemini for voice profiling.

    Accepts 1-2 audio paths (primary, optional secondary). All are sent
    in a single multimodal request so Gemini evaluates the voice holistically.

    Returns parsed JSON profile or None on failure.
    """
    try:
        from google import genai
    except ImportError:
        print("[error] google-genai package not installed", file=sys.stderr)
        return None

    key = api_key or __import__("os").environ.get("GEMINI_API_KEY", "")
    if not key:
        print("[error] GEMINI_API_KEY not set", file=sys.stderr)
        return None

    from services.gemini.client_factory import create_gemini_client
    client = create_gemini_client(api_key=key)
    mime_type = "audio/wav"

    contents: list = []
    for ap in audio_paths:
        contents.append(genai.types.Part.from_bytes(data=ap.read_bytes(), mime_type=mime_type))
    contents.append(PROFILING_PROMPT)

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
        )
        text = response.text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        return json.loads(text)
    except Exception as exc:
        print(f"[error] Gemini call failed: {exc}", file=sys.stderr)
        return None


def run_batch_profiling(
    *,
    samples_dir: Path,
    output_path: Path,
    voices: list[str] | None = None,
    dry_run: bool = False,
    model: str = "gemini-2.5-flash",
) -> dict[str, dict]:
    """Profile all voices in the samples directory.

    Returns a catalog dict: {voice_id: {primary: {...}, secondary: {...}, ...}}
    """
    manifest_path = samples_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        # Discover from directory structure
        manifest = {}
        for voice_dir in sorted(samples_dir.iterdir()):
            if voice_dir.is_dir() and (voice_dir / "primary.wav").exists():
                manifest[voice_dir.name] = {"primary": str(voice_dir / "primary.wav")}

    target_voices = voices if voices else sorted(manifest.keys())
    catalog: dict[str, dict] = {}

    # Load existing catalog if present
    if output_path.exists():
        try:
            catalog = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    for voice_id in target_voices:
        if voice_id in catalog and not dry_run:
            print(f"[skip] {voice_id}: already profiled")
            continue

        entry = manifest.get(voice_id, {})
        primary_path = entry.get("primary")
        if not primary_path or not Path(primary_path).exists():
            print(f"[skip] {voice_id}: no primary sample found")
            continue

        # Collect available audio paths (primary required, secondary optional)
        audio_paths = [Path(primary_path)]
        secondary_path = entry.get("secondary")
        if secondary_path and Path(secondary_path).exists():
            audio_paths.append(Path(secondary_path))

        if dry_run:
            labels = [p.name for p in audio_paths]
            print(f"[dry-run] {voice_id}: would profile {', '.join(labels)}")
            continue

        print(f"[profile] {voice_id} ({len(audio_paths)} sample(s))...")
        result = profile_voice_with_gemini(audio_paths, model=model)
        if result:
            result["voice_id"] = voice_id
            result["labeled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            result["labeled_by"] = model
            catalog[voice_id] = result
            print(f"  -> {json.dumps(result.get('primary', {}))}")

            # Save incrementally
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            print(f"  -> FAILED")

        time.sleep(2)  # Rate limit

    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="B2 Gemini Voice Profiler")
    parser.add_argument("--samples-dir", type=Path, default=Path("data/b2_calibration_samples"))
    parser.add_argument("--output", type=Path, default=Path("data/b2_voice_profiles.json"))
    parser.add_argument("--voice", type=str, default=None, help="Profile a single voice")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    voices = [args.voice] if args.voice else None

    catalog = run_batch_profiling(
        samples_dir=args.samples_dir,
        output_path=args.output,
        voices=voices,
        dry_run=args.dry_run,
        model=args.model,
    )

    print(f"\nTotal profiled: {len(catalog)} voices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
