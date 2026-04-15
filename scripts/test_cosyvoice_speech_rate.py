#!/usr/bin/env python3
"""CosyVoice speech_rate field validation -- Phase 2 smoke test.

Goal
----
Confirm that DashScope SpeechSynthesizer(..., speech_rate=...) actually
changes TTS output duration, now that speech_rate plumbing is wired
end-to-end (helper -> provider -> tts_generator.)

Method
------
Synthesize the same Chinese text with the same voice at 5 speech_rate
values (0.70, 0.85, 1.00, 1.15, 1.30) and compare WAV durations against
the baseline (speech_rate=1.0). A systematic variation >= 10% between
extremes and baseline confirms the SDK is honoring the field.

Safety
------
Paid API (DashScope/Aliyun). Default --dry-run makes NO calls. Pass
--execute to actually hit DashScope. See CLAUDE.md on paid API constraints.

Usage
-----
    python scripts/test_cosyvoice_speech_rate.py --dry-run
    python scripts/test_cosyvoice_speech_rate.py --execute

Environment
-----------
Requires DASHSCOPE_MAINLAND_API_KEY (or DASHSCOPE_API_KEY) and
COSYVOICE_RUNTIME_ENDPOINT_MODE (or DASHSCOPE_DEPLOYMENT_MODE). Inside
the aivideotrans-app container both come from /opt/aivideotrans/config/.env.
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
import wave
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.exists():
    sp = str(_SRC_DIR)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from services.tts.cosyvoice_provider import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    synthesize as cosyvoice_synthesize,
)

# Inlined T1 tech_review (101 hanzi) - same corpus used for VolcEngine
# smoke, keeps cps comparable across providers.
T1_TECH_REVIEW = (
    "这款手机的屏幕素质让我很震惊。"
    "色彩通透，对比度极高，黑色几乎和关屏没有区别。"
    "拿来和上一代对比，亮度提升了将近四成，户外强光下也能看得清楚。"
    "最让我意外的是功耗居然还降低了，续航多了将近两小时。"
    "这块屏幕，确实是今年旗舰里最强的。"
)

_CJK_RANGE_START = 0x4E00
_CJK_RANGE_END = 0x9FFF


def count_hanzi(text: str) -> int:
    return sum(1 for ch in text if _CJK_RANGE_START <= ord(ch) <= _CJK_RANGE_END)


# speech_rate grid. DashScope default is 1.0; SpeechSynthesizer docs
# suggest range 0.5-2.0.  Symmetric around 1.0 so slow/fast deltas are
# directly comparable; baseline (1.0) must be present for ratio math.
SPEECH_RATE_GRID: list[tuple[str, float]] = [
    ("slow_0.70", 0.70),
    ("slow_0.85", 0.85),
    ("default_1.00", 1.00),
    ("fast_1.15", 1.15),
    ("fast_1.30", 1.30),
]

# CosyVoice v3 flash is ¥1/万, 1 hanzi = 2 billed chars.
PRICE_PER_10K_CHARS = 1.0
INTER_CALL_SLEEP_SECONDS = 2.0
GO_NOGO_MIN_VARIATION = 0.10


def _default_csv_path() -> Path:
    linux_tmp = Path("/tmp")
    base = linux_tmp if linux_tmp.is_dir() else Path(tempfile.gettempdir())
    return base / "cosyvoice_speech_rate_test.csv"


def _wav_duration_ms(wav_bytes: bytes) -> int:
    """Return audio duration in milliseconds.

    Primary path: parse WAV header via the stdlib ``wave`` module.
    DashScope CosyVoice returns a WAV whose RIFF size field sometimes
    causes ``wave.getnframes()`` to return junk (observed 2026-04-15
    smoke run: all 5 calls returned frames=~1e9 regardless of actual
    content).  Fallback path: since the helper always requests
    ``AudioFormat.WAV_22050HZ_MONO_16BIT``, we can reconstruct duration
    directly from the payload size:

        bytes_per_second = 22050 Hz x 1 ch x 2 bytes = 44100
        duration_sec = (len(wav_bytes) - 44 header) / 44100

    The fallback triggers when wave-parse yields a duration > 1 hour
    (clearly impossible for a single T1 call) or raises.
    """
    import io

    # Known layout of the format we request in the helper.
    _SAMPLE_RATE = 22050
    _CHANNELS = 1
    _SAMPLE_WIDTH = 2  # 16-bit PCM
    _WAV_HEADER_BYTES = 44

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or _SAMPLE_RATE
            duration_ms = int(round(frames / rate * 1000)) if rate else 0
            if 0 < duration_ms < 3_600_000:  # sanity: under 1 hour
                return duration_ms
    except Exception:
        pass

    # Fallback: reconstruct from byte count.
    if len(wav_bytes) <= _WAV_HEADER_BYTES:
        return 0
    payload_bytes = len(wav_bytes) - _WAV_HEADER_BYTES
    bytes_per_second = _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH
    return int(round(payload_bytes / bytes_per_second * 1000))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify DashScope CosyVoice speech_rate field effect. Default "
            "is --dry-run (no API calls). Pass --execute for real calls."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true",
                        help="Make real paid DashScope calls.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit dry-run flag (default).")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help=f"Voice id. Default: {DEFAULT_VOICE}")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Model. Default: {DEFAULT_MODEL}")
    parser.add_argument("--csv", default=None,
                        help="CSV output path. Default: /tmp/cosyvoice_speech_rate_test.csv")
    parser.add_argument("--sleep", type=float, default=INTER_CALL_SLEEP_SECONDS,
                        help=f"Seconds between calls. Default: {INTER_CALL_SLEEP_SECONDS}")
    args = parser.parse_args(argv)

    text = T1_TECH_REVIEW
    hanzi = count_hanzi(text)
    total_chars = len(text)
    billed_chars = hanzi * 2 + (total_chars - hanzi)  # "1 hanzi = 2 billed, 1 punct = 1"
    csv_path = Path(args.csv) if args.csv else _default_csv_path()

    cost_estimate = billed_chars * len(SPEECH_RATE_GRID) / 10000 * PRICE_PER_10K_CHARS

    print()
    print("=" * 60)
    print("CosyVoice speech_rate field validation")
    print("=" * 60)
    print(f"  voice       : {args.voice}")
    print(f"  model       : {args.model}")
    print(f"  text        : T1 tech_review ({hanzi} hanzi, {total_chars} total chars)")
    print(f"  billed      : {billed_chars} chars per call (hanzi x2 + punct x1)")
    print(f"  grid        : {[r for _, r in SPEECH_RATE_GRID]}")
    print(f"  calls       : {len(SPEECH_RATE_GRID)}")
    print(f"  est. cost   : {len(SPEECH_RATE_GRID)} x {billed_chars} billed x "
          f"CNY{PRICE_PER_10K_CHARS}/10k = CNY{cost_estimate:.2f}")
    print(f"  csv output  : {csv_path}")
    print()

    if not args.execute:
        print("[dry-run] No API calls made. Re-run with --execute to proceed.")
        return 0

    print("[execute] Starting synthesis...")
    results: list[dict] = []
    for idx, (label, rate) in enumerate(SPEECH_RATE_GRID):
        print(f"  [{idx + 1}/{len(SPEECH_RATE_GRID)}] speech_rate={rate:.2f} ({label})",
              end=" ... ", flush=True)
        try:
            t0 = time.time()
            audio_bytes = cosyvoice_synthesize(
                text=text,
                voice=args.voice,
                model=args.model,
                speech_rate=rate,
            )
            api_ms = int((time.time() - t0) * 1000)
            audio_ms = _wav_duration_ms(audio_bytes)
            cps = (hanzi / (audio_ms / 1000)) if audio_ms > 0 else 0.0
            print(f"audio={audio_ms} ms  cps={cps:.2f}  api={api_ms} ms  bytes={len(audio_bytes)}")
            results.append({
                "label": label,
                "speech_rate": rate,
                "audio_ms": audio_ms,
                "cps": round(cps, 3),
                "api_ms": api_ms,
                "audio_bytes": len(audio_bytes),
                "error": "",
            })
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append({
                "label": label,
                "speech_rate": rate,
                "audio_ms": 0,
                "cps": 0.0,
                "api_ms": 0,
                "audio_bytes": 0,
                "error": str(exc)[:240],
            })
        if idx < len(SPEECH_RATE_GRID) - 1:
            time.sleep(max(0.0, args.sleep))

    # CSV
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "label", "speech_rate", "audio_ms", "cps", "api_ms", "audio_bytes", "error",
            ])
            writer.writeheader()
            for r in results:
                writer.writerow(r)
        print(f"\nCSV written: {csv_path}")
    except OSError as exc:
        print(f"\nCSV write failed ({exc}); results still shown above.")

    # Results
    baseline = next(
        (r for r in results if r["speech_rate"] == 1.0 and r["audio_ms"] > 0),
        None,
    )
    print()
    print("=" * 60)
    print("Results")
    print("=" * 60)
    print(f"{'label':<14} {'rate':>6} {'audio_ms':>10} {'cps':>6} {'vs_baseline':>12}")
    print("-" * 60)
    for r in results:
        if not r["audio_ms"]:
            print(f"{r['label']:<14} {r['speech_rate']:>6.2f} {'ERROR':>10} {'-':>6} {r['error'][:30]!r}")
            continue
        vs = "-"
        if baseline and baseline["audio_ms"] > 0:
            ratio = r["audio_ms"] / baseline["audio_ms"]
            vs = f"{ratio:.3f}x"
        print(f"{r['label']:<14} {r['speech_rate']:>6.2f} {r['audio_ms']:>10} {r['cps']:>6.2f} {vs:>12}")

    # Go/No-Go
    print()
    print("=" * 60)
    print("Verdict")
    print("=" * 60)
    if baseline and baseline["audio_ms"] > 0:
        deviations: list[tuple[float, float]] = []
        for r in results:
            if r["audio_ms"] > 0 and r["speech_rate"] != 1.0:
                delta = abs(r["audio_ms"] - baseline["audio_ms"]) / baseline["audio_ms"]
                deviations.append((r["speech_rate"], delta))
        max_dev = max((d for _, d in deviations), default=0.0)
        if max_dev >= GO_NOGO_MIN_VARIATION:
            print(f"GO -- speech_rate is honored. Max |delta|={max_dev:.1%} vs baseline "
                  f"(threshold {GO_NOGO_MIN_VARIATION:.0%}).")
            print("  CosyVoice integration is safe to ship.")
            verdict = "GO"
        else:
            print(f"NO-GO -- speech_rate appears ignored. Max |delta|={max_dev:.1%} vs baseline "
                  f"(below threshold {GO_NOGO_MIN_VARIATION:.0%}).")
            print("  Revert: remove 'cosyvoice' from SPEED_AWARE_TTS_PROVIDERS.")
            verdict = "NO-GO"
    else:
        print("UNKNOWN -- baseline (speech_rate=1.0) call failed; no comparison possible.")
        verdict = "UNKNOWN"

    return 0 if verdict in ("GO", "NO-GO") else 3


if __name__ == "__main__":
    sys.exit(main())
