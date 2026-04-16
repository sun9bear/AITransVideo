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
    """
    ok: bool
    cps: float = 0.0
    total_hanzi: int = 0
    total_duration_ms: int = 0
    per_text: list[TextResult] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Synthesis dispatch — reuses the production provider modules so the
# wire payload is byte-identical to what the pipeline sends.
# ---------------------------------------------------------------------------

def _synthesize_minimax(text: str, voice_id: str, model: str) -> bytes:
    """MiniMax HTTP synth via the production helper. Returns raw bytes
    (WAV format requested below; ``audio`` field is hex-encoded)."""
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
        timeout_seconds=60.0,
        max_retries=2,
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
    """Probe a WAV byte buffer with ffprobe, return duration in ms."""
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
    inter_call_sleep_s: float = 2.0,
    synth_fn: Callable[[str, str, str], bytes] | None = None,
    duration_fn: Callable[[bytes], int] | None = None,
) -> CalibrationResult:
    """Calibrate a single voice's chars-per-second across the 3 standard texts.

    Parameters
    ----------
    provider:
        ``"minimax"`` / ``"cosyvoice"`` / ``"volcengine"`` — picks the default
        synth function. Ignored when ``synth_fn`` is provided (testing).
    model:
        Provider-specific model identifier — passed through to ``synth_fn``.
        For VolcEngine this is the resource_id (e.g. ``"seed-tts-2.0"``).
    voice_id:
        The voice to calibrate.
    inter_call_sleep_s:
        Sleep between the 3 synth calls. ~2s default ≈ the bulk script's
        rate-limited cadence. Kept conservative; per-voice calibration is
        not throughput-critical.
    synth_fn:
        Override for tests. Takes ``(text, voice_id, model)`` returning
        raw audio bytes (must be parseable by ffprobe — WAV is fine).
    duration_fn:
        Override for tests. Takes audio bytes returning duration in ms.
        Production path uses ffprobe.

    Returns
    -------
    CalibrationResult
        Always returns a result object — never raises. Inspect ``ok``.
    """
    sfn = synth_fn or _DEFAULT_SYNTH_FNS.get(provider)
    if sfn is None:
        return CalibrationResult(ok=False, error=f"unknown provider: {provider!r}")

    dfn = duration_fn or _measure_wav_duration_ms

    per_text: list[TextResult] = []
    total_hanzi = 0
    total_ms = 0

    for idx, (name, text) in enumerate(STANDARD_TEXTS.items()):
        if idx > 0 and inter_call_sleep_s > 0:
            time.sleep(inter_call_sleep_s)
        try:
            wav = sfn(text, voice_id, model)
        except Exception as exc:
            return CalibrationResult(
                ok=False,
                error=f"synth failed on {name}: {exc}",
                per_text=per_text,
            )
        try:
            duration_ms = dfn(wav)
        except Exception as exc:
            return CalibrationResult(
                ok=False,
                error=f"duration measurement failed on {name}: {exc}",
                per_text=per_text,
            )
        if duration_ms <= 0:
            return CalibrationResult(
                ok=False,
                error=f"non-positive duration on {name}: {duration_ms}",
                per_text=per_text,
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
            per_text=per_text,
        )

    cps = total_hanzi / (total_ms / 1000.0)
    if not (MIN_VALID_CPS <= cps <= MAX_VALID_CPS):
        return CalibrationResult(
            ok=False,
            error=f"cps {cps:.2f} out of sanity range [{MIN_VALID_CPS}, {MAX_VALID_CPS}]",
            per_text=per_text,
            total_hanzi=total_hanzi,
            total_duration_ms=total_ms,
            cps=round(cps, 4),
        )

    return CalibrationResult(
        ok=True,
        cps=round(cps, 4),
        total_hanzi=total_hanzi,
        total_duration_ms=total_ms,
        per_text=per_text,
    )
