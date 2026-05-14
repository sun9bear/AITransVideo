"""Smart MVP P2 composition root — provider Protocol → real adapter wiring.

⚠️ THIS MODULE LIVES OUTSIDE ``src/services/smart/`` ON PURPOSE.

The AST guard in ``tests/test_smart_skeleton_protocol_guards.py``
forbids ``src/services/smart/**.py`` from importing real provider
modules (``services.voice_clone`` / ``services.tts.*`` / ``services.llm.*``).
This module is the only place those real imports happen for Smart, so
it sits one level up at ``src/services/smart_wiring.py`` — package
sibling, not package member, exempt from the guard.

Usage from a pipeline / job-runner caller:

    from services.smart_wiring import build_smart_clone_provider
    clone_provider = build_smart_clone_provider()  # MiniMax adapter
    # ... pass to auto_voice_review.run(clone_provider=clone_provider, ...)

Tests replace the provider via ``inject_for_test()``:

    from services.smart_wiring import inject_for_test
    from tests.fakes.fake_clone_provider import FakeCloneProvider
    fake = FakeCloneProvider(success=True)
    with inject_for_test(clone_provider=fake):
        # ... exercise auto_voice_review under fake; default restored on exit

Plan refs: §6.0 末段 (wiring lives outside smart/), §5.2 (Smart
auto-clone bypasses the gateway /voice-clone HTTP endpoint and goes
through this protocol-based path so the user is not double-billed
by an independent reserve+capture).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from services.smart.contracts import (
    CloneProvider,
    CloneResult,
    LLMProvider,
    TTSProvider,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Real provider adapters — the only place real provider modules are
# imported for Smart MVP. AST guard skips this file (lives outside
# services.smart package).
# ---------------------------------------------------------------------------


class _MiniMaxCloneAdapter:
    """Wraps ``services.voice_clone.MiniMaxVoiceCloneClient`` to satisfy
    ``CloneProvider`` Protocol.

    Construction is lazy: the real client validates env / config eagerly
    (``VoiceCloneConfig.validate()`` at line ~244 of voice_clone.py),
    so we only build it when the adapter is actually called. This lets
    test environments without MINIMAX_API_KEY env still import the
    Smart wiring module without failing.
    """

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = base_url
        self._client: object | None = None  # lazy

    def clone_voice(
        self,
        *,
        speaker_id: str,
        speaker_name: str,
        source_audio_path: Path,
    ) -> CloneResult:
        # Lazy construction — see docstring rationale.
        if self._client is None:
            # Real provider import lives only in this module. Smart
            # auto-decision code never sees these symbols.
            from services.voice_clone import (
                MiniMaxVoiceCloneClient,
                VoiceCloneConfig,
            )
            import os

            api_key = self._api_key or os.environ.get("MINIMAX_API_KEY") or ""
            if not api_key:
                raise RuntimeError(
                    "MINIMAX_API_KEY missing — Smart clone path can't proceed. "
                    "Set the env var or inject a fake provider via "
                    "smart_wiring.inject_for_test()."
                )
            config = VoiceCloneConfig(
                api_key=api_key,
                base_url=self._base_url or "https://api.minimaxi.com",
            )
            self._client = MiniMaxVoiceCloneClient(config)

        # Translate Smart Protocol args → real client kwargs, then map
        # back the richer VoiceCloneResult to our minimal CloneResult.
        result = self._client.create_voice_clone(  # type: ignore[attr-defined]
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            source_audio_path=source_audio_path,
        )
        return CloneResult(
            speaker_id=result.speaker_id,
            speaker_name=result.speaker_name,
            voice_id=result.voice_id,
            provider_name=result.provider_name,
            model_name=result.model_name,
        )


class _NotWiredTTSAdapter:
    """STUB — raises until retry_budget module (subsequent PR) wires
    real re-TTS through this protocol. See contracts.TTSProvider docstring."""

    def synthesize(self, *, text: str, voice_id: str, model_name: str):
        raise NotImplementedError(
            "Smart TTSProvider not wired yet — retry_budget module lands "
            "in subsequent PR. Inject a fake via "
            "smart_wiring.inject_for_test(tts_provider=...) for tests."
        )


class _NotWiredLLMAdapter:
    """STUB — Smart MVP doesn't call LLMs. Placeholder for P3/P4."""

    def complete(self, *, prompt: str, model_name: str):
        raise NotImplementedError(
            "Smart LLMProvider not wired — Smart MVP auto-decision is "
            "deterministic (no LLM calls). Reserved for P3/P4 verifier work."
        )


# ---------------------------------------------------------------------------
# Default-singleton + test injection
# ---------------------------------------------------------------------------


# Module-level singletons. Lazy-built on first ``build_*`` call so a
# test process that only exercises faking never instantiates a real
# adapter (and never tries to read MINIMAX_API_KEY).
_default_clone_provider: CloneProvider | None = None
_default_tts_provider: TTSProvider | None = None
_default_llm_provider: LLMProvider | None = None

# Test-injected overrides. None means "fall back to default singleton".
_test_clone_override: CloneProvider | None = None
_test_tts_override: TTSProvider | None = None
_test_llm_override: LLMProvider | None = None


def build_smart_clone_provider() -> CloneProvider:
    """Return the Smart CloneProvider — test override if any, else
    the lazily-built MiniMax adapter."""
    global _default_clone_provider
    if _test_clone_override is not None:
        return _test_clone_override
    if _default_clone_provider is None:
        _default_clone_provider = _MiniMaxCloneAdapter()
    return _default_clone_provider


def build_smart_tts_provider() -> TTSProvider:
    """STUB — see ``_NotWiredTTSAdapter``."""
    global _default_tts_provider
    if _test_tts_override is not None:
        return _test_tts_override
    if _default_tts_provider is None:
        _default_tts_provider = _NotWiredTTSAdapter()
    return _default_tts_provider


def build_smart_llm_provider() -> LLMProvider:
    """STUB — see ``_NotWiredLLMAdapter``."""
    global _default_llm_provider
    if _test_llm_override is not None:
        return _test_llm_override
    if _default_llm_provider is None:
        _default_llm_provider = _NotWiredLLMAdapter()
    return _default_llm_provider


@contextmanager
def inject_for_test(
    *,
    clone_provider: CloneProvider | None = None,
    tts_provider: TTSProvider | None = None,
    llm_provider: LLMProvider | None = None,
) -> Iterator[None]:
    """Context manager — temporarily replace one or more default Smart
    providers with test fakes. Restores prior overrides on exit, even
    if the body raises.

    Mainly used by acceptance tests / CI integration tests:

        from tests.fakes.fake_clone_provider import FakeCloneProvider
        from services.smart_wiring import inject_for_test, build_smart_clone_provider

        with inject_for_test(clone_provider=FakeCloneProvider(success=True)):
            provider = build_smart_clone_provider()
            assert provider.clone_voice(...).voice_id == "fake_vt_xxx"
        # default restored automatically here

    Tracks per-axis prior values so nested ``inject_for_test`` calls
    behave like a stack rather than clobbering each other — matters
    when a fixture and a per-test injection compose.
    """
    global _test_clone_override, _test_tts_override, _test_llm_override
    prior_clone = _test_clone_override
    prior_tts = _test_tts_override
    prior_llm = _test_llm_override
    try:
        if clone_provider is not None:
            _test_clone_override = clone_provider
        if tts_provider is not None:
            _test_tts_override = tts_provider
        if llm_provider is not None:
            _test_llm_override = llm_provider
        yield
    finally:
        _test_clone_override = prior_clone
        _test_tts_override = prior_tts
        _test_llm_override = prior_llm


__all__ = [
    "build_smart_clone_provider",
    "build_smart_tts_provider",
    "build_smart_llm_provider",
    "inject_for_test",
]
