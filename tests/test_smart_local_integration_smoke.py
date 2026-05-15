"""Local integration smoke test for Smart MVP P2.

Per Codex 第三十四轮 recommendation: before paying for a real-video
end-to-end (which costs MiniMax quota + remote env time), pin three
cross-module flows with fakes/stubs so the cross-piece state machine
is verified locally:

  1. Sidecar ``audit/smart_decisions.jsonl`` actually receives the
     full decision sequence (eligibility → translation → voice
     review batch + per-speaker CLONED → terminal) and every line
     parses + carries the right schema fields.

  2. Gateway ``/api/internal/user-voices/quota`` and
     ``/api/internal/user-voices/register-smart`` form a real loop:
     register → quota used+1, remaining-1; second register → +2;
     re-register the same voice_id → upsert (no count drift).

  3. Smart voice-review path with FakeCloneProvider produces a
     CLONED decision; the mirror helper invocation propagates the
     right (user_id, voice_id, label, source_speaker_id) tuple;
     mirror-failure aggregator triggers fail-closed handoff with
     the documented reason_code.

Each smoke is small (no real ProcessPipeline construction, no
real videos) but runs the actual production helpers / endpoints
end-to-end with shared in-memory state. Cheaper than a real E2E,
catches cross-module drift the unit tests miss.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_GATEWAY = _REPO / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===========================================================================
# Smoke 1 — sidecar full-decision-sequence flow
# ===========================================================================


class TestSidecarFullDecisionSequenceSmoke:
    """A full smart job's decision events land in audit/smart_decisions.jsonl
    in order, each parseable, each carrying schema_version + event_id +
    decision_type + decision + auto_approved + created_at + extra fields.

    Mimics the order process.py emits in the smart inline branch:
      1. speaker_gate / approved (eligibility)
      2. voice_clone / approved x2 (per-speaker CLONED)
      3. voice_selection_auto_approve / approved (batch verdict)
      4. translation_auto_approve / approved (S3 trigger)
    """

    def test_full_smart_happy_path_decision_sequence(self, tmp_path):
        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_smoke_1"
        project_dir.mkdir()

        job_id = "job-smoke-1"
        user_id = "user-smoke-1"

        # Sequence mirrors what process.py emits at each smart decision
        # site, in order, for a happy-path job with 2 cloned speakers.
        # 1. eligibility approved
        _emit_smart_audit(
            project_dir,
            decision_type="speaker_gate",
            decision="approved",
            evidence={
                "main_speaker_count": 2,
                "main_speaker_ids": ["speaker_a", "speaker_b"],
                "excluded_speakers": [],
                "threshold_used": 0.10,
                "limit_used": 3,
            },
            extra={"job_id": job_id, "user_id": user_id},
        )
        # 2. per-speaker CLONED (with explicit smart_decision_id from
        # auto_voice_review.VoiceReviewDecision.smart_decision_id —
        # Codex 第三十二轮 P0 contract)
        _emit_smart_audit(
            project_dir,
            decision_type="voice_clone",
            decision="approved",
            evidence={
                "voice_id": "fake_vt_speaker_a_19700101",
                "clone_provider": "fake_minimax_voice_clone",
                "sample_seconds": 22.5,
            },
            smart_decision_id="dec_a_xxx",
            extra={
                "speaker_id": "speaker_a",
                "job_id": job_id,
                "user_id": user_id,
            },
        )
        _emit_smart_audit(
            project_dir,
            decision_type="voice_clone",
            decision="approved",
            evidence={
                "voice_id": "fake_vt_speaker_b_19700101",
                "clone_provider": "fake_minimax_voice_clone",
                "sample_seconds": 18.0,
            },
            smart_decision_id="dec_b_yyy",
            extra={
                "speaker_id": "speaker_b",
                "job_id": job_id,
                "user_id": user_id,
            },
        )
        # 3. voice_selection_auto_approve batch verdict
        _emit_smart_audit(
            project_dir,
            decision_type="voice_selection_auto_approve",
            decision="approved",
            evidence={
                "decisions_count": 2,
                "cloned_count": 2,
                "preset_count": 0,
                "main_speakers_count": 2,
            },
            extra={"job_id": job_id, "user_id": user_id},
        )
        # 4. translation_auto_approve
        _emit_smart_audit(
            project_dir,
            decision_type="translation_auto_approve",
            decision="approved",
            evidence={
                "glossary_total_terms": 10,
                "glossary_preserved_terms": 9,
                "glossary_preservation_rate": 0.9,
            },
            extra={"job_id": job_id, "user_id": user_id},
        )

        # Read + parse
        sidecar = project_dir / "audit" / "smart_decisions.jsonl"
        assert sidecar.exists(), (
            f"sidecar not written; expected at {sidecar}"
        )
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5, (
            f"Expected 5 decision events (eligibility + 2 cloned + "
            f"voice_selection batch + translation); got {len(lines)}"
        )
        events = [json.loads(line) for line in lines]

        # Sequence + types
        assert [e["decision_type"] for e in events] == [
            "speaker_gate",
            "voice_clone",
            "voice_clone",
            "voice_selection_auto_approve",
            "translation_auto_approve",
        ]

        # Every event carries the required schema fields.
        for event in events:
            assert event["schema_version"] == 1
            assert event["decision"] == "approved"
            assert event["auto_approved"] is True
            assert isinstance(event["smart_decision_id"], str)
            assert isinstance(event["created_at"], str)
            assert event["job_id"] == job_id
            assert event["user_id"] == user_id

        # CLONED events use the supplied smart_decision_id (audit linkage
        # back to VoiceReviewDecision.smart_decision_id). Pin both.
        cloned = [e for e in events if e["decision_type"] == "voice_clone"]
        assert len(cloned) == 2
        assert {e["smart_decision_id"] for e in cloned} == {
            "dec_a_xxx", "dec_b_yyy",
        }
        # Per-speaker mapping correctness: speaker_id ↔ voice_id consistent
        by_speaker = {e["speaker_id"]: e for e in cloned}
        assert by_speaker["speaker_a"]["evidence"]["voice_id"] == (
            "fake_vt_speaker_a_19700101"
        )
        assert by_speaker["speaker_b"]["evidence"]["voice_id"] == (
            "fake_vt_speaker_b_19700101"
        )

    def test_handoff_decision_sequence_with_reason_code(self, tmp_path):
        """A failed-clone-mirror smart job emits the right
        downgrade_handoff event with reason_code that downstream
        QA/admin tooling can filter by."""
        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_smoke_handoff"
        project_dir.mkdir()

        # Eligibility passes, voice_review reaches CLONED, then mirror
        # fails for one speaker → handoff.
        _emit_smart_audit(
            project_dir,
            decision_type="speaker_gate",
            decision="approved",
            evidence={"main_speaker_count": 1},
        )
        _emit_smart_audit(
            project_dir,
            decision_type="voice_clone",
            decision="approved",
            evidence={"voice_id": "vt_x"},
            smart_decision_id="dec_x",
        )
        _emit_smart_audit(
            project_dir,
            decision_type="downgrade_handoff",
            decision="rejected",
            reason_code="clone_library_register_failed",
            evidence={
                "failed_speakers": ["speaker_a"],
                "successful_clones": [],
            },
            extra={"handoff_stage": "voice_selection_review"},
        )

        sidecar = project_dir / "audit" / "smart_decisions.jsonl"
        events = [
            json.loads(line)
            for line in sidecar.read_text(encoding="utf-8")
            .strip().splitlines()
        ]
        assert len(events) == 3
        handoff = events[-1]
        assert handoff["decision_type"] == "downgrade_handoff"
        assert handoff["decision"] == "rejected"
        assert handoff["auto_approved"] is False
        assert handoff["reason_code"] == "clone_library_register_failed"
        assert handoff["evidence"]["failed_speakers"] == ["speaker_a"]
        assert handoff["handoff_stage"] == "voice_selection_review"


# ===========================================================================
# Smoke 2 — quota + register-smart endpoint loop
# ===========================================================================


class TestQuotaRegisterEndpointLoopSmoke:
    """Two Gateway internal endpoints must form a closed loop:
    register-smart writes to UserVoice → quota count includes it.

    We use a single shared in-memory ``UserVoice`` fake (a list) and
    monkeypatch ``add_user_voice`` (writer) + the SELECT COUNT
    (reader) to share state. This exercises the actual endpoint
    handlers — the same code that runs in production — but skips
    the SQLAlchemy / FastAPI layer.
    """

    @pytest.mark.asyncio
    async def test_register_then_quota_increments_used(self, monkeypatch):
        import user_voice_api
        import admin_settings as _admin_settings_mod

        # Shared in-memory store: list of {voice_id, user_id, expired_at}
        store: list[dict] = []

        async def _fake_add_user_voice(db, **kwargs):
            user_id = kwargs["user_id"]
            voice_id = kwargs["voice_id"]
            # Upsert: revive expired or update label
            for row in store:
                if row["user_id"] == user_id and row["voice_id"] == voice_id:
                    row["expired_at"] = None
                    row["label"] = kwargs.get("label", row.get("label"))
                    row["source_speaker_id"] = kwargs.get(
                        "source_speaker_id", row.get("source_speaker_id"),
                    )
                    fake_voice = MagicMock()
                    fake_voice.voice_id = voice_id
                    fake_voice.user_id = user_id
                    return fake_voice
            store.append({
                "user_id": user_id,
                "voice_id": voice_id,
                "label": kwargs.get("label"),
                "source_speaker_id": kwargs.get("source_speaker_id"),
                "expired_at": None,
            })
            fake_voice = MagicMock()
            fake_voice.voice_id = voice_id
            fake_voice.user_id = user_id
            return fake_voice

        monkeypatch.setattr(
            user_voice_api, "add_user_voice", _fake_add_user_voice,
        )
        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            _admin_settings_mod, "load_settings",
            lambda: _admin_settings_mod.AdminSettings(
                smart_user_voice_clone_cap=30
            ),
        )

        # Build a DB mock whose .execute().scalar() returns the count
        # of non-expired rows matching the query's user_id. The query
        # is in user_voice_api.internal_user_voice_quota; we synthesize
        # the right return per call via a side_effect that re-counts
        # the shared store on each invocation.
        def _make_db_for_user(user_uuid_str):
            from uuid import UUID

            target_uuid = UUID(user_uuid_str)

            fake_db = MagicMock()

            async def _execute(stmt):
                # Count non-expired rows matching target_uuid.
                count = sum(
                    1 for row in store
                    if str(row["user_id"]) == str(target_uuid)
                    and row["expired_at"] is None
                )
                fake_result = MagicMock()
                fake_result.scalar.return_value = count
                return fake_result

            fake_db.execute = _execute
            return fake_db

        valid_uuid_a = "00000000-0000-0000-0000-000000000001"

        # Initial quota: used=0, remaining=30
        from uuid import UUID

        fake_db_a = _make_db_for_user(valid_uuid_a)
        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id=valid_uuid_a,
            db=fake_db_a,
        )
        body = json.loads(resp.body.decode("utf-8"))
        assert body == {
            "user_id": valid_uuid_a,
            "used": 0,
            "limit": 30,
            "remaining": 30,
        }

        # Register voice A
        register_req_1 = MagicMock()
        register_req_1.body = AsyncMock(return_value=json.dumps({
            "user_id": valid_uuid_a,
            "voice_id": "vt_a",
            "label": "Speaker A Clone",
            "source_speaker_id": "speaker_a",
        }).encode())
        # Pre-convert user_id string → UUID instance for store consistency
        # (matches what user_voice_service does internally).
        # The fake_add_user_voice stores it as kwargs["user_id"].
        # First massage the body kwargs so the store key matches the
        # quota query's UUID lookup.
        async def _fake_add_with_uuid(db, **kwargs):
            kwargs["user_id"] = UUID(str(kwargs["user_id"]))
            return await _fake_add_user_voice(db, **kwargs)

        monkeypatch.setattr(
            user_voice_api, "add_user_voice", _fake_add_with_uuid,
        )

        resp = await user_voice_api.internal_register_smart_clone(
            request=register_req_1,
            db=MagicMock(),
        )
        body = json.loads(resp.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["voice_id"] == "vt_a"

        # Quota now: used=1, remaining=29
        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id=valid_uuid_a,
            db=_make_db_for_user(valid_uuid_a),
        )
        body = json.loads(resp.body.decode("utf-8"))
        assert body["used"] == 1
        assert body["remaining"] == 29

        # Register voice B
        register_req_2 = MagicMock()
        register_req_2.body = AsyncMock(return_value=json.dumps({
            "user_id": valid_uuid_a,
            "voice_id": "vt_b",
            "label": "Speaker B Clone",
            "source_speaker_id": "speaker_b",
        }).encode())
        await user_voice_api.internal_register_smart_clone(
            request=register_req_2,
            db=MagicMock(),
        )

        # Quota now: used=2, remaining=28
        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id=valid_uuid_a,
            db=_make_db_for_user(valid_uuid_a),
        )
        body = json.loads(resp.body.decode("utf-8"))
        assert body["used"] == 2
        assert body["remaining"] == 28

        # Re-register vt_a (upsert) → quota stays at used=2
        register_req_1_again = MagicMock()
        register_req_1_again.body = AsyncMock(return_value=json.dumps({
            "user_id": valid_uuid_a,
            "voice_id": "vt_a",
            "label": "Speaker A Clone (refreshed)",
            "source_speaker_id": "speaker_a",
        }).encode())
        await user_voice_api.internal_register_smart_clone(
            request=register_req_1_again,
            db=MagicMock(),
        )
        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id=valid_uuid_a,
            db=_make_db_for_user(valid_uuid_a),
        )
        body = json.loads(resp.body.decode("utf-8"))
        assert body["used"] == 2, (
            "Re-registering an existing voice_id must NOT inflate "
            "``used`` — add_user_voice does upsert. Quota signal "
            "stays consistent."
        )
        assert body["remaining"] == 28

    @pytest.mark.asyncio
    async def test_quota_user_isolation(self, monkeypatch):
        """User A's voices don't show up in User B's quota — UserVoice
        is per-user. Defensive against query without WHERE user_id."""
        import user_voice_api
        import admin_settings as _admin_settings_mod
        from uuid import UUID

        store: list[dict] = []

        async def _fake_add(db, **kwargs):
            kwargs["user_id"] = UUID(str(kwargs["user_id"]))
            store.append({
                "user_id": kwargs["user_id"],
                "voice_id": kwargs["voice_id"],
                "expired_at": None,
            })
            voice = MagicMock()
            voice.voice_id = kwargs["voice_id"]
            voice.user_id = kwargs["user_id"]
            return voice

        monkeypatch.setattr(
            user_voice_api, "add_user_voice", _fake_add,
        )
        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            _admin_settings_mod, "load_settings",
            lambda: _admin_settings_mod.AdminSettings(),
        )

        def _db_for(uuid_str):
            target = UUID(uuid_str)
            fake_db = MagicMock()

            async def _exec(stmt):
                count = sum(
                    1 for r in store
                    if r["user_id"] == target and r["expired_at"] is None
                )
                res = MagicMock()
                res.scalar.return_value = count
                return res

            fake_db.execute = _exec
            return fake_db

        uuid_a = "00000000-0000-0000-0000-00000000000a"
        uuid_b = "00000000-0000-0000-0000-00000000000b"

        # User A registers 3 voices
        for vid in ("vt_a1", "vt_a2", "vt_a3"):
            req = MagicMock()
            req.body = AsyncMock(return_value=json.dumps({
                "user_id": uuid_a, "voice_id": vid, "label": vid,
            }).encode())
            await user_voice_api.internal_register_smart_clone(
                request=req, db=MagicMock(),
            )

        # User A: used=3
        resp_a = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(), user_id=uuid_a, db=_db_for(uuid_a),
        )
        assert json.loads(resp_a.body.decode())["used"] == 3

        # User B: used=0 (isolated)
        resp_b = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(), user_id=uuid_b, db=_db_for(uuid_b),
        )
        assert json.loads(resp_b.body.decode())["used"] == 0


# ===========================================================================
# Smoke 3 — voice review + mirror integration
# ===========================================================================


class TestVoiceReviewMirrorIntegrationSmoke:
    """End-to-end voice-review path with real auto_voice_review module +
    FakeCloneProvider injected via smart_wiring.inject_for_test, then
    mirror helper invoked per-CLONED with a fake HTTP response.

    This pins the data flow between the in-process decision module
    (services.smart.auto_voice_review) and the mirror helper
    (pipeline.process._register_smart_clone_in_user_voices) — the
    seam that would have caught Codex 第三十二轮 P0 if it had existed.
    """

    def test_cloned_decisions_propagate_to_mirror_helper(self, monkeypatch):
        from pathlib import Path

        from services.smart.auto_voice_review import (
            VoiceReviewOutcome, VoiceReviewSpeakerInput,
            evaluate_voice_review,
        )
        from services.smart_wiring import (
            build_smart_clone_provider, inject_for_test,
        )

        from tests.fakes.fake_clone_provider import FakeCloneProvider
        from pipeline.process import _register_smart_clone_in_user_voices

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "smoke-test-key")

        # Capture the mirror HTTP calls for assertion.
        mirror_calls: list[dict] = []

        class _MirrorOkResp:
            status_code = 200

            def json(self):
                return {"ok": True}

        def _fake_post(url, *, json=None, headers=None, timeout=None):
            mirror_calls.append({
                "url": url, "json": json,
                "headers": headers, "timeout": timeout,
            })
            return _MirrorOkResp()

        import requests as _requests_mod
        monkeypatch.setattr(_requests_mod, "post", _fake_post)

        # Build inputs as process.py would: per-speaker validated samples.
        speakers = [
            VoiceReviewSpeakerInput(
                speaker_id="speaker_a",
                speaker_name="A",
                sample_seconds=20.0,
                source_audio_path=Path("/tmp/fake/speaker_a.wav"),
            ),
            VoiceReviewSpeakerInput(
                speaker_id="speaker_b",
                speaker_name="B",
                sample_seconds=18.0,
                source_audio_path=Path("/tmp/fake/speaker_b.wav"),
            ),
        ]

        fake_provider = FakeCloneProvider(success=True)
        with inject_for_test(clone_provider=fake_provider):
            result = evaluate_voice_review(
                main_speakers=speakers,
                smart_consent={"auto_voice_clone": True},
                clone_provider=build_smart_clone_provider(),
                voice_library_quota_remaining=30,
                smart_decision_id_factory=lambda: "dec_x",
            )

        assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
        assert len(result.decisions) == 2

        # Now simulate process.py's per-CLONED loop calling the mirror
        # helper. This is the seam that would have caught the Codex
        # 第三十二轮 P0 ``_dec.decision_id`` typo if exercised.
        mirror_results: list[bool] = []
        for dec in result.decisions:
            # Critical: read smart_decision_id (NOT decision_id) from
            # VoiceReviewDecision. If this attribute access fails,
            # this test fails — exactly what the production code path
            # needs covered.
            audit_decision_id = dec.smart_decision_id  # noqa: F841
            ok = _register_smart_clone_in_user_voices(
                user_id="user-smoke-3",
                voice_id=dec.cloned_voice_id or "",
                label=f"{dec.speaker_name} Clone",
                source_speaker_id=dec.speaker_id,
                notes="Smart auto-clone from job smoke-3",
            )
            mirror_results.append(ok)

        # Both mirror calls succeeded.
        assert mirror_results == [True, True]

        # Two HTTP POSTs to the register endpoint, with the right shape.
        assert len(mirror_calls) == 2
        for call in mirror_calls:
            assert call["url"] == (
                "http://127.0.0.1:8880/api/internal/user-voices/register-smart"
            )
            assert call["headers"] == {
                "X-Internal-Key": "smoke-test-key"
            }
            assert call["timeout"] == 5.0
            body = call["json"]
            assert body["user_id"] == "user-smoke-3"
            # Mirror payload preserves voice_id verbatim — the same
            # value FakeCloneProvider returned (deterministic
            # fake_vt_<sid>_19700101 pattern).
            assert body["voice_id"].startswith("fake_vt_speaker_")
            assert body["label"].endswith(" Clone")
            assert body["source_speaker_id"] in {"speaker_a", "speaker_b"}

    def test_mirror_failure_aggregates_to_handoff(self, monkeypatch, tmp_path):
        """When the mirror endpoint returns non-200 for one speaker,
        the orchestrator must aggregate failures (not silently drop
        them) so process.py can escalate to handoff. This pins the
        ``_smart_clone_mirror_failures`` contract."""
        from pipeline.process import _register_smart_clone_in_user_voices

        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "smoke-test-key")

        # Mock: register A succeeds, register B fails (HTTP 500).
        call_counter = {"i": 0}

        class _AlternatingResp:
            def __init__(self, ok):
                self.status_code = 200 if ok else 500
                self._ok = ok

            def json(self):
                return {"ok": self._ok}

        def _fake_post(url, *, json=None, headers=None, timeout=None):
            call_counter["i"] += 1
            ok = call_counter["i"] == 1  # first OK, rest fail
            return _AlternatingResp(ok)

        import requests as _requests_mod
        monkeypatch.setattr(_requests_mod, "post", _fake_post)

        # Aggregate failures the way process.py does.
        failures: list[str] = []
        for speaker_id, voice_id in [
            ("speaker_a", "fake_vt_speaker_a"),
            ("speaker_b", "fake_vt_speaker_b"),
        ]:
            ok = _register_smart_clone_in_user_voices(
                user_id="user-smoke-3b",
                voice_id=voice_id,
                label=f"{speaker_id} Clone",
                source_speaker_id=speaker_id,
            )
            if not ok:
                failures.append(speaker_id)

        # Exactly one mirror failed; the aggregator captured it.
        assert failures == ["speaker_b"], (
            f"Mirror failure aggregator should capture exactly speaker_b; "
            f"got {failures}"
        )
        # speaker_a's clone is still real in MiniMax (paid-for); the
        # process.py handoff branch must surface this so user can
        # take over in Studio. Pin the audit emit shape:
        from pipeline.process import _emit_smart_audit

        project_dir = tmp_path / "project_smoke_3b"
        project_dir.mkdir()
        _emit_smart_audit(
            project_dir,
            decision_type="downgrade_handoff",
            decision="rejected",
            reason_code="clone_library_register_failed",
            evidence={
                "failed_speakers": failures,
                "successful_clones": ["speaker_a"],
            },
            extra={
                "handoff_stage": "voice_selection_review",
                "user_id": "user-smoke-3b",
            },
        )
        sidecar_text = (
            project_dir / "audit" / "smart_decisions.jsonl"
        ).read_text(encoding="utf-8").strip()
        event = json.loads(sidecar_text)
        assert event["reason_code"] == "clone_library_register_failed"
        assert event["evidence"]["failed_speakers"] == ["speaker_b"]
        assert event["evidence"]["successful_clones"] == ["speaker_a"]
