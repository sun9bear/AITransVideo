"""MiniMax balance / quota exhaustion classifier (2026-05-20).

== Real incident ==

job_f2abf73878b343b6bbbde36ced9fa63c (Stanford communication video,
admin smart submission). MiniMax voice_clone API returned::

    base_resp_status_code=1008
    base_resp_status_msg=insufficient balance.

This is the provider's "account out of money" signal. Smart's
``_looks_like_quota_error`` heuristic only matched the literal
substring "quota", so the error wasn't recognized as exhaustion.
Result: smart retried 3x (wasting credits + time) then fell
through to preset voice — exactly the failure mode plan §7.3
warns about ("fall-through to preset is wrong for provider
exhaustion").

Per user 2026-05-20 spec ("smart 全自动化原则"), provider
balance/quota exhaustion IS one of the 7 acceptable handoff
sites — user must be informed to top up the account or wait,
NOT silently delivered with preset as if everything succeeded.

== This test ==

Pins the expanded heuristic so future regressions are caught:
all known provider-side billing/exhaustion error shapes route
to PAUSE on first attempt (no wasted retries).
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class TestLooksLikeQuotaError:
    """The heuristic must match every known provider-side
    exhaustion error shape so smart pauses on first attempt
    instead of wasting retries."""

    def test_legacy_quota_substring_still_matches(self):
        """Original spec — class name OR message containing 'quota'."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        # Class name match (test fakes use this shape)
        class FakeCloneQuotaError(Exception):
            pass
        assert _looks_like_quota_error(FakeCloneQuotaError("anything"))

        # Message substring match
        assert _looks_like_quota_error(Exception("quota_exceeded"))
        assert _looks_like_quota_error(Exception("Quota low for this account"))

    def test_minimax_insufficient_balance_matches(self):
        """Real production signal that triggered this fix.

        MiniMax voice_clone returns:
            base_resp_status_code=1008
            base_resp_status_msg=insufficient balance.

        and the orchestrator wraps it in a VoiceCloneAPIError with
        the full body in str(exc). The heuristic must recognise
        this exact shape on the FIRST attempt so no retries waste
        any further provider calls."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        # Exact substring as observed in production
        real_err = Exception(
            "voice_clone returned non-success base_resp. "
            "top_level_keys=['base_resp', 'demo_audio', 'input_sensitive', "
            "'input_sensitive_type']. base_resp_status_code=1008. "
            "base_resp_status_msg=insufficient balance."
        )
        assert _looks_like_quota_error(real_err), (
            "MiniMax 'insufficient balance' (status_code=1008) must "
            "be recognized as provider exhaustion so smart pauses on "
            "first attempt. Real incident: job_f2abf73878b... wasted "
            "3 retry attempts before this fix."
        )

    def test_minimax_status_code_1008_matches(self):
        """The numeric error code itself is also a stable anchor —
        even if MiniMax later changes the human message, the code
        stays."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        assert _looks_like_quota_error(
            Exception("provider rejected: status_code=1008")
        )

    def test_chinese_balance_signal_matches(self):
        """If MiniMax ever localises to Chinese error text."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        assert _looks_like_quota_error(Exception("voice_clone 失败：余额不足"))

    def test_other_billing_signals_match(self):
        """Generic provider billing exhaustion phrases — defensive
        in case a different provider lands later or MiniMax adds
        new error phrasings."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        for msg in (
            "Error: insufficient_balance",
            "402 Payment Required for this endpoint",
            "Account balance_exhausted",
            "Your balance is below the minimum",
        ):
            assert _looks_like_quota_error(Exception(msg)), (
                f"Expected billing-exhaustion signal {msg!r} to match. "
                f"Per plan §7.3, all provider exhaustion error shapes "
                f"should route to PAUSE, not retry."
            )

    def test_non_exhaustion_errors_do_not_match(self):
        """Conservative: false positives are SAFER than false negatives
        (per docstring), but we still shouldn't classify obviously-
        unrelated errors as exhaustion. Pin a few cases that MUST
        NOT match so the heuristic stays meaningful."""
        from services.smart.auto_voice_review import _looks_like_quota_error

        for msg in (
            "Connection timeout",
            "Audio sample too short",
            "Invalid voice_id format",
            "internal server error",
            "Network unreachable",
        ):
            assert not _looks_like_quota_error(Exception(msg)), (
                f"Expected {msg!r} to NOT be classified as exhaustion. "
                f"If this matches, smart would handoff on transient "
                f"errors that should just retry."
            )


class TestRetryShortCircuitOnBalance:
    """Behavior contract: when ``_looks_like_quota_error`` matches,
    ``_attempt_clone_with_retries`` returns PAUSED on first attempt,
    no retries.

    This is the key efficiency property: a balance-exhausted account
    must not waste 3 provider calls before pausing."""

    def test_balance_error_pauses_on_first_attempt(self):
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewSpeakerInput,
            _attempt_clone_with_retries,
        )

        attempts_made = []

        class FakeBalanceErrorProvider:
            def clone_voice(self, *, speaker_id, speaker_name, source_audio_path):
                attempts_made.append(1)
                raise Exception(
                    "voice_clone returned non-success base_resp. "
                    "base_resp_status_code=1008. "
                    "base_resp_status_msg=insufficient balance."
                )

        speaker = VoiceReviewSpeakerInput(
            speaker_id="speaker_a",
            speaker_name="Matt",
            sample_seconds=2400.0,
            source_audio_path="/tmp/fake.wav",
        )

        decision = _attempt_clone_with_retries(
            speaker=speaker,
            clone_provider=FakeBalanceErrorProvider(),
            max_attempts=3,
            smart_decision_id_factory=lambda: "deadbeef",
        )

        assert decision.choice == VoiceReviewChoice.PAUSED, (
            f"Balance-exhausted clone must return PAUSED on first "
            f"attempt, not fall-through to PRESET. Got "
            f"choice={decision.choice}. Real incident: smart wasted "
            f"3 attempts before this fix because the heuristic only "
            f"matched 'quota' substring."
        )
        assert decision.reason_code == "provider_quota_exhausted_mid_flight"
        assert len(attempts_made) == 1, (
            f"Expected 1 attempt (paused immediately); got "
            f"{len(attempts_made)} attempts. Retrying on balance "
            f"exhaustion is wasted work — MiniMax balance won't "
            f"refill within the retry budget."
        )

    def test_non_billing_error_still_retries(self):
        """Non-balance errors should still retry up to max_attempts
        (the conservative-false-positive principle: we widen the
        PAUSE classifier, but don't lose retry behavior for
        genuinely transient errors)."""
        from services.smart.auto_voice_review import (
            VoiceReviewChoice,
            VoiceReviewSpeakerInput,
            _attempt_clone_with_retries,
        )

        attempts_made = []

        class FakeTransientErrorProvider:
            def clone_voice(self, *, speaker_id, speaker_name, source_audio_path):
                attempts_made.append(1)
                raise Exception("Connection timeout")

        speaker = VoiceReviewSpeakerInput(
            speaker_id="speaker_a",
            speaker_name="Matt",
            sample_seconds=2400.0,
            source_audio_path="/tmp/fake.wav",
        )

        decision = _attempt_clone_with_retries(
            speaker=speaker,
            clone_provider=FakeTransientErrorProvider(),
            max_attempts=3,
            smart_decision_id_factory=lambda: "cafef00d",
        )

        # Non-balance error → exhausted retries → fall through to PRESET
        assert decision.choice == VoiceReviewChoice.PRESET
        assert len(attempts_made) == 3, (
            "Generic transient error should still use all 3 retry "
            "attempts before falling through to preset. The expanded "
            "heuristic must not over-classify transient errors as "
            "exhaustion."
        )
