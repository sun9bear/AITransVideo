"""Phase 1 — unified candidate API smoke tests.

Plan: docs/plans/2026-05-17-user-voice-candidate-first-plan.md §Phase 1

These tests pin:
- The internal candidate endpoint registers on ``internal_router``
  (so the Caddyfile @internal_block can shield it).
- The internal candidate endpoint returns ``auto_reuse_voice`` only for
  strong matches and ``personal_voice_candidates`` for any match.
- ``official_voice_candidates`` is always returned (empty in Phase 1).
- ``include_cross_source=True`` flag is honoured.
- The public ``POST /job-api/jobs/{id}/voice-candidates`` handler
  validates speaker_id, returns 404 for unknown jobs, and emits the
  empty envelope when the job lacks a ``source_content_hash``.

Endpoint logic is exercised by calling the handler function directly
with mocked dependencies — same pattern as
``test_smart_user_voice_quota_endpoint.py`` /
``test_voice_selection_clone_lock.TestVoiceMatchEndpoint``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

# voice_selection_api imports database.get_db at module import time —
# stub it the same way test_voice_selection_clone_lock does so the
# public-endpoint tests below can import the module under test without
# pulling in the real DB engine.
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


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


def _make_public_request(body: dict) -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    return request


def _make_public_db(job: object | None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db.execute = AsyncMock(return_value=result)
    return db


def _run_public(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPublicVoiceCandidatesEndpoint:
    """Smoke tests for the public-router counterpart in
    ``voice_selection_api.voice_candidates_for_selection``.

    Mirrors the pattern from
    ``tests/test_voice_selection_clone_lock.TestVoiceMatchEndpoint``
    — handlers are invoked directly with mocked DB / auth so we can
    exercise the request-validation and envelope-shape contracts
    without spinning up FastAPI."""

    def test_public_endpoint_rejects_invalid_speaker_id(self, monkeypatch) -> None:
        """``speaker_id`` must match ``_SPEAKER_ID_RE`` — anything else
        (e.g. ``speaker-A`` with a hyphen) is rejected with 400 +
        ``error="invalid_speaker_id"`` before the matcher is touched."""
        import voice_selection_api

        user = SimpleNamespace(id="user-1")
        job = SimpleNamespace(
            job_id="job-bad-spk",
            user_id="user-1",
            source_content_hash="youtube:abc",
        )
        db = _make_public_db(job)
        request = _make_public_request({"speaker_id": "speaker-A"})

        # If the matcher were to run, this would loudly fail — we want
        # to assert the handler never reaches it for an invalid
        # speaker_id.
        matcher = AsyncMock(return_value=[])
        monkeypatch.setattr("user_voice_service.match_user_voices", matcher)
        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        response = _run_public(
            voice_selection_api.voice_candidates_for_selection(
                request, "job-bad-spk", db, user,
            )
        )
        body = json.loads(response.body.decode("utf-8"))

        assert response.status_code == 400
        assert body["error"] == "invalid_speaker_id"
        matcher.assert_not_awaited()

    def test_public_endpoint_returns_404_for_unknown_job(self, monkeypatch) -> None:
        """When ``_verify_job_ownership`` returns ``None`` (job_id not
        found), the public endpoint responds 404 +
        ``error="job_not_found"`` and never queries voice candidates."""
        import voice_selection_api

        user = SimpleNamespace(id="user-1")
        db = _make_public_db(None)  # no Job row
        request = _make_public_request({"speaker_id": "speaker_a"})

        matcher = AsyncMock(return_value=[])
        monkeypatch.setattr("user_voice_service.match_user_voices", matcher)
        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        response = _run_public(
            voice_selection_api.voice_candidates_for_selection(
                request, "job-missing", db, user,
            )
        )
        body = json.loads(response.body.decode("utf-8"))

        assert response.status_code == 404
        assert body["error"] == "job_not_found"
        matcher.assert_not_awaited()

    def test_public_endpoint_returns_empty_envelope_when_job_has_no_source_content_hash(
        self, monkeypatch,
    ) -> None:
        """A job with ``source_content_hash=None`` and no
        ``speaker_name`` in the body produces an empty candidate
        envelope (no same-source candidates, no cross-source
        candidates) but still 200 with the canonical Phase 1 shape.
        """
        import voice_selection_api

        user = SimpleNamespace(id="user-1")
        job = SimpleNamespace(
            job_id="job-no-hash",
            user_id="user-1",
            source_content_hash=None,
        )
        db = _make_public_db(job)
        request = _make_public_request({"speaker_id": "speaker_a"})

        matcher = AsyncMock(return_value=[])
        monkeypatch.setattr("user_voice_service.match_user_voices", matcher)
        monkeypatch.setattr(voice_selection_api.settings, "auth_required", False)

        response = _run_public(
            voice_selection_api.voice_candidates_for_selection(
                request, "job-no-hash", db, user,
            )
        )
        body = json.loads(response.body.decode("utf-8"))

        assert response.status_code == 200
        # Canonical Phase 1 envelope shape, even on the empty path:
        assert body["speaker_id"] == "speaker_a"
        assert body["source_content_hash"] is None
        assert body["auto_reuse_voice"] is None
        assert body["personal_voice_candidates"] == []
        assert body["official_voice_candidates"] == []
        # The matcher is called once with source_content_hash=None —
        # the handler delegates the "missing hash" decision to
        # ``match_user_voices`` rather than short-circuiting itself,
        # since include_cross_source=True can still produce candidates
        # via name match (per the no-hash cross-source unit test).
        matcher.assert_awaited_once()
        kwargs = matcher.await_args.kwargs
        assert kwargs["source_content_hash"] is None
