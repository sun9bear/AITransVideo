"""Jianying draft backend: integration layer wrapping J2 (writer) + J3 (validator) (Task J4).

Exposes a single write(request) -> JianyingDraftResult interface. Catches known
exception types from the writer and translates them to structured validation_status
values. Always writes a compatibility report — even on skip/fail — so the manifest
layer (J7) can register the report artifact regardless of outcome.

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.1 (J4)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from modules.output.jianying.jianying_draft_models import (
    JianyingDraftRequest,
    JianyingDraftResult,
)
from modules.output.jianying.jianying_draft_writer import (
    JianyingDraftWriter,
    JianyingEngineUnavailable,
)
from modules.output.jianying.jianying_draft_validator import write_compatibility_report

logger = logging.getLogger(__name__)

# Schema version for the minimal skip/fail report (mirrors J3's schema)
_REPORT_SCHEMA_VERSION = "jianying_compatibility_report_v1"


class JianyingDraftBackend:
    """Bridges OutputDispatcher and the jianying writer/validator chain.

    Catches engine-availability and input-validation errors, translating
    them into structured JianyingDraftResult.validation_status values
    rather than letting exceptions propagate up to the dispatcher.

    The dispatcher (J6) is responsible for top-level gates
    (include_jianying_draft, service_mode == "studio"); this backend
    only runs when called.
    """

    def __init__(
        self,
        *,
        writer: JianyingDraftWriter | None = None,
        engine_name: str = "pyJianYingDraft",
    ) -> None:
        self._writer = writer or JianyingDraftWriter()
        self._engine_name = engine_name

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def write(self, request: JianyingDraftRequest) -> JianyingDraftResult:
        """Generate Jianying draft + compatibility report.

        Returns a JianyingDraftResult with:
        - validation_status == "ok"                 on success
        - validation_status == "skipped_no_engine"  if pyJianYingDraft is absent
        - validation_status == "skipped_missing_input" if subtitle file is missing
        - validation_status == "failed"             on any other exception

        compatibility_report_path is always set to a real path (file written
        even on skip/fail), so the manifest layer can register it unconditionally.
        All other path fields are empty strings on skip/fail.
        """
        validation_status: str = "ok"
        writer_result: JianyingDraftResult | None = None
        skip_issue_code: str | None = None
        skip_issue_message: str | None = None
        exc_for_log: Exception | None = None

        # --- 1. Attempt write ---
        try:
            writer_result = self._writer.write(request)
        except JianyingEngineUnavailable as exc:
            validation_status = "skipped_no_engine"
            skip_issue_code = "skipped_no_engine"
            skip_issue_message = str(exc)
        except FileNotFoundError as exc:
            validation_status = "skipped_missing_input"
            skip_issue_code = "skipped_missing_input"
            skip_issue_message = str(exc)
        except Exception as exc:  # noqa: BLE001
            validation_status = "failed"
            skip_issue_code = "writer_exception"
            skip_issue_message = f"{type(exc).__name__}: {exc}"
            exc_for_log = exc

        if exc_for_log is not None:
            logger.error(
                "JianyingDraftBackend: writer raised %s: %s",
                type(exc_for_log).__name__,
                exc_for_log,
            )

        # --- 2. Write compatibility report ---
        if validation_status == "ok" and writer_result is not None:
            report_path = self._write_ok_report(request, writer_result)
            return JianyingDraftResult(
                draft_dir=writer_result.draft_dir,
                draft_zip_path=writer_result.draft_zip_path,
                draft_content_path=writer_result.draft_content_path,
                draft_meta_info_path=writer_result.draft_meta_info_path,
                manifest_path=writer_result.manifest_path,
                compatibility_report_path=str(report_path),
                validation_status="ok",
            )
        else:
            report_path = self._write_skip_report(
                request,
                validation_status=validation_status,
                issue_code=skip_issue_code or "unknown",
                issue_message=skip_issue_message or "",
            )
            return JianyingDraftResult(
                draft_dir="",
                draft_zip_path="",
                draft_content_path="",
                draft_meta_info_path="",
                manifest_path=None,
                compatibility_report_path=str(report_path),
                validation_status=validation_status,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _jianying_dir(self, request: JianyingDraftRequest) -> Path:
        """Return {output_dir}/jianying/, creating it if needed."""
        jianying_dir = Path(request.output_dir) / "jianying"
        jianying_dir.mkdir(parents=True, exist_ok=True)
        return jianying_dir

    def _write_ok_report(
        self,
        request: JianyingDraftRequest,
        writer_result: JianyingDraftResult,
    ) -> Path:
        """Delegate to J3's write_compatibility_report for a successful draft."""
        jianying_dir = self._jianying_dir(request)
        return write_compatibility_report(
            request=request,
            draft_dir=Path(writer_result.draft_dir),
            draft_zip_path=Path(writer_result.draft_zip_path),
            output_root=jianying_dir,
            engine_name=self._engine_name,
        )

    def _write_skip_report(
        self,
        request: JianyingDraftRequest,
        *,
        validation_status: str,
        issue_code: str,
        issue_message: str,
    ) -> Path:
        """Write a minimal compatibility report for skip/fail paths.

        J3 inspects real draft files; here no draft exists, so the backend
        builds its own minimal report directly (no J3 call).
        """
        jianying_dir = self._jianying_dir(request)
        report_path = jianying_dir / "jianying_compatibility_report.json"

        report = {
            "schema_version": _REPORT_SCHEMA_VERSION,
            "project_id": request.project_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": {
                "name": self._engine_name,
                "version": None,
            },
            "draft": {},
            "materials": [],
            "tracks": [],
            "draft_zip_path": "",
            "draft_zip_size_bytes": 0,
            "validation_status": validation_status,
            "issues": [
                {
                    "code": issue_code,
                    "message": issue_message,
                }
            ],
        }

        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "wrote skip/fail compatibility report: %s (status=%s, issue=%s)",
            report_path,
            validation_status,
            issue_code,
        )
        return report_path
