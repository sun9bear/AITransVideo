#!/usr/bin/env python3
"""MiniMax English voice synthesizing Chinese text — usability test.

Goal
----
MiniMax Speech 2.8 officially claims cross-language synthesis capability
(one voice -> any of 32 languages). Empirically verify whether Chinese
text synthesized with an English-labelled voice:
  (a) produces audio at all (technical success), and
  (b) the audio is recognisable Chinese (subjective usability).

Method
------
Call speech-2.8-turbo with 2-3 English voice IDs + T_ZH Chinese text.
Record: bytes produced, duration, error (if any). Save audio to /tmp/
for manual listen-check. This script can only verify (a); (b) requires
the user to listen.

Output: audio files at /tmp/minimax_en_voice_zh_test_*.mp3, plus CSV.

Safety
------
Paid API. Default --dry-run. Cost: ~CNY 0.05 (2-3 calls x ~124 billed).

Environment
-----------
AUTODUB_TTS_API_KEY must be set (same env as production MiniMax path).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib import error, request

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


T_ZH = (
    "这款手机的屏幕素质让我很震惊。"
    "色彩通透，对比度极高，黑色几乎和关屏没有区别。"
    "最让我意外的是功耗居然还降低了，续航多了将近两小时。"
)

# MiniMax "English" voices from voice_catalog to test.
# Two samples across persona: narrator vs conversational.
TEST_VOICES: list[str] = [
    "English_ReservedMan",     # narrator-ish, deep voice
    "English_Upbeat_Woman",    # upbeat female
]

DEFAULT_MODEL = "speech-2.8-turbo"
DEFAULT_ENDPOINT = "https://api.minimaxi.com/v1/t2a_v2"
DEFAULT_TIMEOUT = 60.0


def _count_hanzi(text: str) -> int:
    return sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)


def _build_payload(text: str, voice_id: str, model: str) -> dict:
    """MiniMax t2a_v2 payload, identical to production minus speed override."""
    return {
        "model": model,
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1.0,
            "vol": 1.0,
        },
        "audio_setting": {
            "format": "mp3",       # mp3 is easier to listen-check locally
            "sample_rate": 24000,
        },
    }


def _synthesize_once(
    text: str,
    voice_id: str,
    *,
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> tuple[bytes, str, str]:
    """Return (audio_bytes_b64_decoded, trace_id, error_message)."""
    import base64

    api_key = os.environ.get("AUTODUB_TTS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AUTODUB_TTS_API_KEY env var not set")

    payload = _build_payload(text, voice_id, model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(endpoint, data=body, headers=headers, method="POST")

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            status = resp.getcode()
    except error.HTTPError as exc:
        return b"", "", f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}"
    except Exception as exc:
        return b"", "", f"request failed: {exc}"

    if status != 200:
        return b"", "", f"non-200 status: {status}"

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return b"", "", f"invalid JSON: {exc}"

    base_resp = data.get("base_resp", {})
    status_code = base_resp.get("status_code", -1)
    trace_id = data.get("trace_id", "")

    if status_code != 0:
        return b"", trace_id, f"status_code={status_code} msg={base_resp.get('status_msg', '')!r}"

    audio_hex = data.get("data", {}).get("audio", "")
    if not audio_hex:
        return b"", trace_id, "no audio in response"

    try:
        audio_bytes = bytes.fromhex(audio_hex)
    except ValueError:
        # Some responses may be base64 rather than hex
        try:
            audio_bytes = base64.b64decode(audio_hex)
        except Exception as exc:
            return b"", trace_id, f"cannot decode audio: {exc}"

    if not audio_bytes:
        return b"", trace_id, "decoded audio empty"

    return audio_bytes, trace_id, ""


def _default_dir() -> Path:
    linux_tmp = Path("/tmp")
    return linux_tmp if linux_tmp.is_dir() else Path(tempfile.gettempdir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--voices", nargs="*", default=None,
                        help=f"Override voice list. Default: {TEST_VOICES}")
    parser.add_argument("--sleep", type=float, default=2.0)
    args = parser.parse_args(argv)

    voices = args.voices or TEST_VOICES
    hanzi = _count_hanzi(T_ZH)
    total_chars = len(T_ZH)
    billed_per_call = hanzi * 2 + (total_chars - hanzi)
    # turbo CNY 2/10k, hd CNY 3.5/10k
    price = 2.0 if "turbo" in args.model else 3.5
    cost = len(voices) * billed_per_call / 10000 * price
    csv_path = _default_dir() / "minimax_en_voice_zh_test.csv"

    print()
    print("=" * 72)
    print("MiniMax English voice + Chinese text usability test")
    print("=" * 72)
    print(f"  model       : {args.model}")
    print(f"  voices      : {voices}")
    print(f"  text        : T_ZH ({hanzi} hanzi, {total_chars} total, {billed_per_call} billed chars)")
    print(f"  est. cost   : {len(voices)} x {billed_per_call} x CNY{price}/10k = CNY {cost:.2f}")
    print(f"  audio out   : /tmp/minimax_en_voice_zh_test_<voice>.mp3")
    print()

    if not args.execute:
        print("[dry-run] No API calls made.")
        return 0

    print("[execute] Starting...")
    out_dir = _default_dir()
    results: list[dict] = []
    for idx, voice in enumerate(voices):
        print(f"  [{idx + 1}/{len(voices)}] voice={voice}", end=" ... ", flush=True)
        try:
            t0 = time.time()
            audio, trace_id, err = _synthesize_once(T_ZH, voice, model=args.model)
            api_ms = int((time.time() - t0) * 1000)
            if audio and not err:
                out_path = out_dir / f"minimax_en_voice_zh_test_{voice}.mp3"
                try:
                    out_path.write_bytes(audio)
                except OSError as e:
                    print(f"(write failed {e}) ", end="")
                print(f"OK bytes={len(audio)} api={api_ms}ms trace={trace_id} saved={out_path.name}")
                results.append({
                    "voice": voice, "status": "OK",
                    "bytes": len(audio), "api_ms": api_ms,
                    "trace_id": trace_id, "error": "",
                    "audio_path": str(out_path),
                })
            else:
                print(f"FAIL api={api_ms}ms err={err!r}")
                results.append({
                    "voice": voice, "status": "FAIL",
                    "bytes": 0, "api_ms": api_ms,
                    "trace_id": trace_id, "error": err,
                    "audio_path": "",
                })
        except Exception as exc:
            print(f"EXCEPTION: {exc}")
            results.append({
                "voice": voice, "status": "EXCEPTION",
                "bytes": 0, "api_ms": 0, "trace_id": "",
                "error": str(exc)[:200], "audio_path": "",
            })
        if idx < len(voices) - 1:
            time.sleep(args.sleep)

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "voice", "status", "bytes", "api_ms", "trace_id", "error", "audio_path",
            ])
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nCSV written: {csv_path}")
    except OSError as exc:
        print(f"CSV write failed: {exc}")

    print()
    print("=" * 72)
    print("Results")
    print("=" * 72)
    n_ok = sum(1 for r in results if r["status"] == "OK")
    print(f"  {n_ok}/{len(results)} voices produced audio for Chinese text.")
    for r in results:
        print(f"  {r['voice']:<30} {r['status']:<10} {r['error'][:40]}")
    if n_ok > 0:
        print()
        print("NEXT STEP: listen to audio files to judge usability.")
        print(f"  Download from us host: scp ...:/tmp/minimax_en_voice_zh_test_*.mp3")

    return 0


if __name__ == "__main__":
    sys.exit(main())
