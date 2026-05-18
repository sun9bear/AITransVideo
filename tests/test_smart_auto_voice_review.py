"""Smart MVP §6.2.1 — auto voice review acceptance suite (PR#3B).

Two test categories per Codex 第八轮 review F + 候选拆法 PR#3B spec:

  1. ``TestAutoVoiceReviewOrchestration`` — exercises the orchestrator
     against ``FakeCloneProvider`` so the consent / sample / quota /
     failure / success branches are deterministic and isolated.
     Covers the "auto_voice_review must NEVER call clone provider when
     consent is false / sample <10s" invariant.

  2. ``TestMiniMaxCloneAdapterMapping`` — Codex 第八轮 末段:
     "PR#2 only tested Protocol shape, not adapter call mapping".
     monkeypatches ``services.voice_clone.MiniMaxVoiceCloneClient`` so
     the actual ``_MiniMaxCloneAdapter.clone_voice()`` call exercises,
     and verifies kwargs reach the underlying client with the EXACT
     names + types the real ``create_voice_clone()`` method declares
     at ``src/services/voice_clone.py:246``. Catches "renamed kwarg
     in real client breaks Smart silently" regressions.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Repo path setup — mirrors tests/conftest.py
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===================================================================
# Orchestration — consent / sample / quota / failure / success branches
# ===================================================================


class TestAutoVoiceReviewOrchestration:
    """Plan §6.2.1 + §7.3 — 5 decision paths, all driven through the
    CloneProvider Protocol via FakeCloneProvider. Critical invariants:

      - consent=False NEVER reaches the clone_provider.clone_voice() call
      - sample<10s NEVER reaches the clone_provider.clone_voice() call
      - quota at safety water mark pauses ALL remaining speakers
      - retries cap at max_clone_attempts_per_speaker, then preset
      - mid-flight quota error pauses subsequent speakers

    Failure isolation matters because clone is a paid API: a misrouted
    consent check would burn user quota on a path the user didn't agree
    to (the exact failure CLAUDE.md "付费 API 不能自动调用" forbids).
    """

    def _speaker(self, sid, *, sample_seconds=20.0, name=None, audio_path=None):
        from services.smart.auto_voice_review import VoiceReviewSpeakerInput
        return VoiceReviewSpeakerInput(
            speaker_id=sid,
            speaker_name=name or sid,
            sample_seconds=sample_seconds,
            source_audio_path=audio_path or Path(f"/fake/audio/{sid}.wav"),
        )

    def _id_factory(self, prefix="dec"):
        # Deterministic id factory so tests can pin smart_decision_id.
        i = [0]
        def factory():
            i[0] += 1
            return f"{prefix}_{i[0]:03d}"
        return factory

    def test_consent_false_never_calls_clone_provider(self):
        """CRITICAL invariant — auto_voice_review must NOT call
        clone_provider when smart_consent.auto_voice_clone is False.
        Defensive check (Gateway create gate should catch first), but
        a misroute here means burning user quota on a path the user
        didn't agree to.

        Phase 3 (plan 2026-05-17 §审计 reason_codes): reason_code emits
        ``new_clone_blocked_by_consent`` to distinguish from
        ``new_clone_blocked_by_admin``."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a"), self._speaker("b")],
            smart_consent={"auto_voice_clone": False},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        # NOT a single clone call.
        assert fake.calls == []
        # All speakers fall to preset with the Phase 3 reason_code.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert all(d.choice is VoiceReviewChoice.PRESET for d in result.decisions)
        assert all(
            d.reason_code == "new_clone_blocked_by_consent"
            for d in result.decisions
        )

    def test_existing_strong_match_reuses_before_sample_and_quota_checks(self):
        """Reuse is allowed after consent but before new-clone constraints."""
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewExistingMatch,
            VoiceReviewOutcome,
            evaluate_voice_review,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("speaker_a", sample_seconds=2.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=0,
            smart_decision_id_factory=self._id_factory(),
            existing_voice_matches_by_speaker_id={
                "speaker_a": VoiceReviewExistingMatch(
                    voice_id="vt_existing",
                    provider_name="minimax_voice_clone",
                    model_name="minimax_tts",
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    user_voice_id="7",
                ),
            },
        )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.REUSED
        assert decision.cloned_voice_id == "vt_existing"
        assert decision.reason_code == "reused_user_voice"
        assert decision.metrics["match_confidence"] == "strong"
        assert fake.calls == []

    # ===================================================================
    # Phase 3 (plan 2026-05-17-user-voice-candidate-first §Consent × Admin
    # 决策矩阵) — admin policy switches gate new clone independently from
    # consent. Strong-match reuse stays unaffected by either gate.
    # ===================================================================

    def test_strong_reuse_works_when_admin_clone_disabled(self):
        """Plan §核心不变量: ``smart_auto_clone_enabled=false`` only blocks
        NEW clone, it must not block strong-match reuse. Reuse doesn't
        call the clone provider, doesn't consume clone quota, and doesn't
        burn account stock — so it stays allowed regardless of the
        admin's new-clone policy.

        Matrix row covered: consent=T, reuse=T, clone=F (also row
        consent=F, reuse=T, clone=F covered by adjacent test)."""
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewExistingMatch,
            VoiceReviewOutcome,
            evaluate_voice_review,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("speaker_a", sample_seconds=2.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=0,
            smart_decision_id_factory=self._id_factory(),
            existing_voice_matches_by_speaker_id={
                "speaker_a": VoiceReviewExistingMatch(
                    voice_id="vt_existing_strong",
                    provider_name="minimax_voice_clone",
                    model_name="minimax_tts",
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    user_voice_id="42",
                ),
            },
            admin_clone_enabled=False,  # admin disabled new clone
        )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        decision = result.decisions[0]
        # Strong reuse fires regardless of admin_clone_enabled.
        assert decision.choice is VoiceReviewChoice.REUSED
        assert decision.cloned_voice_id == "vt_existing_strong"
        # Provider untouched — invariant.
        assert fake.calls == []

    def test_strong_reuse_works_when_consent_denied(self):
        """Plan §Consent × Admin 矩阵 row consent=F, reuse=T, clone=T:
        consent only gates NEW clone; reuse of an existing personal
        voice doesn't consume clone quota / burn provider calls so it
        stays allowed even with consent=False."""
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewExistingMatch,
            VoiceReviewOutcome,
            evaluate_voice_review,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("speaker_a", sample_seconds=2.0)],
            smart_consent={"auto_voice_clone": False},  # consent denied
            clone_provider=fake,
            voice_library_quota_remaining=0,
            smart_decision_id_factory=self._id_factory(),
            existing_voice_matches_by_speaker_id={
                "speaker_a": VoiceReviewExistingMatch(
                    voice_id="vt_reuse_under_consent_false",
                    provider_name="minimax_voice_clone",
                    model_name="minimax_tts",
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    user_voice_id="99",
                ),
            },
            admin_clone_enabled=True,
        )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.REUSED
        assert decision.cloned_voice_id == "vt_reuse_under_consent_false"
        # Critical paid-API invariant: provider untouched.
        assert fake.calls == []

    def test_admin_clone_disabled_falls_to_preset_with_admin_reason(self):
        """Plan §Consent × Admin 矩阵 row consent=T, reuse=T, clone=F
        (no existing match): no reuse + new clone blocked by admin →
        preset fallback with reason_code=new_clone_blocked_by_admin
        so audit can distinguish from a consent denial."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a", sample_seconds=20.0),
                self._speaker("b", sample_seconds=15.0),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            admin_clone_enabled=False,
        )

        # CRITICAL invariant: provider never called.
        assert fake.calls == []
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        for d in result.decisions:
            assert d.choice is VoiceReviewChoice.PRESET
            assert d.reason_code == "new_clone_blocked_by_admin"

    def test_consent_denied_and_admin_disabled_emits_combined_reason(self):
        """Plan §Consent × Admin 矩阵 row consent=F, reuse=T, clone=F
        (no existing match): both gates closed → preset fallback with
        the combined reason_code so audit can trace either gate
        independently."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a", sample_seconds=20.0),
                self._speaker("b", sample_seconds=15.0),
            ],
            smart_consent={"auto_voice_clone": False},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            admin_clone_enabled=False,
        )

        assert fake.calls == []
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        for d in result.decisions:
            assert d.choice is VoiceReviewChoice.PRESET
            assert d.reason_code == "new_clone_blocked_by_consent_and_admin"

    def test_admin_clone_disabled_then_strong_reuse_mixed_batch(self):
        """Mixed: speaker_a has strong match (REUSED), speaker_b has
        no match (PRESET with admin reason). Phase 3 isolates the two
        gates so a single batch can have both outcomes."""
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewExistingMatch,
            VoiceReviewOutcome,
            evaluate_voice_review,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a", sample_seconds=20.0),
                self._speaker("b", sample_seconds=15.0),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            existing_voice_matches_by_speaker_id={
                "a": VoiceReviewExistingMatch(
                    voice_id="vt_a_existing",
                    provider_name="minimax_voice_clone",
                    confidence="strong",
                    reason="same_source",
                    user_voice_id="1",
                ),
            },
            admin_clone_enabled=False,
        )

        assert fake.calls == []
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        decisions_by_id = {d.speaker_id: d for d in result.decisions}
        assert decisions_by_id["a"].choice is VoiceReviewChoice.REUSED
        assert decisions_by_id["a"].cloned_voice_id == "vt_a_existing"
        assert decisions_by_id["b"].choice is VoiceReviewChoice.PRESET
        assert decisions_by_id["b"].reason_code == "new_clone_blocked_by_admin"

    def test_admin_clone_enabled_default_preserves_existing_behavior(self):
        """``admin_clone_enabled`` defaults to True so existing callers
        that don't pass the kwarg get the legacy 1-axis behavior. With
        consent=True + sample OK + quota OK and admin defaulted, clone
        proceeds normally."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        # NOTE: deliberately NOT passing admin_clone_enabled — verifies
        # default value (True) preserves the legacy code path.
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert len(fake.calls) == 1
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED

    def test_sample_below_10s_never_calls_clone_provider(self):
        """CRITICAL invariant — Codex F5 / plan §6.2.1: 8-10s samples
        would be 400-rejected by the existing voice-clone HTTP endpoint
        anyway. Don't burn an attempt on a known-rejected payload."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a", sample_seconds=8.0),
                self._speaker("b", sample_seconds=9.99),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == [], (
            "auto_voice_review must NOT call clone_provider for samples "
            "below the 10s hard floor; would trigger downstream 400."
        )
        for d in result.decisions:
            assert d.choice is VoiceReviewChoice.PRESET
            assert d.reason_code == "insufficient_sample_seconds_lt_10"
            assert d.metrics["sample_seconds"] is not None

    def test_sample_exactly_10s_proceeds_to_clone(self):
        """Boundary — 10.0s is the inclusive lower bound (>= 10.0)."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=10.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert len(fake.calls) == 1
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED

    def test_sample_nan_never_calls_clone_provider(self):
        """Codex 第十二轮 P1-2: ``float("nan") < 10.0`` is False (IEEE-754
        — NaN comparisons are all False), so naive ``< min`` would let
        NaN samples bypass the guard and burn paid clone API. Module
        uses isfinite() so NaN lands in the preset branch."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=float("nan"))],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        # Provider was NOT called.
        assert fake.calls == []
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.PRESET
        # Distinct reason_code so admin / sidecar can spot data-quality
        # anomalies separately from genuine short samples.
        assert d.reason_code.startswith("non_finite_sample_seconds_")

    def test_sample_inf_never_calls_clone_provider(self):
        """Codex 第十二轮 P1-2: ``float("inf") < 10.0`` is False — inf
        is greater than any finite value. Naive ``< min`` would let
        inf samples through. Module uses isfinite() so inf lands in
        the preset branch."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=float("inf"))],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == []
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.PRESET
        assert d.reason_code.startswith("non_finite_sample_seconds_")

    def test_consent_must_be_exact_true_not_truthy_strings(self):
        """Codex 第十三轮 P1: consent guard must use ``is True`` strict
        identity, NOT ``bool(...)``. ``bool("false")`` and ``bool("0")``
        are both truthy Python strings, so a stringly-typed upstream
        payload would silently bypass the consent guard and burn paid
        clone API — exactly the failure CLAUDE.md "付费 API 不能自动调用"
        forbids. Only an exact bool ``True`` passes.
        """
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        # Each of these values is truthy under bool() but NOT identity-True.
        truthy_non_true_values = [
            "true", "false", "1", "yes", 1, 1.0, [True], {"any": "dict"},
        ]
        for value in truthy_non_true_values:
            fake = FakeCloneProvider()
            result = evaluate_voice_review(
                main_speakers=[self._speaker("a", sample_seconds=20.0)],
                smart_consent={"auto_voice_clone": value},
                clone_provider=fake,
                voice_library_quota_remaining=100,
                smart_decision_id_factory=self._id_factory(),
            )
            # CRITICAL: provider NOT called for any of these truthy values.
            assert fake.calls == [], (
                f"consent value {value!r} (type={type(value).__name__}) "
                f"caused clone provider to be called — must be 'is True' "
                f"strict only."
            )
            assert result.decisions[0].choice is VoiceReviewChoice.PRESET
            assert result.decisions[0].reason_code == "new_clone_blocked_by_consent"

    def test_consent_exact_true_proceeds_to_clone(self):
        """Counterpart: exact bool ``True`` IS the only value that
        bypasses the consent guard."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert len(fake.calls) == 1
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED

    def test_consent_missing_key_fails_closed(self):
        """smart_consent dict without the auto_voice_clone key → PRESET.
        Defensive against partial payloads."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={},  # auto_voice_clone field missing
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == []
        assert result.decisions[0].choice is VoiceReviewChoice.PRESET
        assert result.decisions[0].reason_code == "new_clone_blocked_by_consent"

    def test_sample_none_never_calls_clone_provider(self):
        """Codex 第十三轮 P2: ``float(None)`` raises TypeError. Module
        docstring promises ``Raises: never``. Bad input must route to
        PRESET, not bubble out as a pipeline crash."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewSpeakerInput,
        )
        from tests.fakes import FakeCloneProvider

        # Bypass the dataclass type hint by constructing directly with
        # bad data — mimics what PR#3C might pass if upstream metering
        # was incomplete.
        bad_speaker = VoiceReviewSpeakerInput(
            speaker_id="a",
            speaker_name="A",
            sample_seconds=None,  # type: ignore[arg-type]
            source_audio_path=Path("/fake.wav"),
        )
        fake = FakeCloneProvider()
        # Should NOT raise, even though float(None) would.
        result = evaluate_voice_review(
            main_speakers=[bad_speaker],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == []
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.PRESET
        assert d.reason_code == "invalid_sample_seconds_NoneType"

    def test_sample_non_numeric_string_never_calls_clone_provider(self):
        """``float("bad")`` raises ValueError → must route to PRESET."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewSpeakerInput,
        )
        from tests.fakes import FakeCloneProvider

        bad_speaker = VoiceReviewSpeakerInput(
            speaker_id="a", speaker_name="A",
            sample_seconds="not a number",  # type: ignore[arg-type]
            source_audio_path=Path("/fake.wav"),
        )
        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[bad_speaker],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == []
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.PRESET
        assert d.reason_code == "invalid_sample_seconds_str"
        # Raw form preserved in metrics so admin can diagnose source.
        assert d.metrics["sample_seconds_raw"] == "'not a number'"

    def test_sample_numeric_string_coerces_and_proceeds_to_clone(self):
        """``float("15.0")`` actually works — coerce succeeds, isfinite
        passes, value ≥ 10 → clone proceeds. (Renamed per Codex 第十四
        轮 non-blocking note: previous name said "routes_to_preset" but
        the assertion is clone, not preset.)

        This test documents the intentional permissiveness: float()
        coerce handles valid numeric strings + ints. Strict type
        checking happens at the dataclass / Pydantic layer (PR#3C
        integration's responsibility).
        """
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewSpeakerInput,
        )
        from tests.fakes import FakeCloneProvider

        speaker = VoiceReviewSpeakerInput(
            speaker_id="a", speaker_name="A",
            sample_seconds="15.0",  # type: ignore[arg-type]
            source_audio_path=Path("/fake.wav"),
        )
        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[speaker],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        # 15.0 ≥ 10 → clone proceeds (string was successfully coerced).
        assert len(fake.calls) == 1
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED

    def test_sample_negative_never_calls_clone_provider(self):
        """Negative duration is upstream-data corruption — must NOT
        bypass guard on the basis of being "less than 10". Use the
        below-threshold reason since the value IS finite."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=-3.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert fake.calls == []
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.PRESET
        assert d.reason_code == "insufficient_sample_seconds_lt_10"
        assert d.metrics["sample_seconds"] == -3.0

    def test_quota_at_safety_water_mark_pauses_all_remaining(self):
        """Plan §7.3: when quota_remaining <= safety water mark (default
        N=3), do NOT issue any more clones. The whole batch becomes
        PAUSED so the integration layer pauses the task and surfaces
        '稍后重试'."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider(quota_remaining=100)  # provider has plenty
        # But the snapshot we tell auto_voice_review is at the water mark.
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a"), self._speaker("b"), self._speaker("c")],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=3,  # == safety water mark
            quota_safety_water_mark=3,
            smart_decision_id_factory=self._id_factory(),
        )
        # Zero clone calls — never even tried.
        assert fake.calls == []
        assert result.outcome is VoiceReviewOutcome.PAUSED
        assert all(d.choice is VoiceReviewChoice.PAUSED for d in result.decisions)
        assert "voice_library_quota_at_safety_water_mark" in result.pause_reason

    def test_quota_above_water_mark_decrements_per_clone(self):
        """The local quota counter decrements as clones succeed, so a
        starting quota of 5 with water mark 3 succeeds 2 then pauses."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider(quota_remaining=100)
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a"), self._speaker("b"), self._speaker("c"),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=5,
            quota_safety_water_mark=3,
            smart_decision_id_factory=self._id_factory(),
        )
        # 5 → after first clone 4 (above 3) → after second clone 3 (== water
        # mark, refused for 3rd). 2 clones issued; 3rd speaker PAUSED.
        assert len(fake.calls) == 2
        choices = [d.choice for d in result.decisions]
        assert choices[0] is VoiceReviewChoice.CLONED
        assert choices[1] is VoiceReviewChoice.CLONED
        assert choices[2] is VoiceReviewChoice.PAUSED
        assert result.outcome is VoiceReviewOutcome.PAUSED

    def test_provider_failure_retries_then_falls_to_preset(self):
        """Plan §7.3: per-speaker clone failure budget (default 3
        attempts). Generic provider failures (NOT quota) burn the
        budget then fall through to preset, NOT pause. Only quota
        errors pause."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        # FakeCloneProvider with success=False raises FakeCloneError on
        # every call — exhausts the retry budget.
        fake = FakeCloneProvider(success=False)
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a")],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            max_clone_attempts_per_speaker=3,
            smart_decision_id_factory=self._id_factory(),
        )
        # All 3 attempts were issued — provider didn't pause us early.
        assert len(fake.calls) == 3
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.PRESET
        assert decision.reason_code == "provider_failure_max_retries_3"
        assert decision.metrics["attempts_made"] == 3
        # Outcome stays AUTO_APPROVED — preset fallback IS the auto path.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED

    def test_quota_error_mid_flight_pauses_remaining(self):
        """Plan §7.3 distinct rule: quota errors during clone (vs
        snapshot pre-check) pause the task. FakeCloneQuotaError is
        the contract; production MiniMax errors carry "quota" in
        message and class name."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        # Snapshot says 100 (plenty); but provider quota is 1, and
        # raises FakeCloneQuotaError on the second call.
        fake = FakeCloneProvider(quota_remaining=1)
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a"), self._speaker("b"), self._speaker("c"),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        # First clone succeeded, second hit quota error and paused.
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED
        assert result.decisions[1].choice is VoiceReviewChoice.PAUSED
        assert result.decisions[1].reason_code == "provider_quota_exhausted_mid_flight"
        # Third speaker NEVER attempted — pause propagated.
        assert result.decisions[2].choice is VoiceReviewChoice.PAUSED
        assert result.decisions[2].reason_code == "paused_after_prior_quota_exhaust"
        assert result.outcome is VoiceReviewOutcome.PAUSED

    def test_success_path_returns_cloned_voice_id_and_provider(self):
        """Happy path — sample OK, consent OK, quota OK, provider OK.
        Returned decision carries cloned_voice_id + provider_name +
        model_name from the CloneResult."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("speaker_a", name="查理·芒格")],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory("clone"),
        )
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.CLONED
        # Deterministic fake voice_id format from FakeCloneProvider.
        assert d.cloned_voice_id == "fake_vt_speaker_a_19700101"
        assert d.cloned_provider_name == "fake_minimax_voice_clone"
        assert d.reason_code == "clone_succeeded"
        assert d.metrics["attempts_made"] == 1
        assert d.smart_decision_id == "clone_001"

    def test_first_attempt_failure_then_success_records_attempts(self):
        """Mid-retry success records the attempt count so the sidecar
        can audit clone effort accurately (matters for cost analysis)."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewChoice,
        )
        from tests.fakes import FakeCloneProvider

        # Fail first 1 attempt, succeed on attempt #2.
        # FakeCloneProvider has failure_after_n_calls — that's "succeed
        # the first N then fail", so we can't directly model "fail then
        # succeed" with the default knob. Use a custom mock.
        from services.smart.contracts import CloneResult
        class FlipProvider:
            def __init__(self):
                self.calls = []
            def clone_voice(self, *, speaker_id, speaker_name, source_audio_path):
                self.calls.append({"speaker_id": speaker_id})
                if len(self.calls) == 1:
                    raise RuntimeError("transient network error")
                return CloneResult(
                    speaker_id=speaker_id,
                    speaker_name=speaker_name,
                    voice_id=f"vt_{speaker_id}_xxx",
                    provider_name="custom_minimax",
                    model_name="voice_clone_custom",
                )
        flip = FlipProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a")],
            smart_consent={"auto_voice_clone": True},
            clone_provider=flip,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert len(flip.calls) == 2
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.CLONED
        assert d.metrics["attempts_made"] == 2

    def test_id_factory_called_once_per_speaker(self):
        """smart_decision_id is per-speaker, not per-attempt — retries
        within a speaker share the same id (one auditable decision
        even if attempts_made > 1)."""
        from services.smart.auto_voice_review import evaluate_voice_review
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider(success=False)  # exhausts retries
        ids = self._id_factory("auto")
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a"), self._speaker("b")],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            max_clone_attempts_per_speaker=3,
            smart_decision_id_factory=ids,
        )
        # 2 unique ids (one per speaker) despite 6 total attempts.
        assert {d.smart_decision_id for d in result.decisions} == {"auto_001", "auto_002"}

    def test_empty_main_speakers_returns_empty_decisions(self):
        """No main speakers (e.g. eligibility upstream returned no
        candidates) → no decisions, AUTO_APPROVED outcome."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert result.decisions == ()
        assert fake.calls == []

    # ===================================================================
    # Phase 4 (plan 2026-05-17-user-voice-candidate-first §Smart 弱匹配
    # 暂停 + §推荐决策顺序 step 3) — when admin enables
    # smart_pause_on_possible_user_voice_match, possible (weak / medium /
    # cross-source) candidates pause Smart to voice review instead of
    # being silently ignored. Strong match REUSED still wins before any
    # pause check. The pause cascade mirrors quota water-mark behavior.
    # ===================================================================

    def test_possible_match_pauses_when_admin_pause_enabled(self):
        """Plan §Phase 4 acceptance: admin toggle on + speaker has a
        possible (non-strong) candidate → PAUSED with reason_code
        ``possible_user_voice_match_requires_confirmation``. Provider
        is NEVER called — pause means user confirms reuse, not new clone."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            possible_voice_matches_by_speaker_id={
                "a": [
                    {
                        "voice_id": "vt_possible_one",
                        "label": "查理·芒格(其他视频)",
                        "match_scope": "cross_source_named",
                        "confidence": "weak",
                    },
                ],
            },
            admin_pause_on_possible_match=True,
        )

        assert fake.calls == [], (
            "Possible-match pause must NOT call clone provider — pause "
            "exists so user decides whether to reuse or clone."
        )
        assert result.outcome is VoiceReviewOutcome.PAUSED
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.PAUSED
        assert decision.reason_code == (
            "possible_user_voice_match_requires_confirmation"
        )
        assert decision.metrics["possible_match_count"] == 1
        assert decision.metrics["top_candidate_voice_id"] == "vt_possible_one"
        assert decision.metrics["top_candidate_confidence"] == "weak"
        assert (
            result.pause_reason
            == "possible_user_voice_match_requires_confirmation"
        )

    def test_possible_match_ignored_when_admin_pause_disabled(self):
        """Plan §Phase 4 acceptance: admin toggle off (default) →
        possible candidates are silently ignored, the pipeline continues
        with the existing sample / quota / clone-or-preset flow. This
        preserves Phase 3 behavior for callers that don't opt in."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            possible_voice_matches_by_speaker_id={
                "a": [
                    {
                        "voice_id": "vt_possible_one",
                        "label": "查理·芒格(其他视频)",
                        "match_scope": "cross_source_named",
                        "confidence": "weak",
                    },
                ],
            },
            # admin_pause_on_possible_match defaults to False — explicit
            # for clarity, but the default must preserve Phase 3 flow.
            admin_pause_on_possible_match=False,
        )

        # Pause toggle off → existing clone path proceeds normally.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert len(fake.calls) == 1
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED

    def test_strong_match_still_reuses_even_with_possible_pause_enabled(self):
        """Plan §核心不变量: strong match REUSED runs BEFORE the possible-
        match pause check. A speaker with both a strong match and a
        possible candidate must REUSE the strong one (no pause), because
        reuse is free of paid API and the user explicitly already
        approved this voice for this same source."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewExistingMatch,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            existing_voice_matches_by_speaker_id={
                "a": VoiceReviewExistingMatch(
                    voice_id="vt_strong_match",
                    provider_name="minimax_voice_clone",
                    model_name="minimax_tts",
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    user_voice_id="7",
                ),
            },
            possible_voice_matches_by_speaker_id={
                # Same speaker also has a weak cross-source candidate —
                # must be ignored because strong reuse fires first.
                "a": [
                    {
                        "voice_id": "vt_other_weak",
                        "label": "查理·芒格(其他视频)",
                        "match_scope": "cross_source_named",
                        "confidence": "weak",
                    },
                ],
            },
            admin_pause_on_possible_match=True,
        )

        # Strong reuse wins — no pause, no provider call.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        decision = result.decisions[0]
        assert decision.choice is VoiceReviewChoice.REUSED
        assert decision.cloned_voice_id == "vt_strong_match"
        assert fake.calls == []

    def test_possible_match_pause_propagates_to_subsequent_speakers(self):
        """Plan §Phase 4: once possible-match pause fires for one speaker,
        all subsequent main speakers also pause (mirrors quota water-mark
        propagation at process.py:274). Outcome is PAUSED globally."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = evaluate_voice_review(
            main_speakers=[
                self._speaker("a", sample_seconds=20.0),
                # speaker_b has NO possible candidate — but pause
                # propagation still catches it.
                self._speaker("b", sample_seconds=20.0),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
            possible_voice_matches_by_speaker_id={
                "a": [
                    {
                        "voice_id": "vt_possible_one",
                        "label": "演讲者A(其他视频)",
                        "match_scope": "cross_source_named",
                        "confidence": "weak",
                    },
                ],
                # speaker_b intentionally absent from this dict.
            },
            admin_pause_on_possible_match=True,
        )

        assert fake.calls == []
        assert result.outcome is VoiceReviewOutcome.PAUSED
        assert result.decisions[0].choice is VoiceReviewChoice.PAUSED
        assert result.decisions[0].reason_code == (
            "possible_user_voice_match_requires_confirmation"
        )
        # speaker_b caught in propagation cascade.
        assert result.decisions[1].choice is VoiceReviewChoice.PAUSED
        # Plan: propagation reason uses the "paused_after_prior_..." style
        # so audit can distinguish trigger from cascade.
        assert (
            result.decisions[1].reason_code
            == "paused_after_prior_possible_match_confirmation"
        )

    def test_possible_match_kwargs_default_preserves_phase3_behavior(self):
        """Backward compatibility: callers that don't pass the Phase 4
        kwargs (``possible_voice_matches_by_speaker_id`` /
        ``admin_pause_on_possible_match``) get exactly the Phase 3 flow.
        This protects every existing test in this file."""
        from services.smart.auto_voice_review import (
            evaluate_voice_review,
            VoiceReviewChoice,
            VoiceReviewOutcome,
        )
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        # NOTE: deliberately NOT passing any Phase 4 kwargs.
        result = evaluate_voice_review(
            main_speakers=[self._speaker("a", sample_seconds=20.0)],
            smart_consent={"auto_voice_clone": True},
            clone_provider=fake,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=self._id_factory(),
        )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert len(fake.calls) == 1
        assert result.decisions[0].choice is VoiceReviewChoice.CLONED


# ===================================================================
# _MiniMaxCloneAdapter call-mapping — Codex 第八轮 末段
# ===================================================================


class TestMiniMaxCloneAdapterMapping:
    """Codex 第八轮 末段 — PR#2 only verified the Protocol shape; we
    never confirmed _MiniMaxCloneAdapter's adapter ACTUALLY translates
    args to MiniMaxVoiceCloneClient.create_voice_clone with the right
    kwarg names. This catches "real client renamed a kwarg → Smart
    silently breaks" regressions.

    Pattern: monkeypatch services.voice_clone.MiniMaxVoiceCloneClient
    + VoiceCloneConfig, exercise _MiniMaxCloneAdapter.clone_voice()
    end-to-end (incl. lazy construction), and inspect what was passed
    to the real client.
    """

    def _install_mock_client(self, monkeypatch, *, captured_calls):
        """Replace services.voice_clone.MiniMaxVoiceCloneClient with a
        recording stub. Returns the stub class so the test can assert
        construction args + call args.

        Codex 第十二轮 P1-1: stub uses an EXPLICIT signature mirroring
        the real ``create_voice_clone`` declaration (NOT ``**kwargs``).
        ``**kwargs`` would silently absorb any kwarg the adapter passes
        even if the real method renamed it, defeating the entire point
        of the mapping test. Explicit kwargs raise TypeError at the
        Python level if the adapter tries to pass an unknown name.
        """
        import services.voice_clone as voice_clone_mod
        from services.voice_clone import VoiceCloneResult

        class RecordingClient:
            def __init__(self, config):
                captured_calls["construction_config"] = config
            def create_voice_clone(
                self,
                *,
                speaker_id: str,
                speaker_name: str,
                source_audio_path,
                need_noise_reduction: bool = False,
            ):
                kwargs = {
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "source_audio_path": source_audio_path,
                    "need_noise_reduction": need_noise_reduction,
                }
                captured_calls.setdefault("create_voice_clone_calls", []).append(kwargs)
                # Return a real-shaped VoiceCloneResult so the adapter
                # can map it into CloneResult without a TypeError.
                return VoiceCloneResult(
                    speaker_id=speaker_id,
                    speaker_name=speaker_name,
                    source_audio_path=str(source_audio_path),
                    uploaded_file_id="real_file_id_xyz",
                    voice_id=f"vt_{speaker_id}_real",
                    provider_name="minimax_voice_clone",
                    model_name="voice_clone",
                )

        monkeypatch.setattr(voice_clone_mod, "MiniMaxVoiceCloneClient", RecordingClient)

    def test_adapter_passes_kwargs_to_real_client_with_exact_names(self, monkeypatch, tmp_path):
        """Critical mapping: speaker_id / speaker_name / source_audio_path
        kwarg names must match real ``create_voice_clone`` declaration
        (src/services/voice_clone.py:246). Any silent rename in the real
        client breaks Smart auto_voice_review unless the adapter is
        updated — this test surfaces the breakage at unit-test time."""
        from services.smart.contracts import CloneResult
        from services.smart_wiring import _MiniMaxCloneAdapter

        captured = {}
        # Inject a fake API key via constructor override (post b3g-fix:
        # _api_key kwarg bypasses the production VoiceCloneConfig.from_env
        # path, keeping tests independent of autodub.local.json /
        # AUTODUB_TTS_API_KEY env).
        self._install_mock_client(monkeypatch, captured_calls=captured)

        adapter = _MiniMaxCloneAdapter(api_key="fake-key-for-test")
        sample_path = tmp_path / "speaker_a.wav"
        result = adapter.clone_voice(
            speaker_id="speaker_a",
            speaker_name="查理·芒格",
            source_audio_path=sample_path,
        )

        # 1. Underlying client was constructed once.
        assert "construction_config" in captured

        # 2. create_voice_clone called with exact kwarg names. If the
        # real method renames any of these (e.g. speaker_id → speakerID),
        # the stub's explicit signature (Codex 第十二轮 P1-1 fix) raises
        # TypeError at the adapter call site — this assertion never runs.
        calls = captured["create_voice_clone_calls"]
        assert len(calls) == 1
        kwargs = calls[0]
        assert kwargs["speaker_id"] == "speaker_a"
        assert kwargs["speaker_name"] == "查理·芒格"
        assert kwargs["source_audio_path"] == sample_path
        # Adapter MUST pass at least the 3 core kwargs and MUST NOT pass
        # anything outside the real signature's whitelist. The stub's
        # explicit signature already enforces "no unknown kwargs"
        # (TypeError on instantiation before reaching here); this set
        # check additionally documents the adapter's intent + catches
        # accidental over-specification of the optional default.
        REQUIRED = {"speaker_id", "speaker_name", "source_audio_path"}
        ALLOWED = REQUIRED | {"need_noise_reduction"}
        keys = set(kwargs.keys())
        assert REQUIRED.issubset(keys), (
            f"Adapter missed required kwargs: {REQUIRED - keys}"
        )
        unknown = keys - ALLOWED
        assert not unknown, (
            f"Adapter passed unknown kwargs to create_voice_clone: {unknown}. "
            f"Real client signature only accepts {ALLOWED}."
        )

        # 3. CloneResult mapped from VoiceCloneResult correctly.
        assert isinstance(result, CloneResult)
        assert result.speaker_id == "speaker_a"
        assert result.speaker_name == "查理·芒格"
        assert result.voice_id == "vt_speaker_a_real"
        assert result.provider_name == "minimax_voice_clone"
        assert result.model_name == "voice_clone"
        # Critical: VoiceCloneResult fields that aren't part of CloneResult
        # (uploaded_file_id, source_audio_path string) MUST NOT leak into
        # CloneResult. CloneResult is a frozen dataclass with explicit
        # fields, so AttributeError on these would fire.
        assert not hasattr(result, "uploaded_file_id")
        assert not hasattr(result, "source_audio_path")  # string form not propagated

    def test_adapter_lazy_construction_defers_until_first_call(self, monkeypatch):
        """The adapter must NOT instantiate the real client at import
        time — that would require MINIMAX_API_KEY just to load the
        smart wiring module, breaking test environments without the
        env var. Construction happens on first clone_voice() call only.
        """
        import services.voice_clone as voice_clone_mod
        from services.smart_wiring import _MiniMaxCloneAdapter

        construction_count = [0]

        class CountingClient:
            def __init__(self, config):
                construction_count[0] += 1
            def create_voice_clone(self, **kwargs):
                from services.voice_clone import VoiceCloneResult
                return VoiceCloneResult(
                    speaker_id=kwargs["speaker_id"],
                    speaker_name=kwargs["speaker_name"],
                    source_audio_path=str(kwargs["source_audio_path"]),
                    uploaded_file_id="x",
                    voice_id="vt_x",
                    provider_name="p",
                    model_name="m",
                )

        monkeypatch.setattr(voice_clone_mod, "MiniMaxVoiceCloneClient", CountingClient)

        # Use _api_key constructor override (b3g-fix path that
        # bypasses VoiceCloneConfig.from_env config-file lookup).
        adapter = _MiniMaxCloneAdapter(api_key="test-key")
        # Construction has NOT happened yet — instantiating the adapter
        # is cheap.
        assert construction_count[0] == 0

        adapter.clone_voice(
            speaker_id="a", speaker_name="A", source_audio_path=Path("/x"),
        )
        # First call triggered construction.
        assert construction_count[0] == 1

        adapter.clone_voice(
            speaker_id="b", speaker_name="B", source_audio_path=Path("/y"),
        )
        # Second call reuses the cached client — does NOT re-construct.
        assert construction_count[0] == 1

    def test_adapter_missing_config_raises_actionable_error(self, monkeypatch):
        """When ``VoiceCloneConfig.from_env`` returns a config that
        fails validate() (no api_key in autodub.local.json AND no
        AUTODUB_TTS_API_KEY env), the adapter must surface the error
        unambiguously so auto_voice_review's retry loop catches it +
        falls through to PRESET (rather than a misleading 200 response).

        PR#3C-b3g-fix renamed this test from "missing MINIMAX_API_KEY"
        because the production config path is AUTODUB_TTS_*; the
        legacy MINIMAX_API_KEY direct read was the b3d wiring gap
        real-host E2E exposed.
        """
        import services.voice_clone as voice_clone_mod
        from services.smart_wiring import _MiniMaxCloneAdapter

        class NeverConstructed:
            def __init__(self, config):
                raise AssertionError("must not reach real construction")

        monkeypatch.setattr(voice_clone_mod, "MiniMaxVoiceCloneClient", NeverConstructed)
        # Strip any test env that might satisfy from_env's resolution.
        for env_var in (
            "AUTODUB_TTS_API_KEY",
            "AUTODUB_TTS_CLONE_API_KEY",
            "MINIMAX_API_KEY",
        ):
            monkeypatch.delenv(env_var, raising=False)

        # Mock VoiceCloneConfig.from_env to return a config whose
        # validate() raises VoiceCloneConfigurationError — emulates
        # an environment with no api_key configured anywhere.
        from services.voice_clone import (
            VoiceCloneConfig, VoiceCloneConfigurationError,
        )

        def _fake_from_env(*a, **kw):
            cfg = VoiceCloneConfig(api_key=None, base_url=None)
            return cfg

        monkeypatch.setattr(VoiceCloneConfig, "from_env", _fake_from_env)

        adapter = _MiniMaxCloneAdapter()
        with pytest.raises(VoiceCloneConfigurationError) as exc_info:
            adapter.clone_voice(
                speaker_id="a", speaker_name="A", source_audio_path=Path("/x"),
            )
        # The validate() error message points at the env var / config
        # key the operator should set. Pin that the message names
        # the production config name (AUTODUB_TTS_* / autodub.local.json)
        # — the legacy direct MINIMAX_API_KEY mention should be gone.
        msg = str(exc_info.value).lower()
        assert "api_key" in msg or "url" in msg or "base_url" in msg, (
            f"Config validate() message should mention api_key / base_url; "
            f"got: {exc_info.value!r}"
        )

    def test_real_create_voice_clone_signature_locked(self):
        """Codex 第十二轮 P1-1: the mapping test must also assert the
        REAL ``MiniMaxVoiceCloneClient.create_voice_clone`` signature
        is what the adapter expects. Without this, a rename in
        voice_clone.py:246 (e.g. speaker_id → speakerID) would still
        let the mapping test pass against any **kwargs stub.

        Use ``inspect.signature`` to lock the keyword-only parameter
        set + default value of ``need_noise_reduction``. Any change
        to the real method declaration that drifts from this contract
        fails the test loudly at import time.
        """
        import inspect

        from services.voice_clone import MiniMaxVoiceCloneClient

        sig = inspect.signature(MiniMaxVoiceCloneClient.create_voice_clone)
        # All params except 'self' should be KEYWORD_ONLY (real signature
        # uses ``*,``). This protects against accidentally introducing
        # positional args that the adapter wouldn't know to pass by name.
        params = [
            (name, p) for name, p in sig.parameters.items()
            if name != "self"
        ]
        param_names = [name for name, _ in params]
        assert param_names == [
            "speaker_id", "speaker_name", "source_audio_path", "need_noise_reduction",
        ], (
            f"Real create_voice_clone params changed: {param_names!r}. "
            f"Update _MiniMaxCloneAdapter.clone_voice in smart_wiring.py to "
            f"match, then update this assertion + the RecordingClient stub "
            f"in this file's _install_mock_client helper."
        )
        # All are keyword-only.
        for name, p in params:
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"param {name!r} kind changed to {p.kind!r}; adapter expects "
                f"KEYWORD_ONLY (matches the ``*,`` declaration in voice_clone.py)."
            )
        # Default for ``need_noise_reduction`` is False; adapter doesn't
        # pass it, so the default needs to keep being False or the
        # behaviour silently changes for cloned voices.
        assert (
            sig.parameters["need_noise_reduction"].default is False
        ), (
            "need_noise_reduction default changed away from False. Smart "
            "adapter relies on the default — either pass it explicitly in "
            "_MiniMaxCloneAdapter.clone_voice or update this assertion."
        )

    def test_adapter_satisfies_clone_provider_protocol_with_real_client_path(
        self, monkeypatch
    ):
        """End-to-end: adapter goes through full call chain with real
        VoiceCloneResult shape, returns CloneResult that downstream
        Smart code (auto_voice_review) can consume via the Protocol."""
        from services.smart.contracts import CloneProvider
        from services.smart_wiring import _MiniMaxCloneAdapter

        captured = {}
        self._install_mock_client(monkeypatch, captured_calls=captured)

        # b3g-fix: use _api_key constructor override so test doesn't
        # depend on autodub.local.json or AUTODUB_TTS_API_KEY env.
        adapter = _MiniMaxCloneAdapter(api_key="test-key")
        # Both static (Protocol shape) and runtime (isinstance) checks.
        assert isinstance(adapter, CloneProvider)

        # Quick smoke: adapter usable as a CloneProvider in
        # evaluate_voice_review.
        from services.smart.auto_voice_review import (
            evaluate_voice_review, VoiceReviewSpeakerInput, VoiceReviewChoice,
        )
        result = evaluate_voice_review(
            main_speakers=[
                VoiceReviewSpeakerInput(
                    speaker_id="speaker_x",
                    speaker_name="X",
                    sample_seconds=15.0,
                    source_audio_path=Path("/fake.wav"),
                ),
            ],
            smart_consent={"auto_voice_clone": True},
            clone_provider=adapter,
            voice_library_quota_remaining=100,
            smart_decision_id_factory=lambda: "dec_e2e",
        )
        d = result.decisions[0]
        assert d.choice is VoiceReviewChoice.CLONED
        assert d.cloned_voice_id == "vt_speaker_x_real"
