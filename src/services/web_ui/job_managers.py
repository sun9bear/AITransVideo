from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from services import config_loader
from services.jobs.models import (
    ACTIVE_JOB_STATUSES,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
)

from .constants import (
    DOWNLOAD_PROGRESS_PATTERN,
    MAIN_PY_PATH,
    MAX_LOG_LINES,
    PROCESS_RUN_TIMEOUT_SECONDS,
    PROJECT_ROOT,
    WEB_UI_DEFAULT_JOB_API_BASE_URL,
)
from .models import ProcessJobSnapshot
from .utils import (
    _copy_optional_mapping,
    _normalize_optional_text,
    _parse_web_review_marker,
    _resolve_snapshot_status_from_log_line,
    _utc_timestamp,
)


class ProcessJobManager:
    """Legacy process-backed manager kept for compatibility and focused tests."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        config_path: Path | None = None,
        python_executable: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.project_root = (project_root or PROJECT_ROOT).resolve(strict=False)
        self.config_path = (config_path or config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH).resolve(
            strict=False
        )
        self.python_executable = python_executable or sys.executable
        self._popen_factory = popen_factory
        self._lock = threading.Lock()

        from .config_helpers import _load_selected_translation_model_alias

        self._snapshot = ProcessJobSnapshot(
            job_id=None,
            status="idle",
            youtube_url="",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            translation_model_alias=_load_selected_translation_model_alias(self.config_path),
            project_dir=None,
            current_stage=None,
            current_message="等待任务启动。",
            started_at=None,
            completed_at=None,
            returncode=None,
            logs=[],
            review_gate=None,
        )
        self._process: subprocess.Popen[str] | None = None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot.to_dict()

    def start_job(
        self,
        *,
        youtube_url: str,
        speakers: str,
        voice_a: str | None,
        voice_b: str | None,
        translation_model_alias: str,
        project_dir: str | None = None,
        transcription_method: str | None = None,
    ) -> dict[str, object]:
        normalized_url = youtube_url.strip()
        normalized_speakers = speakers.strip().lower()
        normalized_voice_a = (voice_a or "").strip() or None
        normalized_voice_b = (voice_b or "").strip() or None
        normalized_alias = translation_model_alias.strip()
        normalized_project_dir = (project_dir or "").strip() or None
        normalized_transcription_method = (transcription_method or "").strip().lower() or "assemblyai"

        if not normalized_url:
            raise ValueError("YouTube URL 不能为空。")
        if normalized_speakers not in {"auto", "1", "2"}:
            raise ValueError("说话人设置只支持 auto、1、2。")

        from .config_helpers import build_translation_model_options, set_translation_primary_model

        available_aliases = {
            option["alias"] for option in build_translation_model_options(config_path=self.config_path)
        }
        if normalized_alias not in available_aliases:
            raise ValueError(f"未知翻译模型别名：{normalized_alias}")

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("已有任务正在运行，请等待当前任务完成。")

            updated_route = set_translation_primary_model(
                normalized_alias,
                config_path=self.config_path,
            )
            job_id = uuid4().hex
            self._snapshot = ProcessJobSnapshot(
                job_id=job_id,
                status="running",
                youtube_url=normalized_url,
                speakers=normalized_speakers,
                voice_a=normalized_voice_a,
                voice_b=normalized_voice_b,
                translation_model_alias=normalized_alias,
                project_dir=normalized_project_dir,
                current_stage="S0",
                current_message=(
                    f"准备启动处理流程，S3 当前主模型：{updated_route[0]}"
                ),
                started_at=_utc_timestamp(),
                completed_at=None,
                returncode=None,
                logs=[],
                review_gate=None,
            )

        command = [
            self.python_executable,
            "-u",
            str(MAIN_PY_PATH),
            "process",
            normalized_url,
            "--speakers",
            normalized_speakers,
            "--wait-for-review",
        ]
        if normalized_voice_a is not None:
            command.extend(["--voice-a", normalized_voice_a])
        if normalized_voice_b is not None:
            command.extend(["--voice-b", normalized_voice_b])
        if normalized_project_dir is not None:
            command.extend(["--project-dir", normalized_project_dir])
        if normalized_transcription_method and normalized_transcription_method != "assemblyai":
            command.extend(["--transcription-method", normalized_transcription_method])
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
            with self._lock:
                self._snapshot.status = "failed"
                self._snapshot.current_message = f"启动失败：{exc}"
                self._snapshot.completed_at = _utc_timestamp()
                self._snapshot.returncode = -1
            raise ValueError(f"无法启动处理进程：{exc}") from exc

        with self._lock:
            self._process = process

        monitor = threading.Thread(
            target=self._monitor_process,
            args=(job_id, process),
            name=f"web-ui-process-{job_id[:8]}",
            daemon=True,
        )
        monitor.start()
        return self.snapshot()

    def continue_after_review(self, *, expected_stage: str | None = None) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot
            if snapshot.status != "waiting_for_review":
                raise ValueError("当前没有等待人工确认的任务。")
            active_review = dict(snapshot.review_gate or {})
            if expected_stage is not None and active_review.get("stage") != expected_stage:
                raise ValueError("待继续的确认阶段与当前任务不一致。")
            youtube_url = snapshot.youtube_url
            speakers = snapshot.speakers
            voice_a = snapshot.voice_a
            voice_b = snapshot.voice_b
            translation_model_alias = snapshot.translation_model_alias
            project_dir = snapshot.project_dir

        return self.start_job(
            youtube_url=youtube_url,
            speakers=speakers,
            voice_a=voice_a,
            voice_b=voice_b,
            translation_model_alias=translation_model_alias,
            project_dir=project_dir,
        )

    def cancel_waiting_review(self, *, expected_stage: str | None = None) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot
            if snapshot.status != "waiting_for_review":
                raise ValueError("当前没有等待人工确认的任务。")
            active_review = dict(snapshot.review_gate or {})
            if expected_stage is not None and active_review.get("stage") != expected_stage:
                raise ValueError("待取消的确认阶段与当前任务不一致。")
            next_logs = list(snapshot.logs or [])
            next_logs.append("[WEB] 已取消等待人工确认的任务。")
            self._snapshot = ProcessJobSnapshot(
                job_id=snapshot.job_id,
                status="cancelled",
                youtube_url=snapshot.youtube_url,
                speakers=snapshot.speakers,
                voice_a=snapshot.voice_a,
                voice_b=snapshot.voice_b,
                translation_model_alias=snapshot.translation_model_alias,
                project_dir=snapshot.project_dir,
                current_stage=snapshot.current_stage,
                current_message="任务已取消。",
                started_at=snapshot.started_at,
                completed_at=_utc_timestamp(),
                returncode=snapshot.returncode,
                logs=next_logs,
                review_gate=None,
            )
            return self._snapshot.to_dict()

    def stop_job(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            job_id = self._snapshot.job_id
            if process is None or process.poll() is not None or job_id is None:
                raise ValueError("当前没有正在运行的任务。")
            self._snapshot.status = "stopping"
            self._snapshot.current_message = "正在停止任务..."
            process.terminate()
            self._append_log_unlocked(job_id, "[WEB] 正在请求停止任务...")
        return self.snapshot()

    def _monitor_process(self, job_id: str, process: subprocess.Popen[str]) -> None:
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.rstrip()
                    if not line:
                        continue
                    self._record_line(job_id, line)
            returncode = process.wait(timeout=PROCESS_RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
            self._record_line(job_id, "[WEB] 任务超时，已强制终止。")

        with self._lock:
            if self._snapshot.job_id != job_id:
                self._process = None
                return
            waiting_for_review = self._snapshot.status == "waiting_for_review"
            status = (
                "waiting_for_review"
                if waiting_for_review and returncode == 0
                else ("succeeded" if returncode == 0 else "failed")
            )
            last_message = self._snapshot.current_message
            if returncode == 0 and not waiting_for_review:
                last_message = "处理完成。"
            elif not last_message or last_message.startswith("[WEB]"):
                last_message = "处理失败，请查看日志。"
            self._snapshot = ProcessJobSnapshot(
                job_id=self._snapshot.job_id,
                status=status,
                youtube_url=self._snapshot.youtube_url,
                speakers=self._snapshot.speakers,
                voice_a=self._snapshot.voice_a,
                voice_b=self._snapshot.voice_b,
                translation_model_alias=self._snapshot.translation_model_alias,
                project_dir=self._snapshot.project_dir,
                current_stage=self._snapshot.current_stage,
                current_message=last_message,
                started_at=self._snapshot.started_at,
                completed_at=_utc_timestamp(),
                returncode=returncode,
                logs=list(self._snapshot.logs),
                review_gate=dict(self._snapshot.review_gate or {}),
            )
            self._process = None

    def _record_line(self, job_id: str, line: str) -> None:
        with self._lock:
            if self._snapshot.job_id != job_id:
                return
            review_gate = _parse_web_review_marker(line)
            if review_gate is not None:
                self._append_log_unlocked(job_id, line)
                self._snapshot.status = "waiting_for_review"
                self._snapshot.project_dir = str(review_gate.get("project_dir") or "").strip() or self._snapshot.project_dir
                self._snapshot.review_gate = review_gate
                self._snapshot.current_stage = str(review_gate.get("stage") or self._snapshot.current_stage or "")
                self._snapshot.current_message = str(review_gate.get("message") or self._snapshot.current_message or "")
                return
            current_stage, current_message = _resolve_snapshot_status_from_log_line(
                line=line,
                current_stage=self._snapshot.current_stage,
                current_message=self._snapshot.current_message,
            )
            self._append_log_unlocked(job_id, line)
            self._snapshot.current_stage = current_stage
            self._snapshot.current_message = current_message

    def _append_log_unlocked(self, job_id: str, line: str) -> None:
        if self._snapshot.job_id != job_id:
            return
        logs = deque(self._snapshot.logs, maxlen=MAX_LOG_LINES)
        if DOWNLOAD_PROGRESS_PATTERN.match(line):
            if logs and DOWNLOAD_PROGRESS_PATTERN.match(logs[-1]):
                logs[-1] = line
            else:
                logs.append(line)
            self._snapshot.logs = list(logs)
            return
        logs.append(line)
        self._snapshot.logs = list(logs)


class JobAPIRequestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _read_http_error_message(exc: HTTPError) -> str:
    try:
        raw_body = exc.read()
    except OSError:
        raw_body = b""

    if raw_body:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            text_body = raw_body.decode("utf-8", errors="replace").strip()
            if text_body:
                return text_body
        else:
            if isinstance(payload, dict):
                for key in ("error", "message", "detail"):
                    value = _normalize_optional_text(payload.get(key))
                    if value is not None:
                        return value

    reason = _normalize_optional_text(getattr(exc, "reason", None))
    if reason is not None:
        return reason
    return f"HTTP {exc.code}"


class JobAPIBackedJobManager:
    """Thin Web UI shell over the A1 Job API.

    UI-only fields like selected model and optional voice ids remain cached here,
    while job status, logs, and continue semantics come from the Job API.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        config_path: Path | None = None,
        job_api_base_url: str = WEB_UI_DEFAULT_JOB_API_BASE_URL,
        request_json: Callable[[str, str, dict[str, object] | None], dict[str, object]] | None = None,
    ) -> None:
        self.project_root = (project_root or PROJECT_ROOT).resolve(strict=False)
        self.config_path = (config_path or config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH).resolve(
            strict=False
        )
        self.job_api_base_url = job_api_base_url.rstrip("/")
        self._request_json = request_json or self._request_json_via_http
        self._lock = threading.Lock()
        self._ignored_job_ids: set[str] = set()

        from .config_helpers import _load_selected_translation_model_alias

        self._snapshot = ProcessJobSnapshot(
            job_id=None,
            status="idle",
            youtube_url="",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            translation_model_alias=_load_selected_translation_model_alias(self.config_path),
            project_dir=None,
            current_stage=None,
            current_message="等待任务启动。",
            started_at=None,
            completed_at=None,
            returncode=None,
            logs=[],
            review_gate=None,
            control_mode="job_api",
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            current_snapshot = self._snapshot
            tracked_job_id = current_snapshot.job_id
            if current_snapshot.status == "cancelled":
                return current_snapshot.to_dict()

        if tracked_job_id is None or current_snapshot.status not in ACTIVE_JOB_STATUSES:
            discovered_job_id = self._discover_job_id()
            if discovered_job_id is not None:
                tracked_job_id = discovered_job_id

        if tracked_job_id is None:
            return current_snapshot.to_dict()

        try:
            job_payload = self._request_json("GET", f"/jobs/{tracked_job_id}", None)
            logs_payload = self._request_json("GET", f"/jobs/{tracked_job_id}/logs", None)
        except JobAPIRequestError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                with self._lock:
                    if self._snapshot.job_id == tracked_job_id:
                        self._snapshot = self._replace_snapshot(
                            self._snapshot,
                            job_id=None,
                            status="idle",
                            current_stage=None,
                            current_message="当前未找到对应的 Job 记录。",
                            started_at=None,
                            completed_at=None,
                            project_dir=None,
                            review_gate=None,
                            logs=[],
                        )
                    return self._snapshot.to_dict()
            return self._snapshot_with_error(exc.message)
        except ConnectionError as exc:
            return self._snapshot_with_error(str(exc))

        next_snapshot = self._snapshot_from_job_payload(job_payload=job_payload, logs_payload=logs_payload)
        with self._lock:
            self._snapshot = next_snapshot
            return next_snapshot.to_dict()

    def start_job(
        self,
        *,
        youtube_url: str,
        speakers: str,
        voice_a: str | None,
        voice_b: str | None,
        translation_model_alias: str,
        project_dir: str | None = None,
    ) -> dict[str, object]:
        del project_dir
        normalized_url = youtube_url.strip()
        normalized_speakers = speakers.strip().lower()
        normalized_voice_a = (voice_a or "").strip() or None
        normalized_voice_b = (voice_b or "").strip() or None
        normalized_alias = translation_model_alias.strip()

        if not normalized_url:
            raise ValueError("YouTube URL 不能为空。")
        if normalized_speakers not in {"auto", "1", "2"}:
            raise ValueError("说话人设置只支持 auto、1、2。")

        from .config_helpers import build_translation_model_options, set_translation_primary_model

        available_aliases = {
            option["alias"] for option in build_translation_model_options(config_path=self.config_path)
        }
        if normalized_alias not in available_aliases:
            raise ValueError(f"未知翻译模型别名：{normalized_alias}")

        set_translation_primary_model(normalized_alias, config_path=self.config_path)

        try:
            job_payload = self._request_json(
                "POST",
                "/jobs",
                {
                    "job_type": "localize_video",
                    "source": {
                        "type": "youtube_url",
                        "value": normalized_url,
                    },
                    "output_target": "editor",
                    "speakers": normalized_speakers,
                    "voice_a": normalized_voice_a,
                    "voice_b": normalized_voice_b,
                },
            )
        except JobAPIRequestError as exc:
            if exc.status_code == HTTPStatus.CONFLICT:
                blocking_snapshot = self._refresh_snapshot_after_submit_conflict()
                if blocking_snapshot is not None:
                    blocking_status = str(blocking_snapshot.get("status") or "").strip()
                    if blocking_status == JOB_STATUS_WAITING_FOR_REVIEW:
                        raise ValueError("当前已有任务等待确认，请先完成确认后再继续。") from exc
                    if blocking_status in {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}:
                        raise ValueError("当前已有任务正在运行，请等待完成后再提交新任务。") from exc
            raise ValueError(exc.message) from exc
        except ConnectionError as exc:
            raise ValueError(str(exc)) from exc

        with self._lock:
            self._ignored_job_ids.clear()
            self._snapshot = self._replace_snapshot(
                self._snapshot,
                job_id=_normalize_optional_text(job_payload.get("job_id")),
                status=str(job_payload.get("status") or "queued"),
                youtube_url=normalized_url,
                speakers=normalized_speakers,
                voice_a=normalized_voice_a,
                voice_b=normalized_voice_b,
                translation_model_alias=normalized_alias,
                current_stage=_normalize_optional_text(job_payload.get("current_stage")),
                current_message=_normalize_optional_text(job_payload.get("progress_message")) or "任务已提交。",
                started_at=_normalize_optional_text(job_payload.get("started_at")),
                completed_at=_normalize_optional_text(job_payload.get("completed_at")),
                project_dir=_normalize_optional_text(job_payload.get("project_dir")),
                review_gate=_copy_optional_mapping(job_payload.get("review_gate")),
                logs=[],
            )
        return self.snapshot()

    def continue_after_review(self, *, expected_stage: str | None = None) -> dict[str, object]:
        snapshot = self.snapshot()
        if snapshot.get("status") != JOB_STATUS_WAITING_FOR_REVIEW:
            raise ValueError("当前没有等待人工确认的任务。")
        active_review = dict(snapshot.get("review_gate") or {})
        if expected_stage is not None and active_review.get("stage") != expected_stage:
            raise ValueError("待继续的确认阶段与当前任务不一致。")
        job_id = _normalize_optional_text(snapshot.get("job_id"))
        if job_id is None:
            raise ValueError("当前没有可继续的 job_id。")

        try:
            self._request_json("POST", f"/jobs/{job_id}/continue", {})
        except JobAPIRequestError as exc:
            raise ValueError(exc.message) from exc
        except ConnectionError as exc:
            raise ValueError(str(exc)) from exc
        return self.snapshot()

    def cancel_waiting_review(self, *, expected_stage: str | None = None) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot
            if snapshot.status != JOB_STATUS_WAITING_FOR_REVIEW:
                raise ValueError("当前没有等待人工确认的任务。")
            active_review = dict(snapshot.review_gate or {})
            if expected_stage is not None and active_review.get("stage") != expected_stage:
                raise ValueError("待取消的确认阶段与当前任务不一致。")
            next_logs = list(snapshot.logs or [])
            next_logs.append("[WEB] 已取消等待人工确认的任务。")
            if snapshot.job_id:
                self._ignored_job_ids.add(snapshot.job_id)
            self._snapshot = ProcessJobSnapshot(
                job_id=snapshot.job_id,
                status="cancelled",
                youtube_url=snapshot.youtube_url,
                speakers=snapshot.speakers,
                voice_a=snapshot.voice_a,
                voice_b=snapshot.voice_b,
                translation_model_alias=snapshot.translation_model_alias,
                project_dir=snapshot.project_dir,
                current_stage=snapshot.current_stage,
                current_message="任务已取消。",
                started_at=snapshot.started_at,
                completed_at=_utc_timestamp(),
                returncode=None,
                logs=next_logs,
                review_gate=None,
                control_mode="job_api",
            )
            return self._snapshot.to_dict()

    def stop_job(self) -> dict[str, object]:
        raise ValueError("当前 Web UI 已切到 Job API 模式，A2 不支持 stop。")

    def _discover_job_id(self) -> str | None:
        try:
            payload = self._request_json("GET", "/jobs", None)
        except (JobAPIRequestError, ConnectionError):
            return None
        jobs_payload = payload.get("jobs")
        if not isinstance(jobs_payload, list):
            return None

        for raw_job in jobs_payload:
            if not isinstance(raw_job, dict):
                continue
            job_id = _normalize_optional_text(raw_job.get("job_id"))
            if job_id is None or job_id in self._ignored_job_ids:
                continue
            if str(raw_job.get("status") or "").strip() in ACTIVE_JOB_STATUSES:
                return job_id

        for raw_job in jobs_payload:
            if not isinstance(raw_job, dict):
                continue
            job_id = _normalize_optional_text(raw_job.get("job_id"))
            if job_id is not None and job_id not in self._ignored_job_ids:
                return job_id
        return None

    def _refresh_snapshot_after_submit_conflict(self) -> dict[str, object] | None:
        blocking_job_id = self._discover_job_id()
        if blocking_job_id is None:
            return None
        try:
            job_payload = self._request_json("GET", f"/jobs/{blocking_job_id}", None)
            logs_payload = self._request_json("GET", f"/jobs/{blocking_job_id}/logs", None)
        except (JobAPIRequestError, ConnectionError):
            return None

        next_snapshot = self._snapshot_from_job_payload(
            job_payload=job_payload,
            logs_payload=logs_payload,
        )
        with self._lock:
            self._snapshot = next_snapshot
            return next_snapshot.to_dict()

    def _snapshot_from_job_payload(
        self,
        *,
        job_payload: dict[str, object],
        logs_payload: dict[str, object],
    ) -> ProcessJobSnapshot:
        with self._lock:
            current_snapshot = self._snapshot

        raw_lines = logs_payload.get("lines")
        log_lines = [str(line) for line in raw_lines] if isinstance(raw_lines, list) else []
        progress_message = _normalize_optional_text(job_payload.get("progress_message"))
        current_stage = _normalize_optional_text(job_payload.get("current_stage"))
        if current_stage == "ingestion" and progress_message and progress_message.startswith("Downloading: "):
            progress_message = "下载中：" + progress_message.removeprefix("Downloading: ").strip()

        return ProcessJobSnapshot(
            job_id=_normalize_optional_text(job_payload.get("job_id")),
            status=str(job_payload.get("status") or current_snapshot.status or "idle"),
            youtube_url=_normalize_optional_text(job_payload.get("source_ref")) or current_snapshot.youtube_url,
            speakers=_normalize_optional_text(job_payload.get("speakers")) or current_snapshot.speakers,
            voice_a=(
                _normalize_optional_text(job_payload.get("voice_a"))
                if "voice_a" in job_payload
                else current_snapshot.voice_a
            ),
            voice_b=(
                _normalize_optional_text(job_payload.get("voice_b"))
                if "voice_b" in job_payload
                else current_snapshot.voice_b
            ),
            translation_model_alias=current_snapshot.translation_model_alias,
            project_dir=_normalize_optional_text(job_payload.get("project_dir")) or current_snapshot.project_dir,
            current_stage=current_stage,
            current_message=progress_message,
            started_at=_normalize_optional_text(job_payload.get("started_at")) or current_snapshot.started_at,
            completed_at=_normalize_optional_text(job_payload.get("completed_at")),
            returncode=None,
            logs=log_lines,
            review_gate=_copy_optional_mapping(job_payload.get("review_gate")),
            control_mode="job_api",
        )

    def _snapshot_with_error(self, message: str) -> dict[str, object]:
        with self._lock:
            self._snapshot = self._replace_snapshot(
                self._snapshot,
                current_message=message,
                control_mode="job_api",
            )
            return self._snapshot.to_dict()

    def _request_json_via_http(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        request_payload = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.job_api_base_url}{path}",
            data=request_payload,
            method=method,
        )
        request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urlopen(request, timeout=5) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_message = _read_http_error_message(exc)
            raise JobAPIRequestError(exc.code, error_message) from exc
        except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectionError(f"Job API unavailable at {self.job_api_base_url}: {exc}") from exc

        if not isinstance(response_payload, dict):
            raise ConnectionError("Job API returned a non-object JSON payload.")
        return response_payload

    @staticmethod
    def _replace_snapshot(snapshot: ProcessJobSnapshot, **updates: object) -> ProcessJobSnapshot:
        return ProcessJobSnapshot(
            job_id=str(updates.get("job_id", snapshot.job_id) or "").strip() or None,
            status=str(updates.get("status", snapshot.status) or snapshot.status),
            youtube_url=str(updates.get("youtube_url", snapshot.youtube_url) or ""),
            speakers=str(updates.get("speakers", snapshot.speakers) or "auto"),
            voice_a=_normalize_optional_text(updates.get("voice_a", snapshot.voice_a)),
            voice_b=_normalize_optional_text(updates.get("voice_b", snapshot.voice_b)),
            translation_model_alias=str(
                updates.get("translation_model_alias", snapshot.translation_model_alias) or ""
            ),
            project_dir=_normalize_optional_text(updates.get("project_dir", snapshot.project_dir)),
            current_stage=_normalize_optional_text(updates.get("current_stage", snapshot.current_stage)),
            current_message=_normalize_optional_text(updates.get("current_message", snapshot.current_message)),
            started_at=_normalize_optional_text(updates.get("started_at", snapshot.started_at)),
            completed_at=_normalize_optional_text(updates.get("completed_at", snapshot.completed_at)),
            returncode=updates.get("returncode", snapshot.returncode) if isinstance(updates.get("returncode", snapshot.returncode), int | type(None)) else snapshot.returncode,
            logs=list(updates.get("logs", snapshot.logs) or []),
            review_gate=_copy_optional_mapping(updates.get("review_gate", snapshot.review_gate)),
            control_mode=str(updates.get("control_mode", snapshot.control_mode) or snapshot.control_mode),
        )
