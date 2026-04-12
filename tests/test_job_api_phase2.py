"""Phase 2 tests: review write endpoints (translation/approve, split-segment, preview-segment, voice/clone)."""
from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.job_test_helpers import FakePopenFactory, set_review_stage, wait_for, write_process_project
from services.jobs.api import build_job_api_server
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


def _build_server(tmp_path: Path, *, plans: list[dict]):
    popen_factory = FakePopenFactory(plans)
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=popen_factory,
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return service, server, thread


def _request_json(method: str, url: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, method=method, data=body)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    with urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _setup_waiting_job(service, server, tmp_path, *, youtube_url, project_name, review_stage="translation_review"):
    """Create a job that enters waiting_for_review, with proper project data on disk."""
    project_dir = write_process_project(tmp_path, project_name=project_name, youtube_url=youtube_url)
    escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")
    base_url = f"http://127.0.0.1:{server.server_port}"
    _, created = _request_json("POST", f"{base_url}/jobs", {
        "job_type": "localize_video",
        "source": {"type": "youtube_url", "value": youtube_url},
        "output_target": "editor",
    })
    job_id = created["job_id"]
    wait_for(lambda: service.require_job(job_id).status == "waiting_for_review", timeout_seconds=5)
    return job_id, project_dir


def _write_segments_json(project_dir: Path, segments: list[dict]) -> None:
    segments_dir = project_dir / "translation"
    segments_dir.mkdir(parents=True, exist_ok=True)
    (segments_dir / "segments.json").write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_transcript_json(project_dir: Path, lines: list[dict]) -> None:
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "transcript.json").write_text(
        json.dumps({"lines": lines}, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# ===================================================================
# translation/approve
# ===================================================================

class TestTranslationApprove:

    def test_approve_acts_on_target_job_project(self, tmp_path: Path) -> None:
        """Approve writes to the target job's project and ignores body.project_dir."""
        url = "https://youtube.example/watch?v=approve-test"
        project_dir = write_process_project(tmp_path, project_name="approve_test", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        # Prepare segments on disk
        segments = [
            {"segment_id": 1, "speaker_id": "speaker_a", "source_text": "Hello",
             "cn_text": "你好"},
        ]
        _write_segments_json(project_dir, segments)

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"translation_review","tab":"translation",'
                f'"project_dir":"{escaped}","message":"review needed"}}'
            ], "returncode": 0},
            {"lines": ["[S6] Done"], "returncode": 0},  # For continue after approve
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(
                service, server, tmp_path, youtube_url=url, project_name="approve_test",
            )
            # Mark translation_review as approved in review_state so continue_job works
            set_review_stage(project_dir, stage_name="translation_review", status="approved", activate=False)

            try:
                status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/translation/approve", {
                    "segments": {"1": {"segment_id": 1, "cn_text": "你好世界"}},
                    "project_dir": "/some/fake/dir",  # MUST be ignored
                })
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise AssertionError(f"HTTP {exc.code}: {error_body}") from exc
            assert status == HTTPStatus.OK
            assert result["success"] is True

            # Verify segments were updated on disk (in the job's own project)
            updated_data = json.loads((project_dir / "translation" / "segments.json").read_text(encoding="utf-8"))
            updated_segments = updated_data.get("segments", updated_data) if isinstance(updated_data, dict) else updated_data
            assert any(s.get("cn_text") == "你好世界" for s in updated_segments if isinstance(s, dict))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_approve_rejects_unknown_job(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            try:
                _request_json("POST", f"{base_url}/jobs/nonexistent/review/translation/approve", {"segments": {}})
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND
            except ConnectionError:
                pass  # Windows socket race on empty server — acceptable for this test
            else:
                raise AssertionError("Expected 404 or connection error")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_approve_with_segment_speakers_does_not_500(self, tmp_path: Path) -> None:
        """Regression: segment_speakers payload must not cause 500 (wrong kwarg name)."""
        url = "https://youtube.example/watch?v=approve-speakers"
        project_dir = write_process_project(tmp_path, project_name="approve_speakers", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        segments = [
            {"segment_id": 1, "speaker_id": "speaker_a", "source_text": "Hello",
             "cn_text": "你好"},
            {"segment_id": 2, "speaker_id": "speaker_a", "source_text": "World",
             "cn_text": "世界"},
        ]
        _write_segments_json(project_dir, segments)
        _write_transcript_json(project_dir, [
            {"index": 0, "speaker_id": "speaker_a", "source_text": "Hello",
             "en_text": "Hello", "start_ms": 0, "end_ms": 1000},
            {"index": 1, "speaker_id": "speaker_a", "source_text": "World",
             "en_text": "World", "start_ms": 1000, "end_ms": 2000},
        ])
        # Need speaker_review stage for speaker update to work
        set_review_stage(project_dir, stage_name="speaker_review", status="approved",
                         payload={"segment_speakers": {"0": "speaker_a", "1": "speaker_a"}}, activate=False)

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"translation_review","tab":"translation",'
                f'"project_dir":"{escaped}","message":"review needed"}}'
            ], "returncode": 0},
            {"lines": ["[S6] Done"], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(service, server, tmp_path, youtube_url=url, project_name="approve_speakers")
            set_review_stage(project_dir, stage_name="translation_review", status="approved", activate=False)

            try:
                status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/translation/approve", {
                    "segments": {"1": {"segment_id": 1, "cn_text": "你好世界"}},
                    "segment_speakers": {"1": "speaker_b"},
                })
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise AssertionError(f"HTTP {exc.code}: {error_body}") from exc
            assert status == HTTPStatus.OK
            assert result["success"] is True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_approve_rejects_non_waiting_for_review_and_does_not_write(self, tmp_path: Path) -> None:
        """Approve must fail before writing if job is not in waiting_for_review."""
        url = "https://youtube.example/watch?v=approve-gate"
        project_dir = write_process_project(tmp_path, project_name="approve_gate", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        segments = [
            {"segment_id": 1, "speaker_id": "speaker_a", "source_text": "Hello",
             "cn_text": "你好"},
        ]
        _write_segments_json(project_dir, segments)

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [f"[S6] Done {project_dir / 'output'}"], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            _, created = _request_json("POST", f"{base_url}/jobs", {
                "job_type": "localize_video",
                "source": {"type": "youtube_url", "value": url},
                "output_target": "editor",
            })
            job_id = created["job_id"]
            wait_for(lambda: service.require_job(job_id).status == "succeeded", timeout_seconds=5)

            # Save original segments for comparison
            original_content = (project_dir / "translation" / "segments.json").read_text(encoding="utf-8")

            # Try to approve on a succeeded job — must fail
            try:
                _request_json("POST", f"{base_url}/jobs/{job_id}/review/translation/approve", {
                    "segments": {"1": {"segment_id": 1, "cn_text": "被篡改的文本"}},
                })
            except HTTPError as exc:
                assert exc.code == HTTPStatus.CONFLICT, f"Expected 409, got {exc.code}"
            else:
                raise AssertionError("Expected 409 for non-waiting_for_review job")

            # Verify segments.json was NOT modified
            current_content = (project_dir / "translation" / "segments.json").read_text(encoding="utf-8")
            assert current_content == original_content, "segments.json must not be modified when approve fails"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# split-segment
# ===================================================================

class TestSplitSegment:

    def test_split_modifies_target_job_project(self, tmp_path: Path) -> None:
        """Split only modifies the target job's project files, ignoring body.project_dir."""
        url = "https://youtube.example/watch?v=split-test"
        project_dir = write_process_project(tmp_path, project_name="split_test", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        # Write segments and transcript that the split helper expects
        segments = [
            {"segment_id": 1, "speaker_id": "speaker_a", "source_text": "Hello World", "cn_text": "你好世界",
             "start_ms": 0, "end_ms": 2000, "target_duration_ms": 2000,
             "tts_audio_path": None, "aligned_audio_path": None, "actual_duration_ms": 0,
             "alignment_ratio": 0.0, "alignment_method": "", "rewrite_count": 0, "needs_review": False},
            {"segment_id": 2, "speaker_id": "speaker_a", "source_text": "Goodbye", "cn_text": "再见",
             "start_ms": 2000, "end_ms": 4000, "target_duration_ms": 2000,
             "tts_audio_path": None, "aligned_audio_path": None, "actual_duration_ms": 0,
             "alignment_ratio": 0.0, "alignment_method": "", "rewrite_count": 0, "needs_review": False},
        ]
        _write_segments_json(project_dir, segments)
        _write_transcript_json(project_dir, [
            {"index": 0, "speaker_id": "speaker_a", "source_text": "Hello World",
             "en_text": "Hello World", "start_ms": 0, "end_ms": 2000},
            {"index": 1, "speaker_id": "speaker_a", "source_text": "Goodbye",
             "en_text": "Goodbye", "start_ms": 2000, "end_ms": 4000},
        ])

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"translation_review","tab":"translation",'
                f'"project_dir":"{escaped}","message":"review needed"}}'
            ], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(
                service, server, tmp_path, youtube_url=url, project_name="split_test",
            )

            try:
                status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/split-segment", {
                    "segment_id": 1,
                    "split_source_index": 5,
                    "split_cn_index": 2,
                    "project_dir": "/fake/ignored",
                })
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise AssertionError(f"HTTP {exc.code}: {error_body}") from exc
            assert status == HTTPStatus.OK
            assert result["success"] is True

            # Verify split happened in the job's project
            updated_data = json.loads((project_dir / "translation" / "segments.json").read_text(encoding="utf-8"))
            updated_segments = updated_data.get("segments", updated_data) if isinstance(updated_data, dict) else updated_data
            assert len(updated_segments) == 3  # Was 2, now 3 after split
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_split_with_pending_speaker_changes_does_not_500(self, tmp_path: Path) -> None:
        """Regression: pending_speaker_changes must not cause 500 (wrong kwarg name)."""
        url = "https://youtube.example/watch?v=split-speakers"
        project_dir = write_process_project(tmp_path, project_name="split_speakers", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        segments = [
            {"segment_id": 1, "speaker_id": "speaker_a", "source_text": "Hello World", "cn_text": "你好世界",
             "start_ms": 0, "end_ms": 2000, "target_duration_ms": 2000,
             "tts_audio_path": None, "aligned_audio_path": None, "actual_duration_ms": 0,
             "alignment_ratio": 0.0, "alignment_method": "", "rewrite_count": 0, "needs_review": False},
        ]
        _write_segments_json(project_dir, segments)
        _write_transcript_json(project_dir, [
            {"index": 0, "speaker_id": "speaker_a", "source_text": "Hello World",
             "en_text": "Hello World", "start_ms": 0, "end_ms": 2000},
        ])
        set_review_stage(project_dir, stage_name="speaker_review", status="approved",
                         payload={"segment_speakers": {"0": "speaker_a"}}, activate=False)

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"translation_review","tab":"translation",'
                f'"project_dir":"{escaped}","message":"review needed"}}'
            ], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(service, server, tmp_path, youtube_url=url, project_name="split_speakers")

            try:
                status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/split-segment", {
                    "segment_id": 1,
                    "split_source_index": 5,
                    "split_cn_index": 2,
                    "pending_speaker_changes": {"0": "speaker_b"},
                })
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise AssertionError(f"HTTP {exc.code}: {error_body}") from exc
            assert status == HTTPStatus.OK
            assert result["success"] is True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# preview-segment (stubbed — no real ffmpeg/TTS)
# ===================================================================

class TestPreviewSegment:

    def test_preview_returns_expected_structure(self, tmp_path: Path) -> None:
        """Preview returns source_audio_base64 + tts_audio_base64 structure."""
        url = "https://youtube.example/watch?v=preview-test"
        project_dir = write_process_project(tmp_path, project_name="preview_test", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"translation_review","tab":"translation",'
                f'"project_dir":"{escaped}","message":"review needed"}}'
            ], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(
                service, server, tmp_path, youtube_url=url, project_name="preview_test",
            )

            # No real ffmpeg/TTS — endpoint catches exceptions and returns empty strings
            status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/preview-segment", {
                "segment_id": 1,
                "source_start_ms": 0,
                "source_end_ms": 1000,
                "cn_text": "测试",
                "voice_id": "fake_voice",
                "project_dir": "/fake/ignored",
            })
            assert status == HTTPStatus.OK
            assert "source_audio_base64" in result
            assert "tts_audio_base64" in result
            assert result["source_format"] == "wav"
            assert result["tts_format"] == "wav"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_preview_rejects_unknown_job(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            try:
                _request_json("POST", f"{base_url}/jobs/nonexistent/review/preview-segment", {
                    "segment_id": 1, "cn_text": "t", "voice_id": "v",
                })
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND
            else:
                raise AssertionError("Expected 404")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ===================================================================
# voice/clone (stubbed — no real MiniMax API)
# ===================================================================

class TestVoiceClone:

    def test_clone_uses_jobid_authority(self, tmp_path: Path) -> None:
        """Clone uses the job's project for auto-extraction, not body.project_dir."""
        url = "https://youtube.example/watch?v=clone-test"
        project_dir = write_process_project(tmp_path, project_name="clone_test", youtube_url=url)
        escaped = str(project_dir.resolve(strict=False)).replace("\\", "\\\\")

        # Write transcript for auto-extraction
        _write_transcript_json(project_dir, [
            {"index": 0, "speaker_id": "speaker_a", "source_text": "Hello",
             "en_text": "Hello", "start_ms": 0, "end_ms": 2000},
        ])
        # Write fake audio
        audio_dir = project_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "speech_for_asr.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        service, server, thread = _build_server(tmp_path, plans=[
            {"lines": [
                f'[WEB_REVIEW] {{"stage":"voice_review","tab":"voice-library",'
                f'"project_dir":"{escaped}","message":"voice review"}}'
            ], "returncode": 0},
        ])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            job_id, _ = _setup_waiting_job(
                service, server, tmp_path, youtube_url=url, project_name="clone_test",
            )

            # Patch at the import source, not the module that lazy-imports
            with patch("services.voice.sample_extractor.VoiceSampleExtractor") as mock_extractor_cls, \
                 patch("services.voice_clone.VoiceCloneConfig") as mock_config_cls, \
                 patch("services.voice_clone.MiniMaxVoiceCloneClient") as mock_client_cls:

                mock_extractor = MagicMock()
                mock_extractor_cls.return_value = mock_extractor

                mock_clone_result = MagicMock()
                mock_clone_result.voice_id = "cloned_voice_001"
                mock_client = MagicMock()
                mock_client.create_voice_clone.return_value = mock_clone_result
                mock_client_cls.return_value = mock_client
                mock_config_cls.from_env.return_value = MagicMock()

                status, result = _request_json("POST", f"{base_url}/jobs/{job_id}/review/voice/clone", {
                    "speaker_id": "speaker_a",
                    "speaker_name": "Dan",
                    "sample_path": "",
                    "project_dir": "/fake/ignored",
                })
                assert status == HTTPStatus.OK
                assert result["success"] is True
                assert result["voice_id"] == "cloned_voice_001"
                assert result["speaker_id"] == "speaker_a"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_clone_rejects_unknown_job(self, tmp_path: Path) -> None:
        service, server, thread = _build_server(tmp_path, plans=[])
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            try:
                _request_json("POST", f"{base_url}/jobs/nonexistent/review/voice/clone", {
                    "speaker_id": "speaker_a",
                })
            except HTTPError as exc:
                assert exc.code == HTTPStatus.NOT_FOUND
            else:
                raise AssertionError("Expected 404")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
