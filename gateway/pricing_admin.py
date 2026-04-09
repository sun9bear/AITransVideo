"""Admin-only pricing CRUD API: draft, publish, history."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import async_session
from models import PricingConfigVersion, User
from pricing_schema import PricingPayload, build_default_pricing_payload, detect_frozen_field_changes
from pricing_runtime import write_runtime_snapshot, invalidate_runtime_pricing_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/pricing", tags=["admin-pricing"])


# ---------------------------------------------------------------------------
# Admin auth helpers (self-contained, matching admin_settings.py pattern)
# ---------------------------------------------------------------------------

def _is_admin(user: User) -> bool:
    return (getattr(user, "role", None) or "user") == "admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DraftRequest(BaseModel):
    payload: dict


class PublishRequest(BaseModel):
    payload: dict
    change_note: str | None = None


def _version_to_dict(v: PricingConfigVersion) -> dict:
    return {
        "version": v.version,
        "status": v.status,
        "payload": v.payload_json,
        "change_note": v.change_note,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "activated_at": v.activated_at.isoformat() if v.activated_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def get_pricing(user: User | None = Depends(get_current_user)):
    """Return active pricing and latest draft (if any)."""
    _require_admin(user)

    async with async_session() as db:
        # Active version
        active_row = (
            await db.execute(
                select(PricingConfigVersion)
                .where(PricingConfigVersion.status == "active")
                .order_by(desc(PricingConfigVersion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        if active_row:
            active = _version_to_dict(active_row)
        else:
            # No active version in DB — return defaults (don't seed on read)
            default_payload = build_default_pricing_payload()
            active = {
                "version": 0,
                "status": "default",
                "payload": default_payload.model_dump(),
                "change_note": "系统默认配置",
                "created_at": None,
                "activated_at": None,
            }

        # Latest draft
        draft_row = (
            await db.execute(
                select(PricingConfigVersion)
                .where(PricingConfigVersion.status == "draft")
                .order_by(desc(PricingConfigVersion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        draft = _version_to_dict(draft_row) if draft_row else None

    return {"active": active, "draft": draft}


@router.post("/draft")
async def save_draft(body: DraftRequest, user: User | None = Depends(get_current_user)):
    """Save a draft pricing version."""
    _require_admin(user)

    # Validate payload
    try:
        payload = PricingPayload.model_validate(body.payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"定价配置校验失败: {e}")

    async with async_session() as db:
        # Next version number
        max_ver = (
            await db.execute(
                select(func.max(PricingConfigVersion.version))
            )
        ).scalar_one_or_none() or 0

        row = PricingConfigVersion(
            version=max_ver + 1,
            status="draft",
            payload_json=payload.model_dump(),
            change_note=None,
            updated_by_user_id=user.id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    return {"version": _version_to_dict(row)}


@router.post("/publish")
async def publish_pricing(body: PublishRequest, user: User | None = Depends(get_current_user)):
    """Publish a new active pricing version."""
    _require_admin(user)

    # Validate payload
    try:
        payload = PricingPayload.model_validate(body.payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"定价配置校验失败: {e}")

    async with async_session() as db:
        # Get current active payload for frozen field comparison
        active_row = (
            await db.execute(
                select(PricingConfigVersion)
                .where(PricingConfigVersion.status == "active")
                .order_by(desc(PricingConfigVersion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        if active_row:
            old_payload = PricingPayload.model_validate(active_row.payload_json)
        else:
            old_payload = build_default_pricing_payload()

        # Frozen field check
        frozen_changes = detect_frozen_field_changes(old_payload, payload)
        if frozen_changes and not (body.change_note or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"修改了冻结字段 ({', '.join(frozen_changes)})，必须填写变更说明",
            )

        now = datetime.now(timezone.utc)

        # Archive all current active rows
        await db.execute(
            update(PricingConfigVersion)
            .where(PricingConfigVersion.status == "active")
            .values(status="archived")
        )

        # Archive all current draft rows
        await db.execute(
            update(PricingConfigVersion)
            .where(PricingConfigVersion.status == "draft")
            .values(status="archived")
        )

        # Next version number
        max_ver = (
            await db.execute(
                select(func.max(PricingConfigVersion.version))
            )
        ).scalar_one_or_none() or 0

        row = PricingConfigVersion(
            version=max_ver + 1,
            status="active",
            payload_json=payload.model_dump(),
            change_note=body.change_note,
            updated_by_user_id=user.id,
            activated_at=now,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    # Write runtime snapshot and invalidate cache (outside DB transaction)
    try:
        write_runtime_snapshot(payload)
    except Exception:
        logger.exception("[pricing] Failed to write runtime snapshot after publish")
    invalidate_runtime_pricing_cache()

    return {"version": _version_to_dict(row)}


@router.get("/history")
async def get_pricing_history(user: User | None = Depends(get_current_user)):
    """Return version history (newest first, limit 50)."""
    _require_admin(user)

    async with async_session() as db:
        rows = (
            await db.execute(
                select(PricingConfigVersion)
                .order_by(desc(PricingConfigVersion.created_at))
                .limit(50)
            )
        ).scalars().all()

    versions = []
    for r in rows:
        versions.append({
            "version": r.version,
            "status": r.status,
            "change_note": r.change_note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "activated_at": r.activated_at.isoformat() if r.activated_at else None,
        })

    return {"versions": versions}
