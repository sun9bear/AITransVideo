"""APF P0 — PostgreSQL-backed anonymous preview record store (AD-3 v2).

Writes / reads / updates rows in the ``anonymous_preview_records`` table
defined in gateway/models.py (migration 035).

Design constraints
------------------
* No import of ``services.jobs`` or any module under ``src.pipeline``.
  (gateway container does not have pydub; see CLAUDE.md import guard.)
* The ORM model ``AnonymousPreviewRecord`` lives in gateway.models so
  this store can access it without touching the Job-API codebase.
* The in/out contract uses ``src.services.anonymous_preview_intake.
  PreviewRecord`` (the pure dataclass) — this module converts between
  the dataclass and the ORM row so the adapter wiring layer is
  completely decoupled from SQLAlchemy.
* Any SQLAlchemy exception is caught and re-raised as
  ``RecordStoreError`` so callers get a single, predictable failure mode.
* ``adapter wiring`` calls ``save_record`` then ``update_status`` — the
  two-step pattern mirrors the "status-only record, then update on
  completion" lifecycle described in AD-3.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import AnonymousPreviewRecord

# src/ is on sys.path inside both the test harness and the gateway
# container (see docker-compose.yml bind mount).
from src.services.anonymous_preview_intake import (
    ComplianceStatus,
    PreviewRecord,
    PreviewStatus,
    SourceType,
)

__all__ = [
    "RecordStoreError",
    "PgPreviewRecordStore",
]

logger = logging.getLogger(__name__)


class RecordStoreError(Exception):
    """Raised on any storage-layer failure (DB unavailable, constraint
    violation, unexpected ORM exception).  Callers should treat this as
    fail-closed: the preview flow that triggered the write must not
    proceed as if the record was saved.
    """


# ---------------------------------------------------------------------------
# ORM ↔ dataclass conversion helpers (module-private)
# ---------------------------------------------------------------------------

def _to_orm(record: PreviewRecord) -> AnonymousPreviewRecord:
    """Convert a ``PreviewRecord`` dataclass to an ORM row.

    ``compliance_status`` is an Optional enum; store its ``.value``
    string or None.  ``audit`` carries ``compliance_audit_metadata``
    as a JSONB blob (dict must be JSON-serialisable — the intake module
    guarantees this).
    """
    compliance_status_str: Optional[str] = (
        record.compliance_status.value
        if record.compliance_status is not None
        else None
    )
    audit: Optional[dict] = (
        dict(record.compliance_audit_metadata)
        if record.compliance_audit_metadata
        else None
    )
    return AnonymousPreviewRecord(
        preview_id=record.record_id,
        session_id=record.session_id_hash,
        status=record.status.value,
        status_reason=record.status_reason[:256] if record.status_reason else None,
        source_type=record.source_type.value
        if isinstance(record.source_type, SourceType)
        else str(record.source_type),
        source_hash=record.source_hash or "",
        mode="free",
        job_id=None,
        claim_token_placeholder=record.claim_token_placeholder,
        audit=audit,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


def _from_orm(row: AnonymousPreviewRecord) -> PreviewRecord:
    """Convert an ORM row back to a ``PreviewRecord`` dataclass."""
    try:
        status = PreviewStatus(row.status)
    except ValueError:
        status = PreviewStatus.FAILED

    try:
        source_type = SourceType(row.source_type)
    except ValueError:
        source_type = SourceType.LOCAL_UPLOAD

    compliance_status: Optional[ComplianceStatus] = None
    if row.audit and "compliance_status" in row.audit:
        try:
            compliance_status = ComplianceStatus(row.audit["compliance_status"])
        except ValueError:
            pass

    audit_metadata: dict = row.audit or {}

    return PreviewRecord(
        record_id=row.preview_id,
        session_id_hash=row.session_id,
        source_hash=row.source_hash,
        upload_hash=row.source_hash,
        source_type=source_type,
        status=status,
        status_reason=row.status_reason or "",
        duration_seconds=0.0,
        audio_present=False,
        compliance_status=compliance_status,
        compliance_audit_metadata=audit_metadata,
        created_at=row.created_at,
        expires_at=row.expires_at,
        claim_token_placeholder=row.claim_token_placeholder,
    )


# ---------------------------------------------------------------------------
# Public store class
# ---------------------------------------------------------------------------

class PgPreviewRecordStore:
    """PostgreSQL-backed preview record store.

    Parameters
    ----------
    session:
        A SQLAlchemy ``Session`` already bound to the gateway DB.
        The caller is responsible for commit/rollback.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_record(self, record: PreviewRecord) -> None:
        """Insert a new ``AnonymousPreviewRecord`` row.

        Raises ``RecordStoreError`` on any DB or ORM failure.
        Callers must commit the session after a successful return.
        """
        try:
            orm_row = _to_orm(record)
            self._session.add(orm_row)
            self._session.flush()  # surface constraint violations early
        except Exception as exc:
            raise RecordStoreError(
                f"save_record failed for preview_id={record.record_id!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_record(self, preview_id: str) -> Optional[PreviewRecord]:
        """Return the ``PreviewRecord`` for ``preview_id``, or ``None``.

        Raises ``RecordStoreError`` on any DB failure.
        """
        try:
            row = self._session.get(AnonymousPreviewRecord, preview_id)
            if row is None:
                return None
            return _from_orm(row)
        except RecordStoreError:
            raise
        except Exception as exc:
            raise RecordStoreError(
                f"get_record failed for preview_id={preview_id!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_status(
        self,
        preview_id: str,
        status: PreviewStatus,
        *,
        status_reason: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> None:
        """Update ``status`` (and optionally ``status_reason`` / ``job_id``)
        on an existing row identified by ``preview_id``.

        Raises ``RecordStoreError`` if the row is not found or on any DB
        failure.
        """
        try:
            row = self._session.get(AnonymousPreviewRecord, preview_id)
            if row is None:
                raise RecordStoreError(
                    f"update_status: no row for preview_id={preview_id!r}"
                )
            row.status = status.value
            if status_reason is not None:
                row.status_reason = status_reason[:256]
            if job_id is not None:
                row.job_id = job_id
            self._session.flush()
        except RecordStoreError:
            raise
        except Exception as exc:
            raise RecordStoreError(
                f"update_status failed for preview_id={preview_id!r}"
            ) from exc
