"""User-facing notification center API.

Routes (authenticated only — anonymous users have no notifications):

- ``GET  /api/notifications`` — list with optional ?include_archived=true
- ``POST /api/notifications/read`` — mark ids or all-unread as read
- ``POST /api/notifications/archive`` — archive ids
- ``GET  /api/notifications/unread-count`` — small payload for the bell

Internal-only:

- ``POST /internal/notifications/dispatch`` — Job API / pipeline can post
  here to enqueue a notification without going through the SDK directly.
  Behind the same X-Internal-Key gate as voice-catalog internals.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_auth
from config import settings
from database import get_db
from models import User
from notifications_service import (
    archive as svc_archive,
    dispatch_event,
    list_for_user,
    mark_read as svc_mark_read,
    unread_count as svc_unread_count,
)
from support_models import (
    NotificationArchiveRequest,
    NotificationListResponse,
    NotificationMarkReadRequest,
    NotificationView,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/notifications", tags=["notifications"])
internal_router = APIRouter(prefix="/internal/notifications", tags=["notifications-internal"])


def _to_view(row) -> NotificationView:
    return NotificationView(
        id=str(row.id),
        scope=row.scope,
        topic=row.topic,
        title=row.title,
        body=row.body,
        severity=row.severity,
        job_id=row.job_id,
        related_type=row.related_type,
        related_id=row.related_id,
        artifact_key=row.artifact_key,
        action_url=row.action_url,
        read=row.read_at is not None,
        archived=row.archived_at is not None,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    include_archived: bool = False,
    limit: int = 50,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    rows = await list_for_user(
        db,
        user_id=user.id,
        include_archived=include_archived,
        limit=limit,
    )
    unread = await svc_unread_count(db, user_id=user.id)
    return NotificationListResponse(
        items=[_to_view(r) for r in rows],
        unread_count=unread,
    )


@router.get("/unread-count")
async def unread_count_endpoint(
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return {"unread_count": await svc_unread_count(db, user_id=user.id)}


@router.post("/read")
async def mark_read_endpoint(
    body: NotificationMarkReadRequest,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    n = await svc_mark_read(
        db,
        user_id=user.id,
        ids=body.ids,
        mark_all=body.mark_all,
    )
    await db.commit()
    return {"updated": n}


@router.post("/archive")
async def archive_endpoint(
    body: NotificationArchiveRequest,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    n = await svc_archive(db, user_id=user.id, ids=body.ids)
    await db.commit()
    return {"archived": n}


# ---------------------------------------------------------------------------
# Internal dispatch (called by the pipeline / Job API)
# ---------------------------------------------------------------------------


class InternalDispatchRequest(BaseModel):
    event_type: str
    user_id: str | None = None
    job_id: str | None = None
    payload: dict[str, Any] | None = None
    dedupe_key: str | None = None
    related_id: str | None = None


def _require_internal(x_internal_key: str | None) -> None:
    expected = (settings.internal_api_key or "").strip()
    received = (x_internal_key or "").strip()
    if not expected or expected != received:
        raise HTTPException(status_code=403, detail="invalid internal key")


@internal_router.post("/dispatch")
async def internal_dispatch(
    body: InternalDispatchRequest,
    db: AsyncSession = Depends(get_db),
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
) -> dict[str, Any]:
    _require_internal(x_internal_key)
    user_uuid = None
    if body.user_id:
        try:
            user_uuid = uuid.UUID(body.user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid user_id")
    notif = await dispatch_event(
        db,
        event_type=body.event_type,
        user_id=user_uuid,
        job_id=body.job_id,
        payload=body.payload,
        dedupe_key=body.dedupe_key,
        related_id=body.related_id,
    )
    if notif is None:
        await db.commit()
        return {"created": False}
    await db.commit()
    return {"created": True, "id": str(notif.id)}
