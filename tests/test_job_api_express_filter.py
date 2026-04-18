"""Express/Studio 输出内容分层：Job API 暴露层过滤测试。

覆盖 (见 docs/plans/2026-04-18-express-studio-output-filter-plan.md)：
- /artifacts: Express 只返 publish.dubbed_video + publish.dubbed_video_poster
- /download/{key}: Express 白名单拒非 publish.dubbed_video
- /stream/{kind}: Express 禁 audio
- /tts-segments-zip: Express 禁
- /materials-availability: Express 保持真实值（v3 关键契约）

测试直接向本地 JobStore 注入 JobRecord，避免走 ProcessJobRunner 的子进程链路。
"""
from __future__ import annotations

import json
import threading
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.jobs.api import build_job_api_server
from services.jobs.models import JobRecord
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------

def _start_server(tmp_path: Path):
    """Start a real HTTP server backed by JobStore. Project root is tmp_path,
    so project dirs MUST live under tmp_path/projects/ for download resolver
    to accept them."""
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("subprocess should not be spawned in filter tests"),
        ),
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    # Ensure projects root exists (resolver normalizes against it)
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return service, server, thread, base_url


def _project_dir(tmp_path: Path, name: str) -> Path:
    """Build a project dir under tmp_path/projects/ (resolver requirement)."""
    return tmp_path / "projects" / name


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _make_project_with_artifacts(
    project_dir: Path,
    *,
    has_video: bool = True,
    has_audio: bool = True,
    has_poster: bool = True,
    has_subtitles: bool = True,
    has_source: bool = True,
) -> None:
    """Build a project_dir with manifest.json + actual artifact files on disk."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "publish").mkdir(exist_ok=True)
    (project_dir / "editor").mkdir(exist_ok=True)

    artifact_index: dict[str, str] = {}

    if has_source:
        p = project_dir / "source.mp4"
        p.write_bytes(b"fake-source-mp4" * 4)
        artifact_index["source.original_video"] = str(p)
    if has_video:
        p = project_dir / "publish" / "dubbed_video.mp4"
        p.write_bytes(b"fake-dubbed-mp4" * 4)
        artifact_index["publish.dubbed_video"] = str(p)
    if has_poster:
        p = project_dir / "publish" / "dubbed_video_poster.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)  # minimal JPEG header
        artifact_index["publish.dubbed_video_poster"] = str(p)
    if has_audio:
        p = project_dir / "editor" / "dubbed_audio_complete.wav"
        p.write_bytes(b"RIFF" + b"x" * 100)
        artifact_index["editor.dubbed_audio_complete"] = str(p)
    if has_subtitles:
        p = project_dir / "editor" / "subtitles.srt"
        p.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
        artifact_index["editor.subtitles"] = str(p)
    # tts segments dir (for tts-segments-zip endpoint) — make a dummy dir
    tts_dir = project_dir / "tts"
    tts_dir.mkdir(exist_ok=True)
    aligned_wav = tts_dir / "seg0_aligned.wav"
    aligned_wav.write_bytes(b"RIFF" + b"x" * 100)

    # Write manifest
    manifest = {
        "project_id": project_dir.name,
        "manifest_version": 1,
        "artifact_index": artifact_index,
    }
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _inject_job(
    service: JobService,
    *,
    project_dir: Path,
    service_mode: str,
) -> str:
    """Insert a pre-built JobRecord directly into the store, bypassing submit_job."""
    now = _iso_now()
    job_id = f"job_test_{service_mode}_{project_dir.name}"
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=test",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="succeeded",
        current_stage="completed",
        progress_message=None,
        created_at=now,
        updated_at=now,
        completed_at=now,
        project_dir=str(project_dir),
        service_mode=service_mode,
    )
    service.store.save_job(record)
    return job_id


def _http_get_json(url: str, expect_status: int | None = None):
    try:
        with urlopen(Request(url, method="GET"), timeout=5) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace") or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
    if expect_status is not None:
        assert status == expect_status, f"expected {expect_status}, got {status}; body={body!r}"
    return status, body


def _http_get_raw(url: str):
    try:
        with urlopen(Request(url, method="GET"), timeout=5) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------------------
# /artifacts filter
# ---------------------------------------------------------------------------

class TestArtifactsEndpoint:
    def test_express_returns_only_publish_keys(self, tmp_path: Path) -> None:
        """Express job 的 /artifacts 只返 publish.dubbed_video +
        publish.dubbed_video_poster；editor.* 和 source.* 被过滤。"""
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_express_full")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            _, body = _http_get_json(f"{base_url}/jobs/{job_id}/artifacts", expect_status=200)
            returned = {a["key"] for a in body.get("artifacts", []) if a.get("exists")}
            assert returned == {"publish.dubbed_video", "publish.dubbed_video_poster"}
            assert "editor.dubbed_audio_complete" not in returned
            assert "editor.subtitles" not in returned
            assert "source.original_video" not in returned
        finally:
            server.shutdown()

    def test_studio_returns_all_keys(self, tmp_path: Path) -> None:
        """Studio job 的 /artifacts 不过滤，editor.* 和 source.* 都在."""
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_studio_full")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="studio")

            _, body = _http_get_json(f"{base_url}/jobs/{job_id}/artifacts", expect_status=200)
            returned = {a["key"] for a in body.get("artifacts", []) if a.get("exists")}
            # Studio 看得到至少这些（具体完整 key 集合取决于 manifest_reader，我们只断言非空交集）
            assert "publish.dubbed_video" in returned
            assert "editor.dubbed_audio_complete" in returned
            assert "editor.subtitles" in returned
        finally:
            server.shutdown()

    def test_express_old_job_without_video_returns_empty_publish_set(self, tmp_path: Path) -> None:
        """老 Express job 没 publish.dubbed_video，/artifacts 返回的 existing items
        中不应含 editor.* 或 source.*。VideoGenerationControl fallback 走
        /materials-availability 而非 /artifacts，因此 /artifacts 空集是可接受的。"""
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_express_no_video")
            _make_project_with_artifacts(project, has_video=False, has_poster=False)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            _, body = _http_get_json(f"{base_url}/jobs/{job_id}/artifacts", expect_status=200)
            returned = {a["key"] for a in body.get("artifacts", []) if a.get("exists")}
            assert "editor.dubbed_audio_complete" not in returned
            assert "editor.subtitles" not in returned
            assert "source.original_video" not in returned
            # 大概率为空集（publish.* 不存在，其他都被过滤）
            assert all(
                k in {"publish.dubbed_video", "publish.dubbed_video_poster"}
                for k in returned
            )
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# /download/{key} whitelist
# ---------------------------------------------------------------------------

class TestDownloadWhitelist:
    def test_express_rejects_editor_artifact(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_dl_express")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, body = _http_get_json(
                f"{base_url}/jobs/{job_id}/download/editor.dubbed_audio_complete",
            )
            assert status == 403
            err = body.get("error", "")
            assert "Express" in err or "不可下载" in err
        finally:
            server.shutdown()

    def test_express_rejects_source_video(self, tmp_path: Path) -> None:
        """v3 收敛：Express 不能下载 source.original_video。
        注：该 key 本就不在 PUBLIC_RESULT_DOWNLOAD_KEYS 全局白名单里，现有
        实现会返 400（而非 403）；测试断言 4xx 以覆盖'不可下载'这个语义。"""
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_dl_source")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_json(f"{base_url}/jobs/{job_id}/download/source.original_video")
            assert status in (400, 403), f"source.original_video 应被拒，得到 {status}"
        finally:
            server.shutdown()

    def test_express_allows_publish_video(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_dl_video_ok")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/download/publish.dubbed_video")
            assert status == 200
        finally:
            server.shutdown()

    def test_studio_allows_editor_artifact(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_dl_studio")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="studio")

            status, _ = _http_get_raw(
                f"{base_url}/jobs/{job_id}/download/editor.dubbed_audio_complete",
            )
            assert status == 200
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# /stream/{kind} + /tts-segments-zip
# ---------------------------------------------------------------------------

class TestStreamAndTtsZip:
    def test_express_stream_audio_rejected(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_stream_audio")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/stream/audio")
            assert status == 403
        finally:
            server.shutdown()

    def test_express_stream_video_allowed(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_stream_video")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/stream/video")
            assert status == 200
        finally:
            server.shutdown()

    def test_express_stream_poster_allowed(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_stream_poster")
            _make_project_with_artifacts(project, has_poster=True)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/stream/poster")
            assert status == 200
        finally:
            server.shutdown()

    def test_express_tts_segments_zip_rejected(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_tts_zip")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/tts-segments-zip")
            assert status == 403
        finally:
            server.shutdown()

    def test_studio_stream_audio_allowed(self, tmp_path: Path) -> None:
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_studio_audio")
            _make_project_with_artifacts(project)
            job_id = _inject_job(service, project_dir=project, service_mode="studio")

            status, _ = _http_get_raw(f"{base_url}/jobs/{job_id}/stream/audio")
            assert status == 200
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# /materials-availability contract (Task 4 — 保持真实值)
# ---------------------------------------------------------------------------

class TestMaterialsAvailabilityContract:
    def test_express_keeps_real_audio_value_for_video_fallback(self, tmp_path: Path) -> None:
        """v3 关键契约：Express job 的 dubbed_audio 保持真实值，避免前端
        ResultMediaCard 的 'hasVideo=false && hasAudio=true' fallback 被误屏蔽。

        回归参考：CodeX 评审 P1 #2。如果未来有人把此端点在 Express 下
        改成 false-mask，本测试立即红。
        """
        service, server, _, base_url = _start_server(tmp_path)
        try:
            project = _project_dir(tmp_path, "proj_avail_old_express")
            # 老 Express job：只有 editor.dubbed_audio_complete，没有
            # publish.dubbed_video。磁盘真实情况就是 hasVideo=false/hasAudio=true。
            _make_project_with_artifacts(
                project, has_video=False, has_poster=False, has_source=False,
            )
            job_id = _inject_job(service, project_dir=project, service_mode="express")

            _, body = _http_get_json(
                f"{base_url}/jobs/{job_id}/materials-availability", expect_status=200,
            )
            assert body.get("dubbed_audio") is True, (
                "Express 下 dubbed_audio 必须保持真实值 (True if file exists)，"
                "否则前端 VideoGenerationControl fallback 会因为 hasAudio=false 消失"
            )
            assert body.get("dubbed_video") is False
            # subtitles 也保持真实值 — UI 层决定展不展示，不在 availability 过滤
            assert body.get("subtitles_zh") is True

        finally:
            server.shutdown()
