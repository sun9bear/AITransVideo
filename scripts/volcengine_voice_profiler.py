#!/usr/bin/env python3
"""Phase 3: VolcEngine voice profiling via TTS synthesis + Gemini multimodal analysis.

Synthesizes 3 calibration texts per voice, sends audio to Gemini 3.1 Pro for
structured voice profiling, then merges 3 rounds into a final consensus profile.

Usage (inside aivideotrans-app container):
    python scripts/volcengine_voice_profiler.py

Requires: GEMINI_API_KEY + VOLCENGINE_TTS_APP_ID + VOLCENGINE_TTS_ACCESS_KEY
Output:
  - /tmp/volcengine_calibration_samples/{voice_id}_round{N}.wav
  - /tmp/volcengine_voice_profiles.json (final merged profiles)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from services.tts.volcengine_voice_catalog import VOICES_1_0, VOICES_2_0
from services.tts.volcengine_tts_provider import synthesize as volcengine_synthesize, RESOURCE_ID_1_0, RESOURCE_ID_2_0

# ---------------------------------------------------------------------------
# 3 calibration texts — designed for different vocal characteristics
# ---------------------------------------------------------------------------

CALIBRATION_TEXTS = {
    "zh": [
        # Round 1: Neutral narration — reveals base pitch, warmth, pace
        "今天天气不错，阳光透过窗户洒在书桌上。我拿起一本书，慢慢翻开第一页，准备开始一段安静的阅读时光。",
        # Round 2: Conversational with light emotion — reveals energy, intimacy
        "哎，你知道吗？我昨天看了一部特别好看的电影，讲的是一个小女孩和她的猫咪在城市里冒险的故事，真的好有趣！",
        # Round 3: Formal / professional — reveals authority, maturity, clarity
        "各位观众朋友们，欢迎收看今天的节目。接下来我们将为您详细介绍本次展览的三件核心展品以及它们背后的历史故事。",
    ],
    "en": [
        # Round 1: Neutral
        "The weather is quite nice today. I picked up a book from the shelf and started reading the first chapter by the window.",
        # Round 2: Conversational
        "Hey, did you hear about the new restaurant downtown? I went there last night and the food was absolutely amazing!",
        # Round 3: Formal
        "Good evening, ladies and gentlemen. Welcome to tonight's presentation. We will be discussing three key topics that are shaping the future of technology.",
    ],
}

SAMPLE_DIR = Path("/tmp/volcengine_calibration_samples")
PROFILES_PATH = Path("/tmp/volcengine_voice_profiles.json")
RAW_PROFILES_DIR = Path("/tmp/volcengine_raw_profiles")

GEMINI_PROFILE_PROMPT = """\
你是语音分析专家。听这段音频，分析说话人的声音特征。

输出严格 JSON 格式，不要输出其他内容：
{{
  "pitch_level": "low" 或 "mid" 或 "high",
  "warmth": "low" 或 "medium" 或 "high",
  "authority": "low" 或 "medium" 或 "high",
  "intimacy": "low" 或 "medium" 或 "high",
  "energy_level": "low" 或 "medium" 或 "high",
  "brightness": "low" 或 "medium" 或 "high",
  "maturity": "child" 或 "young" 或 "adult" 或 "elder",
  "delivery_style": "narration" 或 "assistant" 或 "customer_service" 或 "companion" 或 "explainer" 或 "storyteller",
  "texture_tags": ["soft", "crisp", "magnetic", "husky", "airy", "steady"],
  "childlike": true 或 false
}}

注意：
- texture_tags 可多选，从 soft/crisp/magnetic/husky/airy/steady 中选择 1-3 个最匹配的
- 根据实际听到的声音判断，不要猜测
"""


def get_language(voice: dict) -> str:
    lang = voice.get("language", "zh")
    return "en" if lang == "en" or voice["voice_id"].startswith("en_") else "zh"


def get_resource_id(voice: dict) -> str:
    return voice.get("resource_id", RESOURCE_ID_1_0)


def synthesize_calibration(voice_id: str, resource_id: str, text: str, round_num: int,
                           *, provider: str = "volcengine") -> Path | None:
    """Synthesize one calibration sample. Returns wav path or None on failure.

    Dispatches to VolcEngine or CosyVoice based on ``provider``.
    """
    out_path = SAMPLE_DIR / f"{voice_id}_round{round_num}.wav"
    if out_path.exists() and out_path.stat().st_size > 1000:
        return out_path  # already generated

    try:
        if provider == "cosyvoice":
            from services.tts.cosyvoice_provider import synthesize as cosyvoice_synthesize
            wav_bytes = cosyvoice_synthesize(
                text=text,
                voice=voice_id,
            )
        else:
            wav_bytes = volcengine_synthesize(
                text=text,
                voice_id=voice_id,
                resource_id=resource_id,
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(wav_bytes)
        return out_path
    except Exception as e:
        print(f"  SYNTH FAIL: {voice_id} round {round_num}: {e}")
        return None


def profile_audio(audio_path: Path, api_key: str, *, max_retries: int = 3) -> dict | None:
    """Send audio to Gemini for profiling. Returns profile dict or None.

    Retries on rate-limit (429) and transient errors with exponential backoff.
    """
    import importlib
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
    client = genai.Client(api_key=api_key)

    for attempt in range(max_retries + 1):
        try:
            audio_file = client.files.upload(file=audio_path)
            response = client.models.generate_content(
                model="gemini-3.1-pro-preview",
                contents=[audio_file, GEMINI_PROFILE_PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=4096,
                ),
            )

            text = response.text or ""
            import re
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            # Clean common Gemini output issues
            text = text.strip()
            # Sometimes Gemini wraps output in extra text before/after JSON
            if not text.startswith("{"):
                # Try to extract JSON object from the text
                match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
                if match:
                    text = match.group(0)

            return json.loads(text)
        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(k in err_str for k in (
                "429", "rate", "quota", "resource_exhausted", "timeout", "503",
                "expecting value", "jsondecode", "invalid",  # JSON parse errors → retry
            ))
            if is_retryable and attempt < max_retries:
                wait = min(2 ** (attempt + 1), 30)
                print(f"  PROFILE RETRY {attempt + 1}/{max_retries}: {audio_path.name} (wait {wait}s): {e}", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  PROFILE FAIL: {audio_path.name}: {e}", file=sys.stderr)
            if "expecting value" in err_str or "json" in err_str:
                print(f"    Raw response: {(response.text or '')[:200]}", file=sys.stderr)
            return None
    return None


def merge_profiles(profiles: list[dict]) -> dict:
    """Merge multiple round profiles into consensus using majority vote."""
    if not profiles:
        return {}
    if len(profiles) == 1:
        return profiles[0]

    merged = {}

    # String fields: majority vote
    for key in ["pitch_level", "warmth", "authority", "intimacy", "energy_level",
                 "brightness", "maturity", "delivery_style"]:
        values = [p.get(key, "") for p in profiles if p.get(key)]
        if values:
            merged[key] = max(set(values), key=values.count)

    # Boolean: majority
    childlike_vals = [p.get("childlike", False) for p in profiles]
    merged["childlike"] = sum(1 for v in childlike_vals if v) > len(childlike_vals) / 2

    # texture_tags: union of all, sorted by frequency
    all_tags: dict[str, int] = {}
    for p in profiles:
        for tag in (p.get("texture_tags") or []):
            all_tags[tag] = all_tags.get(tag, 0) + 1
    # Keep tags that appear in at least 2 out of 3 rounds (or all if only 1 round)
    threshold = max(1, len(profiles) // 2)
    merged["texture_tags"] = sorted(
        [tag for tag, count in all_tags.items() if count >= threshold],
        key=lambda t: all_tags[t],
        reverse=True,
    )[:3]

    return merged


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    all_voices = VOICES_1_0 + VOICES_2_0
    print(f"Total voices: {len(all_voices)}")

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing merged profiles
    final_profiles: dict[str, dict] = {}
    if PROFILES_PATH.exists():
        try:
            final_profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
            print(f"Loaded {len(final_profiles)} existing profiles")
        except Exception:
            pass

    remaining = [v for v in all_voices if v["voice_id"] not in final_profiles]
    print(f"Remaining to profile: {len(remaining)}")

    for idx, voice in enumerate(remaining):
        vid = voice["voice_id"]
        lang = get_language(voice)
        rid = get_resource_id(voice)
        texts = CALIBRATION_TEXTS[lang]

        print(f"[{idx+1}/{len(remaining)}] {vid} ({lang})...")

        round_profiles: list[dict] = []
        for round_num, text in enumerate(texts, start=1):
            # Synthesize
            wav_path = synthesize_calibration(vid, rid, text, round_num)
            if wav_path is None:
                continue

            # Profile
            raw_path = RAW_PROFILES_DIR / f"{vid}_round{round_num}.json"
            if raw_path.exists():
                try:
                    profile = json.loads(raw_path.read_text(encoding="utf-8"))
                    round_profiles.append(profile)
                    continue
                except Exception:
                    pass

            profile = profile_audio(wav_path, api_key)
            if profile:
                raw_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
                round_profiles.append(profile)

            time.sleep(1)  # rate limit

        if round_profiles:
            merged = merge_profiles(round_profiles)
            merged["_rounds"] = len(round_profiles)
            final_profiles[vid] = merged

            # Save incrementally
            PROFILES_PATH.write_text(
                json.dumps(final_profiles, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  -> {len(round_profiles)} rounds merged, {len(final_profiles)} total profiles")
        else:
            print(f"  -> SKIPPED (no successful rounds)")

        time.sleep(0.5)

    print(f"\nDone: {len(final_profiles)}/{len(all_voices)} profiles in {PROFILES_PATH}")


def main_targeted(voices: list[dict], round_name: str) -> dict[str, dict]:
    """Profile specific voices for a single round, return results (no file I/O).

    ``voices``: list of dicts with voice_id, language, provider_config.
    Accepts both static catalog voices and dynamic DB voices.
    round_name: "round1", "round2", or "round3"
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    round_idx = int(round_name.replace("round", "")) - 1  # 0,1,2
    if round_idx not in (0, 1, 2):
        raise ValueError(f"Invalid round: {round_name}")

    # Build lookup: prefer passed metadata, fallback to static catalog
    all_static = VOICES_1_0 + VOICES_2_0
    static_map = {v["voice_id"]: v for v in all_static}

    voice_list = []
    for v in voices:
        vid = v.get("voice_id", "")
        if not vid:
            continue
        # Merge: passed metadata takes precedence over static
        merged = {**static_map.get(vid, {}), **v}
        voice_list.append(merged)

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}
    for voice in voice_list:
        vid = voice["voice_id"]
        lang = voice.get("language", "zh")
        if lang != "en" and not vid.startswith("en_"):
            lang = "zh"
        elif vid.startswith("en_"):
            lang = "en"

        pc = voice.get("provider_config", {})
        rid = pc.get("resource_id") or get_resource_id(voice)
        text = CALIBRATION_TEXTS.get(lang, CALIBRATION_TEXTS["zh"])[round_idx]
        round_num = round_idx + 1

        provider = voice.get("provider", "volcengine")

        # Synthesize
        wav_path = synthesize_calibration(vid, rid, text, round_num, provider=provider)
        if wav_path is None:
            continue

        # Profile
        profile = profile_audio(wav_path, api_key)
        if profile:
            results[vid] = profile

        time.sleep(3)  # rate limit: Gemini RPM/RPD

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        # Targeted mode: read voices metadata from stdin, output JSON to stdout
        req = json.loads(sys.stdin.read())
        voices = req.get("voices", [])
        round_name = req.get("round_name", "round1")
        try:
            result = main_targeted(voices, round_name)
            print(json.dumps({"ok": True, "labels": result}, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False))
    else:
        main()
