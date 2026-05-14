"""Fake provider implementations for Smart MVP P2 acceptance tests.

Plan §8.1 — local development / CI must NOT depend on real paid APIs.
These fakes implement the Protocol surfaces declared in
``src/services/smart/contracts.py`` with controllable behaviour
(success / failure / quota / latency) so the Smart auto-decision
modules in subsequent PRs can be exercised end-to-end.

Surfaces:
  - ``FakeCloneProvider`` — full impl of ``CloneProvider``; controllable
    success rate, quota, and per-call deterministic voice_id.
  - ``FakeTTSProvider`` — minimal impl of ``TTSProvider`` Protocol shape;
    returns a synthetic ``TTSResult`` with caller-controlled duration /
    billed_chars. Will grow when retry_budget module lands.
  - ``FakeLLMProvider`` — placeholder; returns echo of prompt with
    deterministic token counts. Reserved for P3/P4 verifier work.
  - ``InMemoryUsageMeter`` — replays the subset of UsageMeter API
    that Smart code uses, recording calls in memory rather than
    appending to ``usage_events.jsonl``.
"""
from __future__ import annotations

from tests.fakes.fake_clone_provider import FakeCloneProvider
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.fakes.fake_tts_provider import FakeTTSProvider
from tests.fakes.in_memory_usage_meter import InMemoryUsageMeter

__all__ = [
    "FakeCloneProvider",
    "FakeLLMProvider",
    "FakeTTSProvider",
    "InMemoryUsageMeter",
]
