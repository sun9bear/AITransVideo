"""Personal voice library API — per-user voice CRUD + internal expire endpoint."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_auth
from database import get_db
from models import User
from user_voice_service import (
    add_user_voice,
    delete_user_voice,
    list_user_voices,
    mark_voice_expired,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway", tags=["user-voices"])
internal_router = APIRouter(prefix="/internal", tags=["user-voices-internal"])


def _voice_to_dict(v) -> dict:
    return {
        "id": str(v.id),
        "voice_id": v.voice_id,
        "voice_type": v.voice_type,
        "provider": v.provider,
        "tts_provider": v.tts_provider,
        "platform": v.platform,
        "label": v.label,
        "source_speaker_id": v.source_speaker_id,
        "notes": v.notes,
        "expired_at": v.expired_at.isoformat() if v.expired_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.get("/user-voices")
async def get_user_voices(
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    voices = await list_user_voices(db, user.id)
    return _json(200, {"voices": [_voice_to_dict(v) for v in voices]})


@router.post("/user-voices")
async def create_user_voice(
    request: Request,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    body = await _read_body(request)
    voice_id = str(body.get("voice_id", "")).strip()
    if not voice_id:
        return _json(400, {"error": "voice_id is required"})
    voice = await add_user_voice(
        db,
        user_id=user.id,
        voice_id=voice_id,
        label=str(body.get("label", voice_id)),
        provider=str(body.get("provider", "minimax_voice_clone")),
        tts_provider=body.get("tts_provider", "minimax_tts"),
        platform=body.get("platform", "minimax_domestic"),
        source_speaker_id=body.get("source_speaker_id"),
        notes=body.get("notes"),
    )
    return _json(200, {"ok": True, "voice": _voice_to_dict(voice)})


@router.delete("/user-voices/{voice_id}")
async def remove_user_voice(
    voice_id: str,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if user is None:
        return _json(401, {"error": "unauthorized"})
    deleted = await delete_user_voice(db, user.id, voice_id)
    return _json(200, {"ok": True, "deleted": deleted})


@internal_router.post("/user-voices/expire")
async def internal_expire_voice(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    body = await _read_body(request)
    user_id = body.get("user_id")
    job_id = body.get("job_id")
    voice_id = str(body.get("voice_id", "")).strip()
    if not voice_id:
        return _json(400, {"error": "voice_id required"})

    # Resolve user_id from job_id if not provided
    if not user_id and job_id:
        from sqlalchemy import select
        from models import Job
        result = await db.execute(
            select(Job.user_id).where(Job.job_id == str(job_id))
        )
        row = result.scalar_one_or_none()
        if row is not None:
            user_id = row

    if not user_id:
        return _json(400, {"error": "无法确定用户"})

    expired = await mark_voice_expired(db, user_id, voice_id)
    return _json(200, {"ok": True, "expired": expired})


async def _read_body(request: Request) -> dict:
    raw = await request.body()
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _json(status: int, body: dict) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status,
        headers={"content-type": "application/json"},
    )
