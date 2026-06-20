"""Admin + internal API for the dynamic voice catalog.

Phase 1: read-only endpoints (list, detail).
Phase 2: write endpoints (CRUD, verify, import).
Phase 3: internal endpoint for app runtime (no auth, localhost only).
Phase 4 will add labeling endpoints.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from csrf import require_same_origin_state_change
from database import get_db
from internal_auth import internal_headers as _internal_headers
from models import User
from voice_catalog_models import VoiceCatalog, VoiceLabel
import label_task_queue
from voice_catalog_service import (
    VerifySkipped,
    create_voice,
    generate_final_label,
    get_label_status,
    import_voices,
    parse_import_lines,
    run_verify,
    soft_delete_voice,
    update_voice,
    write_label,
    write_labels_batch,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/voices",
    tags=["voice-catalog"],
    dependencies=[Depends(require_same_origin_state_change)],
)


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if (getattr(user, "role", None) or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# JSONB helper: check if verify_status has at least one verified=true dimension.
#
# verify_status looks like:
#   {"default": {"verified": true, ...}}
# or {"international": {"verified": true}, "mainland": {"verified": false}}
#
# PostgreSQL expression: check that any top-level value object has verified=true.
# We use a lateral jsonb_each to expand top-level keys, then check the "verified"
# sub-key.  This is wrapped in a SQL expression for use in WHERE clauses.
# ---------------------------------------------------------------------------

_VERIFIED_TRUE_SQL = text("""
    EXISTS (
        SELECT 1 FROM jsonb_each(voice_catalog.verify_status) AS kv(k, v)
        WHERE (v ->> 'verified')::boolean = true
    )
""")

_VERIFIED_FALSE_SQL = text("""
    NOT EXISTS (
        SELECT 1 FROM jsonb_each(voice_catalog.verify_status) AS kv(k, v)
        WHERE (v ->> 'verified')::boolean = true
    )
""")


# ---------------------------------------------------------------------------
# Phase 3: Internal endpoint for app runtime (loopback + shared-secret only)
# ---------------------------------------------------------------------------

internal_router = APIRouter(prefix="/api/internal", tags=["voice-catalog-internal"])


async def _require_internal_access(request: Request) -> None:
    """Unconditional internal-endpoint guard (T4).

    Token is read from ``settings`` at request time (not module-import time),
    so monkeypatch works in tests. Header name matches existing convention
    (see voice_catalog_api.py ``_internal_headers()`` and
    src/services/jobs/api.py): ``X-Internal-Key``.

    The localhost check is a secondary safety net — the primary defense is
    the Caddy-level block at ``Caddyfile @internal_block``. In production,
    Caddy ensures public requests never reach here. If Caddy is bypassed
    (direct gateway port access), this IP check still rejects non-loopback
    clients.
    """
    # Import inside the function so monkeypatching ``settings.internal_api_key``
    # in tests works regardless of import order.
    from config import settings as _settings

    # Primary: token must be configured AND match. No soft judgment.
    key = _settings.internal_api_key
    if not key:
        # Defense in depth: if startup check didn't run (e.g. import-only),
        # refuse rather than fail-open.
        raise HTTPException(status_code=503, detail="Internal endpoint misconfigured")
    provided = request.headers.get("X-Internal-Key", "")
    if provided != key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Key")
    # Secondary: reject non-loopback source (belt-and-suspenders vs. Caddy block).
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Non-loopback client not allowed")

# Default voices per resource — constants, never change
_DEFAULT_VOICES = {
    "seed-tts-1.0": "zh_female_shuangkuaisisi_moon_bigtts",
    "seed-tts-2.0": "zh_female_shuangkuaisisi_uranus_bigtts",
    "cosyvoice": "longanyang",
}

# Label priority for demographic fields: final > text
# For profile fields (pitch, warmth, etc.): final > audio_round3 > audio_round2 > audio_round1
_LABEL_PRIORITY = ("final", "text")
_PROFILE_PRIORITY = ("final", "audio_round3", "audio_round2", "audio_round1")


@internal_router.get("/voice-catalog", dependencies=[Depends(_require_internal_access)])
async def internal_voice_catalog(
    provider: str = Query(..., description="Provider name"),
    resource_id: str | None = Query(None, description="Resource ID (e.g. seed-tts-1.0). Optional for CosyVoice."),
    endpoint_mode: str | None = Query(None, description="Endpoint mode filter (international/mainland). CosyVoice only."),
    target_language: str | None = Query(None, description="Dub target language (e.g. zh-CN / en). Filters by compatible_target_languages when the kill switch is on. PR-E."),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return matchable + verified voices for app runtime.

    Protected by ``_require_internal_access``:
      - X-Internal-Key header must match ``settings.internal_api_key``
      - client must be loopback (127.0.0.1 / ::1)
      - Caddy blocks ``/api/internal/*`` from public ingress (primary defense)
    Returns voices with their best available demographic labels (final > text).
    """
    # Query matchable, non-archived, verified voices
    query = (
        select(VoiceCatalog)
        .where(VoiceCatalog.provider == provider)
        .where(VoiceCatalog.matchable == True)  # noqa: E712
        .where(VoiceCatalog.archived_at.is_(None))
        .where(_VERIFIED_TRUE_SQL)
        .order_by(VoiceCatalog.voice_id)
    )
    if resource_id:
        query = query.where(VoiceCatalog.provider_config.op("@>")({"resource_id": resource_id}))
    if endpoint_mode:
        # Filter CosyVoice by endpoint_modes array containing this mode
        query = query.where(VoiceCatalog.provider_config.op("@>")({"endpoint_modes": [endpoint_mode]}))
    # PR-E matchable migration (kill switch): when enabled, also require the voice to
    # declare the requested dub target language, so a zh dub never returns en voices
    # (and vice versa). Default OFF → legacy matchable-only query (byte-identical).
    # The kill switch gates THIS legacy query directly per plan Phase 5 (B).
    if target_language:
        from admin_settings import load_settings as _load_admin_settings

        if getattr(
            _load_admin_settings(), "voice_catalog_target_language_filter_enabled", False
        ):
            query = query.where(
                VoiceCatalog.compatible_target_languages.op("@>")([target_language])
            )
    result = await db.execute(query)
    voices = result.scalars().all()

    # Batch-fetch current labels for all matched voices
    voice_ids = [v.voice_id for v in voices]
    all_relevant_types = set(_LABEL_PRIORITY) | set(_PROFILE_PRIORITY)
    labels_by_voice: dict[str, dict[str, VoiceLabel]] = {}
    if voice_ids:
        label_result = await db.execute(
            select(VoiceLabel)
            .where(VoiceLabel.voice_id.in_(voice_ids))
            .where(VoiceLabel.is_current == True)  # noqa: E712
            .where(VoiceLabel.label_type.in_(list(all_relevant_types)))
        )
        for lbl in label_result.scalars().all():
            labels_by_voice.setdefault(lbl.voice_id, {})[lbl.label_type] = lbl

    # Build response
    out_voices = []
    for v in voices:
        voice_labels = labels_by_voice.get(v.voice_id, {})

        # Demographic fields: field-level fallback final > text
        demo_labels = [voice_labels.get(lt) for lt in _LABEL_PRIORITY if lt in voice_labels]
        def _demo_field(field: str):
            for lbl in demo_labels:
                val = getattr(lbl, field, None)
                if val is not None:
                    return val
            return None

        # Profile fields: final > audio_round3 > audio_round2 > audio_round1
        profile_label = None
        for lt in _PROFILE_PRIORITY:
            if lt in voice_labels:
                profile_label = voice_labels[lt]
                break

        out_voices.append({
            "voice_id": v.voice_id,
            "display_name": v.display_name,
            "gender": v.gender,
            "age_group": _demo_field("age_group"),
            "persona_style": _demo_field("persona_style"),
            "energy_level": _demo_field("energy_level"),
            "resource_id": resource_id or (v.provider_config or {}).get("resource_id"),
            "endpoint_modes": (v.provider_config or {}).get("endpoint_modes"),
            "scene": v.scene,
            "language": v.language,
            "matchable": True,
            # Profile fields for reranking
            "pitch_level": profile_label.pitch_level if profile_label else None,
            "warmth": profile_label.warmth if profile_label else None,
            "maturity": profile_label.maturity if profile_label else None,
            "childlike": profile_label.childlike if profile_label else None,
            "texture_tags": profile_label.texture_tags if profile_label else None,
            "delivery_style": profile_label.delivery_style if profile_label else None,
            # Speed calibration (migration 012). NULL when not yet calibrated —
            # runtime treats NULL as "fall back to probe".
            "chars_per_second": v.chars_per_second,
            "chars_per_second_by_model": v.chars_per_second_by_model,
            "speed_calibrated_at": (
                v.speed_calibrated_at.isoformat() if v.speed_calibrated_at else None
            ),
        })

    return {
        "voices": out_voices,
        "default_voice_id": _DEFAULT_VOICES.get(resource_id or provider, _DEFAULT_VOICES.get(provider, "longanyang")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Phase 1-2: Admin endpoints (auth required)
# ---------------------------------------------------------------------------

@router.get("")
async def list_voices(
    provider: str | None = Query(None, description="Filter by provider (e.g. volcengine, cosyvoice)"),
    resource_id: str | None = Query(None, description="Filter by provider_config.resource_id (e.g. seed-tts-1.0, seed-tts-2.0)"),
    gender: str | None = Query(None, description="Filter by gender"),
    verified: bool | None = Query(None, description="Filter by verified status"),
    matchable: bool | None = Query(None, description="Filter by matchable"),
    label_filter: str | None = Query(None, description="Filter by label progress: text|audio_round1|audio_round2|audio_round3|final|none"),
    archived: bool = Query(False, description="Include archived voices"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List voices with pagination and filtering.

    All filters (including ``verified`` and ``resource_id``) are applied
    in SQL before count and pagination, so ``total`` is always consistent
    with ``items``.
    """
    _require_admin(user)

    query = select(VoiceCatalog)

    if not archived:
        query = query.where(VoiceCatalog.archived_at.is_(None))
    if provider:
        query = query.where(VoiceCatalog.provider == provider)
    if resource_id:
        # JSONB containment: provider_config @> '{"resource_id": "seed-tts-1.0"}'
        query = query.where(
            VoiceCatalog.provider_config.op("@>")({"resource_id": resource_id})
        )
    if gender:
        query = query.where(VoiceCatalog.gender == gender)
    if matchable is not None:
        query = query.where(VoiceCatalog.matchable == matchable)
    if verified is True:
        query = query.where(_VERIFIED_TRUE_SQL)
    elif verified is False:
        query = query.where(_VERIFIED_FALSE_SQL)
    if label_filter:
        # Exact-stage filter: "仅文本标注" = has text but NOT R1, etc.
        _EXACT_STAGES: dict[str, tuple[list[str], str | None]] = {
            "text": (["text"], "audio_round1"),
            "audio_round1": (["text", "audio_round1"], "audio_round2"),
            "audio_round2": (["text", "audio_round1", "audio_round2"], "audio_round3"),
            "audio_round3": (["text", "audio_round1", "audio_round2", "audio_round3"], "final"),
            "final": (["final"], None),
        }
        if label_filter == "none":
            query = query.where(
                ~VoiceCatalog.voice_id.in_(
                    select(VoiceLabel.voice_id).where(VoiceLabel.is_current == True).distinct()  # noqa: E712
                )
            )
        elif label_filter in _EXACT_STAGES:
            must_have, must_not = _EXACT_STAGES[label_filter]
            for lt in must_have:
                query = query.where(
                    VoiceCatalog.voice_id.in_(
                        select(VoiceLabel.voice_id)
                        .where(VoiceLabel.is_current == True)  # noqa: E712
                        .where(VoiceLabel.label_type == lt)
                    )
                )
            if must_not:
                query = query.where(
                    ~VoiceCatalog.voice_id.in_(
                        select(VoiceLabel.voice_id)
                        .where(VoiceLabel.is_current == True)  # noqa: E712
                        .where(VoiceLabel.label_type == must_not)
                    )
                )

    # Count total (all filters already applied)
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination
    query = query.order_by(VoiceCatalog.provider, VoiceCatalog.voice_id)
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    voices = result.scalars().all()

    # For each voice, get label summary + final label content
    voice_ids = [v.voice_id for v in voices]
    label_summary: dict[str, dict[str, bool]] = {}
    final_labels: dict[str, VoiceLabel] = {}
    if voice_ids:
        label_result = await db.execute(
            select(VoiceLabel)
            .where(VoiceLabel.voice_id.in_(voice_ids))
            .where(VoiceLabel.is_current == True)  # noqa: E712
        )
        for lbl in label_result.scalars().all():
            label_summary.setdefault(lbl.voice_id, {})[lbl.label_type] = True
            if lbl.label_type == "final":
                final_labels[lbl.voice_id] = lbl

    items = [_serialize_voice(v, label_summary.get(v.voice_id, {}), final_labels.get(v.voice_id)) for v in voices]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{voice_id}")
async def get_voice_detail(
    voice_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get voice detail with all label history."""
    _require_admin(user)

    result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == voice_id)
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        raise HTTPException(status_code=404, detail=f"音色 {voice_id} 不存在")

    # Get all labels (including superseded)
    labels_result = await db.execute(
        select(VoiceLabel)
        .where(VoiceLabel.voice_id == voice_id)
        .order_by(VoiceLabel.labeled_at.desc())
    )
    labels = labels_result.scalars().all()

    return {
        "voice": _serialize_voice(voice, {}),
        "labels": [_serialize_label(lbl) for lbl in labels],
    }


# ---------------------------------------------------------------------------
# Pydantic request models (Phase 2)
# ---------------------------------------------------------------------------

class CreateVoiceRequest(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=200)
    provider: str = Field(..., min_length=1, max_length=50)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    display_name: str = Field(..., min_length=1, max_length=200)
    gender: str | None = None
    language: str = "zh"
    scene: str | None = None
    matchable: bool = True
    notes: str | None = None


class UpdateVoiceRequest(BaseModel):
    display_name: str | None = None
    gender: str | None = Field(default=None)
    language: str | None = None
    scene: str | None = Field(default=None)
    matchable: bool | None = None
    provider_config: dict[str, Any] | None = None
    notes: str | None = Field(default=None)


class ImportRequest(BaseModel):
    text: str = Field(..., min_length=1, description="CSV/tab 文本")
    provider: str = Field(..., min_length=1, description="目标 provider")
    dry_run: bool = Field(default=True, description="True = 预览不写入")


class BatchVerifyRequest(BaseModel):
    voice_ids: list[str] = Field(..., min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Phase 2 write endpoints
#
# IMPORTANT: Static paths (/verify-batch, /import) MUST be registered before
# parameterised paths (/{voice_id}) so FastAPI doesn't match "verify-batch"
# as a voice_id.
# ---------------------------------------------------------------------------

@router.post("")
async def create_voice_endpoint(
    req: CreateVoiceRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new voice entry."""
    _require_admin(user)

    # Check for duplicate
    existing = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == req.voice_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"音色 {req.voice_id} 已存在")

    voice = await create_voice(db, req.model_dump())
    await db.commit()

    return {"voice": _serialize_voice(voice, {})}


@router.post("/verify-batch")
async def verify_batch_endpoint(
    req: BatchVerifyRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Batch verify multiple voices."""
    _require_admin(user)

    results: list[dict[str, Any]] = []
    for vid in req.voice_ids:
        r = await db.execute(
            select(VoiceCatalog).where(VoiceCatalog.voice_id == vid)
        )
        voice = r.scalar_one_or_none()
        if voice is None:
            results.append({"voice_id": vid, "error": "不存在"})
            continue
        try:
            vr = await run_verify(db, voice)
            results.append({"voice_id": vid, "verify_status": vr})
        except VerifySkipped as exc:
            results.append({"voice_id": vid, "skipped": True, "error": str(exc)})
        except Exception as exc:
            results.append({"voice_id": vid, "error": str(exc)[:200]})

    await db.commit()
    return {"results": results}


@router.post("/import")
async def import_voices_endpoint(
    req: ImportRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Batch import voices from CSV/tab text.

    With dry_run=True: returns preview (parsed entries + skip info).
    With dry_run=False: actually inserts into DB.
    """
    _require_admin(user)

    entries = parse_import_lines(req.text, req.provider)
    if not entries:
        raise HTTPException(status_code=400, detail="未解析到有效音色数据")

    if req.dry_run:
        # Preview: check which already exist
        existing_ids: set[str] = set()
        for entry in entries:
            r = await db.execute(
                select(VoiceCatalog.voice_id).where(
                    VoiceCatalog.voice_id == entry["voice_id"]
                )
            )
            if r.scalar_one_or_none() is not None:
                existing_ids.add(entry["voice_id"])

        preview = []
        for entry in entries:
            preview.append({
                **entry,
                "status": "skip_duplicate" if entry["voice_id"] in existing_ids else "will_create",
            })
        return {"dry_run": True, "entries": preview, "total": len(entries)}

    # Actual import
    summary = await import_voices(db, entries)
    await db.commit()
    return {"dry_run": False, **summary}


# Phase 4: static label routes (before parameterised /{voice_id})

@router.post("/label/batch-text")
async def batch_write_text_labels(
    req: BatchLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Batch write text labels (max 50 per call)."""
    _require_admin(user)
    if len(req.labels) > 50:
        raise HTTPException(status_code=400, detail="单次最多 50 条")
    entries = [lbl.model_dump() for lbl in req.labels]
    result = await write_labels_batch(db, entries, "text", req.labeled_by, req.source_run_id)
    await db.commit()
    return result


@router.post("/label/batch-audio")
async def batch_write_audio_labels(
    req: BatchLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Batch write audio profiling labels (max 50 per call)."""
    _require_admin(user)
    if req.label_type not in ("audio_round1", "audio_round2", "audio_round3"):
        raise HTTPException(status_code=400, detail=f"Invalid label_type: {req.label_type}")
    if len(req.labels) > 50:
        raise HTTPException(status_code=400, detail="单次最多 50 条")
    entries = [lbl.model_dump() for lbl in req.labels]
    result = await write_labels_batch(db, entries, req.label_type, req.labeled_by, req.source_run_id)
    await db.commit()
    return result


@router.get("/label/status")
async def label_status_overview(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get labeling progress overview."""
    _require_admin(user)
    return await get_label_status(db)


class BatchFinalizeRequest(BaseModel):
    voice_ids: list[str] = Field(..., min_length=1, max_length=50)


@router.post("/label/batch-finalize")
async def batch_finalize_labels(
    req: BatchFinalizeRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Batch generate final labels for multiple voices."""
    _require_admin(user)
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    for vid in req.voice_ids:
        try:
            merged = await generate_final_label(db, vid)
            if merged:
                succeeded.append(vid)
            else:
                failed.append({"voice_id": vid, "error": "没有可用标签"})
        except Exception as exc:
            failed.append({"voice_id": vid, "error": str(exc)[:200]})
    await db.commit()
    return {"succeeded": succeeded, "failed": failed}


# Phase 4 v3: async task queue — DB-backed submit + poll

import asyncio


class SubmitTaskRequest(BaseModel):
    voice_ids: list[str] = Field(..., min_length=1)
    task_type: str = Field(..., description="trigger-text | trigger-audio")
    round_name: str | None = Field(None, description="round1|round2|round3 for audio")


@router.post("/label/tasks")
async def submit_label_task(
    req: SubmitTaskRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Submit an async labeling task. Returns task_id for polling."""
    _require_admin(user)

    if req.task_type == "trigger-text":
        label_type = "text"
    elif req.task_type == "trigger-audio" and req.round_name:
        lt = f"audio_{req.round_name}" if not req.round_name.startswith("audio_") else req.round_name
        if lt not in ("audio_round1", "audio_round2", "audio_round3"):
            raise HTTPException(status_code=400, detail=f"Invalid round: {req.round_name}")
        label_type = lt
    else:
        raise HTTPException(status_code=400, detail="Invalid task_type or missing round_name")

    voices = await _validate_labelable_voices(db, req.voice_ids)
    metadata = _voices_to_metadata(voices)

    task_id = await label_task_queue.create_task(db, req.task_type, req.voice_ids, label_type)
    await db.commit()

    # Launch background execution
    asyncio.create_task(_run_label_task_bg(task_id, metadata, label_type, req))

    return {"task_id": task_id, "status": "pending", "total": len(req.voice_ids)}


@router.get("/label/tasks")
async def list_label_tasks_endpoint(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List recent labeling tasks."""
    _require_admin(user)
    return {"tasks": await label_task_queue.list_tasks(db)}


@router.get("/label/tasks/{task_id}")
async def get_label_task(
    task_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll task progress."""
    _require_admin(user)
    task = await label_task_queue.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _run_label_task_bg(
    task_id: str,
    metadata: list[dict[str, Any]],
    label_type: str,
    req: SubmitTaskRequest,
) -> None:
    """Background coroutine: execute labeling in chunks, update progress in DB."""
    from database import async_session

    chunk_size = 10 if label_type.startswith("audio_") else 50
    chunks = [metadata[i:i + chunk_size] for i in range(0, len(metadata), chunk_size)]

    all_written: list[str] = []
    all_errors: list[dict[str, str]] = []

    try:
        for ci, chunk in enumerate(chunks):
            # Update progress in DB
            async with async_session() as db:
                await label_task_queue.update_progress(db, task_id, len(all_written), ci + 1)

            try:
                if req.task_type == "trigger-text":
                    url = f"{settings.job_api_upstream}/internal/voice-label/text"
                else:
                    url = f"{settings.job_api_upstream}/internal/voice-label/audio/{req.round_name}"

                async with httpx.AsyncClient(timeout=600.0, headers=_internal_headers()) as client:
                    resp = await client.post(url, json={"voices": chunk})
                resp.raise_for_status()
                data = resp.json()

                if data.get("ok"):
                    labels = data.get("labels", [])
                    if labels:
                        source_run_id = f"task-{task_id}-batch{ci + 1}"
                        async with async_session() as db:
                            result = await write_labels_batch(
                                db, labels, label_type, "gemini-3.1-pro", source_run_id,
                            )
                            await db.commit()
                        all_written.extend(result.get("written", []))
                        all_errors.extend(result.get("errors", []))
                else:
                    all_errors.append({"batch": ci + 1, "error": data.get("error", "unknown")})
            except Exception as exc:
                all_errors.append({"batch": ci + 1, "error": str(exc)[:200]})

        async with async_session() as db:
            await label_task_queue.complete_task(db, task_id, {
                "written": all_written,
                "errors": all_errors,
                "total_batches": len(chunks),
            })
    except Exception as exc:
        async with async_session() as db:
            await label_task_queue.fail_task(db, task_id, str(exc)[:500])


# App internal API upstream: read from settings.job_api_upstream at call time
# (env var AVT_JOB_API_UPSTREAM). X-Internal-Key header comes from the shared
# gateway/internal_auth.py helper imported at the top of this module.


class TriggerLabelRequest(BaseModel):
    voice_ids: list[str] = Field(..., min_length=1, max_length=50)


_SUPPORTED_LABEL_PROVIDERS = {"volcengine", "cosyvoice"}


async def _validate_labelable_voices(
    db: AsyncSession,
    voice_ids: list[str],
) -> list[VoiceCatalog]:
    """Validate all voice_ids exist, have a supported provider, and are not archived.

    Returns the VoiceCatalog objects.  Raises HTTPException on any failure.
    """
    result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id.in_(voice_ids))
    )
    found = {v.voice_id: v for v in result.scalars().all()}

    missing = [vid for vid in voice_ids if vid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"音色不存在: {', '.join(missing[:5])}")

    unsupported = [vid for vid, v in found.items() if v.provider not in _SUPPORTED_LABEL_PROVIDERS]
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"仅支持 {'/'.join(sorted(_SUPPORTED_LABEL_PROVIDERS))}，以下不是: {', '.join(unsupported[:5])}",
        )

    archived = [vid for vid, v in found.items() if v.archived_at is not None]
    if archived:
        raise HTTPException(status_code=400, detail=f"音色已归档: {', '.join(archived[:5])}")

    return [found[vid] for vid in voice_ids]


def _voices_to_metadata(voices: list[VoiceCatalog]) -> list[dict[str, Any]]:
    """Convert VoiceCatalog rows to minimal metadata for app scripts."""
    return [
        {
            "voice_id": v.voice_id,
            "provider": v.provider,
            "display_name": v.display_name,
            "scene": v.scene or "",
            "language": v.language or "zh",
            "provider_config": v.provider_config or {},
        }
        for v in voices
    ]


@router.post("/label/trigger-text")
async def trigger_text_labeling(
    req: TriggerLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trigger text labeling via app, write results to DB."""
    _require_admin(user)

    voices = await _validate_labelable_voices(db, req.voice_ids)
    metadata = _voices_to_metadata(voices)

    try:
        async with httpx.AsyncClient(timeout=300.0, headers=_internal_headers()) as client:
            resp = await client.post(
                f"{settings.job_api_upstream}/internal/voice-label/text",
                json={"voices": metadata},
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="标注超时")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"app 调用失败: {exc}")

    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=data.get("error", "标注失败"))

    labels = data.get("labels", [])
    if not labels:
        raise HTTPException(status_code=502, detail="标注脚本未返回任何 labels")

    source_run_id = f"trigger-text-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    result = await write_labels_batch(
        db, labels, "text", "gemini-3.1-pro", source_run_id,
    )
    await db.commit()
    return {**result, "source_run_id": source_run_id}


@router.post("/label/trigger-audio/{round_name}")
async def trigger_audio_labeling(
    round_name: str,
    req: TriggerLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trigger audio profiling via app, write results to DB.

    Frontend handles chunking (10 per request) to avoid HTTP timeout.
    """
    _require_admin(user)
    label_type = f"audio_{round_name}" if not round_name.startswith("audio_") else round_name
    if label_type not in ("audio_round1", "audio_round2", "audio_round3"):
        raise HTTPException(status_code=400, detail=f"Invalid round: {round_name}")

    voices = await _validate_labelable_voices(db, req.voice_ids)
    metadata = _voices_to_metadata(voices)

    try:
        async with httpx.AsyncClient(timeout=600.0, headers=_internal_headers()) as client:
            resp = await client.post(
                f"{settings.job_api_upstream}/internal/voice-label/audio/{round_name}",
                json={"voices": metadata},
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="音频 profiling 超时")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"app 调用失败: {exc}")

    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=data.get("error", "profiling 失败"))

    labels = data.get("labels", [])
    if not labels:
        raise HTTPException(status_code=502, detail="profiling 脚本未返回任何 labels")

    source_run_id = f"trigger-{round_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    result = await write_labels_batch(
        db, labels, label_type, "gemini-3.1-pro", source_run_id,
    )
    await db.commit()
    return {**result, "source_run_id": source_run_id}


# Parameterised routes — must come AFTER static routes above

@router.patch("/{voice_id}")
async def update_voice_endpoint(
    voice_id: str,
    req: UpdateVoiceRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update voice metadata (partial update)."""
    _require_admin(user)

    result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == voice_id)
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        raise HTTPException(status_code=404, detail=f"音色 {voice_id} 不存在")

    # Only pass non-None fields
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="没有要更新的字段")

    voice = await update_voice(db, voice, data)
    await db.commit()

    return {"voice": _serialize_voice(voice, {})}


@router.delete("/{voice_id}")
async def delete_voice_endpoint(
    voice_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Soft-delete a voice (set archived_at)."""
    _require_admin(user)

    result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == voice_id)
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        raise HTTPException(status_code=404, detail=f"音色 {voice_id} 不存在")
    if voice.archived_at is not None:
        raise HTTPException(status_code=400, detail=f"音色 {voice_id} 已归档")

    await soft_delete_voice(db, voice)
    await db.commit()

    return {"voice_id": voice_id, "archived": True}


@router.post("/{voice_id}/verify")
async def verify_voice_endpoint(
    voice_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Verify a single voice by calling its TTS provider."""
    _require_admin(user)

    result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == voice_id)
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        raise HTTPException(status_code=404, detail=f"音色 {voice_id} 不存在")

    try:
        verify_result = await run_verify(db, voice)
    except VerifySkipped as exc:
        # Don't overwrite DB — return error without committing
        raise HTTPException(status_code=400, detail=str(exc))

    await db.commit()

    return {
        "voice_id": voice_id,
        "verify_status": verify_result,
        "voice": _serialize_voice(voice, {}),
    }


# ---------------------------------------------------------------------------
# Phase 4: Labeling endpoints
# ---------------------------------------------------------------------------

class WriteLabelRequest(BaseModel):
    voice_id: str
    age_group: str | None = None
    persona_style: str | None = None
    energy_level: str | None = None
    pitch_level: str | None = None
    warmth: str | None = None
    authority: str | None = None
    intimacy: str | None = None
    brightness: str | None = None
    maturity: str | None = None
    delivery_style: str | None = None
    texture_tags: list[str] | None = None
    childlike: bool | None = None


class BatchLabelRequest(BaseModel):
    labels: list[WriteLabelRequest]
    label_type: str = Field(..., description="text | audio_round1 | audio_round2 | audio_round3")
    labeled_by: str = Field(default="gemini-3.1-pro")
    source_run_id: str | None = None


@router.post("/{voice_id}/label/text")
async def write_text_label(
    voice_id: str,
    req: WriteLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Write a text label for a single voice."""
    _require_admin(user)
    try:
        label = await write_label(db, voice_id, "text", req.model_dump(exclude={"voice_id"}),
                                   labeled_by="manual", source_run_id=None)
        await db.commit()
        return {"voice_id": voice_id, "label_type": "text", "ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{voice_id}/label/audio/{round_name}")
async def write_audio_label(
    voice_id: str,
    round_name: str,
    req: WriteLabelRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Write an audio profiling label for a single voice."""
    _require_admin(user)
    label_type = f"audio_{round_name}" if not round_name.startswith("audio_") else round_name
    if label_type not in ("audio_round1", "audio_round2", "audio_round3"):
        raise HTTPException(status_code=400, detail=f"Invalid round: {round_name}")
    try:
        await write_label(db, voice_id, label_type, req.model_dump(exclude={"voice_id"}),
                          labeled_by="gemini-3.1-pro", source_run_id=None)
        await db.commit()
        return {"voice_id": voice_id, "label_type": label_type, "ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{voice_id}/label/finalize")
async def finalize_label(
    voice_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Generate a final label by merging available labels."""
    _require_admin(user)
    try:
        merged = await generate_final_label(db, voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if merged is None:
        raise HTTPException(status_code=400, detail=f"音色 {voice_id} 没有可用标签，无法生成 final")
    await db.commit()
    return {"voice_id": voice_id, "label_type": "final", "merged": merged, "ok": True}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_voice(
    voice: VoiceCatalog,
    label_summary: dict[str, bool],
    final_label: VoiceLabel | None = None,
) -> dict[str, Any]:
    vs = voice.verify_status or {}
    is_verified = any(dim.get("verified") is True for dim in vs.values()) if vs else False
    is_seed = voice.source == "seed_migration"

    result = {
        "voice_id": voice.voice_id,
        "provider": voice.provider,
        "provider_config": voice.provider_config,
        "display_name": voice.display_name,
        "gender": voice.gender,
        "language": voice.language,
        "scene": voice.scene,
        "matchable": voice.matchable,
        "verify_status": voice.verify_status,
        "is_verified": is_verified,
        "is_seed": is_seed,
        "verify_attempts": voice.verify_attempts,
        "source": voice.source,
        "archived_at": voice.archived_at.isoformat() if voice.archived_at else None,
        "notes": voice.notes,
        "created_at": voice.created_at.isoformat() if voice.created_at else None,
        "updated_at": voice.updated_at.isoformat() if voice.updated_at else None,
        # Speed calibration (migration 012)
        "chars_per_second": voice.chars_per_second,
        "chars_per_second_by_model": voice.chars_per_second_by_model,
        "speed_calibrated_at": (
            voice.speed_calibrated_at.isoformat() if voice.speed_calibrated_at else None
        ),
        "label_status": {
            "text": label_summary.get("text", False),
            "audio_round1": label_summary.get("audio_round1", False),
            "audio_round2": label_summary.get("audio_round2", False),
            "audio_round3": label_summary.get("audio_round3", False),
            "final": label_summary.get("final", False),
        },
        "final_label": None,
    }

    if final_label:
        result["final_label"] = {
            "age_group": final_label.age_group,
            "persona_style": final_label.persona_style,
            "energy_level": final_label.energy_level,
            "pitch_level": final_label.pitch_level,
            "warmth": final_label.warmth,
            "maturity": final_label.maturity,
            "delivery_style": final_label.delivery_style,
            "texture_tags": final_label.texture_tags,
            "childlike": final_label.childlike,
        }

    return result


def _serialize_label(label: VoiceLabel) -> dict[str, Any]:
    return {
        "id": label.id,
        "voice_id": label.voice_id,
        "label_type": label.label_type,
        "source_run_id": label.source_run_id,
        "is_current": label.is_current,
        "age_group": label.age_group,
        "persona_style": label.persona_style,
        "energy_level": label.energy_level,
        "pitch_level": label.pitch_level,
        "warmth": label.warmth,
        "authority": label.authority,
        "intimacy": label.intimacy,
        "brightness": label.brightness,
        "maturity": label.maturity,
        "delivery_style": label.delivery_style,
        "texture_tags": label.texture_tags,
        "childlike": label.childlike,
        "labeled_by": label.labeled_by,
        "labeled_at": label.labeled_at.isoformat() if label.labeled_at else None,
        "superseded_at": label.superseded_at.isoformat() if label.superseded_at else None,
    }
