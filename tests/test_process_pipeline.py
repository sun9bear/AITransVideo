import json
import os
from pathlib import Path

from pydub import AudioSegment
import pytest

from modules.ingestion.youtube.downloader import DownloadResult
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.output_models import OutputBundleResult
from modules.output.project_output import AlignedSegment
from modules.workflow.project_builder import ProjectBuilder
from modules.workflow.workflow_result import WorkflowBuildResult
from pipeline.process import ProcessConfig, ProcessPipeline
from services.assemblyai.transcriber import TranscriptLine, TranscriptResult, TranscriptionError
from services.alignment.aligner import PostTTSBudgetTracker
from services.gemini.translator import DubbingSegment, TranslationResult
from services.tts.tts_generator import TTSConfig, TTSResult
from services.voice.auto_clone import AutoCloneError
from services.voice.voice_lookup import VoiceLookupError
import pipeline.process as process_module


# ===================================================================
# ProcessConfig source field normalization
# ===================================================================


class TestProcessConfigSourceNormalization:
    """Verify backward-compatible normalization between youtube_url and source_type/source_ref."""

    def test_legacy_youtube_url_only(self):
        c = ProcessConfig(youtube_url="https://youtube.com/watch?v=abc")
        assert c.source_type == "youtube_url"
        assert c.source_ref == "https://youtube.com/watch?v=abc"
        assert c.youtube_url == "https://youtube.com/watch?v=abc"

    def test_explicit_youtube_source(self):
        c = ProcessConfig(source_type="youtube_url", source_ref="https://youtube.com/watch?v=xyz")
        assert c.youtube_url == "https://youtube.com/watch?v=xyz"
        assert c.source_type == "youtube_url"
        assert c.source_ref == "https://youtube.com/watch?v=xyz"

    def test_explicit_local_video(self):
        c = ProcessConfig(source_type="local_video", source_ref="/uploads/42/video.mp4")
        assert c.source_type == "local_video"
        assert c.source_ref == "/uploads/42/video.mp4"
        assert c.youtube_url == ""

    def test_explicit_local_audio(self):
        c = ProcessConfig(source_type="local_audio", source_ref="D:/input.wav")
        assert c.source_type == "local_audio"
        assert c.source_ref == "D:/input.wav"
        assert c.youtube_url == ""

    def test_explicit_local_wins_over_legacy_youtube_url(self):
        c = ProcessConfig(
            youtube_url="https://youtube.com/old",
            source_type="local_video",
            source_ref="/uploads/new.mp4",
        )
        assert c.source_type == "local_video"
        assert c.source_ref == "/uploads/new.mp4"
        assert c.youtube_url == ""  # non-YouTube: youtube_url must be cleared

    def test_explicit_youtube_overrides_old_youtube_url(self):
        """When both youtube_url and explicit youtube source are given, explicit wins."""
        c = ProcessConfig(
            youtube_url="https://youtube.com/old",
            source_type="youtube_url",
            source_ref="https://youtube.com/new",
        )
        assert c.source_type == "youtube_url"
        assert c.source_ref == "https://youtube.com/new"
        assert c.youtube_url == "https://youtube.com/new"  # must be overridden

    def test_both_empty_leaves_fields_empty(self):
        c = ProcessConfig()
        assert c.source_type == ""
        assert c.source_ref == ""
        assert c.youtube_url == ""

    def test_explicit_youtube_backfills_youtube_url_for_pipeline_compat(self):
        """Existing pipeline code reads config.youtube_url; must still work."""
        c = ProcessConfig(source_type="youtube_url", source_ref="https://yt.com/v=test")
        assert c.youtube_url == "https://yt.com/v=test"


def test_report_source_metadata_can_send_s2_display_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        # Capture all headers so the X-Internal-Key regression can assert.
        captured["headers"] = dict(req.headers)
        return FakeResponse()

    monkeypatch.setenv("AVT_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-internal-key-1234567890ab")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    process_module._report_source_metadata(
        "job-1",
        display_name="巴菲特谈接班",
        stage_label="S2",
    )

    assert captured["url"] == "http://gateway.test/job-api/jobs/job-1/source-metadata"
    assert captured["timeout"] == 5
    assert captured["body"] == {"display_name": "巴菲特谈接班"}
    # P0-1 audit follow-up: gateway tightened auth on /source-metadata, so the
    # pipeline callback MUST forward AVT_INTERNAL_API_KEY as X-Internal-Key.
    # urllib title-cases header names internally, so check both spellings to
    # avoid a flake if urllib internals change.
    headers = captured["headers"]
    header_keys = {k.lower() for k in headers}
    assert "x-internal-key" in header_keys, f"X-Internal-Key missing from {headers}"
    val = headers.get("X-internal-key") or headers.get("X-Internal-Key") or headers.get("x-internal-key")
    assert val == "test-internal-key-1234567890ab"


def test_report_source_metadata_omits_header_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """If AVT_INTERNAL_API_KEY is unset, callback degrades gracefully (request
    will 403 but outer try/except already handles it). We assert NO X-Internal-Key
    header is sent to avoid the request being interpreted as having an empty
    key value (which would still fail server-side comparison)."""
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        return FakeResponse()

    monkeypatch.delenv("AVT_INTERNAL_API_KEY", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    process_module._report_source_metadata("job-x", display_name="t")

    header_keys = {k.lower() for k in captured["headers"]}
    assert "x-internal-key" not in header_keys


def test_report_job_metering_sends_internal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-1 audit follow-up: /job-api/jobs/{id}/metering is auth-gated, so
    the pipeline callback MUST forward X-Internal-Key. Without this, every
    metering writeback (final_cn_chars, tts_billed_chars, glossary metrics)
    silently 403s into the outer try/except."""
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("AVT_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "metering-test-key-1234567890ab")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    # Minimal segments shape — _report_job_metering is permissive on shape.
    segments = [{"cn_text": "你好"}]
    process_module._report_job_metering("job-m1", segments, tts_billed_chars=2)

    assert captured["url"] == "http://gateway.test/job-api/jobs/job-m1/metering"
    header_keys = {k.lower() for k in captured["headers"]}
    assert "x-internal-key" in header_keys, f"X-Internal-Key missing from {captured['headers']}"
    val = (
        captured["headers"].get("X-internal-key")
        or captured["headers"].get("X-Internal-Key")
        or captured["headers"].get("x-internal-key")
    )
    assert val == "metering-test-key-1234567890ab"


# ===================================================================
# ProcessPipeline source-aware ingest tests
# ===================================================================


def _stub_pipeline_configs(monkeypatch):
    """Stub config loaders so pipeline.run() can proceed past config loading."""
    monkeypatch.setattr(
        "pipeline.process.load_assemblyai_config",
        lambda: {"api_key": "fake-assemblyai-key"},
    )
    monkeypatch.setattr(
        "pipeline.process.load_gemini_config",
        lambda: {"api_key": "fake-gemini-key"},
    )
    monkeypatch.setattr(
        "pipeline.process.load_llm_fallback_config",
        lambda: {"provider": "mock"},
    )
    monkeypatch.setattr(
        "pipeline.process.load_tts_config",
        lambda: {"api_key": "fake-tts-key"},
    )
    monkeypatch.setattr(
        "pipeline.process.load_youtube_download_config",
        lambda: {},
    )


class TestProcessPipelineSourceAwareIngest:
    """Verify that ProcessPipeline.run() branches correctly on source_type."""

    def test_local_video_does_not_call_youtube_downloader(self, tmp_path, monkeypatch):
        """local_video source must skip YouTubeDownloader entirely."""
        _stub_pipeline_configs(monkeypatch)

        source_video = tmp_path / "input_video.mp4"
        source_video.write_bytes(b"\x00" * 100)

        project_dir = tmp_path / "workspace"
        project_dir.mkdir()

        download_called = {"count": 0}

        import modules.ingestion.youtube.downloader as dl_module

        class _TrackingDownloader:
            def download(self, *args, **kwargs):
                download_called["count"] += 1
                raise AssertionError("YouTubeDownloader.download should not be called for local_video")

        monkeypatch.setattr(dl_module, "YouTubeDownloader", _TrackingDownloader)

        def fake_extract(video_path, output_audio_path):
            output_audio_path.parent.mkdir(parents=True, exist_ok=True)
            output_audio_path.write_bytes(b"\x00" * 50)

        monkeypatch.setattr(ProcessPipeline, "_extract_audio_from_video", staticmethod(fake_extract))
        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 10000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_video",
            source_ref=str(source_video),
            project_dir=str(project_dir),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass  # Expected to fail in later stages (no real transcriber)

        assert download_called["count"] == 0
        assert (project_dir / "video").exists()
        assert any((project_dir / "video").iterdir())
        meta_path = project_dir / "download_metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["source_type"] == "local_video"

    def test_local_audio_does_not_call_youtube_downloader(self, tmp_path, monkeypatch):
        """local_audio source must skip YouTubeDownloader entirely."""
        _stub_pipeline_configs(monkeypatch)

        source_audio = tmp_path / "input_audio.wav"
        source_audio.write_bytes(b"\x00" * 100)

        project_dir = tmp_path / "workspace"
        project_dir.mkdir()

        download_called = {"count": 0}

        import modules.ingestion.youtube.downloader as dl_module

        class _TrackingDownloader:
            def download(self, *args, **kwargs):
                download_called["count"] += 1
                raise AssertionError("YouTubeDownloader.download should not be called for local_audio")

        monkeypatch.setattr(dl_module, "YouTubeDownloader", _TrackingDownloader)
        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 5000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_audio",
            source_ref=str(source_audio),
            project_dir=str(project_dir),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        assert download_called["count"] == 0
        assert (project_dir / "audio").exists()
        meta_path = project_dir / "download_metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["source_type"] == "local_audio"

    def test_local_video_uses_explicit_project_dir(self, tmp_path, monkeypatch):
        """local_video must use the passed project_dir (workspace_dir from runner)."""
        _stub_pipeline_configs(monkeypatch)

        source_video = tmp_path / "my_video.mp4"
        source_video.write_bytes(b"\x00" * 100)

        workspace = tmp_path / "projects" / "42" / "job_abc"
        workspace.mkdir(parents=True)

        monkeypatch.setattr(ProcessPipeline, "_extract_audio_from_video", staticmethod(
            lambda vp, ap: ap.parent.mkdir(parents=True, exist_ok=True) or ap.write_bytes(b"\x00" * 50)
        ))
        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 8000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_video",
            source_ref=str(source_video),
            project_dir=str(workspace),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        assert (workspace / "video").exists()
        assert (workspace / "download_metadata.json").exists()

    def test_ingestion_stage_payload_has_correct_source_kind(self, tmp_path, monkeypatch):
        """Ingestion stage payload must reflect the actual source_type via download_metadata."""
        _stub_pipeline_configs(monkeypatch)

        source_audio = tmp_path / "speech.wav"
        source_audio.write_bytes(b"\x00" * 100)

        project_dir = tmp_path / "workspace"
        project_dir.mkdir()

        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 3000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_audio",
            source_ref=str(source_audio),
            project_dir=str(project_dir),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        # After ingest, project_state.json should record ingestion stage
        state_path = project_dir / "project_state.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ingestion = state.get("stages", {}).get("ingestion", {})
        payload = ingestion.get("payload", {})
        assert payload.get("source_kind") == "local_audio"
        assert payload.get("execution_mode") == "local_ingest"


# ===================================================================
# Local source metadata path persistence tests
# ===================================================================


class TestProcessPipelineLocalSourceMetadataPaths:
    """Verify that local source metadata records real file paths, not hardcoded .mp4/.wav."""

    def test_local_video_mkv_metadata_has_real_video_path(self, tmp_path, monkeypatch):
        """local_video with .mkv extension: metadata must record .mkv, not .mp4."""
        _stub_pipeline_configs(monkeypatch)

        source_video = tmp_path / "interview.mkv"
        source_video.write_bytes(b"\x00" * 100)

        workspace = tmp_path / "ws"
        workspace.mkdir()

        def fake_extract(video_path, output_audio_path):
            output_audio_path.parent.mkdir(parents=True, exist_ok=True)
            output_audio_path.write_bytes(b"\x00" * 50)

        monkeypatch.setattr(ProcessPipeline, "_extract_audio_from_video", staticmethod(fake_extract))
        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 12000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_video",
            source_ref=str(source_video),
            project_dir=str(workspace),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        # Real file should be .mkv
        assert (workspace / "video" / "original.mkv").exists()
        assert not (workspace / "video" / "original.mp4").exists()

        # Metadata must record the real .mkv path
        meta = json.loads((workspace / "download_metadata.json").read_text(encoding="utf-8"))
        assert "original.mkv" in meta["video_path"]
        assert Path(meta["video_path"]).name == "original.mkv"
        # audio_path should be the extracted .wav
        assert "original.wav" in meta["audio_path"]

    def test_local_audio_mp3_metadata_has_real_audio_path(self, tmp_path, monkeypatch):
        """local_audio with .mp3 extension: metadata must record .mp3, not .wav."""
        _stub_pipeline_configs(monkeypatch)

        source_audio = tmp_path / "podcast.mp3"
        source_audio.write_bytes(b"\x00" * 100)

        workspace = tmp_path / "ws"
        workspace.mkdir()

        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 6000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_audio",
            source_ref=str(source_audio),
            project_dir=str(workspace),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        # Real file should be .mp3
        assert (workspace / "audio" / "original.mp3").exists()
        assert not (workspace / "audio" / "original.wav").exists()

        meta = json.loads((workspace / "download_metadata.json").read_text(encoding="utf-8"))
        assert "original.mp3" in meta["audio_path"]
        assert Path(meta["audio_path"]).name == "original.mp3"

    def test_load_download_result_reads_real_paths_from_metadata(self, tmp_path):
        """_load_download_result must use metadata paths, not hardcoded names."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "video").mkdir()
        (workspace / "audio").mkdir()

        real_video = workspace / "video" / "original.mkv"
        real_audio = workspace / "audio" / "original.mp3"
        real_video.write_bytes(b"\x00" * 10)
        real_audio.write_bytes(b"\x00" * 10)

        (workspace / "download_metadata.json").write_text(
            json.dumps({
                "video_path": str(real_video),
                "audio_path": str(real_audio),
                "video_title": "Test",
                "duration_ms": 5000,
                "url": "/local/test.mkv",
            }),
            encoding="utf-8",
        )

        pipeline = ProcessPipeline()
        result = pipeline._load_download_result(workspace, fallback_url="fallback")

        assert "original.mkv" in result.video_path
        assert "original.mp3" in result.audio_path

    def test_load_download_result_falls_back_to_legacy_paths_when_metadata_missing(self, tmp_path):
        """Without metadata video_path/audio_path, fall back to original.mp4/wav."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Write metadata without video_path/audio_path fields (old format)
        (workspace / "download_metadata.json").write_text(
            json.dumps({
                "video_title": "Old Video",
                "duration_ms": 3000,
                "url": "https://youtube.com/watch?v=old",
            }),
            encoding="utf-8",
        )

        pipeline = ProcessPipeline()
        result = pipeline._load_download_result(workspace, fallback_url="fallback")

        assert result.video_path.endswith("original.mp4")
        assert result.audio_path.endswith("original.wav")

    def test_local_audio_ingestion_artifacts_exclude_nonexistent_video(self, tmp_path, monkeypatch):
        """local_audio: ingestion artifacts must not include nonexistent video file."""
        _stub_pipeline_configs(monkeypatch)

        source_audio = tmp_path / "voice.flac"
        source_audio.write_bytes(b"\x00" * 100)

        workspace = tmp_path / "ws"
        workspace.mkdir()

        monkeypatch.setattr("pipeline.process._ffprobe_duration_ms", lambda p: 4000)

        pipeline = ProcessPipeline()
        config = ProcessConfig(
            source_type="local_audio",
            source_ref=str(source_audio),
            project_dir=str(workspace),
        )

        try:
            pipeline.run(config)
        except Exception:
            pass

        state_path = workspace / "project_state.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ingestion = state.get("stages", {}).get("ingestion", {})
        artifacts = ingestion.get("payload", {}).get("artifacts", {})
        file_paths = artifacts.get("file_paths", [])
        # No None entries and no nonexistent video path
        assert None not in file_paths
        for fp in file_paths:
            if fp and "video" in fp.lower():
                # If a video path is listed, it must actually exist
                assert Path(fp).exists(), f"Listed video path does not exist: {fp}"


# ===================================================================
# Workspace isolation tests (Task 5)
# ===================================================================


class TestProcessPipelineWorkspaceIsolation:
    """Verify new tasks never share workspace with old tasks based on URL match."""

    def test_same_url_without_project_dir_creates_fresh_workspace(self, tmp_path, monkeypatch):
        """An existing project with the same URL must NOT be reused by a new task."""
        monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
        _stub_pipeline_configs(monkeypatch)

        # Set up an old project for same URL
        old_dir = tmp_path / "projects" / "old_project"
        old_dir.mkdir(parents=True)
        _write_video(old_dir / "video" / "original.mp4")
        _export_silent_wav(old_dir / "audio" / "original.wav", duration_ms=2_000)
        _write_download_metadata(
            old_dir,
            video_path=old_dir / "video" / "original.mp4",
            audio_path=old_dir / "audio" / "original.wav",
            video_title="Old Project",
            duration_ms=2_000,
            url="https://youtube.example/watch?v=same-url",
        )

        observed = {}

        class TrackingDownloader:
            def download(self, request):
                observed["output_dir"] = request.output_dir
                vp = _write_video(Path(request.output_dir) / "video" / "original.mp4")
                ap = _export_silent_wav(Path(request.output_dir) / "audio" / "original.wav", duration_ms=2_000)
                _write_download_metadata(
                    Path(request.output_dir), video_path=vp, audio_path=ap,
                    video_title="Same URL", duration_ms=2_000, url=request.url,
                )
                return DownloadResult(
                    video_path=str(vp), audio_path=str(ap),
                    video_title="Same URL", duration_ms=2_000, url=request.url,
                )

        monkeypatch.setattr(process_module, "YouTubeDownloader", TrackingDownloader)

        pipeline = ProcessPipeline()
        config = ProcessConfig(youtube_url="https://youtube.example/watch?v=same-url")

        try:
            pipeline.run(config)
        except Exception:
            pass

        # Download must have been called (no skip via URL reuse)
        assert "output_dir" in observed
        # Output dir must NOT be the old project
        assert Path(observed["output_dir"]).resolve(strict=False) != old_dir.resolve(strict=False)

    def test_explicit_project_dir_still_reuses_cached_media(self, tmp_path, monkeypatch):
        """Explicit --project-dir with cached media must still skip download."""
        _stub_pipeline_configs(monkeypatch)
        _install_single_speaker_pipeline_mocks(monkeypatch)

        project_dir = tmp_path / "explicit_workspace"
        _write_video(project_dir / "video" / "original.mp4")
        _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
        _write_download_metadata(
            project_dir,
            video_path=project_dir / "video" / "original.mp4",
            audio_path=project_dir / "audio" / "original.wav",
            video_title="Explicit Cached",
            duration_ms=2_500,
            url="https://youtube.example/watch?v=explicit-cache",
        )

        download_called = {"count": 0}

        class FailDownloader:
            def download(self, request):
                download_called["count"] += 1
                raise AssertionError("Should not download when explicit dir has cached media")

        monkeypatch.setattr(process_module, "YouTubeDownloader", FailDownloader)

        result = ProcessPipeline().run(
            ProcessConfig(
                youtube_url="https://youtube.example/watch?v=explicit-cache",
                voice_a="voice_demo_001",
                project_dir=str(project_dir),
            )
        )

        assert download_called["count"] == 0
        assert Path(result.project_dir) == project_dir.resolve(strict=False)


# Standard job_record snapshots for tests that need specific pipeline behavior.
# Tests that don't pass job_record get express defaults (no review, cosyvoice).
_STUDIO_JOB_RECORD = {
    "service_mode": "studio",
    "tts_provider": "minimax",
    "requires_review": True,
    "voice_clone_enabled": True,
    "voice_strategy": "user_selected",
    "plan_code_snapshot": "plus",
    "role_snapshot": "user",
}

_EXPRESS_JOB_RECORD = {
    "service_mode": "express",
    "tts_provider": "cosyvoice",
    "requires_review": False,
    "voice_clone_enabled": False,
    "voice_strategy": "preset_mapping",
    "plan_code_snapshot": "free",
    "role_snapshot": "user",
}

# Express mode but with voice cloning enabled — for testing auto-clone without review gates
_EXPRESS_WITH_CLONE_JOB_RECORD = {
    "service_mode": "express",
    "tts_provider": "minimax",
    "requires_review": False,
    "voice_clone_enabled": True,
    "voice_strategy": "user_selected",
    "plan_code_snapshot": "plus",
    "role_snapshot": "user",
}


def _export_silent_wav(path: Path, *, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    AudioSegment.silent(duration=duration_ms).export(path, format="wav")
    return path


def _write_video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def _write_download_metadata(
    output_dir: Path,
    *,
    video_path: Path,
    audio_path: Path,
    video_title: str,
    duration_ms: int,
    url: str,
    description: str = "",
) -> Path:
    metadata_path = output_dir / "download_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "video_path": str(video_path.resolve(strict=False)),
                "audio_path": str(audio_path.resolve(strict=False)),
                "video_title": video_title,
                "duration_ms": duration_ms,
                "url": url,
                "description": description,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return metadata_path


def _write_transcript_cache(project_dir: Path, lines: list[TranscriptLine], *, total_duration_ms: int) -> Path:
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    raw_path = transcript_dir / "raw_assemblyai.json"
    raw_path.write_text("{}", encoding="utf-8")
    transcript_path = transcript_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "lines": [
                    {
                        "index": line.index,
                        "start_ms": line.start_ms,
                        "end_ms": line.end_ms,
                        "speaker_id": line.speaker_id,
                        "speaker_label": line.speaker_label,
                        "source_text": line.source_text,
                    }
                    for line in lines
                ],
                "total_duration_ms": total_duration_ms,
                "language": "en",
                "raw_response_path": str(raw_path.resolve(strict=False)),
                "structured_transcript_path": str(transcript_path.resolve(strict=False)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return transcript_path


def _write_segments_cache(project_dir: Path, segments: list[DubbingSegment]) -> Path:
    translation_dir = project_dir / "translation"
    translation_dir.mkdir(parents=True, exist_ok=True)
    segments_path = translation_dir / "segments.json"
    segments_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "segment_id": segment.segment_id,
                        "speaker_id": segment.speaker_id,
                        "display_name": segment.display_name,
                        "voice_id": segment.voice_id,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "target_duration_ms": segment.target_duration_ms,
                        "source_text": segment.source_text,
                        "cn_text": segment.cn_text,
                        "cn_text": segment.cn_text,
                        "tts_audio_path": segment.tts_audio_path,
                        "aligned_audio_path": segment.aligned_audio_path,
                        "actual_duration_ms": segment.actual_duration_ms,
                        "alignment_ratio": segment.alignment_ratio,
                        "alignment_method": segment.alignment_method,
                        "rewrite_count": segment.rewrite_count,
                        "needs_review": segment.needs_review,
                    }
                    for segment in segments
                ],
                "total_segments": len(segments),
                "output_path": str(segments_path.resolve(strict=False)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return segments_path


def _write_review_state(
    project_dir: Path,
    *,
    active_stage: str | None,
    speaker_status: str | None = None,
    speaker_payload: dict[str, object] | None = None,
    translation_status: str,
    translation_payload: dict[str, object] | None = None,
    approved_at: str | None = "2026-03-18T03:50:49.223596+00:00",
) -> Path:
    review_state_path = project_dir / "review_state.json"
    stages: dict[str, object] = {}
    if speaker_status is not None:
        stages["speaker_review"] = {
            "stage": "speaker_review",
            "tab": "review",
            "status": speaker_status,
            "updated_at": "2026-03-18T03:50:49.223596+00:00",
            "approved_at": approved_at if speaker_status == "approved" else None,
            "payload": speaker_payload or {},
        }
    review_state_path.write_text(
        json.dumps(
            {
                "active_stage": active_stage,
                "stages": {
                    **stages,
                    "translation_review": {
                        "stage": "translation_review",
                        "tab": "translation",
                        "status": translation_status,
                        "updated_at": "2026-03-18T03:50:49.223596+00:00",
                        "approved_at": approved_at if translation_status == "approved" else None,
                        "payload": translation_payload or {},
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return review_state_path


def _make_single_speaker_lines() -> list[TranscriptLine]:
    return [
        TranscriptLine(1, 0, 700, "speaker_a", "A", "Hello there."),
        TranscriptLine(2, 700, 1_400, "speaker_a", "A", "This is a test."),
        TranscriptLine(3, 1_400, 2_000, "speaker_a", "A", "Thanks for watching."),
    ]


def _make_dual_speaker_lines() -> list[TranscriptLine]:
    return [
        TranscriptLine(1, 0, 8_000, "speaker_a", "A", "Welcome back to the show."),
        TranscriptLine(2, 8_000, 16_000, "speaker_b", "B", "Thanks for having me."),
        TranscriptLine(3, 16_000, 24_000, "speaker_a", "A", "Let's get started."),
    ]


def _make_three_speaker_lines() -> list[TranscriptLine]:
    return [
        TranscriptLine(1, 0, 8_000, "speaker_a", "A", "Welcome back to the show."),
        TranscriptLine(2, 8_000, 16_000, "speaker_b", "B", "Thanks for having me."),
        TranscriptLine(3, 16_000, 24_000, "speaker_c", "C", "Happy to join you both."),
    ]


def _make_reviewed_dual_speaker_lines() -> list[TranscriptLine]:
    return [
        TranscriptLine(1, 0, 8_000, "speaker_a", "A", "Welcome back to the show."),
        TranscriptLine(2, 8_000, 16_000, "speaker_a", "A", "Thanks for having me."),
        TranscriptLine(3, 16_000, 24_000, "speaker_b", "B", "Let's get started."),
    ]


def _make_single_speaker_segments() -> list[DubbingSegment]:
    return [
        DubbingSegment(
            segment_id=1,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_demo_001",
            start_ms=0,
            end_ms=1_000,
            target_duration_ms=1_000,
            source_text="Hello there. This is a test.",
            cn_text="大家好，这是一个测试。",
        ),
        DubbingSegment(
            segment_id=2,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_demo_001",
            start_ms=1_000,
            end_ms=2_000,
            target_duration_ms=1_000,
            source_text="Thanks for watching.",
            cn_text="感谢观看。",
        ),
    ]


def _make_dual_speaker_segments(
    *,
    voice_a: str,
    voice_b: str,
    display_name_a: str = "Host",
    display_name_b: str = "Guest",
) -> list[DubbingSegment]:
    return [
        DubbingSegment(
            segment_id=1,
            speaker_id="speaker_a",
            display_name=display_name_a,
            voice_id=voice_a,
            start_ms=0,
            end_ms=1_000,
            target_duration_ms=1_000,
            source_text="Welcome back to the show.",
            cn_text="欢迎回到节目。",
        ),
        DubbingSegment(
            segment_id=2,
            speaker_id="speaker_b",
            display_name=display_name_b,
            voice_id=voice_b,
            start_ms=1_000,
            end_ms=2_000,
            target_duration_ms=1_000,
            source_text="Thanks for having me.",
            cn_text="感谢邀请。",
        ),
    ]


def _make_many_single_speaker_segments(count: int) -> list[DubbingSegment]:
    segments: list[DubbingSegment] = []
    for index in range(count):
        start_ms = index * 1_000
        end_ms = start_ms + 1_000
        segments.append(
            DubbingSegment(
                segment_id=index + 1,
                speaker_id="speaker_a",
                display_name="Speaker A",
                voice_id="cached_voice_a",
                start_ms=start_ms,
                end_ms=end_ms,
                target_duration_ms=1_000,
                source_text=f"Source {index + 1}",
                cn_text=f"Cache text {index + 1}",
            )
        )
    return segments


def _install_config_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        process_module,
        "load_assemblyai_config",
        lambda: {
            "api_key": "assembly-key",
            "speaker_labels": False,
            "http_timeout_seconds": 900.0,
        },
    )
    monkeypatch.setattr(
        process_module,
        "load_gemini_config",
        lambda: {
            "api_key": "gemini-key",
            "model_name": "gemini-3.1-pro-preview",
            "temperature": 0.3,
            "max_output_tokens": 8192,
            "sdk_backend": "google-genai",
        },
    )
    monkeypatch.setattr(
        process_module,
        "load_llm_fallback_config",
        lambda: {
            "openai": {
                "api_key": None,
                "api_key_env_var": "OPENAI_API_KEY",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-4.1",
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "timeout_seconds": 120.0,
            },
            "anthropic": {
                "api_key": None,
                "api_key_env_var": "ANTHROPIC_API_KEY",
                "base_url": "https://api.anthropic.com",
                "model_name": "claude-sonnet-4-6",
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "timeout_seconds": 120.0,
            },
            "deepseek": {
                "api_key": None,
                "api_key_env_var": "DEEPSEEK_API_KEY",
                "base_url": "https://api.deepseek.com/v1",
                "model_name": "deepseek-chat",
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "timeout_seconds": 120.0,
            },
            "llm_models": {
                "deepseek_chat": {"provider": "deepseek", "model_name": "deepseek-chat"},
                "gemini_3_1_flash_lite_preview": {
                    "provider": "gemini",
                    "model_name": "gemini-3.1-flash-lite",
                },
                "gemini_25_flash": {"provider": "gemini", "model_name": "gemini-2.5-flash"},
                "gpt_41_mini": {"provider": "openai", "model_name": "gpt-4.1-mini"},
                "gpt_41": {"provider": "openai", "model_name": "gpt-4.1"},
                "gpt_54": {"provider": "openai", "model_name": "gpt-5.4"},
                "claude_sonnet_46": {"provider": "anthropic", "model_name": "claude-sonnet-4-6"},
            },
            "llm_fallbacks": {
                "s2_infer": ["default_llm", "gemini_25_flash", "gpt_41_mini", "gpt_41"],
                "s2_review": ["default_llm", "gemini_25_flash", "gpt_41", "claude_sonnet_46", "gpt_54"],
                "s3_translate": ["gemini_3_1_flash_lite_preview", "default_llm", "deepseek_chat", "gpt_41"],
                "s5_rewrite": ["gemini_3_1_flash_lite_preview", "default_llm", "deepseek_chat", "gpt_41"],
            },
        },
    )
    monkeypatch.setattr(
        process_module,
        "load_tts_config",
        lambda: TTSConfig(
            api_key="tts-key",
            base_url="https://api.minimaxi.com",
            model="speech-2.8-turbo",
            audio_format="wav",
        ),
    )


def _build_aligned_segments(segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    aligned_segments: list[AlignedSegment] = []
    for segment in segments:
        aligned_audio_path = str(
            _export_silent_wav(
                Path(output_dir) / f"segment_{segment.segment_id:03d}_aligned.wav",
                duration_ms=segment.target_duration_ms,
            ).resolve(strict=False)
        )
        segment.aligned_audio_path = aligned_audio_path
        segment.actual_duration_ms = segment.target_duration_ms
        segment.alignment_ratio = 1.0
        segment.alignment_method = "direct"
        segment.needs_review = False
        segment.tts_input_cn_text = segment.cn_text
        aligned_segments.append(
            AlignedSegment(
                segment_id=segment.segment_id,
                speaker_id=segment.speaker_id,
                display_name=segment.display_name,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                cn_text=segment.cn_text,
                en_text=getattr(segment, "en_text", ""),
                aligned_audio_path=aligned_audio_path,
                actual_duration_ms=segment.target_duration_ms,
                alignment_method="direct",
                needs_review=False,
            )
        )
    return aligned_segments


def _install_single_speaker_pipeline_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reported_duration_ms: int = 5_000,
    actual_duration_ms: int = 2_500,
    video_title: str = "Dan Koe: How to Think",
    description: str = "Dan Koe explains how to think clearly, write online, and build an internet business.",
    inferred_speaker_name: str = "Dan Koe",
    capture: dict[str, object] | None = None,
) -> None:
    _install_config_mocks(monkeypatch)

    class FakeDownloader:
        def download(self, request):
            if capture is not None:
                capture["cookies_from_browser"] = request.cookies_from_browser
                capture["download_max_retries"] = request.max_retries
                capture["download_retry_backoff_seconds"] = request.retry_backoff_seconds
            video_path = _write_video(Path(request.output_dir) / "video" / "original.mp4")
            audio_path = _export_silent_wav(
                Path(request.output_dir) / "audio" / "original.wav",
                duration_ms=actual_duration_ms,
            )
            _write_download_metadata(
                Path(request.output_dir),
                video_path=video_path,
                audio_path=audio_path,
                video_title=video_title,
                duration_ms=reported_duration_ms,
                url=request.url,
                description=description,
            )
            return DownloadResult(
                video_path=str(video_path.resolve(strict=False)),
                audio_path=str(audio_path.resolve(strict=False)),
                video_title=video_title,
                duration_ms=reported_duration_ms,
                url=request.url,
                description=description,
            )

    class FakeAssemblyAITranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(
            self,
            audio_path: str,
            output_dir: str,
            speaker_labels: bool = False,
            speakers_expected: int | None = None,
        ) -> TranscriptResult:
            if capture is not None:
                capture["transcribe_audio_path"] = audio_path
                capture["speaker_labels"] = speaker_labels
                capture["speakers_expected"] = speakers_expected
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            return TranscriptResult(
                lines=_make_single_speaker_lines(),
                total_duration_ms=2_000,
                language="en",
                raw_response_path=str(Path(output_dir) / "raw_assemblyai.json"),
                structured_transcript_path=str(Path(output_dir) / "transcript.json"),
            )

    class FakeGeminiTranslator:
        def __init__(
            self,
            api_key: str,
            model_name: str,
            temperature: float,
            max_output_tokens: int,
            sdk_backend: str = "google-genai",
            llm_router=None,
        ):
            assert api_key == "gemini-key"
            assert model_name == "gemini-3.1-pro-preview"
            assert temperature == 0.3
            assert max_output_tokens == 8192
            assert sdk_backend == "google-genai"
            assert llm_router is not None

        def infer_speaker_names(
            self,
            lines,
            num_speakers: int = 2,
            *,
            video_title: str = "",
            youtube_url: str = "",
            video_description: str = "",
        ):
            del lines
            assert num_speakers == 1
            if capture is not None:
                capture["infer_num_speakers"] = num_speakers
                capture["infer_video_title"] = video_title
                capture["infer_youtube_url"] = youtube_url
                capture["infer_video_description"] = video_description
            return {"speaker_a": inferred_speaker_name}

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
            chars_per_second: float | None = None,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ) -> TranslationResult:
            del lines, max_segment_duration_ms, voice_id_b, display_name_b, glossary, speaker_voices
            if capture is not None:
                capture["video_title"] = video_title
                capture["youtube_url"] = youtube_url
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            segments = _make_single_speaker_segments()
            for segment in segments:
                segment.voice_id = voice_id
                segment.display_name = display_name
            return TranslationResult(
                segments=segments,
                total_segments=len(segments),
                output_path=str(Path(output_dir) / "segments.json"),
            )

    class FakeTTSGenerator:
        def __init__(self, config, job_record=None):
            assert config.api_key == "tts-key"
            del job_record

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            results: list[TTSResult] = []
            for segment in segments:
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.tts_audio_path = str(audio_path.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                segment.alignment_ratio = 1.0
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            return _build_aligned_segments(segments, output_dir)

    monkeypatch.setattr(process_module, "YouTubeDownloader", FakeDownloader)
    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FakeAssemblyAITranscriber)
    monkeypatch.setattr(process_module, "GeminiTranslator", FakeGeminiTranslator)
    monkeypatch.setattr(process_module, "TTSGenerator", FakeTTSGenerator)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)


def test_process_pipeline_passes_configured_browser_cookies_to_downloader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, object] = {}
    _install_single_speaker_pipeline_mocks(monkeypatch, capture=capture)
    monkeypatch.setattr(
        process_module,
        "load_youtube_download_config",
        lambda: {
            "cookies_from_browser": "chrome",
            "max_retries": 4,
            "retry_backoff_seconds": 2.5,
        },
    )

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=with-cookies",
            voice_a="voice_demo_001",
            project_dir=str(tmp_path / "project_with_cookies"),
        )
    )

    assert capture["cookies_from_browser"] == "chrome"
    assert capture["download_max_retries"] == 4
    assert capture["download_retry_backoff_seconds"] == 2.5


def _install_dual_speaker_pipeline_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reviewed_lines: list[TranscriptLine] | None = None,
    source_lines: list[TranscriptLine] | None = None,
    expected_speaker_labels: bool = True,
    expected_speakers_expected: int | None = 2,
) -> dict[str, object]:
    _install_config_mocks(monkeypatch)
    capture: dict[str, object] = {
        "review_called": 0,
        "translate_input_speaker_ids": [],
        "observed_voice_ids": [],
    }

    class FakeDownloader:
        def download(self, request):
            video_path = _write_video(Path(request.output_dir) / "video" / "original.mp4")
            audio_path = _export_silent_wav(
                Path(request.output_dir) / "audio" / "original.wav",
                duration_ms=24_000,
            )
            _write_download_metadata(
                Path(request.output_dir),
                video_path=video_path,
                audio_path=audio_path,
                video_title="Interview Demo",
                duration_ms=24_000,
                url=request.url,
            )
            return DownloadResult(
                video_path=str(video_path.resolve(strict=False)),
                audio_path=str(audio_path.resolve(strict=False)),
                video_title="Interview Demo",
                duration_ms=24_000,
                url=request.url,
            )

    class FakeAssemblyAITranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(
            self,
            audio_path: str,
            output_dir: str,
            speaker_labels: bool = False,
            speakers_expected: int | None = None,
        ) -> TranscriptResult:
            del audio_path
            assert speaker_labels is expected_speaker_labels
            assert speakers_expected == expected_speakers_expected
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            return TranscriptResult(
                lines=source_lines or _make_dual_speaker_lines(),
                total_duration_ms=24_000,
                language="en",
                raw_response_path=str(Path(output_dir) / "raw_assemblyai.json"),
                structured_transcript_path=str(Path(output_dir) / "transcript.json"),
            )

    class FakeGeminiTranslator:
        def __init__(
            self,
            api_key: str,
            model_name: str,
            temperature: float,
            max_output_tokens: int,
            sdk_backend: str = "google-genai",
            llm_router=None,
        ):
            del api_key, model_name, temperature, max_output_tokens, sdk_backend
            assert llm_router is not None

        def infer_speaker_names(
            self,
            lines,
            num_speakers: int = 2,
            *,
            video_title: str = "",
            youtube_url: str = "",
            video_description: str = "",
        ):
            del lines
            assert num_speakers == 2
            del video_title, youtube_url, video_description
            return {"speaker_a": "Host", "speaker_b": "Guest"}

        def review_speaker_labels(
            self,
            lines,
            speaker_names,
            video_title: str = "",
            youtube_url: str = "",
        ):
            del speaker_names, video_title, youtube_url
            capture["review_called"] = int(capture["review_called"]) + 1
            return reviewed_lines or lines

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
            chars_per_second: float | None = None,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ) -> TranslationResult:
            del max_segment_duration_ms, video_title, youtube_url, glossary, speaker_voices
            capture["translate_input_speaker_ids"] = [line.speaker_id for line in lines]
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            segments = _make_dual_speaker_segments(
                voice_a=voice_id,
                voice_b=voice_id_b or "",
                display_name_a=display_name,
                display_name_b=display_name_b or "Guest",
            )
            return TranslationResult(
                segments=segments,
                total_segments=len(segments),
                output_path=str(Path(output_dir) / "segments.json"),
            )

    class FakeTTSGenerator:
        def __init__(self, config, job_record=None):
            assert config.api_key == "tts-key"
            del job_record

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            results: list[TTSResult] = []
            for segment in segments:
                capture["observed_voice_ids"].append(segment.voice_id)
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.tts_audio_path = str(audio_path.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                segment.alignment_ratio = 1.0
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            return _build_aligned_segments(segments, output_dir)

    monkeypatch.setattr(process_module, "YouTubeDownloader", FakeDownloader)
    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FakeAssemblyAITranscriber)
    monkeypatch.setattr(process_module, "GeminiTranslator", FakeGeminiTranslator)
    monkeypatch.setattr(process_module, "TTSGenerator", FakeTTSGenerator)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)
    return capture


def test_process_pipeline_runs_end_to_end_with_mocked_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    _install_single_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=demo",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert Path(result.project_dir).exists()
    assert Path(result.dubbed_audio_path).exists()
    assert Path(result.subtitles_path).exists()
    assert Path(result.segments_dir).exists()
    assert Path(result.alignment_report_path).exists()
    assert Path(result.background_sounds_path).exists()
    assert len(list(Path(result.segments_dir).rglob("*.wav"))) == 2
    assert result.total_segments == 2
    assert result.needs_review_count == 0
    manifest_path = Path(result.project_dir) / "manifest.json"
    assert manifest_path.exists()
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["requested_targets"] == ["editor"]
    assert manifest_payload["primary_outputs"]["editor"]["subtitles_path"] == result.subtitles_path
    assert manifest_payload["artifact_index"]["editor.dubbed_audio_complete"] == result.dubbed_audio_path
    assert manifest_payload["artifact_index"]["source.original_audio"].endswith("audio\\original.wav")
    assert manifest_payload["artifact_index"]["source.download_metadata"].endswith("download_metadata.json")
    assert manifest_payload["artifact_index"]["media.transcript_raw"].endswith("transcript\\raw_assemblyai.json")
    assert manifest_payload["artifact_index"]["media.transcript_structured"].endswith("transcript\\transcript.json")
    assert manifest_payload["artifact_index"]["translation.segments"].endswith("translation\\segments.json")
    assert manifest_payload["artifact_index"]["state.project"].endswith("project_state.json")
    assert "state.review" not in manifest_payload["artifact_index"]
    project_state = json.loads((Path(result.project_dir) / "project_state.json").read_text(encoding="utf-8"))
    assert project_state["project_id"] == Path(result.project_dir).name
    assert project_state["stages"]["ingestion"]["status"] == "done"
    assert project_state["stages"]["audio_preparation"]["status"] == "done"
    assert project_state["stages"]["media_understanding"]["status"] == "done"
    assert project_state["stages"]["translation"]["status"] == "done"
    assert project_state["stages"]["alignment"]["status"] == "done"
    assert project_state["stages"]["legacy_process_output"]["status"] == "done"
    assert (
        project_state["stages"]["legacy_process_output"]["payload"]["manifest_path"]
        == str(manifest_path.resolve(strict=False))
    )
    dubbed_audio = AudioSegment.from_wav(result.dubbed_audio_path)
    assert abs(len(dubbed_audio) - 2_500) <= 10


def test_process_pipeline_uses_output_bundle_as_legacy_output_truth_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_output_bundle_truth"
    custom_manifest_path = (project_dir / "bundle" / "custom_manifest.json").resolve(strict=False)
    captured: dict[str, object] = {}

    class FakeOutputDispatcher:
        def dispatch(self, localized_project, artifact_index, request) -> OutputBundleResult:
            del artifact_index
            captured["requested_targets"] = [target.value for target in request.expanded_targets()]
            captured["stage_snapshot"] = dict(localized_project.stage_snapshot)
            output_dir = (project_dir / "fake_output").resolve(strict=False)
            segments_dir = output_dir / "segments"
            segments_dir.mkdir(parents=True, exist_ok=True)
            dubbed_audio_path = _export_silent_wav(
                output_dir / "dubbed_audio_complete.wav",
                duration_ms=2_500,
            )
            ambient_audio_path = _export_silent_wav(
                output_dir / "ambient.wav",
                duration_ms=2_500,
            )
            subtitles_path = output_dir / "subtitles.srt"
            subtitles_path.write_text("", encoding="utf-8")
            background_sounds_path = output_dir / "background_sounds.txt"
            background_sounds_path.write_text("[]", encoding="utf-8")
            alignment_report_path = output_dir / "alignment_report.json"
            alignment_report_path.write_text("{}", encoding="utf-8")
            custom_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            custom_manifest_path.write_text("{}", encoding="utf-8")
            return OutputBundleResult(
                editor_result=ProjectOutputResult(
                    dubbed_audio_path=str(dubbed_audio_path.resolve(strict=False)),
                    ambient_audio_path=str(ambient_audio_path.resolve(strict=False)),
                    segments_dir=str(segments_dir.resolve(strict=False)),
                    segment_count=2,
                    subtitles_path=str(subtitles_path.resolve(strict=False)),
                    subtitles_en_path=str(subtitles_path.resolve(strict=False)),
                    subtitles_bilingual_path=str(subtitles_path.resolve(strict=False)),
                    background_sounds_path=str(background_sounds_path.resolve(strict=False)),
                    alignment_report_path=str(alignment_report_path.resolve(strict=False)),
                    needs_review_count=0,
                ),
                manifest_path=str(custom_manifest_path),
            )

    monkeypatch.setattr(process_module, "OutputDispatcher", FakeOutputDispatcher)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=bundle-truth",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    project_state = json.loads((project_dir / "project_state.json").read_text(encoding="utf-8"))
    legacy_output_payload = project_state["stages"]["legacy_process_output"]["payload"]

    assert result.dubbed_audio_path.endswith("dubbed_audio_complete.wav")
    assert legacy_output_payload["manifest_path"] == str(custom_manifest_path)
    assert captured["requested_targets"] == ["editor"]
    stage_snapshot = captured["stage_snapshot"]
    assert isinstance(stage_snapshot, dict)
    assert "ingestion" in stage_snapshot
    assert "alignment" in stage_snapshot
    assert "legacy_process_output" in stage_snapshot
    assert stage_snapshot["legacy_process_output"]["status"] == "running"


def test_process_pipeline_builds_canonical_bridge_via_shared_project_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_shared_builder_bridge"
    captured: dict[str, object] = {}
    real_build_canonical_source_info = process_module.build_canonical_source_info
    real_build_core_media_artifact_entries = process_module.build_core_media_artifact_entries

    def recording_build_canonical_source_info(**kwargs):
        captured["source_info_helper_kwargs"] = dict(kwargs)
        return real_build_canonical_source_info(**kwargs)

    def recording_build_core_media_artifact_entries(**kwargs):
        captured["artifact_helper_kwargs"] = dict(kwargs)
        return real_build_core_media_artifact_entries(**kwargs)

    monkeypatch.setattr(
        process_module,
        "build_canonical_source_info",
        recording_build_canonical_source_info,
    )
    monkeypatch.setattr(
        process_module,
        "build_core_media_artifact_entries",
        recording_build_core_media_artifact_entries,
    )

    class RecordingProjectBuilder(ProjectBuilder):
        def build_artifact_index(self, artifact_entries) -> object:
            entries = list(artifact_entries.items()) if isinstance(artifact_entries, dict) else list(artifact_entries)
            captured["artifact_entries"] = entries
            return super().build_artifact_index(entries)

        def build_result(self, **kwargs) -> WorkflowBuildResult:
            captured["source_info"] = dict(kwargs["source_info"])
            captured["stage_snapshot"] = dict(kwargs["stage_snapshot"])
            captured["stage_outputs"] = dict(kwargs["stage_outputs"] or {})
            return super().build_result(**kwargs)

    pipeline = ProcessPipeline(project_builder=RecordingProjectBuilder())
    result = pipeline.run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=shared-builder",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    artifact_entries = captured["artifact_entries"]
    assert isinstance(artifact_entries, list)
    artifact_keys = [key for key, _value in artifact_entries]
    assert "source.original_audio" in artifact_keys
    assert "working.speech_for_asr" in artifact_keys
    assert "translation.segments" in artifact_keys
    source_info = captured["source_info"]
    assert source_info["source_kind"] == "youtube_url"
    assert source_info["locator"] == "https://youtube.example/watch?v=shared-builder"
    assert captured["source_info_helper_kwargs"]["source_kind"] == "youtube_url"
    assert captured["artifact_helper_kwargs"]["translation_segments"].endswith("translation\\segments.json")
    stage_outputs = captured["stage_outputs"]
    assert set(stage_outputs.keys()) == {"semantic_blocks", "aligned_blocks", "captions"}
    assert len(stage_outputs["captions"]) == result.total_segments
    stage_snapshot = captured["stage_snapshot"]
    assert "ingestion" in stage_snapshot
    assert "legacy_process_output" in stage_snapshot


def test_process_pipeline_transcribes_with_speech_stem_and_exports_ambient_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, object] = {}
    _install_single_speaker_pipeline_mocks(monkeypatch, capture=capture)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=stems-demo",
            voice_a="voice_demo_001",
            project_dir=str(tmp_path / "project_stems"),
        )
    )

    assert str(capture["transcribe_audio_path"]).endswith("speech_for_asr.wav")
    assert Path(capture["transcribe_audio_path"]).exists()
    assert Path(result.ambient_audio_path).exists()

    metadata = json.loads((Path(result.project_dir) / "download_metadata.json").read_text(encoding="utf-8"))
    assert Path(metadata["speech_audio_path"]).name == "speech_for_asr.wav"
    assert Path(metadata["ambient_audio_path"]).name == "ambient.wav"


def test_process_pipeline_passes_video_context_to_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, object] = {}
    _install_single_speaker_pipeline_mocks(
        monkeypatch,
        video_title="Prompt Context Demo",
        capture=capture,
    )

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=context-demo",
            voice_a="voice_demo_001",
            project_dir=str(tmp_path / "project_context"),
        )
    )

    assert capture["video_title"] == "Prompt Context Demo"
    assert capture["youtube_url"] == "https://youtube.example/watch?v=context-demo"
    assert capture["infer_num_speakers"] == 1
    assert capture["infer_video_title"] == "Prompt Context Demo"
    assert capture["infer_youtube_url"] == "https://youtube.example/watch?v=context-demo"
    assert capture["infer_video_description"] == (
        "Dan Koe explains how to think clearly, write online, and build an internet business."
    )


def test_process_pipeline_auto_generates_project_dir_from_video_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
    _install_single_speaker_pipeline_mocks(
        monkeypatch,
        reported_duration_ms=2_000,
        actual_duration_ms=2_000,
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=demo",
            voice_a="voice_demo_001",
        )
    )

    assert Path(result.project_dir).name == "dan_koe_how_to_think"
    assert str(Path(result.project_dir).parent) == str((tmp_path / "projects").resolve(strict=False))
    metadata = json.loads((Path(result.project_dir) / "download_metadata.json").read_text(encoding="utf-8"))
    expected_project_dir = Path(result.project_dir).resolve(strict=False)
    assert metadata["video_path"] == str((expected_project_dir / "video" / "original.mp4").resolve(strict=False))
    assert metadata["audio_path"] == str((expected_project_dir / "audio" / "original.wav").resolve(strict=False))
    assert metadata["description"] == (
        "Dan Koe explains how to think clearly, write online, and build an internet business."
    )


def test_process_pipeline_auto_project_dir_uses_configured_projects_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_projects_root = tmp_path / "mounted_projects"
    monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(custom_projects_root))
    _install_single_speaker_pipeline_mocks(
        monkeypatch,
        reported_duration_ms=2_000,
        actual_duration_ms=2_000,
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=custom-project-root",
            voice_a="voice_demo_001",
        )
    )

    assert Path(result.project_dir).name == "dan_koe_how_to_think"
    assert Path(result.project_dir).parent == custom_projects_root.resolve(strict=False)
    assert (custom_projects_root / "dan_koe_how_to_think").exists()


def test_process_pipeline_auto_mode_detects_single_speaker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture: dict[str, object] = {}
    _install_single_speaker_pipeline_mocks(monkeypatch, capture=capture)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=auto-single",
            voice_a="voice_demo_001",
            speakers="auto",
            project_dir=str(tmp_path / "project_auto_single"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert capture["speaker_labels"] is True
    assert capture["speakers_expected"] is None


def test_process_pipeline_auto_mode_detects_two_speakers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _install_dual_speaker_pipeline_mocks(
        monkeypatch,
        expected_speakers_expected=None,
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=auto-dual",
            voice_a="voice_a_001",
            voice_b="voice_b_001",
            speakers="auto",
            project_dir=str(tmp_path / "project_auto_dual"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert capture["review_called"] == 1
    assert capture["translate_input_speaker_ids"] == ["speaker_a", "speaker_b", "speaker_a"]


def test_process_pipeline_auto_mode_supports_three_speakers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-detection with 3 speakers should succeed (1-10 supported)."""
    capture = _install_dual_speaker_pipeline_mocks(
        monkeypatch,
        source_lines=_make_three_speaker_lines(),
        expected_speakers_expected=None,
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=auto-three",
            voice_a="voice_a_001",
            speakers="auto",
            project_dir=str(tmp_path / "project_auto_three"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    # review_speaker_labels no longer called (S2 three-pass handles speaker review)
    assert capture["translate_input_speaker_ids"] == ["speaker_a", "speaker_b", "speaker_c"]


def test_process_pipeline_normalize_speakers_supports_auto_and_numeric_values() -> None:
    pipeline = ProcessPipeline()

    assert pipeline._normalize_speakers(1) == 1
    assert pipeline._normalize_speakers(2) == 2
    assert pipeline._normalize_speakers("1") == 1
    assert pipeline._normalize_speakers("2") == 2
    assert pipeline._normalize_speakers("auto") == "auto"
    assert pipeline._normalize_speakers("AUTO") == "auto"

    assert pipeline._normalize_speakers(3) == 3
    assert pipeline._normalize_speakers(10) == 10

    with pytest.raises(ValueError):
        pipeline._normalize_speakers(11)

    with pytest.raises(ValueError):
        pipeline._normalize_speakers("many")


def test_process_pipeline_skips_download_when_explicit_project_has_cached_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_download"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Demo",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=cached",
    )
    observed = {"download_called": 0}

    class FailDownloader:
        def download(self, request):
            del request
            observed["download_called"] += 1
            raise AssertionError("download should not be called when cached media exists")

    monkeypatch.setattr(process_module, "YouTubeDownloader", FailDownloader)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=cached",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert observed["download_called"] == 0
    assert Path(result.project_dir) == project_dir.resolve(strict=False)


def test_process_pipeline_does_not_reuse_existing_project_by_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same URL without explicit project_dir must create a fresh workspace, not reuse old one."""
    monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
    _install_single_speaker_pipeline_mocks(monkeypatch)
    existing_project_dir = tmp_path / "projects" / "existing_cached_project"
    video_path = _write_video(existing_project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(existing_project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        existing_project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Discovery",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=found",
    )
    observed: dict[str, object] = {"download_called": 0}

    class TrackingDownloader:
        def download(self, request):
            observed["download_called"] = int(observed["download_called"]) + 1
            observed["output_dir"] = request.output_dir
            video_p = _write_video(Path(request.output_dir) / "video" / "original.mp4")
            audio_p = _export_silent_wav(Path(request.output_dir) / "audio" / "original.wav", duration_ms=2_500)
            _write_download_metadata(
                Path(request.output_dir),
                video_path=video_p, audio_path=audio_p,
                video_title="Cached Discovery", duration_ms=2_500,
                url=request.url,
            )
            return DownloadResult(
                video_path=str(video_p.resolve(strict=False)),
                audio_path=str(audio_p.resolve(strict=False)),
                video_title="Cached Discovery", duration_ms=2_500,
                url=request.url,
            )

    monkeypatch.setattr(process_module, "YouTubeDownloader", TrackingDownloader)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=found",
            voice_a="voice_demo_001",
        )
    )

    # Must download (not skip), because URL reuse is disabled
    assert observed["download_called"] == 1
    # Must NOT be in the old project dir
    assert Path(result.project_dir).resolve(strict=False) != existing_project_dir.resolve(strict=False)


def test_process_pipeline_new_task_does_not_reuse_slug_dir_with_same_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When slug dir already exists with same URL, new task keeps its own temp dir."""
    monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
    _install_single_speaker_pipeline_mocks(monkeypatch)

    # Pre-create a slug dir that would match the download title
    slug_dir = tmp_path / "projects" / "dan_koe_how_to_think"
    slug_dir.mkdir(parents=True, exist_ok=True)
    _write_download_metadata(
        slug_dir,
        video_path=slug_dir / "video" / "original.mp4",
        audio_path=slug_dir / "audio" / "original.wav",
        video_title="Dan Koe: How to Think",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=slug-conflict",
    )
    observed: dict[str, object] = {}

    class TrackingDownloader:
        def download(self, request):
            observed["output_dir"] = str(Path(request.output_dir).resolve(strict=False))
            video_path = _write_video(Path(request.output_dir) / "video" / "original.mp4")
            audio_path = _export_silent_wav(
                Path(request.output_dir) / "audio" / "original.wav",
                duration_ms=2_500,
            )
            _write_download_metadata(
                Path(request.output_dir),
                video_path=video_path,
                audio_path=audio_path,
                video_title="Dan Koe: How to Think",
                duration_ms=2_500,
                url=request.url,
            )
            return DownloadResult(
                video_path=str(video_path.resolve(strict=False)),
                audio_path=str(audio_path.resolve(strict=False)),
                video_title="Dan Koe: How to Think",
                duration_ms=2_500,
                url=request.url,
            )

    monkeypatch.setattr(process_module, "YouTubeDownloader", TrackingDownloader)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=slug-conflict",
            voice_a="voice_demo_001",
        )
    )

    # Result should NOT be in the pre-existing slug dir
    assert Path(result.project_dir).resolve(strict=False) != slug_dir.resolve(strict=False)
    # Should be in its own temp dir (starts with _process_)
    result_dir_name = Path(result.project_dir).name
    assert result_dir_name.startswith("_process_")


def test_process_pipeline_skips_transcription_when_transcript_cache_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_transcript"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Transcript",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=transcript",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    observed = {"transcribe_called": 0}

    class FailTranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(self, *args, **kwargs):
            del args, kwargs
            observed["transcribe_called"] += 1
            raise AssertionError("transcribe should not be called when transcript cache exists")

    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FailTranscriber)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=transcript",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert observed["transcribe_called"] == 0


def test_process_pipeline_accepts_cached_transcript_with_speaker_name_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_transcript_speaker_name"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Transcript With Speaker Name",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=transcript-speaker-name",
    )
    transcript_path = _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_payload["lines"][0]["speaker_name"] = "史蒂夫乔布斯"
    transcript_path.write_text(
        json.dumps(transcript_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    observed = {"transcribe_called": 0}

    class FailTranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(self, *args, **kwargs):
            del args, kwargs
            observed["transcribe_called"] += 1
            raise AssertionError("transcribe should not be called when transcript cache exists")

    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FailTranscriber)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=transcript-speaker-name",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert observed["transcribe_called"] == 0


def test_process_pipeline_skips_translation_when_segments_cache_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_translation"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Translation",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=translation",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    _write_segments_cache(project_dir, _make_single_speaker_segments())
    observed = {"translate_called": 0}

    class FailTranslator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def translate(self, *args, **kwargs):
            del args, kwargs
            observed["translate_called"] += 1
            raise AssertionError("translate should not be called when translation cache exists")

    monkeypatch.setattr(process_module, "GeminiTranslator", FailTranslator)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=translation",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert observed["translate_called"] == 0


def test_process_pipeline_preserves_voice_metadata_in_segments_snapshot(tmp_path: Path) -> None:
    pipeline = ProcessPipeline()
    segments_path = tmp_path / "translation" / "segments.json"
    translation_result = TranslationResult(
        segments=[
            DubbingSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Conan O'Brien",
                voice_id="voice_demo_001",
                start_ms=0,
                end_ms=1_000,
                target_duration_ms=1_000,
                source_text="Hello there.",
                tts_input_cn_text="tts snapshot",
                cn_text="你好。",
                voice_description="沉稳低沉的中年男性主持声线",
                gender="male",
                age_group="middle",
                persona_style="serious",
                energy_level="low",
            )
        ],
        total_segments=1,
        output_path=str(segments_path.resolve(strict=False)),
    )

    pipeline._write_segments_snapshot(translation_result)
    cached_result = pipeline._load_translation_result(segments_path)

    assert len(cached_result.segments) == 1
    cached_segment = cached_result.segments[0]
    assert cached_segment.voice_description == "沉稳低沉的中年男性主持声线"
    assert cached_segment.gender == "male"
    assert cached_segment.age_group == "middle"
    assert cached_segment.persona_style == "serious"
    assert cached_segment.energy_level == "low"
    assert cached_segment.tts_input_cn_text == "tts snapshot"


def test_tts_text_audio_sync_publish_backfills_missing_witness() -> None:
    segment = DubbingSegment(
        segment_id=9,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_demo_001",
        start_ms=0,
        end_ms=1_000,
        target_duration_ms=1_000,
        source_text="Hello.",
        cn_text="当前文本",
        tts_audio_path="segment_009.wav",
        rewrite_count=1,
    )

    repairs = process_module._sync_tts_text_audio_for_publish([segment])

    assert repairs == ["segment_9: backfilled missing tts_input"]
    assert segment.cn_text == "当前文本"
    assert segment.tts_input_cn_text == "当前文本"


def test_tts_text_audio_sync_publish_repairs_mismatched_visible_text() -> None:
    segment = DubbingSegment(
        segment_id=10,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_demo_001",
        start_ms=0,
        end_ms=1_000,
        target_duration_ms=1_000,
        source_text="Hello.",
        cn_text="界面文本",
        tts_input_cn_text="实际合成文本",
        tts_audio_path="segment_010.wav",
    )

    repairs = process_module._sync_tts_text_audio_for_publish([segment])

    assert repairs == ["segment_10: cn_text <- tts_input_cn_text"]
    assert segment.cn_text == "实际合成文本"
    assert segment.tts_input_cn_text == "实际合成文本"


def test_tts_generator_regenerates_cache_when_text_witness_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.tts.tts_generator import TTSGenerator
    import services.tts.tts_generator as tts_module

    generator = TTSGenerator(TTSConfig(api_key="test-key"))
    output_root = tmp_path / "tts"
    output_root.mkdir()
    cached_path = output_root / "segment_001_speaker_a.wav"
    cached_path.write_bytes(b"old")
    segment = DubbingSegment(
        segment_id=1,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=1_000,
        target_duration_ms=1_000,
        source_text="hello",
        cn_text="新的合成文本",
        tts_input_cn_text="旧的合成文本",
    )
    calls: list[str] = []

    class _NoopRateLimiter:
        def wait(self) -> None:
            calls.append("wait")

    def _fake_generate_one_with_backoff(segment_arg, output_dir, *, usage_bucket):
        calls.append(usage_bucket)
        output_path = Path(output_dir) / "segment_001_speaker_a.wav"
        output_path.write_bytes(b"new")
        return TTSResult(
            segment_id=segment_arg.segment_id,
            audio_path=str(output_path),
            duration_ms=777,
            voice_id=segment_arg.voice_id,
        )

    monkeypatch.setattr(tts_module, "_ffprobe_duration_ms", lambda path: 123)
    monkeypatch.setattr(generator, "_generate_one_with_backoff", _fake_generate_one_with_backoff)

    result = generator._process_segment(
        segment,
        output_root,
        1,
        1,
        _NoopRateLimiter(),
        usage_bucket="first",
    )

    assert result.duration_ms == 777
    assert calls == ["wait", "first"]
    assert segment.tts_input_cn_text == "新的合成文本"


def test_aligner_reprocesses_when_raw_tts_is_newer_than_aligned_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.alignment.aligner import SegmentAligner

    raw_path = tmp_path / "segment_001_speaker_a.wav"
    aligned_path = tmp_path / "segment_001_aligned.wav"
    raw_path.write_bytes(b"raw")
    aligned_path.write_bytes(b"aligned")
    os.utime(aligned_path, (100, 100))
    os.utime(raw_path, (200, 200))
    segment = DubbingSegment(
        segment_id=1,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=1_000,
        target_duration_ms=1_000,
        source_text="hello",
        cn_text="新的合成文本",
        tts_input_cn_text="新的合成文本",
        tts_audio_path=str(raw_path),
    )
    aligner = SegmentAligner()
    called: list[int] = []

    def _fake_align_one(segment_arg, output_dir, **_kwargs):
        # 2026-05-09 P2-17a-1: accept paid_fallback_semaphore / stop_event
        # kwargs that the parallel path passes via pool.submit. Default
        # AVT_ALIGN_MAX_WORKERS=2 means this stale-cache reprocess test
        # exercises _align_all_parallel, not _align_all_serial.
        called.append(segment_arg.segment_id)
        return AlignedSegment(
            segment_id=segment_arg.segment_id,
            speaker_id=segment_arg.speaker_id,
            display_name=segment_arg.display_name,
            start_ms=segment_arg.start_ms,
            end_ms=segment_arg.end_ms,
            cn_text=segment_arg.cn_text,
            en_text=getattr(segment_arg, "en_text", ""),
            aligned_audio_path=str(aligned_path),
            actual_duration_ms=1_000,
            alignment_method="dsp",
            needs_review=False,
            dubbing_mode="dub",
        )

    monkeypatch.setattr(aligner, "_align_one", _fake_align_one)

    results = aligner.align_all([segment], str(tmp_path))

    assert called == [1]
    assert results[0].alignment_method == "dsp"


def test_process_pipeline_persists_reviewed_voice_metadata_before_tts_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    import src.services.transcript_reviewer as transcript_reviewer_module

    def _fake_review_transcript(lines, **kwargs):
        del kwargs
        return transcript_reviewer_module.ReviewResult(
            speakers={
                "speaker_a": {
                    "name": "Conan O'Brien",
                    "gender": "male",
                    "age_group": "middle",
                    "voice_description": "沉稳低沉的中年男性主持声线",
                    "persona_style": "serious",
                    "energy_level": "low",
                }
            },
            glossary={},
            corrections_applied=0,
            lines=lines,
        )

    monkeypatch.setattr(transcript_reviewer_module, "review_transcript", _fake_review_transcript)
    project_dir = tmp_path / "project_translation_review_voice_metadata"

    class CacheSnapshotTranslator:
        def __init__(
            self,
            api_key: str,
            model_name: str,
            temperature: float,
            max_output_tokens: int,
            sdk_backend: str = "google-genai",
            llm_router=None,
        ):
            del api_key, model_name, temperature, max_output_tokens, sdk_backend
            assert llm_router is not None

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
            chars_per_second: float | None = None,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ) -> TranslationResult:
            del lines, max_segment_duration_ms, voice_id_b, display_name_b, video_title, youtube_url, glossary, speaker_voices
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            segments = _make_single_speaker_segments()
            for segment in segments:
                segment.voice_id = voice_id
                segment.display_name = display_name
            return TranslationResult(
                segments=segments,
                total_segments=len(segments),
                output_path=str(Path(output_dir) / "segments.json"),
            )

    monkeypatch.setattr(process_module, "GeminiTranslator", CacheSnapshotTranslator)

    original_get_approved_review_payload = ProcessPipeline._get_approved_review_payload

    def _fake_get_approved_review_payload(self, review_state_manager, stage: str):
        if stage == process_module.TRANSLATION_CONFIG_REVIEW_STAGE:
            return {
                "selected_model": "gemini_3_1_flash_lite_preview",
                "prompt_template": None,
            }
        return original_get_approved_review_payload(self, review_state_manager, stage)

    monkeypatch.setattr(
        ProcessPipeline,
        "_get_approved_review_payload",
        _fake_get_approved_review_payload,
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=translation-review-voice-metadata",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
            wait_for_review=True,
            job_record={
                "service_mode": "studio",
                "tts_provider": "cosyvoice",
                "requires_review": True,
                "voice_strategy": "preset_mapping",
            },
        )
    )

    assert result.status == "waiting_for_review"
    cached_result = ProcessPipeline()._load_translation_result(project_dir / "translation" / "segments.json")
    cached_segment = cached_result.segments[0]
    assert cached_segment.voice_description == "沉稳低沉的中年男性主持声线"
    assert cached_segment.gender == "male"
    assert cached_segment.age_group == "middle"
    assert cached_segment.persona_style == "serious"
    assert cached_segment.energy_level == "low"


def test_process_pipeline_passes_transcript_dir_to_review_debug_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    import src.services.transcript_reviewer as transcript_reviewer_module

    observed: dict[str, str] = {}

    def _fake_review_transcript(lines, **kwargs):
        observed["debug_output_dir"] = str(Path(kwargs["debug_output_dir"]).resolve(strict=False))
        return transcript_reviewer_module.ReviewResult(
            speakers={},
            glossary={},
            corrections_applied=0,
            lines=lines,
        )

    class _StopAfterReviewTranslator:
        def __init__(
            self,
            api_key: str,
            model_name: str,
            temperature: float,
            max_output_tokens: int,
            sdk_backend: str = "google-genai",
            llm_router=None,
        ):
            del api_key, model_name, temperature, max_output_tokens, sdk_backend, llm_router

        def infer_speaker_names(
            self,
            lines,
            num_speakers: int = 2,
            *,
            video_title: str = "",
            youtube_url: str = "",
            video_description: str = "",
        ):
            del lines, num_speakers, video_title, youtube_url, video_description
            return {"speaker_a": "Dan Koe"}

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
        ):
            del (
                lines,
                output_dir,
                voice_id,
                display_name,
                max_segment_duration_ms,
                voice_id_b,
                display_name_b,
                video_title,
                youtube_url,
                glossary,
                speaker_voices,
            )
            raise RuntimeError("stop after review")

    monkeypatch.setattr(transcript_reviewer_module, "review_transcript", _fake_review_transcript)
    monkeypatch.setattr(process_module, "GeminiTranslator", _StopAfterReviewTranslator)

    project_dir = tmp_path / "project_review_debug_dir"

    with pytest.raises(RuntimeError, match="stop after review"):
        ProcessPipeline().run(
            ProcessConfig(
                youtube_url="https://youtube.example/watch?v=review-debug-dir",
                voice_a="voice_demo_001",
                project_dir=str(project_dir),
            )
        )

    assert observed["debug_output_dir"] == str((project_dir / "transcript").resolve(strict=False))


def test_process_pipeline_reused_project_requires_fresh_translation_config_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused project with cached transcript but no cached translation
    should pause at translation_config_review."""
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_fresh_config_review"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    youtube_url = "https://youtube.example/watch?v=fresh-config-review"
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Fresh Config Review",
        duration_ms=2_500,
        url=youtube_url,
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    # No segments cache — pipeline needs fresh translation → pauses at config review

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url=youtube_url,
            voice_a="voice_demo_001",
            wait_for_review=True,
            project_dir=str(project_dir),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert result.status == "waiting_for_review"
    assert result.paused_review_stage == process_module.TRANSLATION_CONFIG_REVIEW_STAGE
    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    assert review_state["active_stage"] == process_module.TRANSLATION_CONFIG_REVIEW_STAGE
    assert review_state["stages"]["translation_config_review"]["status"] == "pending"


def test_process_pipeline_recovers_missing_voice_metadata_from_cached_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    import src.services.transcript_reviewer as transcript_reviewer_module

    project_dir = tmp_path / "project_cached_translation_voice_metadata"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Translation Voice Metadata",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=cache-voice-metadata",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    _write_segments_cache(project_dir, _make_single_speaker_segments())

    def _fake_review_transcript(lines, **kwargs):
        del kwargs
        return transcript_reviewer_module.ReviewResult(
            speakers={
                "speaker_a": {
                    "name": "Conan O'Brien",
                    "gender": "male",
                    "age_group": "middle",
                    "voice_description": "沉稳低沉的中年男性主持声线",
                    "persona_style": "serious",
                    "energy_level": "low",
                }
            },
            glossary={},
            corrections_applied=0,
            lines=lines,
        )

    monkeypatch.setattr(transcript_reviewer_module, "review_transcript", _fake_review_transcript)
    observed: dict[str, str] = {}

    class CaptureMetadataTTSGenerator:
        def __init__(self, config, job_record=None):
            del job_record
            assert config.api_key == "tts-key"

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            del output_dir
            observed["voice_description"] = segments[0].voice_description
            observed["gender"] = segments[0].gender
            observed["age_group"] = segments[0].age_group
            observed["persona_style"] = segments[0].persona_style
            observed["energy_level"] = segments[0].energy_level
            raise RuntimeError("stop after metadata check")

    monkeypatch.setattr(process_module, "TTSGenerator", CaptureMetadataTTSGenerator)

    with pytest.raises(RuntimeError, match="stop after metadata check"):
        ProcessPipeline().run(
            ProcessConfig(
                youtube_url="https://youtube.example/watch?v=cache-voice-metadata",
                voice_a="voice_demo_001",
                project_dir=str(project_dir),
                job_record={
                    "service_mode": "express",
                    "tts_provider": "cosyvoice",
                    "requires_review": False,
                    "voice_strategy": "preset_mapping",
                },
            )
        )

    assert observed == {
        "voice_description": "沉稳低沉的中年男性主持声线",
        "gender": "male",
        "age_group": "middle",
        "persona_style": "serious",
        "energy_level": "low",
    }


def test_process_pipeline_wait_for_review_writes_state_files_to_final_project_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "PROJECT_ROOT", tmp_path)
    _install_single_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=wait-review-new-project",
            voice_a="voice_demo_001",
            wait_for_review=True,
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    expected_project_dir = (tmp_path / "projects" / "dan_koe_how_to_think").resolve(strict=False)
    assert result.status == "waiting_for_review"
    assert Path(result.project_dir) == expected_project_dir
    review_state_path = expected_project_dir / "review_state.json"
    project_state_path = expected_project_dir / "project_state.json"
    assert review_state_path.exists()
    assert project_state_path.exists()
    assert list((tmp_path / "projects").rglob("review_state.json")) == [review_state_path]
    review_state = json.loads(review_state_path.read_text(encoding="utf-8"))
    # Speaker review is auto-skipped; first pause is translation_config_review
    assert review_state["active_stage"] == process_module.TRANSLATION_CONFIG_REVIEW_STAGE
    project_state = json.loads(project_state_path.read_text(encoding="utf-8"))
    # media_understanding is "running" because pipeline pauses mid-stage at config review
    assert project_state["stages"]["media_understanding"]["status"] in ("done", "running")
    assert "translation" not in project_state["stages"]
    assert "alignment" not in project_state["stages"]


def test_process_pipeline_explicit_project_dir_reuses_approved_translation_review_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_translation_review_resume"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    youtube_url = "https://youtube.example/watch?v=translation-review-resume"
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Translation Resume",
        duration_ms=2_500,
        url=youtube_url,
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    cached_segments = _make_single_speaker_segments()
    _write_segments_cache(project_dir, cached_segments)
    _write_review_state(
        project_dir,
        active_stage=None,
        speaker_status="approved",
        speaker_payload={
            "speaker_names": {"speaker_a": "Dan Koe"},
            "speaker_options": [{"speaker_id": "speaker_a", "display_name": "Dan Koe"}],
            "segment_speakers": {"1": "speaker_a", "2": "speaker_a", "3": "speaker_a"},
            "segment_count": 3,
        },
        translation_status="approved",
        translation_payload={
            "segments": {
                str(segment.segment_id): {
                    "segment_id": segment.segment_id,
                    "cn_text": segment.cn_text,
                    "cn_text": segment.cn_text,
                }
                for segment in cached_segments
            },
            "segment_count": len(cached_segments),
        },
    )

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url=youtube_url,
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
            wait_for_review=True,
        )
    )

    assert result.status == "completed"
    assert Path(result.dubbed_audio_path).exists()
    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    assert review_state["stages"]["translation_review"]["status"] == "approved"


def test_process_pipeline_does_not_treat_translation_checkpoint_as_complete_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_translation_checkpoint_only"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Checkpoint Only",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=checkpoint-only",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    checkpoint_path = project_dir / "translation" / "segments.checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 1,
                "input_fingerprint": "partial",
                "translated_items": [{"segment_id": 1, "cn_text": "旧翻译"}],
                "completed_batches": 1,
                "total_groups": 2,
                "updated_at": "2026-03-16T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    observed = {"translate_called": 0}

    class ResumeTranslator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
            chars_per_second: float | None = None,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ) -> TranslationResult:
            del lines, max_segment_duration_ms, voice_id_b, display_name_b, video_title, youtube_url, glossary, speaker_voices
            observed["translate_called"] += 1
            segments = _make_single_speaker_segments()
            for segment in segments:
                segment.voice_id = voice_id
                segment.display_name = display_name
            return TranslationResult(
                segments=segments,
                total_segments=len(segments),
                output_path=str(Path(output_dir) / "segments.json"),
            )

    monkeypatch.setattr(process_module, "GeminiTranslator", ResumeTranslator)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=checkpoint-only",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    assert observed["translate_called"] == 1


def test_process_pipeline_runs_two_speaker_mode_with_different_voice_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            voice_b="voice_b_001",
            speakers=2,
            project_dir=str(tmp_path / "project_dual"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert capture["observed_voice_ids"] == ["voice_a_001", "voice_b_001"]


def test_process_pipeline_overrides_cached_voice_ids_for_translation_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_voice_override"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_000)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Voices",
        duration_ms=2_000,
        url="https://youtube.example/watch?v=voice-cache",
    )
    _write_transcript_cache(project_dir, _make_dual_speaker_lines(), total_duration_ms=24_000)
    cached_segments = _make_dual_speaker_segments(
        voice_a="old_voice_a",
        voice_b="old_voice_b",
        display_name_a="Old Host",
        display_name_b="Old Guest",
    )
    _write_segments_cache(project_dir, cached_segments)
    observed: dict[str, object] = {"voice_ids": []}

    class FailDownloader:
        def download(self, request):
            del request
            raise AssertionError("download should not be called")

    class FailTranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("transcribe should not be called")

    class CacheAwareTranslator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

    class CaptureTTSGenerator:
        def __init__(self, config, **kwargs):
            assert config.api_key == "tts-key"

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            observed["voice_ids"] = [segment.voice_id for segment in segments]
            results: list[TTSResult] = []
            for segment in segments:
                audio_file = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.tts_audio_path = str(audio_file.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_file.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            return _build_aligned_segments(segments, output_dir)

    monkeypatch.setattr(process_module, "YouTubeDownloader", FailDownloader)
    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FailTranscriber)
    monkeypatch.setattr(process_module, "GeminiTranslator", CacheAwareTranslator)
    monkeypatch.setattr(process_module, "TTSGenerator", CaptureTTSGenerator)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=voice-cache",
            voice_a="new_voice_a",
            voice_b="new_voice_b",
            speakers=2,
            project_dir=str(project_dir),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert observed["voice_ids"] == ["new_voice_a", "new_voice_b"]


def test_process_pipeline_uses_voice_registry_lookup_when_voice_b_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: voice_b not provided → skips registry/clone, downstream auto-matches."""
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Express mode should not call lookup_voice_ids")

    monkeypatch.setattr(process_module, "lookup_voice_ids", unexpected_lookup)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            speakers=2,
            project_dir=str(tmp_path / "project_lookup"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    # voice_a explicit, voice_b auto-matched (None or empty)
    assert capture["observed_voice_ids"][0] == "voice_a_001"
    assert not capture["observed_voice_ids"][1]  # None or ""


def test_process_pipeline_uses_voice_registry_lookup_when_voice_a_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: voice_a not provided → skips registry/clone, downstream auto-matches."""
    _install_single_speaker_pipeline_mocks(monkeypatch)

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Express mode should not call lookup_voice_ids")

    monkeypatch.setattr(process_module, "lookup_voice_ids", unexpected_lookup)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-registry",
            voice_a=None,
            speaker_a_name="Narrator",
            project_dir=str(tmp_path / "project_lookup_a"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_uses_inferred_single_speaker_name_for_voice_registry_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: inferred speaker name available but voice resolution skipped entirely."""
    _install_single_speaker_pipeline_mocks(monkeypatch)

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Express mode should not call lookup_voice_ids")

    monkeypatch.setattr(process_module, "lookup_voice_ids", unexpected_lookup)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-registry-inferred-name",
            voice_a=None,
            project_dir=str(tmp_path / "project_lookup_a_inferred"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_single_speaker_default_placeholder_skips_generic_registry_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: placeholder name → entire voice resolution skipped, no registry, no clone."""
    _install_single_speaker_pipeline_mocks(monkeypatch, inferred_speaker_name="Speaker A")

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Express mode should not call lookup_voice_ids")

    class FailAutoVoiceCloner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Express mode should not create AutoVoiceCloner")

    monkeypatch.setattr(process_module, "lookup_voice_ids", unexpected_lookup)
    monkeypatch.setattr(process_module, "AutoVoiceCloner", FailAutoVoiceCloner)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-placeholder-auto-clone",
            voice_a=None,
            project_dir=str(tmp_path / "project_placeholder_auto_clone_a"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_auto_clones_voice_a_when_missing_in_single_speaker_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: voice_a=None → skips clone entirely, downstream auto-matches."""
    _install_single_speaker_pipeline_mocks(monkeypatch)

    class FailAutoVoiceCloner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Express mode should not create AutoVoiceCloner")

    monkeypatch.setattr(process_module, "AutoVoiceCloner", FailAutoVoiceCloner)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-auto-clone",
            voice_a=None,
            project_dir=str(tmp_path / "project_auto_clone_a"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_auto_clones_voice_b_when_registry_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: voice_b=None → skips clone, voice_a explicit still used."""
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    class FailAutoVoiceCloner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Express mode should not create AutoVoiceCloner")

    monkeypatch.setattr(process_module, "AutoVoiceCloner", FailAutoVoiceCloner)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            speakers=2,
            project_dir=str(tmp_path / "project_auto_clone"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert capture["observed_voice_ids"][0] == "voice_a_001"
    assert not capture["observed_voice_ids"][1]  # None or ""


def test_process_pipeline_auto_clones_both_voices_when_both_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: both voices=None → skips clone, both auto-matched."""
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    class FailAutoVoiceCloner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Express mode should not create AutoVoiceCloner")

    monkeypatch.setattr(process_module, "AutoVoiceCloner", FailAutoVoiceCloner)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual-auto-clone",
            voice_a=None,
            voice_b=None,
            speakers=2,
            project_dir=str(tmp_path / "project_auto_clone_both"),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert not capture["observed_voice_ids"][0]  # None or ""
    assert not capture["observed_voice_ids"][1]  # None or ""


def test_process_pipeline_raises_when_auto_clone_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: no clone attempt → pipeline completes even with missing voice_b."""
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            speakers=2,
            project_dir=str(tmp_path / "project_auto_clone_fail"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()
    assert capture["observed_voice_ids"][0] == "voice_a_001"
    assert not capture["observed_voice_ids"][1]  # None or ""


def test_process_pipeline_raises_when_speaker_a_auto_clone_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: no clone attempt → pipeline completes even with missing voice_a."""
    _install_single_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-auto-clone-fail",
            voice_a=None,
            project_dir=str(tmp_path / "project_auto_clone_a_fail"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_raises_clear_error_before_clone_when_sample_is_too_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express mode: no clone attempt → pipeline completes regardless of sample quality."""
    _install_single_speaker_pipeline_mocks(monkeypatch)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-auto-clone-short-sample",
            voice_a=None,
            project_dir=str(tmp_path / "project_auto_clone_short_sample"),
        )
    )

    assert Path(result.dubbed_audio_path).exists()


def test_process_pipeline_wait_for_review_pauses_for_voice_review_when_sample_is_too_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_auto_clone_short_sample_review"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Dan Koe: How to Think",
        duration_ms=5_000,
        url="https://youtube.example/watch?v=single-auto-clone-short-sample-review",
    )
    _write_review_state(
        project_dir,
        active_stage=None,
        speaker_status="approved",
        speaker_payload={
            "speaker_names": {"speaker_a": "Dan Koe"},
            "speaker_options": [{"speaker_id": "speaker_a", "display_name": "Dan Koe"}],
            "segment_speakers": {"1": "speaker_a", "2": "speaker_a", "3": "speaker_a"},
            "segment_count": 3,
        },
        translation_status="skipped",
        translation_payload={},
        approved_at="2026-03-18T03:50:49.223596+00:00",
    )
    monkeypatch.setattr(
        process_module,
        "lookup_voice_ids",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            VoiceLookupError(
                "Missing voice_id for speaker_a (Speaker A). Pass --voice-a or register this speaker in voice_registry.json."
            )
        ),
    )

    class FakeVoiceSampleExtractor:
        def extract_sample(
            self,
            audio_path: str,
            speaker_lines,
            output_path: str,
            min_duration_s: float = 10.0,
            max_duration_s: float = 300.0,
        ) -> str:
            del audio_path, speaker_lines, min_duration_s, max_duration_s
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            _export_silent_wav(Path(output_path), duration_ms=6_900)
            return output_path

        def validate_sample(self, sample_path: str) -> dict:
            del sample_path
            return {
                "duration_s": 6.9,
                "rms_dbfs": -35.7,
                "silence_ratio": 0.38,
                "is_valid": False,
                "warnings": ["样本时长不足10秒", "静音占比超过30%"],
            }

    class FakeAutoVoiceCloner:
        def __init__(self, api_key: str, base_url: str = "https://api.minimaxi.com"):
            del api_key, base_url

        def clone_voice(self, sample_path: str, speaker_name: str) -> str:
            raise AssertionError("clone_voice should not be called when sample duration is too short")

    monkeypatch.setattr(process_module, "VoiceSampleExtractor", FakeVoiceSampleExtractor)
    monkeypatch.setattr(process_module, "AutoVoiceCloner", FakeAutoVoiceCloner)

    result = ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=single-auto-clone-short-sample-review",
            voice_a=None,
            project_dir=str(project_dir),
            wait_for_review=True,
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert result.status == "waiting_for_review"
    review_state = json.loads((project_dir / "review_state.json").read_text(encoding="utf-8"))
    assert review_state["active_stage"] == process_module.TRANSLATION_CONFIG_REVIEW_STAGE
    translation_config_review = review_state["stages"]["translation_config_review"]
    assert translation_config_review["status"] == "pending"


def test_process_pipeline_reuses_partial_tts_cache_when_translation_cache_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config_mocks(monkeypatch)
    project_dir = tmp_path / "project_partial_tts_cache"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=9_000)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Partial TTS Cache",
        duration_ms=9_000,
        url="https://youtube.example/watch?v=partial-tts",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    cached_segments = _make_many_single_speaker_segments(9)
    _write_segments_cache(project_dir, cached_segments)
    tts_dir = project_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    for segment in cached_segments[:5]:
        _export_silent_wav(
            tts_dir / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
            duration_ms=segment.target_duration_ms,
        )

    observed = {"generated_count": 0}

    class FailDownloader:
        def download(self, request):
            del request
            raise AssertionError("download should not be called")

    class FailTranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("transcribe should not be called")

    class CacheAwareTranslator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

    class PartialCacheTTSGenerator:
        def __init__(self, config, **kwargs):
            assert config.api_key == "tts-key"

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            observed["generated_count"] = len(segments)
            results: list[TTSResult] = []
            for segment in segments:
                audio_file = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.tts_audio_path = str(audio_file.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_file.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            return _build_aligned_segments(segments, output_dir)

    monkeypatch.setattr(process_module, "YouTubeDownloader", FailDownloader)
    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FailTranscriber)
    monkeypatch.setattr(process_module, "GeminiTranslator", CacheAwareTranslator)
    monkeypatch.setattr(process_module, "TTSGenerator", PartialCacheTTSGenerator)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=partial-tts",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert observed["generated_count"] == 4


def test_process_pipeline_uses_persisted_probe_cps_for_pre_tts_on_translation_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    project_dir = tmp_path / "project_cached_translation_probe_cps"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="Cached Translation Probe CPS",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=cache-probe-cps",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    _write_segments_cache(project_dir, _make_single_speaker_segments())
    speech_audio_path = _export_silent_wav(project_dir / "audio" / "speech_for_asr.wav", duration_ms=2_500)
    ambient_audio_path = _export_silent_wav(project_dir / "audio" / "ambient.wav", duration_ms=2_500)
    (project_dir / "audio" / "probe_calibration.json").write_text(
        json.dumps(
            {
                "version": 1,
                "global_chars_per_second": 7.25,
                "chars_per_second_by_speaker": {"speaker_a": 8.5},
                "speaker_voice_ids": {"speaker_a": "voice_demo_001"},
                "calibrated_at": "2026-04-25T00:00:00+00:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    def fake_ensure_separated_audio_assets(self, *, project_dir, source_audio_path):
        del self, project_dir
        return process_module.AudioSeparationResult(
            source_audio_path=str(source_audio_path),
            speech_audio_path=str(speech_audio_path.resolve(strict=False)),
            ambient_audio_path=str(ambient_audio_path.resolve(strict=False)),
            reused_cache=True,
        )

    monkeypatch.setattr(
        ProcessPipeline,
        "_ensure_separated_audio_assets",
        fake_ensure_separated_audio_assets,
    )
    monkeypatch.setattr(process_module, "_ffprobe_duration_ms", lambda path: 2_500)

    import services.tts.voice_speed_catalog as voice_speed_catalog_module

    monkeypatch.setattr(
        voice_speed_catalog_module,
        "lookup_per_speaker",
        lambda *args, **kwargs: (0.0, {}),
    )

    captured: dict[str, object] = {}

    class CaptureRewriter:
        def __init__(
            self,
            translator,
            chars_per_second: float = 4.5,
            chars_per_second_by_speaker: dict[str, float] | None = None,
            **kwargs,
        ):
            del translator, kwargs
            self.chars_per_second = chars_per_second
            self.chars_per_second_by_speaker = chars_per_second_by_speaker or {}
            captured.setdefault("rewriter_chars_per_second", []).append(chars_per_second)
            captured.setdefault("rewriter_chars_per_second_by_speaker", []).append(
                dict(self.chars_per_second_by_speaker)
            )

    def capture_pre_tts(
        self,
        *,
        segments,
        rewriter,
        chars_per_second,
        chars_per_second_by_speaker,
        job_provider=None,
    ):
        del self, segments, rewriter, job_provider
        captured["pre_tts_chars_per_second"] = chars_per_second
        captured["pre_tts_chars_per_second_by_speaker"] = dict(chars_per_second_by_speaker or {})
        return 0

    monkeypatch.setattr(process_module, "GeminiRewriter", CaptureRewriter)
    monkeypatch.setattr(
        ProcessPipeline,
        "_pre_rewrite_obvious_overshoot_segments_before_tts",
        capture_pre_tts,
    )

    def fake_dispatch_output_bundle(self, *, project_dir, build_result):
        del self, build_result
        output_dir = Path(project_dir) / "fake_output"
        segments_dir = output_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        dubbed_audio_path = _export_silent_wav(output_dir / "dubbed.wav", duration_ms=2_000)
        ambient_path = _export_silent_wav(output_dir / "ambient.wav", duration_ms=2_000)
        subtitles_path = output_dir / "subtitles.srt"
        subtitles_en_path = output_dir / "subtitles_en.srt"
        subtitles_bilingual_path = output_dir / "subtitles_bilingual.srt"
        background_sounds_path = output_dir / "background.json"
        alignment_report_path = output_dir / "alignment_report.json"
        manifest_path = output_dir / "manifest.json"
        for path in (
            subtitles_path,
            subtitles_en_path,
            subtitles_bilingual_path,
            background_sounds_path,
            alignment_report_path,
            manifest_path,
        ):
            path.write_text("", encoding="utf-8")
        return OutputBundleResult(
            editor_result=ProjectOutputResult(
                dubbed_audio_path=str(dubbed_audio_path.resolve(strict=False)),
                ambient_audio_path=str(ambient_path.resolve(strict=False)),
                segments_dir=str(segments_dir.resolve(strict=False)),
                segment_count=2,
                subtitles_path=str(subtitles_path.resolve(strict=False)),
                subtitles_en_path=str(subtitles_en_path.resolve(strict=False)),
                subtitles_bilingual_path=str(subtitles_bilingual_path.resolve(strict=False)),
                background_sounds_path=str(background_sounds_path.resolve(strict=False)),
                alignment_report_path=str(alignment_report_path.resolve(strict=False)),
                needs_review_count=0,
            ),
            manifest_path=str(manifest_path.resolve(strict=False)),
        )

    monkeypatch.setattr(
        ProcessPipeline,
        "_dispatch_process_output_bundle",
        fake_dispatch_output_bundle,
    )

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=cache-probe-cps",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert captured["rewriter_chars_per_second"][0] == 7.25
    assert captured["rewriter_chars_per_second_by_speaker"][0] == {"speaker_a": 8.5}
    assert captured["pre_tts_chars_per_second"] == 7.25
    assert captured["pre_tts_chars_per_second_by_speaker"] == {"speaker_a": 8.5}


def test_process_pipeline_does_not_reuse_tts_cache_when_translation_is_regenerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config_mocks(monkeypatch)
    project_dir = tmp_path / "project_no_tts_cache_reuse"
    video_path = _write_video(project_dir / "video" / "original.mp4")
    audio_path = _export_silent_wav(project_dir / "audio" / "original.wav", duration_ms=2_500)
    _write_download_metadata(
        project_dir,
        video_path=video_path,
        audio_path=audio_path,
        video_title="No TTS Cache Reuse",
        duration_ms=2_500,
        url="https://youtube.example/watch?v=no-tts-reuse",
    )
    _write_transcript_cache(project_dir, _make_single_speaker_lines(), total_duration_ms=2_000)
    tts_dir = project_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    for segment in _make_single_speaker_segments():
        _export_silent_wav(
            tts_dir / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
            duration_ms=segment.target_duration_ms,
        )

    observed = {"generated_count": 0, "translate_called": 0}

    class FailDownloader:
        def download(self, request):
            del request
            raise AssertionError("download should not be called")

    class FailTranscriber:
        def __init__(self, api_key: str, http_timeout_seconds: float = 900.0):
            assert api_key == "assembly-key"
            assert http_timeout_seconds == 900.0

        def transcribe(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("transcribe should not be called")

    class FreshTranslator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def translate(
            self,
            lines,
            output_dir: str,
            voice_id: str,
            display_name: str = "Speaker A",
            max_segment_duration_ms: int = 60_000,
            voice_id_b: str | None = None,
            display_name_b: str | None = None,
            video_title: str = "",
            youtube_url: str = "",
            glossary: dict[str, str] | None = None,
            speaker_voices: dict[str, str] | None = None,
            chars_per_second: float | None = None,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ) -> TranslationResult:
            del lines, max_segment_duration_ms, voice_id_b, display_name_b, video_title, youtube_url, glossary, speaker_voices
            observed["translate_called"] += 1
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            segments = _make_single_speaker_segments()
            for segment in segments:
                segment.voice_id = voice_id
                segment.display_name = display_name
            return TranslationResult(
                segments=segments,
                total_segments=len(segments),
                output_path=str(Path(output_dir) / "segments.json"),
            )

    class FreshTTSGenerator:
        def __init__(self, config, **kwargs):
            assert config.api_key == "tts-key"

        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            observed["generated_count"] = len(segments)
            results: list[TTSResult] = []
            for segment in segments:
                audio_file = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.tts_audio_path = str(audio_file.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_file.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            return _build_aligned_segments(segments, output_dir)

    monkeypatch.setattr(process_module, "YouTubeDownloader", FailDownloader)
    monkeypatch.setattr(process_module, "AssemblyAITranscriber", FailTranscriber)
    monkeypatch.setattr(process_module, "GeminiTranslator", FreshTranslator)
    monkeypatch.setattr(process_module, "TTSGenerator", FreshTTSGenerator)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=no-tts-reuse",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    assert observed["translate_called"] == 1
    assert observed["generated_count"] == 2


def test_process_pipeline_fails_before_download_when_api_key_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"download": False}

    class FakeDownloader:
        def download(self, request):
            del request
            called["download"] = True
            raise AssertionError("download should not be called when config loading fails")

    monkeypatch.setattr(process_module, "YouTubeDownloader", FakeDownloader)
    monkeypatch.setattr(
        process_module,
        "load_assemblyai_config",
        lambda: (_ for _ in ()).throw(TranscriptionError("missing key")),
    )

    with pytest.raises(TranscriptionError, match="missing key"):
        ProcessPipeline().run(
            ProcessConfig(
                youtube_url="https://youtube.example/watch?v=demo",
                voice_a="voice_demo_001",
                project_dir=str(tmp_path / "project"),
            )
        )

    assert called["download"] is False


def test_process_pipeline_runs_review_step_for_two_speaker_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _install_dual_speaker_pipeline_mocks(
        monkeypatch,
        reviewed_lines=_make_reviewed_dual_speaker_lines(),
    )

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            voice_b="voice_b_001",
            speakers=2,
            project_dir=str(tmp_path / "project_review"),
            skip_review=False,
            job_record=_STUDIO_JOB_RECORD,
        )
    )

    transcript_path = tmp_path / "project_review" / "transcript" / "transcript.json"
    assert capture["review_called"] == 1
    assert capture["translate_input_speaker_ids"] == ["speaker_a", "speaker_a", "speaker_b"]
    assert transcript_path.exists()
    assert '"speaker_id": "speaker_b"' in transcript_path.read_text(encoding="utf-8")


def test_process_pipeline_skips_review_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _install_dual_speaker_pipeline_mocks(monkeypatch)

    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=dual",
            voice_a="voice_a_001",
            voice_b="voice_b_001",
            speakers=2,
            project_dir=str(tmp_path / "project_skip_review"),
            skip_review=True,
            job_record=_EXPRESS_JOB_RECORD,
        )
    )

    assert capture["review_called"] == 0
    assert capture["translate_input_speaker_ids"] == ["speaker_a", "speaker_b", "speaker_a"]


def test_process_pipeline_calibrates_tts_duration_and_writes_rewrite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_single_speaker_pipeline_mocks(monkeypatch)
    capture: dict[str, object] = {}

    class FakeEstimator:
        def __init__(self, chars_per_second: float = 4.5):
            capture["initial_chars_per_second"] = chars_per_second
            self.chars_per_second = chars_per_second

        def calibrate(self, samples):
            capture["calibration_samples"] = samples
            self.chars_per_second = 5.25
            return self.chars_per_second

    class FakeRewriter:
        def __init__(
            self,
            translator,
            chars_per_second: float = 4.5,
            chars_per_second_by_speaker: dict[str, float] | None = None,
        ):
            del translator
            self.chars_per_second = chars_per_second
            self.chars_per_second_by_speaker = chars_per_second_by_speaker or {}
            capture["rewriter_chars_per_second"] = chars_per_second
            capture["rewriter_chars_per_second_by_speaker"] = dict(self.chars_per_second_by_speaker)

    class FakeAligner:
        def __init__(self, *args, rewriter=None, tts_generator=None, **kwargs):
            del args, kwargs
            capture["received_tts_generator"] = tts_generator is not None
            capture["received_rewriter_chars_per_second"] = getattr(rewriter, "chars_per_second", None)

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            aligned_segments: list[AlignedSegment] = []
            for segment in segments:
                aligned_audio_path = str(
                    _export_silent_wav(
                        Path(output_dir) / f"segment_{segment.segment_id:03d}_aligned.wav",
                        duration_ms=segment.target_duration_ms,
                    ).resolve(strict=False)
                )
                if segment.segment_id == 1:
                    segment.cn_text = "更适合配音的文本。"
                    segment.tts_input_cn_text = segment.cn_text
                    segment.rewrite_count = 1
                    segment.alignment_method = "rewrite_dsp"
                    segment.actual_duration_ms = 950
                else:
                    segment.alignment_method = "direct"
                    segment.actual_duration_ms = segment.target_duration_ms
                segment.aligned_audio_path = aligned_audio_path
                segment.alignment_ratio = (
                    segment.actual_duration_ms / segment.target_duration_ms
                    if segment.target_duration_ms > 0
                    else 0.0
                )
                segment.needs_review = False
                aligned_segments.append(
                    AlignedSegment(
                        segment_id=segment.segment_id,
                        speaker_id=segment.speaker_id,
                        display_name=segment.display_name,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        cn_text=segment.cn_text,
                        en_text=getattr(segment, "en_text", ""),
                        aligned_audio_path=aligned_audio_path,
                        actual_duration_ms=segment.actual_duration_ms,
                        alignment_method=segment.alignment_method,
                        needs_review=False,
                    )
                )
            return aligned_segments

    monkeypatch.setattr(process_module, "TTSDurationEstimator", FakeEstimator)
    monkeypatch.setattr(process_module, "GeminiRewriter", FakeRewriter)
    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    project_dir = tmp_path / "project_rewrite_snapshot"
    ProcessPipeline().run(
        ProcessConfig(
            youtube_url="https://youtube.example/watch?v=demo",
            voice_a="voice_demo_001",
            project_dir=str(project_dir),
        )
    )

    segments_payload = json.loads(
        (project_dir / "translation" / "segments.json").read_text(encoding="utf-8")
    )

    assert capture["initial_chars_per_second"] == 4.5
    assert capture["received_tts_generator"] is True
    assert capture["rewriter_chars_per_second"] == 5.25
    assert capture["rewriter_chars_per_second_by_speaker"] == {}
    assert capture["received_rewriter_chars_per_second"] == 5.25
    assert capture["calibration_samples"] == [
        ("大家好，这是一个测试。", 1000),
        ("感谢观看。", 1000),
    ]
    # segments[0] is segment_id=1 → rewritten by FakeAligner (line 3341);
    # segments[1] is segment_id=2 → kept as-is. Match the actual second
    # segment text from _install_single_speaker_pipeline_mocks (probe sample
    # "感谢观看。"). Original test had a typo on the first assert.
    assert segments_payload["segments"][0]["cn_text"] == "更适合配音的文本。"
    assert segments_payload["segments"][0]["rewrite_count"] == 1
    assert segments_payload["segments"][0]["alignment_method"] == "rewrite_dsp"
    assert segments_payload["segments"][1]["cn_text"] == "感谢观看。"


def test_process_pipeline_calibrates_tts_duration_by_speaker_when_enough_samples_exist() -> None:
    pipeline = ProcessPipeline()
    segments = [
        DubbingSegment(
            segment_id=1,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=0,
            end_ms=1_000,
            target_duration_ms=1_000,
            source_text="A1",
            cn_text="甲甲甲甲",
            actual_duration_ms=1_000,
        ),
        DubbingSegment(
            segment_id=2,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=1_000,
            end_ms=2_000,
            target_duration_ms=1_000,
            source_text="A2",
            cn_text="甲甲甲甲甲甲",
            actual_duration_ms=1_000,
        ),
        DubbingSegment(
            segment_id=3,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=2_000,
            end_ms=3_000,
            target_duration_ms=1_000,
            source_text="A3",
            cn_text="甲甲甲甲甲",
            actual_duration_ms=1_000,
        ),
        DubbingSegment(
            segment_id=4,
            speaker_id="speaker_b",
            display_name="Speaker B",
            voice_id="voice_b",
            start_ms=3_000,
            end_ms=4_000,
            target_duration_ms=1_000,
            source_text="B1",
            cn_text="乙乙",
            actual_duration_ms=1_000,
        ),
        DubbingSegment(
            segment_id=5,
            speaker_id="speaker_b",
            display_name="Speaker B",
            voice_id="voice_b",
            start_ms=4_000,
            end_ms=5_000,
            target_duration_ms=1_000,
            source_text="B2",
            cn_text="乙乙乙",
            actual_duration_ms=1_000,
        ),
        DubbingSegment(
            segment_id=6,
            speaker_id="speaker_b",
            display_name="Speaker B",
            voice_id="voice_b",
            start_ms=5_000,
            end_ms=6_000,
            target_duration_ms=1_000,
            source_text="B3",
            cn_text="乙乙乙乙",
            actual_duration_ms=1_000,
        ),
    ]

    global_chars_per_second, chars_per_second_by_speaker = pipeline._calibrate_tts_duration(segments)

    assert global_chars_per_second == pytest.approx(4.0, abs=0.001)
    assert chars_per_second_by_speaker["speaker_a"] == pytest.approx(5.0, abs=0.001)
    assert chars_per_second_by_speaker["speaker_b"] == pytest.approx(3.0, abs=0.001)


def test_calibrate_tts_duration_normalizes_dsp_speed_param() -> None:
    """Phase 2 Task 1 (2026-04-15): when ``dsp_speed_param`` is non-1.0,
    ``_calibrate_tts_duration`` MUST multiply ``actual_duration_ms`` by
    ``dsp_speed_param`` to recover the speed=1.0 baseline before computing
    cps. Otherwise a segment that ran at speed=1.5 reports 33% shorter
    duration, yielding a cps that is 33% too high — poisoning the
    calibration whenever Phase 2 TTS speed is enabled.

    Setup: speaker_a has 3 segments, all with cn_text of 5 hanzi and
    actual_duration_ms=1_000ms but speed=1.5. The natural duration
    (speed=1.0 equivalent) is 1_500ms, so the real cps is 5 / 1.5 = 3.333.
    Without normalization the test would see 5.0 (= 5 / 1.0) and fail.
    """
    pipeline = ProcessPipeline()
    segments = [
        DubbingSegment(
            segment_id=i,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=(i - 1) * 1_000,
            end_ms=i * 1_000,
            target_duration_ms=1_000,
            source_text=f"A{i}",
            cn_text="甲甲甲甲甲",
            actual_duration_ms=1_000,
            dsp_speed_param=1.5,  # segment ran 50% faster than natural
        )
        for i in range(1, 4)
    ]

    global_cps, cps_by_speaker = pipeline._calibrate_tts_duration(segments)

    # Natural duration 1500ms × 3 = 4500ms for 15 hanzi → cps = 3.333
    assert global_cps == pytest.approx(3.333, abs=0.01)
    assert cps_by_speaker["speaker_a"] == pytest.approx(3.333, abs=0.01)


def test_calibrate_tts_duration_defaults_to_identity_when_speed_param_absent() -> None:
    """Pre-Phase 2 segments (no dsp_speed_param set) use dataclass default
    1.0, so the normalization is a no-op and the legacy path stays
    byte-identical. Guards against regression in the legacy branch."""
    pipeline = ProcessPipeline()
    segments = [
        DubbingSegment(
            segment_id=i,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=(i - 1) * 1_000,
            end_ms=i * 1_000,
            target_duration_ms=1_000,
            source_text=f"A{i}",
            cn_text="甲甲甲甲",  # 4 hanzi
            actual_duration_ms=1_000,
            # dsp_speed_param left at dataclass default (1.0)
        )
        for i in range(1, 4)
    ]

    global_cps, cps_by_speaker = pipeline._calibrate_tts_duration(segments)

    assert global_cps == pytest.approx(4.0, abs=0.001)
    assert cps_by_speaker["speaker_a"] == pytest.approx(4.0, abs=0.001)


def test_calibrate_tts_duration_handles_invalid_speed_param() -> None:
    """Guard against dsp_speed_param being 0.0 / negative / None — all
    should fall back to 1.0 rather than produce infinite or negative cps."""
    pipeline = ProcessPipeline()
    segments = [
        DubbingSegment(
            segment_id=1,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=0, end_ms=1_000,
            target_duration_ms=1_000,
            source_text="A1", cn_text="甲甲甲甲",
            actual_duration_ms=1_000,
            dsp_speed_param=0.0,  # invalid; should fall back to 1.0
        ),
        DubbingSegment(
            segment_id=2,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=1_000, end_ms=2_000,
            target_duration_ms=1_000,
            source_text="A2", cn_text="甲甲甲甲",
            actual_duration_ms=1_000,
            dsp_speed_param=-0.5,  # invalid; should fall back to 1.0
        ),
        DubbingSegment(
            segment_id=3,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=2_000, end_ms=3_000,
            target_duration_ms=1_000,
            source_text="A3", cn_text="甲甲甲甲",
            actual_duration_ms=1_000,
            dsp_speed_param=1.0,
        ),
    ]

    global_cps, cps_by_speaker = pipeline._calibrate_tts_duration(segments)

    # All 3 segments normalized to natural=1000ms, so cps = 4.0.
    assert global_cps == pytest.approx(4.0, abs=0.001)
    assert cps_by_speaker["speaker_a"] == pytest.approx(4.0, abs=0.001)


def test_process_pipeline_marks_only_same_speaker_short_merge_candidates() -> None:
    pipeline = ProcessPipeline()
    segments = [
        DubbingSegment(
            segment_id=1,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=0,
            end_ms=6_000,
            target_duration_ms=6_000,
            source_text="Long lead in.",
            cn_text="long lead in",
        ),
        DubbingSegment(
            segment_id=2,
            speaker_id="speaker_a",
            display_name="Speaker A",
            voice_id="voice_a",
            start_ms=6_100,
            end_ms=7_600,
            target_duration_ms=1_500,
            source_text="Right.",
            cn_text="ok",
        ),
        DubbingSegment(
            segment_id=3,
            speaker_id="speaker_b",
            display_name="Speaker B",
            voice_id="voice_b",
            start_ms=7_800,
            end_ms=9_300,
            target_duration_ms=1_500,
            source_text="Yes.",
            cn_text="yes",
        ),
    ]

    summary = pipeline._annotate_short_segment_merge_candidates(segments)

    assert summary == {"candidate_count": 1, "blocked_cross_speaker_count": 1}
    assert segments[1].short_merge_candidate is True
    assert segments[1].short_merge_target_segment_id == 1
    assert segments[1].short_merge_reason == "same_speaker_prev"
    assert segments[2].short_merge_candidate is False
    assert segments[2].short_merge_blocked_reason == "cross_speaker_adjacent"


def test_process_pipeline_merges_same_speaker_short_segment_before_tts(tmp_path: Path) -> None:
    pipeline = ProcessPipeline()
    translation_result = TranslationResult(
        segments=[
            DubbingSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Speaker A",
                voice_id="voice_a",
                start_ms=0,
                end_ms=6_000,
                target_duration_ms=6_000,
                source_text="This is the main point.",
                cn_text="这是主要内容",
                tts_audio_path="stale.wav",
                actual_duration_ms=6_500,
                alignment_method="force_dsp",
                needs_review=True,
            ),
            DubbingSegment(
                segment_id=2,
                speaker_id="speaker_a",
                display_name="Speaker A",
                voice_id="voice_a",
                start_ms=6_100,
                end_ms=7_300,
                target_duration_ms=1_200,
                source_text="Right.",
                cn_text="对",
            ),
            DubbingSegment(
                segment_id=3,
                speaker_id="speaker_b",
                display_name="Speaker B",
                voice_id="voice_b",
                start_ms=7_500,
                end_ms=9_000,
                target_duration_ms=1_500,
                source_text="Yes.",
                cn_text="是的",
            ),
        ],
        total_segments=3,
        output_path=str(tmp_path / "segments.json"),
    )

    summary = pipeline._apply_short_segment_merges_before_tts(translation_result)

    assert summary["candidate_count"] == 1
    assert summary["blocked_cross_speaker_count"] == 1
    assert summary["applied_count"] == 1
    assert summary["absorbed_count"] == 1
    assert len(translation_result.segments) == 2
    merged = translation_result.segments[0]
    assert merged.segment_id == 1
    assert merged.start_ms == 0
    assert merged.end_ms == 7_300
    assert merged.target_duration_ms == 7_300
    assert merged.source_text == "This is the main point. Right."
    assert merged.cn_text == "这是主要内容 对"
    assert merged.tts_audio_path is None
    assert merged.actual_duration_ms == 0
    assert merged.alignment_method == ""
    assert merged.needs_review is False
    assert merged.short_merge_applied is True
    assert merged.short_merge_absorbed_segment_ids == "2"
    assert translation_result.total_segments == 2
    assert translation_result.segments[1].short_merge_blocked_reason == "cross_speaker_adjacent"


def test_process_pipeline_does_not_merge_cross_speaker_short_segment(tmp_path: Path) -> None:
    pipeline = ProcessPipeline()
    translation_result = TranslationResult(
        segments=[
            DubbingSegment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Speaker A",
                voice_id="voice_a",
                start_ms=0,
                end_ms=3_000,
                target_duration_ms=3_000,
                source_text="Question?",
                cn_text="问题",
            ),
            DubbingSegment(
                segment_id=2,
                speaker_id="speaker_b",
                display_name="Speaker B",
                voice_id="voice_b",
                start_ms=3_100,
                end_ms=4_100,
                target_duration_ms=1_000,
                source_text="Yes.",
                cn_text="是",
            ),
        ],
        total_segments=2,
        output_path=str(tmp_path / "segments.json"),
    )

    summary = pipeline._apply_short_segment_merges_before_tts(translation_result)

    assert summary["candidate_count"] == 0
    assert summary["blocked_cross_speaker_count"] == 1
    assert summary["applied_count"] == 0
    assert summary["absorbed_count"] == 0
    assert len(translation_result.segments) == 2
    assert translation_result.segments[1].short_merge_blocked_reason == "cross_speaker_adjacent"


def test_process_pipeline_classifies_low_share_short_interaction_speaker() -> None:
    pipeline = ProcessPipeline()
    lines = [
        TranscriptLine(1, 0, 300_000, "speaker_a", "A", "Main talk."),
        TranscriptLine(2, 300_000, 600_000, "speaker_a", "A", "More main talk."),
        TranscriptLine(3, 610_000, 614_000, "speaker_b", "B", "Yes."),
        TranscriptLine(4, 620_000, 624_000, "speaker_b", "B", "No."),
        TranscriptLine(5, 630_000, 635_000, "speaker_b", "B", "Okay."),
        TranscriptLine(6, 640_000, 646_000, "speaker_b", "B", "Right."),
    ]

    profiles = pipeline._build_speaker_structure_profiles(lines)

    assert profiles["speaker_a"]["speaker_role"] == "primary"
    assert profiles["speaker_b"]["speaker_role"] == "incidental"
    assert profiles["speaker_b"]["speaker_structure_reason"] == "low_share_short_interactions"
    assert profiles["speaker_b"]["speaker_segment_count"] == 4
    assert profiles["speaker_b"]["speaker_short_segment_count"] == 4


def test_process_pipeline_keeps_balanced_interview_speakers_primary() -> None:
    pipeline = ProcessPipeline()
    lines = [
        TranscriptLine(1, 0, 60_000, "speaker_a", "A", "Question."),
        TranscriptLine(2, 60_000, 120_000, "speaker_b", "B", "Answer."),
        TranscriptLine(3, 120_000, 180_000, "speaker_a", "A", "Follow up."),
        TranscriptLine(4, 180_000, 240_000, "speaker_b", "B", "More answer."),
    ]

    profiles = pipeline._build_speaker_structure_profiles(lines)

    assert profiles["speaker_a"]["speaker_role"] == "primary"
    assert profiles["speaker_b"]["speaker_role"] == "primary"
    assert profiles["speaker_b"]["speaker_structure_reason"] == "balanced_main_speaker"


def test_voice_selection_payload_marks_incidental_speaker_without_forcing_clone() -> None:
    pipeline = ProcessPipeline()
    lines = [
        TranscriptLine(1, 0, 300_000, "speaker_a", "A", "Main talk."),
        TranscriptLine(2, 300_000, 600_000, "speaker_a", "A", "More main talk."),
        TranscriptLine(3, 610_000, 614_000, "speaker_b", "B", "Yes."),
        TranscriptLine(4, 620_000, 624_000, "speaker_b", "B", "No."),
        TranscriptLine(5, 630_000, 635_000, "speaker_b", "B", "Okay."),
        TranscriptLine(6, 640_000, 646_000, "speaker_b", "B", "Right."),
    ]
    transcript = TranscriptResult(
        lines=lines,
        total_duration_ms=646_000,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )
    profiles = pipeline._build_speaker_structure_profiles(lines)

    payload = pipeline._build_voice_selection_review_payload(
        transcript_result=transcript,
        tts_provider="minimax",
        service_mode="studio",
        source_audio_path="",
        effective_speakers=2,
        speaker_names={"speaker_a": "Host", "speaker_b": "Speaker B"},
        speaker_styles={},
        speaker_structure_profiles=profiles,
    )

    speakers = {
        str(speaker["speaker_id"]): speaker
        for speaker in payload["speakers"]
        if isinstance(speaker, dict)
    }
    assert speakers["speaker_b"]["speaker_role"] == "incidental"
    assert speakers["speaker_b"]["speaker_name"] == "短互动/观众"
    assert speakers["speaker_b"]["can_clone"] is False
    assert speakers["speaker_b"]["speaker_duration_share"] < 0.08


def test_voice_selection_payload_marks_non_speech_speaker_without_clone() -> None:
    pipeline = ProcessPipeline()
    lines = [
        TranscriptLine(1, 0, 300_000, "speaker_a", "A", "Main talk."),
        TranscriptLine(2, 300_000, 315_000, "speaker_c", "C", "Crowd cheering."),
    ]
    transcript = TranscriptResult(
        lines=lines,
        total_duration_ms=315_000,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )
    speaker_styles = {
        "speaker_a": {"name": "Main speaker"},
        "speaker_c": {
            "name": "未知说话人1",
            "is_non_speech": "true",
            "non_speech_reason": "crowd cheering",
        },
    }
    profiles = pipeline._build_speaker_structure_profiles(lines, speaker_styles=speaker_styles)

    payload = pipeline._build_voice_selection_review_payload(
        transcript_result=transcript,
        tts_provider="minimax",
        service_mode="studio",
        source_audio_path="",
        effective_speakers=2,
        speaker_names={"speaker_a": "Main speaker", "speaker_c": "speaker_c"},
        speaker_styles=speaker_styles,
        speaker_structure_profiles=profiles,
    )

    speakers = {
        str(speaker["speaker_id"]): speaker
        for speaker in payload["speakers"]
        if isinstance(speaker, dict)
    }
    assert profiles["speaker_c"]["speaker_role"] == "non_speech"
    assert profiles["speaker_c"]["speaker_structure_reason"] == "review_profile_non_speech"
    assert speakers["speaker_c"]["speaker_name"] == "背景音/非对白"
    assert speakers["speaker_c"]["speaker_role_label"] == "背景音/非对白"
    assert speakers["speaker_c"]["can_clone"] is False


def test_process_pipeline_detects_low_information_underflow_cue() -> None:
    segment = DubbingSegment(
        segment_id=3,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=10_552,
        target_duration_ms=10_552,
        source_text="10 seconds left. Next exercise.",
        cn_text="还剩十秒。下一个动作。",
        first_pass_duration_ms=2_682,
        alignment_method="capped_dsp_underflow",
    )

    reason = ProcessPipeline._low_information_underflow_keep_original_reason(segment)

    assert reason == "low_information_cue_underflow"


def test_process_pipeline_does_not_auto_keep_contentful_underflow_sentence() -> None:
    segment = DubbingSegment(
        segment_id=3,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=9_596,
        target_duration_ms=9_596,
        source_text="OK may be the most recognizable word in the world.",
        cn_text="OK 可能是全球辨识度最高的词了。",
        first_pass_duration_ms=3_274,
        alignment_method="capped_dsp_underflow",
    )

    reason = ProcessPipeline._low_information_underflow_keep_original_reason(segment)

    assert reason == ""


def test_process_pipeline_detects_short_content_compact_candidate() -> None:
    segment = DubbingSegment(
        segment_id=21,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=3_145,
        target_duration_ms=3_145,
        source_text=(
            "Would you repeat that this time? If trouble's coming, "
            "would you still say buy stocks right now?"
        ),
        cn_text="您现在还会重复那句话吗？如果麻烦要来了，您还会建议现在买入股票吗？",
    )

    candidate, content_class, lower, upper = (
        ProcessPipeline._is_short_content_compact_candidate(
            segment,
            rewrite_label="overshoot",
            pre_chars=30,
            estimated_duration_ms=6_650,
            decision_estimated_duration_ms=7_648,
            target_duration_ms=3_145,
        )
    )

    assert candidate is True
    assert content_class == "question"
    assert (lower, upper) == (8, 13)


def test_process_pipeline_excludes_low_information_from_short_content_compact() -> None:
    segment = DubbingSegment(
        segment_id=18,
        speaker_id="speaker_a",
        display_name="Coach",
        voice_id="voice_a",
        start_ms=0,
        end_ms=5_158,
        target_duration_ms=5_158,
        source_text="10 seconds left. Next exercise.",
        cn_text="还剩十秒钟。下一个动作。",
    )

    candidate, content_class, lower, upper = (
        ProcessPipeline._is_short_content_compact_candidate(
            segment,
            rewrite_label="overshoot",
            pre_chars=12,
            estimated_duration_ms=8_000,
            decision_estimated_duration_ms=9_200,
            target_duration_ms=5_158,
        )
    )

    assert candidate is False
    assert content_class == "low_information"
    assert (lower, upper) == (0, 0)


def test_process_pipeline_auto_keeps_low_information_underflow_after_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=18,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=10_000,
        end_ms=15_158,
        target_duration_ms=5_158,
        source_text="know, 10 seconds left.",
        cn_text="还剩十秒钟。",
        first_pass_duration_ms=1_614,
        alignment_method="capped_dsp_underflow",
        needs_review=True,
        dsp_speed_ratio_used=0.67,
        dsp_silence_padded_ms=2_791,
        force_dsp_severity="high",
    )
    materialized: list[DubbingSegment] = []

    def fake_materialize(
        self: ProcessPipeline,
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        del self, source_audio_path, tts_dir
        materialized.extend(segments)
        for item in segments:
            item.alignment_method = "keep_original"
            item.needs_review = False
            item.dsp_silence_padded_ms = 0
        return len(segments)

    monkeypatch.setattr(
        ProcessPipeline,
        "_materialize_keep_original_segments",
        fake_materialize,
    )

    count = pipeline._auto_keep_low_information_underflow_segments(
        [segment],
        source_audio_path=tmp_path / "source.wav",
        tts_dir=tmp_path / "tts",
    )

    assert count == 1
    assert materialized == [segment]
    assert segment.dubbing_mode == "keep_original"
    assert segment.alignment_method == "keep_original"
    assert segment.auto_keep_original_reason == "low_information_cue_underflow"
    assert segment.auto_keep_original_source == "low_information_underflow_route"
    assert segment.needs_review is False


def test_process_pipeline_uses_absorbed_ids_for_output_block_indices() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=3,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=4_000,
        target_duration_ms=4_000,
        source_text="A B",
        cn_text="甲 乙",
        short_merge_applied=True,
        short_merge_absorbed_segment_ids="1,2",
    )

    blocks = pipeline._build_process_output_blocks([segment])

    assert blocks[0].original_srt_indices == [1, 2, 3]


def test_process_pipeline_attempts_semantic_split_repair_for_failed_long_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_006_speaker_a.wav", duration_ms=78_000)
    failed_segment = DubbingSegment(
        segment_id=6,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        aligned_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=78_000,
        alignment_ratio=1.3,
        alignment_method="force_dsp",
        rewrite_count=2,
        needs_review=True,
    )
    observed: dict[str, object] = {}

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            observed["tts_texts"] = [segment.cn_text for segment in segments]
            results: list[TTSResult] = []
            for segment in segments:
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=segment.target_duration_ms - 400,
                )
                segment.tts_audio_path = str(audio_path.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms - 400
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=segment.target_duration_ms - 400,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            aligned_segments: list[AlignedSegment] = []
            for segment in segments:
                aligned_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_aligned.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.aligned_audio_path = str(aligned_path.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                segment.alignment_ratio = 1.0
                segment.alignment_method = "dsp"
                segment.needs_review = False
                aligned_segments.append(
                    AlignedSegment(
                        segment_id=segment.segment_id,
                        speaker_id=segment.speaker_id,
                        display_name=segment.display_name,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        cn_text=segment.cn_text,
                        en_text=getattr(segment, "en_text", ""),
                        aligned_audio_path=str(aligned_path.resolve(strict=False)),
                        actual_duration_ms=segment.target_duration_ms,
                        alignment_method="dsp",
                        needs_review=False,
                    )
                )
            return aligned_segments

    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    repaired_segments = pipeline._attempt_semantic_split_repair(
        segment=failed_segment,
        next_segment_id=100,
        tts_generator=FakeTTSGenerator(),
        rewriter=object(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert repaired_segments is not None
    assert [segment.segment_id for segment in repaired_segments] == [100, 101]
    assert observed["tts_texts"] == ["第一句。第二句。", "第三句。第四句。"]
    assert repaired_segments[0].start_ms == 0
    assert repaired_segments[0].end_ms == repaired_segments[1].start_ms
    assert repaired_segments[1].end_ms == 60_000
    assert all(segment.needs_review is False for segment in repaired_segments)
    assert all(segment.alignment_method == "dsp" for segment in repaired_segments)


def test_process_pipeline_skips_semantic_split_repair_without_clear_sentence_boundary(
    tmp_path: Path,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_010_speaker_a.wav", duration_ms=70_000)
    failed_segment = DubbingSegment(
        segment_id=10,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="This is one very long sentence without a clean split point " * 3,
        cn_text="这是一个没有明确句号而且非常长的句子这是一个没有明确句号而且非常长的句子这是一个没有明确句号而且非常长的句子",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        aligned_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=70_000,
        alignment_ratio=1.16,
        alignment_method="force_dsp",
        rewrite_count=2,
        needs_review=True,
    )

    repaired_segments = pipeline._attempt_semantic_split_repair(
        segment=failed_segment,
        next_segment_id=200,
        tts_generator=object(),  # type: ignore[arg-type]
        rewriter=object(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert repaired_segments is None


def test_process_pipeline_keeps_semantic_split_when_one_child_still_requires_force_dsp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_011_speaker_a.wav", duration_ms=78_000)
    failed_segment = DubbingSegment(
        segment_id=11,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        aligned_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=78_000,
        alignment_ratio=1.3,
        alignment_method="force_dsp",
        rewrite_count=2,
        needs_review=True,
    )
    observed: dict[str, object] = {
        "tts_calls": [],
        "align_call_sizes": [],
        "rewrite_texts": [],
    }

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            observed["tts_calls"].append([(segment.segment_id, segment.cn_text) for segment in segments])
            results: list[TTSResult] = []
            for segment in segments:
                if len(segments) == 1:
                    duration_ms = segment.target_duration_ms + 1_200
                elif segment.segment_id % 2 == 0:
                    duration_ms = segment.target_duration_ms - 400
                else:
                    duration_ms = segment.target_duration_ms + 3_200
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                    duration_ms=duration_ms,
                )
                segment.tts_audio_path = str(audio_path.resolve(strict=False))
                segment.actual_duration_ms = duration_ms
                results.append(
                    TTSResult(
                        segment_id=segment.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=duration_ms,
                        voice_id=segment.voice_id,
                    )
                )
            return results

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            observed["rewrite_texts"].append(
                (cn_text, actual_duration_ms, target_duration_ms, source_text, speaker_id)
            )
            return "第三句。第四句。精简版。"

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            observed["align_call_sizes"].append(len(segments))
            aligned_segments: list[AlignedSegment] = []
            for index, segment in enumerate(segments):
                aligned_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{segment.segment_id:03d}_aligned.wav",
                    duration_ms=segment.target_duration_ms,
                )
                segment.aligned_audio_path = str(aligned_path.resolve(strict=False))
                segment.actual_duration_ms = segment.target_duration_ms
                segment.alignment_ratio = 1.0
                if len(segments) == 1:
                    segment.alignment_method = "force_dsp"
                    segment.needs_review = True
                elif index == 0:
                    segment.alignment_method = "dsp"
                    segment.needs_review = False
                else:
                    segment.alignment_method = "force_dsp"
                    segment.needs_review = True
                aligned_segments.append(
                    AlignedSegment(
                        segment_id=segment.segment_id,
                        speaker_id=segment.speaker_id,
                        display_name=segment.display_name,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        cn_text=segment.cn_text,
                        en_text=getattr(segment, "en_text", ""),
                        aligned_audio_path=str(aligned_path.resolve(strict=False)),
                        actual_duration_ms=segment.target_duration_ms,
                        alignment_method=segment.alignment_method,
                        needs_review=segment.needs_review,
                    )
                )
            return aligned_segments

    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)

    repaired_segments = pipeline._attempt_semantic_split_repair(
        segment=failed_segment,
        next_segment_id=300,
        tts_generator=FakeTTSGenerator(),
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert repaired_segments is not None
    assert [segment.segment_id for segment in repaired_segments] == [300, 301]
    assert observed["align_call_sizes"] == [2, 1]
    assert observed["tts_calls"] == [
        [(300, "第一句。第二句。"), (301, "第三句。第四句。")],
        [(301, "第三句。第四句。精简版。")],
    ]
    assert observed["rewrite_texts"] == [
        (
            "第三句。第四句。",
            33_200,
            30_000,
            "Third sentence.Fourth sentence.",
            "speaker_a",
        )
    ]
    assert repaired_segments[0].needs_review is False
    assert repaired_segments[0].alignment_method == "dsp"
    assert repaired_segments[1].needs_review is True
    assert repaired_segments[1].alignment_method == "force_dsp"


def test_retry_failed_semantic_child_forces_resynthesis_after_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    tts_dir = tmp_path / "tts"
    old_tts_path = _export_silent_wav(
        tts_dir / "segment_007_speaker_a.wav",
        duration_ms=5_000,
    )
    child_segment = DubbingSegment(
        segment_id=7,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=3_000,
        target_duration_ms=3_000,
        source_text="Old child source.",
        cn_text="old child text",
        tts_audio_path=str(old_tts_path.resolve(strict=False)),
        actual_duration_ms=5_000,
        alignment_ratio=5_000 / 3_000,
        alignment_method="force_dsp",
        needs_review=True,
    )
    observed: dict[str, object] = {
        "generate_one_calls": [],
        "generated": False,
    }

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            del segments, output_dir
            raise AssertionError("retry rewrite must not use cache-skipping generate_all")

        def _generate_one(
            self,
            segment: DubbingSegment,
            output_dir: str,
            *,
            usage_bucket: str | None = None,
        ) -> TTSResult:
            observed["generate_one_calls"].append(
                (segment.segment_id, segment.cn_text, usage_bucket)
            )
            observed["generated"] = True
            audio_path = _export_silent_wav(
                Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav",
                duration_ms=2_600,
            )
            return TTSResult(
                segment_id=segment.segment_id,
                audio_path=str(audio_path.resolve(strict=False)),
                duration_ms=2_600,
                voice_id=segment.voice_id,
                selected_voice="voice_generated",
                match_confidence="high",
            )

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            assert cn_text == "old child text"
            assert actual_duration_ms == 5_000
            assert target_duration_ms == 3_000
            assert source_text == "Old child source."
            assert speaker_id == "speaker_a"
            return "rewritten child text"

    class FakeAligner:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def align_all(self, segments: list[DubbingSegment], output_dir: str) -> list[AlignedSegment]:
            del output_dir
            assert len(segments) == 1
            segment = segments[0]
            assert segment.cn_text == "rewritten child text"
            assert segment.tts_input_cn_text == "rewritten child text"
            segment.aligned_audio_path = segment.tts_audio_path
            segment.alignment_method = "dsp"
            segment.needs_review = False
            return []

    def fake_duration(path: Path | str) -> int:
        if (
            Path(path).resolve(strict=False) == old_tts_path.resolve(strict=False)
            and not observed["generated"]
        ):
            return 5_000
        return 2_600

    monkeypatch.setattr(process_module, "SegmentAligner", FakeAligner)
    monkeypatch.setattr(process_module, "_ffprobe_duration_ms", fake_duration)

    pipeline._retry_failed_semantic_child(
        child_segment=child_segment,
        tts_generator=FakeTTSGenerator(),  # type: ignore[arg-type]
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        tts_dir=tts_dir,
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert observed["generate_one_calls"] == [
        (7, "rewritten child text", process_module.TTS_BUCKET_POST_TTS_RESYNTH)
    ]
    assert child_segment.cn_text == "rewritten child text"
    assert child_segment.tts_input_cn_text == "rewritten child text"
    assert child_segment.actual_duration_ms == 2_600
    assert child_segment.selected_voice == "voice_generated"
    assert child_segment.match_confidence == "high"
    assert child_segment.needs_review is False
    assert child_segment.alignment_method == "dsp"


def test_process_pipeline_presplits_long_overshoot_segment_before_alignment(
    tmp_path: Path,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_020_speaker_a.wav", duration_ms=78_000)
    segment = DubbingSegment(
        segment_id=20,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=78_000,
        alignment_ratio=1.3,
    )
    translation_result = TranslationResult(
        segments=[segment],
        total_segments=1,
        output_path=str((tmp_path / "translation" / "segments.json").resolve(strict=False)),
    )
    observed: dict[str, object] = {"tts_calls": []}

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            observed["tts_calls"].append([(segment.segment_id, segment.cn_text) for segment in segments])
            results: list[TTSResult] = []
            for item in segments:
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{item.segment_id:03d}_{item.speaker_id}.wav",
                    duration_ms=item.target_duration_ms - 600,
                )
                item.tts_audio_path = str(audio_path.resolve(strict=False))
                item.actual_duration_ms = item.target_duration_ms - 600
                item.alignment_ratio = item.actual_duration_ms / item.target_duration_ms
                results.append(
                    TTSResult(
                        segment_id=item.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=item.actual_duration_ms,
                        voice_id=item.voice_id,
                    )
                )
            return results

    presplit_count = pipeline._presplit_long_overshoot_segments_before_alignment(
        translation_result=translation_result,
        tts_generator=FakeTTSGenerator(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert presplit_count == 1
    assert translation_result.total_segments == 2
    assert [item.segment_id for item in translation_result.segments] == [21, 22]
    assert observed["tts_calls"] == [[(21, "第一句。第二句。"), (22, "第三句。第四句。")]]
    assert translation_result.segments[0].start_ms == 0
    assert translation_result.segments[0].end_ms == translation_result.segments[1].start_ms
    assert translation_result.segments[1].end_ms == 60_000


def test_process_pipeline_does_not_presplit_long_segment_below_overshoot_threshold(
    tmp_path: Path,
) -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=30,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        actual_duration_ms=77_000,
        alignment_ratio=77_000 / 60_000,
    )
    translation_result = TranslationResult(
        segments=[segment],
        total_segments=1,
        output_path=str((tmp_path / "translation" / "segments.json").resolve(strict=False)),
    )

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            del segments, output_dir
            raise AssertionError("generate_all should not be called below the presplit threshold")

    presplit_count = pipeline._presplit_long_overshoot_segments_before_alignment(
        translation_result=translation_result,
        tts_generator=FakeTTSGenerator(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert presplit_count == 0
    assert translation_result.total_segments == 1
    assert [item.segment_id for item in translation_result.segments] == [30]


def test_process_pipeline_presplits_severely_overshot_medium_long_segment_before_alignment(
    tmp_path: Path,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_031_speaker_a.wav", duration_ms=43_000)
    segment = DubbingSegment(
        segment_id=31,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=30_000,
        target_duration_ms=30_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=43_000,
        alignment_ratio=43_000 / 30_000,
    )
    translation_result = TranslationResult(
        segments=[segment],
        total_segments=1,
        output_path=str((tmp_path / "translation" / "segments.json").resolve(strict=False)),
    )
    observed: dict[str, object] = {"tts_calls": []}

    class FakeTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            observed["tts_calls"].append([(segment.segment_id, segment.cn_text) for segment in segments])
            results: list[TTSResult] = []
            for item in segments:
                audio_path = _export_silent_wav(
                    Path(output_dir) / f"segment_{item.segment_id:03d}_{item.speaker_id}.wav",
                    duration_ms=item.target_duration_ms - 400,
                )
                item.tts_audio_path = str(audio_path.resolve(strict=False))
                item.actual_duration_ms = item.target_duration_ms - 400
                item.alignment_ratio = item.actual_duration_ms / item.target_duration_ms
                results.append(
                    TTSResult(
                        segment_id=item.segment_id,
                        audio_path=str(audio_path.resolve(strict=False)),
                        duration_ms=item.actual_duration_ms,
                        voice_id=item.voice_id,
                    )
                )
            return results

    presplit_count = pipeline._presplit_long_overshoot_segments_before_alignment(
        translation_result=translation_result,
        tts_generator=FakeTTSGenerator(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=PostTTSBudgetTracker(),
    )

    assert presplit_count == 1
    assert translation_result.total_segments == 2
    assert [item.segment_id for item in translation_result.segments] == [32, 33]
    assert observed["tts_calls"] == [[(32, "第一句。第二句。"), (33, "第三句。第四句。")]]


def test_process_pipeline_skips_semantic_split_repair_when_post_tts_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    pipeline = ProcessPipeline()
    long_tts_path = _export_silent_wav(tmp_path / "tts" / "segment_050_speaker_a.wav", duration_ms=78_000)
    failed_segment = DubbingSegment(
        segment_id=50,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=60_000,
        target_duration_ms=60_000,
        source_text="First sentence. Second sentence. Third sentence. Fourth sentence.",
        cn_text="第一句。第二句。第三句。第四句。",
        tts_audio_path=str(long_tts_path.resolve(strict=False)),
        aligned_audio_path=str(long_tts_path.resolve(strict=False)),
        actual_duration_ms=78_000,
        alignment_ratio=1.3,
        alignment_method="force_dsp",
        rewrite_count=2,
        needs_review=True,
    )
    budget_tracker = PostTTSBudgetTracker(max_extra_tts_per_root=3)
    assert budget_tracker.try_consume_for_segment(failed_segment, 3) is True

    class FailingTTSGenerator:
        def generate_all(self, segments: list[DubbingSegment], output_dir: str) -> list[TTSResult]:
            del segments, output_dir
            raise AssertionError("generate_all should not be called when post-TTS budget is exhausted")

    repaired_segments = pipeline._attempt_semantic_split_repair(
        segment=failed_segment,
        next_segment_id=500,
        tts_generator=FailingTTSGenerator(),  # type: ignore[arg-type]
        rewriter=object(),  # type: ignore[arg-type]
        tts_dir=tmp_path / "tts_repaired",
        post_tts_budget_tracker=budget_tracker,
    )

    assert repaired_segments is None


def test_process_pipeline_pre_rewrites_obvious_overshoot_before_tts() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=40,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="a" * 120,
    )
    observed: dict[str, object] = {}

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            observed["args"] = (
                cn_text,
                actual_duration_ms,
                target_duration_ms,
                source_text,
                speaker_id,
            )
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert observed["args"] == ("a" * 120, 26_666, 20_000, "Original source text", "speaker_a")
    assert segment.cn_text == "b" * 90
    assert segment.rewrite_count == 1
    assert segment.pre_tts_rewrite_direction == "overshoot"
    assert segment.pre_tts_estimate_ms == 26_666
    assert segment.pre_tts_target_ms == 20_000
    assert segment.pre_tts_pre_chars == 120
    assert segment.pre_tts_post_chars == 90


def test_process_pipeline_pre_tts_rewrite_clears_stale_text_witness() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=40,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="a" * 120,
        tts_input_cn_text="a" * 120,
    )

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert segment.cn_text == "b" * 90
    assert segment.tts_input_cn_text == ""


def test_process_pipeline_pre_tts_rewrite_clears_stale_audio_cache(tmp_path: Path) -> None:
    segment = DubbingSegment(
        segment_id=40,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="rewritten",
        tts_audio_path=str(tmp_path / "segment_040_speaker_a.wav"),
        aligned_audio_path=str(tmp_path / "segment_040_aligned.wav"),
        actual_duration_ms=12_345,
        alignment_ratio=0.5,
        alignment_method="rewrite_dsp",
        pre_tts_rewrite_direction="overshoot",
    )
    raw_path = tmp_path / "segment_040_speaker_a.wav"
    aligned_path = tmp_path / "segment_040_aligned.wav"
    whisper_path = tmp_path / "segment_040_aligned.wav.whisper_small_zh.json"
    raw_path.write_bytes(b"raw")
    aligned_path.write_bytes(b"aligned")
    whisper_path.write_text("{}", encoding="utf-8")

    cleared = ProcessPipeline._clear_pre_tts_rewrite_audio_cache([segment], tmp_path)

    assert cleared == 3
    assert not raw_path.exists()
    assert not aligned_path.exists()
    assert not whisper_path.exists()
    assert segment.tts_audio_path is None
    assert segment.aligned_audio_path is None
    assert segment.actual_duration_ms == 0
    assert segment.alignment_ratio == 0.0
    assert segment.alignment_method == ""


def test_process_pipeline_uses_short_content_compact_before_tts() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=21,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=3_145,
        target_duration_ms=3_145,
        source_text=(
            "Would you repeat that this time? If trouble's coming, "
            "would you still say buy stocks right now?"
        ),
        cn_text="您现在还会重复那句话吗？如果麻烦要来了，您还会建议现在买入股票吗？",
    )
    observed: dict[str, object] = {}

    class FakeRewriter:
        def rewrite_short_content_compact(self, cn_text: str, **kwargs: object) -> str:
            observed["cn_text"] = cn_text
            observed["kwargs"] = kwargs
            return "现在还建议买股票吗"

        def rewrite_for_duration_with_profile(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("short content should use compact rewrite")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert observed["kwargs"] == {
        "source_text": segment.source_text,
        "target_duration_ms": 3_145,
        "target_lower_chars": 8,
        "target_upper_chars": 13,
        "task_name": "s5_short_content_compact",
    }
    assert segment.cn_text == "现在还建议买股票吗"
    assert segment.rewrite_count == 1
    assert segment.pre_tts_rewrite_direction == "overshoot"
    assert segment.pre_tts_rewrite_task == "s5_short_content_compact"
    assert segment.short_content_compact_attempted is True
    assert segment.short_content_compact_accepted is True
    assert segment.short_content_compact_class == "question"
    assert segment.short_content_compact_pre_chars == 30
    assert segment.short_content_compact_post_chars == 9


def test_process_pipeline_rejects_short_content_compact_when_required_digits_drop() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=178,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=3_077,
        target_duration_ms=3_077,
        source_text="Auto insurance, I'm not sure, I might prefer the 80-year-olds over the 20-year-olds.",
        cn_text="汽车保险的话，我不确定，我可能更倾向于保80岁的，而不是20岁的。",
    )

    class FakeRewriter:
        def rewrite_short_content_compact(self, cn_text: str, **kwargs: object) -> str:
            del cn_text, kwargs
            return "车险我更看好老人"

        def rewrite_for_duration_with_profile(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("rejected compact rewrite should not fall back to generic rewrite")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "汽车保险的话，我不确定，我可能更倾向于保80岁的，而不是20岁的。"
    assert segment.short_content_compact_attempted is True
    assert segment.short_content_compact_accepted is False
    assert segment.short_content_compact_rejected_reason == "missing_required_token"
    assert segment.pre_tts_rewrite_rejected is True
    assert segment.pre_tts_rewrite_rejected_reason == "short_compact_missing_required_token"


def test_process_pipeline_rejects_pre_tts_rewrite_below_char_floor() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=41,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="a" * 120,
    )

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            del cn_text, actual_duration_ms, target_duration_ms, source_text, speaker_id
            return "b" * 60

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "a" * 120
    assert segment.rewrite_count == 0
    assert getattr(segment, "pre_tts_rewrite_direction", "") == ""
    assert segment.pre_tts_rewrite_rejected is True
    assert segment.pre_tts_rewrite_rejected_reason == "below_floor"
    assert segment.pre_tts_rewrite_retry_attempted is False


def test_process_pipeline_strict_retries_long_pre_tts_rewrite_once() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=43,
        speaker_id="speaker_a",
        display_name="Long Speaker",
        voice_id="voice_a",
        start_ms=0,
        end_ms=30_000,
        target_duration_ms=30_000,
        source_text="Original source text",
        cn_text="a" * 200,
    )
    observed_tasks: list[str] = []
    observed_reasons: list[str] = []

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            del args
            observed_tasks.append(kwargs["task_name"])
            observed_reasons.append(kwargs.get("strict_retry_reason", ""))
            if kwargs["task_name"] == "s5_rewrite":
                return "b" * 120
            return "c" * 165

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert observed_tasks == ["s5_rewrite", "s5_rewrite_strict"]
    assert observed_reasons == ["", "below_floor"]
    assert segment.cn_text == "c" * 165
    assert segment.pre_tts_rewrite_direction == "overshoot"
    assert segment.pre_tts_rewrite_task == "s5_rewrite_strict"
    assert segment.pre_tts_rewrite_retry_attempted is True
    assert segment.pre_tts_rewrite_retry_accepted is True
    assert segment.pre_tts_rewrite_initial_rejected_reason == "below_floor"
    assert segment.pre_tts_rewrite_rejected is False


def test_process_pipeline_records_long_pre_tts_strict_retry_rejection() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=44,
        speaker_id="speaker_a",
        display_name="Long Speaker",
        voice_id="voice_a",
        start_ms=0,
        end_ms=30_000,
        target_duration_ms=30_000,
        source_text="Original source text",
        cn_text="a" * 200,
    )

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            del args
            if kwargs["task_name"] == "s5_rewrite":
                return "b" * 120
            return "c" * 140

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "a" * 200
    assert segment.pre_tts_rewrite_rejected is True
    assert segment.pre_tts_rewrite_rejected_reason == "strict_below_floor"
    assert segment.pre_tts_rewrite_rejected_pre_chars == 200
    assert segment.pre_tts_rewrite_rejected_post_chars == 140
    assert segment.pre_tts_rewrite_rejected_lower_chars == 160
    assert segment.pre_tts_rewrite_rejected_upper_chars == 173
    assert segment.pre_tts_rewrite_retry_attempted is True
    assert segment.pre_tts_rewrite_retry_accepted is False


def test_process_pipeline_passes_pre_tts_guardrail_bounds_to_profile_rewriter() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=42,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="a" * 120,
    )
    observed: dict[str, object] = {}

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert observed["args"] == ("a" * 120,)
    assert observed["kwargs"]["target_lower_chars"] == 90
    assert observed["kwargs"]["target_upper_chars"] == 101


def test_process_pipeline_tightens_short_high_shrink_pre_tts_bounds() -> None:
    bounds = ProcessPipeline._pre_tts_rewrite_char_bounds(
        rewrite_label="overshoot",
        pre_chars=99,
        target_duration_ms=11_939,
        chars_per_second=4.088,
    )

    assert bounds == (60, 63)


def test_process_pipeline_rejects_short_high_shrink_rewrite_below_risk_floor() -> None:
    assert not ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=99,
        post_chars=54,
        target_duration_ms=11_939,
        chars_per_second=4.088,
    )
    assert ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=99,
        post_chars=60,
        target_duration_ms=11_939,
        chars_per_second=4.088,
    )


def test_process_pipeline_tightens_mid_undershoot_risk_pre_tts_bounds() -> None:
    bounds = ProcessPipeline._pre_tts_rewrite_char_bounds(
        rewrite_label="overshoot",
        pre_chars=89,
        target_duration_ms=16_898,
        chars_per_second=4.126,
    )

    assert bounds == (77, 83)


def test_process_pipeline_rejects_mid_undershoot_risk_rewrite_below_floor() -> None:
    assert not ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=89,
        post_chars=74,
        target_duration_ms=16_898,
        chars_per_second=4.126,
    )
    assert ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=89,
        post_chars=77,
        target_duration_ms=16_898,
        chars_per_second=4.126,
    )


def test_process_pipeline_tightens_long_undershoot_risk_pre_tts_bounds() -> None:
    bounds = ProcessPipeline._pre_tts_rewrite_char_bounds(
        rewrite_label="overshoot",
        pre_chars=127,
        target_duration_ms=28_183,
        chars_per_second=3.373,
    )

    assert bounds == (110, 122)


def test_process_pipeline_rejects_long_undershoot_risk_rewrite_below_floor() -> None:
    assert not ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=127,
        post_chars=103,
        target_duration_ms=28_183,
        chars_per_second=3.373,
    )
    assert ProcessPipeline._is_pre_tts_rewrite_within_char_guardrails(
        rewrite_label="overshoot",
        pre_chars=127,
        post_chars=110,
        target_duration_ms=28_183,
        chars_per_second=3.373,
    )


def test_process_pipeline_pre_rewrites_short_obvious_overshoot_before_tts() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=44,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=4_000,
        target_duration_ms=4_000,
        source_text="Okay.",
        cn_text="a" * 40,
    )
    observed: dict[str, object] = {}

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return "b" * 24

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert observed["args"] == ("a" * 40,)
    assert observed["kwargs"]["target_lower_chars"] == 24
    assert observed["kwargs"]["target_upper_chars"] == 26
    assert segment.cn_text == "b" * 24
    assert segment.pre_tts_rewrite_direction == "overshoot"


def test_process_pipeline_skips_short_low_overshoot_before_tts() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=45,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=6_000,
        target_duration_ms=6_000,
        source_text="Short source text",
        cn_text="a" * 30,
    )

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            raise AssertionError("short segments require a higher overshoot threshold")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "a" * 30


def test_process_pipeline_short_estimate_margin_catches_borderline_overshoot() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=47,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=6_000,
        target_duration_ms=6_000,
        source_text="Okay.",
        cn_text="a" * 35,
    )

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            return "b" * 27

        def rewrite_for_duration(self, *args, **kwargs):
            return "b" * 27

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert segment.cn_text == "b" * 27
    assert segment.pre_tts_estimate_ms == 7777


def test_process_pipeline_near_short_estimate_margin_catches_borderline_overshoot() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=48,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=9_500,
        target_duration_ms=9_500,
        source_text="Near short source text",
        cn_text="a" * 44,
    )

    class FakeRewriter:
        def rewrite_for_duration_with_profile(self, *args, **kwargs):
            return "b" * 38

        def rewrite_for_duration(self, *args, **kwargs):
            return "b" * 38

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.0,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 1
    assert segment.cn_text == "b" * 38
    assert segment.pre_tts_estimate_ms == 11000


def test_process_pipeline_skips_micro_segment_pre_tts_rewrite() -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=46,
        speaker_id="speaker_a",
        display_name="Interviewer",
        voice_id="voice_a",
        start_ms=0,
        end_ms=1_000,
        target_duration_ms=1_000,
        source_text="Yes.",
        cn_text="a" * 30,
    )

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            raise AssertionError("micro segments should not use pre-TTS rewrite")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "a" * 30


def test_process_pipeline_skips_pre_tts_rewrite_when_speed_can_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan-C+: when TTS speed can absorb the drift safely (within both the
    admin speed clamp AND a listen-comfort guardrail), pre-TTS rewrite is a
    wasted LLM call — skip it.

    Setup: 110 chars at 4.5 cps → estimate 24,444ms, target 20,000ms,
    ratio 1.222.  Old logic rewrites (>20% threshold).  New logic with
    unlimited mode (0.50-2.00) clamped by listen-limit 1.30 yields
    effective_max = 1.30, ratio 1.222 < 1.30 → skip rewrite.
    """
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=42,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="minimax",  # CodeX P1-2: skip only fires for speed-aware providers
    )
    # Force unlimited-mode clamp + admin flag ON.
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            raise AssertionError("rewrite must NOT be called when speed can handle")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )
    assert rewritten_count == 0
    assert segment.cn_text == "a" * 110  # untouched
    assert segment.rewrite_count == 0


def test_process_pipeline_pre_tts_rewrite_when_speed_cant_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan-C+: when ratio exceeds the listen-comfort guardrail (>1.30),
    even unlimited-mode speed can't handle it safely — rewrite must fire."""
    pipeline = ProcessPipeline()
    # 200 chars at 4.5 cps → estimate 44,444ms, target 30,000ms, ratio 1.481
    segment = DubbingSegment(
        segment_id=43,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=30_000,
        target_duration_ms=30_000,
        source_text="Original",
        cn_text="a" * 200,
        tts_provider="minimax",
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    rewrite_calls = {"n": 0}

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            rewrite_calls["n"] += 1
            return "b" * 160

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )
    assert rewritten_count == 1
    assert rewrite_calls["n"] == 1
    assert segment.cn_text == "b" * 160


def test_pre_tts_rewrite_skip_disabled_when_speed_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """CodeX P1-1: when tts_speed_adjustment_enabled is False, the
    speed-aware skip MUST NOT fire — speed won't actually run, so
    rewrite remains the only safety net for over/under-shoot."""
    pipeline = ProcessPipeline()
    # Ratio 1.222 (110 chars / 4.5 cps / 20s target); inside listen-limit but
    # speed is OFF, so we must still hit the legacy 20% threshold and rewrite.
    segment = DubbingSegment(
        segment_id=44,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="minimax",
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: False)  # ← OFF

    rewrite_calls = {"n": 0}

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            rewrite_calls["n"] += 1
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )
    # ratio 1.222 → overshoot 22.2% > 20% threshold → rewrite (skip disabled)
    assert rewritten_count == 1
    assert rewrite_calls["n"] == 1


def test_pre_tts_rewrite_skip_disabled_for_provider_without_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    """CodeX P1-2: skip is provider-gated. For any segment whose provider is
    NOT in ``SPEED_AWARE_TTS_PROVIDERS`` the skip must NOT fire — because
    speed won't actually run, so rewrite stays the only safety net.

    As of 2026-04-15 MiniMax + VolcEngine + CosyVoice all have speed wired,
    so this test uses a placeholder provider name that is guaranteed to stay
    outside the set; that way future additions don't silently break the
    negative assertion.
    """
    pipeline = ProcessPipeline()
    # Ratio 1.222 on a provider that has no speed knob — inside listen-limit
    # but must still hit the legacy 20% threshold and rewrite.
    segment = DubbingSegment(
        segment_id=45,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="mimo",  # ← MiMo Omni has no per-segment speed knob
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    rewrite_calls = {"n": 0}

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            rewrite_calls["n"] += 1
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )
    # ratio 1.222 → overshoot 22% > 20% → rewrite (skip provider-gated off)
    assert rewritten_count == 1
    assert rewrite_calls["n"] == 1


def test_pre_tts_rewrite_skip_falls_back_to_job_provider_when_segment_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """CodeX 2026-04-15 follow-up: single-engine VolcEngine jobs don't populate
    ``segment.tts_provider`` (``_speaker_providers`` stays empty), but the TTS
    runtime still routes the segment to VolcEngine via ``self._job_provider``.
    The skip must mirror that fallback or it under-fires on the common path.
    """
    pipeline = ProcessPipeline()
    # Same ratio 1.222 as the baseline skip test — inside listen-limit, so the
    # only thing gating skip is provider resolution.
    segment = DubbingSegment(
        segment_id=46,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="",  # ← single-engine job; no per-speaker override
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            raise AssertionError("rewrite must NOT fire when job_provider is speed-aware")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
        job_provider="volcengine",  # ← job-level provider populated here
    )
    assert rewritten_count == 0
    assert segment.cn_text == "a" * 110
    assert segment.rewrite_count == 0


def test_pre_tts_rewrite_skip_job_provider_without_speed_still_rewrites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative complement: a single-engine job on a provider without
    per-segment speed wiring must not produce a false skip — legacy rewrite
    path keeps running. Using the placeholder ``mimo`` provider (MiMo Omni
    has no per-segment speed knob) so this stays negative even if more
    providers gain speed support later."""
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=47,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="",
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    rewrite_calls = {"n": 0}

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            rewrite_calls["n"] += 1
            return "b" * 90

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
        job_provider="mimo",  # ← not in SPEED_AWARE_TTS_PROVIDERS
    )
    # ratio 1.222 → overshoot 22% > 20% threshold → rewrite (provider not speed-aware)
    assert rewritten_count == 1
    assert rewrite_calls["n"] == 1


def test_pre_tts_rewrite_skip_segment_provider_wins_over_job_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Segment-level override takes precedence over job-level provider, matching
    ``TTSGenerator._generate_one``'s resolution order.  Here: segment says
    minimax (speed-aware) but job says mimo (no speed knob) — skip fires
    because the segment override wins.
    """
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=48,
        speaker_id="speaker_a",
        display_name="x",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original",
        cn_text="a" * 110,
        tts_provider="minimax",  # ← per-speaker override (speed-aware)
    )
    from services.tts import speed_decision as _sd
    monkeypatch.setattr(_sd, "_get_speed_clamp", lambda: (0.50, 2.00))
    monkeypatch.setattr(_sd, "is_speed_adjustment_enabled", lambda: True)

    class FakeRewriter:
        def rewrite_for_duration(self, *args, **kwargs):
            raise AssertionError("rewrite must NOT fire — segment override is speed-aware")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
        job_provider="mimo",  # ← job-level (ignored because segment set)
    )
    assert rewritten_count == 0


def test_process_pipeline_skips_pre_tts_rewrite_below_overshoot_threshold() -> None:
    pipeline = ProcessPipeline()
    # 100 chars at 4.5 c/s = ~22222ms estimated, target 20000ms → ratio 0.111 < 0.20 threshold
    segment = DubbingSegment(
        segment_id=41,
        speaker_id="speaker_a",
        display_name="Dan Koe",
        voice_id="voice_a",
        start_ms=0,
        end_ms=20_000,
        target_duration_ms=20_000,
        source_text="Original source text",
        cn_text="a" * 100,
    )

    class FakeRewriter:
        def rewrite_for_duration(
            self,
            cn_text: str,
            actual_duration_ms: int,
            target_duration_ms: int,
            source_text: str = "",
            speaker_id: str | None = None,
        ) -> str:
            del cn_text, actual_duration_ms, target_duration_ms, source_text, speaker_id
            raise AssertionError("rewrite_for_duration should not be called below the pre-TTS threshold")

    rewritten_count = pipeline._pre_rewrite_obvious_overshoot_segments_before_tts(
        segments=[segment],
        rewriter=FakeRewriter(),  # type: ignore[arg-type]
        chars_per_second=4.5,
        chars_per_second_by_speaker={},
    )

    assert rewritten_count == 0
    assert segment.cn_text == "a" * 100
    assert segment.rewrite_count == 0
