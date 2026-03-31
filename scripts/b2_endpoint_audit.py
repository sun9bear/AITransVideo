#!/usr/bin/env python3
"""B2 dual-endpoint voice availability audit.

Probes each B2 candidate voice on both intl and mainland DashScope endpoints
to determine TTS availability. Outputs a structured JSON report.

Usage (inside container):
    python3 scripts/b2_endpoint_audit.py --output /opt/aivideotrans/data/b2_endpoint_audit.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PROBE_TEXT = "你好"
MODEL = "cosyvoice-v3-flash"
INTL_WS = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
MAINLAND_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

B2_CANDIDATES = [
    "longanhuan", "longanlang_v3", "longanran_v3", "longanwen_v3",
    "longanyang", "longanyun_v3", "longanzhi_v3", "longcheng_v3",
    "longfei_v3", "longhuhu_v3", "longjielidou_v3", "longjiqi_v3",
    "longlaobo_v3", "longlaoyi_v3", "longling_v3", "longmiao_v3",
    "longniuniu_v3", "longpaopao_v3", "longsanshu_v3", "longshanshan_v3",
    "longshuo_v3", "longxian_v3", "longxiaochun_v3", "longxiaoxia_v3",
    "longyingjing_v3", "longyingling_v3", "loongbella_v3",
]


def _load_env():
    env_path = "/opt/aivideotrans/config/.env"
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()


def probe_voice(voice_id: str, api_key: str, ws_url: str) -> dict:
    """Probe a single voice. Returns {ok, bytes, error_code, error_message, elapsed_ms}."""
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    dashscope.api_key = api_key
    dashscope.base_websocket_api_url = ws_url

    result = {"ok": False, "bytes": 0, "error_code": None, "error_message": None, "elapsed_ms": 0}
    synth = None
    try:
        t0 = time.monotonic()
        synth = SpeechSynthesizer(model=MODEL, voice=voice_id, format=AudioFormat.WAV_22050HZ_MONO_16BIT)
        audio = synth.call(PROBE_TEXT)
        elapsed = int((time.monotonic() - t0) * 1000)
        result["elapsed_ms"] = elapsed
        if audio and isinstance(audio, (bytes, bytearray)):
            result["ok"] = True
            result["bytes"] = len(audio)
        else:
            result["error_message"] = "audio is None"
    except Exception as exc:
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        err_str = str(exc)
        import re
        code_m = re.search(r'"error_code"\s*:\s*"([^"]+)"', err_str)
        msg_m = re.search(r'"error_message"\s*:\s*"([^"]+)"', err_str)
        if code_m:
            result["error_code"] = code_m.group(1)
        if msg_m:
            result["error_message"] = msg_m.group(1)
        if not result["error_message"]:
            result["error_message"] = f"{type(exc).__name__}: {err_str[:100]}"
    finally:
        if synth:
            ws = getattr(synth, "ws", None)
            if ws:
                try:
                    setattr(ws, "keep_running", False)
                except Exception:
                    pass
                close_fn = getattr(ws, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("b2_endpoint_audit.json"))
    args = parser.parse_args()

    _load_env()
    intl_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    mainland_key = os.environ.get("B0_MAINLAND_KEY", "").strip()

    if not intl_key:
        print("ERROR: DASHSCOPE_API_KEY not set", file=sys.stderr)
        return 1
    if not mainland_key:
        print("ERROR: B0_MAINLAND_KEY not set", file=sys.stderr)
        return 1

    print(f"Audit: {len(B2_CANDIDATES)} voices x 2 endpoints")
    print(f"  intl_key: {intl_key[:8]}***")
    print(f"  mainland_key: {mainland_key[:8]}***")
    print(f"  model: {MODEL}")
    print(f"  probe_text: {PROBE_TEXT!r}")
    print()

    results = {}
    for i, voice_id in enumerate(B2_CANDIDATES):
        print(f"[{i+1}/{len(B2_CANDIDATES)}] {voice_id}...", end=" ", flush=True)

        intl_r = probe_voice(voice_id, intl_key, INTL_WS)
        time.sleep(0.5)
        mainland_r = probe_voice(voice_id, mainland_key, MAINLAND_WS)
        time.sleep(0.5)

        intl_status = "OK" if intl_r["ok"] else intl_r.get("error_code") or "FAIL"
        mainland_status = "OK" if mainland_r["ok"] else mainland_r.get("error_code") or "FAIL"
        print(f"intl={intl_status} mainland={mainland_status}")

        results[voice_id] = {"intl": intl_r, "mainland": mainland_r}

    # Summary
    intl_ok = sum(1 for v in results.values() if v["intl"]["ok"])
    mainland_ok = sum(1 for v in results.values() if v["mainland"]["ok"])
    print(f"\nSummary: intl={intl_ok}/{len(B2_CANDIDATES)}, mainland={mainland_ok}/{len(B2_CANDIDATES)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
