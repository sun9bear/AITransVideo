"""In-memory replacement for services.usage_meter.UsageMeter.

Plan §8.1 — replays the subset of UsageMeter API that Smart auto-
decision modules use, recording calls in memory rather than appending
to ``{project_dir}/usage_events.jsonl``. Lets Smart unit tests run
without a real filesystem layout / job_id / project_dir.

Real UsageMeter (src/services/usage_meter.py:57) has many record_*
methods; this fake covers the ones Smart actually invokes:

  - ``record_voice_clone(...)`` — auto_voice_review on successful clone
  - ``record_tts_call(...)`` — retry_budget per re-TTS attempt
    (placeholder method; real call site lands with retry_budget)
  - ``summarize()`` — partial_capture_actual_cost reads this for
    fail_and_refund settlement (plan §5.2 step 3)

Extend the surface as Smart code grows new instrumentation
requirements; keep behaviour pure-Python (no file I/O) so fakes
compose with monkeypatching cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InMemoryUsageMeter:
    """Drop-in for ``services.usage_meter.UsageMeter`` on test paths."""

    voice_clone_calls: list[dict[str, Any]] = field(default_factory=list)
    tts_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)

    def record_voice_clone(
        self,
        *,
        provider: str,
        model: str | None,
        voice_id: str,
        speaker_id: str,
        source_audio_seconds: float = 0.0,
        source_audio_bytes: int = 0,
        selected_segment_count: int = 0,
        clone_count: int = 1,
        billable: bool = True,
        success: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.voice_clone_calls.append(
            {
                "provider": provider,
                "model": model,
                "voice_id": voice_id,
                "speaker_id": speaker_id,
                "source_audio_seconds": source_audio_seconds,
                "source_audio_bytes": source_audio_bytes,
                "selected_segment_count": selected_segment_count,
                "clone_count": clone_count,
                "billable": billable,
                "success": success,
                "extra": dict(extra or {}),
            }
        )

    def record_tts_call(
        self,
        *,
        provider: str,
        model: str | None,
        voice_id: str,
        billed_chars: int,
        bucket: str = "post_tts_resynth",
        success: bool = True,
    ) -> None:
        self.tts_calls.append(
            {
                "provider": provider,
                "model": model,
                "voice_id": voice_id,
                "billed_chars": billed_chars,
                "bucket": bucket,
                "success": success,
            }
        )

    def record_llm_call(
        self,
        *,
        provider: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        task: str = "smart_verifier",
    ) -> None:
        self.llm_calls.append(
            {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "task": task,
            }
        )

    def summarize(self) -> dict[str, Any]:
        """Mirror the bucket-aware shape of real UsageMeter.summarize().

        Returns a dict that Smart's partial_capture_actual_cost helper
        (subsequent PR) can consume. Keys match the real implementation
        for the buckets Smart cares about; everything else returns 0.
        """
        return {
            "voice_clone_call_count": len(self.voice_clone_calls),
            "voice_clone_billable_count": sum(
                1 for c in self.voice_clone_calls if c["billable"] and c["success"]
            ),
            "tts_call_count": len(self.tts_calls),
            "tts_billed_chars": sum(c["billed_chars"] for c in self.tts_calls),
            # Per-bucket — Smart cost summary (plan §4.6) needs these
            # to derive tts_chars_wasted_in_retries.
            "post_tts_resynth_billed_chars": sum(
                c["billed_chars"] for c in self.tts_calls
                if c["bucket"] == "post_tts_resynth"
            ),
            "post_edit_resynth_billed_chars": sum(
                c["billed_chars"] for c in self.tts_calls
                if c["bucket"] == "post_edit_resynth"
            ),
            "first_tts_billed_chars": sum(
                c["billed_chars"] for c in self.tts_calls
                if c["bucket"] == "first_tts"
            ),
            "llm_call_count": len(self.llm_calls),
            "llm_input_tokens": sum(c["input_tokens"] for c in self.llm_calls),
            "llm_output_tokens": sum(c["output_tokens"] for c in self.llm_calls),
        }
