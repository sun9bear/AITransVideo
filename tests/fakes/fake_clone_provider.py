"""Fake CloneProvider — full impl of services.smart.contracts.CloneProvider.

Used by Smart MVP P2 acceptance tests to exercise the auto_voice_review
clone path without hitting MiniMax. Plan §8.1 — local default path
must not require paid APIs.

Knobs:
  - ``success`` — True returns a deterministic CloneResult; False raises
    ``FakeCloneError`` (caller catches → records clone_skipped_reason)
  - ``quota_remaining`` — when 0, raises ``FakeCloneQuotaError`` (smart
    auto_voice_review treats this distinctly from success-rate failures
    per plan §7.3 — quota errors pause the task, success-rate failures
    fall through to preset)
  - ``failure_after_n_calls`` — succeeds the first N calls, then raises
    on the (N+1)th. Used for "retries exhaust budget" tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.smart.contracts import CloneResult


class FakeCloneError(RuntimeError):
    """Generic provider failure — auto_voice_review should fall through
    to preset and record clone_skipped_reason='provider_error'."""


class FakeCloneQuotaError(RuntimeError):
    """Provider quota exhausted — auto_voice_review must pause the task
    rather than fall through (plan §7.3 N=3 safety water mark)."""


@dataclass
class FakeCloneProvider:
    """Test double for CloneProvider.

    Default: succeeds, deterministic voice_id derived from speaker_id.
    Override knobs at construction or per-call by mutating the instance.
    """

    success: bool = True
    quota_remaining: int = 100
    failure_after_n_calls: int | None = None
    provider_name: str = "fake_minimax_voice_clone"
    model_name: str | None = "voice_clone_fake"

    # Recorded calls — tests assert on these to verify the smart
    # auto_voice_review module routed correctly.
    calls: list[dict[str, Any]] = field(default_factory=list)

    def clone_voice(
        self,
        *,
        speaker_id: str,
        speaker_name: str,
        source_audio_path: Path,
    ) -> CloneResult:
        # Record the call before any failure so tests can assert on the
        # invocation even when it fails (e.g. "auto_voice_review tried
        # to clone, then fell through to preset").
        self.calls.append(
            {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "source_audio_path": str(source_audio_path),
            }
        )

        if self.quota_remaining <= 0:
            raise FakeCloneQuotaError(
                f"fake quota exhausted; cannot clone speaker_id={speaker_id!r}"
            )

        if self.failure_after_n_calls is not None and len(self.calls) > self.failure_after_n_calls:
            raise FakeCloneError(
                f"fake provider configured to fail after {self.failure_after_n_calls} "
                f"calls; this is call #{len(self.calls)}"
            )

        if not self.success:
            raise FakeCloneError(
                f"fake provider configured success=False; speaker_id={speaker_id!r}"
            )

        # Decrement quota AFTER success — failed calls don't count
        # against quota in real provider semantics either.
        self.quota_remaining -= 1

        # Deterministic voice_id so tests can assert on exact value.
        # Mirrors production vt_<speaker>_<timestamp> naming pattern with
        # a fixed timestamp for reproducibility.
        return CloneResult(
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            voice_id=f"fake_vt_{speaker_id}_19700101",
            provider_name=self.provider_name,
            model_name=self.model_name,
        )
