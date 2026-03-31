from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlparse

from services.jobs.service import (
    JobConflictError,
    JobNotFoundError,
    JobService,
    UnsupportedJobRequestError,
)


JOB_API_DEFAULT_HOST = "127.0.0.1"
JOB_API_DEFAULT_PORT = 8877


def build_job_api_server(
    *,
    service: JobService,
    host: str = JOB_API_DEFAULT_HOST,
    port: int = JOB_API_DEFAULT_PORT,
) -> ThreadingHTTPServer:
    handler_class = _build_job_api_handler(service=service)
    return ThreadingHTTPServer((host, port), handler_class)


def _build_job_api_handler(*, service: JobService) -> type[BaseHTTPRequestHandler]:
    class JobAPIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if path_parts == ["jobs"]:
                    payload = {
                        "jobs": [record.to_dict() for record in service.list_jobs()],
                    }
                    self._write_json(HTTPStatus.OK, payload)
                    return
                if len(path_parts) == 2 and path_parts[0] == "jobs":
                    job = service.require_job(path_parts[1])
                    self._write_json(HTTPStatus.OK, job.to_dict())
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "logs":
                    events = service.read_logs(path_parts[1])
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            "job_id": path_parts[1],
                            "events": [event.to_dict() for event in events],
                            "lines": [event.message for event in events if event.message],
                        },
                    )
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "result-summary":
                    self._write_json(
                        HTTPStatus.OK,
                        service.get_result_summary(path_parts[1]),
                    )
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "artifacts":
                    self._write_json(
                        HTTPStatus.OK,
                        service.get_artifacts(path_parts[1]),
                    )
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except JobNotFoundError as exc:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except UnsupportedJobRequestError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflictError as exc:
                self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if path_parts == ["jobs"]:
                    payload = self._read_json_payload()
                    source_payload = payload.get("source", {})
                    if not isinstance(source_payload, dict):
                        raise ValueError("source must be an object")
                    # Parse optional snapshot fields
                    raw_requires_review = payload.get("requires_review")
                    parsed_requires_review = bool(raw_requires_review) if raw_requires_review is not None else None
                    raw_voice_clone = payload.get("voice_clone_enabled")
                    parsed_voice_clone = bool(raw_voice_clone) if raw_voice_clone is not None else None
                    raw_duration = payload.get("source_duration_seconds")
                    parsed_duration = float(raw_duration) if raw_duration is not None else None
                    raw_quota = payload.get("quota_cost")
                    parsed_quota = int(raw_quota) if raw_quota is not None else None
                    raw_est_duration = payload.get("estimated_duration_seconds")
                    parsed_est_duration = float(raw_est_duration) if raw_est_duration is not None else None

                    job = service.submit_job(
                        job_type=str(payload.get("job_type") or "localize_video"),
                        source_type=str(source_payload.get("type") or ""),
                        source_ref=str(source_payload.get("value") or ""),
                        output_target=str(payload.get("output_target") or "editor"),
                        speakers=str(payload.get("speakers") or "auto"),
                        voice_a=str(payload.get("voice_a") or "").strip() or None,
                        voice_b=str(payload.get("voice_b") or "").strip() or None,
                        transcription_method=str(payload.get("transcription_method") or "assemblyai").strip() or None,
                        service_mode=str(payload["service_mode"]).strip() if payload.get("service_mode") else None,
                        tts_provider=str(payload["tts_provider"]).strip() if payload.get("tts_provider") else None,
                        tts_model=str(payload["tts_model"]).strip() if payload.get("tts_model") else None,
                        requires_review=parsed_requires_review,
                        voice_clone_enabled=parsed_voice_clone,
                        voice_strategy=str(payload["voice_strategy"]).strip() if payload.get("voice_strategy") else None,
                        plan_code_snapshot=str(payload["plan_code_snapshot"]).strip() if payload.get("plan_code_snapshot") else None,
                        role_snapshot=str(payload["role_snapshot"]).strip() if payload.get("role_snapshot") else None,
                        source_duration_seconds=parsed_duration,
                        estimated_duration_seconds=parsed_est_duration,
                        quota_cost=parsed_quota,
                        quota_state=str(payload.get("quota_state") or "none").strip(),
                        create_idempotency_key=str(payload["create_idempotency_key"]).strip() if payload.get("create_idempotency_key") else None,
                        user_id=str(payload["user_id"]).strip() if payload.get("user_id") else None,
                        source_content_hash=str(payload["source_content_hash"]).strip() if payload.get("source_content_hash") else None,
                    )
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "continue":
                    job = service.continue_job(path_parts[1])
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except JobNotFoundError as exc:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except UnsupportedJobRequestError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflictError as exc:
                self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if len(path_parts) == 2 and path_parts[0] == "jobs":
                    deleted = service.cancel_and_delete_job(path_parts[1])
                    if deleted:
                        self._write_json(HTTPStatus.OK, {"deleted": True, "job_id": path_parts[1]})
                    else:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": f"Job not found: {path_parts[1]}"})
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

        def _read_json_payload(self) -> dict[str, object]:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                return {}
            body = self.rfile.read(content_length)
            if not body:
                return {}
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            return payload

        def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(serialized_payload)))
            self.end_headers()
            self.wfile.write(serialized_payload)

    return JobAPIHandler
