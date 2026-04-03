#!/usr/bin/env python3
"""Batch-label VolcEngine voice catalog with Gemini 3.1 Pro.

Reads current catalog, sends display_name + scene to Gemini in batches,
outputs a JSON file with refined age_group / persona_style / energy_level.

Usage (inside aivideotrans-app container):
    python scripts/volcengine_batch_label.py

Requires: GEMINI_API_KEY in environment.
Output: /tmp/volcengine_voice_labels.json
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from services.tts.volcengine_voice_catalog import VOICES_1_0, VOICES_2_0

BATCH_SIZE = 10  # smaller batches to avoid output truncation
OUTPUT_PATH = "/tmp/volcengine_voice_labels.json"

PROMPT_TEMPLATE = """\
你是语音合成音色标注专家。以下是一批 TTS 音色，请根据名称和场景推断每个音色的属性。

对每个音色，输出：
- age_group: "young" / "middle" / "elderly" / "child"
- persona_style: "professional" / "warm" / "serious" / "energetic" / "cute" / "neutral"
- energy_level: "low" / "medium" / "high"

判断规则：
- 名称含"少年/青年/学弟/学妹/女孩/男孩" → young
- 名称含"总裁/御姐/叔叔/姐姐/老师" → middle
- 名称含"奶奶/大爷/老伯/阿姨" → elderly
- 名称含"萌娃/童声/小朋友/佩奇/熊二" → child
- 名称含"温柔/温暖/贴心/柔美" → warm + low/medium
- 名称含"霸道/冷酷/高冷/严肃/沉稳" → serious + low
- 名称含"活泼/爽快/元气/阳光/热血" → energetic + high
- 名称含"专业/知性/解说/播音" → professional + medium
- 名称含"可爱/甜美/撒娇" → cute + medium

严格输出 JSON 数组，不要输出其他内容：
[
  {{"voice_id": "...", "age_group": "...", "persona_style": "...", "energy_level": "..."}},
  ...
]

音色列表：
{voices_json}
"""


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    all_voices = VOICES_1_0 + VOICES_2_0
    print(f"Total voices to label: {len(all_voices)}")

    # Prepare input: only send voice_id + display_name + scene
    voice_inputs = [
        {"voice_id": v["voice_id"], "display_name": v.get("display_name", ""), "scene": v.get("scene", "")}
        for v in all_voices
    ]

    # Load existing results to avoid re-labeling
    results: dict[str, dict] = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                results = json.load(f)
            print(f"Loaded {len(results)} existing labels from {OUTPUT_PATH}")
        except Exception:
            pass

    # Filter out already-labeled voices
    voice_inputs = [v for v in voice_inputs if v["voice_id"] not in results]
    print(f"Remaining to label: {len(voice_inputs)}")

    if not voice_inputs:
        print("All voices already labeled!")
        return

    batches = [voice_inputs[i:i+BATCH_SIZE] for i in range(0, len(voice_inputs), BATCH_SIZE)]

    import importlib
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
    client = genai.Client(api_key=api_key)

    for batch_idx, batch in enumerate(batches):
        print(f"Batch {batch_idx + 1}/{len(batches)}: {len(batch)} voices...")

        prompt = PROMPT_TEMPLATE.format(voices_json=json.dumps(batch, ensure_ascii=False, indent=2))

        try:
            response = client.models.generate_content(
                model="gemini-3.1-pro-preview",
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=2048,
                ),
            )

            text = response.text or ""
            if text.startswith("```"):
                import re
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            labels = json.loads(text)
            for item in labels:
                vid = item.get("voice_id", "")
                if vid:
                    results[vid] = {
                        "age_group": item.get("age_group", ""),
                        "persona_style": item.get("persona_style", ""),
                        "energy_level": item.get("energy_level", ""),
                    }

            print(f"  -> {len(labels)} labels parsed")
        except Exception as e:
            print(f"  -> ERROR: {e}")

        # Rate limit
        if batch_idx < len(batches) - 1:
            time.sleep(2)

    # Write results
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {len(results)} labels written to {OUTPUT_PATH}")
    # Summary
    labeled = len(results)
    total = len(all_voices)
    print(f"Coverage: {labeled}/{total} ({labeled/total*100:.0f}%)")


def main_targeted(voices: list[dict]) -> dict[str, dict]:
    """Label specific voices and return results as dict (no file I/O).

    ``voices``: list of dicts with at least voice_id, display_name, scene.
    Accepts both static catalog voices and dynamic DB voices.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    # Build inputs from provided metadata (no static catalog dependency)
    voice_inputs = [
        {
            "voice_id": v["voice_id"],
            "display_name": v.get("display_name", ""),
            "scene": v.get("scene", ""),
        }
        for v in voices
        if v.get("voice_id")
    ]

    if not voice_inputs:
        return {}

    import importlib
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
    client = genai.Client(api_key=api_key)

    results: dict[str, dict] = {}
    batches = [voice_inputs[i:i+BATCH_SIZE] for i in range(0, len(voice_inputs), BATCH_SIZE)]

    for batch in batches:
        prompt = PROMPT_TEMPLATE.format(voices_json=json.dumps(batch, ensure_ascii=False, indent=2))
        response = client.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=2048,
            ),
        )
        text = response.text or ""
        if text.startswith("```"):
            import re
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        labels = json.loads(text)
        for item in labels:
            vid = item.get("voice_id", "")
            if vid:
                results[vid] = {
                    "age_group": item.get("age_group", ""),
                    "persona_style": item.get("persona_style", ""),
                    "energy_level": item.get("energy_level", ""),
                }
        if len(batches) > 1:
            time.sleep(2)

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        # Targeted mode: read voices metadata from stdin JSON, output JSON to stdout
        req = json.loads(sys.stdin.read())
        voices = req.get("voices", [])
        try:
            result = main_targeted(voices)
            print(json.dumps({"ok": True, "labels": result}, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False))
    else:
        main()
