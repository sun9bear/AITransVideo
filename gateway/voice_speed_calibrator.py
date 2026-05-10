"""Single-voice TTS speed calibration — extracted from
``scripts/calibrate_voice_speeds.py`` so it can be reused by:

  - the bulk script (which iterates over many voices and writes to
    ``voice_catalog``)
  - the new ``POST /gateway/user-voices/{id}/calibrate-speed`` endpoint
    (which calibrates a single cloned voice and writes to ``user_voices``)

The function below DOES NOT touch any database — it just runs synth + ffprobe
and returns the cps. The caller decides where to persist (voice_catalog
vs user_voices vs nowhere). This keeps the helper:
  - testable (no DB / no API mocks beyond synth_fn)
  - reusable across two storage tables
  - safe against partial-write bugs (any DB error is on caller's path)

The 3 standard texts (T1/T2/T3, 458 hanzi total) are imported from
``scripts/standard_calibration_texts`` so the bulk script and the API
share the exact same baseline — calibration values are directly
comparable across provider + cloned voices.

Sanity bounds (2.0..8.0 cps) match ``src/pipeline/process.py``'s
post-TTS calibration filter, so any value we accept here can be
trusted by the pipeline downstream without re-validation.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# --- sys.path setup so we can import standard_calibration_texts and the
# provider helpers regardless of whether this is loaded from the gateway
# container (where /opt/gateway is the cwd) or the app container path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_SCRIPTS = Path(__file__).resolve().parent / "scripts"
_SRC_DIR = _REPO_ROOT / "src"
for p in (_GATEWAY_SCRIPTS, _SRC_DIR):
    sp = str(p)
    if p.exists() and sp not in sys.path:
        sys.path.insert(0, sp)

from standard_calibration_texts import STANDARD_TEXTS, count_hanzi  # noqa: E402
# Zero-dependency constants module — shared with the runtime catalog
# client (src/services/tts/voice_speed_catalog.py) so calibrator-side
# writes and pipeline-side reads can never drift.
from services.tts.voice_speed_bounds import (  # noqa: E402
    MAX_VALID_CPS,
    MIN_VALID_CPS,
)


@dataclass(slots=True)
class TextResult:
    """Per-text intermediate result (T1 / T2 / T3)."""
    name: str
    hanzi: int
    duration_ms: int
    cps: float


@dataclass(slots=True)
class CalibrationResult:
    """Outcome of calibrating one voice across the 3 standard texts.

    On success: ``ok=True``, ``cps`` is the float chars/sec (rounded 4
    decimals), ``per_text`` lists each text's contribution.

    On failure: ``ok=False``, ``error`` is a human-readable message
    naming the failing text (e.g. ``"synth failed on T2_documentary: ..."``)
    so the user-facing error in the UI is actionable.

    Plan v3+ additions for the auto-calibration callers (T1 clone hook,
    T2 review preflight, T3 admin batch):

    ``error_class`` (v3 codex F9 + v4.1 F-v4.1-7):
        Machine-parseable error category so callers can decide refund
        / retry / skip without parsing the human-readable string. One
        of: ``""`` (success), ``"unknown_provider"``, ``"voice_not_found"``,
        ``"synth_failed"``, ``"duration_measurement_failed"``,
        ``"non_positive_duration"``, ``"total_duration_zero"``,
        ``"out_of_bounds_cps"``, ``"total_timeout"``, ``"db_write_failed"``,
        ``"internal_error"``, ``"rate_limited"``, ``"unsupported_provider"``.

    ``paid_call_count`` (v4.1 codex F-v4.1-7):
        Number of paid TTS calls the calibration ACTUALLY ISSUED to the
        provider — incremented BEFORE each ``synth_fn`` invocation so
        that synth_fn raising (5xx, timeout) still increments the count.
        Caller (clone hook / review preflight / manual endpoint) reads
        this to decide refund: refund only when ``paid_call_count == 0``.
        Refunding paid_call_count > 0 would let provider failure storms
        bypass the budget.

    ``model_key`` (v3 codex F2 + v4 model-aware writes):
        The canonical model id this result is FOR (e.g. ``"speech-2.8-turbo"``,
        ``"speech-2.8-hd"``). Mandatory for write helpers — they store
        results into ``chars_per_second_by_model[model_key]`` JSONB. Empty
        only on early-failure paths where the caller already knows the
        intended model.
    """
    ok: bool
    cps: float = 0.0
    total_hanzi: int = 0
    total_duration_ms: int = 0
    per_text: list[TextResult] = field(default_factory=list)
    error: str = ""
    error_class: str = ""
    paid_call_count: int = 0
    model_key: str = ""


# ---------------------------------------------------------------------------
# Synthesis dispatch — reuses the production provider modules so the
# wire payload is byte-identical to what the pipeline sends.
# ---------------------------------------------------------------------------

# T0-C bounded primitives (plan v4.3 §3.0): per-call timeouts must be
# tight so calibrate_voice's outer total_timeout_seconds budget can't
# be silently bypassed by a single slow provider call. v1's 60s × 2
# retries gave a 180s worst case per text (540s for the full 3-text
# calibration), unsuitable for the synchronous T2 review preflight.
#
# 12s × max_retries=1 gives 24s worst case per text. MiniMax production
# voice probe averages ~5s per call, p95 < 10s — 12s is conservative
# without being wasteful.
_MINIMAX_CALIBRATION_SYNTH_TIMEOUT_S = 12.0
_MINIMAX_CALIBRATION_SYNTH_MAX_RETRIES = 1

# T0-C ffprobe timeout (plan v4.3 §3.0): WAV duration probe should be
# sub-second; 10s is a generous upper bound. Without it, a hung ffprobe
# subprocess would extend calibration indefinitely beyond
# total_timeout_seconds.
_FFPROBE_TIMEOUT_S = 10


def _synthesize_minimax(text: str, voice_id: str, model: str) -> bytes:
    """MiniMax HTTP synth via the production helper. Returns raw bytes
    (WAV format requested below; ``audio`` field is hex-encoded).

    Uses calibration-specific bounded timeouts (12s × 1 retry) — NOT
    the production TTS helper's defaults (60s × 2 retries which suit
    long-text dubbing but would let a single calibration call hang for
    180s).
    """
    from services.tts.tts_generator import _build_tts_endpoint, _post_json

    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or os.environ.get("AUTODUB_TTS_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY or AUTODUB_TTS_API_KEY not set")

    endpoint = _build_tts_endpoint("https://api.minimaxi.com")
    payload = {
        "model": model,
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1.0,
            "vol": 1.0,
        },
        "audio_setting": {
            "format": "wav",
            "sample_rate": 24000,
        },
    }
    response = _post_json(
        endpoint=endpoint,
        api_key=api_key,
        payload=payload,
        timeout_seconds=_MINIMAX_CALIBRATION_SYNTH_TIMEOUT_S,
        max_retries=_MINIMAX_CALIBRATION_SYNTH_MAX_RETRIES,
        retry_backoff_seconds=2.0,
    )
    base = response.get("base_resp") or {}
    if base.get("status_code") != 0:
        raise RuntimeError(f"MiniMax error: {base}")
    data = response.get("data") or {}
    audio_hex = data.get("audio", "")
    if not audio_hex:
        raise RuntimeError("MiniMax returned no audio data")
    return bytes.fromhex(audio_hex)


def _synthesize_cosyvoice(text: str, voice_id: str, model: str) -> bytes:
    from services.tts.cosyvoice_provider import synthesize as cv_synth
    return cv_synth(text, voice_id, model=model)


def _synthesize_volcengine(text: str, voice_id: str, resource_id: str) -> bytes:
    from services.tts.volcengine_tts_provider import synthesize as vc_synth
    return vc_synth(text, voice_id, resource_id=resource_id)


_DEFAULT_SYNTH_FNS: dict[str, Callable[[str, str, str], bytes]] = {
    "minimax": _synthesize_minimax,
    "cosyvoice": _synthesize_cosyvoice,
    "volcengine": _synthesize_volcengine,
}


def _measure_wav_duration_ms(wav_bytes: bytes) -> int:
    """Probe a WAV byte buffer with ffprobe, return duration in ms.

    T0-C (plan v4.3): ``timeout=_FFPROBE_TIMEOUT_S`` so a hung ffprobe
    subprocess can't extend calibration past ``total_timeout_seconds``.
    Raises ``subprocess.TimeoutExpired`` when ffprobe doesn't return in
    time; callers map that to ``error_class="duration_measurement_failed"``.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        path = f.name
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, check=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
        seconds = float(result.stdout.strip())
        return int(round(seconds * 1000))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def calibrate_voice(
    *,
    provider: str,
    model: str,
    voice_id: str,
    total_timeout_seconds: float = 60.0,
    inter_call_sleep_s: float = 2.0,
    synth_fn: Callable[[str, str, str], bytes] | None = None,
    duration_fn: Callable[[bytes], int] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> CalibrationResult:
    """Calibrate a single voice's chars-per-second across the 3 standard texts.

    Plan v3+ contract: **NEVER raises**. All failure modes packed into
    ``CalibrationResult`` so the caller (clone hook / review preflight /
    manual endpoint) can decide refund based purely on
    ``result.paid_call_count`` (codex v4 F-v4-4).

    Parameters
    ----------
    provider:
        ``"minimax"`` / ``"cosyvoice"`` / ``"volcengine"`` — picks the default
        synth function. Ignored when ``synth_fn`` is provided (testing).
    model:
        Provider-specific canonical model identifier (e.g. ``"speech-2.8-turbo"``,
        ``"speech-2.8-hd"``). Passed through to ``synth_fn``. The result's
        ``model_key`` field is set to this value so callers can write into
        the right ``chars_per_second_by_model`` JSONB key (T0-D).
    voice_id:
        Provider-side voice id to calibrate.
    total_timeout_seconds:
        Outer budget for the entire 3-text calibration. Plan v4.1 codex
        F-v4.1-7: this is checked at SEGMENT BOUNDARIES — not enforced
        inside a blocking ``synth_fn`` call. The bounded primitives
        (``_post_json`` 12s / ffprobe 10s) limit each call's worst-case
        contribution to ~24s; the total budget catches "first 2 calls
        each took 28s, abort before issuing call 3". Once an individual
        synth_fn is in flight, this budget cannot interrupt it.
    inter_call_sleep_s:
        Sleep between the 3 synth calls. ~2s default ≈ bulk script's
        rate-limited cadence. The sleep is also subject to the segment-
        boundary timeout check.
    synth_fn:
        Override for tests. Takes ``(text, voice_id, model)`` returning
        raw audio bytes (must be parseable by ffprobe — WAV is fine).
    duration_fn:
        Override for tests. Takes audio bytes returning duration in ms.
        Production path uses ffprobe.
    monotonic_fn:
        Override for tests (fake clock). Defaults to ``time.monotonic``.
        Used for total_timeout_seconds budget tracking.

    Returns
    -------
    CalibrationResult
        Always a result object — never raises. ``paid_call_count`` reflects
        how many synth calls were actually issued (incremented BEFORE each
        call so 5xx/timeout are counted — codex v4.1 F-v4.1-7).
    """
    sfn = synth_fn or _DEFAULT_SYNTH_FNS.get(provider)
    if sfn is None:
        return CalibrationResult(
            ok=False,
            error=f"unknown provider: {provider!r}",
            error_class="unknown_provider",
            paid_call_count=0,
            model_key=model,
        )

    dfn = duration_fn or _measure_wav_duration_ms
    clock = monotonic_fn or time.monotonic
    started_at = clock()

    per_text: list[TextResult] = []
    total_hanzi = 0
    total_ms = 0
    paid_call_count = 0

    items = list(STANDARD_TEXTS.items())
    for idx, (name, text) in enumerate(items):
        # T0-C segment-boundary timeout check (plan v4.1 F-v4.1-7): enforce
        # total budget BEFORE issuing the next paid call. Already-running
        # synth_fn cannot be interrupted; this prevents NEW work from
        # starting once budget is exhausted.
        if clock() - started_at > total_timeout_seconds:
            return CalibrationResult(
                ok=False,
                error=f"total_timeout: budget {total_timeout_seconds}s exhausted before {name}",
                error_class="total_timeout",
                paid_call_count=paid_call_count,
                per_text=per_text,
                model_key=model,
            )

        if idx > 0 and inter_call_sleep_s > 0:
            time.sleep(inter_call_sleep_s)
            # Re-check after sleep — sleep itself may have consumed the budget
            if clock() - started_at > total_timeout_seconds:
                return CalibrationResult(
                    ok=False,
                    error=f"total_timeout: budget {total_timeout_seconds}s exhausted during inter-call sleep before {name}",
                    error_class="total_timeout",
                    paid_call_count=paid_call_count,
                    per_text=per_text,
                    model_key=model,
                )

        # paid_call_count increments BEFORE synth (codex v4.1 F-v4.1-7).
        # If synth_fn raises (provider 5xx, timeout, network error), the
        # count still reflects "we issued this call" so caller's refund
        # logic correctly sees count > 0 and skips refund.
        paid_call_count += 1
        try:
            wav = sfn(text, voice_id, model)
        except Exception as exc:
            return CalibrationResult(
                ok=False,
                error=f"synth failed on {name}: {exc}",
                error_class="synth_failed",
                paid_call_count=paid_call_count,
                per_text=per_text,
                model_key=model,
            )
        try:
            duration_ms = dfn(wav)
        except Exception as exc:
            return CalibrationResult(
                ok=False,
                error=f"duration measurement failed on {name}: {exc}",
                error_class="duration_measurement_failed",
                paid_call_count=paid_call_count,
                per_text=per_text,
                model_key=model,
            )
        if duration_ms <= 0:
            return CalibrationResult(
                ok=False,
                error=f"non-positive duration on {name}: {duration_ms}",
                error_class="non_positive_duration",
                paid_call_count=paid_call_count,
                per_text=per_text,
                model_key=model,
            )
        hanzi = count_hanzi(text)
        seg_cps = hanzi / (duration_ms / 1000.0) if duration_ms > 0 else 0.0
        per_text.append(TextResult(name=name, hanzi=hanzi, duration_ms=duration_ms, cps=round(seg_cps, 4)))
        total_hanzi += hanzi
        total_ms += duration_ms

    if total_ms <= 0:
        return CalibrationResult(
            ok=False,
            error="total duration zero",
            error_class="total_duration_zero",
            paid_call_count=paid_call_count,
            per_text=per_text,
            model_key=model,
        )

    cps = total_hanzi / (total_ms / 1000.0)
    if not (MIN_VALID_CPS <= cps <= MAX_VALID_CPS):
        return CalibrationResult(
            ok=False,
            error=f"cps {cps:.2f} out of sanity range [{MIN_VALID_CPS}, {MAX_VALID_CPS}]",
            error_class="out_of_bounds_cps",
            paid_call_count=paid_call_count,
            per_text=per_text,
            total_hanzi=total_hanzi,
            total_duration_ms=total_ms,
            cps=round(cps, 4),
            model_key=model,
        )

    return CalibrationResult(
        ok=True,
        cps=round(cps, 4),
        total_hanzi=total_hanzi,
        total_duration_ms=total_ms,
        per_text=per_text,
        paid_call_count=paid_call_count,
        model_key=model,
    )
