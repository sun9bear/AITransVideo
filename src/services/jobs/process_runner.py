from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Callable

from core.exceptions import StateError
from modules.output.manifest_writer import ManifestWriter
from services.jobs.events import (
    EVENT_LEVEL_ERROR,
    EVENT_TYPE_LOG,
    EVENT_TYPE_STATUS,
    JobEvent,
)
from services.jobs.models import (
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JobRecord,
    STAGE_COMPLETED,
    STAGE_DRAFT,
    STAGE_FAILED,
    STAGE_INGESTION,
    STAGE_LEGACY_PROCESS_OUTPUT,
    STAGE_MEDIA_UNDERSTANDING,
    STAGE_SPEAKER_REVIEW,
    STAGE_TRANSLATION_REVIEW,
    STAGE_VOICE_REVIEW,
)
from services.jobs.store import JobStore
from services.manifest_reader import load_manifest_payload
from services.review_state import REVIEW_STATUS_APPROVED, ReviewStateManager
from services.state_manager import StateManager, utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAIN_PY_PATH = PROJECT_ROOT / "main.py"
# 分层超时：根据视频时长动态调整
TIMEOUT_TIERS = {
    "tier1": 2 * 3600,    # ≤30 分钟视频：2 小时
    "tier2": 6 * 3600,    # 30-120 分钟视频：6 小时
    "tier3": 8 * 3600,    # 120-180 分钟视频：8 小时
}

def get_timeout_for_duration(video_duration_min: float = 30.0) -> int:
    """根据视频时长返回合适的超时时间（秒）"""
    if video_duration_min <= 30:
        return TIMEOUT_TIERS["tier1"]
    elif video_duration_min <= 120:
        return TIMEOUT_TIERS["tier2"]
    else:
        return TIMEOUT_TIERS["tier3"]

PROCESS_RUN_TIMEOUT_SECONDS = TIMEOUT_TIERS["tier2"]  # 默认 6 小时（兼容旧任务）

STAGE_LOG_PATTERN = re.compile(r"^\[(S[0-9]+)\]\s*(.*)$")
DOWNLOAD_PROGRESS_PATTERN = re.compile(r"^\[download\]\s*(.+)$", re.IGNORECASE)
# A1 is validated in the current Windows local runtime, so stdout path inference
# intentionally looks for Windows-style absolute paths only.
WINDOWS_PATH_PATTERN = re.compile(r"([A-Za-z]:[\\/][^\r\n]+)")
STAGE_CODE_MAP = {
    "S0": STAGE_INGESTION,
    "S1": STAGE_MEDIA_UNDERSTANDING,
    "S2": STAGE_SPEAKER_REVIEW,
    "S3": STAGE_TRANSLATION_REVIEW,
    "S4": STAGE_DRAFT,
    "S5": STAGE_DRAFT,
    "S6": STAGE_LEGACY_PROCESS_OUTPUT,
}
INTERNAL_STAGE_MAP = {
    "ingestion": STAGE_INGESTION,
    "audio_preparation": STAGE_INGESTION,
    "media_understanding": STAGE_MEDIA_UNDERSTANDING,
    "speaker_review": STAGE_SPEAKER_REVIEW,
    "translation": STAGE_TRANSLATION_REVIEW,
    "translation_review": STAGE_TRANSLATION_REVIEW,
    "voice_review": STAGE_VOICE_REVIEW,
    "alignment": STAGE_DRAFT,
    "draft": STAGE_DRAFT,
    "legacy_process_output": STAGE_LEGACY_PROCESS_OUTPUT,
    "completed": STAGE_COMPLETED,
    "failed": STAGE_FAILED,
}


class ProcessJobRunner:
    def __init__(
        self,
        *,
        store: JobStore,
        project_root: Path | None = None,
        python_executable: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        run_timeout_seconds: int = PROCESS_RUN_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.project_root = (project_root or PROJECT_ROOT).resolve(strict=False)
        self.python_executable = python_executable or sys.executable
        self._popen_factory = popen_factory
        self.run_timeout_seconds = run_timeout_seconds
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def start(self, job: JobRecord, *, continue_existing: bool = False) -> JobRecord:
        timestamp = utc_now_iso()
        running_job = self._save_job(
            job,
            status=JOB_STATUS_RUNNING,
            current_stage=STAGE_INGESTION,
            progress_message="Starting process-backed localization job.",
            updated_at=timestamp,
            started_at=job.started_at or timestamp,
            completed_at=None,
            review_gate=None,
            error_summary=None,
        )
        self.store.append_event(
            running_job.job_id,
            JobEvent(
                job_id=running_job.job_id,
                event_type=EVENT_TYPE_STATUS,
                created_at=timestamp,
                stage=running_job.current_stage,
                status=running_job.status,
                message=running_job.progress_message,
            ),
        )

        command = self._build_command(running_job, continue_existing=continue_existing)
        process_env = os.environ.copy()
        process_env["PYTHONIOENCODING"] = "utf-8"
        process_env["PYTHONUTF8"] = "1"
        process_env["PYTHONUNBUFFERED"] = "1"

        try:
            process = self._popen_factory(
                command,
                cwd=str(self.project_root),
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            timestamp = utc_now_iso()
            failed_job = self._save_job(
                running_job,
                status=JOB_STATUS_FAILED,
                current_stage=STAGE_FAILED,
                progress_message=f"Failed to start process-backed job: {exc}",
                updated_at=timestamp,
                completed_at=timestamp,
                error_summary={
                    "stage": STAGE_FAILED,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            self.store.append_event(
                failed_job.job_id,
                JobEvent(
                    job_id=failed_job.job_id,
                    event_type=EVENT_TYPE_STATUS,
                    created_at=timestamp,
                    stage=failed_job.current_stage,
                    status=failed_job.status,
                    level=EVENT_LEVEL_ERROR,
                    message=failed_job.progress_message,
                ),
            )
            return failed_job

        with self._lock:
            self._processes[running_job.job_id] = process

        monitor = threading.Thread(
            target=self._monitor_process,
            args=(running_job.job_id, process),
            name=f"process-job-{running_job.job_id[:8]}",
            daemon=True,
        )
        monitor.start()
        return running_job

    def is_process_active(self, job_id: str) -> bool:
        with self._lock:
            process = self._processes.get(job_id)
            if process is None:
                return False
            return process.poll() is None

    def _build_command(self, job: JobRecord, *, continue_existing: bool) -> list[str]:
        command = [
            self.python_executable,
            "-u",
            str(MAIN_PY_PATH),
            "process",
            job.source_ref,
            "--speakers",
            job.speakers,
            "--wait-for-review",
        ]
        if job.voice_a:
            command.extend(["--voice-a", job.voice_a])
        if job.voice_b:
            command.extend(["--voice-b", job.voice_b])
        if continue_existing and job.project_dir:
            command.extend(["--project-dir", job.project_dir])
        if getattr(job, "transcription_method", None) and job.transcription_method != "assemblyai":
            command.extend(["--transcription-method", job.transcription_method])
        return command

    def _monitor_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        returncode = 0
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\r\n")
                    if not line:
                        continue
                    self._record_line(job_id, line)
            returncode = process.wait(timeout=self.run_timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill_process(process)
            returncode = process.wait()
            self._record_line(job_id, "[JOB] Process timed out and was terminated.")
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
        self._finalize_process(job_id, returncode)

    def _record_line(self, job_id: str, line: str) -> None:
        current_job = self.store.require_job(job_id)
        detected_project_dir = _parse_project_dir_from_line(line, self.project_root)
        review_gate = _parse_web_review_marker(line)
        if review_gate is not None:
            timestamp = utc_now_iso()
            review_stage = normalize_public_stage(review_gate.get("stage")) or STAGE_VOICE_REVIEW
            next_job = self._save_job(
                current_job,
                status=JOB_STATUS_WAITING_FOR_REVIEW,
                current_stage=review_stage,
                progress_message=_normalize_optional_text(review_gate.get("message"))
                or current_job.progress_message,
                updated_at=timestamp,
                project_dir=_normalize_optional_text(review_gate.get("project_dir"))
                or detected_project_dir
                or current_job.project_dir,
                review_gate={
                    "stage": review_stage,
                    "message": _normalize_optional_text(review_gate.get("message")),
                },
            )
            self.store.append_event(
                job_id,
                JobEvent(
                    job_id=job_id,
                    event_type=EVENT_TYPE_LOG,
                    created_at=timestamp,
                    stage=next_job.current_stage,
                    status=next_job.status,
                    message=line,
                    payload={"review_gate": dict(next_job.review_gate or {})},
                ),
            )
            self.store.append_event(
                job_id,
                JobEvent(
                    job_id=job_id,
                    event_type=EVENT_TYPE_STATUS,
                    created_at=timestamp,
                    stage=next_job.current_stage,
                    status=next_job.status,
                    message=next_job.progress_message,
                ),
            )
            return

        next_stage, next_message = _resolve_stage_from_log_line(
            line=line,
            current_stage=current_job.current_stage,
            current_message=current_job.progress_message,
        )
        timestamp = utc_now_iso()
        next_job = self._save_job(
            current_job,
            current_stage=next_stage,
            progress_message=next_message,
            updated_at=timestamp,
            project_dir=detected_project_dir or current_job.project_dir,
        )
        self.store.append_event(
            job_id,
            JobEvent(
                job_id=job_id,
                event_type=EVENT_TYPE_LOG,
                created_at=timestamp,
                stage=next_job.current_stage,
                status=next_job.status,
                message=line,
            ),
        )

    def _finalize_process(self, job_id: str, returncode: int) -> None:
        current_job = self.store.require_job(job_id)
        if current_job.status == JOB_STATUS_WAITING_FOR_REVIEW and returncode == 0:
            return

        resolved_project_dir = _resolve_job_project_dir(
            project_root=self.project_root,
            source_ref=current_job.source_ref,
            preferred_project_dir=current_job.project_dir,
        )
        manifest_path = _resolve_manifest_path(resolved_project_dir)
        fallback_summary = _resolve_fallback_summary(
            project_dir=resolved_project_dir,
            manifest_path=manifest_path,
        )

        if returncode == 0:
            timestamp = utc_now_iso()
            next_job = self._save_job(
                current_job,
                status=JOB_STATUS_SUCCEEDED,
                current_stage=STAGE_COMPLETED,
                progress_message="Job completed successfully.",
                updated_at=timestamp,
                completed_at=timestamp,
                project_dir=str(resolved_project_dir)
                if resolved_project_dir is not None
                else current_job.project_dir,
                manifest_path=manifest_path,
                fallback_summary=fallback_summary,
            )
            self.store.append_event(
                job_id,
                JobEvent(
                    job_id=job_id,
                    event_type=EVENT_TYPE_STATUS,
                    created_at=timestamp,
                    stage=next_job.current_stage,
                    status=next_job.status,
                    message=next_job.progress_message,
                ),
            )
            return

        error_summary = _resolve_error_summary(
            project_dir=resolved_project_dir,
            current_stage=current_job.current_stage,
            current_message=current_job.progress_message,
        )
        timestamp = utc_now_iso()
        next_job = self._save_job(
            current_job,
            status=JOB_STATUS_FAILED,
            current_stage=STAGE_FAILED,
            progress_message=error_summary.get("message")
            if isinstance(error_summary, dict)
            else current_job.progress_message,
            updated_at=timestamp,
            completed_at=timestamp,
            project_dir=str(resolved_project_dir)
            if resolved_project_dir is not None
            else current_job.project_dir,
            manifest_path=manifest_path,
            fallback_summary=fallback_summary,
            error_summary=error_summary,
        )
        self.store.append_event(
            job_id,
            JobEvent(
                job_id=job_id,
                event_type=EVENT_TYPE_STATUS,
                created_at=timestamp,
                stage=next_job.current_stage,
                status=next_job.status,
                level=EVENT_LEVEL_ERROR,
                message=next_job.progress_message,
                payload={"error_summary": dict(next_job.error_summary or {})},
            ),
        )

    def _save_job(self, record: JobRecord, **updates: object) -> JobRecord:
        next_record = replace(record, **updates)
        self.store.save_job(next_record)
        return next_record

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()


def normalize_public_stage(raw_stage: object) -> str | None:
    normalized_stage = _normalize_optional_text(raw_stage)
    if normalized_stage is None:
        return None
    if normalized_stage in INTERNAL_STAGE_MAP:
        return INTERNAL_STAGE_MAP[normalized_stage]
    if normalized_stage in STAGE_CODE_MAP:
        return STAGE_CODE_MAP[normalized_stage]
    return None


def is_review_stage_approved(project_dir: str, stage_name: str) -> bool:
    review_state_path = Path(project_dir).expanduser().resolve(strict=False) / "review_state.json"
    if not review_state_path.exists():
        return False
    review_state_manager = ReviewStateManager(review_state_path)
    try:
        stage_payload = review_state_manager.get_stage(stage_name)
    except StateError:
        return False
    if not isinstance(stage_payload, dict):
        return False
    return _normalize_optional_text(stage_payload.get("status")) == REVIEW_STATUS_APPROVED


def _resolve_stage_from_log_line(
    *,
    line: str,
    current_stage: str | None,
    current_message: str | None,
) -> tuple[str | None, str | None]:
    stage_match = STAGE_LOG_PATTERN.match(line)
    if stage_match:
        stage_code = stage_match.group(1)
        stage_message = stage_match.group(2).strip() or current_message
        return normalize_public_stage(stage_code), stage_message

    download_match = DOWNLOAD_PROGRESS_PATTERN.match(line)
    if download_match:
        download_message = download_match.group(1).strip()
        return STAGE_INGESTION, f"Downloading: {download_message}" if download_message else current_message

    normalized_line = line.strip()
    if current_stage is not None:
        return current_stage, normalized_line or current_message
    return None, normalized_line or current_message


def _parse_web_review_marker(line: str) -> dict[str, object] | None:
    normalized_line = line.strip()
    if not normalized_line.startswith("[WEB_REVIEW]"):
        return None

    raw_payload = normalized_line.removeprefix("[WEB_REVIEW]").strip()
    if not raw_payload:
        return None

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_project_dir_from_line(line: str, project_root: Path) -> str | None:
    path_candidates = WINDOWS_PATH_PATTERN.findall(line)
    if not path_candidates:
        return None

    normalized_candidate = _normalize_path_text(path_candidates[-1], project_root)
    if normalized_candidate is None:
        return None

    candidate_path = Path(normalized_candidate).resolve(strict=False)
    if candidate_path.name.lower() == "output":
        return str(candidate_path.parent)
    return str(candidate_path)


def _normalize_path_text(raw_path: object, project_root: Path) -> str | None:
    normalized_path = _normalize_optional_text(raw_path)
    if normalized_path is None:
        return None
    candidate = Path(normalized_path).expanduser()
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve(strict=False)
    else:
        candidate = candidate.resolve(strict=False)
    return str(candidate)


def _resolve_job_project_dir(
    *,
    project_root: Path,
    source_ref: str,
    preferred_project_dir: str | None,
) -> Path | None:
    if preferred_project_dir:
        candidate = Path(preferred_project_dir).expanduser().resolve(strict=False)
        if candidate.exists():
            return candidate

    projects_root = (project_root / "projects").resolve(strict=False)
    if not projects_root.exists():
        return None

    matching_projects: list[tuple[float, Path]] = []
    for candidate in projects_root.iterdir():
        if not candidate.is_dir():
            continue
        metadata_path = candidate / "download_metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cached_url = str(metadata.get("url") or "").strip()
        if cached_url != source_ref:
            continue

        project_state_path = candidate / "project_state.json"
        manifest_path = candidate / "manifest.json"
        ordering_mtime = max(
            metadata_path.stat().st_mtime,
            project_state_path.stat().st_mtime if project_state_path.exists() else 0.0,
            manifest_path.stat().st_mtime if manifest_path.exists() else 0.0,
            candidate.stat().st_mtime,
        )
        matching_projects.append((ordering_mtime, candidate.resolve(strict=False)))

    if not matching_projects:
        return None
    matching_projects.sort(key=lambda item: item[0], reverse=True)
    return matching_projects[0][1]


def _resolve_manifest_path(project_dir: Path | None) -> str | None:
    if project_dir is None:
        return None

    project_state_path = (project_dir / "project_state.json").resolve(strict=False)
    if project_state_path.exists():
        stage_snapshot = StateManager(str(project_state_path)).load().get("stages", {})
        if isinstance(stage_snapshot, dict):
            legacy_output = stage_snapshot.get(STAGE_LEGACY_PROCESS_OUTPUT)
            if isinstance(legacy_output, dict):
                payload = legacy_output.get("payload", {})
                if isinstance(payload, dict):
                    raw_manifest_path = _normalize_optional_text(payload.get("manifest_path"))
                    if raw_manifest_path:
                        candidate = Path(raw_manifest_path).expanduser()
                        if not candidate.is_absolute():
                            candidate = (project_dir / candidate).resolve(strict=False)
                        else:
                            candidate = candidate.resolve(strict=False)
                        if candidate.exists():
                            return str(candidate)

    manifest_path = (project_dir / "manifest.json").resolve(strict=False)
    if manifest_path.exists():
        return str(manifest_path)
    return None


def _resolve_fallback_summary(
    *,
    project_dir: Path | None,
    manifest_path: str | None,
) -> dict[str, object] | None:
    if manifest_path is not None:
        manifest_payload = load_manifest_payload(manifest_path=manifest_path)
        if isinstance(manifest_payload, dict):
            fallback_summary = manifest_payload.get("fallback_summary")
            if isinstance(fallback_summary, dict) and fallback_summary:
                return dict(fallback_summary)

    if project_dir is None:
        return None

    project_state_path = (project_dir / "project_state.json").resolve(strict=False)
    if not project_state_path.exists():
        return None
    stage_snapshot = StateManager(str(project_state_path)).load().get("stages", {})
    if not isinstance(stage_snapshot, dict):
        return None
    fallback_summary = ManifestWriter._build_fallback_summary(stage_snapshot)
    return dict(fallback_summary) if fallback_summary else None


def _resolve_error_summary(
    *,
    project_dir: Path | None,
    current_stage: str | None,
    current_message: str | None,
) -> dict[str, object]:
    if project_dir is not None:
        project_state_path = (project_dir / "project_state.json").resolve(strict=False)
        if project_state_path.exists():
            state_snapshot = StateManager(str(project_state_path)).load()
            stage_snapshot = state_snapshot.get("stages", {})
            if isinstance(stage_snapshot, dict):
                failed_stage_name: str | None = None
                failed_stage_payload: dict[str, object] | None = None
                failed_stage_updated_at = ""
                for stage_name, stage_data in stage_snapshot.items():
                    if not isinstance(stage_data, dict):
                        continue
                    if stage_data.get("status") != "failed":
                        continue
                    updated_at = str(stage_data.get("updated_at") or "")
                    if failed_stage_name is None or updated_at >= failed_stage_updated_at:
                        failed_stage_name = stage_name
                        failed_stage_payload = stage_data
                        failed_stage_updated_at = updated_at
                if failed_stage_name is not None and failed_stage_payload is not None:
                    payload = failed_stage_payload.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}
                    error_type = _normalize_optional_text(payload.get("error_type")) or "process_failed"
                    message = (
                        _normalize_optional_text(failed_stage_payload.get("error_message"))
                        or current_message
                        or "Job failed."
                    )
                    return {
                        "stage": normalize_public_stage(failed_stage_name) or STAGE_FAILED,
                        "error_type": error_type,
                        "message": message,
                    }

    return {
        "stage": current_stage or STAGE_FAILED,
        "error_type": "process_failed",
        "message": current_message or "Job failed.",
    }


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized_value = str(value).strip()
    return normalized_value or None
