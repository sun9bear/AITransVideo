"""CosyVoice TTS provider — subprocess isolation.

Each synthesize() call spawns a short-lived helper process that imports
DashScope, runs one SpeechSynthesizer.call(), writes audio to a temp file,
and exits.  This guarantees that SDK-internal threads (ObjectPool,
__auto_reconnect, run_forever) cannot leak into the main worker process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "cosyvoice-v3-flash"
DEFAULT_VOICE = "longanyang"
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 3.0
RETRY_BACKOFF_MAX = 60.0
RETRYABLE_STATUS_CODES = {429, 503}

# Per-call timeout for the helper subprocess (seconds).
_HELPER_TIMEOUT_SECONDS = 90

# Resolve helper script path once at import time.
_HELPER_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "cosyvoice_tts_helper.py"


def _resolve_deployment_mode() -> str:
    """Return the current runtime deployment mode ('international' or 'mainland')."""
    from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode
    return get_runtime_endpoint_mode()


def _resolve_ws_url() -> str:
    """Return the current runtime WebSocket URL."""
    from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode, get_ws_url
    return get_ws_url(get_runtime_endpoint_mode())


class CosyVoiceTTSError(Exception):
    """Raised when CosyVoice synthesis fails."""


def shutdown_runtime() -> None:
    """No-op.  Subprocess isolation means nothing to clean up in this process."""
    pass


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _should_retry(exc: Exception) -> bool:
    lowered = str(exc).lower()
    for code in RETRYABLE_STATUS_CODES:
        if str(code) in lowered:
            return True
    for keyword in (
        "timeout",
        "connection reset",
        "temporary",
        "unavailable",
        "rate limit",
        "throttl",
        "too many requests",
        "socket reset",
    ):
        if keyword in lowered:
            return True
    return False


def _backoff_sleep(attempt: int) -> float:
    delay = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
    time.sleep(delay)
    return delay


# ---------------------------------------------------------------------------
# Subprocess-based synthesize
# ---------------------------------------------------------------------------

def _synthesize_once(
    text: str,
    voice: str,
    model: str,
    speech_rate: float = 1.0,
) -> bytes:
    """Run a single TTS call in an isolated helper subprocess.

    ``speech_rate`` is a multiplier matching DashScope SDK semantics
    (default 1.0, range ~0.5-2.0). Only written into request.json when
    different from 1.0 so that pre-Phase 2 callers emit byte-identical
    request payloads.
    """
    tmp_dir = tempfile.mkdtemp(prefix="cosyvoice_")
    request_path = os.path.join(tmp_dir, "request.json")
    output_path = os.path.join(tmp_dir, "output.wav")

    request_data = {
        "text": text,
        "voice": voice,
        "model": model,
        "output_path": output_path,
        "endpoint_mode": _resolve_deployment_mode(),
    }
    if speech_rate != 1.0:
        request_data["speech_rate"] = float(speech_rate)
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(request_data, f, ensure_ascii=False)

    helper_cmd = [sys.executable, "-u", str(_HELPER_SCRIPT), request_path]

    try:
        proc = subprocess.Popen(
            helper_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=_HELPER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise CosyVoiceTTSError(
                f"CosyVoice helper timed out after {_HELPER_TIMEOUT_SECONDS}s "
                f"for voice={voice}, model={model}"
            )

        # Log helper stderr (truncated) for diagnostics
        if stderr and stderr.strip():
            for line in stderr.strip().splitlines()[:10]:
                logger.debug("[CosyVoice helper] %s", line)

        # Parse stdout JSON
        stdout_stripped = stdout.strip()
        if not stdout_stripped:
            raise CosyVoiceTTSError(
                f"CosyVoice helper produced no output (returncode={proc.returncode}). "
                f"stderr: {(stderr or '')[:300]}"
            )

        try:
            result = json.loads(stdout_stripped.splitlines()[-1])
        except json.JSONDecodeError:
            raise CosyVoiceTTSError(
                f"CosyVoice helper produced invalid JSON: {stdout_stripped[:200]}. "
                f"stderr: {(stderr or '')[:300]}"
            )

        if not result.get("ok"):
            error_msg = result.get("error", "unknown error")
            error_type = result.get("error_type", "HelperError")
            raise CosyVoiceTTSError(
                f"CosyVoice helper failed: [{error_type}] {error_msg}"
            )

        # Read output audio
        if not os.path.exists(output_path):
            raise CosyVoiceTTSError(
                f"CosyVoice helper reported success but output file missing: {output_path}"
            )
        with open(output_path, "rb") as f:
            audio_bytes = f.read()

        if len(audio_bytes) == 0:
            raise CosyVoiceTTSError("CosyVoice helper produced empty audio file")

        return audio_bytes

    finally:
        # Clean up temp files
        for path in (request_path, output_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API — same signature as before
# ---------------------------------------------------------------------------

def synthesize(
    text: str,
    voice: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    *,
    speech_rate: float = 1.0,
) -> bytes:
    """Synthesize text to audio using CosyVoice via an isolated subprocess.

    Interface is a superset of the previous direct-call version.
    ``api_key`` is accepted for signature compatibility but ignored —
    the helper reads DASHSCOPE_API_KEY from the environment.
    ``speech_rate`` is a DashScope-SDK-native multiplier (default 1.0).
    """
    if not text or not text.strip():
        raise CosyVoiceTTSError("Text must not be empty")

    truncated = text[:50] + "..." if len(text) > 50 else text

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt == 0:
            logger.info("[CosyVoice] voice=%s, text=%s, speech_rate=%.4f", voice, truncated, speech_rate)
        else:
            logger.info("[CosyVoice] voice=%s, text=%s, speech_rate=%.4f (retry %d/%d)",
                        voice, truncated, speech_rate, attempt, MAX_RETRIES)
        try:
            audio = _synthesize_once(text, voice, model, speech_rate=speech_rate)
            logger.info("[CosyVoice] success: %d bytes, voice=%s", len(audio), voice)
            return audio
        except CosyVoiceTTSError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES and _should_retry(exc):
                delay = _backoff_sleep(attempt)
                logger.warning(
                    "[CosyVoice] transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES, delay, exc,
                )
                continue
            break

    raise CosyVoiceTTSError(
        f"CosyVoice synthesis failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc
