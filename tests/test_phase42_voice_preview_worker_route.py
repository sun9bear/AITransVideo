"""CosyVoice clone preview must use mainland-worker routing metadata.

Regression covered:

The voice-selection "试听" endpoint used to call the legacy direct
CosyVoice helper with model ``cosyvoice-v3-flash`` even for clone voices whose
ids are bound to ``cosyvoice-v3.5-flash`` / ``plus``. DashScope then returned
no audio for clone ids. Gateway must inject trusted routing from
``user_voices`` and the upstream Job API must synthesize through the worker.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "gateway", REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _inline_zip(audio_path: str, wav_bytes: bytes) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(audio_path, wav_bytes)
    payload = buf.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def test_preview_voice_cosyvoice_clone_uses_worker_target_model(monkeypatch):
    from services.jobs import review_actions
    from services.mainland_worker.client import MainlandWorkerClient
    from services.mainland_worker.types import (
        WorkerArtifactPackage,
        WorkerSegmentResult,
        WorkerSynthesizeBatchResponse,
    )
    import services.mainland_worker.client_factory as client_factory

    wav_bytes = b"RIFF" + (b"\x00" * 256)
    audio_path = "segments/segment_001_preview.wav"
    package_bytes, package_sha = _inline_zip(audio_path, wav_bytes)

    class FakeClient:
        request = None
        closed = False

        def synthesize_batch(self, request):
            self.request = request
            return WorkerSynthesizeBatchResponse(
                ok=True,
                job_id=request.job_id,
                target_model=request.target_model,
                segments=(
                    WorkerSegmentResult(
                        segment_id=1,
                        speaker_id="preview",
                        voice_id=request.segments[0].voice_id,
                        audio_path=audio_path,
                        duration_ms=1000,
                        billed_chars=10,
                        sha256=hashlib.sha256(wav_bytes).hexdigest(),
                    ),
                ),
                package=WorkerArtifactPackage(
                    kind="inline_base64",
                    download_url="",
                    sha256=package_sha,
                    expires_at="2026-05-27T00:00:00Z",
                    inline_bytes=package_bytes,
                ),
                worker_request_id="wr-preview-1",
            )

        def extract_artifact_segments(self, response):
            return MainlandWorkerClient.extract_artifact_segments(response)

        def close(self):
            self.closed = True

    fake_client = FakeClient()
    monkeypatch.setattr(
        client_factory, "build_client_from_env", lambda: fake_client
    )

    result = review_actions.preview_voice(
        voice_id="cosyvoice-v3.5-flash-avtspeak-test",
        tts_provider="cosyvoice",
        config_path=Path("unused.toml"),
        sample_text="hello preview text",
        requires_worker=True,
        worker_target_model="cosyvoice-v3.5-flash",
        job_id="job_preview",
    )

    assert result["error"] is None
    assert base64.b64decode(str(result["audio_base64"])) == wav_bytes
    assert fake_client.closed is True
    assert fake_client.request is not None
    assert fake_client.request.job_id == "job_preview"
    assert fake_client.request.target_model == "cosyvoice-v3.5-flash"
    assert fake_client.request.segments[0].voice_id == "cosyvoice-v3.5-flash-avtspeak-test"


def test_preview_voice_cosyvoice_clone_without_target_model_fails_closed(monkeypatch):
    from services.jobs import review_actions
    import services.mainland_worker.client_factory as client_factory

    called = False

    def fake_build_client():
        nonlocal called
        called = True
        raise AssertionError("worker client should not be constructed")

    monkeypatch.setattr(client_factory, "build_client_from_env", fake_build_client)

    result = review_actions.preview_voice(
        voice_id="cosyvoice-v3.5-flash-avtspeak-test",
        tts_provider="cosyvoice",
        config_path=Path("unused.toml"),
        requires_worker=True,
        worker_target_model="",
    )

    assert called is False
    assert result["audio_base64"] == ""
    assert "worker_target_model" in str(result["error"])


class _StubSession:
    pass


def _run_preview_enrich(
    monkeypatch,
    payload: dict[str, Any],
    *,
    user_id: object = "u-test",
    lookup_map: dict[str, dict[str, Any]] | None = None,
    lookup_raises: type[BaseException] | None = None,
):
    import job_intercept
    import user_voice_service

    async def fake_lookup(db, *, user_id, voice_ids):
        if lookup_raises:
            raise lookup_raises("simulated db failure")
        return dict(lookup_map or {})

    monkeypatch.setattr(
        user_voice_service, "lookup_clone_voice_routing_metadata", fake_lookup
    )
    return asyncio.run(
        job_intercept._enrich_voice_preview_routing(
            _StubSession(), user_id=user_id, payload=payload
        )
    )


def test_gateway_preview_enrichment_injects_worker_routing(monkeypatch):
    enriched, error = _run_preview_enrich(
        monkeypatch,
        {
            "voice_id": "cosyvoice-v3.5-flash-avtspeak-test",
            "tts_provider": "cosyvoice",
            "requires_worker": False,
            "worker_target_model": "MALICIOUS",
        },
        lookup_map={
            "cosyvoice-v3.5-flash-avtspeak-test": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        },
    )

    assert error is None
    assert enriched is not None
    assert enriched["tts_provider"] == "cosyvoice"
    assert enriched["requires_worker"] is True
    assert enriched["worker_target_model"] == "cosyvoice-v3.5-flash"


def test_gateway_preview_enrichment_rejects_provider_mismatch(monkeypatch):
    enriched, error = _run_preview_enrich(
        monkeypatch,
        {
            "voice_id": "cosyvoice-v3.5-flash-avtspeak-test",
            "tts_provider": "minimax",
        },
        lookup_map={
            "cosyvoice-v3.5-flash-avtspeak-test": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            }
        },
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_provider_mismatch"


def test_gateway_preview_enrichment_strips_forged_routing_for_unknown_voice(monkeypatch):
    enriched, error = _run_preview_enrich(
        monkeypatch,
        {
            "voice_id": "builtin_voice",
            "tts_provider": "cosyvoice",
            "requires_worker": True,
            "worker_target_model": "cosyvoice-v3.5-flash",
        },
        lookup_map={},
    )

    assert error is None
    assert enriched is not None
    assert "requires_worker" not in enriched
    assert "worker_target_model" not in enriched


def test_gateway_preview_enrichment_fails_closed_on_cosyvoice_lookup_error(monkeypatch):
    enriched, error = _run_preview_enrich(
        monkeypatch,
        {
            "voice_id": "cosyvoice-v3.5-flash-avtspeak-test",
            "tts_provider": "cosyvoice",
        },
        lookup_raises=RuntimeError,
    )

    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"
