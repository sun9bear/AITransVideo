"""Admin-only pricing CRUD API: draft, publish, history."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func, update
from sqlalchemy.exc import IntegrityError
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
    """Save a draft pricing version.

    Concurrency: ``pricing_config_versions.version`` is UNIQUE (alembic
    017 / P1-11c). The ``select(func.max(version)) + 1 → INSERT`` sequence
    here is intentionally NOT serialised by an advisory lock — instead
    we rely on the UNIQUE constraint as the correctness barrier. Two
    admins clicking "Save Draft" simultaneously both compute version=N+1
    and both try to INSERT; one succeeds, the other gets ``IntegrityError``
    on commit. We surface that to the client as HTTP 409 Conflict with
    a Chinese-language hint to refresh + retry.

    Audit ref: docs/audits/2026-05-07-comprehensive-codebase-audit.md
    P1-11c follow-up — IntegrityError → 409.
    """
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
        try:
            await db.commit()
        except IntegrityError:
            # Another admin won the race against UNIQUE(version).
            # The session is auto-rolled-back; surface a clear retry hint.
            await db.rollback()
            logger.info(
                "[pricing] save_draft concurrent UNIQUE conflict on version=%d "
                "(user=%s); returning 409 to caller.",
                max_ver + 1,
                user.id,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "版本号冲突，可能有其他管理员同时保存了定价草稿。"
                    "请刷新页面后重试。"
                ),
            )
        await db.refresh(row)

    return {"version": _version_to_dict(row)}


@router.post("/publish")
async def publish_pricing(body: PublishRequest, user: User | None = Depends(get_current_user)):
    """Publish a new active pricing version.

    Concurrency: same UNIQUE-on-version constraint as ``save_draft``
    above. Two admins simultaneously clicking "Publish" both archive
    the current active row (idempotent UPDATE) then both compute
    ``max+1=N+1`` and both INSERT — one succeeds, the other gets
    ``IntegrityError`` on commit and is surfaced as HTTP 409 Conflict.

    Note: even though publish does multiple statements
    (UPDATE active→archived, UPDATE draft→archived, INSERT new active),
    the entire sequence is one transaction. PostgreSQL rolls back the
    whole transaction when the INSERT trips UNIQUE — the archive
    UPDATEs do NOT partially commit. So the loser of the race observes
    no state change; the winner's transaction is the only one that
    lands.

    Audit ref: docs/audits/2026-05-07-comprehensive-codebase-audit.md
    P1-11c follow-up — IntegrityError → 409.
    """
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
        try:
            await db.commit()
        except IntegrityError:
            # Another admin's publish landed first against UNIQUE(version).
            # The whole transaction (including the archive UPDATEs) is
            # rolled back atomically; this caller observes no state change.
            await db.rollback()
            logger.info(
                "[pricing] publish concurrent UNIQUE conflict on version=%d "
                "(user=%s); returning 409 to caller.",
                max_ver + 1,
                user.id,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "版本号冲突，可能有其他管理员同时发布了定价。"
                    "请刷新页面查看最新状态后重试。"
                ),
            )
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
