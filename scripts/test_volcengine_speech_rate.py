#!/usr/bin/env python3
"""VolcEngine speech_rate field validation -- Phase 2 Go/No-Go gate.

Goal
----
Verify whether ``req_params.audio_params.speech_rate`` (integer, -50..100)
is honored by the VolcEngine V3 unidirectional TTS endpoint, for both
``seed-tts-1.0`` and ``seed-tts-2.0`` resources.

If verified (audio duration varies systematically with speech_rate), Phase 2
can extend ``SPEED_AWARE_TTS_PROVIDERS`` in ``src/pipeline/process.py`` to
include ``"volcengine"`` and wire the parameter through the provider.

If not verified (duration is flat regardless of speech_rate), the VolcEngine
branch stays at speed=1.0 and relies purely on voice-match + DSP. The
pre-rewrite skip path will permanently treat it as speed-unaware.

Method
------
Synthesize the same text with the same voice at 5 different speech_rate
values (-30, -15, 0, +15, +30) and compare PCM durations against the
baseline (speech_rate=0). A systematic delta >= 10% versus baseline at the
extremes is treated as "field is honored".

Safety
------
Paid API. Default ``--dry-run`` makes NO calls. Pass ``--execute`` to
actually hit the endpoint. See CLAUDE.md on paid API constraints.

Usage
-----
    # Plan-only (zero API cost):
    python scripts/test_volcengine_speech_rate.py --dry-run

    # Real run (costs ~CNY0.76 for 5 calls on seed-tts-2.0):
    python scripts/test_volcengine_speech_rate.py --execute

    # Test both resources in one session:
    python scripts/test_volcengine_speech_rate.py --execute --resource-id seed-tts-1.0
    python scripts/test_volcengine_speech_rate.py --execute --resource-id seed-tts-2.0

Environment
-----------
Requires ``VOLCENGINE_TTS_APP_ID`` + ``VOLCENGINE_TTS_ACCESS_KEY`` in the
environment (or legacy APPID / ACCESS_TOKEN). Inside the aivideotrans-app
container these are loaded from ``/opt/aivideotrans/config/.env`` via
docker-compose env_file.
"""
from __future__ import annotations

import argparse
import base64
import csv
import sys
import tempfile
import time
from pathlib import Path

# --- sys.path setup -- work both inside the container and on dev host ---
# The app container only mounts src/ (not gateway/), so we inline the
# calibration texts below rather than importing from gateway/scripts.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.exists():
    sp = str(_SRC_DIR)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# Reuse the provider's internal helpers so the test matches production
# auth / streaming parsing exactly. Only the payload builder needs to
# change (to inject speech_rate).
from services.tts.volcengine_tts_provider import (  # noqa: E402
    CODE_AUDIO_CHUNK,
    CODE_FINISH,
    DEFAULT_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    RESOURCE_ID_1_0,
    RESOURCE_ID_2_0,
    VolcEngineTTSError,
    _build_headers,
    _do_post,
    _iter_chunk_events,
    _resolve_credentials,
    default_speaker_for_resource,
)

# ---------------------------------------------------------------------------
# Inlined calibration text (mirrored from gateway/scripts/standard_calibration_texts.py)
# ---------------------------------------------------------------------------
# T1 tech_review -- 101 汉字, 150 total chars (incl. punctuation).
# Kept inline so this script can run inside the app container where
# gateway/ is not mounted. Do NOT edit -- must stay byte-identical to the
# gateway copy so calibration comparisons remain valid.
T1_TECH_REVIEW = (
    "这款手机的屏幕素质让我很震惊。"
    "色彩通透，对比度极高，黑色几乎和关屏没有区别。"
    "拿来和上一代对比，亮度提升了将近四成，户外强光下也能看得清楚。"
    "最让我意外的是功耗居然还降低了，续航多了将近两小时。"
    "这块屏幕，确实是今年旗舰里最强的。"
)

STANDARD_TEXTS: dict[str, str] = {"T1_tech_review": T1_TECH_REVIEW}

_CJK_RANGE_START = 0x4E00
_CJK_RANGE_END = 0x9FFF


def count_hanzi(text: str) -> int:
    """Count CJK Unified Ideographs in *text* (matches gateway helper)."""
    return sum(1 for ch in text if _CJK_RANGE_START <= ord(ch) <= _CJK_RANGE_END)

# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------

# speech_rate grid. VolcEngine docs document -50..+100; we stay well inside
# that envelope. Symmetric around 0 so slow/fast deltas are directly
# comparable. Baseline (rate=0) must be inside this list for the ratio math.
SPEECH_RATE_GRID: list[tuple[str, int]] = [
    ("slow_-30", -30),
    ("slow_-15", -15),
    ("default_0", 0),
    ("fast_+15", 15),
    ("fast_+30", 30),
]

DEFAULT_TEXT_KEY = "T1_tech_review"

# CNY/10k chars -- from the Phase 1 calibration cost sheet. Used for the
# dry-run cost estimate only; actual billing comes from VolcEngine's console.
PRICE_PER_10K_CHARS: dict[str, float] = {
    RESOURCE_ID_1_0: 5.0,
    RESOURCE_ID_2_0: 3.0,
}

# Delay between successive API calls. VolcEngine doesn't advertise a strict
# RPM for unidirectional V3, but ``calibrate_voice_speeds.py`` uses 30 RPM
# (2 s/call) for VolcEngine; keeping parity.
INTER_CALL_SLEEP_SECONDS = 2.0

# Go/No-Go threshold: if the extremes diverge from baseline by at least
# this fraction, speech_rate is honored. 10% is well above TTS's natural
# run-to-run variation (~2-5%) and well below the expected +/-30% that
# a truly active rate parameter should produce at grid edges.
GO_NOGO_MIN_VARIATION = 0.10


# ---------------------------------------------------------------------------
# API call with explicit speech_rate
# ---------------------------------------------------------------------------

def _build_payload_with_rate(
    text: str,
    speaker: str,
    speech_rate: int,
    *,
    model: str | None = None,
) -> dict:
    """Same shape as the provider's ``_build_payload`` but with
    ``audio_params.speech_rate`` injected.
    """
    req_params: dict = {
        "speaker": speaker,
        "text": text,
        "audio_params": {
            "format": "pcm",
            "sample_rate": PCM_SAMPLE_RATE,
            "speech_rate": int(speech_rate),
        },
    }
    if model:
        req_params["model"] = model
    return {
        "user": {"uid": "aivideotrans-speechrate-test"},
        "req_params": req_params,
    }


def _synthesize_once(
    text: str,
    voice_id: str,
    resource_id: str,
    speech_rate: int,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, str]:
    """Run one synth call with explicit speech_rate. Returns (pcm, log_id)."""
    app_id, access_key, _ = _resolve_credentials()
    headers = _build_headers(app_id, access_key, resource_id)
    payload = _build_payload_with_rate(text, voice_id, speech_rate)

    pcm_chunks: list[bytes] = []
    finished = False
    log_id = ""

    response = _do_post(
        DEFAULT_ENDPOINT,
        headers=headers,
        json=payload,
        stream=True,
        timeout=timeout_seconds,
    )
    try:
        log_id = response.headers.get("X-Tt-Logid", "")
        for event in _iter_chunk_events(response):
            code = event.get("code", -1)
            if code == CODE_AUDIO_CHUNK:
                data_b64 = event.get("data", "")
                if data_b64:
                    pcm_chunks.append(base64.b64decode(data_b64))
                continue
            if code == CODE_FINISH:
                finished = True
                break
            if code > 0:
                raise VolcEngineTTSError(
                    f"VolcEngine error code={code} message={event.get('message', '')!r} "
                    f"logid={log_id}"
                )
    finally:
        response.close()

    if not finished:
        raise VolcEngineTTSError(f"Stream ended without finish event (logid={log_id})")
    if not pcm_chunks:
        raise VolcEngineTTSError(f"No audio data (logid={log_id})")
    return b"".join(pcm_chunks), log_id


def _pcm_duration_ms(pcm_bytes: bytes) -> int:
    """Compute PCM duration in ms from raw bytes.

    bytes_per_second = sample_rate * channels * sample_width
    """
    bytes_per_second = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH
    if bytes_per_second <= 0:
        return 0
    return int(round(len(pcm_bytes) / bytes_per_second * 1000))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _default_csv_path() -> Path:
    """Prefer /tmp/ on Linux; fall back to tempfile.gettempdir() on Windows."""
    linux_tmp = Path("/tmp")
    base = linux_tmp if linux_tmp.is_dir() else Path(tempfile.gettempdir())
    return base / "volcengine_speech_rate_test.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify VolcEngine speech_rate field effect. Default is --dry-run "
            "(no API calls). Pass --execute for real synthesis."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Make real paid API calls. Without this flag the script only prints the plan.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run flag (default behavior). Has no effect beyond being explicit.",
    )
    parser.add_argument(
        "--resource-id",
        default=RESOURCE_ID_2_0,
        choices=[RESOURCE_ID_1_0, RESOURCE_ID_2_0],
        help="Which V3 resource to test. Default: seed-tts-2.0 (cheaper, CNY3/10k).",
    )
    parser.add_argument(
        "--voice-id",
        default=None,
        help="Override the default speaker voice. Must be compatible with --resource-id.",
    )
    parser.add_argument(
        "--text-name",
        default=DEFAULT_TEXT_KEY,
        choices=list(STANDARD_TEXTS.keys()),
        help=f"Standard calibration text key. Default: {DEFAULT_TEXT_KEY}.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="CSV output path. Default: /tmp/volcengine_speech_rate_test.csv",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=INTER_CALL_SLEEP_SECONDS,
        help=f"Seconds between calls (rate-limit). Default: {INTER_CALL_SLEEP_SECONDS}",
    )
    args = parser.parse_args(argv)

    resource_id = args.resource_id
    voice_id = args.voice_id or default_speaker_for_resource(resource_id)
    text = STANDARD_TEXTS[args.text_name]
    hanzi = count_hanzi(text)
    total_chars = len(text)
    csv_path = Path(args.csv) if args.csv else _default_csv_path()

    # --- Plan header ---
    price = PRICE_PER_10K_CHARS.get(resource_id, 3.0)
    total_billed = total_chars * len(SPEECH_RATE_GRID)
    cost_estimate = total_billed / 10000 * price

    print()
    print("=" * 60)
    print("VolcEngine speech_rate field validation")
    print("=" * 60)
    print(f"  resource_id : {resource_id}")
    print(f"  voice_id    : {voice_id}")
    print(f"  text_name   : {args.text_name}  ({hanzi} hanzi, {total_chars} total chars)")
    print(f"  grid        : {[r for _, r in SPEECH_RATE_GRID]}")
    print(f"  calls       : {len(SPEECH_RATE_GRID)}")
    print(
        f"  est. cost   : {len(SPEECH_RATE_GRID)} x {total_chars} chars x "
        f"CNY{price}/10k = CNY{cost_estimate:.2f}"
    )
    print(f"  csv output  : {csv_path}")
    print()

    if not args.execute:
        print("[dry-run] No API calls made. Re-run with --execute to proceed.")
        return 0

    # --- Live synthesis loop ---
    print("[execute] Starting synthesis...")
    try:
        _resolve_credentials()  # fail fast if env is missing
    except VolcEngineTTSError as exc:
        print(f"\nFATAL: credentials not available -- {exc}")
        return 2

    results: list[dict] = []
    for idx, (label, rate) in enumerate(SPEECH_RATE_GRID):
        print(f"  [{idx + 1}/{len(SPEECH_RATE_GRID)}] speech_rate={rate:+d} ({label})", end=" ... ", flush=True)
        try:
            t0 = time.time()
            pcm, log_id = _synthesize_once(text, voice_id, resource_id, rate)
            api_ms = int((time.time() - t0) * 1000)
            audio_ms = _pcm_duration_ms(pcm)
            cps = (hanzi / (audio_ms / 1000)) if audio_ms > 0 else 0.0
            print(f"audio={audio_ms} ms  cps={cps:.2f}  api={api_ms} ms  logid={log_id}")
            results.append({
                "label": label,
                "speech_rate": rate,
                "audio_ms": audio_ms,
                "cps": round(cps, 3),
                "api_ms": api_ms,
                "log_id": log_id,
                "error": "",
            })
        except Exception as exc:  # broad except: record failure, keep going
            print(f"ERROR: {exc}")
            results.append({
                "label": label,
                "speech_rate": rate,
                "audio_ms": 0,
                "cps": 0.0,
                "api_ms": 0,
                "log_id": "",
                "error": str(exc)[:240],
            })
        if idx < len(SPEECH_RATE_GRID) - 1:
            time.sleep(max(0.0, args.sleep))

    # --- Write CSV ---
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "label", "speech_rate", "audio_ms", "cps", "api_ms", "log_id", "error",
            ])
            writer.writeheader()
            for r in results:
                writer.writerow(r)
        print(f"\nCSV written: {csv_path}")
    except OSError as exc:
        print(f"\nCSV write failed ({exc}); results still shown above.")

    # --- Results table ---
    baseline = next(
        (r for r in results if r["speech_rate"] == 0 and r["audio_ms"] > 0),
        None,
    )
    print()
    print("=" * 60)
    print("Results")
    print("=" * 60)
    print(f"{'label':<12} {'rate':>6} {'audio_ms':>10} {'cps':>6} {'vs_baseline':>12}")
    print("-" * 60)
    for r in results:
        if not r["audio_ms"]:
            print(f"{r['label']:<12} {r['speech_rate']:+6d} {'ERROR':>10} {'-':>6} {r['error'][:30]!r}")
            continue
        vs = "-"
        if baseline and baseline["audio_ms"] > 0:
            ratio = r["audio_ms"] / baseline["audio_ms"]
            vs = f"{ratio:.3f}x"
        print(f"{r['label']:<12} {r['speech_rate']:+6d} {r['audio_ms']:>10} {r['cps']:>6.2f} {vs:>12}")

    # --- Go/No-Go verdict ---
    print()
    print("=" * 60)
    print("Verdict")
    print("=" * 60)
    if baseline and baseline["audio_ms"] > 0:
        deviations: list[tuple[int, float]] = []
        for r in results:
            if r["audio_ms"] > 0 and r["speech_rate"] != 0:
                delta = abs(r["audio_ms"] - baseline["audio_ms"]) / baseline["audio_ms"]
                deviations.append((r["speech_rate"], delta))
        max_dev = max((d for _, d in deviations), default=0.0)
        if max_dev >= GO_NOGO_MIN_VARIATION:
            print(f"GO -- speech_rate is honored. Max |delta|={max_dev:.1%} vs baseline "
                  f"(threshold {GO_NOGO_MIN_VARIATION:.0%}).")
            print("  Next steps:")
            print("    1. Update VolcEngine provider synthesize() to accept speech_rate.")
            print("    2. Add 'volcengine' to SPEED_AWARE_TTS_PROVIDERS in src/pipeline/process.py.")
            print("    3. Map speed_decision output (0.5-2.0 multiplier) to speech_rate (-50..100).")
            verdict = "GO"
        else:
            print(f"NO-GO -- speech_rate appears ignored. Max |delta|={max_dev:.1%} vs baseline "
                  f"(below threshold {GO_NOGO_MIN_VARIATION:.0%}).")
            print("  Next steps:")
            print("    1. VolcEngine stays at speed=1.0 in production.")
            print("    2. Phase 2 relies on voice-match + DSP only for VolcEngine segments.")
            print("    3. Document this finding and move on to CosyVoice rate integration.")
            verdict = "NO-GO"
    else:
        print("UNKNOWN -- baseline (speech_rate=0) call failed; no comparison possible.")
        print("  Re-run after checking credentials / network / quota, or try --resource-id=seed-tts-1.0.")
        verdict = "UNKNOWN"

    # Exit code: 0 on GO/NO-GO (both are valid outcomes), 3 on UNKNOWN, 2 on creds fail.
    return 0 if verdict in ("GO", "NO-GO") else 3


if __name__ == "__main__":
    sys.exit(main())
