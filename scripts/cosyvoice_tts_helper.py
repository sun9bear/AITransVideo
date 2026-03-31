#!/usr/bin/env python3
"""Isolated CosyVoice TTS helper — runs in its own short-lived process.

Usage:
    python scripts/cosyvoice_tts_helper.py /path/to/request.json

request.json:
    {
        "text": "要合成的文本",
        "voice": "longanyang",
        "model": "cosyvoice-v3-flash",
        "output_path": "/tmp/tts_output.wav"
    }

stdout (single JSON line):
    Success: {"ok": true, "output_path": "...", "bytes": 12345, "elapsed_ms": 1234}
    Failure: {"ok": false, "error": "...", "error_type": "CosyVoiceTTSError"}

All diagnostic/debug output goes to stderr only.
API key is read from DASHSCOPE_API_KEY env var (not passed in request).
"""

from __future__ import annotations

import json
import os
import sys
import time


def _load_env() -> None:
    """Load .env file if present (same logic as process.py)."""
    for candidate in (
        "/opt/aivideotrans/config/.env",
        os.path.join(os.path.dirname(__file__), "..", ".env"),
    ):
        if os.path.exists(candidate):
            for line in open(candidate):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() and k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip()
            break


def main() -> int:
    if len(sys.argv) != 2:
        print(
            json.dumps({"ok": False, "error": "usage: cosyvoice_tts_helper.py <request.json>", "error_type": "UsageError"}),
            flush=True,
        )
        return 1

    request_path = sys.argv[1]
    try:
        with open(request_path, "r", encoding="utf-8") as f:
            req = json.load(f)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"Failed to read request: {exc}", "error_type": type(exc).__name__}),
            flush=True,
        )
        return 1

    text = req.get("text", "")
    voice = req.get("voice", "longanyang")
    model = req.get("model", "cosyvoice-v3-flash")
    output_path = req.get("output_path", "")
    # Optional: override endpoint mode from request (used by B2 offline tools)
    req_endpoint_mode = req.get("endpoint_mode", "")

    if not text or not text.strip():
        print(json.dumps({"ok": False, "error": "text is empty", "error_type": "ValueError"}), flush=True)
        return 1
    if not output_path:
        print(json.dumps({"ok": False, "error": "output_path is required", "error_type": "ValueError"}), flush=True)
        return 1

    _load_env()

    # --- Import DashScope only inside this helper process ---
    t0 = time.monotonic()
    try:
        import dashscope
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

        # Resolve effective endpoint mode
        # Priority: request JSON > env DASHSCOPE_WS_URL (implies mode) > env DASHSCOPE_DEPLOYMENT_MODE > default
        req_mode = req_endpoint_mode.strip().lower() if req_endpoint_mode else ""
        if req_mode:
            effective_mode = "international" if req_mode in ("international", "intl") else "mainland"
            if effective_mode == "international":
                ws_url = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
            else:
                ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        else:
            ws_url = os.environ.get("DASHSCOPE_WS_URL", "").strip()
            if ws_url:
                effective_mode = "international" if "intl" in ws_url else "mainland"
            else:
                mode = os.environ.get("DASHSCOPE_DEPLOYMENT_MODE", "mainland").strip().lower()
                effective_mode = "international" if mode in ("international", "intl") else "mainland"
                if effective_mode == "international":
                    ws_url = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
                else:
                    ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        # API key selection: mode-specific key > generic fallback
        # DASHSCOPE_INTERNATIONAL_API_KEY / DASHSCOPE_MAINLAND_API_KEY > DASHSCOPE_API_KEY
        if effective_mode == "international":
            api_key = os.environ.get("DASHSCOPE_INTERNATIONAL_API_KEY", "").strip()
        else:
            api_key = os.environ.get("DASHSCOPE_MAINLAND_API_KEY", "").strip()
        if not api_key:
            api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            mode_key_name = (
                "DASHSCOPE_INTERNATIONAL_API_KEY" if effective_mode == "international"
                else "DASHSCOPE_MAINLAND_API_KEY"
            )
            raise RuntimeError(
                f"No API key for endpoint mode '{effective_mode}'. "
                f"Set {mode_key_name} or DASHSCOPE_API_KEY in env/.env"
            )

        dashscope.api_key = api_key
        dashscope.base_websocket_api_url = ws_url

        audio_format = AudioFormat.WAV_22050HZ_MONO_16BIT

        print(f"[helper] voice={voice} model={model} endpoint={ws_url}", file=sys.stderr, flush=True)

        synthesizer = SpeechSynthesizer(model=model, voice=voice, format=audio_format)
        try:
            audio = synthesizer.call(text)
        finally:
            # Best-effort close — same logic as cosyvoice_provider._close_synthesizer
            ws = getattr(synthesizer, "ws", None)
            if ws is not None:
                try:
                    setattr(ws, "keep_running", False)
                except Exception:
                    pass
                ws_close = getattr(ws, "close", None)
                if callable(ws_close):
                    try:
                        ws_close()
                    except Exception:
                        pass
            close_fn = getattr(synthesizer, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

        if audio is None:
            raise RuntimeError(
                f"DashScope SDK returned None for voice={voice}, model={model}. "
                "Voice parameter may not match the model."
            )
        if not isinstance(audio, (bytes, bytearray)):
            raise RuntimeError(
                f"DashScope SDK returned unexpected type {type(audio).__name__}"
            )

        audio_bytes = bytes(audio)
        with open(output_path, "wb") as f:
            f.write(audio_bytes)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        print(
            json.dumps({
                "ok": True,
                "output_path": output_path,
                "bytes": len(audio_bytes),
                "elapsed_ms": elapsed_ms,
            }),
            flush=True,
        )
        return 0

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        print(
            json.dumps({
                "ok": False,
                "error": str(exc)[:500],
                "error_type": type(exc).__name__,
                "elapsed_ms": elapsed_ms,
            }),
            flush=True,
        )
        print(f"[helper] error detail: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
