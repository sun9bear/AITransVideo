from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4
import webbrowser

from core.exceptions import StateError
from services import config_loader
from services.gemini.translator import (
    TranslationError,
    get_effective_rewrite_prompt_template,
    get_effective_speaker_infer_prompt_template,
    get_effective_translation_prompt_template,
    validate_rewrite_prompt_template,
    validate_speaker_infer_prompt_template,
    validate_translation_prompt_template,
)
import services.llm.router as llm_router_module
from services.review_state import (
    REVIEW_STAGE_TAB_MAP,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_SKIPPED,
    SPEAKER_REVIEW_STAGE,
    TRANSLATION_CONFIG_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    VOICE_REVIEW_STAGE,
    ReviewStateManager,
    utc_now_iso,
)
from services.llm.router import (
    DEFAULT_DEFAULT_LLM_ALIAS,
    DEFAULT_LLM_FALLBACKS,
    S3_SYNCED_TASKS,
    load_llm_fallback_config,
)
from services.project_state_summary import (
    build_empty_project_state_summary,
    build_project_state_summary,
)
from services.manifest_reader import (
    load_manifest_artifact_index,
    load_manifest_payload,
    resolve_manifest_artifact_path,
)
from services.jobs.api import JOB_API_DEFAULT_HOST, JOB_API_DEFAULT_PORT
from services.jobs.models import (
    ACTIVE_JOB_STATUSES,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
)
from services.source_context_summary import (
    build_empty_source_context_summary,
    build_source_context_summary,
)
from services.state_manager import StateManager
from services.voice_registry import SpeakerVoiceProfile, VoiceRegistry, VoiceResolver


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY_PATH = PROJECT_ROOT / "main.py"
WEB_UI_DEFAULT_HOST = "127.0.0.1"
WEB_UI_DEFAULT_PORT = 8876
WEB_UI_DEFAULT_JOB_API_BASE_URL = f"http://{JOB_API_DEFAULT_HOST}:{JOB_API_DEFAULT_PORT}"
WEB_UI_TITLE = "AIVideoTrans Web UI"
MAX_LOG_LINES = 500
PROCESS_RUN_TIMEOUT_SECONDS = 60 * 60 * 6
RESULT_PAGE_SIZE_OPTIONS = (20, 50, 100)
DEFAULT_RESULT_PAGE_SIZE = 20
RESULT_DOWNLOAD_KEY_MANIFEST = "manifest.file"
PUBLIC_RESULT_DOWNLOAD_KEYS = frozenset(
    {
        RESULT_DOWNLOAD_KEY_MANIFEST,
        "translation.segments",
        "editor.subtitles",
        "editor.dubbed_audio_complete",
        "publish.dubbed_video",
    }
)
PROJECT_AUDIO_FILE_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
STAGE_LOG_PATTERN = re.compile(r"^\[(S\d+)\]\s*(.*)$")
DOWNLOAD_PROGRESS_PATTERN = re.compile(r"^\[download\]\s*(.+)$", re.IGNORECASE)
WINDOWS_PATH_PATTERN = re.compile(r"([A-Za-z]:[\\/][^\r\n]+)")
RESULT_SOURCE_LABELS = {
    "matched_youtube_url": "按当前任务 URL 匹配到项目",
    "log_path": "从运行日志识别到项目目录",
    "latest_project": "回退到最近项目目录",
    "no_projects_root": "尚未发现 projects 目录",
    "no_project_match": "未匹配到可用项目结果",
}
PROVIDER_DISPLAY_NAMES = {
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}
SPEAKER_OPTIONS = (
    {"value": "auto", "label": "自动"},
    {"value": "1", "label": "1 人"},
    {"value": "2", "label": "2 人"},
)
PROMPT_TEMPLATE_LOADERS: dict[str, tuple[Callable[[object | None], str], Callable[[str], str]]] = {
    "s2_infer": (
        get_effective_speaker_infer_prompt_template,
        validate_speaker_infer_prompt_template,
    ),
    "s3_translate": (
        get_effective_translation_prompt_template,
        validate_translation_prompt_template,
    ),
    "s5_rewrite": (
        get_effective_rewrite_prompt_template,
        validate_rewrite_prompt_template,
    ),
}


@dataclass(frozen=True, slots=True)
class WebUICommandArgs:
    port: int = WEB_UI_DEFAULT_PORT


@dataclass(slots=True)
class ProcessJobSnapshot:
    job_id: str | None
    status: str
    youtube_url: str
    speakers: str
    voice_a: str | None
    voice_b: str | None
    translation_model_alias: str
    project_dir: str | None = None
    current_stage: str | None = None
    current_message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    returncode: int | None = None
    logs: list[str] | None = None
    review_gate: dict[str, object] | None = None
    control_mode: str = "legacy_process"

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "youtube_url": self.youtube_url,
            "speakers": self.speakers,
            "voice_a": self.voice_a,
            "voice_b": self.voice_b,
            "translation_model_alias": self.translation_model_alias,
            "project_dir": self.project_dir,
            "current_stage": self.current_stage,
            "current_message": self.current_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "returncode": self.returncode,
            "logs": list(self.logs or []),
            "review_gate": dict(self.review_gate or {}),
            "control_mode": self.control_mode,
        }


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
    ) -> dict[str, object]:
        normalized_url = youtube_url.strip()
        normalized_speakers = speakers.strip().lower()
        normalized_voice_a = (voice_a or "").strip() or None
        normalized_voice_b = (voice_b or "").strip() or None
        normalized_alias = translation_model_alias.strip()
        normalized_project_dir = (project_dir or "").strip() or None

        if not normalized_url:
            raise ValueError("YouTube URL 不能为空。")
        if normalized_speakers not in {"auto", "1", "2"}:
            raise ValueError("说话人设置只支持 auto、1、2。")

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


def _resolve_snapshot_status_from_log_line(
    *,
    line: str,
    current_stage: str | None,
    current_message: str | None,
) -> tuple[str | None, str | None]:
    stage_match = STAGE_LOG_PATTERN.match(line)
    if stage_match:
        next_stage = stage_match.group(1)
        next_message = stage_match.group(2).strip() or current_message
        return next_stage, next_message

    download_match = DOWNLOAD_PROGRESS_PATTERN.match(line)
    if download_match:
        progress_message = download_match.group(1).strip()
        if not progress_message:
            return "S0", "下载中..."
        return "S0", f"下载中：{progress_message}"

    if line.lower().startswith("process failed:"):
        return current_stage, line

    if line.startswith("[WEB]"):
        return current_stage, current_message

    normalized_line = line.strip()
    if normalized_line:
        return current_stage, normalized_line

    return current_stage, current_message


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
    stage = _normalize_optional_text(payload.get("stage"))
    project_dir = _normalize_optional_text(payload.get("project_dir"))
    message = _normalize_optional_text(payload.get("message"))
    tab = _normalize_optional_text(payload.get("tab")) or (
        REVIEW_STAGE_TAB_MAP.get(stage) if stage is not None else None
    )
    if stage is None:
        return None
    return {
        "stage": stage,
        "tab": tab,
        "project_dir": project_dir,
        "message": message,
    }


def build_translation_model_options(*, config_path: Path | None = None) -> list[dict[str, str]]:
    config = load_llm_fallback_config_for_path(config_path)
    current_route = list(
        config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"])
    )
    ordered_aliases: list[str] = []
    for alias in [DEFAULT_DEFAULT_LLM_ALIAS, *current_route, *config["llm_models"].keys()]:
        if alias not in ordered_aliases:
            ordered_aliases.append(alias)

    options: list[dict[str, str]] = []
    gemini_model_name = str(config["gemini"].get("model_name") or "").strip()
    for alias in ordered_aliases:
        if alias == DEFAULT_DEFAULT_LLM_ALIAS:
            label = f"default_llm (当前默认: {gemini_model_name or '未设置'})"
            options.append(
                {
                    "alias": alias,
                    "label": label,
                    "provider": "gemini",
                    "model_name": gemini_model_name or "",
                }
            )
            continue
        model_payload = config["llm_models"].get(alias)
        if not isinstance(model_payload, dict):
            continue
        provider = str(model_payload.get("provider") or "").strip()
        model_name = str(model_payload.get("model_name") or "").strip()
        label = f"{alias} ({provider}: {model_name})" if provider and model_name else alias
        options.append(
            {
                "alias": alias,
                "label": label,
                "provider": provider,
                "model_name": model_name,
            }
        )
    return options


def build_provider_key_options(*, config_path: Path | None = None) -> list[dict[str, object]]:
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    llm_config = load_llm_fallback_config_for_path(config_path)
    model_options = build_translation_model_options(config_path=config_path)

    provider_to_aliases: dict[str, list[str]] = {}
    provider_to_models: dict[str, list[str]] = {}
    for option in model_options:
        provider = str(option.get("provider") or "").strip()
        alias = str(option.get("alias") or "").strip()
        model_name = str(option.get("model_name") or "").strip()
        if not provider or not alias:
            continue
        provider_to_aliases.setdefault(provider, []).append(alias)
        if model_name:
            provider_to_models.setdefault(provider, []).append(model_name)

    rows: list[dict[str, object]] = []
    for provider in ("gemini", "deepseek", "openai", "anthropic"):
        section = editable_payload.get(provider)
        if not isinstance(section, dict):
            section = {}
        api_key_env_var = str(section.get("api_key_env_var") or "")
        configured_source = _resolve_provider_key_source(section, api_key_env_var=api_key_env_var)
        rows.append(
            {
                "provider": provider,
                "label": PROVIDER_DISPLAY_NAMES.get(provider, provider),
                "api_key_env_var": api_key_env_var,
                "is_configured": configured_source is not None,
                "configured_source": configured_source or "",
                "model_aliases": provider_to_aliases.get(provider, []),
                "model_names": provider_to_models.get(provider, []),
                "default_model_name": str(_ensure_dict(llm_config.get(provider)).get("model_name") or ""),
            }
        )
    return rows


def build_route_visualization(task: str, *, config_path: Path | None = None) -> list[dict[str, str]]:
    config = load_llm_fallback_config_for_path(config_path)
    route = list(config["llm_fallbacks"].get(task, DEFAULT_LLM_FALLBACKS.get(task, [])))
    option_map = {
        option["alias"]: option["label"]
        for option in build_translation_model_options(config_path=config_path)
    }
    return [{"alias": alias, "label": option_map.get(alias, alias)} for alias in route]


def set_translation_primary_model(
    alias: str,
    *,
    config_path: Path | None = None,
) -> list[str]:
    config = load_llm_fallback_config_for_path(config_path)
    available_aliases = {
        option["alias"] for option in build_translation_model_options(config_path=config_path)
    }
    if alias not in available_aliases:
        raise ValueError(f"未知翻译模型别名：{alias}")

    current_route = list(
        config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"])
    )
    updated_route = [alias, *[item for item in current_route if item != alias]]
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    existing_fallbacks = editable_payload.get("llm_fallbacks")
    merged_fallbacks = dict(existing_fallbacks) if isinstance(existing_fallbacks, dict) else {}
    merged_fallbacks["s3_translate"] = updated_route
    for task in S3_SYNCED_TASKS:
        merged_fallbacks[task] = list(updated_route)
    config_loader.save_project_local_config_sections(
        {"llm_fallbacks": merged_fallbacks},
        config_path=config_path,
    )
    return updated_route


def save_web_ui_settings(
    *,
    translation_model_alias: str,
    speaker_infer_prompt_template: str | None = None,
    translation_prompt_template: str | None = None,
    rewrite_prompt_template: str | None = None,
    provider_api_keys: dict[str, str | None],
    config_path: Path | None = None,
) -> list[str]:
    updated_route = set_translation_primary_model(translation_model_alias, config_path=config_path)
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    section_overrides: dict[str, object] = {}
    for provider in ("gemini", "deepseek", "openai", "anthropic"):
        existing_section = editable_payload.get(provider)
        merged_section = dict(existing_section) if isinstance(existing_section, dict) else {}
        if provider in provider_api_keys:
            normalized_value = (provider_api_keys[provider] or "").strip()
            merged_section["api_key"] = normalized_value or None
        section_overrides[provider] = merged_section
    prompts_section = editable_payload.get("prompts")
    merged_prompts = dict(prompts_section) if isinstance(prompts_section, dict) else {}
    prompt_updates = {
        "s2_infer": speaker_infer_prompt_template,
        "s3_translate": translation_prompt_template,
        "s5_rewrite": rewrite_prompt_template,
    }
    for prompt_key, raw_template in prompt_updates.items():
        _default_loader, validator = PROMPT_TEMPLATE_LOADERS[prompt_key]
        normalized_prompt_template = _normalize_optional_text(raw_template)
        if normalized_prompt_template is None:
            merged_prompts[prompt_key] = None
            continue
        try:
            merged_prompts[prompt_key] = validator(normalized_prompt_template)
        except TranslationError as exc:
            raise ValueError(str(exc)) from exc
    section_overrides["prompts"] = merged_prompts
    config_loader.save_project_local_config_sections(section_overrides, config_path=config_path)
    return updated_route


def build_web_ui_snapshot(
    *,
    manager: ProcessJobManager | JobAPIBackedJobManager,
) -> dict[str, object]:
    selected_alias = _load_selected_translation_model_alias(manager.config_path)
    prompt_templates = _load_prompt_templates(
        manager.config_path
    )
    job_snapshot = manager.snapshot()
    results_snapshot = _build_results_snapshot(
        project_root=manager.project_root,
        job_snapshot=job_snapshot,
    )
    project_dir_value = _normalize_optional_text(results_snapshot.get("project_dir"))
    voice_library_snapshot = _build_voice_library_snapshot(
        project_root=manager.project_root,
        config_path=manager.config_path,
        project_dir=(
            Path(project_dir_value).expanduser().resolve(strict=False)
            if project_dir_value is not None
            else None
        ),
        transcript_items=list(results_snapshot.get("transcript_review", {}).get("items", []))
        if isinstance(results_snapshot.get("transcript_review"), dict)
        else [],
    )
    results_snapshot["voice_library"] = voice_library_snapshot
    return {
        "meta": {
            "title": WEB_UI_TITLE,
            "config_path": str(manager.config_path),
            "project_root": str(manager.project_root),
        },
        "settings": {
            "speaker_options": list(SPEAKER_OPTIONS),
            "translation_model_options": build_translation_model_options(
                config_path=manager.config_path
            ),
            "s3_translate_route": build_route_visualization(
                "s3_translate",
                config_path=manager.config_path,
            ),
            "provider_key_options": build_provider_key_options(
                config_path=manager.config_path
            ),
            "selected_translation_model": selected_alias,
            "selected_translation_model_label": _find_translation_model_label(
                selected_alias,
                config_path=manager.config_path,
            ),
            "speaker_infer_prompt_template": prompt_templates["s2_infer"]["template"],
            "speaker_infer_prompt_source": prompt_templates["s2_infer"]["source"],
            "translation_prompt_template": prompt_templates["s3_translate"]["template"],
            "translation_prompt_source": prompt_templates["s3_translate"]["source"],
            "rewrite_prompt_template": prompt_templates["s5_rewrite"]["template"],
            "rewrite_prompt_source": prompt_templates["s5_rewrite"]["source"],
        },
        "job": job_snapshot,
        "results": results_snapshot,
    }


def run_web_ui_server(
    *,
    host: str = WEB_UI_DEFAULT_HOST,
    port: int = WEB_UI_DEFAULT_PORT,
) -> None:
    server = create_web_ui_server(host=host, port=port)
    web_ui_url = f"http://{host}:{port}"
    print(f"{WEB_UI_TITLE} 已启动：{web_ui_url}")
    print(f"配置文件：{server.config_path}")
    _open_browser(web_ui_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 Web UI。")
    finally:
        server.server_close()


def create_web_ui_server(
    *,
    host: str = WEB_UI_DEFAULT_HOST,
    port: int = WEB_UI_DEFAULT_PORT,
    job_manager: ProcessJobManager | JobAPIBackedJobManager | None = None,
) -> ThreadingHTTPServer:
    handler_class = _build_web_ui_handler()
    server = ThreadingHTTPServer((host, port), handler_class)
    server.job_manager = job_manager or JobAPIBackedJobManager()  # type: ignore[attr-defined]
    server.config_path = str(server.job_manager.config_path)  # type: ignore[attr-defined]
    return server


def _legacy_render_web_ui_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIVideoTrans Web UI</title>
  <style>
    :root {
      --bg: #f4efe7;
      --panel: #fffaf2;
      --ink: #1f2725;
      --muted: #697370;
      --line: #decfbc;
      --accent: #b45d38;
      --accent-soft: #f2dfd3;
      --accent-2: #275f58;
      --shadow: 0 18px 40px rgba(31, 39, 37, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", "PingFang SC", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(180, 93, 56, 0.14), transparent 26%),
        radial-gradient(circle at top right, rgba(39, 95, 88, 0.11), transparent 24%),
        linear-gradient(180deg, #fbf7f2 0%, var(--bg) 100%);
    }
    header {
      padding: 28px 24px 12px;
    }
    header h1 {
      margin: 0 0 8px;
      font-size: 34px;
      line-height: 1.08;
      font-family: Georgia, "Times New Roman", serif;
    }
    header p {
      margin: 0;
      color: var(--muted);
      max-width: 980px;
      line-height: 1.55;
    }
    main {
      padding: 18px 24px 36px;
      display: grid;
      gap: 18px;
    }
    .layout {
      display: grid;
      gap: 18px;
      grid-template-columns: 380px 1fr;
      align-items: start;
    }
    .stack {
      display: grid;
      gap: 18px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .panel h2 {
      margin: 0 0 14px;
      font-size: 20px;
      font-family: Georgia, "Times New Roman", serif;
    }
    .field {
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
    }
    .field label {
      font-size: 14px;
      color: var(--muted);
    }
    input[type="text"], select, button, textarea {
      font: inherit;
    }
    input[type="text"], select, textarea.prompt-editor {
      width: 100%;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #fffdf9;
      color: var(--ink);
    }
    textarea.prompt-editor {
      min-height: 320px;
      line-height: 1.55;
      resize: vertical;
    }
    button {
      border: none;
      border-radius: 999px;
      padding: 11px 16px;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }
    button.secondary {
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    button.warning {
      background: var(--accent-2);
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      margin: 0;
    }
    .workbench-admin-only {
      display: none !important;
    }
    .meta-list, .route-list, .key-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }
    .meta-list li, .route-list li, .key-card {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.72);
    }
    .meta-list strong, .route-list strong, .key-card strong {
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 13px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      padding: 6px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--ink);
      font-size: 13px;
      width: fit-content;
    }
    .status-pill.is-running,
    .status-pill.is-queued {
      background: rgba(39, 95, 88, 0.14);
      color: var(--accent-2);
    }
    .status-pill.is-waiting {
      background: rgba(180, 93, 56, 0.14);
      color: var(--accent);
    }
    .status-pill.is-success {
      background: rgba(39, 95, 88, 0.18);
      color: var(--accent-2);
    }
    .status-pill.is-failed,
    .status-pill.is-cancelled {
      background: rgba(180, 93, 56, 0.18);
      color: var(--accent);
    }
    .subgrid {
      display: grid;
      gap: 18px;
      grid-template-columns: 1fr 1fr;
    }
    .job-runtime-shell {
      display: grid;
      gap: 14px;
      margin-top: 14px;
    }
    .job-summary-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px 18px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.86), rgba(246, 238, 226, 0.94)),
        radial-gradient(circle at top right, rgba(39, 95, 88, 0.08), transparent 28%);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.55);
      display: grid;
      gap: 12px;
    }
    .job-summary-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
    }
    .job-summary-eyebrow {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .job-summary-title {
      font-size: 22px;
      line-height: 1.2;
      font-family: Georgia, "Times New Roman", serif;
      margin-top: 4px;
    }
    .job-log-preview {
      display: grid;
      gap: 8px;
    }
    .job-log-preview-item {
      border: 1px solid rgba(31, 39, 37, 0.08);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(255, 253, 249, 0.9);
      color: var(--ink);
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.55;
      word-break: break-word;
    }
    .job-log-preview-item.latest {
      border-color: rgba(180, 93, 56, 0.22);
      background: rgba(242, 223, 211, 0.58);
    }
    .job-log-preview-item.empty {
      color: var(--muted);
      font-family: "Segoe UI", "PingFang SC", sans-serif;
    }
    .job-log-details {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255, 250, 242, 0.72);
      overflow: hidden;
    }
    .job-log-details summary {
      list-style: none;
      cursor: pointer;
      padding: 14px 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-weight: 600;
      color: var(--ink);
    }
    .job-log-details summary::-webkit-details-marker {
      display: none;
    }
    .job-log-details[open] summary {
      border-bottom: 1px solid rgba(31, 39, 37, 0.08);
      background: rgba(255, 255, 255, 0.44);
    }
    .job-log-frame {
      padding: 0 16px 16px;
    }
    textarea.log-output {
      width: 100%;
      min-height: 220px;
      border-radius: 16px;
      border: 1px solid rgba(31, 39, 37, 0.18);
      background: #23302d;
      color: #f7f0e4;
      padding: 14px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      line-height: 1.55;
      resize: vertical;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
    }
    .progress-wrap {
      display: none;
      gap: 8px;
      margin-top: 14px;
    }
    .progress-wrap.active {
      display: grid;
    }
    .progress-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .progress-bar {
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: rgba(31, 39, 37, 0.08);
      overflow: hidden;
      border: 1px solid rgba(31, 39, 37, 0.08);
    }
    .progress-bar > span {
      display: block;
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), #d08956);
      transition: width 180ms ease;
    }
    .mono {
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      word-break: break-all;
    }
    @media (max-width: 1120px) {
      .layout, .subgrid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>AIVideoTrans Web UI</h1>
    <p>这版先把最需要的入口做顺：YouTube URL、说话人数、可选的两个发言人 Voice ID、S3 主翻译模型、S3 fallback 链展示，以及各 provider 的 API Key 读写。更细的确认流和试听页后面再补。</p>
  </header>
  <main>
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <h2>处理设置</h2>
          <div class="field">
            <label for="youtube-url">YouTube URL</label>
            <input id="youtube-url" type="text" placeholder="https://www.youtube.com/watch?v=..." />
          </div>
          <div class="field">
            <label for="speakers">说话人数</label>
            <select id="speakers"></select>
          </div>
          <div class="field">
            <label for="voice-a">Speaker A Voice ID（可选）</label>
            <input id="voice-a" type="text" placeholder="留空则匹配音色库或自动克隆" />
          </div>
          <div class="field">
            <label for="voice-b">Speaker B Voice ID（可选）</label>
            <input id="voice-b" type="text" placeholder="留空则匹配音色库或自动克隆" />
          </div>
          <div class="field workbench-admin-only">
            <label for="translation-model">S3 翻译主模型</label>
            <select id="translation-model"></select>
          </div>
          <p class="hint workbench-admin-only">这里改的是 S3 主模型。保存后会写回本地配置；S3 fallback 链会在右侧按当前顺序展示。</p>
          <div class="toolbar">
            <button id="save-settings-button" class="workbench-admin-only" type="button">保存设置</button>
            <button id="run-button" type="button">开始处理</button>
            <button id="stop-button" class="warning" type="button" style="display: none;">停止当前任务</button>
            <button id="refresh-button" class="secondary" type="button">刷新状态</button>
          </div>
          <div id="stop-unavailable-hint" class="hint" style="display: none;">
            当前远程工作台模式暂不支持停止任务；如流程进入待确认，请先完成确认后继续。
          </div>
        </section>

        <section class="panel workbench-admin-only">
          <h2>Provider API Key</h2>
          <p class="hint">每个模型会使用所属 provider 的 API Key。这里直接从本地配置读取和回写。</p>
          <div id="provider-keys" class="key-list"></div>
        </section>
      </div>

      <div class="stack">
        <section class="panel workbench-admin-only">
          <h2>S3 Fallback 链</h2>
          <ul id="route-list" class="route-list"></ul>
        </section>

        <section class="panel">
          <h2>运行进度</h2>
          <div class="status-pill" id="job-status">等待任务启动</div>
          <div id="pending-review-wrap" class="panel review-attention-callout" style="display: none; margin-top: 12px; padding: 14px;">
            <div class="toolbar" style="margin-bottom: 8px;">
              <strong id="pending-review-title">待人工确认</strong>
              <button id="pending-review-open" class="secondary" type="button">前往确认页面</button>
            </div>
            <div class="hint" id="pending-review-message">当前没有待确认事项。</div>
          </div>
          <div class="subgrid" style="margin-top: 12px;">
            <ul class="meta-list">
              <li><strong>当前阶段</strong><span id="current-stage">-</span></li>
              <li><strong>最新状态</strong><span id="current-message">-</span></li>
              <li><strong>当前 URL</strong><span id="current-url">-</span></li>
            </ul>
            <ul class="meta-list">
              <li class="workbench-admin-only"><strong>S3 主模型</strong><span id="current-model">-</span></li>
              <li><strong>Speaker A Voice ID</strong><span id="current-voice-a">-</span></li>
              <li><strong>Speaker B Voice ID</strong><span id="current-voice-b">-</span></li>
            </ul>
          </div>
          <div id="download-progress-wrap" class="progress-wrap">
            <div class="progress-meta">
              <strong>下载进度</strong>
              <span id="download-progress-label">-</span>
            </div>
            <div class="progress-bar"><span id="download-progress-fill"></span></div>
          </div>
          <div class="job-runtime-shell">
            <div class="job-summary-card">
              <div class="job-summary-head">
                <div>
                  <div class="job-summary-eyebrow">运行摘要</div>
                  <div id="job-highlight-title" class="job-summary-title">等待任务启动</div>
                </div>
                <span id="job-highlight-badge" class="status-pill">空闲</span>
              </div>
              <p id="job-highlight-copy" class="hint">提交任务后，这里会优先显示最重要的一条进展和最近摘要。</p>
              <div id="job-log-preview" class="job-log-preview">
                <div class="job-log-preview-item empty">详细日志会在运行后出现在这里，原始输出可按需展开查看。</div>
              </div>
            </div>
            <details id="job-log-details" class="job-log-details">
              <summary>
                <span>详细日志</span>
                <span id="job-log-details-meta" class="pagination-meta">0 行</span>
              </summary>
              <div class="job-log-frame">
                <textarea id="job-logs" class="log-output" readonly placeholder="运行日志会显示在这里..."></textarea>
              </div>
            </details>
          </div>
        </section>
      </div>
    </div>
    <section class="panel">
      <h2>LLM 提示词</h2>
      <p class="hint">这里可以统一编辑 S2 说话人识别、S3 翻译、S5 重写的提示词。留空保存会恢复默认提示词。请保留各自必需的占位符：S2 用 <code>__CONTEXT_EXCERPT__</code>，S3 用 <code>__GROUPS_JSON__</code>，S5 用 <code>__TTS_CN_TEXT__</code>、<code>__DIRECTION_DESC__</code>、<code>__DIRECTION_INSTRUCTION__</code>、<code>__TARGET_CHARS__</code>。</p>
      <div class="field">
        <label for="speaker-infer-prompt">S2 说话人识别 Prompt</label>
        <textarea id="speaker-infer-prompt" class="prompt-editor" spellcheck="false" placeholder="加载中..."></textarea>
      </div>
      <div class="field">
        <label for="translation-prompt">S3 翻译 Prompt</label>
        <textarea id="translation-prompt" class="prompt-editor" spellcheck="false" placeholder="加载中..."></textarea>
      </div>
      <div class="field" style="margin-bottom: 0;">
        <label for="rewrite-prompt">S5 重写 Prompt</label>
        <textarea id="rewrite-prompt" class="prompt-editor" spellcheck="false" placeholder="加载中..."></textarea>
      </div>
    </section>
  </main>

  <script>
    async function fetchJson(url, options = {}) {
      let response;
      try {
        response = await fetch(url, {
          headers: { "Content-Type": "application/json" },
          ...options,
        });
      } catch (error) {
        const detail = error && error.message ? ` (${error.message})` : "";
        throw new Error(
          `无法连接到 Web UI 服务，请确认 "python main.py web-ui" 仍在运行，然后刷新页面重试。${detail}`
        );
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `请求失败 (${response.status})`);
      }
      return payload;
    }

    function readSettingsDraft() {
      const speakerInferPromptNode = document.getElementById("speaker-infer-prompt");
      const translationPromptNode = document.getElementById("translation-prompt");
      const rewritePromptNode = document.getElementById("rewrite-prompt");
      return {
        speakers: document.getElementById("speakers")?.value || "auto",
        translationModelAlias: document.getElementById("translation-model")?.value || "",
        speakerInferPromptTemplate: speakerInferPromptNode ? speakerInferPromptNode.value : null,
        translationPromptTemplate: translationPromptNode ? translationPromptNode.value : null,
        rewritePromptTemplate: rewritePromptNode ? rewritePromptNode.value : null,
        providerApiKeys: {
          gemini: document.getElementById("key-gemini")?.value,
          deepseek: document.getElementById("key-deepseek")?.value,
          openai: document.getElementById("key-openai")?.value,
          anthropic: document.getElementById("key-anthropic")?.value,
        },
      };
    }

    function renderSettings(settings, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      const draft = preserveDraft ? readSettingsDraft() : {
        speakers: "auto",
        translationModelAlias: "",
        speakerInferPromptTemplate: null,
        translationPromptTemplate: null,
        rewritePromptTemplate: null,
        providerApiKeys: {},
      };
      const speakerSelect = document.getElementById("speakers");
      speakerSelect.innerHTML = "";
      (settings.speaker_options || []).forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });
      const nextSpeakerValue = (settings.speaker_options || []).some((option) => option.value === draft.speakers)
        ? draft.speakers
        : "auto";
      speakerSelect.value = nextSpeakerValue;

      const modelSelect = document.getElementById("translation-model");
      modelSelect.innerHTML = "";
      const nextModelAlias = (settings.translation_model_options || []).some(
        (option) => option.alias === draft.translationModelAlias
      )
        ? draft.translationModelAlias
        : (settings.selected_translation_model || "");
      (settings.translation_model_options || []).forEach((option) => {
        const node = document.createElement("option");
        node.value = option.alias;
        node.textContent = option.label;
        if (option.alias === nextModelAlias) {
          node.selected = true;
        }
        modelSelect.appendChild(node);
      });
      if (nextModelAlias) {
        modelSelect.value = nextModelAlias;
      }

      const routeList = document.getElementById("route-list");
      routeList.innerHTML = "";
      (settings.s3_translate_route || []).forEach((routeItem, index) => {
        const li = document.createElement("li");
        li.innerHTML = `<strong>${index === 0 ? "主模型" : `Fallback ${index}`}</strong><span>${routeItem.label}</span>`;
        routeList.appendChild(li);
      });

      const providerKeys = document.getElementById("provider-keys");
      providerKeys.innerHTML = "";
      (settings.provider_key_options || []).forEach((provider) => {
        const draftApiKey = draft.providerApiKeys[provider.provider];
        const inputValue = draftApiKey !== undefined ? draftApiKey : "";
        const configuredHint = provider.is_configured
          ? (provider.configured_source === "env" ? "当前通过环境变量提供" : "当前已保存在本地配置中")
          : "当前未设置";
        const wrapper = document.createElement("div");
        wrapper.className = "key-card";
        wrapper.innerHTML = `
          <strong>${provider.label}</strong>
          <div class="hint">模型别名：${(provider.model_aliases || []).join(", ") || "-"}</div>
          <div class="hint">模型名：${(provider.model_names || []).join(", ") || "-"}</div>
          <div class="hint">Key 状态：${configuredHint}</div>
          <div class="field" style="margin-top: 10px; margin-bottom: 0;">
            <label for="key-${provider.provider}">API Key</label>
            <input id="key-${provider.provider}" type="text" value="${inputValue}" placeholder="${provider.api_key_env_var || ""}" />
          </div>
        `;
        providerKeys.appendChild(wrapper);
      });

      const speakerInferPromptEditor = document.getElementById("speaker-infer-prompt");
      const nextSpeakerInferPromptTemplate = draft.speakerInferPromptTemplate !== null
        ? draft.speakerInferPromptTemplate
        : (settings.speaker_infer_prompt_template || "");
      speakerInferPromptEditor.value = nextSpeakerInferPromptTemplate;
      speakerInferPromptEditor.placeholder = settings.speaker_infer_prompt_source === "custom"
        ? "当前使用自定义 S2 提示词"
        : "当前使用默认 S2 提示词";

      const promptEditor = document.getElementById("translation-prompt");
      const nextPromptTemplate = draft.translationPromptTemplate !== null
        ? draft.translationPromptTemplate
        : (settings.translation_prompt_template || "");
      promptEditor.value = nextPromptTemplate;
      promptEditor.placeholder = settings.translation_prompt_source === "custom"
        ? "当前使用自定义提示词"
        : "当前使用默认提示词";

      const rewritePromptEditor = document.getElementById("rewrite-prompt");
      const nextRewritePromptTemplate = draft.rewritePromptTemplate !== null
        ? draft.rewritePromptTemplate
        : (settings.rewrite_prompt_template || "");
      rewritePromptEditor.value = nextRewritePromptTemplate;
      rewritePromptEditor.placeholder = settings.rewrite_prompt_source === "custom"
        ? "当前使用自定义 S5 提示词"
        : "当前使用默认 S5 提示词";
    }

    function renderJob(job) {
      webUiState.latestJob = job;
      const jobStatusNode = document.getElementById("job-status");
      const summaryBadgeNode = document.getElementById("job-highlight-badge");
      const summaryTitleNode = document.getElementById("job-highlight-title");
      const summaryCopyNode = document.getElementById("job-highlight-copy");
      const logPreviewNode = document.getElementById("job-log-preview");
      const logDetailsNode = document.getElementById("job-log-details");
      const logDetailsMetaNode = document.getElementById("job-log-details-meta");
      const status = String(job.status || "idle");
      const stage = String(job.current_stage || "").trim();
      const currentMessage = job.current_message || "";
      const logs = Array.isArray(job.logs)
        ? job.logs.filter((line) => String(line || "").trim())
        : [];
      const latestLogLines = logs.slice(-3);
      const statusLabelMap = {
        idle: "空闲",
        queued: "排队中",
        running: "运行中",
        stopping: "停止中",
        waiting_for_review: "待确认",
        succeeded: "已完成",
        failed: "失败",
        cancelled: "已取消",
      };
      const statusClassMap = {
        idle: "",
        queued: "is-queued",
        running: "is-running",
        stopping: "is-running",
        waiting_for_review: "is-waiting",
        succeeded: "is-success",
        failed: "is-failed",
        cancelled: "is-cancelled",
      };
      let summaryTitle = "等待任务启动";
      if (status === "queued") {
        summaryTitle = "任务已提交，正在等待执行";
      } else if (status === "running" && (stage === "S0" || stage === "ingestion")) {
        summaryTitle = "正在下载并准备素材";
      } else if (status === "running") {
        summaryTitle = "任务正在处理中";
      } else if (status === "stopping") {
        summaryTitle = "正在停止当前任务";
      } else if (status === "waiting_for_review") {
        summaryTitle = "等待人工确认后继续";
      } else if (status === "succeeded") {
        summaryTitle = "任务已完成";
      } else if (status === "failed") {
        summaryTitle = "任务执行失败";
      } else if (status === "cancelled") {
        summaryTitle = "任务已取消";
      }
      jobStatusNode.textContent = `状态：${statusLabelMap[status] || status}`;
      jobStatusNode.className = `status-pill ${statusClassMap[status] || ""}`.trim();
      summaryBadgeNode.textContent = statusLabelMap[status] || status;
      summaryBadgeNode.className = `status-pill ${statusClassMap[status] || ""}`.trim();
      summaryTitleNode.textContent = summaryTitle;
      summaryCopyNode.textContent = currentMessage || "提交任务后，这里会优先显示最重要的一条进展和最近摘要。";
      document.getElementById("current-stage").textContent = job.current_stage || "-";
      document.getElementById("current-message").textContent = job.current_message || "-";
      document.getElementById("current-url").textContent = job.youtube_url || "-";
      document.getElementById("current-model").textContent = job.translation_model_alias || "-";
      document.getElementById("current-voice-a").textContent = job.voice_a || "留空";
      document.getElementById("current-voice-b").textContent = job.voice_b || "留空";
      const progressWrap = document.getElementById("download-progress-wrap");
      const progressFill = document.getElementById("download-progress-fill");
      const progressLabel = document.getElementById("download-progress-label");
      const progressMatch = currentMessage.match(/^下载中：([0-9]+(?:[.][0-9]+)?)%(.*)$/);
      if ((job.current_stage === "S0" || job.current_stage === "ingestion") && progressMatch) {
        const progressPercent = Math.max(0, Math.min(100, Number(progressMatch[1] || "0")));
        progressWrap.classList.add("active");
        progressFill.style.width = `${progressPercent}%`;
        progressLabel.textContent = `${progressPercent.toFixed(1)}%${progressMatch[2] || ""}`.trim();
      } else {
        progressWrap.classList.remove("active");
        progressFill.style.width = "0%";
        progressLabel.textContent = "-";
      }
      const logsField = document.getElementById("job-logs");
      logsField.value = logs.join("\\n");
      logDetailsMetaNode.textContent = `${logs.length} 行`;
      if (logs.length) {
        logPreviewNode.innerHTML = latestLogLines.map((line, index) => `
          <div class="job-log-preview-item${index === latestLogLines.length - 1 ? " latest" : ""}">
            ${escapeHtml(line)}
          </div>
        `).join("");
      } else {
        logPreviewNode.innerHTML = '<div class="job-log-preview-item empty">运行后这里会显示最近的关键日志，完整原始输出可按需展开查看。</div>';
      }
      if (logDetailsNode.open) {
        logsField.scrollTop = logsField.scrollHeight;
      }
      const isQueued = job.status === "queued";
      const isRunning = job.status === "running" || job.status === "stopping";
      const isWaitingForReview = job.status === "waiting_for_review";
      const isActive = isQueued || isRunning || isWaitingForReview;
      const isJobApiMode = (job.control_mode || "legacy_process") === "job_api";
      const canStop = !isJobApiMode && isRunning;
      const stopButton = document.getElementById("stop-button");
      const stopUnavailableHint = document.getElementById("stop-unavailable-hint");
      document.getElementById("run-button").disabled = isActive;
      stopButton.disabled = !canStop;
      stopButton.style.display = isJobApiMode ? "none" : "";
      stopUnavailableHint.style.display = isJobApiMode && isActive ? "block" : "none";
    }

    function renderReviewFlow(results, job = {}) {
      const reviewFlow = results.review_flow || {};
      const activeReview = reviewFlow.active_review || null;
      const pendingWrap = document.getElementById("pending-review-wrap");
      const pendingTitle = document.getElementById("pending-review-title");
      const pendingMessage = document.getElementById("pending-review-message");
      const pendingOpenButton = document.getElementById("pending-review-open");
      if (!activeReview || activeReview.status !== "pending") {
        pendingWrap.style.display = "none";
        webUiState.lastAutoReviewStage = null;
        clearReviewAttention();
        return;
      }

      const stage = activeReview.stage || reviewFlow.active_stage || "";
      const nextTab = activeReview.tab || "review";
      const stageLabelMap = {
        speaker_review: "待确认：发言人",
        translation_review: "待确认：翻译稿",
        voice_review: "待确认：音色",
        audio_alignment_review: "待确认：试听与对齐",
      };
      const statusMessage = activeReview.payload?.message || activeReview.message || job.current_message || "当前阶段需要人工确认。";
      pendingWrap.style.display = "block";
      pendingTitle.textContent = stageLabelMap[stage] || "待人工确认";
      pendingMessage.textContent = statusMessage;
      pendingOpenButton.onclick = () => setActiveTab(nextTab);
      const shouldPulseAttention = job.status === "waiting_for_review" && webUiState.lastAutoReviewStage !== stage;
      applyReviewAttention(stage, { pulse: shouldPulseAttention, tab: nextTab });

      if (shouldPulseAttention) {
        webUiState.lastAutoReviewStage = stage;
        setActiveTab(nextTab);
      }
    }

    async function refreshState(options = {}) {
      const snapshot = await fetchJson("/api/state");
      renderSnapshot(snapshot, options);
    }

    async function saveSettings() {
      const payload = {
        translation_model_alias: document.getElementById("translation-model").value,
        speaker_infer_prompt_template: document.getElementById("speaker-infer-prompt")?.value ?? "",
        translation_prompt_template: document.getElementById("translation-prompt")?.value ?? "",
        rewrite_prompt_template: document.getElementById("rewrite-prompt")?.value ?? "",
        gemini_api_key: document.getElementById("key-gemini")?.value || "",
        deepseek_api_key: document.getElementById("key-deepseek")?.value || "",
        openai_api_key: document.getElementById("key-openai")?.value || "",
        anthropic_api_key: document.getElementById("key-anthropic")?.value || "",
      };
      const snapshot = await fetchJson("/api/settings", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderSnapshot(snapshot, { preserveDraft: false });
    }

    async function startRun() {
      const youtubeUrl = document.getElementById("youtube-url").value.trim();
      if (!youtubeUrl) {
        alert("请先输入 YouTube URL。");
        return;
      }
      const snapshot = await fetchJson("/api/run", {
        method: "POST",
        body: JSON.stringify({
          youtube_url: youtubeUrl,
          speakers: document.getElementById("speakers").value,
          voice_a: document.getElementById("voice-a").value,
          voice_b: document.getElementById("voice-b").value,
          translation_model_alias: document.getElementById("translation-model").value,
        }),
      });
      renderSnapshot(snapshot, { preserveDraft: false });
    }

    async function stopRun() {
      const snapshot = await fetchJson("/api/stop", {
        method: "POST",
        body: JSON.stringify({}),
      });
      renderSnapshot(snapshot, { preserveDraft: false });
    }

    async function saveSpeakerReview(options = {}) {
      const preserveDraft = options.preserveDraft === true;
      const currentResults = webUiState.latestResults || {};
      setSaveIndicatorState("review", currentResults, {
        phase: "saving",
        dirty: false,
        errorMessage: "",
      });
      try {
        const snapshot = await fetchJson("/api/review/speaker/save", {
          method: "POST",
          body: JSON.stringify(collectSpeakerReviewPayload(currentResults)),
        });
        clearReviewConfirmationsStorage(currentResults);
        renderSnapshot(snapshot, { preserveDraft });
        const savedAt = snapshot.results?.review_flow?.stages?.speaker_review?.updated_at || new Date().toISOString();
        setSaveIndicatorState("review", snapshot.results || {}, {
          phase: "saved",
          dirty: false,
          updatedAt: savedAt,
          errorMessage: "",
        });
      } catch (error) {
        setSaveIndicatorState("review", currentResults, {
          phase: "error",
          dirty: true,
          errorMessage: error?.message || "",
        });
        throw error;
      }
    }

    async function approveSpeakerReview() {
      const currentResults = webUiState.latestResults || {};
      setSaveIndicatorState("review", currentResults, {
        phase: "saving",
        dirty: false,
        errorMessage: "",
      });
      try {
        const snapshot = await fetchJson("/api/review/speaker/approve", {
          method: "POST",
          body: JSON.stringify(collectSpeakerReviewPayload(currentResults)),
        });
        clearReviewConfirmationsStorage(currentResults);
        webUiState.speakerReviewDraft = { speakerNames: {}, segmentSpeakers: {} };
        renderSnapshot(snapshot, { preserveDraft: false });
        const savedAt = snapshot.results?.review_flow?.stages?.speaker_review?.updated_at || new Date().toISOString();
        setSaveIndicatorState("review", snapshot.results || {}, {
          phase: "saved",
          dirty: false,
          updatedAt: savedAt,
          errorMessage: "",
        });
      } catch (error) {
        setSaveIndicatorState("review", currentResults, {
          phase: "error",
          dirty: true,
          errorMessage: error?.message || "",
        });
        throw error;
      }
    }

    function collectTranslationReviewPayload(results) {
      const projectDir = String(results?.project_dir || "").trim();
      if (!projectDir) {
        throw new Error("当前没有可保存的项目目录。");
      }
      const translationReview = results.translation_review || {};
      const allItems = Array.isArray(translationReview.items) ? translationReview.items : [];
      const state = loadTranslationReviewState(results);
      const segments = {};
      allItems.forEach((item) => {
        const segmentId = String(item.segment_id || "").trim();
        if (!segmentId) {
          return;
        }
        const entry = state[segmentId] || {};
        const cnText = typeof entry.cnText === "string" ? entry.cnText : String(item.cn_text || "");
        const ttsCnText = typeof entry.ttsCnText === "string"
          ? entry.ttsCnText
          : String(item.tts_cn_text || cnText || "");
        segments[segmentId] = {
          cn_text: cnText,
          tts_cn_text: ttsCnText || cnText,
          translation_confirmed: Boolean(entry.translationConfirmed),
          rewrite_requested: Boolean(entry.rewriteRequested),
          updated_at: String(entry.updatedAt || ""),
        };
      });
      return {
        project_dir: projectDir,
        segments,
      };
    }

    async function saveTranslationReview(options = {}) {
      const preserveDraft = options.preserveDraft === true;
      const currentResults = webUiState.latestResults || {};
      setSaveIndicatorState("translation", currentResults, {
        phase: "saving",
        dirty: false,
        errorMessage: "",
      });
      try {
        const snapshot = await fetchJson("/api/review/translation/save", {
          method: "POST",
          body: JSON.stringify(collectTranslationReviewPayload(currentResults)),
        });
        clearTranslationReviewStateStorage(currentResults);
        renderSnapshot(snapshot, { preserveDraft });
        const savedAt = snapshot.results?.review_flow?.stages?.translation_review?.updated_at || new Date().toISOString();
        setSaveIndicatorState("translation", snapshot.results || {}, {
          phase: "saved",
          dirty: false,
          updatedAt: savedAt,
          errorMessage: "",
        });
      } catch (error) {
        setSaveIndicatorState("translation", currentResults, {
          phase: "error",
          dirty: true,
          errorMessage: error?.message || "",
        });
        throw error;
      }
    }

    async function approveTranslationReview() {
      const currentResults = webUiState.latestResults || {};
      setSaveIndicatorState("translation", currentResults, {
        phase: "saving",
        dirty: false,
        errorMessage: "",
      });
      try {
        const snapshot = await fetchJson("/api/review/translation/approve", {
          method: "POST",
          body: JSON.stringify(collectTranslationReviewPayload(currentResults)),
        });
        clearTranslationReviewStateStorage(currentResults);
        renderSnapshot(snapshot, { preserveDraft: false });
        const savedAt = snapshot.results?.review_flow?.stages?.translation_review?.updated_at || new Date().toISOString();
        setSaveIndicatorState("translation", snapshot.results || {}, {
          phase: "saved",
          dirty: false,
          updatedAt: savedAt,
          errorMessage: "",
        });
      } catch (error) {
        setSaveIndicatorState("translation", currentResults, {
          phase: "error",
          dirty: true,
          errorMessage: error?.message || "",
        });
        throw error;
      }
    }

    function persistSpeakerReviewDraftInBackground() {
      saveSpeakerReview({ preserveDraft: true }).catch((error) => {
        console.error("Failed to persist speaker review draft", error);
      });
    }

    function persistTranslationReviewDraftInBackground() {
      saveTranslationReview({ preserveDraft: true }).catch((error) => {
        console.error("Failed to persist translation review draft", error);
      });
    }

    async function bootstrap() {
      await refreshState({ preserveDraft: false });
      document.getElementById("save-settings-button").addEventListener("click", async () => {
        try {
          await saveSettings();
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("run-button").addEventListener("click", async () => {
        try {
          await startRun();
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("stop-button").addEventListener("click", async () => {
        try {
          await stopRun();
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("refresh-button").addEventListener("click", async () => {
        try {
          await refreshState({ preserveDraft: false });
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("review-save").addEventListener("click", async () => {
        try {
          await saveSpeakerReview();
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("review-approve").addEventListener("click", async () => {
        try {
          await approveSpeakerReview();
        } catch (error) {
          alert(error.message);
        }
      });
      const translationSaveButton = document.getElementById("translation-save");
      if (translationSaveButton) {
        translationSaveButton.addEventListener("click", async () => {
          try {
            await saveTranslationReview();
          } catch (error) {
            alert(error.message);
          }
        });
      }
      const translationApproveButton = document.getElementById("translation-approve");
      if (translationApproveButton) {
        translationApproveButton.addEventListener("click", async () => {
          try {
            await approveTranslationReview();
          } catch (error) {
            alert(error.message);
          }
        });
      }
      window.setInterval(async () => {
        try {
          await refreshState();
        } catch (error) {
          console.warn(error);
        }
      }, 2000);
    }

    bootstrap().catch((error) => {
      alert(error.message);
    });
  </script>
</body>
</html>
"""


def render_web_ui_html() -> str:
    html = _legacy_render_web_ui_html()
    html = html.replace("  </style>", _web_ui_extra_styles() + "\n  </style>", 1)
    html = html.replace("</header>", "</header>\n" + _web_ui_tabs_markup(), 1)
    html = html.replace(
        '    <div class="layout">',
        '    <section class="tab-panel active" data-tab-panel="run">\n    <div class="layout">',
        1,
    )
    html = html.replace(
        '    </div>\n    <section class="panel">\n      <h2>LLM ',
        '    </div>\n    </section>\n    <section class="tab-panel workbench-admin-only" data-tab-panel="settings">\n    <section class="panel">\n      <h2>LLM ',
        1,
    )
    html = html.replace(
        '    </section>\n  </main>',
        '    </section>\n    </section>\n'
        + _web_ui_results_panel_markup()
        + "\n"
        + _web_ui_review_panel_markup()
        + "\n"
        + _web_ui_translation_panel_markup()
        + "\n"
        + _web_ui_voice_library_panel_markup()
        + "\n"
        + _web_ui_audio_alignment_panel_markup()
        + '\n  </main>',
        1,
    )
    html = html.replace(
        '      const progressMatch = currentMessage.match(/^涓嬭浇涓細([0-9]+(?:[.][0-9]+)?)%(.*)$/);',
        '      const progressMatch = currentMessage.match(/^下载中：([0-9]+(?:[.][0-9]+)?)%(.*)$/) || currentMessage.match(/^涓嬭浇涓細([0-9]+(?:[.][0-9]+)?)%(.*)$/);',
        1,
    )
    html = html.replace(
        '    async function refreshState(options = {}) {',
        _web_ui_script_extension() + '\n    async function refreshState(options = {}) {',
        1,
    )
    html = html.replace(
        '      renderJob(snapshot.job || {});\n    }\n\n    async function saveSettings() {',
        '      renderJob(snapshot.job || {});\n      renderResults(snapshot.results || {}, options);\n      renderTranscriptReview(snapshot.results || {}, options);\n      renderTranslationReview(snapshot.results || {}, options);\n      renderVoiceLibrary(snapshot.results || {}, options);\n      renderAudioAlignment(snapshot.results || {}, options);\n    }\n\n    async function saveSettings() {',
        1,
    )
    html = html.replace(
        '      renderSettings(snapshot.settings || {}, { preserveDraft: false });\n      renderJob(snapshot.job || {});\n    }\n\n    async function startRun() {',
        '      renderSettings(snapshot.settings || {}, { preserveDraft: false });\n      renderJob(snapshot.job || {});\n      renderResults(snapshot.results || {}, { preserveDraft: true });\n      renderTranscriptReview(snapshot.results || {}, { preserveDraft: true });\n      renderTranslationReview(snapshot.results || {}, { preserveDraft: true });\n      renderVoiceLibrary(snapshot.results || {}, { preserveDraft: true });\n      renderAudioAlignment(snapshot.results || {}, { preserveDraft: true });\n    }\n\n    async function startRun() {',
        1,
    )
    html = html.replace(
        '      renderSettings(snapshot.settings || {}, { preserveDraft: false });\n      renderJob(snapshot.job || {});\n    }\n\n    async function stopRun() {',
        '      renderSettings(snapshot.settings || {}, { preserveDraft: false });\n      renderJob(snapshot.job || {});\n      renderResults(snapshot.results || {}, { preserveDraft: true });\n      renderTranscriptReview(snapshot.results || {}, { preserveDraft: true });\n      renderTranslationReview(snapshot.results || {}, { preserveDraft: true });\n      renderVoiceLibrary(snapshot.results || {}, { preserveDraft: true });\n      renderAudioAlignment(snapshot.results || {}, { preserveDraft: true });\n    }\n\n    async function stopRun() {',
        1,
    )
    html = html.replace(
        '      renderSettings(snapshot.settings || {});\n      renderJob(snapshot.job || {});\n    }\n\n    async function bootstrap() {',
        '      renderSettings(snapshot.settings || {});\n      renderJob(snapshot.job || {});\n      renderResults(snapshot.results || {}, { preserveDraft: true });\n      renderTranscriptReview(snapshot.results || {}, { preserveDraft: true });\n      renderTranslationReview(snapshot.results || {}, { preserveDraft: true });\n      renderVoiceLibrary(snapshot.results || {}, { preserveDraft: true });\n      renderAudioAlignment(snapshot.results || {}, { preserveDraft: true });\n    }\n\n    async function bootstrap() {',
        1,
    )
    html = html.replace(
        '    async function bootstrap() {\n      await refreshState({ preserveDraft: false });',
        '    async function bootstrap() {\n      initializeTabs();\n      initializeResultsControls();\n      initializeReviewControls();\n      initializeTranslationControls();\n      initializeVoiceLibraryControls();\n      initializeAudioAlignmentControls();\n      await refreshState({ preserveDraft: false });',
        1,
    )
    return html


def _web_ui_extra_styles() -> str:
    return """
    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding: 4px 24px 0;
    }
    .tab-button {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.58);
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 16px;
      cursor: pointer;
      transition: all 160ms ease;
    }
    .tab-button.active {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }
    .tab-button.review-attention-active {
      border-color: rgba(180, 93, 56, 0.36);
      box-shadow: 0 0 0 3px rgba(180, 93, 56, 0.14);
      background: rgba(255, 247, 240, 0.95);
      color: var(--accent);
    }
    .tab-button.active.review-attention-active {
      box-shadow: 0 0 0 4px rgba(180, 93, 56, 0.18);
    }
    .tab-panel {
      display: none;
      gap: 18px;
    }
    .tab-panel.active {
      display: grid;
    }
    .review-attention-callout {
      transition: box-shadow 180ms ease, border-color 180ms ease, background 180ms ease, transform 180ms ease;
    }
    .review-attention-callout.review-attention-active {
      border-color: rgba(180, 93, 56, 0.34);
      background: linear-gradient(180deg, rgba(255, 247, 238, 0.96), rgba(255, 251, 245, 0.88));
      box-shadow: 0 0 0 3px rgba(180, 93, 56, 0.14);
    }
    .review-attention-pulse {
      animation: reviewAttentionPulse 1.2s ease-in-out 3;
    }
    @keyframes reviewAttentionPulse {
      0%, 100% {
        box-shadow: 0 0 0 0 rgba(180, 93, 56, 0.08);
        transform: translateY(0);
      }
      50% {
        box-shadow: 0 0 0 8px rgba(180, 93, 56, 0.12);
        transform: translateY(-1px);
      }
    }
    .save-state {
      min-height: 20px;
    }
    .save-state.dirty {
      color: #8b5a2b;
    }
    .save-state.saving,
    .save-state.saved {
      color: var(--accent-2);
    }
    .save-state.error {
      color: #b43d2a;
    }
    .artifact-list, .todo-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }
    .artifact-item, .review-card, .todo-list li {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.72);
    }
    .stats-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .summary-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .filter-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: 220px 220px 220px minmax(0, 1fr);
      align-items: end;
      margin-bottom: 14px;
    }
    .field.wide {
      grid-column: 1 / -1;
    }
    .review-list {
      display: grid;
      gap: 12px;
    }
    .review-card-header {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 10px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      background: rgba(39, 95, 88, 0.12);
      color: var(--accent-2);
    }
    .badge.alert {
      background: rgba(180, 93, 56, 0.14);
      color: var(--accent);
    }
    .badge.ok {
      background: rgba(39, 95, 88, 0.16);
      color: var(--accent-2);
    }
    .review-card.target {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(180, 93, 56, 0.16);
    }
    .review-card.playing {
      border-color: var(--accent-2);
      box-shadow: 0 0 0 2px rgba(39, 95, 88, 0.18);
      background: rgba(255, 255, 255, 0.96);
    }
    .review-card.playing .audio-preview-card {
      border-color: rgba(39, 95, 88, 0.28);
    }
    .review-copy {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    .translation-review-textarea {
      width: 100%;
      min-height: 116px;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #fffdf9;
      color: var(--ink);
      line-height: 1.55;
      resize: vertical;
      box-sizing: border-box;
    }
    .audio-preview-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background: rgba(255, 255, 255, 0.68);
      display: grid;
      gap: 8px;
    }
    .section-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    .empty-state {
      padding: 16px;
      border: 1px dashed var(--line);
      border-radius: 14px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.4);
    }
    .pagination {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 14px;
      flex-wrap: wrap;
    }
    .pagination-meta {
      color: var(--muted);
      font-size: 13px;
    }
    .action-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .action-row button {
      margin: 0;
    }
    @media (max-width: 1120px) {
      .stats-grid, .summary-grid, .filter-grid {
        grid-template-columns: 1fr;
      }
    }
"""


def _web_ui_tabs_markup() -> str:
    return """
  <nav class="tabs" id="primary-tabs" aria-label="Web UI 标签页">
    <button id="tab-button-run" class="tab-button active" type="button" data-tab-target="run">运行</button>
    <button id="tab-button-settings" class="tab-button workbench-admin-only" type="button" data-tab-target="settings">设置</button>
    <button id="tab-button-results" class="tab-button" type="button" data-tab-target="results">结果</button>
    <button id="tab-button-review" class="tab-button" type="button" data-tab-target="review">转录与发言人</button>
    <button id="tab-button-translation" class="tab-button" type="button" data-tab-target="translation">翻译与重写</button>
    <button id="tab-button-voice-library" class="tab-button" type="button" data-tab-target="voice-library">音色与语音库</button>
    <button id="tab-button-audio-alignment" class="tab-button" type="button" data-tab-target="audio-alignment">音频试听与对齐</button>
  </nav>"""


def _web_ui_results_panel_markup() -> str:
    return """
    <section class="tab-panel" data-tab-panel="results">
      <section id="review-stage-callout" class="panel review-attention-callout">
        <h2>最近项目结果</h2>
        <p class="hint" id="results-workflow-note">结果页优先展示当前能自动识别到的最近项目目录，以及 editor / publish 产物和只读审校列表。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>项目名</strong><span id="results-project-name">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>结果来源</strong><span id="results-source">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>可用输出数</strong><span id="results-output-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>Needs Review 段落</strong><span id="results-needs-review-count">0</span></li>
          </ul>
        </div>
        <div class="summary-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>项目目录</strong><span id="results-project-dir" class="mono">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>Manifest</strong><span id="results-manifest-path" class="mono">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>Source Kind</strong><span id="results-source-kind">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>Source Locator</strong><span id="results-source-locator" class="mono">-</span></li>
          </ul>
        </div>
      </section>

      <section id="translation-stage-callout" class="panel review-attention-callout">
        <h2>Project State</h2>
        <p class="hint" id="results-project-state-note">这里展示 project_state.json 的阶段摘要，方便观察 legacy process 是否已经对齐到统一状态语义。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>项目状态</strong><span id="results-project-state-status">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>最新阶段</strong><span id="results-project-state-latest-stage">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>已完成阶段</strong><span id="results-project-state-done-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>失败阶段</strong><span id="results-project-state-failed-count">0</span></li>
          </ul>
        </div>
        <div class="summary-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>project_state.json</strong><span id="results-project-state-path" class="mono">-</span></li>
          </ul>
        </div>
        <ul id="results-project-state-list" class="artifact-list"></ul>
      </section>

      <div class="subgrid">
        <section class="panel">
          <h2>Editor 产物</h2>
          <ul id="editor-output-list" class="artifact-list"></ul>
        </section>

        <section class="panel">
          <h2>Publish 产物</h2>
          <ul id="publish-output-list" class="artifact-list"></ul>
        </section>
      </div>

      <section class="panel">
        <h2>Needs Review 段落</h2>
        <p class="hint">这里先做只读集中展示，方便按说话人、关键字和分页快速定位。后续再接逐段确认、重写和试听。</p>
        <div class="filter-grid">
          <div class="field" style="margin-bottom: 0;">
            <label for="needs-review-filter-segment-id">段号搜索 / 跳段</label>
            <div class="toolbar" style="margin-top: 0;">
              <input id="needs-review-filter-segment-id" type="text" placeholder="例如 12" />
              <button id="needs-review-jump" class="secondary" type="button">跳到段落</button>
            </div>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="needs-review-filter-speaker">按说话人筛选</label>
            <select id="needs-review-filter-speaker"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="needs-review-page-size">每页条数</label>
            <select id="needs-review-page-size"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="needs-review-filter-keyword">关键字搜索</label>
            <input id="needs-review-filter-keyword" type="text" placeholder="搜索原文、译文或 TTS 文本" />
          </div>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <span class="status-pill" id="needs-review-total-pill">待处理 0 条</span>
          <span class="hint" id="needs-review-summary">当前没有需要人工处理的段落。</span>
        </div>
        <div id="needs-review-list" class="review-list"></div>
        <div id="needs-review-empty" class="empty-state" style="display: none;">当前筛选条件下没有段落。</div>
        <div class="pagination">
          <div class="pagination-meta" id="needs-review-page-info">第 1 / 1 页</div>
          <div class="toolbar" style="margin-top: 0;">
            <button id="needs-review-prev" class="secondary" type="button">上一页</button>
            <button id="needs-review-next" class="secondary" type="button">下一页</button>
          </div>
        </div>
      </section>
    </section>"""


def _web_ui_review_panel_markup() -> str:
    return """
    <section class="tab-panel" data-tab-panel="review">
      <section class="panel">
        <h2>转录与发言人</h2>
        <div class="toolbar" style="margin-top: 12px; margin-bottom: 12px;">
          <button id="review-save" class="secondary" type="button">保存发言人草稿</button>
          <button id="review-approve" type="button">确认并继续</button>
        </div>
        <div id="review-save-status" class="hint save-state" style="margin-bottom: 12px;">段落确认会同步到项目审校草稿；显示名称修改仍需点击保存发言人草稿。</div>
        <div id="review-gate-message" class="hint" style="margin-bottom: 12px;">当前没有待处理的 S2 确认。</div>
        <div id="review-speaker-editor" class="subgrid" style="margin-bottom: 12px;"></div>
        <p class="hint">这一页先做“查看 + 审校确认”：集中展示全部分段、说话人和转录文本，方便先完成人工复核和初步确认。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>总段落数</strong><span id="review-total-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>说话人数</strong><span id="review-speaker-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>已确认段落</strong><span id="review-confirmed-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>需复核段落</strong><span id="review-needs-review-count">0</span></li>
          </ul>
        </div>
      </section>

      <section class="panel">
        <h2>逐段确认</h2>
        <p class="hint">段落确认会回写到项目审校草稿；显示名称编辑仍可先暂存在当前页，再手动保存。</p>
        <div class="filter-grid">
          <div class="field" style="margin-bottom: 0;">
            <label for="review-filter-segment-id">段号搜索 / 跳段</label>
            <div class="toolbar" style="margin-top: 0;">
              <input id="review-filter-segment-id" type="text" placeholder="例如 12" />
              <button id="review-jump" class="secondary" type="button">跳到段落</button>
            </div>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="review-filter-speaker">按说话人筛选</label>
            <select id="review-filter-speaker"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="review-filter-status">按确认状态筛选</label>
            <select id="review-filter-status"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="review-page-size">每页条数</label>
            <select id="review-page-size"></select>
          </div>
          <div class="field wide" style="margin-bottom: 0;">
            <label for="review-filter-keyword">关键字搜索</label>
            <input id="review-filter-keyword" type="text" placeholder="搜索段号、说话人、转录或中文参考文本" />
          </div>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <span class="status-pill" id="review-total-pill">已确认 0 / 0</span>
          <span class="hint" id="review-summary">当前还没有可复核的分段数据。</span>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <button id="review-bulk-confirm-speaker" class="secondary" type="button">批量确认说话人</button>
          <button id="review-bulk-confirm-transcript" class="secondary" type="button">批量确认转录</button>
          <button id="review-bulk-reset" class="secondary" type="button">批量清空确认</button>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <button id="review-nav-prev" class="secondary" type="button">上一条</button>
          <button id="review-nav-next" class="secondary" type="button">下一条</button>
          <button id="review-nav-next-pending" class="secondary" type="button">下一条待确认</button>
          <button id="review-nav-next-needs-review" class="secondary" type="button">下一条需复核</button>
          <span class="hint" id="review-nav-status">当前还没有可导航的段落。</span>
        </div>
        <div id="review-list" class="review-list"></div>
        <div id="review-empty" class="empty-state" style="display: none;">当前筛选条件下没有段落。</div>
        <div class="pagination">
          <div class="pagination-meta" id="review-page-info">第 1 / 1 页</div>
          <div class="toolbar" style="margin-top: 0;">
            <button id="review-prev" class="secondary" type="button">上一页</button>
            <button id="review-next" class="secondary" type="button">下一页</button>
          </div>
        </div>
      </section>
    </section>"""


def _web_ui_translation_panel_markup() -> str:
    return """
    <section class="tab-panel" data-tab-panel="translation">
      <section class="panel">
        <div class="toolbar" style="margin-top: 12px; margin-bottom: 12px;">
          <button id="translation-save" class="secondary" type="button">保存翻译草稿</button>
          <button id="translation-approve" type="button">确认并继续</button>
        </div>
        <div id="translation-save-status" class="hint save-state" style="margin-bottom: 12px;">翻译确认和需重写标记会同步到项目审校草稿；文本修改仍需点击保存翻译草稿。</div>
        <div id="translation-gate-message" class="hint" style="margin-bottom: 12px;">当前没有待处理的 S3 确认。</div>
        <h2>翻译与重写</h2>
        <p class="hint">这一页先做“查看 + 翻译确认 + 单条标记需重写”：集中看 literal 翻译、TTS 文本和当前重写情况，方便先做人工判断。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>总段落数</strong><span id="translation-total-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>已确认翻译</strong><span id="translation-confirmed-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>标记需重写</strong><span id="translation-rewrite-requested-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>已有重写次数</strong><span id="translation-existing-rewrite-count">0</span></li>
          </ul>
        </div>
      </section>

      <section class="panel">
        <h2>逐段翻译确认</h2>
        <p class="hint">翻译确认和需重写标记会回写到项目审校草稿；文本修改仍可先暂存在当前页，再手动保存。</p>
        <div class="filter-grid">
          <div class="field" style="margin-bottom: 0;">
            <label for="translation-filter-segment-id">段号搜索 / 跳段</label>
            <div class="toolbar" style="margin-top: 0;">
              <input id="translation-filter-segment-id" type="text" placeholder="例如 12" />
              <button id="translation-jump" class="secondary" type="button">跳到段落</button>
            </div>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="translation-filter-speaker">按说话人筛选</label>
            <select id="translation-filter-speaker"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="translation-filter-status">按翻译状态筛选</label>
            <select id="translation-filter-status"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="translation-page-size">每页条数</label>
            <select id="translation-page-size"></select>
          </div>
          <div class="field wide" style="margin-bottom: 0;">
            <label for="translation-filter-keyword">关键字搜索</label>
            <input id="translation-filter-keyword" type="text" placeholder="搜索段号、说话人、直译、TTS 文本或原文" />
          </div>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <span class="status-pill" id="translation-total-pill">已确认 0 / 0</span>
          <span class="hint" id="translation-summary">当前还没有可复核的翻译分段。</span>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <button id="translation-bulk-confirm" class="secondary" type="button">批量确认翻译</button>
          <button id="translation-bulk-mark-rewrite" class="secondary" type="button">批量标记需重写</button>
          <button id="translation-bulk-reset" class="secondary" type="button">批量清空状态</button>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <button id="translation-nav-prev" class="secondary" type="button">上一条</button>
          <button id="translation-nav-next" class="secondary" type="button">下一条</button>
          <button id="translation-nav-next-pending" class="secondary" type="button">下一条待确认</button>
          <button id="translation-nav-next-needs-review" class="secondary" type="button">下一条需复核</button>
          <span class="hint" id="translation-nav-status">当前还没有可导航的段落。</span>
        </div>
        <div id="translation-list" class="review-list"></div>
        <div id="translation-empty" class="empty-state" style="display: none;">当前筛选条件下没有段落。</div>
        <div class="pagination">
          <div class="pagination-meta" id="translation-page-info">第 1 / 1 页</div>
          <div class="toolbar" style="margin-top: 0;">
            <button id="translation-prev" class="secondary" type="button">上一页</button>
            <button id="translation-next" class="secondary" type="button">下一页</button>
          </div>
        </div>
      </section>
    </section>"""


def _web_ui_voice_library_panel_markup() -> str:
    return """
    <section class="tab-panel" data-tab-panel="voice-library">
      <section class="panel review-attention-callout" id="voice-review-wrap" style="display: none;">
        <h2>音色确认</h2>
        <p class="hint" id="voice-review-message">当前没有待处理的音色确认。</p>
        <div id="voice-review-speaker-list" class="review-list"></div>
        <div class="toolbar" style="margin-top: 12px;">
          <button id="voice-review-approve" type="button">确认并继续</button>
          <button id="voice-review-cancel" class="secondary" type="button">取消任务</button>
        </div>
      </section>

      <section class="panel">
        <h2>音色与语音库</h2>
        <p class="hint">这一页先做“查看 + 默认绑定”：展示当前项目 speaker 的音色解析结果、项目默认 builtin，以及注册表里的现有音色记录。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>注册 speaker 数</strong><span id="voice-library-speaker-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>音色总数</strong><span id="voice-library-voice-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>当前项目 speaker 数</strong><span id="voice-library-current-project-speaker-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>builtin 音色数</strong><span id="voice-library-builtin-count">0</span></li>
          </ul>
        </div>
        <div class="summary-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>注册表路径</strong><span id="voice-library-path" class="mono">-</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>项目默认 builtin</strong><span id="voice-library-project-default" class="mono">-</span></li>
          </ul>
        </div>
        <div class="toolbar" style="margin-top: 12px;">
          <span class="hint" id="voice-library-summary">当前还没有音色注册表数据。</span>
          <span class="hint" id="voice-library-status"></span>
        </div>
      </section>

      <section class="panel">
        <h2>当前项目 speaker 绑定</h2>
        <p class="hint">这里优先看当前项目里已经出现的 speaker，方便快速确认每个 speaker 会落到哪个音色上。</p>
        <div id="voice-library-binding-list" class="review-list"></div>
        <div id="voice-library-binding-empty" class="empty-state" style="display: none;">当前项目还没有可绑定的 speaker。</div>
      </section>

      <section class="panel">
        <h2>项目默认 builtin</h2>
        <p class="hint">当某个 speaker 没有命中自己的默认音色时，会回退到项目默认 builtin。这里先支持从现有 builtin 音色中选择。</p>
        <div class="filter-grid">
          <div class="field wide" style="margin-bottom: 0;">
            <label for="voice-library-project-default-select">选择 builtin 音色</label>
            <div class="toolbar" style="margin-top: 0;">
              <select id="voice-library-project-default-select"></select>
              <button id="voice-library-set-project-default" class="secondary" type="button">设为项目默认 builtin</button>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <h2>音色库浏览</h2>
        <p class="hint">当前版本先做浏览和设默认，不在这里直接做增删改。后续再补标签修改、音色删除和导入入口。</p>
        <div class="filter-grid">
          <div class="field" style="margin-bottom: 0;">
            <label for="voice-library-filter-speaker">按 speaker 筛选</label>
            <select id="voice-library-filter-speaker"></select>
          </div>
          <div class="field wide" style="margin-bottom: 0;">
            <label for="voice-library-filter-keyword">关键字搜索</label>
            <input id="voice-library-filter-keyword" type="text" placeholder="搜索 speaker、voice_id、label 或备注" />
          </div>
        </div>
        <div id="voice-library-registry-list" class="review-list"></div>
        <div id="voice-library-registry-empty" class="empty-state" style="display: none;">当前筛选条件下没有音色记录。</div>
      </section>
    </section>"""


def _web_ui_audio_alignment_panel_markup() -> str:
    return """
    <section class="tab-panel" data-tab-panel="audio-alignment">
      <section id="audio-alignment-stage-callout" class="panel review-attention-callout">
        <div id="audio-alignment-gate-message" class="hint" style="margin-bottom: 12px;">当前没有待处理的试听与对齐确认。</div>
        <h2>音频试听与对齐</h2>
        <p class="hint">这一页先做“试听 + 本地确认”：集中查看每段的 TTS / 对齐音频、时长偏差和对齐方法，方便快速找到异常段落。</p>
        <div class="stats-grid" style="margin-top: 14px;">
          <ul class="meta-list">
            <li><strong>总段落数</strong><span id="audio-alignment-total-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>可试听段落</strong><span id="audio-alignment-playable-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>已试听确认</strong><span id="audio-alignment-confirmed-count">0</span></li>
          </ul>
          <ul class="meta-list">
            <li><strong>需复核段落</strong><span id="audio-alignment-needs-review-count">0</span></li>
          </ul>
        </div>
      </section>

      <section class="panel">
        <h2>逐段试听与对齐复核</h2>
        <p class="hint">当前版本的试听确认状态只保存在当前浏览器，不会回写项目文件。后续再接单条重 TTS 和局部重跑。</p>
        <div class="filter-grid">
          <div class="field" style="margin-bottom: 0;">
            <label for="audio-alignment-filter-segment-id">段号搜索 / 跳段</label>
            <div class="toolbar" style="margin-top: 0;">
              <input id="audio-alignment-filter-segment-id" type="text" placeholder="例如 12" />
              <button id="audio-alignment-jump" class="secondary" type="button">跳到段落</button>
            </div>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="audio-alignment-filter-speaker">按说话人筛选</label>
            <select id="audio-alignment-filter-speaker"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="audio-alignment-filter-status">按状态筛选</label>
            <select id="audio-alignment-filter-status"></select>
          </div>
          <div class="field" style="margin-bottom: 0;">
            <label for="audio-alignment-page-size">每页条数</label>
            <select id="audio-alignment-page-size"></select>
          </div>
          <div class="field wide" style="margin-bottom: 0;">
            <label for="audio-alignment-filter-keyword">关键字搜索</label>
            <input id="audio-alignment-filter-keyword" type="text" placeholder="搜索段号、说话人、文本或对齐方法" />
          </div>
        </div>
        <div class="toolbar" style="margin: 0 0 10px;">
          <span class="status-pill" id="audio-alignment-total-pill">已试听 0 / 0</span>
          <span class="hint" id="audio-alignment-summary">当前还没有可试听的分段数据。</span>
        </div>
        <div id="audio-alignment-list" class="review-list"></div>
        <div id="audio-alignment-empty" class="empty-state" style="display: none;">当前筛选条件下没有段落。</div>
        <div class="pagination">
          <div class="pagination-meta" id="audio-alignment-page-info">第 1 / 1 页</div>
          <div class="toolbar" style="margin-top: 0;">
            <button id="audio-alignment-prev" class="secondary" type="button">上一页</button>
            <button id="audio-alignment-next" class="secondary" type="button">下一页</button>
          </div>
        </div>
      </section>
    </section>"""


def _web_ui_script_extension() -> str:
    return """
    const webUiState = {
      activeTab: "run",
      latestJob: null,
      latestResults: null,
      resultsDraft: {
        segmentId: "",
        speaker: "all",
        keyword: "",
        pageSize: 20,
        page: 1,
      },
      reviewDraft: {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: 20,
        page: 1,
      },
      speakerReviewDraft: {
        speakerNames: {},
        segmentSpeakers: {},
      },
      speakerReviewSaveState: null,
      translationDraft: {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: 20,
        page: 1,
      },
      translationSaveState: null,
      voiceLibraryDraft: {
        speaker: "all",
        keyword: "",
      },
      audioAlignmentDraft: {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: 20,
        page: 1,
      },
      targetSegmentId: null,
      reviewTargetSegmentId: null,
      reviewProjectKey: null,
      translationTargetSegmentId: null,
      translationProjectKey: null,
      audioAlignmentTargetSegmentId: null,
      audioAlignmentProjectKey: null,
      lastAutoReviewStage: null,
      reviewAttentionTimer: null,
    };

    const REVIEW_CONFIRMATION_STORAGE_PREFIX = "autodub:web-ui:review-confirmations:";
    const TRANSLATION_REVIEW_STORAGE_PREFIX = "autodub:web-ui:translation-review:";
    const AUDIO_ALIGNMENT_REVIEW_STORAGE_PREFIX = "autodub:web-ui:audio-alignment:";

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function setActiveTab(nextTab) {
      webUiState.activeTab = nextTab;
      document.querySelectorAll("[data-tab-target]").forEach((button) => {
        button.classList.toggle("active", button.dataset.tabTarget === nextTab);
      });
      document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.tabPanel === nextTab);
      });
    }

    function clearReviewAttention() {
      if (webUiState.reviewAttentionTimer !== null) {
        window.clearTimeout(webUiState.reviewAttentionTimer);
        webUiState.reviewAttentionTimer = null;
      }
      document.querySelectorAll(".review-attention-active").forEach((node) => {
        node.classList.remove("review-attention-active");
      });
      document.querySelectorAll(".review-attention-pulse").forEach((node) => {
        node.classList.remove("review-attention-pulse");
      });
    }

    function resolveReviewAttentionElementIds(stage, tab) {
      const tabId = tab ? `tab-button-${tab}` : "";
      const stageElementIds = {
        speaker_review: ["review-stage-callout"],
        translation_review: ["translation-stage-callout"],
        voice_review: ["voice-review-wrap"],
        audio_alignment_review: ["audio-alignment-stage-callout"],
      };
      return [
        "pending-review-wrap",
        ...(tabId ? [tabId] : []),
        ...(stageElementIds[stage] || []),
      ];
    }

    function applyReviewAttention(stage, options = {}) {
      const tab = String(options.tab || "").trim();
      const pulse = options.pulse === true;
      const targetIds = resolveReviewAttentionElementIds(stage, tab);

      clearReviewAttention();
      targetIds.forEach((elementId) => {
        const node = document.getElementById(elementId);
        if (!node) {
          return;
        }
        node.classList.add("review-attention-active");
        if (pulse) {
          node.classList.remove("review-attention-pulse");
          void node.offsetWidth;
          node.classList.add("review-attention-pulse");
        }
      });

      if (pulse) {
        webUiState.reviewAttentionTimer = window.setTimeout(() => {
          targetIds.forEach((elementId) => {
            document.getElementById(elementId)?.classList.remove("review-attention-pulse");
          });
          webUiState.reviewAttentionTimer = null;
        }, 4200);
      }
    }

    function initializeTabs() {
      document.querySelectorAll("[data-tab-target]").forEach((button) => {
        button.addEventListener("click", () => {
          setActiveTab(button.dataset.tabTarget || "run");
        });
      });
      setActiveTab(webUiState.activeTab);
    }

    function readResultsDraft() {
      return {
        segmentId: document.getElementById("needs-review-filter-segment-id")?.value || "",
        speaker: document.getElementById("needs-review-filter-speaker")?.value || "all",
        keyword: document.getElementById("needs-review-filter-keyword")?.value || "",
        pageSize: Number(document.getElementById("needs-review-page-size")?.value || 20),
        page: webUiState.resultsDraft.page || 1,
      };
    }

    function readReviewDraft() {
      return {
        segmentId: document.getElementById("review-filter-segment-id")?.value || "",
        speaker: document.getElementById("review-filter-speaker")?.value || "all",
        status: document.getElementById("review-filter-status")?.value || "all",
        keyword: document.getElementById("review-filter-keyword")?.value || "",
        pageSize: Number(document.getElementById("review-page-size")?.value || 20),
        page: webUiState.reviewDraft.page || 1,
      };
    }

    function readSpeakerReviewDraft() {
      const speakerNames = {};
      document.querySelectorAll("[data-review-speaker-name]").forEach((inputNode) => {
        const speakerId = String(inputNode.dataset.speakerId || "").trim();
        if (speakerId) {
          speakerNames[speakerId] = inputNode.value || "";
        }
      });
      const segmentSpeakers = {};
      document.querySelectorAll("[data-review-segment-speaker]").forEach((selectNode) => {
        const segmentId = String(selectNode.dataset.segmentId || "").trim();
        if (segmentId) {
          segmentSpeakers[segmentId] = selectNode.value || "";
        }
      });
      if (!Object.keys(speakerNames).length && !Object.keys(segmentSpeakers).length) {
        return {
          speakerNames: { ...(webUiState.speakerReviewDraft.speakerNames || {}) },
          segmentSpeakers: { ...(webUiState.speakerReviewDraft.segmentSpeakers || {}) },
        };
      }
      return {
        speakerNames,
        segmentSpeakers,
      };
    }

    function getSpeakerReviewPayload(results) {
      return results.review_flow?.stages?.speaker_review?.payload || {};
    }

    function buildSpeakerReviewDraft(results, items) {
      const payload = getSpeakerReviewPayload(results);
      const draft = readSpeakerReviewDraft();
      const speakerNames = {};
      (payload.speaker_options || []).forEach((option) => {
        const speakerId = String(option.speaker_id || option.value || "").trim();
        if (!speakerId) {
          return;
        }
        speakerNames[speakerId] = String(option.display_name || option.label || speakerId).trim() || speakerId;
      });
      items.forEach((item) => {
        const speakerId = String(item.speaker_id || "").trim();
        if (speakerId && !speakerNames[speakerId]) {
          speakerNames[speakerId] = String(item.display_name || speakerId).trim() || speakerId;
        }
      });
      Object.entries(payload.speaker_names || {}).forEach(([speakerId, displayName]) => {
        if (speakerId) {
          speakerNames[speakerId] = String(displayName || "").trim() || speakerNames[speakerId] || speakerId;
        }
      });
      Object.entries(draft.speakerNames || {}).forEach(([speakerId, displayName]) => {
        if (speakerId) {
          speakerNames[speakerId] = String(displayName || "").trim() || speakerNames[speakerId] || speakerId;
        }
      });

      const segmentSpeakers = {};
      items.forEach((item) => {
        const segmentId = String(item.segment_id || "").trim();
        const speakerId = String(item.speaker_id || "").trim();
        if (segmentId) {
          segmentSpeakers[segmentId] = speakerId;
        }
      });
      Object.entries(payload.segment_speakers || {}).forEach(([segmentId, speakerId]) => {
        if (segmentId && speakerId) {
          segmentSpeakers[segmentId] = String(speakerId);
        }
      });
      Object.entries(draft.segmentSpeakers || {}).forEach(([segmentId, speakerId]) => {
        if (segmentId && speakerId) {
          segmentSpeakers[segmentId] = String(speakerId);
        }
      });

      const normalizedDraft = {
        speakerNames,
        segmentSpeakers,
      };
      webUiState.speakerReviewDraft = normalizedDraft;
      return normalizedDraft;
    }

    function collectSpeakerReviewPayload(results) {
      const projectDir = String(results?.project_dir || "").trim();
      if (!projectDir) {
        throw new Error("当前没有可保存的项目目录。");
      }
      const draft = readSpeakerReviewDraft();
      const confirmations = loadReviewConfirmations(results);
      return {
        project_dir: projectDir,
        speaker_names: draft.speakerNames || {},
        segment_speakers: draft.segmentSpeakers || {},
        confirmations,
      };
    }

    function readTranslationDraft() {
      return {
        segmentId: document.getElementById("translation-filter-segment-id")?.value || "",
        speaker: document.getElementById("translation-filter-speaker")?.value || "all",
        status: document.getElementById("translation-filter-status")?.value || "all",
        keyword: document.getElementById("translation-filter-keyword")?.value || "",
        pageSize: Number(document.getElementById("translation-page-size")?.value || 20),
        page: webUiState.translationDraft.page || 1,
      };
    }

    function readVoiceLibraryDraft() {
      return {
        speaker: document.getElementById("voice-library-filter-speaker")?.value || "all",
        keyword: document.getElementById("voice-library-filter-keyword")?.value || "",
      };
    }

    function readAudioAlignmentDraft() {
      return {
        segmentId: document.getElementById("audio-alignment-filter-segment-id")?.value || "",
        speaker: document.getElementById("audio-alignment-filter-speaker")?.value || "all",
        status: document.getElementById("audio-alignment-filter-status")?.value || "all",
        keyword: document.getElementById("audio-alignment-filter-keyword")?.value || "",
        pageSize: Number(document.getElementById("audio-alignment-page-size")?.value || 20),
        page: webUiState.audioAlignmentDraft.page || 1,
      };
    }

    function formatDuration(ms) {
      if (!Number.isFinite(ms)) {
        return "-";
      }
      const absoluteSeconds = Math.abs(ms / 1000);
      const prefix = ms > 0 ? "+" : ms < 0 ? "-" : "";
      return `${prefix}${absoluteSeconds.toFixed(2)}s`;
    }

    function formatTimestamp(ms) {
      if (!Number.isFinite(ms) || ms < 0) {
        return "--:--.--";
      }
      const totalSeconds = ms / 1000;
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds - minutes * 60;
      return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`;
    }

    function normalizeSegmentId(value) {
      const normalized = String(value ?? "").trim();
      if (!normalized) {
        return "";
      }
      return normalized.replace(/^#/, "");
    }

    function isReviewEditingActive() {
      const activeElement = document.activeElement;
      return Boolean(
        activeElement
        && ["INPUT", "TEXTAREA", "SELECT"].includes(activeElement.tagName)
        && (
          activeElement.closest("#review-speaker-editor")
          || activeElement.closest("#review-list")
        )
      );
    }

    function formatLocalDateTime(value) {
      if (!value) {
        return "";
      }
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) {
        return "";
      }
      return parsed.toLocaleString("zh-CN", { hour12: false });
    }

    function buildSaveIndicatorState(projectKey = "") {
      return {
        projectKey,
        phase: "idle",
        dirty: false,
        updatedAt: "",
        errorMessage: "",
      };
    }

    function getSaveIndicatorConfig(scope) {
      if (scope === "review") {
        return {
          stateKey: "speakerReviewSaveState",
          statusNodeId: "review-save-status",
          saveButtonId: "review-save",
          approveButtonId: "review-approve",
          extraButtonIds: [
            "review-bulk-confirm-speaker",
            "review-bulk-confirm-transcript",
            "review-bulk-reset",
          ],
          idleMessage: "段落确认会同步到项目审校草稿；显示名称修改仍需点击保存发言人草稿。",
          getProjectKey: getReviewStorageKey,
        };
      }
      return {
        stateKey: "translationSaveState",
        statusNodeId: "translation-save-status",
        saveButtonId: "translation-save",
        approveButtonId: "translation-approve",
        extraButtonIds: [
          "translation-bulk-confirm",
          "translation-bulk-mark-rewrite",
          "translation-bulk-reset",
        ],
        idleMessage: "翻译确认和需重写标记会同步到项目审校草稿；文本修改仍需点击保存翻译草稿。",
        getProjectKey: getTranslationStorageKey,
      };
    }

    function ensureSaveIndicatorState(scope, results) {
      const config = getSaveIndicatorConfig(scope);
      const projectKey = config.getProjectKey(results);
      const currentState = webUiState[config.stateKey];
      if (!currentState || currentState.projectKey !== projectKey) {
        webUiState[config.stateKey] = buildSaveIndicatorState(projectKey);
      }
      return webUiState[config.stateKey];
    }

    function setSaveIndicatorState(scope, results, patch) {
      const config = getSaveIndicatorConfig(scope);
      const currentState = ensureSaveIndicatorState(scope, results);
      webUiState[config.stateKey] = {
        ...currentState,
        ...patch,
        projectKey: config.getProjectKey(results),
      };
      renderSaveIndicator(scope, results);
      return webUiState[config.stateKey];
    }

    function markSaveIndicatorDirty(scope, results) {
      const currentState = ensureSaveIndicatorState(scope, results);
      const nextUpdatedAt = currentState.phase === "saved" ? currentState.updatedAt : "";
      return setSaveIndicatorState(scope, results, {
        phase: "dirty",
        dirty: true,
        updatedAt: nextUpdatedAt,
        errorMessage: "",
      });
    }

    function renderSaveIndicator(scope, results) {
      const config = getSaveIndicatorConfig(scope);
      const state = ensureSaveIndicatorState(scope, results);
      const statusNode = document.getElementById(config.statusNodeId);
      const saveButton = document.getElementById(config.saveButtonId);
      const approveButton = document.getElementById(config.approveButtonId);
      const hasProject = Boolean(state.projectKey);

      let message = config.idleMessage;
      let modifier = "idle";
      if (!hasProject) {
        message = "当前没有可保存的项目草稿。";
      } else if (state.phase === "saving") {
        message = "正在保存...";
        modifier = "saving";
      } else if (state.phase === "error") {
        message = `保存失败：${state.errorMessage || "请重试"}`;
        modifier = "error";
      } else if (state.dirty) {
        const updatedAtLabel = formatLocalDateTime(state.updatedAt);
        message = updatedAtLabel
          ? `有未保存修改，上次保存于 ${updatedAtLabel}`
          : "有未保存修改";
        modifier = "dirty";
      } else if (state.phase === "saved") {
        const updatedAtLabel = formatLocalDateTime(state.updatedAt);
        message = updatedAtLabel ? `已保存：${updatedAtLabel}` : "已保存";
        modifier = "saved";
      }

      if (statusNode) {
        statusNode.textContent = message;
        statusNode.className = `hint save-state ${modifier}`;
      }
      if (saveButton) {
        saveButton.disabled = !hasProject || state.phase === "saving";
      }
      if (approveButton) {
        approveButton.disabled = !hasProject || state.phase === "saving";
      }
      (config.extraButtonIds || []).forEach((buttonId) => {
        const extraButton = document.getElementById(buttonId);
        if (extraButton) {
          const filteredCount = Number(extraButton.dataset.filteredCount || "0");
          extraButton.disabled = !hasProject || state.phase === "saving" || filteredCount <= 0;
        }
      });
    }

    function formatVoiceResolutionSource(source) {
      const labels = {
        speaker_default_cloned: "speaker 默认克隆音色",
        speaker_default_builtin: "speaker 默认 builtin",
        project_default_builtin: "项目默认 builtin",
        unresolved: "未命中音色",
      };
      return labels[source] || source || "暂无";
    }

    function formatVerificationStatus(status) {
      const labels = {
        unverified: "未验证",
        verified: "已验证",
        failed: "验证失败",
      };
      return labels[status] || status || "暂无";
    }

    function getReviewStorageKey(results) {
      const projectDir = String(results?.project_dir || "").trim();
      if (!projectDir) {
        return "";
      }
      return `${REVIEW_CONFIRMATION_STORAGE_PREFIX}${projectDir}`;
    }

    function getTranslationStorageKey(results) {
      const projectDir = String(results?.project_dir || "").trim();
      if (!projectDir) {
        return "";
      }
      return `${TRANSLATION_REVIEW_STORAGE_PREFIX}${projectDir}`;
    }

    function loadLocalStorageState(storageKey) {
      if (!storageKey) {
        return {};
      }
      try {
        const rawValue = window.localStorage.getItem(storageKey);
        if (!rawValue) {
          return {};
        }
        const parsed = JSON.parse(rawValue);
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch (_error) {
        return {};
      }
    }

    function buildServerTranscriptReviewState(results) {
      const items = Array.isArray(results?.transcript_review?.items) ? results.transcript_review.items : [];
      const serverState = {};
      items.forEach((item) => {
        const segmentId = String(item?.segment_id || "").trim();
        if (!segmentId) {
          return;
        }
        serverState[segmentId] = {
          speakerConfirmed: Boolean(item?.speaker_confirmed),
          transcriptConfirmed: Boolean(item?.transcript_confirmed),
          updatedAt: String(item?.review_updated_at || ""),
        };
      });
      return serverState;
    }

    function loadReviewConfirmations(results) {
      const serverState = buildServerTranscriptReviewState(results);
      const storageKey = getReviewStorageKey(results);
      if (!storageKey) {
        return serverState;
      }
      const draftState = loadLocalStorageState(storageKey);
      return {
        ...serverState,
        ...draftState,
      };
    }

    function saveReviewConfirmations(results, payload) {
      const storageKey = getReviewStorageKey(results);
      if (!storageKey) {
        return;
      }
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(payload));
      } catch (_error) {
        return;
      }
    }

    function clearReviewConfirmationsStorage(results) {
      const storageKey = getReviewStorageKey(results);
      if (!storageKey) {
        return;
      }
      try {
        window.localStorage.removeItem(storageKey);
      } catch (_error) {
        return;
      }
    }

    function buildServerTranslationReviewState(results) {
      const serverState = {};
      const items = Array.isArray(results?.translation_review?.items) ? results.translation_review.items : [];
      items.forEach((item) => {
        const normalizedSegmentId = String(item?.segment_id || "").trim();
        if (!normalizedSegmentId) {
          return;
        }
        const cnText = typeof item?.cn_text === "string" ? item.cn_text : "";
        const ttsCnText = typeof item?.tts_cn_text === "string" ? item.tts_cn_text : cnText;
        serverState[normalizedSegmentId] = {
          cnText,
          ttsCnText,
          translationConfirmed: Boolean(item?.translation_confirmed),
          rewriteRequested: Boolean(item?.rewrite_requested),
          updatedAt: String(item?.review_updated_at || ""),
        };
      });
      return serverState;
    }

    function loadTranslationReviewState(results) {
      const serverState = buildServerTranslationReviewState(results);
      const storageKey = getTranslationStorageKey(results);
      if (!storageKey) {
        return serverState;
      }
      const draftState = loadLocalStorageState(storageKey);
      const mergedState = { ...serverState };
      Object.entries(draftState).forEach(([segmentId, entry]) => {
        if (!entry || typeof entry !== "object") {
          return;
        }
        const normalizedSegmentId = String(segmentId || "").trim();
        if (!normalizedSegmentId) {
          return;
        }
        mergedState[normalizedSegmentId] = {
          ...(mergedState[normalizedSegmentId] || {}),
          ...entry,
        };
      });
      return mergedState;
    }

    function saveTranslationReviewState(results, payload) {
      const storageKey = getTranslationStorageKey(results);
      if (!storageKey) {
        return;
      }
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(payload));
      } catch (_error) {
        return;
      }
    }

    function clearTranslationReviewStateStorage(results) {
      const storageKey = getTranslationStorageKey(results);
      if (!storageKey) {
        return;
      }
      try {
        window.localStorage.removeItem(storageKey);
      } catch (_error) {
        return;
      }
    }

    function getAudioAlignmentStorageKey(results) {
      const projectDir = String(results?.project_dir || "").trim();
      if (!projectDir) {
        return "";
      }
      return `${AUDIO_ALIGNMENT_REVIEW_STORAGE_PREFIX}${projectDir}`;
    }

    function loadAudioAlignmentState(results) {
      const storageKey = getAudioAlignmentStorageKey(results);
      if (!storageKey) {
        return {};
      }
      try {
        const rawValue = window.localStorage.getItem(storageKey);
        if (!rawValue) {
          return {};
        }
        const parsed = JSON.parse(rawValue);
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch (_error) {
        return {};
      }
    }

    function saveAudioAlignmentState(results, payload) {
      const storageKey = getAudioAlignmentStorageKey(results);
      if (!storageKey) {
        return;
      }
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(payload));
      } catch (_error) {
        return;
      }
    }

    function getTranscriptReviewStatus(entry) {
      const speakerConfirmed = Boolean(entry?.speakerConfirmed);
      const transcriptConfirmed = Boolean(entry?.transcriptConfirmed);
      if (speakerConfirmed && transcriptConfirmed) {
        return {
          code: "confirmed",
          label: "已确认",
          speakerConfirmed,
          transcriptConfirmed,
          updatedAt: entry?.updatedAt || "",
        };
      }
      if (speakerConfirmed || transcriptConfirmed) {
        return {
          code: "partial",
          label: "部分确认",
          speakerConfirmed,
          transcriptConfirmed,
          updatedAt: entry?.updatedAt || "",
        };
      }
      return {
        code: "pending",
        label: "待确认",
        speakerConfirmed,
        transcriptConfirmed,
        updatedAt: entry?.updatedAt || "",
      };
    }

    function updateTranscriptConfirmation(segmentId, patch) {
      const results = webUiState.latestResults || {};
      const confirmations = loadReviewConfirmations(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      const nextEntry = {
        ...(confirmations[normalizedSegmentId] || {}),
        ...patch,
        updatedAt: new Date().toISOString(),
      };
      confirmations[normalizedSegmentId] = nextEntry;
      saveReviewConfirmations(results, confirmations);
      markSaveIndicatorDirty("review", results);
    }

    function clearTranscriptConfirmation(segmentId) {
      const results = webUiState.latestResults || {};
      const confirmations = loadReviewConfirmations(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      delete confirmations[normalizedSegmentId];
      saveReviewConfirmations(results, confirmations);
      markSaveIndicatorDirty("review", results);
    }

    function getTranslationReviewStatus(entry) {
      const translationConfirmed = Boolean(entry?.translationConfirmed);
      const rewriteRequested = Boolean(entry?.rewriteRequested);
      if (rewriteRequested) {
        return {
          code: "rewrite_requested",
          label: "待重写",
          translationConfirmed,
          rewriteRequested,
          updatedAt: entry?.updatedAt || "",
        };
      }
      if (translationConfirmed) {
        return {
          code: "confirmed",
          label: "已确认",
          translationConfirmed,
          rewriteRequested,
          updatedAt: entry?.updatedAt || "",
        };
      }
      return {
        code: "pending",
        label: "待确认",
        translationConfirmed,
        rewriteRequested,
        updatedAt: entry?.updatedAt || "",
      };
    }

    function getAudioAlignmentStatus(entry) {
      const listenedConfirmed = Boolean(entry?.listenedConfirmed);
      if (listenedConfirmed) {
        return {
          code: "confirmed",
          label: "已试听确认",
          listenedConfirmed,
          updatedAt: entry?.updatedAt || "",
        };
      }
      return {
        code: "pending",
        label: "待试听",
        listenedConfirmed,
        updatedAt: entry?.updatedAt || "",
      };
    }

    function updateTranslationReviewState(segmentId, patch) {
      const results = webUiState.latestResults || {};
      const state = loadTranslationReviewState(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      const nextEntry = {
        ...(state[normalizedSegmentId] || {}),
        ...patch,
        updatedAt: new Date().toISOString(),
      };
      state[normalizedSegmentId] = nextEntry;
      saveTranslationReviewState(results, state);
      markSaveIndicatorDirty("translation", results);
    }

    function updateAudioAlignmentState(segmentId, patch) {
      const results = webUiState.latestResults || {};
      const state = loadAudioAlignmentState(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      const nextEntry = {
        ...(state[normalizedSegmentId] || {}),
        ...patch,
        updatedAt: new Date().toISOString(),
      };
      state[normalizedSegmentId] = nextEntry;
      saveAudioAlignmentState(results, state);
    }

    function clearTranslationReviewState(segmentId) {
      const results = webUiState.latestResults || {};
      const state = loadTranslationReviewState(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      delete state[normalizedSegmentId];
      saveTranslationReviewState(results, state);
      markSaveIndicatorDirty("translation", results);
    }

    function clearAudioAlignmentState(segmentId) {
      const results = webUiState.latestResults || {};
      const state = loadAudioAlignmentState(results);
      const normalizedSegmentId = String(segmentId || "");
      if (!normalizedSegmentId) {
        return;
      }
      delete state[normalizedSegmentId];
      saveAudioAlignmentState(results, state);
    }

    function filterTranscriptItems(items, filters, confirmations) {
      let filteredItems = Array.isArray(items) ? items.slice() : [];
      const speaker = filters?.speaker || "all";
      const status = filters?.status || "all";
      const keyword = String(filters?.keyword || "").trim().toLowerCase();
      const segmentId = normalizeSegmentId(filters?.segmentId || "");

      if (speaker !== "all") {
        filteredItems = filteredItems.filter((item) => String(item.speaker_id || "") === speaker);
      }
      if (status === "needs_review") {
        filteredItems = filteredItems.filter((item) => Boolean(item.needs_review));
      } else if (status !== "all") {
        filteredItems = filteredItems.filter((item) => {
          const statusInfo = getTranscriptReviewStatus(
            confirmations[String(item.segment_id || "")] || {}
          );
          return statusInfo.code === status;
        });
      }
      if (keyword) {
        filteredItems = filteredItems.filter((item) => {
          const haystack = [
            item.segment_id,
            item.speaker_id,
            item.display_name,
            item.source_text,
            item.cn_text,
            item.tts_cn_text,
            item.alignment_method,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }
      if (segmentId) {
        filteredItems = filteredItems.filter((item) => String(item.segment_id || "") === segmentId);
      }
      return filteredItems;
    }

    function filterTranslationItems(items, filters, state) {
      let filteredItems = Array.isArray(items) ? items.slice() : [];
      const speaker = filters?.speaker || "all";
      const status = filters?.status || "all";
      const keyword = String(filters?.keyword || "").trim().toLowerCase();
      const segmentId = normalizeSegmentId(filters?.segmentId || "");

      if (speaker !== "all") {
        filteredItems = filteredItems.filter((item) => String(item.speaker_id || "") === speaker);
      }
      if (status === "needs_review") {
        filteredItems = filteredItems.filter((item) => Boolean(item.needs_review));
      } else if (status !== "all") {
        filteredItems = filteredItems.filter((item) => {
          const statusInfo = getTranslationReviewStatus(
            state[String(item.segment_id || "")] || {}
          );
          return statusInfo.code === status;
        });
      }
      if (keyword) {
        filteredItems = filteredItems.filter((item) => {
          const segmentState = state[String(item.segment_id || "")] || {};
          const cnText = typeof segmentState.cnText === "string" ? segmentState.cnText : item.cn_text;
          const ttsCnText = typeof segmentState.ttsCnText === "string" ? segmentState.ttsCnText : item.tts_cn_text;
          const haystack = [
            item.segment_id,
            item.speaker_id,
            item.display_name,
            item.source_text,
            cnText,
            ttsCnText,
            item.alignment_method,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }
      if (segmentId) {
        filteredItems = filteredItems.filter((item) => String(item.segment_id || "") === segmentId);
      }
      return filteredItems;
    }

    function filterAudioAlignmentItems(items, filters, state) {
      let filteredItems = Array.isArray(items) ? items.slice() : [];
      const speaker = filters?.speaker || "all";
      const status = filters?.status || "all";
      const keyword = String(filters?.keyword || "").trim().toLowerCase();
      const segmentId = normalizeSegmentId(filters?.segmentId || "");

      if (speaker !== "all") {
        filteredItems = filteredItems.filter((item) => String(item.speaker_id || "") === speaker);
      }
      if (status === "needs_review") {
        filteredItems = filteredItems.filter((item) => Boolean(item.needs_review));
      } else if (status === "missing_audio") {
        filteredItems = filteredItems.filter((item) => !item.has_audio_preview);
      } else if (status !== "all") {
        filteredItems = filteredItems.filter((item) => {
          const statusInfo = getAudioAlignmentStatus(state[String(item.segment_id || "")] || {});
          return statusInfo.code === status;
        });
      }
      if (keyword) {
        filteredItems = filteredItems.filter((item) => {
          const haystack = [
            item.segment_id,
            item.speaker_id,
            item.display_name,
            item.source_text,
            item.cn_text,
            item.tts_cn_text,
            item.alignment_method,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }
      if (segmentId) {
        filteredItems = filteredItems.filter((item) => String(item.segment_id || "") === segmentId);
      }
      return filteredItems;
    }

    function getCurrentTranscriptReviewFilters() {
      const draft = readReviewDraft();
      return {
        segmentId: normalizeSegmentId(draft.segmentId || ""),
        speaker: draft.speaker || "all",
        status: draft.status || "all",
        keyword: String(draft.keyword || "").trim().toLowerCase(),
      };
    }

    function getFilteredTranscriptReviewItems(results, filters = null, confirmations = null) {
      const transcriptReview = results.transcript_review || {};
      const allItems = Array.isArray(transcriptReview.items) ? transcriptReview.items : [];
      return filterTranscriptItems(
        allItems,
        filters || getCurrentTranscriptReviewFilters(),
        confirmations || loadReviewConfirmations(results)
      );
    }

    function getCurrentTranslationReviewFilters() {
      const draft = readTranslationDraft();
      return {
        segmentId: normalizeSegmentId(draft.segmentId || ""),
        speaker: draft.speaker || "all",
        status: draft.status || "all",
        keyword: String(draft.keyword || "").trim().toLowerCase(),
      };
    }

    function getFilteredTranslationReviewItems(results, filters = null, state = null) {
      const translationReview = results.translation_review || {};
      const allItems = Array.isArray(translationReview.items) ? translationReview.items : [];
      return filterTranslationItems(
        allItems,
        filters || getCurrentTranslationReviewFilters(),
        state || loadTranslationReviewState(results)
      );
    }

    function getCurrentAudioAlignmentFilters() {
      const draft = readAudioAlignmentDraft();
      return {
        segmentId: normalizeSegmentId(draft.segmentId || ""),
        speaker: draft.speaker || "all",
        status: draft.status || "all",
        keyword: String(draft.keyword || "").trim().toLowerCase(),
      };
    }

    function getFilteredAudioAlignmentItems(results, filters = null, state = null) {
      const audioAlignment = results.audio_alignment || {};
      const allItems = Array.isArray(audioAlignment.items) ? audioAlignment.items : [];
      return filterAudioAlignmentItems(
        allItems,
        filters || getCurrentAudioAlignmentFilters(),
        state || loadAudioAlignmentState(results)
      );
    }

    function findSegmentIndex(items, segmentId) {
      const normalizedSegmentId = normalizeSegmentId(segmentId || "");
      if (!normalizedSegmentId) {
        return -1;
      }
      return items.findIndex((item) => String(item.segment_id || "") === normalizedSegmentId);
    }

    function resolveNavigationAnchorIndex(items, targetSegmentId, page, pageSize) {
      if (!items.length) {
        return -1;
      }
      const explicitIndex = findSegmentIndex(items, targetSegmentId);
      if (explicitIndex >= 0) {
        return explicitIndex;
      }
      const normalizedPage = Math.max(1, Number(page || 1));
      const normalizedPageSize = Math.max(1, Number(pageSize || 20));
      return Math.min((normalizedPage - 1) * normalizedPageSize, items.length - 1);
    }

    function findRelativeItemIndex(items, startIndex, direction, predicate = null) {
      if (!items.length) {
        return -1;
      }
      const step = direction === "backward" ? -1 : 1;
      const initialIndex = startIndex < 0
        ? (step > 0 ? 0 : items.length - 1)
        : startIndex + step;
      for (
        let index = initialIndex;
        index >= 0 && index < items.length;
        index += step
      ) {
        if (!predicate || predicate(items[index], index)) {
          return index;
        }
      }
      return -1;
    }

    function scrollTargetCardIntoView(containerId) {
      window.requestAnimationFrame(() => {
        const targetCard = document.querySelector(`#${containerId} .review-card.target`);
        if (targetCard) {
          targetCard.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      });
    }

    function applyReviewDraftToInputs(draft) {
      const speakerSelect = document.getElementById("review-filter-speaker");
      const statusSelect = document.getElementById("review-filter-status");
      const pageSizeSelect = document.getElementById("review-page-size");
      const keywordInput = document.getElementById("review-filter-keyword");
      const segmentIdInput = document.getElementById("review-filter-segment-id");
      if (speakerSelect && Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)) {
        speakerSelect.value = draft.speaker;
      }
      if (statusSelect && Array.from(statusSelect.options).some((option) => option.value === draft.status)) {
        statusSelect.value = draft.status;
      }
      if (pageSizeSelect && Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))) {
        pageSizeSelect.value = String(draft.pageSize);
      }
      if (keywordInput) {
        keywordInput.value = draft.keyword || "";
      }
      if (segmentIdInput) {
        segmentIdInput.value = draft.segmentId || "";
      }
    }

    function applyTranslationDraftToInputs(draft) {
      const speakerSelect = document.getElementById("translation-filter-speaker");
      const statusSelect = document.getElementById("translation-filter-status");
      const pageSizeSelect = document.getElementById("translation-page-size");
      const keywordInput = document.getElementById("translation-filter-keyword");
      const segmentIdInput = document.getElementById("translation-filter-segment-id");
      if (speakerSelect && Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)) {
        speakerSelect.value = draft.speaker;
      }
      if (statusSelect && Array.from(statusSelect.options).some((option) => option.value === draft.status)) {
        statusSelect.value = draft.status;
      }
      if (pageSizeSelect && Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))) {
        pageSizeSelect.value = String(draft.pageSize);
      }
      if (keywordInput) {
        keywordInput.value = draft.keyword || "";
      }
      if (segmentIdInput) {
        segmentIdInput.value = draft.segmentId || "";
      }
    }

    function applyAudioAlignmentDraftToInputs(draft) {
      const speakerSelect = document.getElementById("audio-alignment-filter-speaker");
      const statusSelect = document.getElementById("audio-alignment-filter-status");
      const pageSizeSelect = document.getElementById("audio-alignment-page-size");
      const keywordInput = document.getElementById("audio-alignment-filter-keyword");
      const segmentIdInput = document.getElementById("audio-alignment-filter-segment-id");
      if (speakerSelect && Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)) {
        speakerSelect.value = draft.speaker;
      }
      if (statusSelect && Array.from(statusSelect.options).some((option) => option.value === draft.status)) {
        statusSelect.value = draft.status;
      }
      if (pageSizeSelect && Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))) {
        pageSizeSelect.value = String(draft.pageSize);
      }
      if (keywordInput) {
        keywordInput.value = draft.keyword || "";
      }
      if (segmentIdInput) {
        segmentIdInput.value = draft.segmentId || "";
      }
    }

    function navigateToReviewSegment(segmentId, options = {}) {
      const results = webUiState.latestResults || {};
      const currentDraft = readReviewDraft();
      const nextDraft = {
        segmentId: "",
        speaker: currentDraft.speaker || "all",
        status: currentDraft.status || "all",
        keyword: String(currentDraft.keyword || "").trim().toLowerCase(),
        pageSize: Number(currentDraft.pageSize || webUiState.reviewDraft.pageSize || 20),
        ...(options.draft || {}),
      };
      const candidateItems = getFilteredTranscriptReviewItems(
        results,
        {
          segmentId: "",
          speaker: nextDraft.speaker,
          status: nextDraft.status,
          keyword: nextDraft.keyword,
        },
        options.confirmations || loadReviewConfirmations(results)
      );
      const itemIndex = findSegmentIndex(candidateItems, segmentId);
      if (itemIndex < 0) {
        return false;
      }
      webUiState.reviewTargetSegmentId = normalizeSegmentId(segmentId);
      webUiState.reviewDraft = {
        ...webUiState.reviewDraft,
        ...nextDraft,
        page: Math.floor(itemIndex / Math.max(1, nextDraft.pageSize)) + 1,
      };
      applyReviewDraftToInputs(webUiState.reviewDraft);
      setActiveTab("review");
      rerenderTranscriptReview();
      scrollTargetCardIntoView("review-list");
      return true;
    }

    function navigateToTranslationSegment(segmentId, options = {}) {
      const results = webUiState.latestResults || {};
      const currentDraft = readTranslationDraft();
      const nextDraft = {
        segmentId: "",
        speaker: currentDraft.speaker || "all",
        status: currentDraft.status || "all",
        keyword: String(currentDraft.keyword || "").trim().toLowerCase(),
        pageSize: Number(currentDraft.pageSize || webUiState.translationDraft.pageSize || 20),
        ...(options.draft || {}),
      };
      const candidateItems = getFilteredTranslationReviewItems(
        results,
        {
          segmentId: "",
          speaker: nextDraft.speaker,
          status: nextDraft.status,
          keyword: nextDraft.keyword,
        },
        options.state || loadTranslationReviewState(results)
      );
      const itemIndex = findSegmentIndex(candidateItems, segmentId);
      if (itemIndex < 0) {
        return false;
      }
      webUiState.translationTargetSegmentId = normalizeSegmentId(segmentId);
      webUiState.translationDraft = {
        ...webUiState.translationDraft,
        ...nextDraft,
        page: Math.floor(itemIndex / Math.max(1, nextDraft.pageSize)) + 1,
      };
      applyTranslationDraftToInputs(webUiState.translationDraft);
      setActiveTab("translation");
      rerenderTranslationReviewEnhanced();
      scrollTargetCardIntoView("translation-list");
      return true;
    }

    function navigateToAudioAlignmentSegment(segmentId, options = {}) {
      const results = webUiState.latestResults || {};
      const currentDraft = readAudioAlignmentDraft();
      const nextDraft = {
        segmentId: "",
        speaker: currentDraft.speaker || "all",
        status: currentDraft.status || "all",
        keyword: String(currentDraft.keyword || "").trim().toLowerCase(),
        pageSize: Number(currentDraft.pageSize || webUiState.audioAlignmentDraft.pageSize || 20),
        ...(options.draft || {}),
      };
      const candidateItems = getFilteredAudioAlignmentItems(
        results,
        {
          segmentId: "",
          speaker: nextDraft.speaker,
          status: nextDraft.status,
          keyword: nextDraft.keyword,
        },
        options.state || loadAudioAlignmentState(results)
      );
      const itemIndex = findSegmentIndex(candidateItems, segmentId);
      if (itemIndex < 0) {
        return false;
      }
      webUiState.audioAlignmentTargetSegmentId = normalizeSegmentId(segmentId);
      webUiState.audioAlignmentDraft = {
        ...webUiState.audioAlignmentDraft,
        ...nextDraft,
        page: Math.floor(itemIndex / Math.max(1, nextDraft.pageSize)) + 1,
      };
      applyAudioAlignmentDraftToInputs(webUiState.audioAlignmentDraft);
      setActiveTab("audio-alignment");
      rerenderAudioAlignment();
      scrollTargetCardIntoView("audio-alignment-list");
      return true;
    }

    function openNeedsReviewSegment(tabName, segmentId) {
      const normalizedSegmentId = normalizeSegmentId(segmentId || "");
      if (!normalizedSegmentId) {
        return false;
      }
      if (tabName === "review") {
        return navigateToReviewSegment(normalizedSegmentId, {
          draft: {
            speaker: "all",
            status: "needs_review",
            keyword: "",
            pageSize: Number(document.getElementById("review-page-size")?.value || webUiState.reviewDraft.pageSize || 20),
          },
        });
      }
      if (tabName === "translation") {
        return navigateToTranslationSegment(normalizedSegmentId, {
          draft: {
            speaker: "all",
            status: "needs_review",
            keyword: "",
            pageSize: Number(document.getElementById("translation-page-size")?.value || webUiState.translationDraft.pageSize || 20),
          },
        });
      }
      if (tabName === "audio-alignment") {
        return navigateToAudioAlignmentSegment(normalizedSegmentId, {
          draft: {
            speaker: "all",
            status: "needs_review",
            keyword: "",
            pageSize: Number(document.getElementById("audio-alignment-page-size")?.value || webUiState.audioAlignmentDraft.pageSize || 20),
          },
        });
      }
      return false;
    }

    function navigateTranscriptReview(action) {
      const results = webUiState.latestResults || {};
      const filters = {
        ...getCurrentTranscriptReviewFilters(),
        segmentId: "",
      };
      const confirmations = loadReviewConfirmations(results);
      const candidateItems = getFilteredTranscriptReviewItems(results, filters, confirmations);
      const currentIndex = resolveNavigationAnchorIndex(
        candidateItems,
        webUiState.reviewTargetSegmentId,
        webUiState.reviewDraft.page,
        webUiState.reviewDraft.pageSize
      );
      let targetIndex = -1;
      if (action === "prev") {
        targetIndex = findRelativeItemIndex(candidateItems, currentIndex, "backward");
      } else if (action === "next") {
        targetIndex = findRelativeItemIndex(candidateItems, currentIndex, "forward");
      } else if (action === "next-pending") {
        targetIndex = findRelativeItemIndex(
          candidateItems,
          currentIndex,
          "forward",
          (item) => getTranscriptReviewStatus(confirmations[String(item.segment_id || "")] || {}).code !== "confirmed"
        );
      } else if (action === "next-needs-review") {
        targetIndex = findRelativeItemIndex(
          candidateItems,
          currentIndex,
          "forward",
          (item) => Boolean(item.needs_review)
        );
      }
      if (targetIndex < 0) {
        return;
      }
      navigateToReviewSegment(candidateItems[targetIndex].segment_id, {
        draft: filters,
        confirmations,
      });
    }

    function navigateTranslationReview(action) {
      const results = webUiState.latestResults || {};
      const filters = {
        ...getCurrentTranslationReviewFilters(),
        segmentId: "",
      };
      const state = loadTranslationReviewState(results);
      const candidateItems = getFilteredTranslationReviewItems(results, filters, state);
      const currentIndex = resolveNavigationAnchorIndex(
        candidateItems,
        webUiState.translationTargetSegmentId,
        webUiState.translationDraft.page,
        webUiState.translationDraft.pageSize
      );
      let targetIndex = -1;
      if (action === "prev") {
        targetIndex = findRelativeItemIndex(candidateItems, currentIndex, "backward");
      } else if (action === "next") {
        targetIndex = findRelativeItemIndex(candidateItems, currentIndex, "forward");
      } else if (action === "next-pending") {
        targetIndex = findRelativeItemIndex(
          candidateItems,
          currentIndex,
          "forward",
          (item) => getTranslationReviewStatus(state[String(item.segment_id || "")] || {}).code !== "confirmed"
        );
      } else if (action === "next-needs-review") {
        targetIndex = findRelativeItemIndex(
          candidateItems,
          currentIndex,
          "forward",
          (item) => Boolean(item.needs_review)
        );
      }
      if (targetIndex < 0) {
        return;
      }
      navigateToTranslationSegment(candidateItems[targetIndex].segment_id, {
        draft: filters,
        state,
      });
    }

    function updateBulkActionButton(buttonId, label, count) {
      const buttonNode = document.getElementById(buttonId);
      if (!buttonNode) {
        return;
      }
      buttonNode.textContent = `${label}（${count}）`;
      buttonNode.dataset.filteredCount = String(count);
      buttonNode.disabled = count === 0;
    }

    function updateNavigationButtonState(buttonId, disabled) {
      const buttonNode = document.getElementById(buttonId);
      if (buttonNode) {
        buttonNode.disabled = disabled;
      }
    }

    function updateNavigationStatus(statusNodeId, currentIndex, totalCount) {
      const statusNode = document.getElementById(statusNodeId);
      if (!statusNode) {
        return;
      }
      statusNode.textContent = totalCount
        ? `当前定位 ${Math.max(0, currentIndex) + 1} / ${totalCount}`
        : "当前还没有可导航的段落。";
    }

    function applyBulkTranscriptReviewAction(action) {
      const results = webUiState.latestResults || {};
      const filteredItems = getFilteredTranscriptReviewItems(results);
      if (!filteredItems.length) {
        return;
      }
      const confirmations = loadReviewConfirmations(results);
      const updatedAt = new Date().toISOString();
      filteredItems.forEach((item) => {
        const segmentId = normalizeSegmentId(item.segment_id || "");
        if (!segmentId) {
          return;
        }
        if (action === "reset") {
          delete confirmations[segmentId];
          return;
        }
        confirmations[segmentId] = {
          ...(confirmations[segmentId] || {}),
          ...(action === "confirm-speaker" ? { speakerConfirmed: true } : {}),
          ...(action === "confirm-transcript" ? { transcriptConfirmed: true } : {}),
          updatedAt,
        };
      });
      saveReviewConfirmations(results, confirmations);
      markSaveIndicatorDirty("review", results);
      rerenderTranscriptReview();
      persistSpeakerReviewDraftInBackground();
    }

    function applyBulkTranslationReviewAction(action) {
      const results = webUiState.latestResults || {};
      const filteredItems = getFilteredTranslationReviewItems(results);
      if (!filteredItems.length) {
        return;
      }
      const state = loadTranslationReviewState(results);
      const updatedAt = new Date().toISOString();
      filteredItems.forEach((item) => {
        const segmentId = normalizeSegmentId(item.segment_id || "");
        if (!segmentId) {
          return;
        }
        if (action === "reset") {
          delete state[segmentId];
          return;
        }
        state[segmentId] = {
          ...(state[segmentId] || {}),
          ...(action === "confirm" ? { translationConfirmed: true } : {}),
          ...(action === "mark-rewrite" ? { rewriteRequested: true } : {}),
          updatedAt,
        };
      });
      saveTranslationReviewState(results, state);
      markSaveIndicatorDirty("translation", results);
      rerenderTranslationReviewEnhanced();
      persistTranslationReviewDraftInBackground();
    }

    function buildProjectFileUrl(path) {
      const normalized = String(path || "").trim();
      if (!normalized) {
        return "";
      }
      return `/api/project-file?path=${encodeURIComponent(normalized)}`;
    }

    function buildResultDownloadUrl(projectDir, downloadKey) {
      const normalizedProjectDir = String(projectDir || "").trim();
      const normalizedKey = String(downloadKey || "").trim();
      if (!normalizedProjectDir || !normalizedKey) {
        return "";
      }
      return `/api/result-download?project_dir=${encodeURIComponent(normalizedProjectDir)}&key=${encodeURIComponent(normalizedKey)}`;
    }

    function isTranslationReviewEditingActive() {
      const activeElement = document.activeElement;
      return Boolean(
        activeElement
        && activeElement.tagName === "TEXTAREA"
        && activeElement.closest("#translation-list")
      );
    }

    function isAudioAlignmentPlaybackActive() {
      const audioNodes = document.querySelectorAll("#audio-alignment-list audio");
      return Array.from(audioNodes).some((node) => !node.paused && !node.ended);
    }

    function syncAudioAlignmentPlaybackIndicators() {
      const reviewCards = document.querySelectorAll("#audio-alignment-list .review-card");
      reviewCards.forEach((card) => {
        const hasPlayingAudio = Array.from(card.querySelectorAll("audio")).some(
          (audioNode) => !audioNode.paused && !audioNode.ended
        );
        card.classList.toggle("playing", hasPlayingAudio);
        const indicator = card.querySelector('[data-audio-alignment-role="playing-indicator"]');
        if (indicator) {
          indicator.style.display = hasPlayingAudio ? "inline-flex" : "none";
        }
      });
    }

    function renderArtifactList(containerId, items, emptyText, results = {}) {
      const container = document.getElementById(containerId);
      container.innerHTML = "";
      if (!Array.isArray(items) || items.length === 0) {
        const emptyItem = document.createElement("li");
        emptyItem.className = "artifact-item";
        emptyItem.textContent = emptyText;
        container.appendChild(emptyItem);
        return;
      }
      items.forEach((item) => {
        const li = document.createElement("li");
        li.className = "artifact-item";
        const label = escapeHtml(item.label || "未命名产物");
        const path = item.path ? escapeHtml(item.path) : "暂未生成";
        const downloadUrl = buildResultDownloadUrl(results.project_dir, item.download_key);
        const downloadMarkup = downloadUrl
          ? `<div style="margin-top: 8px;"><a class="secondary" href="${downloadUrl}" download>下载</a></div>`
          : "";
        li.innerHTML = `
          <strong>${label}</strong>
          <div class="mono">${path}</div>
          ${downloadMarkup}
        `;
        container.appendChild(li);
      });
    }

    function renderProjectStateList(projectState) {
      const container = document.getElementById("results-project-state-list");
      container.innerHTML = "";
      const stages = Array.isArray(projectState?.stages) ? projectState.stages : [];
      let emptyText = "当前项目还没有 project_state.json。";
      if (projectState?.load_error) {
        emptyText = `无法读取 project_state.json: ${String(projectState.load_error)}`;
      } else if (projectState?.available) {
        emptyText = "project_state.json 已存在，但还没有可展示的阶段记录。";
      }
      if (stages.length === 0) {
        const emptyItem = document.createElement("li");
        emptyItem.className = "artifact-item";
        emptyItem.textContent = emptyText;
        container.appendChild(emptyItem);
        return;
      }

      stages.forEach((stage) => {
        const li = document.createElement("li");
        li.className = "artifact-item";
        const label = escapeHtml(stage.label || stage.name || "Unknown stage");
        const statusLabel = escapeHtml(stage.status_label || stage.status || "Unknown");
        const detailParts = [];
        if (stage.summary) {
          detailParts.push(String(stage.summary));
        }
        if (stage.updated_at) {
          detailParts.push(`updated: ${String(stage.updated_at)}`);
        }
        const detail = escapeHtml(detailParts.join(" | ") || "-");
        li.innerHTML = `
          <strong>${label} <span class="badge">${statusLabel}</span></strong>
          <div class="mono">${detail}</div>
        `;
        container.appendChild(li);
      });
    }

    function rerenderResults() {
      const results = webUiState.latestResults || {};
      const needsReview = results.needs_review || {};
      const allItems = Array.isArray(needsReview.items) ? needsReview.items : [];
      const draft = readResultsDraft();
      const segmentId = normalizeSegmentId(draft.segmentId);
      const keyword = String(draft.keyword || "").trim().toLowerCase();
      const speaker = draft.speaker || "all";
      const pageSize = Number(draft.pageSize || 20);

      let filteredItems = allItems;
      if (speaker !== "all") {
        filteredItems = filteredItems.filter((item) => String(item.speaker_id || "") === speaker);
      }
      if (keyword) {
        filteredItems = filteredItems.filter((item) => {
          const haystack = [
            item.segment_id,
            item.display_name,
            item.source_text,
            item.cn_text,
            item.tts_cn_text,
            item.alignment_method,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }
      if (segmentId) {
        filteredItems = filteredItems.filter((item) => String(item.segment_id || "") === segmentId);
      }

      const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize) || 1);
      const currentPage = Math.min(Math.max(webUiState.resultsDraft.page || 1, 1), totalPages);
      webUiState.resultsDraft = {
        segmentId,
        speaker,
        keyword,
        pageSize,
        page: currentPage,
      };

      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;
      const pagedItems = filteredItems.slice(startIndex, endIndex);

      document.getElementById("needs-review-total-pill").textContent = `待处理 ${allItems.length} 条`;
      document.getElementById("needs-review-summary").textContent = filteredItems.length
        ? `共筛出 ${filteredItems.length} 条，当前显示第 ${startIndex + 1} - ${Math.min(endIndex, filteredItems.length)} 条。`
        : "当前筛选条件下没有段落。";
      document.getElementById("needs-review-page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
      document.getElementById("needs-review-prev").disabled = currentPage <= 1;
      document.getElementById("needs-review-next").disabled = currentPage >= totalPages;

      const listNode = document.getElementById("needs-review-list");
      const emptyNode = document.getElementById("needs-review-empty");
      listNode.innerHTML = "";
      if (!pagedItems.length) {
        emptyNode.style.display = "block";
        return;
      }
      emptyNode.style.display = "none";

      pagedItems.forEach((item) => {
        const card = document.createElement("article");
        card.className = "review-card";
        if (String(item.segment_id || "") === String(webUiState.targetSegmentId || "")) {
          card.classList.add("target");
        }
        const durationDelta = Number(item.actual_duration_ms || 0) - Number(item.target_duration_ms || 0);
        const selectedDisplayName = String(item.display_name || item.speaker_id || "Unknown speaker");
        card.innerHTML = `
          <div class="review-card-header">
            <strong>段落 #${escapeHtml(item.segment_id || "-")}</strong>
            <span class="badge alert">需要复核</span>
            <span class="badge">状态：待复核</span>
            <span class="badge">${escapeHtml(selectedDisplayName)}</span>
            <span class="badge">对齐：${escapeHtml(item.alignment_method || "-")}</span>
            <span class="badge">重写次数：${escapeHtml(item.rewrite_count || 0)}</span>
            <span class="badge">时长偏差：${formatDuration(durationDelta)}</span>
          </div>
          <div class="review-copy">
            <div>
              <div class="section-label">原文</div>
              <p>${escapeHtml(item.source_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">中文直译</div>
              <p>${escapeHtml(item.cn_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">TTS 文本</div>
              <p>${escapeHtml(item.tts_cn_text || "-")}</p>
            </div>
            <div class="hint">时间范围：${formatTimestamp(Number(item.start_ms || 0))} - ${formatTimestamp(Number(item.end_ms || 0))} ｜ 目标时长：${formatDuration(Number(item.target_duration_ms || 0))} ｜ 实际时长：${formatDuration(Number(item.actual_duration_ms || 0))}</div>
            <div class="hint">入口摘要：当前为只读复核视图，后续会在这里接入音频试听、字幕和局部重跑入口。</div>
          </div>
          <div class="action-row">
            <button class="secondary" type="button" data-results-action="open-review" data-segment-id="${escapeHtml(item.segment_id || "")}">转录页定位</button>
            <button class="secondary" type="button" data-results-action="open-translation" data-segment-id="${escapeHtml(item.segment_id || "")}">翻译页定位</button>
            <button class="secondary" type="button" data-results-action="open-audio" data-segment-id="${escapeHtml(item.segment_id || "")}">对齐页定位</button>
          </div>
        `;
        listNode.appendChild(card);
      });
      if (webUiState.targetSegmentId) {
        scrollTargetCardIntoView("needs-review-list");
      }
    }

    function renderSpeakerReviewEditor(results, items, speakerReviewDraft) {
      const editorNode = document.getElementById("review-speaker-editor");
      const gateMessageNode = document.getElementById("review-gate-message");
      const activeReview = results.review_flow?.active_review || null;
      const isPendingSpeakerReview = activeReview?.stage === "speaker_review" && activeReview?.status === "pending";
      gateMessageNode.textContent = isPendingSpeakerReview
        ? (activeReview.message || activeReview.payload?.message || "当前正在等待你确认说话人。")
        : "当前没有待处理的 S2 确认。";
      editorNode.innerHTML = "";
      const speakerEntries = Object.entries(speakerReviewDraft.speakerNames || {});
      if (!speakerEntries.length) {
        const emptyNode = document.createElement("div");
        emptyNode.className = "empty-state";
        emptyNode.textContent = "当前没有可编辑的发言人。";
        editorNode.appendChild(emptyNode);
        return;
      }
      speakerEntries.forEach(([speakerId, displayName]) => {
        const wrapper = document.createElement("div");
        wrapper.className = "key-card";
        wrapper.innerHTML = `
          <strong>${escapeHtml(speakerId)}</strong>
          <div class="field" style="margin-top: 10px; margin-bottom: 0;">
            <label for="review-speaker-name-${escapeHtml(speakerId)}">显示名称</label>
            <input
              id="review-speaker-name-${escapeHtml(speakerId)}"
              type="text"
              data-review-speaker-name="true"
              data-speaker-id="${escapeHtml(speakerId)}"
              value="${escapeHtml(displayName || speakerId)}"
              placeholder="${escapeHtml(speakerId)}"
            />
          </div>
        `;
        editorNode.appendChild(wrapper);
      });
    }

    function rerenderTranscriptReview() {
      const results = webUiState.latestResults || {};
      const transcriptReview = results.transcript_review || {};
      const allItems = Array.isArray(transcriptReview.items) ? transcriptReview.items : [];
      const confirmations = loadReviewConfirmations(results);
      const reviewDraftState = loadLocalStorageState(getReviewStorageKey(results));
      const hasLocalReviewOverlay = Object.keys(reviewDraftState).length > 0;
      const speakerReviewDraft = buildSpeakerReviewDraft(results, allItems);
      const draft = readReviewDraft();
      const segmentId = normalizeSegmentId(draft.segmentId);
      const speaker = draft.speaker || "all";
      const status = draft.status || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();
      const pageSize = Number(draft.pageSize || 20);
      const filteredItems = getFilteredTranscriptReviewItems(
        results,
        {
          segmentId,
          speaker,
          status,
          keyword,
        },
        confirmations
      );

      const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize) || 1);
      const currentPage = Math.min(Math.max(webUiState.reviewDraft.page || 1, 1), totalPages);
      webUiState.reviewDraft = {
        segmentId,
        speaker,
        status,
        keyword,
        pageSize,
        page: currentPage,
      };

      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;
      const pagedItems = filteredItems.slice(startIndex, endIndex);
      const confirmedCount = !hasLocalReviewOverlay && Number.isFinite(Number(transcriptReview.confirmed_count))
        ? Number(transcriptReview.confirmed_count)
        : allItems.filter((item) => {
          const statusInfo = getTranscriptReviewStatus(confirmations[String(item.segment_id || "")] || {});
          return statusInfo.code === "confirmed";
        }).length;
      const speakerCount = Number.isFinite(Number(transcriptReview.speaker_count))
        ? Number(transcriptReview.speaker_count)
        : Array.isArray(transcriptReview.speaker_options)
          ? transcriptReview.speaker_options.length
          : 0;
      const needsReviewCount = Number.isFinite(Number(transcriptReview.needs_review_count))
        ? Number(transcriptReview.needs_review_count)
        : allItems.filter((item) => Boolean(item.needs_review)).length;
      renderSpeakerReviewEditor(results, allItems, speakerReviewDraft);

      document.getElementById("review-total-count").textContent = String(allItems.length);
      document.getElementById("review-speaker-count").textContent = String(speakerCount);
      document.getElementById("review-confirmed-count").textContent = String(confirmedCount);
      document.getElementById("review-needs-review-count").textContent = String(needsReviewCount);
      document.getElementById("review-total-pill").textContent = `已确认 ${confirmedCount} / ${allItems.length}`;
      document.getElementById("review-summary").textContent = filteredItems.length
        ? `共筛出 ${filteredItems.length} 条，当前显示第 ${startIndex + 1} - ${Math.min(endIndex, filteredItems.length)} 条。`
        : "当前筛选条件下没有段落。";
      document.getElementById("review-page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
      updateBulkActionButton("review-bulk-confirm-speaker", "批量确认说话人", filteredItems.length);
      updateBulkActionButton("review-bulk-confirm-transcript", "批量确认转录", filteredItems.length);
      updateBulkActionButton("review-bulk-reset", "批量清空确认", filteredItems.length);
      const reviewAnchorIndex = resolveNavigationAnchorIndex(
        filteredItems,
        webUiState.reviewTargetSegmentId,
        currentPage,
        pageSize
      );
      const reviewNextPendingIndex = findRelativeItemIndex(
        filteredItems,
        reviewAnchorIndex,
        "forward",
        (item) => getTranscriptReviewStatus(confirmations[String(item.segment_id || "")] || {}).code !== "confirmed"
      );
      const reviewNextNeedsReviewIndex = findRelativeItemIndex(
        filteredItems,
        reviewAnchorIndex,
        "forward",
        (item) => Boolean(item.needs_review)
      );
      updateNavigationStatus("review-nav-status", reviewAnchorIndex, filteredItems.length);
      updateNavigationButtonState("review-nav-prev", filteredItems.length === 0 || reviewAnchorIndex <= 0);
      updateNavigationButtonState(
        "review-nav-next",
        filteredItems.length === 0 || reviewAnchorIndex >= filteredItems.length - 1
      );
      updateNavigationButtonState("review-nav-next-pending", reviewNextPendingIndex < 0);
      updateNavigationButtonState("review-nav-next-needs-review", reviewNextNeedsReviewIndex < 0);
      document.getElementById("review-prev").disabled = currentPage <= 1;
      document.getElementById("review-next").disabled = currentPage >= totalPages;

      const listNode = document.getElementById("review-list");
      const emptyNode = document.getElementById("review-empty");
      listNode.innerHTML = "";
      if (!pagedItems.length) {
        emptyNode.style.display = "block";
        return;
      }
      emptyNode.style.display = "none";

      pagedItems.forEach((item) => {
        const segmentIdKey = String(item.segment_id || "");
        const statusInfo = getTranscriptReviewStatus(confirmations[segmentIdKey] || {});
        const updatedAtLabel = formatLocalDateTime(statusInfo.updatedAt);
        const selectedSpeakerId = String(
          speakerReviewDraft.segmentSpeakers?.[segmentIdKey]
          || item.speaker_id
          || ""
        ).trim() || String(item.speaker_id || "").trim();
        const selectedDisplayName = speakerReviewDraft.speakerNames?.[selectedSpeakerId]
          || item.display_name
          || selectedSpeakerId
          || "Unknown speaker";
        const statusBadgeClass = statusInfo.code === "confirmed"
          ? "badge ok"
          : statusInfo.code === "partial"
            ? "badge"
            : "badge alert";
        const card = document.createElement("article");
        card.className = "review-card";
        if (segmentIdKey === String(webUiState.reviewTargetSegmentId || "")) {
          card.classList.add("target");
        }
        card.innerHTML = `
          <div class="review-card-header">
            <strong>段落 #${escapeHtml(item.segment_id || "-")}</strong>
            <span class="${statusBadgeClass}">审校状态：${escapeHtml(statusInfo.label)}</span>
            ${item.needs_review ? '<span class="badge alert">需要复核</span>' : '<span class="badge ok">未标记复核</span>'}
            <span class="badge">${escapeHtml(selectedDisplayName)}</span>
            <span class="badge">Speaker ID：${escapeHtml(selectedSpeakerId || "-")}</span>
          </div>
          <div class="review-copy">
            <div>
              <div class="section-label">转录原文</div>
              <p>${escapeHtml(item.source_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">中文参考</div>
              <p>${escapeHtml(item.cn_text || "-")}</p>
            </div>
            <div class="hint">时间范围：${formatTimestamp(Number(item.start_ms || 0))} - ${formatTimestamp(Number(item.end_ms || 0))} ｜ 对齐方式：${escapeHtml(item.alignment_method || "-")} ｜ 重写次数：${escapeHtml(item.rewrite_count || 0)}</div>
            <div class="hint">段落确认会写入项目审校草稿；显示名称编辑仍可先暂存，确认后再统一保存。</div>
            ${updatedAtLabel ? `<div class="hint">最近一次审校保存：${escapeHtml(updatedAtLabel)}</div>` : ""}
          </div>
          <div class="field" style="margin-top: 10px; margin-bottom: 0;">
            <label for="review-segment-speaker-${escapeHtml(segmentIdKey)}">本段 speaker</label>
            <select
              id="review-segment-speaker-${escapeHtml(segmentIdKey)}"
              data-review-segment-speaker="true"
              data-segment-id="${escapeHtml(segmentIdKey)}"
            >
              ${Object.entries(speakerReviewDraft.speakerNames || {}).map(([speakerId, displayName]) => `
                <option value="${escapeHtml(speakerId)}" ${speakerId === selectedSpeakerId ? "selected" : ""}>
                  ${escapeHtml(displayName || speakerId)}
                </option>
              `).join("")}
            </select>
          </div>
          <div class="action-row">
            <button class="${statusInfo.speakerConfirmed ? "secondary" : ""}" type="button" data-review-action="toggle-speaker" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.speakerConfirmed ? "取消 speaker 确认" : "确认 speaker"}
            </button>
            <button class="${statusInfo.transcriptConfirmed ? "secondary" : ""}" type="button" data-review-action="toggle-transcript" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.transcriptConfirmed ? "取消转录确认" : "确认转录"}
            </button>
            <button class="secondary" type="button" data-review-action="reset" data-segment-id="${escapeHtml(segmentIdKey)}">清空确认</button>
          </div>
        `;
        listNode.appendChild(card);
      });
      if (webUiState.reviewTargetSegmentId) {
        scrollTargetCardIntoView("review-list");
      }
    }

    function rerenderTranslationReview() {
      const results = webUiState.latestResults || {};
      const translationReview = results.translation_review || {};
      const allItems = Array.isArray(translationReview.items) ? translationReview.items : [];
      const state = loadTranslationReviewState(results);
      const translationDraftState = loadLocalStorageState(getTranslationStorageKey(results));
      const hasLocalTranslationOverlay = Object.keys(translationDraftState).length > 0;
      const draft = readTranslationDraft();
      const segmentId = normalizeSegmentId(draft.segmentId);
      const speaker = draft.speaker || "all";
      const status = draft.status || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();
      const pageSize = Number(draft.pageSize || 20);
      const filteredItems = getFilteredTranslationReviewItems(
        results,
        {
          segmentId,
          speaker,
          status,
          keyword,
        },
        state
      );

      const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize) || 1);
      const currentPage = Math.min(Math.max(webUiState.translationDraft.page || 1, 1), totalPages);
      webUiState.translationDraft = {
        segmentId,
        speaker,
        status,
        keyword,
        pageSize,
        page: currentPage,
      };

      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;
      const pagedItems = filteredItems.slice(startIndex, endIndex);
      const confirmedCount = !hasLocalTranslationOverlay && Number.isFinite(Number(translationReview.confirmed_count))
        ? Number(translationReview.confirmed_count)
        : allItems.filter((item) => {
          const statusInfo = getTranslationReviewStatus(state[String(item.segment_id || "")] || {});
          return statusInfo.code === "confirmed";
        }).length;
      const rewriteRequestedCount = !hasLocalTranslationOverlay && Number.isFinite(Number(translationReview.rewrite_requested_count))
        ? Number(translationReview.rewrite_requested_count)
        : allItems.filter((item) => {
          const statusInfo = getTranslationReviewStatus(state[String(item.segment_id || "")] || {});
          return statusInfo.code === "rewrite_requested";
        }).length;
      const existingRewriteCount = Number.isFinite(Number(translationReview.existing_rewrite_count))
        ? Number(translationReview.existing_rewrite_count)
        : allItems.filter((item) => Number(item.rewrite_count || 0) > 0).length;
      const activeReview = results.review_flow?.active_review || null;
      const gateMessageNode = document.getElementById("translation-gate-message");
      const isPendingTranslationReview = activeReview?.stage === "translation_review" && activeReview?.status === "pending";
      gateMessageNode.textContent = isPendingTranslationReview
        ? (activeReview.message || activeReview.payload?.message || "当前正在等待你确认翻译。")
        : "当前没有待处理的 S3 确认。";

      document.getElementById("translation-total-count").textContent = String(allItems.length);
      document.getElementById("translation-confirmed-count").textContent = String(confirmedCount);
      document.getElementById("translation-rewrite-requested-count").textContent = String(rewriteRequestedCount);
      document.getElementById("translation-existing-rewrite-count").textContent = String(existingRewriteCount);
      document.getElementById("translation-total-pill").textContent = `已确认 ${confirmedCount} / ${allItems.length}`;
      document.getElementById("translation-summary").textContent = filteredItems.length
        ? `共筛出 ${filteredItems.length} 条，当前显示第 ${startIndex + 1} - ${Math.min(endIndex, filteredItems.length)} 条。`
        : "当前筛选条件下没有段落。";
      document.getElementById("translation-page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
      updateBulkActionButton("translation-bulk-confirm", "批量确认翻译", filteredItems.length);
      updateBulkActionButton("translation-bulk-mark-rewrite", "批量标记需重写", filteredItems.length);
      updateBulkActionButton("translation-bulk-reset", "批量清空状态", filteredItems.length);
      document.getElementById("translation-prev").disabled = currentPage <= 1;
      document.getElementById("translation-next").disabled = currentPage >= totalPages;

      const listNode = document.getElementById("translation-list");
      const emptyNode = document.getElementById("translation-empty");
      listNode.innerHTML = "";
      if (!pagedItems.length) {
        emptyNode.style.display = "block";
        return;
      }
      emptyNode.style.display = "none";

      pagedItems.forEach((item) => {
        const segmentIdKey = String(item.segment_id || "");
        const statusInfo = getTranslationReviewStatus(state[segmentIdKey] || {});
        const updatedAtLabel = formatLocalDateTime(statusInfo.updatedAt);
        const statusBadgeClass = statusInfo.code === "confirmed"
          ? "badge ok"
          : statusInfo.code === "rewrite_requested"
            ? "badge alert"
            : "badge";
        const card = document.createElement("article");
        card.className = "review-card";
        if (segmentIdKey === String(webUiState.translationTargetSegmentId || "")) {
          card.classList.add("target");
        }
        card.innerHTML = `
          <div class="review-card-header">
            <strong>段落 #${escapeHtml(item.segment_id || "-")}</strong>
            <span class="${statusBadgeClass}">本地状态：${escapeHtml(statusInfo.label)}</span>
            ${item.needs_review ? '<span class="badge alert">需要复核</span>' : '<span class="badge ok">未标记复核</span>'}
            <span class="badge">${escapeHtml(item.display_name || item.speaker_id || "Unknown speaker")}</span>
            <span class="badge">重写次数：${escapeHtml(item.rewrite_count || 0)}</span>
          </div>
          <div class="review-copy">
            <div>
              <div class="section-label">原文</div>
              <p>${escapeHtml(item.source_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">中文直译</div>
              <p>${escapeHtml(item.cn_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">TTS 文本</div>
              <p>${escapeHtml(item.tts_cn_text || "-")}</p>
            </div>
            <div class="hint">时间范围：${formatTimestamp(Number(item.start_ms || 0))} - ${formatTimestamp(Number(item.end_ms || 0))} ｜ 对齐方式：${escapeHtml(item.alignment_method || "-")} ｜ 当前重写次数：${escapeHtml(item.rewrite_count || 0)}</div>
            <div class="hint">当前只做本地确认和重写标记。后续会在这里接真实重写触发、批量重写和试听回看。</div>
            ${updatedAtLabel ? `<div class="hint">最近一次本地操作：${escapeHtml(updatedAtLabel)}</div>` : ""}
          </div>
          <div class="action-row">
            <button class="${statusInfo.translationConfirmed ? "secondary" : ""}" type="button" data-translation-action="toggle-confirm" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.translationConfirmed ? "取消翻译确认" : "确认翻译"}
            </button>
            <button class="${statusInfo.rewriteRequested ? "secondary" : ""}" type="button" data-translation-action="toggle-rewrite" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.rewriteRequested ? "取消重写标记" : "标记需重写"}
            </button>
            <button class="secondary" type="button" data-translation-action="reset" data-segment-id="${escapeHtml(segmentIdKey)}">清空本地状态</button>
          </div>
        `;
        listNode.appendChild(card);
      });
      if (webUiState.audioAlignmentTargetSegmentId) {
        scrollTargetCardIntoView("audio-alignment-list");
      }
    }

    function rerenderTranslationReviewEnhanced() {
      const results = webUiState.latestResults || {};
      const translationReview = results.translation_review || {};
      const allItems = Array.isArray(translationReview.items) ? translationReview.items : [];
      const state = loadTranslationReviewState(results);
      const translationDraftState = loadLocalStorageState(getTranslationStorageKey(results));
      const hasLocalTranslationOverlay = Object.keys(translationDraftState).length > 0;
      const draft = readTranslationDraft();
      const segmentId = normalizeSegmentId(draft.segmentId);
      const speaker = draft.speaker || "all";
      const status = draft.status || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();
      const pageSize = Number(draft.pageSize || 20);
      const filteredItems = filterTranslationItems(
        allItems,
        {
          segmentId,
          speaker,
          status,
          keyword,
        },
        state
      );

      const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize) || 1);
      const currentPage = Math.min(Math.max(webUiState.translationDraft.page || 1, 1), totalPages);
      webUiState.translationDraft = {
        segmentId,
        speaker,
        status,
        keyword,
        pageSize,
        page: currentPage,
      };

      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;
      const pagedItems = filteredItems.slice(startIndex, endIndex);
      const confirmedCount = !hasLocalTranslationOverlay && Number.isFinite(Number(translationReview.confirmed_count))
        ? Number(translationReview.confirmed_count)
        : allItems.filter((item) => {
          const statusInfo = getTranslationReviewStatus(state[String(item.segment_id || "")] || {});
          return statusInfo.code === "confirmed";
        }).length;
      const rewriteRequestedCount = !hasLocalTranslationOverlay && Number.isFinite(Number(translationReview.rewrite_requested_count))
        ? Number(translationReview.rewrite_requested_count)
        : allItems.filter((item) => {
          const statusInfo = getTranslationReviewStatus(state[String(item.segment_id || "")] || {});
          return statusInfo.code === "rewrite_requested";
        }).length;
      const existingRewriteCount = Number.isFinite(Number(translationReview.existing_rewrite_count))
        ? Number(translationReview.existing_rewrite_count)
        : allItems.filter((item) => Number(item.rewrite_count || 0) > 0).length;
      const activeReview = results.review_flow?.active_review || null;
      const gateMessageNode = document.getElementById("translation-gate-message");
      const isPendingTranslationReview = activeReview?.stage === "translation_review" && activeReview?.status === "pending";
      if (gateMessageNode) {
        gateMessageNode.textContent = isPendingTranslationReview
          ? (activeReview.message || activeReview.payload?.message || "当前正在等待你确认翻译。")
          : "当前没有待处理的 S3 确认。";
      }

      document.getElementById("translation-total-count").textContent = String(allItems.length);
      document.getElementById("translation-confirmed-count").textContent = String(confirmedCount);
      document.getElementById("translation-rewrite-requested-count").textContent = String(rewriteRequestedCount);
      document.getElementById("translation-existing-rewrite-count").textContent = String(existingRewriteCount);
      document.getElementById("translation-total-pill").textContent = `已确认 ${confirmedCount} / ${allItems.length}`;
      document.getElementById("translation-summary").textContent = filteredItems.length
        ? `共筛出 ${filteredItems.length} 条，当前显示第 ${startIndex + 1} - ${Math.min(endIndex, filteredItems.length)} 条。`
        : "当前筛选条件下没有段落。";
      document.getElementById("translation-page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
      updateBulkActionButton("translation-bulk-confirm", "批量确认翻译", filteredItems.length);
      updateBulkActionButton("translation-bulk-mark-rewrite", "批量标记需重写", filteredItems.length);
      updateBulkActionButton("translation-bulk-reset", "批量清空状态", filteredItems.length);
      const translationAnchorIndex = resolveNavigationAnchorIndex(
        filteredItems,
        webUiState.translationTargetSegmentId,
        currentPage,
        pageSize
      );
      const translationNextPendingIndex = findRelativeItemIndex(
        filteredItems,
        translationAnchorIndex,
        "forward",
        (item) => getTranslationReviewStatus(state[String(item.segment_id || "")] || {}).code !== "confirmed"
      );
      const translationNextNeedsReviewIndex = findRelativeItemIndex(
        filteredItems,
        translationAnchorIndex,
        "forward",
        (item) => Boolean(item.needs_review)
      );
      updateNavigationStatus("translation-nav-status", translationAnchorIndex, filteredItems.length);
      updateNavigationButtonState(
        "translation-nav-prev",
        filteredItems.length === 0 || translationAnchorIndex <= 0
      );
      updateNavigationButtonState(
        "translation-nav-next",
        filteredItems.length === 0 || translationAnchorIndex >= filteredItems.length - 1
      );
      updateNavigationButtonState("translation-nav-next-pending", translationNextPendingIndex < 0);
      updateNavigationButtonState("translation-nav-next-needs-review", translationNextNeedsReviewIndex < 0);
      document.getElementById("translation-prev").disabled = currentPage <= 1;
      document.getElementById("translation-next").disabled = currentPage >= totalPages;

      const listNode = document.getElementById("translation-list");
      const emptyNode = document.getElementById("translation-empty");
      listNode.innerHTML = "";
      if (!pagedItems.length) {
        emptyNode.style.display = "block";
        return;
      }
      emptyNode.style.display = "none";

      pagedItems.forEach((item) => {
        const segmentIdKey = String(item.segment_id || "");
        const segmentState = state[segmentIdKey] || {};
        const statusInfo = getTranslationReviewStatus(segmentState);
        const updatedAtLabel = formatLocalDateTime(statusInfo.updatedAt);
        const cnText = typeof segmentState.cnText === "string" ? segmentState.cnText : String(item.cn_text || "");
        const ttsCnText = typeof segmentState.ttsCnText === "string"
          ? segmentState.ttsCnText
          : String(item.tts_cn_text || cnText || "");
        const statusBadgeClass = statusInfo.code === "confirmed"
          ? "badge ok"
          : statusInfo.code === "rewrite_requested"
            ? "badge alert"
            : "badge";
        const card = document.createElement("article");
        card.className = "review-card";
        if (segmentIdKey === String(webUiState.translationTargetSegmentId || "")) {
          card.classList.add("target");
        }
        card.innerHTML = `
          <div class="review-card-header">
            <strong>段落 #${escapeHtml(item.segment_id || "-")}</strong>
            <span class="${statusBadgeClass}">确认状态：${escapeHtml(statusInfo.label)}</span>
            ${item.needs_review ? '<span class="badge alert">需要复核</span>' : '<span class="badge ok">未标记复核</span>'}
            <span class="badge">${escapeHtml(item.display_name || item.speaker_id || "Unknown speaker")}</span>
            <span class="badge">重写次数：${escapeHtml(item.rewrite_count || 0)}</span>
          </div>
          <div class="review-copy">
            <div>
              <div class="section-label">原文</div>
              <p>${escapeHtml(item.source_text || "-")}</p>
            </div>
            <div>
              <div class="section-label">中文直译</div>
              <textarea
                id="translation-cn-text-${escapeHtml(segmentIdKey)}"
                class="translation-review-textarea"
                rows="3"
                spellcheck="false"
                data-translation-cn-text="true"
                data-segment-id="${escapeHtml(segmentIdKey)}"
              >${escapeHtml(cnText)}</textarea>
            </div>
            <div>
              <div class="section-label">TTS 文本</div>
              <textarea
                id="translation-tts-text-${escapeHtml(segmentIdKey)}"
                class="translation-review-textarea"
                rows="3"
                spellcheck="false"
                data-translation-tts-text="true"
                data-segment-id="${escapeHtml(segmentIdKey)}"
              >${escapeHtml(ttsCnText)}</textarea>
            </div>
            <div class="hint">时间范围：${formatTimestamp(Number(item.start_ms || 0))} - ${formatTimestamp(Number(item.end_ms || 0))} ｜ 对齐方式：${escapeHtml(item.alignment_method || "-")} ｜ 当前重写次数：${escapeHtml(item.rewrite_count || 0)}</div>
            <div class="hint">可以先修改文案、标记需重写，再保存或直接确认。</div>
            ${updatedAtLabel ? `<div class="hint">最近一次状态更新：${escapeHtml(updatedAtLabel)}</div>` : ""}
          </div>
          <div class="action-row">
            <button class="${statusInfo.translationConfirmed ? "secondary" : ""}" type="button" data-translation-action="toggle-confirm" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.translationConfirmed ? "取消翻译确认" : "确认翻译"}
            </button>
            <button class="${statusInfo.rewriteRequested ? "secondary" : ""}" type="button" data-translation-action="toggle-rewrite" data-segment-id="${escapeHtml(segmentIdKey)}">
              ${statusInfo.rewriteRequested ? "取消重写标记" : "标记需重写"}
            </button>
            <button class="secondary" type="button" data-translation-action="reset" data-segment-id="${escapeHtml(segmentIdKey)}">清空状态</button>
          </div>
        `;
        listNode.appendChild(card);
      });
      if (webUiState.translationTargetSegmentId) {
        scrollTargetCardIntoView("translation-list");
      }
    }

    function rerenderVoiceLibrary() {
      const results = webUiState.latestResults || {};
      const voiceLibrary = results.voice_library || {};
      const registrySpeakers = Array.isArray(voiceLibrary.speakers) ? voiceLibrary.speakers : [];
      const currentProjectSpeakers = Array.isArray(voiceLibrary.current_project_speakers)
        ? voiceLibrary.current_project_speakers
        : [];
      const draft = readVoiceLibraryDraft();
      const speaker = draft.speaker || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();

      let filteredRegistrySpeakers = registrySpeakers;
      if (speaker !== "all") {
        filteredRegistrySpeakers = filteredRegistrySpeakers.filter(
          (item) => String(item.speaker_id || "") === speaker
        );
      }
      if (keyword) {
        filteredRegistrySpeakers = filteredRegistrySpeakers.filter((item) => {
          const voiceHaystack = Array.isArray(item.voices)
            ? item.voices.map((voice) => [
              voice.voice_id,
              voice.label,
              voice.notes,
              voice.verification_status,
            ].join(" ")).join(" ")
            : "";
          const haystack = [
            item.speaker_id,
            item.speaker_name,
            item.default_voice_id,
            voiceHaystack,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }

      const filteredCurrentSpeakers = currentProjectSpeakers.filter((item) => {
        if (speaker !== "all" && String(item.speaker_id || "") !== speaker) {
          return false;
        }
        if (!keyword) {
          return true;
        }
        const haystack = [
          item.speaker_id,
          item.display_name,
          item.speaker_name,
          item.default_voice_id,
          item.resolved_voice_id,
          item.resolved_label,
        ].join(" ").toLowerCase();
        return haystack.includes(keyword);
      });

      document.getElementById("voice-library-speaker-count").textContent = String(voiceLibrary.speaker_count || 0);
      document.getElementById("voice-library-voice-count").textContent = String(voiceLibrary.voice_count || 0);
      document.getElementById("voice-library-current-project-speaker-count").textContent = String(currentProjectSpeakers.length);
      document.getElementById("voice-library-builtin-count").textContent = String(voiceLibrary.builtin_voice_count || 0);
      document.getElementById("voice-library-path").textContent = voiceLibrary.path || "-";
      const projectDefault = voiceLibrary.project_default_builtin_voice;
      document.getElementById("voice-library-project-default").textContent = projectDefault
        ? `${projectDefault.voice_id} ｜ ${projectDefault.label || "未命名"}`
        : "未设置";
      document.getElementById("voice-library-summary").textContent = voiceLibrary.load_error
        ? `注册表加载失败：${voiceLibrary.load_error}`
        : `当前展示 ${filteredRegistrySpeakers.length} 个 speaker 条目，当前项目识别到 ${currentProjectSpeakers.length} 个 speaker。`;

      const bindingList = document.getElementById("voice-library-binding-list");
      const bindingEmpty = document.getElementById("voice-library-binding-empty");
      bindingList.innerHTML = "";
      if (!filteredCurrentSpeakers.length) {
        bindingEmpty.style.display = "block";
      } else {
        bindingEmpty.style.display = "none";
        filteredCurrentSpeakers.forEach((item) => {
          const card = document.createElement("article");
          card.className = "review-card";
          card.dataset.speakerId = String(item.speaker_id || "");
          const voices = Array.isArray(item.available_voices) ? item.available_voices : [];
          const options = voices.map((voice) => `
            <option value="${escapeHtml(voice.voice_id || "")}" ${voice.voice_id === item.default_voice_id ? "selected" : ""}>
              ${escapeHtml(voice.voice_id || "-")} ｜ ${escapeHtml(voice.voice_type || "-")} ｜ ${escapeHtml(voice.label || "未命名")} ｜ ${escapeHtml(formatVerificationStatus(voice.verification_status))}
            </option>
          `).join("");
          const emptyOption = '<option value="">当前没有可选音色</option>';
          card.innerHTML = `
            <div class="review-card-header">
              <strong>${escapeHtml(item.display_name || item.speaker_id || "Unknown speaker")}</strong>
              <span class="badge">Speaker ID：${escapeHtml(item.speaker_id || "-")}</span>
              <span class="badge">解析来源：${escapeHtml(formatVoiceResolutionSource(item.resolved_source))}</span>
              <span class="${item.resolved_status === "resolved" ? "badge ok" : "badge alert"}">
                当前解析：${escapeHtml(item.resolved_voice_id || "未命中")}
              </span>
            </div>
            <div class="review-copy">
              <div class="hint">注册表默认：${escapeHtml(item.default_voice_id || "未设置")} ｜ 类型：${escapeHtml(item.default_voice_type || "暂无")} ｜ 当前解析标签：${escapeHtml(item.resolved_label || "暂无")}</div>
              <div class="toolbar" style="margin-top: 0;">
                <select data-voice-library-role="speaker-default-select">
                  ${voices.length ? options : emptyOption}
                </select>
                <button type="button" class="secondary" data-voice-library-action="set-speaker-default" data-speaker-id="${escapeHtml(item.speaker_id || "")}" ${voices.length ? "" : "disabled"}>设为 speaker 默认</button>
              </div>
            </div>
          `;
          bindingList.appendChild(card);
        });
      }

      const registryList = document.getElementById("voice-library-registry-list");
      const registryEmpty = document.getElementById("voice-library-registry-empty");
      registryList.innerHTML = "";
      if (!filteredRegistrySpeakers.length) {
        registryEmpty.style.display = "block";
      } else {
        registryEmpty.style.display = "none";
        filteredRegistrySpeakers.forEach((speakerItem) => {
          const card = document.createElement("article");
          card.className = "review-card";
          const voices = Array.isArray(speakerItem.voices) ? speakerItem.voices : [];
          const voiceListMarkup = voices.length
            ? voices.map((voice) => `
                <div class="hint">
                  ${escapeHtml(voice.voice_id || "-")} ｜ ${escapeHtml(voice.voice_type || "-")} ｜ ${escapeHtml(voice.label || "未命名")} ｜ ${escapeHtml(formatVerificationStatus(voice.verification_status))}
                </div>
              `).join("")
            : '<div class="hint">当前没有音色记录。</div>';
          card.innerHTML = `
            <div class="review-card-header">
              <strong>${escapeHtml(speakerItem.speaker_name || speakerItem.speaker_id || "Unknown speaker")}</strong>
              <span class="badge">Speaker ID：${escapeHtml(speakerItem.speaker_id || "-")}</span>
              <span class="badge">默认：${escapeHtml(speakerItem.default_voice_id || "未设置")}</span>
              <span class="badge">解析来源：${escapeHtml(formatVoiceResolutionSource(speakerItem.resolution_source))}</span>
            </div>
            <div class="review-copy">
              ${voiceListMarkup}
            </div>
          `;
          registryList.appendChild(card);
        });
      }
    }

    function renderSnapshot(snapshot, options = {}) {
      renderSettings(snapshot.settings || {}, options);
      renderJob(snapshot.job || {});
      renderResults(snapshot.results || {}, options);
      renderTranscriptReview(snapshot.results || {}, options);
      renderTranslationReview(snapshot.results || {}, options);
      renderVoiceLibrary(snapshot.results || {}, options);
      renderAudioAlignment(snapshot.results || {}, options);
      renderReviewFlow(snapshot.results || {}, snapshot.job || {});
    }

    function renderResults(results, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      webUiState.latestResults = results;
      const projectState = results.project_state || {};
      const sourceContext = results.source_context || {};

      document.getElementById("results-project-name").textContent = results.project_name || "-";
      document.getElementById("results-source").textContent = results.source_label || results.source || "-";
      document.getElementById("results-output-count").textContent = String(results.available_output_count || 0);
      document.getElementById("results-needs-review-count").textContent = String(results.needs_review?.total_items || 0);
      document.getElementById("results-project-dir").textContent = results.project_dir || "-";
      document.getElementById("results-manifest-path").textContent = results.manifest_path || "-";
      document.getElementById("results-source-kind").textContent = sourceContext.source_kind || "-";
      document.getElementById("results-source-locator").textContent = sourceContext.locator || "-";
      document.getElementById("results-project-state-status").textContent =
        projectState.overall_status_label || projectState.overall_status || "-";
      document.getElementById("results-project-state-latest-stage").textContent =
        projectState.latest_stage_label
          ? `${projectState.latest_stage_label} (${projectState.latest_stage_status_label || projectState.latest_stage_status || "-"})`
          : "-";
      document.getElementById("results-project-state-done-count").textContent = String(
        projectState.completed_stage_count || 0
      );
      document.getElementById("results-project-state-failed-count").textContent = String(
        projectState.failed_stage_count || 0
      );
      document.getElementById("results-project-state-path").textContent = projectState.path || "-";
      document.getElementById("results-workflow-note").textContent = results.workflow_note || "当前还没有可展示的项目结果。";
      document.getElementById("results-project-state-note").textContent = projectState.load_error
        ? `无法读取 project_state.json: ${projectState.load_error}`
        : projectState.available
          ? "这里展示 process 已回写的统一阶段状态，方便验证 legacy 与新主干的收敛进度。"
          : "当前项目还没有 project_state.json。";

      renderArtifactList("editor-output-list", results.editor_outputs || [], "当前没有可展示的 editor 产物。", results);
      renderArtifactList("publish-output-list", results.publish_outputs || [], "当前没有可展示的 publish 产物。", results);
      renderProjectStateList(projectState);

      const speakerSelect = document.getElementById("needs-review-filter-speaker");
      const pageSizeSelect = document.getElementById("needs-review-page-size");
      const keywordInput = document.getElementById("needs-review-filter-keyword");
      const segmentIdInput = document.getElementById("needs-review-filter-segment-id");
      const draft = preserveDraft ? readResultsDraft() : {
        segmentId: "",
        speaker: "all",
        keyword: "",
        pageSize: Number(results.needs_review?.default_page_size || 20),
        page: 1,
      };

      speakerSelect.innerHTML = "";
      [{ value: "all", label: "全部说话人" }, ...(results.needs_review?.speaker_options || [])].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });

      pageSizeSelect.innerHTML = "";
      (results.needs_review?.page_size_options || [20]).forEach((pageSize) => {
        const node = document.createElement("option");
        node.value = String(pageSize);
        node.textContent = `${pageSize} 条 / 页`;
        pageSizeSelect.appendChild(node);
      });

      speakerSelect.value = Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)
        ? draft.speaker
        : "all";
      pageSizeSelect.value = Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))
        ? String(draft.pageSize)
        : String(results.needs_review?.default_page_size || 20);
      keywordInput.value = draft.keyword || "";
      segmentIdInput.value = draft.segmentId || "";

      webUiState.resultsDraft = {
        segmentId: segmentIdInput.value,
        speaker: speakerSelect.value,
        keyword: keywordInput.value,
        pageSize: Number(pageSizeSelect.value || 20),
        page: preserveDraft ? webUiState.resultsDraft.page || 1 : 1,
      };
      rerenderResults();
    }

    function renderTranscriptReview(results, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      const transcriptReview = results.transcript_review || {};
      const projectKey = getReviewStorageKey(results);
      ensureSaveIndicatorState("review", results);
      if (
        preserveDraft
        && webUiState.reviewProjectKey === projectKey
        && isReviewEditingActive()
      ) {
        webUiState.speakerReviewDraft = readSpeakerReviewDraft();
        renderSaveIndicator("review", results);
        return;
      }
      if (webUiState.reviewProjectKey !== projectKey) {
        webUiState.reviewProjectKey = projectKey;
        webUiState.reviewTargetSegmentId = null;
        if (!preserveDraft) {
          webUiState.reviewDraft.page = 1;
        }
      }

      const speakerSelect = document.getElementById("review-filter-speaker");
      const statusSelect = document.getElementById("review-filter-status");
      const pageSizeSelect = document.getElementById("review-page-size");
      const keywordInput = document.getElementById("review-filter-keyword");
      const segmentIdInput = document.getElementById("review-filter-segment-id");
      const draft = preserveDraft ? readReviewDraft() : {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: Number(transcriptReview.default_page_size || 20),
        page: 1,
      };

      speakerSelect.innerHTML = "";
      [{ value: "all", label: "全部说话人" }, ...(transcriptReview.speaker_options || [])].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });

      statusSelect.innerHTML = "";
      [
        { value: "all", label: "全部状态" },
        { value: "pending", label: "待确认" },
        { value: "partial", label: "部分确认" },
        { value: "confirmed", label: "已确认" },
        { value: "needs_review", label: "仅看需复核" },
      ].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        statusSelect.appendChild(node);
      });

      pageSizeSelect.innerHTML = "";
      (transcriptReview.page_size_options || [20]).forEach((pageSize) => {
        const node = document.createElement("option");
        node.value = String(pageSize);
        node.textContent = `${pageSize} 条 / 页`;
        pageSizeSelect.appendChild(node);
      });

      speakerSelect.value = Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)
        ? draft.speaker
        : "all";
      statusSelect.value = Array.from(statusSelect.options).some((option) => option.value === draft.status)
        ? draft.status
        : "all";
      pageSizeSelect.value = Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))
        ? String(draft.pageSize)
        : String(transcriptReview.default_page_size || 20);
      keywordInput.value = draft.keyword || "";
      segmentIdInput.value = draft.segmentId || "";

      webUiState.reviewDraft = {
        segmentId: segmentIdInput.value,
        speaker: speakerSelect.value,
        status: statusSelect.value,
        keyword: keywordInput.value,
        pageSize: Number(pageSizeSelect.value || 20),
        page: preserveDraft ? webUiState.reviewDraft.page || 1 : 1,
      };
      rerenderTranscriptReview();
      renderSaveIndicator("review", results);
    }

    function renderTranslationReview(results, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      const translationReview = results.translation_review || {};
      const projectKey = getTranslationStorageKey(results);
      ensureSaveIndicatorState("translation", results);
      if (
        preserveDraft
        && webUiState.translationProjectKey === projectKey
        && isTranslationReviewEditingActive()
      ) {
        renderSaveIndicator("translation", results);
        return;
      }
      if (webUiState.translationProjectKey !== projectKey) {
        webUiState.translationProjectKey = projectKey;
        webUiState.translationTargetSegmentId = null;
        if (!preserveDraft) {
          webUiState.translationDraft.page = 1;
        }
      }

      const speakerSelect = document.getElementById("translation-filter-speaker");
      const statusSelect = document.getElementById("translation-filter-status");
      const pageSizeSelect = document.getElementById("translation-page-size");
      const keywordInput = document.getElementById("translation-filter-keyword");
      const segmentIdInput = document.getElementById("translation-filter-segment-id");
      const draft = preserveDraft ? readTranslationDraft() : {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: Number(translationReview.default_page_size || 20),
        page: 1,
      };

      speakerSelect.innerHTML = "";
      [{ value: "all", label: "全部说话人" }, ...(translationReview.speaker_options || [])].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });

      statusSelect.innerHTML = "";
      [
        { value: "all", label: "全部状态" },
        { value: "pending", label: "待确认" },
        { value: "confirmed", label: "已确认" },
        { value: "rewrite_requested", label: "待重写" },
        { value: "needs_review", label: "仅看需复核" },
      ].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        statusSelect.appendChild(node);
      });

      pageSizeSelect.innerHTML = "";
      (translationReview.page_size_options || [20]).forEach((pageSize) => {
        const node = document.createElement("option");
        node.value = String(pageSize);
        node.textContent = `${pageSize} 条 / 页`;
        pageSizeSelect.appendChild(node);
      });

      speakerSelect.value = Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)
        ? draft.speaker
        : "all";
      statusSelect.value = Array.from(statusSelect.options).some((option) => option.value === draft.status)
        ? draft.status
        : "all";
      pageSizeSelect.value = Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))
        ? String(draft.pageSize)
        : String(translationReview.default_page_size || 20);
      keywordInput.value = draft.keyword || "";
      segmentIdInput.value = draft.segmentId || "";

      webUiState.translationDraft = {
        segmentId: segmentIdInput.value,
        speaker: speakerSelect.value,
        status: statusSelect.value,
        keyword: keywordInput.value,
        pageSize: Number(pageSizeSelect.value || 20),
        page: preserveDraft ? webUiState.translationDraft.page || 1 : 1,
      };
      rerenderTranslationReviewEnhanced();
      renderSaveIndicator("translation", results);
    }

    function rerenderAudioAlignment() {
      const results = webUiState.latestResults || {};
      const audioAlignment = results.audio_alignment || {};
      const activeReview = results.review_flow?.active_review || null;
      const allItems = Array.isArray(audioAlignment.items) ? audioAlignment.items : [];
      const state = loadAudioAlignmentState(results);
      const draft = readAudioAlignmentDraft();
      const segmentId = normalizeSegmentId(draft.segmentId);
      const speaker = draft.speaker || "all";
      const status = draft.status || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();
      const pageSize = Number(draft.pageSize || 20);
      const filteredItems = filterAudioAlignmentItems(
        allItems,
        {
          segmentId,
          speaker,
          status,
          keyword,
        },
        state
      );

      const totalPages = Math.max(1, Math.ceil(filteredItems.length / pageSize) || 1);
      const currentPage = Math.min(Math.max(webUiState.audioAlignmentDraft.page || 1, 1), totalPages);
      webUiState.audioAlignmentDraft = {
        segmentId,
        speaker,
        status,
        keyword,
        pageSize,
        page: currentPage,
      };

      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;
      const pagedItems = filteredItems.slice(startIndex, endIndex);
      const confirmedCount = allItems.filter((item) => {
        const statusInfo = getAudioAlignmentStatus(state[String(item.segment_id || "")] || {});
        return statusInfo.code === "confirmed";
      }).length;
      const playableCount = allItems.filter((item) => Boolean(item.has_audio_preview)).length;
      const needsReviewCount = allItems.filter((item) => Boolean(item.needs_review)).length;
      const gateMessageNode = document.getElementById("audio-alignment-gate-message");
      const isPendingAudioAlignmentReview = activeReview?.stage === "audio_alignment_review" && activeReview?.status === "pending";
      if (gateMessageNode) {
        gateMessageNode.textContent = isPendingAudioAlignmentReview
          ? (activeReview.message || activeReview.payload?.message || "当前正在等待你试听并确认对齐结果。")
          : "当前没有待处理的试听与对齐确认。";
      }

      document.getElementById("audio-alignment-total-count").textContent = String(allItems.length);
      document.getElementById("audio-alignment-playable-count").textContent = String(playableCount);
      document.getElementById("audio-alignment-confirmed-count").textContent = String(confirmedCount);
      document.getElementById("audio-alignment-needs-review-count").textContent = String(needsReviewCount);
      document.getElementById("audio-alignment-total-pill").textContent = `已试听 ${confirmedCount} / ${allItems.length}`;
      document.getElementById("audio-alignment-summary").textContent = filteredItems.length
        ? `共筛出 ${filteredItems.length} 条，当前显示第 ${startIndex + 1} - ${Math.min(endIndex, filteredItems.length)} 条。`
        : "当前筛选条件下没有段落。";
      document.getElementById("audio-alignment-page-info").textContent = `第 ${currentPage} / ${totalPages} 页`;
      document.getElementById("audio-alignment-prev").disabled = currentPage <= 1;
      document.getElementById("audio-alignment-next").disabled = currentPage >= totalPages;

      const listNode = document.getElementById("audio-alignment-list");
      const emptyNode = document.getElementById("audio-alignment-empty");
      listNode.innerHTML = "";
      if (!pagedItems.length) {
        emptyNode.style.display = "block";
        return;
      }
      emptyNode.style.display = "none";

      pagedItems.forEach((item) => {
        const segmentIdKey = String(item.segment_id || "");
        const statusInfo = getAudioAlignmentStatus(state[segmentIdKey] || {});
        const updatedAtLabel = formatLocalDateTime(statusInfo.updatedAt);
        const durationDelta = Number(item.actual_duration_ms || 0) - Number(item.target_duration_ms || 0);
        const card = document.createElement("article");
        card.className = "review-card";
        if (segmentIdKey === String(webUiState.audioAlignmentTargetSegmentId || "")) {
          card.classList.add("target");
        }
        const alignedAudioUrl = buildProjectFileUrl(item.aligned_audio_path);
        const ttsAudioUrl = buildProjectFileUrl(item.tts_audio_path);
        const statusBadgeClass = statusInfo.code === "confirmed"
          ? "badge ok"
          : item.has_audio_preview
            ? "badge"
            : "badge alert";
        const alignedMarkup = alignedAudioUrl
          ? `
              <div class="audio-preview-card">
                <strong>对齐后音频</strong>
                <audio controls preload="none" style="width: 100%; margin-top: 8px;" src="${escapeHtml(alignedAudioUrl)}"></audio>
                <div class="mono" style="margin-top: 6px;">${escapeHtml(item.aligned_audio_path || "-")}</div>
              </div>
            `
          : `
              <div class="audio-preview-card">
                <strong>对齐后音频</strong>
                <div class="hint" style="margin-top: 8px;">当前还没有对齐后音频。</div>
              </div>
            `;
        const ttsMarkup = ttsAudioUrl
          ? `
              <div class="audio-preview-card">
                <strong>TTS 原始音频</strong>
                <audio controls preload="none" style="width: 100%; margin-top: 8px;" src="${escapeHtml(ttsAudioUrl)}"></audio>
                <div class="mono" style="margin-top: 6px;">${escapeHtml(item.tts_audio_path || "-")}</div>
              </div>
            `
          : `
              <div class="audio-preview-card">
                <strong>TTS 原始音频</strong>
                <div class="hint" style="margin-top: 8px;">当前还没有 TTS 原始音频。</div>
              </div>
            `;
        card.innerHTML = `
          <div class="review-card-header">
            <span class="badge ok" data-audio-alignment-role="playing-indicator" style="display: none;">播放中</span>
            <strong>段落 #${escapeHtml(item.segment_id || "-")}</strong>
            <span class="${statusBadgeClass}">试听状态：${escapeHtml(statusInfo.label)}</span>
            <span class="badge">${escapeHtml(item.display_name || item.speaker_id || "Unknown speaker")}</span>
            <span class="badge">对齐：${escapeHtml(item.alignment_method || "-")}</span>
            <span class="${item.needs_review ? "badge alert" : "badge ok"}">${item.needs_review ? "需复核" : "已通过"}</span>
            <span class="badge">时长偏差：${formatDuration(durationDelta)}</span>
          </div>
          <div class="review-copy">
            <div class="hint">
              时间范围：${formatTimestamp(Number(item.start_ms || 0))} - ${formatTimestamp(Number(item.end_ms || 0))}
              ｜ 目标时长：${formatDuration(Number(item.target_duration_ms || 0))}
              ｜ 实际时长：${formatDuration(Number(item.actual_duration_ms || 0))}
              ${updatedAtLabel ? `｜ 最近确认：${escapeHtml(updatedAtLabel)}` : ""}
            </div>
            <div><strong>原文：</strong>${escapeHtml(item.source_text || "（空）")}</div>
            <div><strong>译文：</strong>${escapeHtml(item.cn_text || "（空）")}</div>
            <div><strong>TTS 文本：</strong>${escapeHtml(item.tts_cn_text || "（空）")}</div>
            <div class="subgrid" style="margin-top: 10px;">
              ${alignedMarkup}
              ${ttsMarkup}
            </div>
            <div class="toolbar">
              <button type="button" class="secondary" data-audio-alignment-action="toggle-confirm" data-segment-id="${escapeHtml(segmentIdKey)}">
                ${statusInfo.code === "confirmed" ? "取消试听确认" : "标记已试听确认"}
              </button>
              <button type="button" class="secondary" data-audio-alignment-action="reset" data-segment-id="${escapeHtml(segmentIdKey)}">
                清空本地状态
              </button>
            </div>
          </div>
        `;
        listNode.appendChild(card);
      });
    }

    function renderVoiceLibrary(results, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      const voiceLibrary = results.voice_library || {};
      const speakerSelect = document.getElementById("voice-library-filter-speaker");
      const keywordInput = document.getElementById("voice-library-filter-keyword");
      const projectDefaultSelect = document.getElementById("voice-library-project-default-select");
      const draft = preserveDraft ? readVoiceLibraryDraft() : {
        speaker: "all",
        keyword: "",
      };

      speakerSelect.innerHTML = "";
      [{ value: "all", label: "全部 speaker" }, ...((voiceLibrary.speakers || []).map((speaker) => ({
        value: speaker.speaker_id,
        label: speaker.speaker_name || speaker.speaker_id,
      })))].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });

      projectDefaultSelect.innerHTML = "";
      const builtinOptions = Array.isArray(voiceLibrary.builtin_voice_options)
        ? voiceLibrary.builtin_voice_options
        : [];
      if (!builtinOptions.length) {
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "当前没有可用 builtin 音色";
        projectDefaultSelect.appendChild(emptyOption);
      } else {
        builtinOptions.forEach((option) => {
          const node = document.createElement("option");
          node.value = String(option.voice_id || "");
          node.textContent = `${option.speaker_name || option.speaker_id || "Unknown"} ｜ ${option.voice_id || "-"} ｜ ${option.label || "未命名"}`;
          node.selected = option.voice_id === voiceLibrary.project_default_builtin_voice?.voice_id;
          projectDefaultSelect.appendChild(node);
        });
      }

      speakerSelect.value = Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)
        ? draft.speaker
        : "all";
      keywordInput.value = draft.keyword || "";

      webUiState.voiceLibraryDraft = {
        speaker: speakerSelect.value,
        keyword: keywordInput.value,
      };
      rerenderVoiceLibrary();
    }

    function renderAudioAlignment(results, options = {}) {
      const preserveDraft = options.preserveDraft !== false;
      const audioAlignment = results.audio_alignment || {};
      const projectKey = getAudioAlignmentStorageKey(results);
      if (
        preserveDraft
        && webUiState.audioAlignmentProjectKey === projectKey
        && isAudioAlignmentPlaybackActive()
      ) {
        return;
      }
      if (webUiState.audioAlignmentProjectKey !== projectKey) {
        webUiState.audioAlignmentProjectKey = projectKey;
        webUiState.audioAlignmentTargetSegmentId = null;
        if (!preserveDraft) {
          webUiState.audioAlignmentDraft.page = 1;
        }
      }

      const speakerSelect = document.getElementById("audio-alignment-filter-speaker");
      const statusSelect = document.getElementById("audio-alignment-filter-status");
      const pageSizeSelect = document.getElementById("audio-alignment-page-size");
      const keywordInput = document.getElementById("audio-alignment-filter-keyword");
      const segmentIdInput = document.getElementById("audio-alignment-filter-segment-id");
      const draft = preserveDraft ? readAudioAlignmentDraft() : {
        segmentId: "",
        speaker: "all",
        status: "all",
        keyword: "",
        pageSize: Number(audioAlignment.default_page_size || 20),
        page: 1,
      };

      speakerSelect.innerHTML = "";
      [{ value: "all", label: "全部说话人" }, ...(audioAlignment.speaker_options || [])].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        speakerSelect.appendChild(node);
      });

      statusSelect.innerHTML = "";
      [
        { value: "all", label: "全部状态" },
        { value: "pending", label: "待试听" },
        { value: "confirmed", label: "已试听确认" },
        { value: "needs_review", label: "仅看需复核" },
        { value: "missing_audio", label: "仅看音频缺失" },
      ].forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        statusSelect.appendChild(node);
      });

      pageSizeSelect.innerHTML = "";
      (audioAlignment.page_size_options || [20]).forEach((pageSize) => {
        const node = document.createElement("option");
        node.value = String(pageSize);
        node.textContent = `${pageSize} 条 / 页`;
        pageSizeSelect.appendChild(node);
      });

      speakerSelect.value = Array.from(speakerSelect.options).some((option) => option.value === draft.speaker)
        ? draft.speaker
        : "all";
      statusSelect.value = Array.from(statusSelect.options).some((option) => option.value === draft.status)
        ? draft.status
        : "all";
      pageSizeSelect.value = Array.from(pageSizeSelect.options).some((option) => option.value === String(draft.pageSize))
        ? String(draft.pageSize)
        : String(audioAlignment.default_page_size || 20);
      keywordInput.value = draft.keyword || "";
      segmentIdInput.value = draft.segmentId || "";

      webUiState.audioAlignmentDraft = {
        segmentId: segmentIdInput.value,
        speaker: speakerSelect.value,
        status: statusSelect.value,
        keyword: keywordInput.value,
        pageSize: Number(pageSizeSelect.value || 20),
        page: preserveDraft ? webUiState.audioAlignmentDraft.page || 1 : 1,
      };
      rerenderAudioAlignment();
    }

    function initializeResultsControls() {
      document.getElementById("needs-review-filter-segment-id").addEventListener("input", () => {
        webUiState.resultsDraft.page = 1;
        webUiState.targetSegmentId = null;
        rerenderResults();
      });
      document.getElementById("needs-review-filter-speaker").addEventListener("change", () => {
        webUiState.resultsDraft.page = 1;
        webUiState.targetSegmentId = null;
        rerenderResults();
      });
      document.getElementById("needs-review-page-size").addEventListener("change", () => {
        webUiState.resultsDraft.page = 1;
        rerenderResults();
      });
      document.getElementById("needs-review-filter-keyword").addEventListener("input", () => {
        webUiState.resultsDraft.page = 1;
        webUiState.targetSegmentId = null;
        rerenderResults();
      });
      document.getElementById("needs-review-jump").addEventListener("click", () => {
        const segmentInput = document.getElementById("needs-review-filter-segment-id");
        const segmentId = normalizeSegmentId(segmentInput?.value || "");
        if (!segmentId) {
          alert("请先输入段号。");
          return;
        }
        const results = webUiState.latestResults || {};
        const allItems = Array.isArray(results.needs_review?.items) ? results.needs_review.items : [];
        const speaker = document.getElementById("needs-review-filter-speaker")?.value || "all";
        const keyword = String(document.getElementById("needs-review-filter-keyword")?.value || "").trim().toLowerCase();
        let candidateItems = allItems;
        if (speaker !== "all") {
          candidateItems = candidateItems.filter((item) => String(item.speaker_id || "") === speaker);
        }
        if (keyword) {
          candidateItems = candidateItems.filter((item) => {
            const haystack = [
              item.segment_id,
              item.display_name,
              item.source_text,
              item.cn_text,
              item.tts_cn_text,
              item.alignment_method,
            ].join(" ").toLowerCase();
            return haystack.includes(keyword);
          });
        }
        const itemIndex = candidateItems.findIndex((item) => String(item.segment_id || "") === segmentId);
        if (itemIndex < 0) {
          alert(`没有找到段号 ${segmentId}。`);
          return;
        }
        const pageSize = Number(document.getElementById("needs-review-page-size")?.value || 20);
        if (segmentInput) {
          segmentInput.value = "";
        }
        webUiState.targetSegmentId = segmentId;
        webUiState.resultsDraft.segmentId = "";
        webUiState.resultsDraft.page = Math.floor(itemIndex / pageSize) + 1;
        rerenderResults();
      });
      document.getElementById("needs-review-filter-segment-id").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          document.getElementById("needs-review-jump").click();
        }
      });
      document.getElementById("needs-review-prev").addEventListener("click", () => {
        webUiState.resultsDraft.page = Math.max(1, (webUiState.resultsDraft.page || 1) - 1);
        rerenderResults();
      });
      document.getElementById("needs-review-next").addEventListener("click", () => {
        webUiState.resultsDraft.page = (webUiState.resultsDraft.page || 1) + 1;
        rerenderResults();
      });
      document.getElementById("needs-review-list").addEventListener("click", (event) => {
        const actionButton = event.target.closest("[data-results-action]");
        if (!actionButton) {
          return;
        }
        const segmentId = normalizeSegmentId(actionButton.dataset.segmentId || "");
        if (!segmentId) {
          return;
        }
        const action = String(actionButton.dataset.resultsAction || "").trim();
        if (!action) {
          return;
        }
        if (action === "open-review") {
          openNeedsReviewSegment("review", segmentId);
        } else if (action === "open-translation") {
          openNeedsReviewSegment("translation", segmentId);
        } else if (action === "open-audio") {
          openNeedsReviewSegment("audio-alignment", segmentId);
        }
      });
    }

    function initializeReviewControls() {
      document.getElementById("review-filter-segment-id").addEventListener("input", () => {
        webUiState.reviewDraft.page = 1;
        webUiState.reviewTargetSegmentId = null;
        rerenderTranscriptReview();
      });
      document.getElementById("review-filter-speaker").addEventListener("change", () => {
        webUiState.reviewDraft.page = 1;
        webUiState.reviewTargetSegmentId = null;
        rerenderTranscriptReview();
      });
      document.getElementById("review-filter-status").addEventListener("change", () => {
        webUiState.reviewDraft.page = 1;
        webUiState.reviewTargetSegmentId = null;
        rerenderTranscriptReview();
      });
      document.getElementById("review-page-size").addEventListener("change", () => {
        webUiState.reviewDraft.page = 1;
        rerenderTranscriptReview();
      });
      document.getElementById("review-filter-keyword").addEventListener("input", () => {
        webUiState.reviewDraft.page = 1;
        webUiState.reviewTargetSegmentId = null;
        rerenderTranscriptReview();
      });
      document.getElementById("review-jump").addEventListener("click", () => {
        const segmentInput = document.getElementById("review-filter-segment-id");
        const segmentId = normalizeSegmentId(segmentInput?.value || "");
        if (!segmentId) {
          alert("请先输入段号。");
          return;
        }
        const results = webUiState.latestResults || {};
        const allItems = Array.isArray(results.transcript_review?.items) ? results.transcript_review.items : [];
        const candidateItems = getFilteredTranscriptReviewItems(
          results,
          {
            segmentId: "",
            speaker: document.getElementById("review-filter-speaker")?.value || "all",
            status: document.getElementById("review-filter-status")?.value || "all",
            keyword: document.getElementById("review-filter-keyword")?.value || "",
          },
          loadReviewConfirmations(results)
        );
        const itemIndex = candidateItems.findIndex((item) => String(item.segment_id || "") === segmentId);
        if (itemIndex < 0) {
          alert(`没有找到段号 ${segmentId}。`);
          return;
        }
        const pageSize = Number(document.getElementById("review-page-size")?.value || 20);
        if (segmentInput) {
          segmentInput.value = "";
        }
        webUiState.reviewTargetSegmentId = segmentId;
        webUiState.reviewDraft.segmentId = "";
        webUiState.reviewDraft.page = Math.floor(itemIndex / pageSize) + 1;
        rerenderTranscriptReview();
      });
      document.getElementById("review-filter-segment-id").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          document.getElementById("review-jump").click();
        }
      });
      document.getElementById("review-prev").addEventListener("click", () => {
        webUiState.reviewDraft.page = Math.max(1, (webUiState.reviewDraft.page || 1) - 1);
        rerenderTranscriptReview();
      });
      document.getElementById("review-next").addEventListener("click", () => {
        webUiState.reviewDraft.page = (webUiState.reviewDraft.page || 1) + 1;
        rerenderTranscriptReview();
      });
      document.getElementById("review-speaker-editor").addEventListener("input", () => {
        webUiState.speakerReviewDraft = readSpeakerReviewDraft();
        markSaveIndicatorDirty("review", webUiState.latestResults || {});
      });
      document.getElementById("review-speaker-editor").addEventListener("change", () => {
        webUiState.speakerReviewDraft = readSpeakerReviewDraft();
        markSaveIndicatorDirty("review", webUiState.latestResults || {});
        rerenderTranscriptReview();
      });
      document.getElementById("review-list").addEventListener("change", (event) => {
        const speakerSelect = event.target.closest("[data-review-segment-speaker]");
        if (!speakerSelect) {
          return;
        }
        webUiState.speakerReviewDraft = readSpeakerReviewDraft();
        markSaveIndicatorDirty("review", webUiState.latestResults || {});
        rerenderTranscriptReview();
        persistSpeakerReviewDraftInBackground();
      });
      document.getElementById("review-list").addEventListener("click", (event) => {
        const actionButton = event.target.closest("[data-review-action]");
        if (!actionButton) {
          return;
        }
        const segmentId = normalizeSegmentId(actionButton.dataset.segmentId || "");
        if (!segmentId) {
          return;
        }
        const results = webUiState.latestResults || {};
        const confirmations = loadReviewConfirmations(results);
        const currentStatus = getTranscriptReviewStatus(confirmations[segmentId] || {});
        if (actionButton.dataset.reviewAction === "toggle-speaker") {
          updateTranscriptConfirmation(segmentId, {
            speakerConfirmed: !currentStatus.speakerConfirmed,
          });
        } else if (actionButton.dataset.reviewAction === "toggle-transcript") {
          updateTranscriptConfirmation(segmentId, {
            transcriptConfirmed: !currentStatus.transcriptConfirmed,
          });
        } else if (actionButton.dataset.reviewAction === "reset") {
          clearTranscriptConfirmation(segmentId);
        }
        rerenderTranscriptReview();
        persistSpeakerReviewDraftInBackground();
      });
      document.getElementById("review-bulk-confirm-speaker").addEventListener("click", () => {
        applyBulkTranscriptReviewAction("confirm-speaker");
      });
      document.getElementById("review-bulk-confirm-transcript").addEventListener("click", () => {
        applyBulkTranscriptReviewAction("confirm-transcript");
      });
      document.getElementById("review-bulk-reset").addEventListener("click", () => {
        applyBulkTranscriptReviewAction("reset");
      });
      document.getElementById("review-nav-prev").addEventListener("click", () => {
        navigateTranscriptReview("prev");
      });
      document.getElementById("review-nav-next").addEventListener("click", () => {
        navigateTranscriptReview("next");
      });
      document.getElementById("review-nav-next-pending").addEventListener("click", () => {
        navigateTranscriptReview("next-pending");
      });
      document.getElementById("review-nav-next-needs-review").addEventListener("click", () => {
        navigateTranscriptReview("next-needs-review");
      });
    }

    function initializeTranslationControls() {
      document.getElementById("translation-filter-segment-id").addEventListener("input", () => {
        webUiState.translationDraft.page = 1;
        webUiState.translationTargetSegmentId = null;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-filter-speaker").addEventListener("change", () => {
        webUiState.translationDraft.page = 1;
        webUiState.translationTargetSegmentId = null;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-filter-status").addEventListener("change", () => {
        webUiState.translationDraft.page = 1;
        webUiState.translationTargetSegmentId = null;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-page-size").addEventListener("change", () => {
        webUiState.translationDraft.page = 1;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-filter-keyword").addEventListener("input", () => {
        webUiState.translationDraft.page = 1;
        webUiState.translationTargetSegmentId = null;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-jump").addEventListener("click", () => {
        const segmentInput = document.getElementById("translation-filter-segment-id");
        const segmentId = normalizeSegmentId(segmentInput?.value || "");
        if (!segmentId) {
          alert("请先输入段号。");
          return;
        }
        const results = webUiState.latestResults || {};
        const allItems = Array.isArray(results.translation_review?.items) ? results.translation_review.items : [];
        const candidateItems = getFilteredTranslationReviewItems(
          results,
          {
            segmentId: "",
            speaker: document.getElementById("translation-filter-speaker")?.value || "all",
            status: document.getElementById("translation-filter-status")?.value || "all",
            keyword: document.getElementById("translation-filter-keyword")?.value || "",
          },
          loadTranslationReviewState(results)
        );
        const itemIndex = candidateItems.findIndex((item) => String(item.segment_id || "") === segmentId);
        if (itemIndex < 0) {
          alert(`没有找到段号 ${segmentId}。`);
          return;
        }
        const pageSize = Number(document.getElementById("translation-page-size")?.value || 20);
        if (segmentInput) {
          segmentInput.value = "";
        }
        webUiState.translationTargetSegmentId = segmentId;
        webUiState.translationDraft.segmentId = "";
        webUiState.translationDraft.page = Math.floor(itemIndex / pageSize) + 1;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-filter-segment-id").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          document.getElementById("translation-jump").click();
        }
      });
      document.getElementById("translation-prev").addEventListener("click", () => {
        webUiState.translationDraft.page = Math.max(1, (webUiState.translationDraft.page || 1) - 1);
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-next").addEventListener("click", () => {
        webUiState.translationDraft.page = (webUiState.translationDraft.page || 1) + 1;
        rerenderTranslationReviewEnhanced();
      });
      document.getElementById("translation-list").addEventListener("input", (event) => {
        const textInput = event.target.closest(
          "[data-translation-cn-text], [data-translation-tts-text]"
        );
        if (!textInput) {
          return;
        }
        const segmentId = normalizeSegmentId(textInput.dataset.segmentId || "");
        if (!segmentId) {
          return;
        }
        if (textInput.hasAttribute("data-translation-cn-text")) {
          updateTranslationReviewState(segmentId, {
            cnText: textInput.value,
          });
          return;
        }
        updateTranslationReviewState(segmentId, {
          ttsCnText: textInput.value,
        });
      });
      document.getElementById("translation-list").addEventListener("click", (event) => {
        const actionButton = event.target.closest("[data-translation-action]");
        if (!actionButton) {
          return;
        }
        const segmentId = normalizeSegmentId(actionButton.dataset.segmentId || "");
        if (!segmentId) {
          return;
        }
        const results = webUiState.latestResults || {};
        const state = loadTranslationReviewState(results);
        const currentStatus = getTranslationReviewStatus(state[segmentId] || {});
        if (actionButton.dataset.translationAction === "toggle-confirm") {
          updateTranslationReviewState(segmentId, {
            translationConfirmed: !currentStatus.translationConfirmed,
          });
        } else if (actionButton.dataset.translationAction === "toggle-rewrite") {
          updateTranslationReviewState(segmentId, {
            rewriteRequested: !currentStatus.rewriteRequested,
          });
        } else if (actionButton.dataset.translationAction === "reset") {
          clearTranslationReviewState(segmentId);
        }
        rerenderTranslationReviewEnhanced();
        persistTranslationReviewDraftInBackground();
      });
      document.getElementById("translation-bulk-confirm").addEventListener("click", () => {
        applyBulkTranslationReviewAction("confirm");
      });
      document.getElementById("translation-bulk-mark-rewrite").addEventListener("click", () => {
        applyBulkTranslationReviewAction("mark-rewrite");
      });
      document.getElementById("translation-bulk-reset").addEventListener("click", () => {
        applyBulkTranslationReviewAction("reset");
      });
      document.getElementById("translation-nav-prev").addEventListener("click", () => {
        navigateTranslationReview("prev");
      });
      document.getElementById("translation-nav-next").addEventListener("click", () => {
        navigateTranslationReview("next");
      });
      document.getElementById("translation-nav-next-pending").addEventListener("click", () => {
        navigateTranslationReview("next-pending");
      });
      document.getElementById("translation-nav-next-needs-review").addEventListener("click", () => {
        navigateTranslationReview("next-needs-review");
      });
    }

    function initializeVoiceLibraryControls() {
      document.getElementById("voice-library-filter-speaker").addEventListener("change", () => {
        rerenderVoiceLibrary();
      });
      document.getElementById("voice-library-filter-keyword").addEventListener("input", () => {
        rerenderVoiceLibrary();
      });
      document.getElementById("voice-library-binding-list").addEventListener("click", async (event) => {
        const actionButton = event.target.closest("[data-voice-library-action='set-speaker-default']");
        if (!actionButton) {
          return;
        }
        const speakerId = String(actionButton.dataset.speakerId || "").trim();
        const card = actionButton.closest("[data-speaker-id]");
        const select = card?.querySelector("[data-voice-library-role='speaker-default-select']");
        const voiceId = String(select?.value || "").trim();
        if (!speakerId || !voiceId) {
          alert("请先选择一个可用音色。");
          return;
        }
        try {
          document.getElementById("voice-library-status").textContent = `正在更新 ${speakerId} 的默认音色...`;
          const snapshot = await fetchJson("/api/voice-library/set-default", {
            method: "POST",
            body: JSON.stringify({
              speaker_id: speakerId,
              voice_id: voiceId,
            }),
          });
          renderSnapshot(snapshot, { preserveDraft: true });
          document.getElementById("voice-library-status").textContent = `已更新 ${speakerId} 的默认音色。`;
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
      document.getElementById("voice-library-set-project-default").addEventListener("click", async () => {
        const select = document.getElementById("voice-library-project-default-select");
        const voiceId = String(select?.value || "").trim();
        if (!voiceId) {
          alert("请先选择一个 builtin 音色。");
          return;
        }
        try {
          document.getElementById("voice-library-status").textContent = "正在更新项目默认 builtin...";
          const snapshot = await fetchJson("/api/voice-library/set-project-default-builtin", {
            method: "POST",
            body: JSON.stringify({ voice_id: voiceId }),
          });
          renderSnapshot(snapshot, { preserveDraft: true });
          document.getElementById("voice-library-status").textContent = "已更新项目默认 builtin。";
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
    }

    function rerenderVoiceLibrary() {
      const results = webUiState.latestResults || {};
      const voiceLibrary = results.voice_library || {};
      const registrySpeakers = Array.isArray(voiceLibrary.speakers) ? voiceLibrary.speakers : [];
      const currentProjectSpeakers = Array.isArray(voiceLibrary.current_project_speakers)
        ? voiceLibrary.current_project_speakers
        : [];
      const activeReview = voiceLibrary.active_review || null;
      const reviewSpeakers = Array.isArray(activeReview?.speakers) ? activeReview.speakers : [];
      const reviewSpeakerMap = new Map(
        reviewSpeakers.map((item) => [String(item.speaker_id || ""), item])
      );
      const draft = readVoiceLibraryDraft();
      const speaker = draft.speaker || "all";
      const keyword = String(draft.keyword || "").trim().toLowerCase();

      let filteredRegistrySpeakers = registrySpeakers;
      if (speaker !== "all") {
        filteredRegistrySpeakers = filteredRegistrySpeakers.filter(
          (item) => String(item.speaker_id || "") === speaker
        );
      }
      if (keyword) {
        filteredRegistrySpeakers = filteredRegistrySpeakers.filter((item) => {
          const voiceHaystack = Array.isArray(item.voices)
            ? item.voices.map((voice) => [
              voice.voice_id,
              voice.label,
              voice.notes,
              voice.verification_status,
            ].join(" ")).join(" ")
            : "";
          const haystack = [
            item.speaker_id,
            item.speaker_name,
            item.default_voice_id,
            voiceHaystack,
          ].join(" ").toLowerCase();
          return haystack.includes(keyword);
        });
      }

      const filteredCurrentSpeakers = currentProjectSpeakers.filter((item) => {
        if (speaker !== "all" && String(item.speaker_id || "") !== speaker) {
          return false;
        }
        if (!keyword) {
          return true;
        }
        const pendingReview = reviewSpeakerMap.get(String(item.speaker_id || ""));
        const haystack = [
          item.speaker_id,
          item.display_name,
          item.speaker_name,
          item.default_voice_id,
          item.resolved_voice_id,
          item.resolved_label,
          pendingReview?.sample_path,
        ].join(" ").toLowerCase();
        return haystack.includes(keyword);
      });

      document.getElementById("voice-library-speaker-count").textContent = String(voiceLibrary.speaker_count || 0);
      document.getElementById("voice-library-voice-count").textContent = String(voiceLibrary.voice_count || 0);
      document.getElementById("voice-library-current-project-speaker-count").textContent = String(currentProjectSpeakers.length);
      document.getElementById("voice-library-builtin-count").textContent = String(voiceLibrary.builtin_voice_count || 0);
      document.getElementById("voice-library-path").textContent = voiceLibrary.path || "-";
      const projectDefault = voiceLibrary.project_default_builtin_voice;
      document.getElementById("voice-library-project-default").textContent = projectDefault
        ? `${projectDefault.voice_id} ｜ ${projectDefault.label || "未命名"}`
        : "未设置";
      document.getElementById("voice-library-summary").textContent = voiceLibrary.load_error
        ? `注册表加载失败：${voiceLibrary.load_error}`
        : `当前展示 ${filteredRegistrySpeakers.length} 个 speaker 条目，当前项目识别到 ${currentProjectSpeakers.length} 个 speaker。`;

      const reviewWrap = document.getElementById("voice-review-wrap");
      const reviewMessageNode = document.getElementById("voice-review-message");
      const reviewSpeakerList = document.getElementById("voice-review-speaker-list");
      reviewSpeakerList.innerHTML = "";
      if (activeReview && activeReview.stage === "voice_review" && activeReview.status === "pending") {
        reviewWrap.style.display = "block";
        reviewMessageNode.textContent = activeReview.message || "当前样本不足，需要先确认音色。";
        reviewSpeakers.forEach((item) => {
          const card = document.createElement("article");
          card.className = "review-card";
          card.innerHTML = `
            <div class="review-card-header">
              <strong>${escapeHtml(item.speaker_name || item.speaker_label || item.speaker_id || "Unknown speaker")}</strong>
              <span class="badge">Speaker ID：${escapeHtml(item.speaker_id || "-")}</span>
              <span class="${item.resolved_status === "resolved" ? "badge ok" : "badge alert"}">
                当前解析：${escapeHtml(item.resolved_voice_id || "未命中")}
              </span>
            </div>
            <div class="review-copy">
              <div class="hint">样本时长：${escapeHtml(item.sample_duration_s || 0)} 秒 ｜ 静音占比：${escapeHtml(item.silence_ratio || 0)}</div>
              <div class="hint">样本路径：${escapeHtml(item.sample_path || "-")}</div>
              <div class="hint">可以从现有音色里选择，或手动输入 Voice ID。</div>
            </div>
          `;
          reviewSpeakerList.appendChild(card);
        });
      } else {
        reviewWrap.style.display = "none";
        reviewMessageNode.textContent = "当前没有待处理的音色确认。";
      }

      const bindingList = document.getElementById("voice-library-binding-list");
      const bindingEmpty = document.getElementById("voice-library-binding-empty");
      bindingList.innerHTML = "";
      if (!filteredCurrentSpeakers.length) {
        bindingEmpty.style.display = "block";
      } else {
        bindingEmpty.style.display = "none";
        filteredCurrentSpeakers.forEach((item) => {
          const pendingReview = reviewSpeakerMap.get(String(item.speaker_id || ""));
          const card = document.createElement("article");
          card.className = "review-card";
          card.dataset.speakerId = String(item.speaker_id || "");
          card.dataset.speakerName = String(item.display_name || item.speaker_name || item.speaker_id || "");
          const voices = Array.isArray(item.available_voices) ? item.available_voices : [];
          const options = voices.map((voice) => `
            <option value="${escapeHtml(voice.voice_id || "")}" ${voice.voice_id === item.default_voice_id ? "selected" : ""}>
              ${escapeHtml(voice.voice_id || "-")} ｜ ${escapeHtml(voice.voice_type || "-")} ｜ ${escapeHtml(voice.label || "未命名")} ｜ ${escapeHtml(formatVerificationStatus(voice.verification_status))}
            </option>
          `).join("");
          const emptyOption = '<option value="">当前没有可选音色</option>';
          card.innerHTML = `
            <div class="review-card-header">
              <strong>${escapeHtml(item.display_name || item.speaker_id || "Unknown speaker")}</strong>
              <span class="badge">Speaker ID：${escapeHtml(item.speaker_id || "-")}</span>
              <span class="badge">解析来源：${escapeHtml(formatVoiceResolutionSource(item.resolved_source))}</span>
              <span class="${item.resolved_status === "resolved" ? "badge ok" : "badge alert"}">
                当前解析：${escapeHtml(item.resolved_voice_id || "未命中")}
              </span>
            </div>
            <div class="review-copy">
              <div class="hint">注册表默认：${escapeHtml(item.default_voice_id || "未设置")} ｜ 类型：${escapeHtml(item.default_voice_type || "暂无")} ｜ 当前解析标签：${escapeHtml(item.resolved_label || "暂无")}</div>
              ${pendingReview ? `<div class="hint">待确认样本：${escapeHtml(pendingReview.sample_duration_s || 0)} 秒 ｜ ${escapeHtml(pendingReview.sample_path || "-")}</div>` : ""}
              <div class="toolbar" style="margin-top: 0;">
                <select data-voice-library-role="speaker-default-select">
                  ${voices.length ? options : emptyOption}
                </select>
                <button type="button" class="secondary" data-voice-library-action="set-speaker-default" data-speaker-id="${escapeHtml(item.speaker_id || "")}" ${voices.length ? "" : "disabled"}>设为 speaker 默认</button>
              </div>
              ${pendingReview ? `
                <div class="toolbar" style="margin-top: 8px;">
                  <input
                    type="text"
                    data-voice-library-role="manual-voice-id"
                    placeholder="手动输入 Voice ID"
                  />
                  <button type="button" class="secondary" data-voice-library-action="use-manual-voice" data-speaker-id="${escapeHtml(item.speaker_id || "")}">使用这个 Voice ID</button>
                </div>
              ` : ""}
            </div>
          `;
          bindingList.appendChild(card);
        });
      }

      const registryList = document.getElementById("voice-library-registry-list");
      const registryEmpty = document.getElementById("voice-library-registry-empty");
      registryList.innerHTML = "";
      if (!filteredRegistrySpeakers.length) {
        registryEmpty.style.display = "block";
      } else {
        registryEmpty.style.display = "none";
        filteredRegistrySpeakers.forEach((speakerItem) => {
          const card = document.createElement("article");
          card.className = "review-card";
          const voices = Array.isArray(speakerItem.voices) ? speakerItem.voices : [];
          const voiceListMarkup = voices.length
            ? voices.map((voice) => `
                <div class="hint">
                  ${escapeHtml(voice.voice_id || "-")} ｜ ${escapeHtml(voice.voice_type || "-")} ｜ ${escapeHtml(voice.label || "未命名")} ｜ ${escapeHtml(formatVerificationStatus(voice.verification_status))}
                </div>
              `).join("")
            : '<div class="hint">当前没有音色记录。</div>';
          card.innerHTML = `
            <div class="review-card-header">
              <strong>${escapeHtml(speakerItem.speaker_name || speakerItem.speaker_id || "Unknown speaker")}</strong>
              <span class="badge">Speaker ID：${escapeHtml(speakerItem.speaker_id || "-")}</span>
              <span class="badge">默认：${escapeHtml(speakerItem.default_voice_id || "未设置")}</span>
              <span class="badge">解析来源：${escapeHtml(formatVoiceResolutionSource(speakerItem.resolution_source))}</span>
            </div>
            <div class="review-copy">
              ${voiceListMarkup}
            </div>
          `;
          registryList.appendChild(card);
        });
      }
    }

    function initializeVoiceLibraryControls() {
      document.getElementById("voice-library-filter-speaker").addEventListener("change", () => {
        rerenderVoiceLibrary();
      });
      document.getElementById("voice-library-filter-keyword").addEventListener("input", () => {
        rerenderVoiceLibrary();
      });
      document.getElementById("voice-library-binding-list").addEventListener("click", async (event) => {
        const actionButton = event.target.closest("[data-voice-library-action]");
        if (!actionButton) {
          return;
        }
        const action = String(actionButton.dataset.voiceLibraryAction || "").trim();
        const speakerId = String(actionButton.dataset.speakerId || "").trim();
        const card = actionButton.closest("[data-speaker-id]");
        if (!speakerId || !card) {
          return;
        }

        try {
          if (action === "set-speaker-default") {
            const select = card.querySelector("[data-voice-library-role='speaker-default-select']");
            const voiceId = String(select?.value || "").trim();
            if (!voiceId) {
              alert("请先选择一个可用音色。");
              return;
            }
            document.getElementById("voice-library-status").textContent = `正在更新 ${speakerId} 的默认音色...`;
            const snapshot = await fetchJson("/api/voice-library/set-default", {
              method: "POST",
              body: JSON.stringify({
                speaker_id: speakerId,
                voice_id: voiceId,
              }),
            });
            renderSnapshot(snapshot, { preserveDraft: true });
            document.getElementById("voice-library-status").textContent = `已更新 ${speakerId} 的默认音色。`;
            return;
          }

          if (action === "use-manual-voice") {
            const input = card.querySelector("[data-voice-library-role='manual-voice-id']");
            const voiceId = String(input?.value || "").trim();
            if (!voiceId) {
              alert("请先输入 Voice ID。");
              return;
            }
            document.getElementById("voice-library-status").textContent = `正在绑定 ${speakerId} 的手动 Voice ID...`;
            const snapshot = await fetchJson("/api/voice-library/register-manual", {
              method: "POST",
              body: JSON.stringify({
                speaker_id: speakerId,
                speaker_name: String(card.dataset.speakerName || speakerId),
                voice_id: voiceId,
              }),
            });
            renderSnapshot(snapshot, { preserveDraft: true });
            document.getElementById("voice-library-status").textContent = `已绑定 ${speakerId} 的手动 Voice ID。`;
          }
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
      document.getElementById("voice-library-set-project-default").addEventListener("click", async () => {
        const select = document.getElementById("voice-library-project-default-select");
        const voiceId = String(select?.value || "").trim();
        if (!voiceId) {
          alert("请先选择一个 builtin 音色。");
          return;
        }
        try {
          document.getElementById("voice-library-status").textContent = "正在更新项目默认 builtin...";
          const snapshot = await fetchJson("/api/voice-library/set-project-default-builtin", {
            method: "POST",
            body: JSON.stringify({ voice_id: voiceId }),
          });
          renderSnapshot(snapshot, { preserveDraft: true });
          document.getElementById("voice-library-status").textContent = "已更新项目默认 builtin。";
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
      document.getElementById("voice-review-approve").addEventListener("click", async () => {
        const projectDir = String((webUiState.latestResults || {}).project_dir || "").trim();
        if (!projectDir) {
          alert("当前没有可继续的项目目录。");
          return;
        }
        try {
          document.getElementById("voice-library-status").textContent = "正在确认音色并继续处理...";
          const snapshot = await fetchJson("/api/review/voice/approve", {
            method: "POST",
            body: JSON.stringify({ project_dir: projectDir }),
          });
          renderSnapshot(snapshot, { preserveDraft: false });
          document.getElementById("voice-library-status").textContent = "已确认音色，继续处理中。";
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
      document.getElementById("voice-review-cancel").addEventListener("click", async () => {
        const projectDir = String((webUiState.latestResults || {}).project_dir || "").trim();
        if (!projectDir) {
          alert("当前没有可取消的项目目录。");
          return;
        }
        if (!confirm("取消后本次等待人工确认的任务会结束，确定继续吗？")) {
          return;
        }
        try {
          document.getElementById("voice-library-status").textContent = "正在取消任务...";
          const snapshot = await fetchJson("/api/review/voice/cancel", {
            method: "POST",
            body: JSON.stringify({ project_dir: projectDir }),
          });
          renderSnapshot(snapshot, { preserveDraft: false });
          document.getElementById("voice-library-status").textContent = "任务已取消。";
        } catch (error) {
          document.getElementById("voice-library-status").textContent = error.message;
          alert(error.message);
        }
      });
    }

    function initializeAudioAlignmentControls() {
      document.getElementById("audio-alignment-filter-segment-id").addEventListener("input", () => {
        webUiState.audioAlignmentDraft.page = 1;
        webUiState.audioAlignmentTargetSegmentId = null;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-filter-speaker").addEventListener("change", () => {
        webUiState.audioAlignmentDraft.page = 1;
        webUiState.audioAlignmentTargetSegmentId = null;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-filter-status").addEventListener("change", () => {
        webUiState.audioAlignmentDraft.page = 1;
        webUiState.audioAlignmentTargetSegmentId = null;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-page-size").addEventListener("change", () => {
        webUiState.audioAlignmentDraft.page = 1;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-filter-keyword").addEventListener("input", () => {
        webUiState.audioAlignmentDraft.page = 1;
        webUiState.audioAlignmentTargetSegmentId = null;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-jump").addEventListener("click", () => {
        const segmentInput = document.getElementById("audio-alignment-filter-segment-id");
        const segmentId = normalizeSegmentId(segmentInput?.value || "");
        if (!segmentId) {
          alert("请先输入段号。");
          return;
        }
        const results = webUiState.latestResults || {};
        const allItems = Array.isArray(results.audio_alignment?.items) ? results.audio_alignment.items : [];
        const candidateItems = filterAudioAlignmentItems(
          allItems,
          {
            segmentId: "",
            speaker: document.getElementById("audio-alignment-filter-speaker")?.value || "all",
            status: document.getElementById("audio-alignment-filter-status")?.value || "all",
            keyword: document.getElementById("audio-alignment-filter-keyword")?.value || "",
          },
          loadAudioAlignmentState(results)
        );
        const itemIndex = candidateItems.findIndex((item) => String(item.segment_id || "") === segmentId);
        if (itemIndex < 0) {
          alert(`没有找到段号 ${segmentId}。`);
          return;
        }
        const pageSize = Number(document.getElementById("audio-alignment-page-size")?.value || 20);
        if (segmentInput) {
          segmentInput.value = "";
        }
        webUiState.audioAlignmentTargetSegmentId = segmentId;
        webUiState.audioAlignmentDraft.segmentId = "";
        webUiState.audioAlignmentDraft.page = Math.floor(itemIndex / pageSize) + 1;
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-filter-segment-id").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          document.getElementById("audio-alignment-jump").click();
        }
      });
      document.getElementById("audio-alignment-prev").addEventListener("click", () => {
        webUiState.audioAlignmentDraft.page = Math.max(1, (webUiState.audioAlignmentDraft.page || 1) - 1);
        rerenderAudioAlignment();
      });
      document.getElementById("audio-alignment-next").addEventListener("click", () => {
        webUiState.audioAlignmentDraft.page = (webUiState.audioAlignmentDraft.page || 1) + 1;
        rerenderAudioAlignment();
      });
      const audioAlignmentList = document.getElementById("audio-alignment-list");
      audioAlignmentList.addEventListener("click", (event) => {
        const actionButton = event.target.closest("[data-audio-alignment-action]");
        if (!actionButton) {
          return;
        }
        const segmentId = normalizeSegmentId(actionButton.dataset.segmentId || "");
        if (!segmentId) {
          return;
        }
        if (actionButton.dataset.audioAlignmentAction === "toggle-confirm") {
          const results = webUiState.latestResults || {};
          const state = loadAudioAlignmentState(results);
          const statusInfo = getAudioAlignmentStatus(state[segmentId] || {});
          updateAudioAlignmentState(segmentId, {
            listenedConfirmed: !statusInfo.listenedConfirmed,
          });
        } else if (actionButton.dataset.audioAlignmentAction === "reset") {
          clearAudioAlignmentState(segmentId);
        }
        rerenderAudioAlignment();
      });
      ["play", "pause", "ended"].forEach((eventName) => {
        audioAlignmentList.addEventListener(eventName, () => {
          window.setTimeout(() => {
            syncAudioAlignmentPlaybackIndicators();
          }, 0);
        }, true);
      });
    }
"""


def _build_web_ui_handler() -> type[BaseHTTPRequestHandler]:
    class WebUIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            if parsed_path.path == "/":
                self._write_html(HTTPStatus.OK, render_web_ui_html())
                return
            if parsed_path.path == "/api/state":
                snapshot = build_web_ui_snapshot(manager=self.server.job_manager)  # type: ignore[attr-defined]
                self._write_json(HTTPStatus.OK, snapshot)
                return
            if parsed_path.path == "/api/result-download":
                query = parse_qs(parsed_path.query)
                requested_project_dir = str((query.get("project_dir") or [""])[0]).strip()
                requested_job_id = str((query.get("job_id") or [""])[0]).strip()
                requested_key = str((query.get("key") or [""])[0]).strip()
                if not requested_key:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "key query parameter is required"},
                    )
                    return
                manager = self.server.job_manager  # type: ignore[attr-defined]
                project_root = manager.project_root.resolve(strict=False)
                resolved_project_dir_text = requested_project_dir or _resolve_project_dir_by_job_id(
                    manager=manager,
                    job_id=requested_job_id,
                )
                if not resolved_project_dir_text:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "project_dir or job_id query parameter is required",
                        },
                    )
                    return
                try:
                    download_path = _resolve_public_result_download_path(
                        project_root=project_root,
                        project_dir=Path(resolved_project_dir_text).expanduser().resolve(strict=False),
                        download_key=requested_key,
                    )
                except ValueError as exc:
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                if download_path is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "Requested download was not found."},
                    )
                    return
                content_type = mimetypes.guess_type(str(download_path))[0] or "application/octet-stream"
                self._write_binary(
                    HTTPStatus.OK,
                    download_path.read_bytes(),
                    content_type=content_type,
                    download_name=download_path.name,
                )
                return
            if parsed_path.path == "/api/project-file":
                query = parse_qs(parsed_path.query)
                requested_path = str((query.get("path") or [""])[0]).strip()
                if not requested_path:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "path query parameter is required"})
                    return
                try:
                    candidate_path = _resolve_allowed_project_file_download_path(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_path=requested_path,
                    )
                except FileNotFoundError:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "Requested file was not found."})
                    return
                except ValueError as exc:
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                content_type = mimetypes.guess_type(str(candidate_path))[0] or "application/octet-stream"
                self._write_binary(HTTPStatus.OK, candidate_path.read_bytes(), content_type=content_type)
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed_path = urlparse(self.path)
                if parsed_path.path == "/api/upload-video":
                    self._handle_video_upload()
                    return
                if parsed_path.path == "/api/run":
                    payload = self._read_json()
                    snapshot = self.server.job_manager.start_job(  # type: ignore[attr-defined]
                        youtube_url=str(payload.get("youtube_url") or ""),
                        speakers=str(payload.get("speakers") or "auto"),
                        voice_a=_normalize_optional_text(payload.get("voice_a")),
                        voice_b=_normalize_optional_text(payload.get("voice_b")),
                        translation_model_alias=str(payload.get("translation_model_alias") or ""),
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/stop":
                    snapshot = self.server.job_manager.stop_job()  # type: ignore[attr-defined]
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/voice-library/set-default":
                    payload = self._read_json()
                    speaker_id = str(payload.get("speaker_id") or "").strip()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    if not speaker_id or not voice_id:
                        raise ValueError("speaker_id 和 voice_id 不能为空。")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    VoiceRegistry(str(registry_path)).set_default_voice(speaker_id, voice_id)
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/voice-library/register-manual":
                    payload = self._read_json()
                    speaker_id = str(payload.get("speaker_id") or "").strip()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    speaker_name = _normalize_optional_text(payload.get("speaker_name")) or speaker_id
                    if not speaker_id or not voice_id:
                        raise ValueError("speaker_id 和 voice_id 不能为空。")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    VoiceRegistry(str(registry_path)).register_voice(
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        voice_id=voice_id,
                        voice_type="cloned",
                        provider="minimax",
                        tts_provider="minimax_tts",
                        platform="minimax_domestic",
                        label=f"{speaker_name} Manual Voice ID",
                        source_audio_path=_normalize_optional_text(payload.get("sample_path")),
                        notes="Registered from Web UI manual voice review.",
                        set_default=True,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/voice-library/set-project-default-builtin":
                    payload = self._read_json()
                    voice_id = str(payload.get("voice_id") or "").strip()
                    if not voice_id:
                        raise ValueError("voice_id 不能为空。")
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    registry = VoiceRegistry(str(registry_path))
                    builtin_voice = _find_builtin_voice_option(registry=registry, voice_id=voice_id)
                    if builtin_voice is None:
                        raise ValueError(f"未找到 builtin voice_id={voice_id}")
                    registry.set_project_default_builtin_voice(
                        voice_id=str(builtin_voice["voice_id"]),
                        provider=str(builtin_voice["provider"]),
                        tts_provider=_normalize_optional_text(builtin_voice.get("tts_provider")),
                        platform=_normalize_optional_text(builtin_voice.get("platform")),
                        label=str(builtin_voice["label"]),
                        created_at=_normalize_optional_text(builtin_voice.get("created_at")),
                        notes=_normalize_optional_text(builtin_voice.get("notes")),
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/speaker/save":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                    )
                    _save_speaker_review_submission(
                        project_dir=project_dir,
                        speaker_names_payload=payload.get("speaker_names"),
                        segment_speakers_payload=payload.get("segment_speakers"),
                        review_confirmations_payload=payload.get("confirmations"),
                        status=REVIEW_STATUS_PENDING,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/speaker/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=SPEAKER_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    _save_speaker_review_submission(
                        project_dir=project_dir,
                        speaker_names_payload=payload.get("speaker_names"),
                        segment_speakers_payload=payload.get("segment_speakers"),
                        review_confirmations_payload=payload.get("confirmations"),
                        status=REVIEW_STATUS_APPROVED,
                    )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=SPEAKER_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=VOICE_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
                    voice_stage = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
                    if not voice_stage or voice_stage.get("status") != REVIEW_STATUS_PENDING:
                        raise ValueError("当前没有待确认的音色阶段。")
                    review_payload = voice_stage.get("payload")
                    if not isinstance(review_payload, dict):
                        review_payload = {}
                    registry_path = _resolve_voice_registry_path(
                        project_root=self.server.job_manager.project_root,  # type: ignore[attr-defined]
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    registry = VoiceRegistry(str(registry_path))
                    resolver = VoiceResolver(registry)
                    unresolved: list[str] = []
                    resolved_speakers: list[dict[str, object]] = []
                    raw_speakers = review_payload.get("speakers", [])
                    if isinstance(raw_speakers, list):
                        for raw_speaker in raw_speakers:
                            if not isinstance(raw_speaker, dict):
                                continue
                            speaker_id = str(raw_speaker.get("speaker_id") or "").strip()
                            if not speaker_id:
                                continue
                            resolution = resolver.resolve(speaker_id)
                            if not resolution.resolved or not resolution.voice_id:
                                unresolved.append(
                                    str(raw_speaker.get("speaker_name") or raw_speaker.get("speaker_label") or speaker_id)
                                )
                                continue
                            resolved_speakers.append(
                                {
                                    "speaker_id": speaker_id,
                                    "voice_id": resolution.voice_id,
                                    "voice_type": resolution.voice_type,
                                    "label": resolution.label,
                                    "source": resolution.source,
                                }
                            )
                    if unresolved:
                        raise ValueError(
                            f"仍有 speaker 未绑定可用音色：{', '.join(unresolved)}"
                        )
                    review_state_manager.set_stage(
                        VOICE_REVIEW_STAGE,
                        status=REVIEW_STATUS_APPROVED,
                        payload={
                            **review_payload,
                            "resolved_speakers": resolved_speakers,
                        },
                    )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=VOICE_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/voice/cancel":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=VOICE_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
                    voice_stage = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
                    if not voice_stage or voice_stage.get("status") != REVIEW_STATUS_PENDING:
                        raise ValueError("当前没有待取消的音色确认。")
                    review_state_manager.set_stage(
                        VOICE_REVIEW_STAGE,
                        status=REVIEW_STATUS_SKIPPED,
                        activate=False,
                    )
                    snapshot = self.server.job_manager.cancel_waiting_review(  # type: ignore[attr-defined]
                        expected_stage=VOICE_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/translation/save":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                    )
                    _save_translation_review_submission(
                        project_dir=project_dir,
                        translation_segments_payload=payload.get("segments"),
                        status=REVIEW_STATUS_PENDING,
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                if parsed_path.path == "/api/review/translation-config/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=TRANSLATION_CONFIG_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    # Save selected model and prompt to review state
                    review_state_path = Path(project_dir) / "review_state.json"
                    review_state_manager = ReviewStateManager(review_state_path)
                    review_state_manager.set_stage(
                        TRANSLATION_CONFIG_REVIEW_STAGE,
                        status=REVIEW_STATUS_APPROVED,
                        payload={
                            "selected_model": payload.get("selected_model"),
                            "prompt_template": payload.get("prompt_template"),
                        },
                    )
                    # Optionally persist prompt to config
                    if payload.get("save_prompt"):
                        try:
                            save_web_ui_settings(
                                translation_model_alias=str(payload.get("selected_model") or ""),
                                translation_prompt_template=payload.get("prompt_template"),
                                provider_api_keys={},
                            )
                        except Exception:
                            pass  # Non-critical: prompt save failure shouldn't block flow
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=TRANSLATION_CONFIG_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/review/translation/approve":
                    payload = self._read_json()
                    project_dir = _resolve_authoritative_review_project_dir(
                        manager=self.server.job_manager,  # type: ignore[attr-defined]
                        requested_project_dir=payload.get("project_dir"),
                        expected_stage=TRANSLATION_REVIEW_STAGE,
                        require_waiting_review=True,
                    )
                    _save_translation_review_submission(
                        project_dir=project_dir,
                        translation_segments_payload=payload.get("segments"),
                        status=REVIEW_STATUS_APPROVED,
                    )
                    snapshot = self.server.job_manager.continue_after_review(  # type: ignore[attr-defined]
                        expected_stage=TRANSLATION_REVIEW_STAGE
                    )
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            **build_web_ui_snapshot(  # type: ignore[arg-type]
                                manager=self.server.job_manager  # type: ignore[attr-defined]
                            ),
                            "job": snapshot,
                        },
                    )
                    return
                if parsed_path.path == "/api/settings":
                    payload = self._read_json()
                    updated_route = save_web_ui_settings(
                        translation_model_alias=str(payload.get("translation_model_alias") or ""),
                        speaker_infer_prompt_template=_normalize_optional_text(
                            payload.get("speaker_infer_prompt_template")
                        ),
                        translation_prompt_template=_normalize_optional_text(
                            payload.get("translation_prompt_template")
                        ),
                        rewrite_prompt_template=_normalize_optional_text(
                            payload.get("rewrite_prompt_template")
                        ),
                        provider_api_keys={
                            "gemini": _normalize_optional_text(payload.get("gemini_api_key")),
                            "deepseek": _normalize_optional_text(payload.get("deepseek_api_key")),
                            "openai": _normalize_optional_text(payload.get("openai_api_key")),
                            "anthropic": _normalize_optional_text(payload.get("anthropic_api_key")),
                        },
                        config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                    )
                    snapshot = build_web_ui_snapshot(  # type: ignore[arg-type]
                        manager=self.server.job_manager  # type: ignore[attr-defined]
                    )
                    snapshot["settings"]["s3_translate_route"] = [
                        {
                            "alias": alias,
                            "label": _find_translation_model_label(
                                alias,
                                config_path=self.server.job_manager.config_path,  # type: ignore[attr-defined]
                            ),
                        }
                        for alias in updated_route
                    ]
                    self._write_json(HTTPStatus.OK, snapshot)
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (ValueError, StateError) as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive server fallback
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_video_upload(self) -> None:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "需要 multipart/form-data 格式上传。"})
                return

            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length <= 0:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "上传文件不能为空。"})
                return

            # 限制最大 2GB
            max_size = 2 * 1024 * 1024 * 1024
            if content_length > max_size:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "文件太大，最大支持 2GB。"})
                return

            import cgi
            import tempfile as _tempfile

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(content_length),
                },
            )
            file_item = form["file"] if "file" in form else None
            if file_item is None or not hasattr(file_item, "file"):
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "未找到上传文件字段 'file'。"})
                return

            original_filename = getattr(file_item, "filename", "uploaded_video.mp4") or "uploaded_video.mp4"
            upload_dir = Path(
                getattr(self.server.job_manager, "project_root", None) or "."  # type: ignore[attr-defined]
            ) / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)

            # 用时间戳避免文件名冲突
            import time as _time
            safe_name = re.sub(r"[^\w.\-]", "_", original_filename)
            dest_path = upload_dir / f"{int(_time.time())}_{safe_name}"

            with open(dest_path, "wb") as dest_file:
                while True:
                    chunk = file_item.file.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    dest_file.write(chunk)

            file_size_mb = dest_path.stat().st_size / (1024 * 1024)
            self._write_json(HTTPStatus.OK, {
                "file_path": str(dest_path),
                "file_name": original_filename,
                "file_size_mb": round(file_size_mb, 2),
            })

        def _read_json(self) -> dict[str, object]:
            content_length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象。")
            return payload

        def _write_html(self, status: HTTPStatus, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_binary(
            self,
            status: HTTPStatus,
            payload: bytes,
            *,
            content_type: str,
            download_name: str | None = None,
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            if download_name:
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(download_name)}",
                )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return WebUIHandler


def load_llm_fallback_config_for_path(config_path: Path | None) -> dict[str, object]:
    if config_path is None:
        return load_llm_fallback_config()

    resolved_path = config_path.resolve(strict=False)
    original_path = llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH
    try:
        llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = resolved_path
        return load_llm_fallback_config()
    finally:
        llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = original_path


def _load_selected_translation_model_alias(config_path: Path) -> str:
    config = load_llm_fallback_config_for_path(config_path)
    route = list(config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"]))
    if route:
        return str(route[0])
    return "gemini_3_1_flash_lite_preview"


def _find_translation_model_label(alias: str, *, config_path: Path) -> str:
    for option in build_translation_model_options(config_path=config_path):
        if option["alias"] == alias:
            return option["label"]
    return alias


def _load_prompt_templates(config_path: Path) -> dict[str, dict[str, str]]:
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    prompts_section = editable_payload.get("prompts")
    result: dict[str, dict[str, str]] = {}
    for prompt_key, (default_loader, validator) in PROMPT_TEMPLATE_LOADERS.items():
        raw_template = None
        if isinstance(prompts_section, dict):
            raw_template = _normalize_optional_text(prompts_section.get(prompt_key))
        if raw_template is not None:
            try:
                result[prompt_key] = {
                    "template": validator(raw_template),
                    "source": "custom",
                }
                continue
            except TranslationError:
                pass
        result[prompt_key] = {
            "template": default_loader(None),
            "source": "default",
        }
    return result


def _build_results_snapshot(
    *,
    project_root: Path,
    job_snapshot: dict[str, object],
) -> dict[str, object]:
    project_dir, source = _resolve_project_dir_for_results(project_root=project_root, job_snapshot=job_snapshot)
    if project_dir is None:
        return {
            "available": False,
            "source": source,
            "source_label": _describe_results_source(source),
            "project_dir": None,
            "project_name": None,
            "source_context": build_empty_source_context_summary(),
            "workflow_note": "当前还没有可展示的项目结果。先运行一次任务，或等待已有项目产物被识别。",
            "manifest_path": None,
            "editor_outputs": [],
            "publish_outputs": [],
            "needs_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "items": [],
            },
            "transcript_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "speaker_count": 0,
                "confirmed_count": 0,
                "needs_review_count": 0,
                "items": [],
            },
            "translation_review": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "confirmed_count": 0,
                "rewrite_requested_count": 0,
                "existing_rewrite_count": 0,
                "items": [],
            },
            "audio_alignment": {
                "total_items": 0,
                "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
                "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
                "speaker_options": [],
                "items": [],
            },
            "review_flow": {
                "path": None,
                "load_error": None,
                "active_stage": None,
                "active_review": None,
                "stages": {},
            },
            "project_state": build_empty_project_state_summary(),
        }

    editor_outputs = _build_editor_output_entries(project_dir)
    publish_outputs = _build_publish_output_entries(project_dir)
    transcript_items = _load_transcript_review_items(project_dir)
    translation_items = _load_translation_review_items(project_dir)
    audio_alignment_items = translation_items
    needs_review_items = [item for item in translation_items if bool(item.get("needs_review"))]
    transcript_confirmed_count = sum(
        1
        for item in transcript_items
        if bool(item.get("speaker_confirmed")) and bool(item.get("transcript_confirmed"))
    )
    transcript_needs_review_count = sum(1 for item in transcript_items if bool(item.get("needs_review")))
    translation_confirmed_count = sum(1 for item in translation_items if bool(item.get("translation_confirmed")))
    translation_rewrite_requested_count = sum(1 for item in translation_items if bool(item.get("rewrite_requested")))
    translation_existing_rewrite_count = sum(1 for item in translation_items if int(item.get("rewrite_count") or 0) > 0)
    project_state = _load_project_state_summary(project_dir)
    manifest_payload = load_manifest_payload(project_dir=project_dir)
    manifest_path = _stringify_existing_path(project_dir / "manifest.json")
    review_flow = _build_review_flow_snapshot(project_dir)
    source_context = build_source_context_summary(
        manifest_payload=manifest_payload,
        fallback_locator=_normalize_optional_text(job_snapshot.get("youtube_url")),
    )
    project_name = source_context["video_title"] or project_dir.name
    available_output_count = sum(
        1
        for item in [*editor_outputs, *publish_outputs]
        if item.get("path")
    )
    return {
        "available": True,
        "source": source,
        "source_label": _describe_results_source(source),
        "project_dir": str(project_dir),
        "project_name": project_name,
        "source_context": source_context,
        "workflow_note": (
            "当前 Web UI 仍以 legacy process 为主，所以结果页优先展示 editor 产物；"
            "manifest 和 publish 产物会在项目中存在时自动显示。"
        ),
        "manifest_path": manifest_path,
        "available_output_count": available_output_count,
        "editor_outputs": editor_outputs,
        "publish_outputs": publish_outputs,
        "needs_review": {
            "total_items": len(needs_review_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(needs_review_items),
            "items": needs_review_items,
        },
        "transcript_review": {
            "total_items": len(transcript_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(transcript_items),
            "speaker_count": len(_build_segment_speaker_options(transcript_items)),
            "confirmed_count": transcript_confirmed_count,
            "needs_review_count": transcript_needs_review_count,
            "items": transcript_items,
        },
        "translation_review": {
            "total_items": len(translation_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(translation_items),
            "confirmed_count": translation_confirmed_count,
            "rewrite_requested_count": translation_rewrite_requested_count,
            "existing_rewrite_count": translation_existing_rewrite_count,
            "items": translation_items,
        },
        "audio_alignment": {
            "total_items": len(audio_alignment_items),
            "default_page_size": DEFAULT_RESULT_PAGE_SIZE,
            "page_size_options": list(RESULT_PAGE_SIZE_OPTIONS),
            "speaker_options": _build_segment_speaker_options(audio_alignment_items),
            "items": audio_alignment_items,
        },
        "review_flow": review_flow,
        "project_state": project_state,
    }


def _resolve_project_dir_for_results(
    *,
    project_root: Path,
    job_snapshot: dict[str, object],
) -> tuple[Path | None, str]:
    projects_root = (project_root / "projects").resolve(strict=False)
    if not projects_root.exists():
        return None, "no_projects_root"

    explicit_project_dir = _normalize_optional_text(job_snapshot.get("project_dir"))
    if explicit_project_dir is not None:
        candidate_project_dir = Path(explicit_project_dir).resolve(strict=False)
        if candidate_project_dir.exists() and _path_is_within_root(candidate_project_dir, projects_root):
            return candidate_project_dir, "matched_youtube_url"

    youtube_url = _normalize_optional_text(job_snapshot.get("youtube_url"))
    if youtube_url is not None:
        matched_project = _find_project_dir_by_youtube_url(projects_root=projects_root, youtube_url=youtube_url)
        if matched_project is not None:
            return matched_project, "matched_youtube_url"

    logs = job_snapshot.get("logs")
    if isinstance(logs, list):
        for raw_line in reversed(logs):
            if not isinstance(raw_line, str):
                continue
            path = _extract_project_dir_from_log_line(raw_line, projects_root=projects_root)
            if path is not None:
                return path, "log_path"

    return None, "no_project_match"


def _describe_results_source(source: str) -> str:
    normalized_source = source.strip()
    if not normalized_source:
        return "未知来源"
    return RESULT_SOURCE_LABELS.get(normalized_source, normalized_source)


def _find_project_dir_by_youtube_url(*, projects_root: Path, youtube_url: str) -> Path | None:
    normalized_url = youtube_url.strip()
    if not normalized_url:
        return None
    for candidate in projects_root.iterdir():
        if not candidate.is_dir():
            continue
        manifest_source_url = build_source_context_summary(
            manifest_payload=load_manifest_payload(project_dir=candidate),
        ).get("locator")
        if manifest_source_url == normalized_url:
            return candidate.resolve(strict=False)
        metadata_path = candidate / "download_metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        stored_url = _normalize_optional_text(metadata.get("url"))
        if stored_url == normalized_url:
            return candidate.resolve(strict=False)
    return None


def _find_latest_project_dir(projects_root: Path) -> Path | None:
    candidates = [candidate for candidate in projects_root.iterdir() if candidate.is_dir()]
    if not candidates:
        return None
    latest_candidate = max(candidates, key=lambda item: item.stat().st_mtime)
    return latest_candidate.resolve(strict=False)


def _extract_project_dir_from_log_line(raw_line: str, *, projects_root: Path) -> Path | None:
    for match in WINDOWS_PATH_PATTERN.findall(raw_line):
        candidate_path = Path(match).resolve(strict=False)
        if candidate_path.name.lower() == "output":
            project_dir = candidate_path.parent
        else:
            project_dir = candidate_path
        if project_dir.exists() and _path_is_within_root(project_dir, projects_root):
            return project_dir
    return None


def _path_is_within_root(candidate_path: Path, root_path: Path) -> bool:
    try:
        candidate_path.resolve(strict=False).relative_to(root_path.resolve(strict=False))
    except ValueError:
        return False
    return True


def _is_project_audio_file(candidate_path: Path) -> bool:
    if not candidate_path.exists() or not candidate_path.is_file():
        return False
    if candidate_path.suffix.lower() in PROJECT_AUDIO_FILE_SUFFIXES:
        return True
    guessed_type = mimetypes.guess_type(str(candidate_path))[0] or ""
    return guessed_type.lower().startswith("audio/")


def _resolve_authoritative_review_project_dir(
    *,
    manager: ProcessJobManager | JobAPIBackedJobManager,
    requested_project_dir: object,
    expected_stage: str | None = None,
    require_waiting_review: bool = False,
) -> Path:
    job_snapshot = manager.snapshot()
    project_root = manager.project_root.resolve(strict=False)
    projects_root = (project_root / "projects").resolve(strict=False)
    authoritative_project_dir_text = _normalize_optional_text(job_snapshot.get("project_dir"))
    if authoritative_project_dir_text is None:
        raise ValueError("当前没有可写入 review 的真实项目上下文。")

    authoritative_project_dir = Path(authoritative_project_dir_text).expanduser().resolve(strict=False)
    if not _path_is_within_root(authoritative_project_dir, projects_root):
        raise ValueError("当前任务绑定的项目目录超出了 projects 根目录。")

    requested_project_dir_text = _normalize_optional_text(requested_project_dir)
    if requested_project_dir_text is not None:
        requested_path = Path(requested_project_dir_text).expanduser().resolve(strict=False)
        if requested_path != authoritative_project_dir:
            raise ValueError("请求里的 project_dir 与当前真实任务项目不一致。")

    if require_waiting_review:
        if str(job_snapshot.get("status") or "").strip() != JOB_STATUS_WAITING_FOR_REVIEW:
            raise ValueError("当前任务不在等待 review 的状态。")
        active_review = _copy_optional_mapping(job_snapshot.get("review_gate")) or {}
        active_stage = _normalize_optional_text(active_review.get("stage"))
        if expected_stage is not None and active_stage != expected_stage:
            raise ValueError("当前等待确认的 review 阶段与请求不一致。")

    return authoritative_project_dir


def _build_current_project_audio_preview_paths(
    *,
    project_dir: Path,
    results_snapshot: dict[str, object],
) -> set[Path]:
    allowed_paths: set[Path] = set()
    for section_name in ("translation_review", "audio_alignment"):
        section_payload = results_snapshot.get(section_name)
        if not isinstance(section_payload, dict):
            continue
        raw_items = section_payload.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            for field_name in ("tts_audio_path", "aligned_audio_path"):
                resolved_path_text = _normalize_optional_text(raw_item.get(field_name))
                if resolved_path_text is None:
                    continue
                resolved_path = Path(resolved_path_text).expanduser().resolve(strict=False)
                if _path_is_within_root(resolved_path, project_dir) and _is_project_audio_file(resolved_path):
                    allowed_paths.add(resolved_path)
    return allowed_paths


def _resolve_allowed_project_file_download_path(
    *,
    manager: ProcessJobManager | JobAPIBackedJobManager,
    requested_path: str,
) -> Path:
    candidate_path = Path(requested_path).expanduser().resolve(strict=False)
    if not candidate_path.exists() or not candidate_path.is_file():
        raise FileNotFoundError("Requested file was not found.")

    snapshot = build_web_ui_snapshot(manager=manager)
    results_snapshot = _ensure_dict(snapshot.get("results"))
    current_project_dir_text = _normalize_optional_text(results_snapshot.get("project_dir"))
    if current_project_dir_text is None:
        raise ValueError("当前没有可用于音频预览的项目目录。")

    project_root = manager.project_root.resolve(strict=False)
    projects_root = (project_root / "projects").resolve(strict=False)
    current_project_dir = Path(current_project_dir_text).expanduser().resolve(strict=False)
    if not _path_is_within_root(current_project_dir, projects_root):
        raise ValueError("当前结果项目目录超出了 projects 根目录。")
    if not _path_is_within_root(candidate_path, current_project_dir):
        raise ValueError("Requested file is outside the current project directory.")
    if not _is_project_audio_file(candidate_path):
        raise ValueError("Requested file is not an allowed audio preview file.")

    allowed_paths = _build_current_project_audio_preview_paths(
        project_dir=current_project_dir,
        results_snapshot=results_snapshot,
    )
    if candidate_path not in allowed_paths:
        raise ValueError("Requested file is not in the current project's audio preview whitelist.")
    return candidate_path


def _resolve_public_result_download_path(
    *,
    project_root: Path,
    project_dir: Path,
    download_key: str,
) -> Path | None:
    normalized_key = download_key.strip()
    if normalized_key not in PUBLIC_RESULT_DOWNLOAD_KEYS:
        raise ValueError(f"Requested download key is not allowed: {normalized_key}")

    projects_root = (project_root / "projects").resolve(strict=False)
    resolved_project_dir = _resolve_project_dir_under_projects_root(
        project_dir=project_dir,
        projects_root=projects_root,
    )
    if resolved_project_dir is None:
        raise ValueError("Requested project is outside projects root.")

    resolved_project_dir = resolved_project_dir.resolve(strict=False)
    projects_root = projects_root.resolve(strict=False)
    if not _path_is_within_root(resolved_project_dir, projects_root):
        raise ValueError("Requested project is outside projects root.")

    if normalized_key == RESULT_DOWNLOAD_KEY_MANIFEST:
        candidate_path = (resolved_project_dir / "manifest.json").resolve(strict=False)
    else:
        artifact_index = load_manifest_artifact_index(project_dir=resolved_project_dir)
        candidate_path = _resolve_artifact_path(
            resolved_project_dir,
            normalized_key,
            artifact_index=artifact_index,
        )
        if candidate_path is None:
            return None
        candidate_path = candidate_path.resolve(strict=False)

    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    if not _path_is_within_root(candidate_path, resolved_project_dir):
        raise ValueError("Resolved download path is outside the project directory.")
    return candidate_path


def _resolve_project_dir_by_job_id(
    *,
    manager: ProcessJobManager | JobAPIBackedJobManager,
    job_id: str,
) -> str | None:
    normalized_job_id = job_id.strip()
    if not normalized_job_id:
        return None

    if isinstance(manager, JobAPIBackedJobManager):
        try:
            payload = manager._request_json("GET", f"/jobs/{normalized_job_id}", None)
        except (JobAPIRequestError, ConnectionError):
            return None
        return _normalize_optional_text(payload.get("project_dir"))

    snapshot = manager.snapshot()
    snapshot_job_id = _normalize_optional_text(snapshot.get("job_id"))
    if snapshot_job_id != normalized_job_id:
        return None
    return _normalize_optional_text(snapshot.get("project_dir"))


def _resolve_project_dir_under_projects_root(
    *,
    project_dir: Path,
    projects_root: Path,
) -> Path | None:
    normalized_projects_root = projects_root.resolve(strict=False)
    resolved_project_dir = project_dir.resolve(strict=False)
    if _path_is_within_root(resolved_project_dir, normalized_projects_root):
        return resolved_project_dir

    relative_candidate = _extract_relative_path_after_projects_segment(resolved_project_dir)
    if relative_candidate is None:
        return None
    rewritten_project_dir = (normalized_projects_root / relative_candidate).resolve(strict=False)
    if not _path_is_within_root(rewritten_project_dir, normalized_projects_root):
        return None
    return rewritten_project_dir


def _extract_relative_path_after_projects_segment(path: Path) -> Path | None:
    parts = path.parts
    projects_index = -1
    for index, value in enumerate(parts):
        if value == "projects":
            projects_index = index
    if projects_index < 0 or projects_index + 1 >= len(parts):
        return None
    return Path(*parts[projects_index + 1 :])


def _resolve_artifact_path(
    project_dir: Path,
    artifact_key: str,
    *,
    artifact_index: dict[str, str] | None = None,
) -> Path | None:
    return resolve_manifest_artifact_path(
        project_dir,
        artifact_key,
        artifact_index=artifact_index,
    )


def _build_output_entry_from_artifact(
    label: str,
    *,
    project_dir: Path,
    artifact_index: dict[str, str],
    artifact_key: str,
    fallback_path: Path,
) -> dict[str, object]:
    artifact_path = _resolve_artifact_path(
        project_dir,
        artifact_key,
        artifact_index=artifact_index,
    )
    return _build_output_entry(
        label,
        artifact_path or fallback_path,
        download_key=artifact_key if artifact_path is not None else None,
    )


def _resolve_review_state_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "state.review") or (
        project_dir / "review_state.json"
    ).resolve(strict=False)


def _resolve_transcript_structured_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "media.transcript_structured") or (
        project_dir / "transcript" / "transcript.json"
    ).resolve(strict=False)


def _resolve_translation_segments_path(project_dir: Path) -> Path:
    return _resolve_artifact_path(project_dir, "translation.segments") or (
        project_dir / "translation" / "segments.json"
    ).resolve(strict=False)


def _build_editor_output_entries(project_dir: Path) -> list[dict[str, object]]:
    artifact_index = load_manifest_artifact_index(project_dir=project_dir)
    output_dir = project_dir / "output"
    return [
        _build_output_entry("项目目录", project_dir),
        _build_output_entry("输出目录", output_dir),
        _build_output_entry_from_artifact(
            "完整配音",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.dubbed_audio_complete",
            fallback_path=output_dir / "dubbed_audio_complete.wav",
        ),
        _build_output_entry_from_artifact(
            "环境音轨",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.ambient_audio",
            fallback_path=output_dir / "ambient_audio.wav",
        ),
        _build_output_entry_from_artifact(
            "字幕文件",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.subtitles",
            fallback_path=output_dir / "subtitles.srt",
        ),
        _build_output_entry_from_artifact(
            "分段音频目录",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.segments_dir",
            fallback_path=output_dir / "segments",
        ),
        _build_output_entry_from_artifact(
            "对齐报告",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="editor.alignment_report",
            fallback_path=output_dir / "alignment_report.md",
        ),
        _build_output_entry("背景音说明", output_dir / "background_sounds.txt"),
        _build_output_entry_from_artifact(
            "翻译分段",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="translation.segments",
            fallback_path=project_dir / "translation" / "segments.json",
        ),
    ]


def _build_publish_output_entries(project_dir: Path) -> list[dict[str, object]]:
    artifact_index = load_manifest_artifact_index(project_dir=project_dir)
    return [
        _build_output_entry(
            "Manifest",
            project_dir / "manifest.json",
            download_key=RESULT_DOWNLOAD_KEY_MANIFEST,
        ),
        _build_output_entry("发布目录", project_dir / "publish"),
        _build_output_entry_from_artifact(
            "成品视频",
            project_dir=project_dir,
            artifact_index=artifact_index,
            artifact_key="publish.dubbed_video",
            fallback_path=project_dir / "publish" / "dubbed_video.mp4",
        ),
    ]


def _build_output_entry(label: str, path: Path, *, download_key: str | None = None) -> dict[str, object]:
    resolved_path = path.resolve(strict=False)
    exists = resolved_path.exists()
    return {
        "label": label,
        "path": str(resolved_path) if exists else None,
        "exists": exists,
        "download_key": download_key if exists and download_key in PUBLIC_RESULT_DOWNLOAD_KEYS else None,
    }


def _load_project_state_summary(project_dir: Path) -> dict[str, object]:
    state_path = _resolve_artifact_path(project_dir, "state.project") or (
        project_dir / "project_state.json"
    ).resolve(strict=False)
    snapshot = build_empty_project_state_summary()
    if not state_path.exists():
        return snapshot

    state_manager = StateManager(str(state_path))
    try:
        state = state_manager.load()
    except StateError as exc:
        snapshot["path"] = str(state_path)
        snapshot["load_error"] = str(exc)
        return snapshot
    return build_project_state_summary(state, state_path=str(state_path))


def _build_review_flow_snapshot(project_dir: Path) -> dict[str, object]:
    review_state_path = _resolve_review_state_path(project_dir)
    snapshot: dict[str, object] = {
        "path": str(review_state_path),
        "load_error": None,
        "active_stage": None,
        "active_review": None,
        "stages": {},
    }
    review_state_manager = ReviewStateManager(review_state_path)
    try:
        state = review_state_manager.load()
    except StateError as exc:
        snapshot["load_error"] = str(exc)
        return snapshot

    stages = state.get("stages", {})
    active_stage = state.get("active_stage")
    active_review = stages.get(active_stage) if isinstance(stages, dict) and active_stage else None
    snapshot["active_stage"] = active_stage
    snapshot["active_review"] = active_review
    snapshot["stages"] = stages if isinstance(stages, dict) else {}
    return snapshot


def _load_review_stage_payload(project_dir: Path, stage_name: str) -> dict[str, object] | None:
    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
    try:
        stage_payload = review_state_manager.get_stage(stage_name)
    except StateError:
        return None
    if not stage_payload:
        return None
    payload = stage_payload.get("payload")
    return payload if isinstance(payload, dict) else None


def _read_speaker_review_mappings(project_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    speaker_names, segment_speakers, _confirmations = _read_speaker_review_state(project_dir)
    return speaker_names, segment_speakers


def _read_speaker_review_state(
    project_dir: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, object]]]:
    speaker_review_payload = _load_review_stage_payload(project_dir, SPEAKER_REVIEW_STAGE) or {}
    speaker_names = speaker_review_payload.get("speaker_names", {})
    segment_speakers = speaker_review_payload.get("segment_speakers", {})
    confirmations = speaker_review_payload.get("confirmations", {})
    if not isinstance(speaker_names, dict):
        speaker_names = {}
    if not isinstance(segment_speakers, dict):
        segment_speakers = {}
    if not isinstance(confirmations, dict):
        confirmations = {}
    return (
        {
            str(speaker_id): str(display_name)
            for speaker_id, display_name in speaker_names.items()
            if _normalize_optional_text(speaker_id) is not None
            and _normalize_optional_text(display_name) is not None
        },
        {
            str(segment_id): str(speaker_id)
            for segment_id, speaker_id in segment_speakers.items()
            if _normalize_optional_text(segment_id) is not None
            and _normalize_optional_text(speaker_id) is not None
        },
        {
            str(segment_id): {
                "speaker_confirmed": bool(raw_entry.get("speaker_confirmed")),
                "transcript_confirmed": bool(raw_entry.get("transcript_confirmed")),
                "updated_at": _normalize_optional_text(raw_entry.get("updated_at")) or "",
            }
            for segment_id, raw_entry in confirmations.items()
            if _normalize_optional_text(segment_id) is not None and isinstance(raw_entry, dict)
        },
    )


def _apply_speaker_review_overrides(
    items: list[dict[str, object]],
    *,
    speaker_names: dict[str, str],
    segment_speakers: dict[str, str],
    confirmations: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    confirmations = confirmations or {}
    for item in items:
        segment_id = str(item.get("segment_id") or "").strip()
        reviewed_speaker_id = _normalize_optional_text(segment_speakers.get(segment_id))
        if reviewed_speaker_id is not None:
            item["speaker_id"] = reviewed_speaker_id
        reviewed_display_name = _normalize_optional_text(speaker_names.get(str(item.get("speaker_id") or "")))
        if reviewed_display_name is not None:
            item["display_name"] = reviewed_display_name
        confirmation_entry = confirmations.get(segment_id)
        if isinstance(confirmation_entry, dict):
            item["speaker_confirmed"] = bool(confirmation_entry.get("speaker_confirmed"))
            item["transcript_confirmed"] = bool(confirmation_entry.get("transcript_confirmed"))
            item["review_updated_at"] = _normalize_optional_text(confirmation_entry.get("updated_at")) or ""
    return items


def _load_transcript_review_items(project_dir: Path) -> list[dict[str, object]]:
    segment_items = _load_segment_items(project_dir)
    speaker_names, segment_speakers, confirmations = _read_speaker_review_state(project_dir)
    if segment_items:
        return _apply_speaker_review_overrides(
            segment_items,
            speaker_names=speaker_names,
            segment_speakers=segment_speakers,
            confirmations=confirmations,
        )

    transcript_path = _resolve_transcript_structured_path(project_dir)
    if not transcript_path.exists():
        return []
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    lines = payload.get("lines", [])
    if not isinstance(lines, list):
        return []

    transcript_items: list[dict[str, object]] = []
    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        raw_segment_id = raw_line.get("index")
        segment_id = int(raw_segment_id) if isinstance(raw_segment_id, int) else raw_segment_id
        speaker_id = _normalize_optional_text(segment_speakers.get(str(segment_id))) or _normalize_optional_text(
            raw_line.get("speaker_id")
        ) or "speaker_a"
        display_name = _normalize_optional_text(speaker_names.get(speaker_id)) or speaker_id
        start_ms = int(raw_line.get("start_ms") or 0)
        end_ms = int(raw_line.get("end_ms") or 0)
        transcript_items.append(
            {
                "segment_id": segment_id,
                "speaker_id": speaker_id,
                "display_name": display_name,
                "source_text": _normalize_optional_text(raw_line.get("source_text")) or "",
                "cn_text": "",
                "tts_cn_text": "",
                "tts_audio_path": None,
                "aligned_audio_path": None,
                "alignment_method": "",
                "rewrite_count": 0,
                "needs_review": False,
                "speaker_confirmed": False,
                "transcript_confirmed": False,
                "translation_confirmed": False,
                "rewrite_requested": False,
                "review_updated_at": "",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "actual_duration_ms": max(0, end_ms - start_ms),
                "target_duration_ms": max(0, end_ms - start_ms),
                "has_audio_preview": False,
            }
        )
    transcript_items.sort(key=_segment_item_sort_key)
    return transcript_items


def _load_translation_review_items(project_dir: Path) -> list[dict[str, object]]:
    segment_items = _load_segment_items(project_dir)
    if not segment_items:
        return []

    speaker_names, segment_speakers, speaker_confirmations = _read_speaker_review_state(project_dir)
    translation_review_payload = _load_review_stage_payload(project_dir, TRANSLATION_REVIEW_STAGE) or {}
    translation_segments = translation_review_payload.get("segments", {})
    if not isinstance(translation_segments, dict):
        translation_segments = {}

    _apply_speaker_review_overrides(
        segment_items,
        speaker_names=speaker_names,
        segment_speakers=segment_speakers,
        confirmations=speaker_confirmations,
    )

    for item in segment_items:
        translation_override = translation_segments.get(str(item.get("segment_id")))
        if isinstance(translation_override, dict):
            cn_text = _normalize_optional_text(translation_override.get("cn_text"))
            tts_cn_text = _normalize_optional_text(translation_override.get("tts_cn_text"))
            if cn_text is not None:
                item["cn_text"] = cn_text
            if tts_cn_text is not None:
                item["tts_cn_text"] = tts_cn_text
            item["translation_confirmed"] = bool(translation_override.get("translation_confirmed"))
            item["rewrite_requested"] = bool(translation_override.get("rewrite_requested"))
            item["review_updated_at"] = _normalize_optional_text(translation_override.get("updated_at")) or ""
    return segment_items


def _load_transcript_payload(project_dir: Path) -> dict[str, object]:
    transcript_path = _resolve_transcript_structured_path(project_dir)
    if not transcript_path.exists():
        raise ValueError("当前项目还没有可确认的 transcript.json。")
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 transcript.json：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("transcript.json 结构无效。")
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise ValueError("transcript.json 缺少 lines 列表。")
    return payload


def _load_translation_segments_payload(project_dir: Path) -> dict[str, object]:
    segments_path = _resolve_translation_segments_path(project_dir)
    if not segments_path.exists():
        raise ValueError("Current project does not have translation/segments.json yet.")
    try:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read segments.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("segments.json has an invalid structure.")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("segments.json is missing the segments list.")
    return payload


def _normalize_translation_review_submission(
    *,
    translation_payload: dict[str, object],
    translation_segments_payload: object,
) -> dict[str, object]:
    segments = translation_payload.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("segments.json is missing the segments list.")

    submitted_segments = translation_segments_payload if isinstance(translation_segments_payload, dict) else {}
    normalized_segments: dict[str, dict[str, object]] = {}
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        raw_segment_id = raw_segment.get("segment_id")
        segment_id = str(raw_segment_id or "").strip()
        if not segment_id:
            continue

        submitted_entry = submitted_segments.get(segment_id, {})
        if not isinstance(submitted_entry, dict):
            submitted_entry = {}

        cn_text = (
            _normalize_optional_text(submitted_entry.get("cn_text"))
            or _normalize_optional_text(raw_segment.get("cn_text"))
            or ""
        )
        tts_cn_text = (
            _normalize_optional_text(submitted_entry.get("tts_cn_text"))
            or _normalize_optional_text(raw_segment.get("tts_cn_text"))
            or cn_text
        )

        normalized_segments[segment_id] = {
            "segment_id": raw_segment_id,
            "speaker_id": _normalize_optional_text(raw_segment.get("speaker_id")) or "",
            "display_name": _normalize_optional_text(raw_segment.get("display_name")) or "",
            "source_text": _normalize_optional_text(raw_segment.get("source_text")) or "",
            "cn_text": cn_text,
            "tts_cn_text": tts_cn_text,
            "target_duration_ms": int(raw_segment.get("target_duration_ms") or 0),
            "rewrite_count": int(raw_segment.get("rewrite_count") or 0),
            "needs_review": bool(raw_segment.get("needs_review")),
            "translation_confirmed": bool(submitted_entry.get("translation_confirmed")),
            "rewrite_requested": bool(submitted_entry.get("rewrite_requested")),
            "updated_at": _normalize_optional_text(submitted_entry.get("updated_at")) or utc_now_iso(),
        }

    return {
        "segments": normalized_segments,
        "segment_count": len(normalized_segments),
    }


def _normalize_speaker_review_submission(
    *,
    transcript_payload: dict[str, object],
    speaker_names_payload: object,
    segment_speakers_payload: object,
    review_confirmations_payload: object,
) -> dict[str, object]:
    lines = transcript_payload.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("transcript.json 缺少 lines 列表。")

    discovered_speaker_ids: list[str] = []
    discovered_segment_ids: set[str] = set()
    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        segment_id = str(raw_line.get("index") or "").strip()
        speaker_id = _normalize_optional_text(raw_line.get("speaker_id")) or "speaker_a"
        if segment_id:
            discovered_segment_ids.add(segment_id)
        if speaker_id not in discovered_speaker_ids:
            discovered_speaker_ids.append(speaker_id)

    if not discovered_speaker_ids:
        discovered_speaker_ids = ["speaker_a"]

    normalized_speaker_names: dict[str, str] = {}
    if isinstance(speaker_names_payload, dict):
        for speaker_id in discovered_speaker_ids:
            normalized_speaker_names[speaker_id] = (
                _normalize_optional_text(speaker_names_payload.get(speaker_id)) or speaker_id
            )
    else:
        for speaker_id in discovered_speaker_ids:
            normalized_speaker_names[speaker_id] = speaker_id

    normalized_segment_speakers: dict[str, str] = {}
    if isinstance(segment_speakers_payload, dict):
        for segment_id, speaker_id in segment_speakers_payload.items():
            normalized_segment_id = str(segment_id or "").strip()
            normalized_speaker_id = _normalize_optional_text(speaker_id)
            if (
                normalized_segment_id
                and normalized_segment_id in discovered_segment_ids
                and normalized_speaker_id in discovered_speaker_ids
            ):
                normalized_segment_speakers[normalized_segment_id] = normalized_speaker_id

    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        segment_id = str(raw_line.get("index") or "").strip()
        speaker_id = _normalize_optional_text(raw_line.get("speaker_id")) or "speaker_a"
        if segment_id and segment_id not in normalized_segment_speakers:
            normalized_segment_speakers[segment_id] = speaker_id

    normalized_confirmations: dict[str, dict[str, object]] = {}
    if isinstance(review_confirmations_payload, dict):
        for segment_id, raw_entry in review_confirmations_payload.items():
            normalized_segment_id = str(segment_id or "").strip()
            if not normalized_segment_id or normalized_segment_id not in discovered_segment_ids:
                continue
            if not isinstance(raw_entry, dict):
                continue
            normalized_confirmations[normalized_segment_id] = {
                "speaker_confirmed": bool(raw_entry.get("speaker_confirmed")),
                "transcript_confirmed": bool(raw_entry.get("transcript_confirmed")),
                "updated_at": _normalize_optional_text(raw_entry.get("updated_at")) or utc_now_iso(),
            }

    return {
        "speaker_names": normalized_speaker_names,
        "speaker_options": [
            {"speaker_id": speaker_id, "display_name": normalized_speaker_names[speaker_id]}
            for speaker_id in discovered_speaker_ids
        ],
        "segment_speakers": normalized_segment_speakers,
        "confirmations": normalized_confirmations,
        "segment_count": len(discovered_segment_ids),
    }


def _write_approved_speaker_review_to_transcript(
    *,
    project_dir: Path,
    normalized_payload: dict[str, object],
) -> None:
    transcript_path = project_dir / "transcript" / "transcript.json"
    transcript_payload = _load_transcript_payload(project_dir)
    segment_speakers = normalized_payload.get("segment_speakers", {})
    speaker_names = normalized_payload.get("speaker_names", {})
    if not isinstance(segment_speakers, dict):
        segment_speakers = {}
    if not isinstance(speaker_names, dict):
        speaker_names = {}
    lines = transcript_payload.get("lines", [])
    if isinstance(lines, list):
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            segment_id = str(raw_line.get("index") or "").strip()
            reviewed_speaker_id = _normalize_optional_text(segment_speakers.get(segment_id))
            if reviewed_speaker_id is not None:
                raw_line["speaker_id"] = reviewed_speaker_id
            reviewed_speaker_name = _normalize_optional_text(speaker_names.get(str(raw_line.get("speaker_id") or "")))
            if reviewed_speaker_name is not None:
                raw_line["speaker_name"] = reviewed_speaker_name
    transcript_path.write_text(
        json.dumps(transcript_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_speaker_review_submission(
    *,
    project_dir: Path,
    speaker_names_payload: object,
    segment_speakers_payload: object,
    review_confirmations_payload: object,
    status: str,
) -> dict[str, object]:
    transcript_payload = _load_transcript_payload(project_dir)
    normalized_payload = _normalize_speaker_review_submission(
        transcript_payload=transcript_payload,
        speaker_names_payload=speaker_names_payload,
        segment_speakers_payload=segment_speakers_payload,
        review_confirmations_payload=review_confirmations_payload,
    )
    review_state_manager = ReviewStateManager(project_dir / "review_state.json")
    activate = status == REVIEW_STATUS_PENDING
    review_state_manager.set_stage(
        SPEAKER_REVIEW_STAGE,
        status=status,
        payload=normalized_payload,
        activate=activate,
    )
    if status == REVIEW_STATUS_APPROVED:
        _write_approved_speaker_review_to_transcript(
            project_dir=project_dir,
            normalized_payload=normalized_payload,
        )
    return normalized_payload


def _write_approved_translation_review_to_segments(
    *,
    project_dir: Path,
    normalized_payload: dict[str, object],
) -> None:
    segments_path = project_dir / "translation" / "segments.json"
    translation_payload = _load_translation_segments_payload(project_dir)
    reviewed_segments = normalized_payload.get("segments", {})
    if not isinstance(reviewed_segments, dict):
        reviewed_segments = {}

    segments = translation_payload.get("segments", [])
    if isinstance(segments, list):
        for raw_segment in segments:
            if not isinstance(raw_segment, dict):
                continue
            segment_id = str(raw_segment.get("segment_id") or "").strip()
            reviewed_segment = reviewed_segments.get(segment_id)
            if not isinstance(reviewed_segment, dict):
                continue
            raw_segment["cn_text"] = _normalize_optional_text(reviewed_segment.get("cn_text")) or ""
            raw_segment["tts_cn_text"] = (
                _normalize_optional_text(reviewed_segment.get("tts_cn_text"))
                or raw_segment["cn_text"]
            )

    segments_path.write_text(
        json.dumps(translation_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_translation_review_submission(
    *,
    project_dir: Path,
    translation_segments_payload: object,
    status: str,
) -> dict[str, object]:
    translation_payload = _load_translation_segments_payload(project_dir)
    normalized_payload = _normalize_translation_review_submission(
        translation_payload=translation_payload,
        translation_segments_payload=translation_segments_payload,
    )
    review_state_manager = ReviewStateManager(project_dir / "review_state.json")
    activate = status == REVIEW_STATUS_PENDING
    review_state_manager.set_stage(
        TRANSLATION_REVIEW_STAGE,
        status=status,
        payload=normalized_payload,
        activate=activate,
    )
    if status == REVIEW_STATUS_APPROVED:
        _write_approved_translation_review_to_segments(
            project_dir=project_dir,
            normalized_payload=normalized_payload,
        )
    return normalized_payload


def _resolve_project_file_path(project_dir: Path, raw_path: object) -> str | None:
    normalized_path = _normalize_optional_text(raw_path)
    if normalized_path is None:
        return None
    candidate_path = Path(normalized_path).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = (project_dir / candidate_path).resolve(strict=False)
    else:
        candidate_path = candidate_path.resolve(strict=False)
    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    return str(candidate_path)


def _load_segment_items(project_dir: Path) -> list[dict[str, object]]:
    segments_path = _resolve_translation_segments_path(project_dir)
    if not segments_path.exists():
        return []
    try:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        return []

    segment_items: list[dict[str, object]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        segment_id = item.get("segment_id")
        segment_items.append(
            {
                "segment_id": int(segment_id) if isinstance(segment_id, int) else segment_id,
                "speaker_id": _normalize_optional_text(item.get("speaker_id")) or "",
                "display_name": _normalize_optional_text(item.get("display_name")) or "Unknown speaker",
                "source_text": _normalize_optional_text(item.get("source_text")) or "",
                "cn_text": _normalize_optional_text(item.get("cn_text")) or "",
                "tts_cn_text": _normalize_optional_text(item.get("tts_cn_text")) or "",
                "tts_audio_path": _resolve_project_file_path(project_dir, item.get("tts_audio_path")),
                "aligned_audio_path": _resolve_project_file_path(project_dir, item.get("aligned_audio_path")),
                "alignment_method": _normalize_optional_text(item.get("alignment_method")) or "",
                "rewrite_count": int(item.get("rewrite_count") or 0),
                "needs_review": bool(item.get("needs_review")),
                "speaker_confirmed": False,
                "transcript_confirmed": False,
                "translation_confirmed": False,
                "rewrite_requested": False,
                "review_updated_at": "",
                "start_ms": int(item.get("start_ms") or 0),
                "end_ms": int(item.get("end_ms") or 0),
                "actual_duration_ms": int(item.get("actual_duration_ms") or 0),
                "target_duration_ms": int(item.get("target_duration_ms") or 0),
            }
        )
        segment_items[-1]["has_audio_preview"] = bool(
            segment_items[-1].get("tts_audio_path") or segment_items[-1].get("aligned_audio_path")
        )
    segment_items.sort(key=_segment_item_sort_key)
    return segment_items


def _segment_item_sort_key(item: dict[str, object]) -> int:
    try:
        return int(item.get("segment_id") or 0)
    except (TypeError, ValueError):
        return 0


def _build_segment_speaker_options(items: list[dict[str, object]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    options: list[dict[str, str]] = []
    for item in items:
        speaker_id = str(item.get("speaker_id") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        key = (speaker_id, display_name)
        if not speaker_id or key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "value": speaker_id,
                "label": display_name or speaker_id,
            }
        )
    options.sort(key=lambda item: item["label"].lower())
    return options


def _build_voice_library_snapshot(
    *,
    project_root: Path,
    config_path: Path,
    project_dir: Path | None,
    transcript_items: list[dict[str, object]],
) -> dict[str, object]:
    registry_path = _resolve_voice_registry_path(project_root=project_root, config_path=config_path)
    snapshot: dict[str, object] = {
        "path": str(registry_path),
        "exists": registry_path.exists(),
        "load_error": None,
        "speaker_count": 0,
        "voice_count": 0,
        "builtin_voice_count": 0,
        "project_default_builtin_voice": None,
        "builtin_voice_options": [],
        "active_review": None,
        "current_project_speakers": [],
        "speakers": [],
    }

    registry = VoiceRegistry(str(registry_path))
    try:
        registry_data = registry.load()
    except StateError as exc:
        snapshot["load_error"] = str(exc)
        return snapshot

    resolver = VoiceResolver(registry)
    speakers_payload = registry_data.get("speakers", {})
    registry_speakers: list[dict[str, object]] = []
    builtin_voice_options: list[dict[str, object]] = []
    total_voice_count = 0

    if isinstance(speakers_payload, dict):
        for speaker_id in sorted(speakers_payload.keys(), key=str):
            speaker_payload = speakers_payload.get(speaker_id)
            if not isinstance(speaker_payload, dict):
                continue
            profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
            resolution = resolver.resolve(profile.speaker_id)
            serialized_voices = [_serialize_registry_voice(voice) for voice in profile.voices]
            total_voice_count += len(serialized_voices)
            registry_speakers.append(
                {
                    "speaker_id": profile.speaker_id,
                    "speaker_name": profile.speaker_name,
                    "default_voice_id": profile.default_voice_id,
                    "default_voice_type": profile.default_voice_type,
                    "resolution_source": resolution.source,
                    "voice_count": len(serialized_voices),
                    "voices": serialized_voices,
                }
            )
            for voice in profile.voices:
                if voice.voice_type != "builtin":
                    continue
                builtin_voice_options.append(
                    {
                        "voice_id": voice.voice_id,
                        "speaker_id": profile.speaker_id,
                        "speaker_name": profile.speaker_name,
                        "label": voice.label,
                        "provider": voice.provider,
                        "tts_provider": voice.tts_provider,
                        "platform": voice.platform,
                        "voice_type": voice.voice_type,
                        "created_at": voice.created_at,
                        "verification_status": voice.verification_status,
                    }
                )

    builtin_voice_options.sort(
        key=lambda item: (
            str(item.get("speaker_name") or item.get("speaker_id") or "").lower(),
            str(item.get("label") or "").lower(),
            str(item.get("voice_id") or "").lower(),
        )
    )
    project_default_builtin_voice = registry.get_project_default_builtin_voice()
    snapshot.update(
        {
            "speaker_count": len(registry_speakers),
            "voice_count": total_voice_count,
            "builtin_voice_count": len(builtin_voice_options),
            "project_default_builtin_voice": (
                project_default_builtin_voice.to_dict()
                if project_default_builtin_voice is not None
                else None
            ),
            "builtin_voice_options": builtin_voice_options,
            "active_review": _build_active_voice_review_snapshot(
                project_dir=project_dir,
                registry=registry,
                resolver=resolver,
            ),
            "current_project_speakers": _build_current_project_voice_bindings(
                transcript_items=transcript_items,
                registry=registry,
                resolver=resolver,
            ),
            "speakers": registry_speakers,
        }
    )
    return snapshot


def _resolve_voice_registry_path(*, project_root: Path, config_path: Path) -> Path:
    project_config = config_loader.load_project_local_config(config_path)
    resolved_path, _ = config_loader.resolve_path_value(
        env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
        config=project_config,
        config_key_paths=(
            ("voice_registry", "registry_path"),
            ("tts", "voice_registry_path"),
            ("paths", "voice_registry_path"),
        ),
    )
    if resolved_path is not None:
        return Path(resolved_path).expanduser().resolve(strict=False)
    return (project_root / "voice_registry.json").resolve(strict=False)


def _build_active_voice_review_snapshot(
    *,
    project_dir: Path | None,
    registry: VoiceRegistry,
    resolver: VoiceResolver,
) -> dict[str, object] | None:
    if project_dir is None:
        return None
    review_state_manager = ReviewStateManager(_resolve_review_state_path(project_dir))
    stage_payload = review_state_manager.get_stage(VOICE_REVIEW_STAGE)
    if not stage_payload or stage_payload.get("status") != REVIEW_STATUS_PENDING:
        return None
    payload = stage_payload.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    serialized_speakers: list[dict[str, object]] = []
    raw_speakers = payload.get("speakers", [])
    if isinstance(raw_speakers, list):
        for raw_speaker in raw_speakers:
            if not isinstance(raw_speaker, dict):
                continue
            speaker_id = _normalize_optional_text(raw_speaker.get("speaker_id"))
            if speaker_id is None:
                continue
            profile = registry.get_speaker_profile(speaker_id)
            resolution = resolver.resolve(speaker_id)
            serialized_speakers.append(
                {
                    "speaker_id": speaker_id,
                    "speaker_label": _normalize_optional_text(raw_speaker.get("speaker_label")),
                    "speaker_name": _normalize_optional_text(raw_speaker.get("speaker_name")) or speaker_id,
                    "voice_arg_name": _normalize_optional_text(raw_speaker.get("voice_arg_name")),
                    "sample_path": _normalize_optional_text(raw_speaker.get("sample_path")),
                    "sample_duration_s": _coerce_float(raw_speaker.get("sample_duration_s"), default=0.0),
                    "silence_ratio": _coerce_float(raw_speaker.get("silence_ratio"), default=0.0),
                    "default_voice_id": profile.default_voice_id if profile is not None else None,
                    "default_voice_type": profile.default_voice_type if profile is not None else None,
                    "resolved_status": resolution.status,
                    "resolved_source": resolution.source,
                    "resolved_voice_id": resolution.voice_id,
                    "resolved_voice_type": resolution.voice_type,
                    "resolved_label": resolution.label,
                    "available_voices": (
                        [_serialize_registry_voice(voice) for voice in profile.voices]
                        if profile is not None
                        else []
                    ),
                }
            )

    return {
        "stage": VOICE_REVIEW_STAGE,
        "status": stage_payload.get("status"),
        "message": _normalize_optional_text(payload.get("message"))
        or _normalize_optional_text(stage_payload.get("message"))
        or "",
        "reason": _normalize_optional_text(payload.get("reason")),
        "speakers": serialized_speakers,
    }


def _build_current_project_voice_bindings(
    *,
    transcript_items: list[dict[str, object]],
    registry: VoiceRegistry,
    resolver: VoiceResolver,
) -> list[dict[str, object]]:
    current_speakers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in transcript_items:
        speaker_id = str(item.get("speaker_id") or "").strip()
        if not speaker_id or speaker_id in seen:
            continue
        seen.add(speaker_id)
        display_name = str(item.get("display_name") or "").strip() or speaker_id
        current_speakers.append((speaker_id, display_name))

    bindings: list[dict[str, object]] = []
    for speaker_id, display_name in current_speakers:
        profile = registry.get_speaker_profile(speaker_id)
        resolution = resolver.resolve(speaker_id)
        bindings.append(
            {
                "speaker_id": speaker_id,
                "display_name": display_name,
                "speaker_name": profile.speaker_name if profile is not None else None,
                "default_voice_id": profile.default_voice_id if profile is not None else None,
                "default_voice_type": profile.default_voice_type if profile is not None else None,
                "resolved_status": resolution.status,
                "resolved_source": resolution.source,
                "resolved_voice_id": resolution.voice_id,
                "resolved_voice_type": resolution.voice_type,
                "resolved_label": resolution.label,
                "available_voices": (
                    [_serialize_registry_voice(voice) for voice in profile.voices]
                    if profile is not None
                    else []
                ),
            }
        )
    return bindings


def _serialize_registry_voice(voice: object) -> dict[str, object]:
    return {
        "voice_id": getattr(voice, "voice_id", None),
        "voice_type": getattr(voice, "voice_type", None),
        "provider": getattr(voice, "provider", None),
        "tts_provider": getattr(voice, "tts_provider", None),
        "platform": getattr(voice, "platform", None),
        "label": getattr(voice, "label", None),
        "created_at": getattr(voice, "created_at", None),
        "source_audio_path": getattr(voice, "source_audio_path", None),
        "notes": getattr(voice, "notes", None),
        "verification_status": getattr(voice, "verification_status", None),
        "last_verified_at": getattr(voice, "last_verified_at", None),
        "last_verification_success": getattr(voice, "last_verification_success", None),
        "last_verification_audio_path": getattr(voice, "last_verification_audio_path", None),
        "last_verification_error": getattr(voice, "last_verification_error", None),
    }


def _find_builtin_voice_option(
    *,
    registry: VoiceRegistry,
    voice_id: str,
) -> dict[str, object] | None:
    registry_data = registry.load()
    speakers_payload = registry_data.get("speakers", {})
    if not isinstance(speakers_payload, dict):
        return None
    normalized_voice_id = str(voice_id).strip()
    if not normalized_voice_id:
        return None
    for speaker_id, speaker_payload in speakers_payload.items():
        if not isinstance(speaker_payload, dict):
            continue
        profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
        for voice in profile.voices:
            if voice.voice_type == "builtin" and voice.voice_id == normalized_voice_id:
                return {
                    "voice_id": voice.voice_id,
                    "provider": voice.provider,
                    "tts_provider": voice.tts_provider,
                    "platform": voice.platform,
                    "label": voice.label,
                    "created_at": voice.created_at,
                    "notes": voice.notes,
                }
    return None


def _stringify_existing_path(path: Path) -> str | None:
    resolved_path = path.resolve(strict=False)
    if not resolved_path.exists():
        return None
    return str(resolved_path)


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_provider_key_source(
    section: dict[str, object],
    *,
    api_key_env_var: str,
) -> str | None:
    configured_key = _normalize_optional_text(section.get("api_key"))
    if configured_key is not None:
        return "config"
    if api_key_env_var and _normalize_optional_text(os.environ.get(api_key_env_var)) is not None:
        return "env"
    return None


def _copy_optional_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return dict(value)


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:
        return


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
