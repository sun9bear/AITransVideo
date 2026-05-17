"""Phase 1 — unified candidate API smoke tests.

Plan: docs/plans/2026-05-17-user-voice-candidate-first-plan.md §Phase 1

These tests pin:
- The internal candidate endpoint registers on ``internal_router``
  (so the Caddyfile @internal_block can shield it).
- The internal candidate endpoint returns ``auto_reuse_voice`` only for
  strong matches and ``personal_voice_candidates`` for any match.
- ``official_voice_candidates`` is always returned (empty in Phase 1).
- ``include_cross_source=True`` flag is honoured.

Endpoint logic is exercised by calling the handler function directly
with mocked dependencies — same pattern as
``test_smart_user_voice_quota_endpoint.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


class TestCandidatesEndpointRegistration:
    """The new candidate endpoints must be wired alongside their
    backward-compat siblings — internal under ``/api/internal`` so
    Caddy's @internal_block shields it, and a public ``/job-api`` route
    for the Studio frontend."""

    def test_internal_endpoint_registered_on_internal_router(self):
        import user_voice_api

        routes = [
            (r.path, r.methods)
            for r in user_voice_api.internal_router.routes
        ]
        candidate_routes = [
            (path, methods)
            for path, methods in routes
            if path.endswith("/user-voices/candidates")
        ]
        assert candidate_routes, (
            "POST /api/internal/user-voices/candidates not registered "
            "on internal_router. Phase 1 candidate endpoint must stay "
            "internal-only.\n"
            f"Available internal routes: {routes}"
        )
        path, methods = candidate_routes[0]
        assert path == "/api/internal/user-voices/candidates", path
        assert "POST" in methods, f"Expected POST method, got {methods}"


class TestInternalCandidatesEndpoint:

    @pytest.mark.asyncio
    async def test_returns_auto_reuse_when_strong_match_present(self, monkeypatch):
        import user_voice_api
        from user_voice_service import UserVoiceMatch

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            user_voice_api, "_voice_to_dict",
            lambda v: {
                "voice_id": v.voice_id,
                "label": v.label,
                "source_video_title": getattr(v, "source_video_title", None),
                "source_speaker_name": getattr(v, "source_speaker_name", None),
                "clone_sample_seconds": getattr(v, "clone_sample_seconds", None),
                "created_at": None,
            },
        )

        recorded = {}

        async def _fake_match(db, **kwargs):
            recorded.update(kwargs)
            return [
                UserVoiceMatch(
                    voice=SimpleNamespace(
                        voice_id="vt_strong",
                        label="Alice · 2026-05-16",
                        source_video_title="Charlie Talks",
                        source_speaker_name="Alice",
                        clone_sample_seconds=22.4,
                    ),
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    score=100,
                    match_scope="same_source_strong",
                )
            ]

        monkeypatch.setattr(user_voice_api, "match_user_voices", _fake_match)

        valid_uuid = "00000000-0000-0000-0000-000000000001"
        body = {
            "user_id": valid_uuid,
            "source_content_hash": "youtube:abc",
            "speaker_id": "speaker_a",
            "speaker_name": "Alice",
            "provider": "minimax_voice_clone",
            "tts_provider": "minimax_tts",
            "platform": "minimax_domestic",
        }
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=json.dumps(body).encode())

        resp = await user_voice_api.internal_user_voice_candidates(
            request=fake_req,
            db=MagicMock(),
        )
        out = json.loads(resp.body.decode("utf-8"))

        assert out["speaker_id"] == "speaker_a"
        assert out["source_content_hash"] == "youtube:abc"
        assert out["auto_reuse_voice"] is not None
        assert out["auto_reuse_voice"]["voice_id"] == "vt_strong"
        assert out["auto_reuse_voice"]["match_scope"] == "same_source_strong"
        assert out["auto_reuse_voice"]["auto_reuse_allowed"] is True
        assert len(out["personal_voice_candidates"]) == 1
        cand = out["personal_voice_candidates"][0]
        assert cand["match_scope"] == "same_source_strong"
        assert cand["requires_user_confirmation"] is False
        assert cand["confidence"] == "strong"
        assert cand["score"] == 100
        assert cand["evidence"]["source_video_title"] == "Charlie Talks"
        assert cand["evidence"]["source_speaker_name"] == "Alice"
        assert cand["evidence"]["clone_sample_seconds"] == 22.4
        # Phase 1: official candidates always empty.
        assert out["official_voice_candidates"] == []
        # Default include_cross_source=True for the new endpoint.
        assert recorded.get("include_cross_source") is True

    @pytest.mark.asyncio
    async def test_returns_only_candidates_for_weak_match(self, monkeypatch):
        """When the top match is not strong, auto_reuse_voice is null
        but personal_voice_candidates still has the row, flagged for
        user confirmation."""
        import user_voice_api
        from user_voice_service import UserVoiceMatch

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            user_voice_api, "_voice_to_dict",
            lambda v: {"voice_id": v.voice_id, "label": v.label, "created_at": None},
        )

        async def _fake_match(db, **kwargs):
            return [
                UserVoiceMatch(
                    voice=SimpleNamespace(voice_id="vt_weak", label="cross-Alice"),
                    confidence="weak",
                    reason="cross_source_same_speaker_name_key",
                    score=20,
                    match_scope="cross_source_named_person",
                )
            ]

        monkeypatch.setattr(user_voice_api, "match_user_voices", _fake_match)

        valid_uuid = "00000000-0000-0000-0000-000000000001"
        body = {
            "user_id": valid_uuid,
            "source_content_hash": "youtube:abc",
            "speaker_id": "speaker_a",
            "speaker_name": "Alice",
            "include_cross_source": True,
        }
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=json.dumps(body).encode())

        resp = await user_voice_api.internal_user_voice_candidates(
            request=fake_req,
            db=MagicMock(),
        )
        out = json.loads(resp.body.decode("utf-8"))

        assert out["auto_reuse_voice"] is None
        assert len(out["personal_voice_candidates"]) == 1
        cand = out["personal_voice_candidates"][0]
        assert cand["match_scope"] == "cross_source_named_person"
        assert cand["confidence"] == "weak"
        assert cand["requires_user_confirmation"] is True
        assert out["official_voice_candidates"] == []

    @pytest.mark.asyncio
    async def test_returns_empty_envelope_when_no_match(self, monkeypatch):
        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        matcher = AsyncMock(return_value=[])
        monkeypatch.setattr(user_voice_api, "match_user_voices", matcher)

        valid_uuid = "00000000-0000-0000-0000-000000000001"
        body = {
            "user_id": valid_uuid,
            "source_content_hash": "youtube:abc",
            "speaker_id": "speaker_a",
        }
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=json.dumps(body).encode())

        resp = await user_voice_api.internal_user_voice_candidates(
            request=fake_req,
            db=MagicMock(),
        )
        out = json.loads(resp.body.decode("utf-8"))

        assert out["auto_reuse_voice"] is None
        assert out["personal_voice_candidates"] == []
        assert out["official_voice_candidates"] == []

    @pytest.mark.asyncio
    async def test_rejects_missing_user_id(self, monkeypatch):
        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=b'{"speaker_id": "speaker_a"}')

        resp = await user_voice_api.internal_user_voice_candidates(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_internal_auth_required(self, monkeypatch):
        """Without a valid X-Internal-Key, the endpoint must refuse."""
        import user_voice_api
        from fastapi import Response

        # Simulate auth error short-circuit.
        deny = Response(content="forbidden", status_code=403)
        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: deny,
        )
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=b'{}')

        resp = await user_voice_api.internal_user_voice_candidates(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 403
