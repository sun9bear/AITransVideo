"""Fake TTSProvider — minimal impl of services.smart.contracts.TTSProvider.

Smart MVP P2 doesn't call TTS through the protocol yet (re-TTS flows
through the existing pipeline path); this fake exists for the
retry_budget module landing in the next PR.

Knobs:
  - ``simulated_duration_seconds`` — fixed audio duration returned
  - ``billed_chars_per_call`` — fixed char count for UsageMeter
  - ``failure_after_n_calls`` — raises ``FakeTTSError`` past this point
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.smart.contracts import TTSResult


class FakeTTSError(RuntimeError):
    """Generic fake-provider TTS failure."""


@dataclass
class FakeTTSProvider:
    """Test double for TTSProvider.

    Returns a synthetic ``TTSResult`` with caller-controlled duration
    and billed_chars. The ``audio_path`` returned points to a fake
    location — callers writing real WAV bytes for downstream pipeline
    integration will need to extend this.
    """

    simulated_duration_seconds: float = 3.0
    billed_chars_per_call: int = 100
    failure_after_n_calls: int | None = None
    provider_name: str = "fake_minimax_tts"
    model_name: str | None = "speech-2.8-hd-fake"
    fake_audio_root: Path = Path("/tmp/fake_tts")

    calls: list[dict[str, Any]] = field(default_factory=list)

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        model_name: str,
    ) -> TTSResult:
        self.calls.append(
            {"text": text, "voice_id": voice_id, "model_name": model_name}
        )
        if self.failure_after_n_calls is not None and len(self.calls) > self.failure_after_n_calls:
            raise FakeTTSError(
                f"fake TTS configured to fail after {self.failure_after_n_calls} "
                f"calls; this is call #{len(self.calls)}"
            )

        # Fake audio path — caller-side tests should not actually open
        # this file. The synthetic duration / billed_chars are what the
        # retry_budget module will compare against thresholds.
        return TTSResult(
            audio_path=self.fake_audio_root / f"call_{len(self.calls):03d}.wav",
            duration_seconds=float(self.simulated_duration_seconds),
            billed_chars=int(self.billed_chars_per_call),
            provider_name=self.provider_name,
            model_name=self.model_name,
        )
