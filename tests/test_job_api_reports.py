from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.jobs.api import build_job_api_server
from services.jobs.models import JobRecord
from services.jobs.process_runner import ProcessJobRunner
from services.jobs.service import JobService
from services.jobs.store import JobStore


def _spin_up_job_api(tmp_path: Path):
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *_a, **_kw: None,
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    return service, base_url, server, thread


def _save_job(store: JobStore, *, tmp_path: Path, job_id: str = "job_reports") -> Path:
    project_dir = tmp_path / "projects" / job_id
    (project_dir / "reports").mkdir(parents=True, exist_ok=True)
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref=f"https://yt.example/{job_id}",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="succeeded",
        current_stage="completed",
        progress_message="ok",
        created_at="2026-05-22T00:00:00+00:00",
        updated_at="2026-05-22T00:00:01+00:00",
        service_mode="studio",
        project_dir=str(project_dir.resolve(strict=False)),
    )
    store.save_job(record)
    return project_dir


def _request(method: str, url: str) -> tuple[int, str, str]:
    request = Request(url, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            return response.status, response.headers.get("Content-Type", ""), body
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, exc.headers.get("Content-Type", ""), body


def _request_json(method: str, url: str) -> tuple[int, dict[str, object]]:
    status, _content_type, body = _request(method, url)
    return status, json.loads(body)


def test_job_reports_catalog_and_fetch_are_job_scoped(tmp_path: Path) -> None:
    service, base_url, server, thread = _spin_up_job_api(tmp_path)
    del thread
    try:
        project_dir = _save_job(service.store, tmp_path=tmp_path)
        evidence_text = (
            '{"schema_version":"speaker_evidence_v1","line_id":"line-1"}\n'
        )
        (project_dir / "reports" / "speaker_evidence.jsonl").write_bytes(
            evidence_text.encode("utf-8")
        )
        (project_dir / "reports" / "subtitle_width_report.json").write_text(
            json.dumps(
                {"schema_version": "subtitle_width_report_v1", "issues": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (project_dir / "reports" / "translation_quality_report.json").write_text(
            json.dumps(
                {"schema_version": "translation_quality_report_v1", "issues": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status, catalog = _request_json("GET", f"{base_url}/jobs/job_reports/reports")
        assert status == 200
        by_name = {
            str(entry["name"]): entry
            for entry in catalog["reports"]  # type: ignore[index]
        }
        assert by_name["speaker-evidence"]["exists"] is True
        assert by_name["subtitle-width"]["exists"] is True
        assert by_name["translation-quality"]["exists"] is True
        assert by_name["speaker-evidence"]["filename"] == "speaker_evidence.jsonl"

        status, content_type, body = _request(
            "GET",
            f"{base_url}/jobs/job_reports/reports/speaker-evidence",
        )
        assert status == 200
        assert content_type.startswith("application/x-ndjson")
        assert body == evidence_text

        status, content_type, body = _request(
            "GET",
            f"{base_url}/jobs/job_reports/reports/subtitle_width",
        )
        assert status == 200
        assert content_type.startswith("application/json")
        assert json.loads(body)["schema_version"] == "subtitle_width_report_v1"

        status, content_type, body = _request(
            "GET",
            f"{base_url}/jobs/job_reports/reports/translation_quality",
        )
        assert status == 200
        assert content_type.startswith("application/json")
        assert json.loads(body)["schema_version"] == "translation_quality_report_v1"
    finally:
        server.shutdown()
        server.server_close()


def test_job_reports_return_404_for_unknown_or_missing_report(tmp_path: Path) -> None:
    service, base_url, server, thread = _spin_up_job_api(tmp_path)
    del thread
    try:
        _save_job(service.store, tmp_path=tmp_path)

        status, payload = _request_json(
            "GET",
            f"{base_url}/jobs/job_reports/reports/not-a-report",
        )
        assert status == 404
        assert payload["error"] == "unknown_report"

        status, payload = _request_json(
            "GET",
            f"{base_url}/jobs/job_reports/reports/speaker-evidence",
        )
        assert status == 404
        assert payload["error"] == "report_not_written"
        assert payload["report"] == "speaker-evidence"
    finally:
        server.shutdown()
        server.server_close()


def test_job_reports_do_not_join_artifacts_or_result_summary(tmp_path: Path) -> None:
    service, base_url, server, thread = _spin_up_job_api(tmp_path)
    del thread
    try:
        project_dir = _save_job(service.store, tmp_path=tmp_path)
        (project_dir / "reports" / "speaker_evidence.jsonl").write_bytes(
            b'{"schema_version":"speaker_evidence_v1","line_id":"line-1"}\n'
        )
        (project_dir / "reports" / "subtitle_width_report.json").write_text(
            json.dumps({"schema_version": "subtitle_width_report_v1"}),
            encoding="utf-8",
        )
        (project_dir / "reports" / "translation_quality_report.json").write_text(
            json.dumps({"schema_version": "translation_quality_report_v1"}),
            encoding="utf-8",
        )
        (project_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "artifact_index": {
                        "editor.subtitles": "output/subtitles.srt",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        for endpoint in ("artifacts", "result-summary"):
            status, payload = _request_json(
                "GET",
                f"{base_url}/jobs/job_reports/{endpoint}",
            )
            assert status == 200
            serialized = json.dumps(payload, ensure_ascii=False)
            assert "speaker_evidence" not in serialized
            assert "subtitle_width_report" not in serialized
            assert "translation_quality_report" not in serialized
            assert "/reports/" not in serialized.replace("\\", "/")
    finally:
        server.shutdown()
        server.server_close()
