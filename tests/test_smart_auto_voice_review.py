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
        didn't agree to."""
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
        # All speakers fall to preset with consent_denied reason.
        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert all(d.choice is VoiceReviewChoice.PRESET for d in result.decisions)
        assert all(d.reason_code == "consent_denied" for d in result.decisions)

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
        construction args + call args."""
        import services.voice_clone as voice_clone_mod
        from services.voice_clone import VoiceCloneResult

        class RecordingClient:
            def __init__(self, config):
                captured_calls["construction_config"] = config
            def create_voice_clone(self, **kwargs):
                captured_calls.setdefault("create_voice_clone_calls", []).append(kwargs)
                # Return a real-shaped VoiceCloneResult so the adapter
                # can map it into CloneResult without a TypeError.
                return VoiceCloneResult(
                    speaker_id=kwargs["speaker_id"],
                    speaker_name=kwargs["speaker_name"],
                    source_audio_path=str(kwargs["source_audio_path"]),
                    uploaded_file_id="real_file_id_xyz",
                    voice_id=f"vt_{kwargs['speaker_id']}_real",
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
        # Inject a fake API key so lazy construction doesn't refuse.
        monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-test")
        self._install_mock_client(monkeypatch, captured_calls=captured)

        adapter = _MiniMaxCloneAdapter()
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
        # this assertion fails loudly.
        calls = captured["create_voice_clone_calls"]
        assert len(calls) == 1
        kwargs = calls[0]
        assert kwargs["speaker_id"] == "speaker_a"
        assert kwargs["speaker_name"] == "查理·芒格"
        assert kwargs["source_audio_path"] == sample_path
        # Adapter MUST NOT pass any unexpected kwargs that would
        # TypeError against the real signature.
        assert set(kwargs.keys()) == {
            "speaker_id", "speaker_name", "source_audio_path",
        }, (
            f"Adapter passed unexpected kwargs to create_voice_clone: "
            f"{set(kwargs.keys())}. Real client signature only accepts "
            f"speaker_id / speaker_name / source_audio_path / need_noise_reduction."
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
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

        adapter = _MiniMaxCloneAdapter()
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

    def test_adapter_missing_api_key_raises_actionable_error(self, monkeypatch):
        """Without MINIMAX_API_KEY, construction must fail with a clear
        message that points to inject_for_test() as the test path."""
        import services.voice_clone as voice_clone_mod
        from services.smart_wiring import _MiniMaxCloneAdapter

        class NeverConstructed:
            def __init__(self, config):
                raise AssertionError("must not reach real construction")

        monkeypatch.setattr(voice_clone_mod, "MiniMaxVoiceCloneClient", NeverConstructed)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

        adapter = _MiniMaxCloneAdapter()
        with pytest.raises(RuntimeError) as exc_info:
            adapter.clone_voice(
                speaker_id="a", speaker_name="A", source_audio_path=Path("/x"),
            )
        msg = str(exc_info.value)
        assert "MINIMAX_API_KEY" in msg
        assert "inject_for_test" in msg, (
            "Error message should point devs to the inject_for_test() escape "
            "hatch so they know how to run tests without the real env var."
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
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        self._install_mock_client(monkeypatch, captured_calls=captured)

        adapter = _MiniMaxCloneAdapter()
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
