"""Smart MVP P2 — provider Protocol接口 + result dataclasses (PR #2 候选 B).

Plan §6.0 / §5.2 / §8.2 #1 — Smart auto-decision modules talk to the
outside world (paid APIs, downstream services) ONLY through the
Protocol interfaces defined here. Real provider adapters live in the
sibling module ``src/services/smart_wiring.py`` (deliberately OUTSIDE
the ``services.smart`` package — see §6.0 末段). Test fakes live in
``tests/fakes/``.

The AST guard in tests/test_smart_skeleton_protocol_guards.py
verifies no module under ``src/services/smart/`` imports the real
provider modules directly (``services.voice_clone`` /
``services.voice.auto_clone`` / ``services.tts.*`` / ``services.llm.*``).
This is what keeps Smart auto-decision modules unit-testable without
booting a full pipeline + paid API stack.

Protocol coverage rationale (which surfaces are protocol-ised, which
aren't):

  - ``CloneProvider`` — REAL surface needed today. Smart auto-decision
    code calls ``clone_voice(...)`` directly (NOT through the gateway
    /voice-clone HTTP endpoint, per plan §5.2 + Codex F4). The
    composition root in smart_wiring.py wraps
    ``services.voice_clone.MiniMaxVoiceCloneClient``.

  - ``TTSProvider`` — STUB Protocol shape. Smart pipeline does NOT
    call TTS directly — re-TTS retries flow through the existing
    pipeline TTS path in ``src/pipeline/process.py``. The Protocol is
    declared here so the F4 retry_budget module (subsequent PR) has a
    type to import; the real adapter is intentionally not wired yet
    (raises NotImplementedError on ``synthesize`` until the retry
    path lands).

  - ``LLMProvider`` — STUB Protocol shape. Smart auto-decision is
    deterministic (glossary checksum / length budget / speaker
    consistency / etc.) and does not call LLMs. Declared as a
    placeholder so future verifier work (P3/P4) has a slot. Real
    adapter intentionally not wired.

Why dataclasses instead of dicts for results: the runner / sidecar
emitter / tests need typed access to provider responses so refactors
of the underlying provider don't silently change Smart behaviour.
``CloneResult`` mirrors the existing ``services.voice_clone.VoiceCloneResult``
shape minus the file-system details that aren't relevant to the
Smart decision (uploaded_file_id is provider bookkeeping).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloneResult:
    """Outcome of one clone attempt — what Smart needs to record / decide on.

    Mirrors the public-facing fields of
    ``services.voice_clone.VoiceCloneResult`` minus the upload bookkeeping
    that's specific to the MiniMax HTTP flow. ``provider_name`` /
    ``model_name`` are kept so smart_decisions.jsonl can record provenance
    for downstream cost / quality analysis (plan §6.4).
    """

    speaker_id: str
    speaker_name: str
    voice_id: str
    provider_name: str
    model_name: str | None


@dataclass(frozen=True)
class TTSResult:
    """Stub TTSResult shape — final shape lands when the retry_budget
    module (subsequent PR) wires real re-TTS through this Protocol.

    Kept minimal on purpose: ``audio_path`` + ``duration_seconds`` is
    what the duration-quality check needs; ``billed_chars`` is what the
    UsageMeter records. The real adapter will likely return more
    (per-segment timing, provider request id, etc.) — extend the
    dataclass when those become Smart-decision-relevant.
    """

    audio_path: Path
    duration_seconds: float
    billed_chars: int
    provider_name: str
    model_name: str | None


@dataclass(frozen=True)
class LLMResult:
    """Stub LLMResult shape — placeholder for P3/P4 verifier work.

    Smart MVP does NOT call LLMs (auto-decision is deterministic). When
    the multimodal verifier work lands, this shape will carry whatever
    the verifier needs to publish back — probably a structured
    proposal dict + token usage for cost accounting.
    """

    text: str
    input_tokens: int
    output_tokens: int
    model_name: str


# ---------------------------------------------------------------------------
# Protocol interfaces
# ---------------------------------------------------------------------------


@runtime_checkable
class CloneProvider(Protocol):
    """Smart auto_voice_review's clone capability.

    Single method, sync (the underlying real adapter is sync; running it
    inside ``loop.run_in_executor`` is the caller's job, mirroring how
    ``gateway/voice_selection_api.py:439`` already wraps it).

    Args:
      speaker_id: Internal stable id (e.g. ``"speaker_a"``). Used as
        the ``vt_<speaker>_<timestamp>`` voice-id stem in production.
      speaker_name: Human-readable display name (Chinese ok). Surfaces
        in the user-visible voice library list.
      source_audio_path: Concatenated speaker sample (≥10s per Codex F5
        / plan §6.2.1 hard floor). Caller built this via ffmpeg from
        S2-selected segments before invoking.

    Returns:
      ``CloneResult`` on success.

    Raises:
      Implementation-specific exception on provider failure / quota /
      input rejection. Smart auto_voice_review catches these and
      writes ``smart_decisions.jsonl::clone_skipped_reason``.

    NOTE: This protocol is the ONLY clone surface Smart auto-decision
    code may call. Direct imports of ``services.voice_clone.*`` from
    ``services.smart.*`` are forbidden by the AST guard in
    ``tests/test_smart_skeleton_protocol_guards.py``.
    """

    def clone_voice(
        self,
        *,
        speaker_id: str,
        speaker_name: str,
        source_audio_path: Path,
    ) -> CloneResult:
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """STUB Protocol — TTSResult shape kept minimal until retry_budget
    module lands. See module docstring for rationale.

    The intended call site is single-segment re-TTS during retry —
    NOT batch first-pass TTS (that stays in ``services.tts.tts_generator``).
    Smart's retry_budget module will own the budget cap + per-segment
    re-TTS dispatch through this protocol.
    """

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        model_name: str,
    ) -> TTSResult:
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """STUB Protocol — placeholder for P3/P4 verifier work. See module
    docstring. Real LLM calls do NOT happen in Smart MVP.
    """

    def complete(
        self,
        *,
        prompt: str,
        model_name: str,
    ) -> LLMResult:
        ...


__all__ = [
    "CloneProvider",
    "CloneResult",
    "LLMProvider",
    "LLMResult",
    "TTSProvider",
    "TTSResult",
]
