"""Job API intercept layer — inject user_id, filter by ownership.

Gateway intercepts job-related requests to:
1. Inject user_id when creating a job
2. Filter job listings by user_id
3. Verify job ownership for single-job operations
4. Sync job metadata to PostgreSQL (dual-write)

The upstream Job API (8877) is the sole backend service.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import sys
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Make src/ importable for any future helpers that legitimately live in
# ``src/`` and don't drag the ``services.jobs`` package init's pydub chain.
# logs_redactor itself goes through ``log_redactor_loader`` below, which
# bypasses the package init entirely (gateway has no pydub).
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

# Gateway-local file-location loader for src/services/jobs/logs_redactor.py.
# Direct ``from services.jobs.logs_redactor import ...`` ImportError's in the
# gateway container because services.jobs.__init__ pulls pydub. The loader
# returns ``None`` on any failure → callers fail-open (verbatim). See
# ``gateway/log_redactor_loader.py`` for the full rationale.
from log_redactor_loader import build_default_redactor

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from auth import require_auth
from config import settings
from database import get_db
from display_name_orchestrator import DisplayNameContext, compute_display_name
from models import Job, User, UserVoice
from proxy import proxy_request
from quota import check_quota, reserve_quota, settle_job_quota
from smart_clone_reservation_service import (
    reserve_smart_clone_credit,
    settle_smart_clone_reservation,
)
from credits_service import (
    InsufficientCreditsError,
    ensure_credit_buckets_for_user, estimate_credits,
    reserve_credits_or_raise,
)
# Plan 2026-05-07 §4.5 (P1.1 fix): shared mirror helper so the sweeper
# (gateway/r2_artifact_sweeper.py) and this list-jobs path produce
# identical mirror side-effects (including quota settlement).
from job_terminal_mirror import mirror_job_terminal_state
from storage.job_store_reader import JobJsonRecord, parse_iso_timestamp
# Canonical language defaults (src/ is on sys.path via the tweak above).
# services.language_registry is stdlib-only and does NOT drag the
# services.jobs package init (pydub) — it's a top-level services module.
from services.language_registry import (
    DEFAULT_LANGUAGE_PAIR_PROFILE,
    LANG_EN,
    LANG_ZH_CN,
    SUPPORTED_LANGUAGE_PAIRS,
    make_pair_key,
    resolve_language_pair,
)

# Non-default pairs that ship on the §3/D1 "non-interactive lane": the job runs
# straight through (requires_review forced False), decoupled from Studio's
# interactive review default. Today this is only zh-CN->en; future pairs opt in
# EXPLICITLY here — never auto-inherited from is_default — so adding a pair can
# never silently skip review.
_NON_INTERACTIVE_LANGUAGE_PAIRS = frozenset({make_pair_key(LANG_ZH_CN, LANG_EN)})

_SMART_PAID_CLONE_CONFIRM_FIELDS = (
    "confirm_paid_voice_clone_credits",
    "confirm_paid_voice_clone_600_credits",
)
_SMART_CLONE_CREATE_RESERVATION_TTL_MINUTES = 24 * 60


def _get_smart_clone_cost_credits() -> int:
    try:
        from pricing_runtime import get_runtime_pricing

        raw_value = get_runtime_pricing().credits.voice_clone_cost_credits
        if raw_value is None:
            return 600
        value = int(raw_value)
        return value if value >= 0 else 600
    except Exception:
        return 600


def _smart_paid_clone_confirmed(raw_consent: object) -> bool:
    if not isinstance(raw_consent, dict):
        return False
    if raw_consent.get("confirm_paid_voice_clone_credits") is True:
        return True
    return (
        raw_consent.get("confirm_paid_voice_clone_600_credits") is True
        and _get_smart_clone_cost_credits() == 600
    )


POST_EDIT_RESPONSE_FIELDS = (
    "display_name",
    "expires_at",
    "editing_touched_at",
    "copy_of_job_id",
    "root_job_id",
    "edit_generation",
    "role_snapshot",
)

JOB_LIST_DEFAULT_LIMIT = 20
JOB_LIST_MAX_LIMIT = 100
POST_EDIT_USAGE_KEY = "post_edit_usage"
POST_EDIT_MAX_SEGMENT_SAVE_CHARS = 5000
POST_EDIT_BATCH_MAX_SEGMENTS_DEFAULT = 50
POST_EDIT_BATCH_MAX_SEGMENTS_PRO = 150

POST_EDIT_LIMITS: dict[str, dict[str, int | None]] = {
    "trial": {
        "overwrite_commits": 3,
        "copy_as_new": 0,
        "tts_segments": 8,
        "tts_chars": 1000,
        "batch_regenerates": 1,
        "preview_voice_daily": 20,
    },
    "plus": {
        "overwrite_commits": 10,
        "copy_as_new": 2,
        "tts_segments": 30,
        "tts_chars": 5000,
        "batch_regenerates": 2,
        "preview_voice_daily": 60,
    },
    "pro": {
        "overwrite_commits": 30,
        "copy_as_new": 5,
        "tts_segments": 100,
        "tts_chars": 20000,
        "batch_regenerates": 5,
        "preview_voice_daily": 200,
    },
    "admin": {
        "overwrite_commits": None,
        "copy_as_new": None,
        "tts_segments": None,
        "tts_chars": None,
        "batch_regenerates": None,
        "preview_voice_daily": None,
    },
}

POST_EDIT_BATCH_TRIGGER_STATUSES = {"text_dirty", "voice_dirty", "tts_failed"}


def _clean_youtube_video_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned):
        return None
    return cleaned


def canonicalize_youtube_source_content_hash(url: str) -> str | None:
    """Return the stable source hash for known YouTube URL shapes."""
    raw_url = (url or "").strip()
    if raw_url.startswith("youtu.be/"):
        raw_url = f"https://{raw_url}"
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    video_id: str | None = None
    path_parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be":
        video_id = _clean_youtube_video_id(path_parts[0] if path_parts else None)
    elif host in {"youtube.com", "music.youtube.com"} or host.endswith(".youtube.com"):
        route = path_parts[0].lower() if path_parts else ""
        if path_parts and route == "watch":
            video_id = _clean_youtube_video_id(parse_qs(parsed.query).get("v", [None])[0])
        elif len(path_parts) >= 2 and route in {"shorts", "live", "embed", "v"}:
            video_id = _clean_youtube_video_id(path_parts[1])

    return f"youtube:{video_id}" if video_id else None


def _project_root_for_uploaded_sources() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "")
        or os.environ.get("AIVIDEOTRANS_PROJECT_ROOT", "")
        or "/opt/aivideotrans/app"
    ).resolve(strict=False)


def _candidate_local_source_paths(source_value: str) -> list[Path]:
    raw = (source_value or "").strip()
    if not raw:
        return []
    path = Path(raw)
    candidates: list[Path] = [path]
    project_root = _project_root_for_uploaded_sources()
    if not path.is_absolute():
        candidates.append(project_root / raw)
    stripped = raw.lstrip("/\\")
    if stripped != raw or stripped.startswith("uploads/") or stripped.startswith("uploads\\"):
        candidates.append(project_root / stripped)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _sha256_content_hash_for_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("source_content_hash: failed to hash local source %s: %s", path, exc)
        return None
    return f"sha256:{digest.hexdigest()}"


def _resolved_upload_source_path(candidate: Path, uploads_root: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    if not resolved.is_relative_to(uploads_root):
        return None
    try:
        if candidate.is_symlink() or resolved.is_symlink():
            return None
        if not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


async def _compute_source_content_hash(source_type: str, source_value: str) -> str | None:
    normalized_type = (source_type or "").strip().lower()
    if normalized_type == "youtube_url":
        return canonicalize_youtube_source_content_hash(source_value)
    if normalized_type == "local_video":
        uploads_root = (_project_root_for_uploaded_sources() / "uploads").resolve(strict=False)
        for candidate in _candidate_local_source_paths(source_value):
            resolved = _resolved_upload_source_path(candidate, uploads_root)
            if resolved is not None:
                return await asyncio.to_thread(_sha256_content_hash_for_file, resolved)
        logger.warning(
            "source_content_hash: no valid upload found inside %s for %s",
            uploads_root,
            source_value,
        )
    return None


def _parse_job_list_pagination(request: Request) -> tuple[int | None, int]:
    query_params = getattr(request, "query_params", {}) or {}
    raw_limit = query_params.get("limit") if hasattr(query_params, "get") else None
    raw_offset = query_params.get("offset") if hasattr(query_params, "get") else None

    if raw_limit in (None, ""):
        limit: int | None = None
    else:
        try:
            limit = max(0, min(int(str(raw_limit)), JOB_LIST_MAX_LIMIT))
        except (TypeError, ValueError):
            limit = JOB_LIST_DEFAULT_LIMIT

    try:
        offset = max(0, int(str(raw_offset or "0")))
    except (TypeError, ValueError):
        offset = 0

    return limit, offset


def _serialize_response_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _merge_gateway_job_metadata(upstream_job: dict, db_job: Job | None) -> dict:
    """Overlay Gateway-owned metadata onto a Job API JSON record.

    The Job API JSON store owns pipeline progress fields, but Gateway is the
    source of truth for user-facing commercial/post-edit metadata. Returning
    upstream rows without this merge makes `display_name` and `expires_at`
    silently disappear from `/job-api/jobs`, and lets stale upstream JSON
    resurrect DB rows that Gateway cleanup already marked `purged`.
    """
    if db_job is None:
        return upstream_job

    merged = dict(upstream_job)
    db_status = getattr(db_job, "status", None)
    if db_status == "purged":
        merged["status"] = "purged"

    db_stage = getattr(db_job, "current_stage", None)
    if db_status == "purged" and db_stage is not None:
        merged["current_stage"] = db_stage

    is_admin_job = getattr(db_job, "role_snapshot", None) == "admin"
    if is_admin_job:
        merged["expires_at"] = None

    for field in POST_EDIT_RESPONSE_FIELDS:
        if is_admin_job and field == "expires_at":
            continue
        value = getattr(db_job, field, None)
        if value is not None:
            merged[field] = _serialize_response_value(value)

    # Language fields (PR-A part 2 §5): the Gateway PG row is the authoritative
    # resolved pair (set at create). Overlay so the job summary always reflects
    # it, even if an older upstream JSON record predates the language fields.
    for lang_field in ("source_language", "target_language", "language_pair"):
        value = getattr(db_job, lang_field, None)
        if value is not None:
            merged[lang_field] = value
    return merged


def _job_create_response_payload_from_db_job(job: Job, *, idempotent: bool = False) -> dict:
    """Serialize an existing Gateway job for create-idempotency retries."""
    payload = {
        "job_id": getattr(job, "job_id", ""),
        "job_type": "translation",
        "source_type": getattr(job, "source_type", "") or "",
        "source_ref": getattr(job, "source_ref", "") or "",
        "output_target": "jianying_draft",
        "speakers": getattr(job, "speakers", "auto") or "auto",
        "voice_a": None,
        "voice_b": None,
        "source_language": getattr(job, "source_language", None),
        "target_language": getattr(job, "target_language", None),
        "language_pair": getattr(job, "language_pair", None),
        "status": getattr(job, "status", "queued") or "queued",
        "current_stage": getattr(job, "current_stage", None),
        "progress_message": None,
        "created_at": _serialize_response_value(getattr(job, "created_at", None)),
        "updated_at": _serialize_response_value(getattr(job, "updated_at", None)),
        "started_at": _serialize_response_value(getattr(job, "started_at", None)),
        "completed_at": _serialize_response_value(getattr(job, "completed_at", None)),
        "project_dir": getattr(job, "project_dir", None),
        "manifest_path": None,
        "review_gate": getattr(job, "review_gate", None),
        "error_summary": getattr(job, "error_summary", None),
        "fallback_summary": None,
        "service_mode": getattr(job, "service_mode", None),
        "display_name": getattr(job, "display_name", None),
        "expires_at": _serialize_response_value(getattr(job, "expires_at", None)),
        "editing_touched_at": _serialize_response_value(
            getattr(job, "editing_touched_at", None)
        ),
        "copy_of_job_id": getattr(job, "copy_of_job_id", None),
        "root_job_id": getattr(job, "root_job_id", None),
        "edit_generation": getattr(job, "edit_generation", 0) or 0,
        "role_snapshot": getattr(job, "role_snapshot", None),
    }
    if idempotent:
        payload["idempotent"] = True
    return payload


async def _settle_smart_clone_reservation_from_job_state(
    db: AsyncSession,
    job: Job,
    *,
    reason: str,
) -> None:
    smart_state = getattr(job, "smart_state", None)
    if not isinstance(smart_state, dict):
        return
    if smart_state.get("smart_clone_credit_reserved") is not True:
        return
    reservation_id = smart_state.get("smart_clone_reservation_id")
    if not reservation_id:
        return
    try:
        outcome = await settle_smart_clone_reservation(
            db,
            reservation_id=reservation_id,
            service_mode=getattr(job, "service_mode", None) or "smart",
            smart_state_override=smart_state,
        )
        logger.info(
            "settled smart clone reservation=%s for job=%s reason=%s outcome=%s",
            reservation_id,
            getattr(job, "job_id", None),
            reason,
            getattr(outcome, "status", None),
        )
    except Exception:
        logger.warning(
            "failed to settle smart clone reservation=%s for job=%s reason=%s",
            reservation_id,
            getattr(job, "job_id", None),
            reason,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Structured error codes — spec §7
# ---------------------------------------------------------------------------

def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    detail: dict | None = None,
) -> Response:
    """Return a JSON error with structured error_code for frontend consumption."""
    body: dict = {"error": error_code, "message": message}
    if detail:
        body["detail"] = detail
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )


def _insufficient_credits_response(exc: InsufficientCreditsError) -> Response:
    return _error_response(
        402,
        "insufficient_credits",
        f"点数不足：本次预计需要 {exc.required} 点，当前可用 {exc.available} 点。请充值或升级后再试。",
        {"required_credits": exc.required, "available_credits": exc.available},
    )


# ---------------------------------------------------------------------------
# Service-mode resolution + fail-closed gate (Phase 2a free tier, Task 1)
# ---------------------------------------------------------------------------

_SUPPORTED_SERVICE_MODES = ("express", "studio", "smart", "free")
_SERVICE_MODE_STUDIO = "studio"


def _gate_service_mode(requested_mode, *, free_enabled: bool):
    """Resolve service_mode and apply the free-tier fail-closed gate.

    Returns ``(resolved_mode, error_response_or_None)``:

    * Unknown / unsupported modes keep the legacy coercion to ``express``.
    * ``free`` is recognized (NOT coerced to express) so the flag gate runs:
      ``free_enabled`` False -> reject (403 ``free_disabled``), never a silent
      express downgrade; True -> pass through to compute_job_policy free branch.
    """
    mode = requested_mode if requested_mode in _SUPPORTED_SERVICE_MODES else "express"
    if mode == "free" and not free_enabled:
        return mode, _error_response(
            403,
            "free_disabled",
            "免费版当前未开放。",
            {"requested_mode": "free"},
        )
    return mode, None


# --- Plan catalog ---
# The authoritative plan gate facts now live in ``plan_catalog.py``. The module-level
# ``PLAN_CATALOG`` name is a frozen import-time snapshot preserved for backward-compatible
# test imports. Request-time code calls the live functions directly.
from plan_catalog import get_legacy_plan_gate_dict  # noqa: E402

PLAN_CATALOG = get_legacy_plan_gate_dict()


# Gateway-local allowed TTS providers (no cross-layer import from tts_strategy)
_VALID_EXPRESS_PROVIDERS = {"cosyvoice", "mimo", "volcengine"}
_VALID_STUDIO_PROVIDERS = {"minimax", "mimo", "volcengine", "cosyvoice"}
_DEFAULT_EXPRESS_PROVIDER = "cosyvoice"
_DEFAULT_STUDIO_PROVIDER = "minimax"


def _apply_validated_express_consent(
    request_data: dict,
    *,
    express_consent_payload: dict | None,
    express_consent_parse_error: str | None,
) -> None:
    """Phase 4.3a §3.2 + Codex GitHub PR #17 复审 P2-1：把 validated Express
    consent 写进转发给 Job API 的 ``request_data``，**先清掉客户端夹带的**。

    安全契约：``express_consent`` / ``express_consent_parse_error`` 只能由
    Gateway 的 validated Express path 生成（``service_mode==express`` +
    ``validate_express_consent`` 跑过 + 后端 ``server_confirmed_at``）。任何
    客户端**直接**在 body 里塞的 ``express_consent`` 都是 forged，必须丢弃 ——
    否则 Studio / Smart job（``express_consent_payload`` 为 None）会把 forged
    dict 透传给 Job API 并被 ``src/services/jobs/api.py`` 持久化，破坏"非
    Express job 的 ``express_consent`` 必须为 None"的模型契约。

    原地修改 ``request_data``（无返回值）：

    1. **无条件** pop 掉 ``express_consent`` / ``express_consent_parse_error``
       （清客户端 forged 值）
    2. 仅当 caller 传入 validated payload / parse_error（非 None）时写回
    """
    request_data.pop("express_consent", None)
    request_data.pop("express_consent_parse_error", None)
    if express_consent_payload is not None:
        request_data["express_consent"] = express_consent_payload
    if express_consent_parse_error is not None:
        request_data["express_consent_parse_error"] = express_consent_parse_error


def compute_job_policy(user, service_mode: str) -> dict:
    """Compute job execution policy based on user role, plan, and service mode.

    TTS provider is read from admin settings (express_tts_provider / studio_tts_provider).
    Invalid values fall back to defaults (cosyvoice / minimax).

    Note on ``tts_model`` semantics — this field means different things per provider:

    * **minimax**: MiniMax model name (``speech-2.8-hd`` / ``speech-2.8-turbo``)
    * **cosyvoice**: CosyVoice model name (``cosyvoice-v3-flash``)
    * **volcengine**: value for ``req_params.model`` in the V3 API body
      (``seed-tts-1.1`` for express / *None* for studio 2.0 public voices).
      The ``resource_id`` (``seed-tts-1.0`` vs ``seed-tts-2.0``) is **not** stored
      in the snapshot — it is derived at runtime by the Generator layer from
      ``tts_provider + service_mode``.
    """
    from admin_settings import load_settings

    role = getattr(user, "role", "user") or "user"
    plan = getattr(user, "plan_code", "free") or "free"

    # Admin bypasses all limits
    is_admin = role == "admin"

    admin = load_settings()

    # Smart MVP P2 launch fix (2026-05-16): master plan §5.0 + §15 P2
    # locks smart to MiniMax (smart's clone API + quota model both
    # MiniMax-specific). Without this branch, smart submissions fell
    # into the ``else`` (express) branch — got tts_provider=cosyvoice
    # (admin default), voice_clone_enabled=False, requires_review=False
    # — user paid 100 credits/min but got express experience.
    if service_mode == "smart":
        return {
            "service_mode": "smart",
            # Hard-locked to MiniMax — admin's express/studio TTS
            # settings do NOT override (smart contract requires MiniMax).
            "tts_provider": "minimax",
            # Master plan §15 P2: smart uses 高质量 TTS regardless of
            # user plan tier (the fixed 100 credits/min price covers it).
            "tts_model": "speech-2.8-hd",
            # requires_review=True so review_state_manager + gate code
            # treat smart as a review job; the smart inline branch in
            # process.py auto-approves the review payloads
            # without user interaction.
            "requires_review": True,
            # smart's whole value proposition. Runtime still gated by
            # smart_consent.auto_voice_clone via validate_smart_consent
            # (Codex 第四十轮 P1.1).
            "voice_clone_enabled": True,
            # Distinct strategy string for audit clarity; downstream
            # code can branch on this if needed.
            "voice_strategy": "smart_auto",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
            # Single-tier smart product; "standard" only for compat with
            # the 2D (service_mode, quality_tier) pricing table (§5.1).
            "quality_tier": "standard",
        }

    if service_mode == "free":
        # MiMo free tier (Phase 2a). Reuses Express non-interactive
        # orchestration; only the TTS voice path differs (MiMo voiceclone,
        # which preserves the original speaker). credits=0 is enforced via
        # the pricing_runtime / DEBIT_RATES (free, standard)=0 truth source
        # (Task 3), not here. MiMo voiceclone uses an inline reference
        # (no voice_id) so the MiniMax/CosyVoice clone path stays OFF.
        #
        # Task 6 (gate #6) kill-switch: admin.free_tier_voiceclone_enabled
        # (default True) gates the MiMo voiceclone path. When OFF, free
        # DEGRADES to the cheapest preset engine (CosyVoice preset_mapping)
        # and the free tier KEEPS RUNNING (credits still 0, service_mode
        # still "free") — it never fails and never touches a paid clone API.
        if admin.free_tier_voiceclone_enabled:
            return {
                "service_mode": "free",
                "tts_provider": "mimo",
                "tts_model": "mimo-v2.5-tts-voiceclone",
                "requires_review": False,
                "voice_clone_enabled": False,
                "voice_strategy": "free_voiceclone",
                "plan_code_snapshot": plan,
                "role_snapshot": role,
                "quality_tier": "standard",
            }
        return {
            "service_mode": "free",
            "tts_provider": "cosyvoice",
            "tts_model": "cosyvoice-v3-flash",
            "requires_review": False,
            "voice_clone_enabled": False,
            "voice_strategy": "preset_mapping",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
            "quality_tier": "standard",
        }

    if service_mode == "studio":
        configured_provider = (admin.studio_tts_provider or "").strip().lower()
        tts_provider = configured_provider if configured_provider in _VALID_STUDIO_PROVIDERS else _DEFAULT_STUDIO_PROVIDER

        if tts_provider == "volcengine":
            # 豆包 2.0 — public voices do not need req_params.model;
            # voice cloning not supported on 2.0 (reserved for future seed-icl-2.0).
            tts_model = None
            voice_clone_enabled = False
        else:
            tts_model = "speech-2.8-hd" if (plan == "pro" or is_admin) else "speech-2.8-turbo"
            voice_clone_enabled = True

        return {
            "service_mode": "studio",
            "tts_provider": tts_provider,
            "tts_model": tts_model,
            "requires_review": True,
            "voice_clone_enabled": voice_clone_enabled,
            "voice_strategy": "user_selected",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
            # V3-6: single authoritative quality_tier source.
            # Current state: all jobs are "standard". When multi-tier
            # is productized, this is the one place to change.
            "quality_tier": "standard",
        }
    else:
        # Default: express
        configured_provider = (admin.express_tts_provider or "").strip().lower()
        tts_provider = configured_provider if configured_provider in _VALID_EXPRESS_PROVIDERS else _DEFAULT_EXPRESS_PROVIDER

        if tts_provider == "volcengine":
            # 豆包 1.0 — use seed-tts-1.1 for improved quality / latency.
            tts_model = "seed-tts-1.1"
        else:
            tts_model = "cosyvoice-v3-flash"

        return {
            "service_mode": "express",
            "tts_provider": tts_provider,
            "tts_model": tts_model,
            "requires_review": False,
            "voice_clone_enabled": False,
            "voice_strategy": "preset_mapping",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
            "quality_tier": "standard",
        }


def _probe_youtube_metadata(
    url: str, timeout: float = 5.0
) -> dict[str, object] | None:
    """Lightweight yt-dlp metadata probe.

    Returns the parsed JSON metadata dict (has ``duration`` / ``title`` /
    ``uploader`` / ...) on success, or ``None`` on any failure (missing
    binary, timeout, non-zero exit, invalid JSON).

    Single yt-dlp invocation — extending callers with more fields (title,
    etc.) does not add network round trips. See ``_probe_youtube_duration``
    for the legacy thin wrapper.
    """
    import subprocess
    try:
        command = _build_youtube_probe_command(url)
        result = subprocess.run(
            command,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                return parsed
        stderr = (result.stderr or "").strip()
        if stderr:
            logger.warning("yt-dlp probe failed for %s: %s", url, stderr[:500])
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, Exception) as e:
        logger.warning("yt-dlp probe failed for %s: %s", url, e)
    return None


def _build_youtube_probe_command(
    url: str, *, config_path: Path | None = None
) -> list[str]:
    command = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--ignore-no-formats-error",
    ]
    command.extend(_youtube_probe_auth_args(config_path=config_path))
    command.append(url)
    return command


def _youtube_probe_auth_args(*, config_path: Path | None = None) -> list[str]:
    try:
        from services import config_loader
    except Exception:
        return []

    config = None
    if config_path is not None:
        config = config_loader.load_project_local_config(config_path)
    else:
        for candidate in (
            Path("/opt/aivideotrans/config/autodub.local.json"),
            Path("/opt/aivideotrans/app/autodub.local.json"),
            Path(__file__).resolve().parent.parent / "autodub.local.json",
        ):
            if candidate.exists():
                config = config_loader.load_project_local_config(candidate)
                break
    if config is None:
        config = config_loader.load_project_local_config()

    cookie_file, _ = config_loader.resolve_path_value(
        config=config,
        config_key_paths=(("youtube", "cookie_file"),),
    )
    if cookie_file and Path(cookie_file).exists():
        return ["--cookies", cookie_file]

    cookies_from_browser, _ = config_loader.resolve_text_value(
        config=config,
        config_key_paths=(("youtube", "cookies_from_browser"),),
    )
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser]
    return []


def _probe_youtube_duration(url: str, timeout: float = 5.0) -> float | None:
    """Thin wrapper over :func:`_probe_youtube_metadata` returning duration only.

    Preserved for backward compatibility with older unit test mocks that
    patched this symbol directly. New callers should use
    :func:`_probe_youtube_metadata` and extract fields as needed.
    """
    meta = _probe_youtube_metadata(url, timeout=timeout)
    if isinstance(meta, dict):
        dur = meta.get("duration")
        if dur is not None:
            try:
                return float(dur)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def _extract_youtube_title(meta: dict[str, object] | None) -> str | None:
    """Return a non-empty stripped title from yt-dlp metadata, else None.

    Centralised so callers don't each reinvent the "whitespace-only title
    counts as missing" rule (see display_name branch 2 / M2)."""
    if not isinstance(meta, dict):
        return None
    title = meta.get("title")
    if isinstance(title, str):
        stripped = title.strip()
        if stripped:
            return stripped
    return None


def _clean_metadata_text(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def _metadata_string_list(value: object, *, limit: int, item_max_chars: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_metadata_text(item, max_chars=item_max_chars)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _extract_youtube_published_at(meta: dict[str, object] | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    for key in ("release_timestamp", "timestamp"):
        raw_ts = meta.get(key)
        if raw_ts is None:
            continue
        try:
            dt = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        return dt.isoformat()

    upload_date = meta.get("upload_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        try:
            dt = datetime(
                int(upload_date[0:4]),
                int(upload_date[4:6]),
                int(upload_date[6:8]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
        return dt.isoformat()
    return None


def _extract_youtube_source_metadata(meta: dict[str, object] | None) -> dict[str, object]:
    """Extract deterministic source metadata from yt-dlp JSON.

    This avoids LLM summarisation and uses only metadata already returned by
    the existing yt-dlp probe: real title, publication year, channel,
    categories/tags, and a short description excerpt.
    """
    if not isinstance(meta, dict):
        return {}

    title = _extract_youtube_title(meta)
    channel = (
        _clean_metadata_text(meta.get("channel"), max_chars=120)
        or _clean_metadata_text(meta.get("uploader"), max_chars=120)
    )
    description = _clean_metadata_text(meta.get("description"), max_chars=240)
    published_at = _extract_youtube_published_at(meta)
    era = published_at[:4] if published_at else None

    categories = _metadata_string_list(meta.get("categories"), limit=3)
    tags = _metadata_string_list(meta.get("tags"), limit=8)
    source_tags: dict[str, object] = {}
    if channel:
        source_tags["channel"] = channel
    if categories:
        source_tags["categories"] = categories
    if tags:
        source_tags["tags"] = tags

    summary_parts: list[str] = []
    if channel:
        summary_parts.append(f"频道：{channel}")
    if description and description != title:
        summary_parts.append(f"简介：{description}")
    if not summary_parts and title:
        summary_parts.append(f"标题：{title}")

    return {
        "source_video_title": title,
        "source_published_at": published_at,
        "source_content_summary": "；".join(summary_parts)[:500] if summary_parts else None,
        "source_content_era": era,
        "source_content_tags": source_tags or None,
    }


_CJK_TEXT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_AUTO_PLACEHOLDER_DISPLAY_NAME_RE = re.compile(
    r"^(?:上传视频|油管视频) \d{4}-\d{2}-\d{2} \d{3}(?:_[a-z0-9]{4})?$"
)


def _sanitize_s2_display_name(value: object) -> str | None:
    """Normalize a Chinese S2 title before writing it to Job.display_name."""
    if not isinstance(value, str):
        return None
    name = re.sub(r"\s+", " ", value).strip()
    name = name.strip(" \t\r\n\"'“”‘’《》<>（）()[]【】")
    name = re.sub(r"^(?:中文)?(?:任务名|标题|视频名|视频标题)\s*[:：]\s*", "", name).strip()
    if not name or not _CJK_TEXT_RE.search(name):
        return None
    if name.lower() in {"null", "none"} or name in {"无", "未知", "未命名视频"}:
        return None
    return name[:60].strip()


def _looks_like_truncated_source_title(job: Job, current_name: str) -> bool:
    source_title = (getattr(job, "title", "") or "").strip()
    if not source_title or not current_name:
        return False
    if _CJK_TEXT_RE.search(current_name):
        return False
    return source_title.startswith(current_name)


def _looks_like_youtube_id_fallback(job: Job, current_name: str) -> bool:
    source_ref = (getattr(job, "source_ref", "") or "").strip()
    if not source_ref or not current_name:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,16}", current_name):
        return False
    return current_name in source_ref


def _should_replace_display_name_from_s2(job: Job) -> bool:
    """Avoid overwriting explicit user names when S2 proposes a Chinese title."""
    current = (getattr(job, "display_name", None) or "").strip()
    if not current:
        return True
    if _AUTO_PLACEHOLDER_DISPLAY_NAME_RE.match(current):
        return True
    if (getattr(job, "source_type", "") or "").strip().lower() == "youtube_url":
        return (
            _looks_like_truncated_source_title(job, current)
            or _looks_like_youtube_id_fallback(job, current)
        )
    return False


# ---------------------------------------------------------------------------
# display_name DB lookups — production adapters for the orchestrator's
# injectable fetcher signatures (``gateway.display_name_orchestrator``).
# ---------------------------------------------------------------------------


async def _fetch_user_existing_display_names(
    db: AsyncSession, user_id: str
) -> set[str]:
    """Return the set of non-null ``display_name`` values owned by ``user_id``.

    Used for collision detection when naming a new job. Scope is the whole
    user (across all jobs, all statuses, all time) — mirrors the pure
    algorithm contract in ``display_name.resolve_collision``.

    Concurrent-submit race note: two near-simultaneous submits for the same
    user read the same snapshot here and may both produce the same base
    name before either commits. The ``_xxxx`` suffix absorbs the collision
    probabilistically; in the rare event it doesn't, the UI still renders
    via the fallback chain, so the worst case is "two cards look the same
    until one is renamed". A tighter guarantee would need a DB unique
    constraint, out of scope for T0-4.
    """
    result = await db.execute(
        select(Job.display_name).where(
            Job.user_id == user_id,
            Job.display_name.is_not(None),
        )
    )
    return {row[0] for row in result.all() if row[0]}


def _branch4_prefix_for_source(source_type: str | None) -> str:
    return "油管视频" if (source_type or "").strip().lower() == "youtube_url" else "上传视频"


async def _fetch_user_branch4_sequence_today(
    db: AsyncSession, user_id: str, local_date: date, source_type: str | None = None
) -> int:
    """Return how many branch-4 placeholder names this user already owns.

    Orchestrator adds 1 to the returned count for the next sequence number.
    Matches display_name._branch_4_default's source-specific prefix exactly."""
    date_str = local_date.strftime("%Y-%m-%d")
    pattern = f"{_branch4_prefix_for_source(source_type)} {date_str} %"
    result = await db.execute(
        select(func.count()).select_from(Job).where(
            Job.user_id == user_id,
            Job.display_name.like(pattern),
        )
    )
    return int(result.scalar() or 0)


async def _compensate_upstream_job(job_id: str) -> None:
    """Best-effort cancel/delete of an upstream job after local quota failure."""
    import httpx as _httpx
    upstream_url = f"{settings.job_api_upstream.rstrip('/')}/jobs/{job_id}"
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            resp = await client.delete(upstream_url)
            logger.info("Compensated upstream job %s: status=%s", job_id, resp.status_code)
    except Exception as exc:
        logger.error("Failed to compensate upstream job %s: %s", job_id, exc)


async def _release_smart_clone_reservation_on_create_failure(
    db: "AsyncSession", reservation_id: str | None
) -> None:
    """P3e-2b 对抗性复核 P1-B/C：create 失败路径（forward 非 2xx / minute reserve
    失败回滚 / job_id 不一致）**及时**释放已预留的 smart clone 600——避免挂满
    60min TTL 才被 sweeper 回收（``smart_clone_reservation_sweeper`` 仍是兜底）。

    无 chargeable billing event → ``settle_smart_clone_reservation`` 走 release
    （退还 600）。绝不抛（释放失败靠 sweeper TTL 兜底）。设计上 settle 自带独立
    事务（行锁 + 自 commit），在 create 失败/回滚后的 session 上调用安全。
    """
    if not reservation_id:
        return
    try:
        from smart_clone_reservation_service import settle_smart_clone_reservation
        await settle_smart_clone_reservation(db, reservation_id=reservation_id)
    except Exception as exc:  # noqa: BLE001 — 释放失败靠 sweeper 兜底，绝不阻断
        logger.warning(
            "smart clone reservation release on create failure failed "
            "(reservation=%s; sweeper will reclaim within TTL): %s",
            reservation_id, exc,
        )


# ---------------------------------------------------------------------------
# Language facts endpoint (PR-A part 2 §5 / D5)
# ---------------------------------------------------------------------------
# DISPLAY-layer capabilities + labels — owned HERE (the Gateway facts
# endpoint), deliberately NOT in services.language_registry. D5 forbids reusing
# the internal ``adapted_paid_capabilities`` constant name for the frontend
# display bitset; these are the user-facing workflow steps every supported pair
# runs (the paid post-edit / suggest-split gate is internal-only).
_LANGUAGE_WORKFLOW_CAPABILITIES = ["transcribe", "translate", "tts", "subtitles", "jianying"]
_LANGUAGE_DISPLAY_LABELS = {LANG_EN: "英文", LANG_ZH_CN: "中文"}


def _language_pair_label(profile) -> str:
    src = _LANGUAGE_DISPLAY_LABELS.get(profile.source_language, profile.source_language)
    tgt = _LANGUAGE_DISPLAY_LABELS.get(profile.target_language, profile.target_language)
    return f"{src} → {tgt}"


async def intercept_language_facts(
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /api/language-facts — the language directions this user may pick.

    Returns the supported pairs filtered to the caller's entitlements
    (``get_effective_allowed_language_pairs``): every user sees the GA default
    (en->zh-CN); zh-CN->en appears only for admin-enabled allowlisted users (or
    admins). Each entry carries a human label + the DISPLAY
    ``workflow_capabilities`` (D5 — distinct from the internal
    ``adapted_paid_capabilities`` paid-gate set).
    """
    from entitlements import get_effective_allowed_language_pairs

    allowed = set(get_effective_allowed_language_pairs(user))
    facts = [
        {
            "pair_key": pair_key,
            "source_language": profile.source_language,
            "target_language": profile.target_language,
            "label": _language_pair_label(profile),
            "is_default": profile.is_default,
            # Create-path hard gate: false → the selector shows 即将上线 + disables
            # the option; create-path 409s `language_pair_not_yet_available`.
            "pipeline_ready": profile.pipeline_ready,
            "workflow_capabilities": list(_LANGUAGE_WORKFLOW_CAPABILITIES),
        }
        for pair_key, profile in SUPPORTED_LANGUAGE_PAIRS.items()
        if pair_key in allowed
    ]
    return Response(
        content=json.dumps({"language_pairs": facts}, ensure_ascii=False),
        status_code=200,
        headers={"content-type": "application/json"},
    )


async def intercept_list_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs — forward to upstream, then filter by user_id."""
    upstream_query = "" if settings.auth_required and user is not None else None
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
        override_query=upstream_query,
    )


    # If auth not required or no user, return as-is
    if not settings.auth_required or user is None:
        return upstream_response

    # Filter jobs by user_id, with auto-reconciliation for orphan jobs
    try:
        data = json.loads(upstream_response.body)
        all_jobs = data.get("jobs", [])

        # Get all job_ids in DB (any user)
        result_all = await db.execute(select(Job.job_id))
        all_db_job_ids = {row[0] for row in result_all.all()}

        # Get this user's rows. We need the full row, not just job_id, because
        # Gateway owns the user-facing metadata that the Job API JSON store
        # does not always carry (`display_name`, `expires_at`, cleanup status).
        result_user = await db.execute(select(Job).where(Job.user_id == user.id))
        user_jobs: dict[str, Job] = {}
        user_job_ids: set[str] = set()
        try:
            scalar_rows = result_user.scalars().all()
        except Exception:
            scalar_rows = None
        if isinstance(scalar_rows, list):
            user_jobs = {
                row.job_id: row
                for row in scalar_rows
                if getattr(row, "job_id", None)
            }
            user_job_ids = set(user_jobs)
        if not user_job_ids:
            # Compatibility for tests / older adapters that still return rows
            # shaped like (job_id,) for this query.
            try:
                user_job_ids = {row[0] for row in result_user.all()}
            except Exception:
                user_job_ids = set()

        # Log orphan jobs but do NOT auto-claim
        orphan_ids = [j.get("job_id") for j in all_jobs if j.get("job_id") and j.get("job_id") not in all_db_job_ids]
        if orphan_ids:
            print(f"[GATEWAY] ⚠ {len(orphan_ids)} orphan job(s) not in DB: {orphan_ids[:5]}", flush=True)

        # Sync status from upstream to DB + settle quota on terminal transitions
        upstream_by_id = {j.get("job_id"): j for j in all_jobs if j.get("job_id")}
        for jid in user_job_ids:
            db_job = user_jobs.get(jid)
            upstream_job = upstream_by_id.get(jid)
            if upstream_job:
                upstream_status = upstream_job.get("status", "")
                upstream_stage = upstream_job.get("current_stage")
                try:
                    if db_job is None:
                        result_job = await db.execute(
                            select(Job).where(Job.job_id == jid)
                        )
                        db_job = result_job.scalar_one_or_none()
                        if db_job is not None:
                            user_jobs[jid] = db_job
                    if db_job is not None:
                        if db_job.status == "purged":
                            # Gateway cleanup is authoritative. A stale Job API
                            # JSON row must not resurrect a project whose
                            # artifacts have already been removed. (Mirror
                            # helper also short-circuits on this; check up
                            # front so we skip the V3 shadow block too.)
                            continue
                        old_status = db_job.status
                        upstream_project_dir = upstream_job.get("project_dir")
                        # Plan 2026-05-07 §4.5 (P1.1): single source of truth
                        # for mirror semantics. Sweeper invokes the same helper.
                        # Day 2 follow-up: pass real upstream edit_generation
                        # (None when absent) so the mirror can keep PG in
                        # sync with overwrite bumps. See
                        # gateway/job_terminal_mirror.py for the drift-fix
                        # rationale.
                        upstream_edit_generation = upstream_job.get("edit_generation")
                        try:
                            upstream_edit_generation_int = (
                                int(upstream_edit_generation)
                                if upstream_edit_generation is not None
                                else None
                            )
                        except (TypeError, ValueError):
                            upstream_edit_generation_int = None
                        # Smart MVP P2 — pull smart_state straight from
                        # the upstream payload so terminal mirror keeps
                        # parity with the JSON store on this Job-API
                        # poll path. Without it the DB stays NULL and
                        # the F4 settle dispatcher misses the smart
                        # credits_policy branch on this code path.
                        upstream_smart_state = upstream_job.get("smart_state")
                        await mirror_job_terminal_state(
                            db,
                            db_job,
                            JobJsonRecord(
                                job_id=jid,
                                status=upstream_status,
                                completed_at=parse_iso_timestamp(
                                    upstream_job.get("completed_at")
                                ),
                                project_dir=upstream_project_dir,
                                current_stage=upstream_stage,
                                edit_generation=upstream_edit_generation_int,
                                jianying_draft_zip_path=None,  # not read by mirror
                                service_mode=None,  # not read by mirror
                                smart_state=(
                                    upstream_smart_state
                                    if isinstance(upstream_smart_state, dict)
                                    else None
                                ),
                            ),
                        )
                except Exception:
                    pass
        try:
            await db.commit()
        except Exception:
            await db.rollback()

        # Only return jobs that belong to this user in DB
        filtered_jobs = [
            _merge_gateway_job_metadata(j, user_jobs.get(j.get("job_id")))
            for j in all_jobs
            if j.get("job_id") in user_job_ids
        ]
        total_jobs = len(filtered_jobs)
        limit, offset = _parse_job_list_pagination(request)
        if limit is None:
            paged_jobs = filtered_jobs[offset:]
        else:
            paged_jobs = filtered_jobs[offset: offset + limit]
        print(f"[GATEWAY] list_jobs: upstream={len(all_jobs)}, db_user={len(user_job_ids)}, returning={len(paged_jobs)}/{total_jobs}", flush=True)

        # Plan §10.4 deepening: redact provider names / paths / UUIDs
        # from progress_message + error_summary.message for non-admin
        # users. Admin gets pass-through (handled inside the helper).
        # Build the redactor once and reuse across rows — saves N-1
        # rebuilds on a typical 20-job list.
        if not _is_admin_user(user):
            _shared_redactor = build_default_redactor()
            if _shared_redactor is not None:
                for job_dict in paged_jobs:
                    if isinstance(job_dict, dict):
                        _redact_job_record_in_place(
                            job_dict, user, redactor=_shared_redactor
                        )

        data["jobs"] = paged_jobs
        data["total"] = total_jobs
        data["limit"] = limit
        data["offset"] = offset
        data["has_more"] = offset + len(paged_jobs) < total_jobs

        return Response(
            content=json.dumps(data, ensure_ascii=False),
            status_code=200,
            headers={"content-type": "application/json"},
        )
    except Exception as exc:
        import traceback
        print(f"[GATEWAY] ❌ Failed to filter jobs: {exc}", flush=True)
        print(f"[GATEWAY] ❌ Traceback: {traceback.format_exc()}", flush=True)
        return upstream_response


async def intercept_create_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs — full spec §8.1 flow:

    1. Parse request
    2. Validate service_mode against plan
    3. Check concurrency limit
    4. Accept estimated_duration from frontend, validate against plan max
    5. Compute execution snapshot
    6. Generate create_idempotency_key
    7. Write PostgreSQL job record (quota_state='none')
    8. Forward to upstream Job API
    9. On upstream failure, rollback DB record
    """
    raw_request_body = await request.body()
    try:
        request_data = json.loads(raw_request_body) if raw_request_body else {}
    except Exception:
        request_data = {}

    # plan 2026-06-14 §3.4 安全硬化（CodeX P2 审核）：``anonymous_preview`` 是
    # **server-only 信任标记**——只有 gateway 匿名 create 路径
    # （anonymous_preview_api，直发 Job API + internal_headers）能设。公共
    # 认证 create 路径**无条件剥离**客户端夹带值，否则登录用户可夹带
    # ``anonymous_preview:true`` 让 pipeline 切到匿名克隆分支（改用匿名主开关、
    # 跳过登录态 allowlist）实现提权 / 绕 cap。剥离后该标记对公共路径永远为假。
    if isinstance(request_data, dict):
        request_data.pop("anonymous_preview", None)
        # P3e-2b CodeX 终审 P1 #1：``job_id`` / ``smart_state`` 同为 **server-only
        # 信任标记**——只有 gateway 在 forward 前 reserve 成功后才设（Option C）。
        # 公共认证 create 路径无条件剥离客户端夹带值；同样剥离顶层 smart-clone
        # marker，避免客户端伪造 reservation 状态打开 paid-provider gate。
        request_data.pop("job_id", None)
        request_data.pop("smart_state", None)
        request_data.pop("smart_clone_reservation_id", None)
        request_data.pop("smart_clone_credit_reserved", None)

    # PR#3C-b3g (2026-05-15): "smart" added to whitelist. Smart MVP P2
    # pipeline code landed in src/services/smart/ + src/pipeline/process.py
    # but the entry-side gate was never updated — submissions of
    # service_mode=smart were silently coerced to express, never reaching
    # the smart inline branch. Plan §4.2 lists smart as the third
    # supported service_mode alongside express/studio.
    service_mode, _mode_gate_error = _gate_service_mode(
        request_data.get("service_mode", "express"),
        free_enabled=settings.enable_free_tier,
    )
    if _mode_gate_error is not None:
        return _mode_gate_error

    # --- Smart kill switch — runs FIRST (Task #23, P2 launch blocker #1) ---
    # Codex audit 2026-05-24: previous implementation put the kill switch
    # at L1135 inside ``if user and not is_admin:`` so admin users could
    # bypass it via direct API call even when ops flipped the emergency
    # toggle. Plus: the disabled gate ran AFTER smart_consent validation,
    # so a user with empty consent on a kill-switched smart job got the
    # misleading ``smart_consent_invalid`` error instead of ``smart_disabled``.
    #
    # Fix (both issues at once):
    #   - Move kill switch HERE, before smart_consent validation
    #   - Apply to all users including admin (concurrency/quota/duration
    #     admin bypass is preserved below; kill switch is a safety
    #     boundary that must NOT be bypassable)
    if service_mode == "smart" and user is not None:
        from entitlements import get_effective_allowed_service_modes
        effective_modes = get_effective_allowed_service_modes(user)
        if "smart" not in effective_modes:
            return _error_response(
                403, "smart_disabled",
                "智能版当前未启用。请联系管理员开启 Smart 模式后再试。",
                {"requested_mode": "smart"},
            )

    # --- smart_consent validation (PR#3C-b3g, hardened Codex 第四十轮 P1.1) ---
    # Smart pipeline reads `_snap("smart_consent")` to gate auto-clone
    # and (future) on_budget_exhausted policy. Master plan §5.3 mandates
    # a complete 6-field payload — before this validator, Gateway just
    # passed any dict through, so partial / malformed consent slipped
    # in. Codex 40 P1.1: validate-or-reject for service_mode=smart.
    # Defensive: only validate when service_mode==smart so a non-smart
    # submission can't trigger consent errors.
    smart_consent_payload = None
    smart_paid_clone_confirmed = False
    if service_mode == "smart":
        from smart_consent import validate_smart_consent
        raw_consent = request_data.get("smart_consent")
        smart_paid_clone_confirmed = _smart_paid_clone_confirmed(raw_consent)
        consent_obj, consent_error = validate_smart_consent(raw_consent)
        if consent_error is not None:
            return _error_response(
                400, "smart_consent_invalid",
                f"智能版需要完整的同意确认: {consent_error}",
                {"validator_error": consent_error},
            )
        # Persist the canonical 6-field form (no extras leaked through).
        smart_consent_payload = consent_obj.to_dict()

    # --- express_consent validation (Phase 4.3a §3.1.a / §3.2) ---
    # Express consent is OPTIONAL and uses **soft skip** semantics (unlike
    # Smart's hard-fail). validate_express_consent never raises:
    #   - missing / non-dict → returns (None, "express_consent_missing_or_invalid_type")
    #   - malformed field type → returns (None, "<reason>")
    #   - well-formed → returns (parsed_dict, None)
    # When parse_error != None the JobRecord still carries the reason so
    # pipeline can emit audit JSONL with a specific reason_code; the
    # job is NOT rejected (auto-clone simply does not run).
    # spec v0.3 §3.1 / §3.1.a — and design rationale §1.1 G6.
    express_consent_payload: dict | None = None
    express_consent_parse_error: str | None = None
    if service_mode == "express":
        from express_consent import validate_express_consent
        raw_express_consent = request_data.get("express_consent")
        express_consent_payload, express_consent_parse_error = (
            validate_express_consent(raw_express_consent)
        )
        # Phase 4.3a §3.1.a server-side authoritative timestamp.
        # When the user explicitly opted into auto-clone (auto_voice_clone
        # is True, strict identity), Gateway stamps a UTC ISO 8601 string
        # as the **authoritative** consent time. Pipeline writes this into
        # WorkerCloneConsent.confirmed_at and the audit JSONL key timestamp
        # — NOT the client-supplied client_confirmed_at (which can be
        # forged by a malicious client).
        # We deliberately set server_confirmed_at ONLY when auto_voice_clone
        # is True: if the user did not opt in, there is no consent action
        # for the server to confirm.
        if (
            express_consent_payload is not None
            and express_consent_payload.get("auto_voice_clone") is True
        ):
            express_consent_payload["server_confirmed_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

    # --- free_consent validation (Phase 2a LAUNCH GATE / 《民法典》1023) ---
    # The free voiceclone reproduces the SOURCE speaker's voice, so the user must
    # attest they hold the rights (design §5.3). HARD-fail (unlike express
    # soft-skip): no confirmed consent -> reject the free job HERE, before any
    # quota reserve or upstream forward. validate_free_consent returns a payload
    # ONLY when voice_rights_confirmed is True.
    free_consent_payload: dict | None = None
    if service_mode == "free":
        from free_consent import validate_free_consent

        free_consent_payload, free_consent_reason = validate_free_consent(
            request_data.get("free_consent")
        )
        if free_consent_payload is None:
            return _error_response(
                403,
                "consent_required",
                "免费版需先确认您对该视频内容及其中说话人声音的使用拥有合法授权。",
                {"requested_mode": "free", "reason": free_consent_reason},
            )
        # Server-side authoritative consent timestamp (client_confirmed_at is
        # untrusted / forgeable). This is the liability evidence forwarded to the
        # pipeline / audit.
        free_consent_payload["server_confirmed_at"] = (
            datetime.now(timezone.utc).isoformat()
        )

    # --- User context ---
    user_role = getattr(user, "role", "user") or "user" if user else "user"
    user_plan = getattr(user, "plan_code", "free") or "free" if user else "free"
    is_admin = user_role == "admin"
    # Trial-aware plan gate (P3): if user is in active trial window, elevate
    # capabilities to Plus-tier (Studio, higher duration/concurrency) without
    # changing plan_code. Falls back to PLAN_CATALOG for non-trial users.
    from plan_catalog import get_effective_plan_gate
    plan_info = get_effective_plan_gate(user) if user else get_legacy_plan_gate_dict().get("free", {})

    # --- 0. Idempotency key ---
    # Retry/double-submit requests must return the original job before active
    # concurrency or clone-reservation gates can reject an otherwise harmless
    # duplicate create call.
    idempotency_key = request_data.get("create_idempotency_key") or str(_uuid.uuid4())
    if user is not None and idempotency_key:
        existing_by_create_key = (
            await db.execute(
                select(Job).where(
                    Job.user_id == user.id,
                    Job.create_idempotency_key == str(idempotency_key),
                )
            )
        ).scalar_one_or_none()
        if existing_by_create_key is not None:
            return Response(
                content=json.dumps(
                    _job_create_response_payload_from_db_job(
                        existing_by_create_key, idempotent=True
                    ),
                    ensure_ascii=False,
                ),
                status_code=202,
                headers={"content-type": "application/json"},
            )

    # --- Smart MVP §7.3 pre-flight voice library quota check (2026-05-16) ---
    # Without this, smart jobs that would hit the water-mark brake at
    # voice_review fail HALFWAY through (after S0/S1/S2 ASR + speaker
    # review + translation). Better UX per user feedback: reject at job
    # creation with a clear actionable message so the user can clean up
    # voice library BEFORE spending time/budget.
    #
    # Skipped for admin (testing / demo bypass — matches the same
    # skip in process.py smart inline branch on role_snapshot=admin).
    #
    # IMPORTANT: do NOT re-import ``select`` / ``func`` / ``UserVoice``
    # inside this branch. They are already imported at module top
    # (lines 45 / 54). A local re-import would mark those names
    # function-local throughout intercept_create_job, so any code path
    # that SKIPS this branch (admin smart / studio / express / no-user)
    # hits UnboundLocalError at the next ``select(Job)`` call (e.g. the
    # PG insert at line ~1213). That's exactly the 2026-05-16 incident
    # where admin smart submissions left orphan JSON-store jobs with
    # no PG row → user task list looked empty.
    if service_mode == "smart" and user and not is_admin:
        try:
            from admin_settings import load_settings

            # Phase 3 (plan 2026-05-17-user-voice-candidate-first
            # §Consent × Admin 决策矩阵): voice-library quota only
            # matters when the runtime will ACTUALLY clone. Both gates
            # must allow new clone for the preflight to apply:
            #   - smart_consent.auto_voice_clone (user-side gate)
            #   - admin.smart_auto_clone_enabled (admin-side gate)
            # When either gate is closed, smart REUSES (strong match
            # in user voice library) or falls to PRESET — no clone
            # slot consumed, so a near-cap library does NOT block the
            # submission. Matrix rows 2/3/5/6/7/8 all expect runtime
            # to skip new clone; the preflight must skip in lockstep
            # to avoid product-level inconsistency ("admin disabled
            # clones, why does it still complain about clone quota?").
            # Defensive: if admin_settings unreadable, fall back to
            # admin_clone_enabled=True (mirrors process.py:3457-3470).
            admin_settings = load_settings()
            admin_clone_enabled = bool(
                getattr(admin_settings, "smart_auto_clone_enabled", True)
            )
            consent_allows_clone = (
                smart_consent_payload is not None
                and smart_consent_payload.get("auto_voice_clone") is True
                and smart_paid_clone_confirmed
            )
            if consent_allows_clone and admin_clone_enabled:
                quota_used_result = await db.execute(
                    select(func.count())
                    .select_from(UserVoice)
                    .where(
                        UserVoice.user_id == user.id,
                        UserVoice.expired_at.is_(None),
                    )
                )
                quota_used = int(quota_used_result.scalar() or 0)
                cap = int(
                    getattr(admin_settings, "smart_user_voice_clone_cap", 30)
                    or 30
                )
                remaining = max(0, cap - quota_used)
                # Water mark matches services.smart.auto_voice_review's
                # internal threshold (reason_code suffix _le_3 confirmed).
                water_mark = 3
                if remaining <= water_mark:
                    return _error_response(
                        400, "smart_voice_library_at_safety_water_mark",
                        f"您的个人音色库已接近上限（{quota_used} / {cap} 已用，"
                        f"剩余 {remaining}）。智能版需要至少 {water_mark + 1} "
                        f"个剩余位置才能自动克隆主说话人音色。请先清理音色库后重试，"
                        f"或改用工作台版手动管理音色。",
                        {
                            "quota_used": quota_used,
                            "quota_cap": cap,
                            "remaining": remaining,
                            "water_mark": water_mark,
                        },
                    )
        except Exception as _quota_check_exc:
            # Defensive: quota check failure must NOT block submission
            # entirely (e.g., admin_settings unreadable, DB transient
            # failure). Log via standard pattern, fall through. The
            # smart inline branch's runtime quota check will still
            # fail-closed as fallback.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "smart pre-flight quota check failed for user=%s: %s",
                getattr(user, "id", None), _quota_check_exc,
            )

    # --- 1. Validate service_mode ---
    # Use get_effective_allowed_service_modes (Task #23, P2 launch
    # blocker #1) — single source of truth that applies the smart kill
    # switch (env AVT_ENABLE_SMART_MODE + admin runtime toggle). Reading
    # plan_info["allowed_service_modes"] directly bypasses both layers
    # of the kill switch and lets Plus/Pro users create smart jobs even
    # when ops flipped the toggle off.
    if user and not is_admin:
        from entitlements import get_effective_allowed_service_modes
        effective_modes = get_effective_allowed_service_modes(user)
        if service_mode not in effective_modes:
            return _error_response(
                403, "service_mode_not_allowed",
                f"当前套餐（{user_plan}）不支持{service_mode}模式，请升级套餐。",
                {"plan_code": user_plan, "requested_mode": service_mode,
                 "allowed_modes": effective_modes},
            )

    # --- 2. Concurrency limit ---
    if user and not is_admin:
        active_count_result = await db.execute(
            select(func.count()).where(
                Job.user_id == user.id,
                # Concurrency limit counts any "active" job. editing is active
                # (user holds a paused editing session) and must count — see
                # docs/plans/2026-04-18-studio-post-edit-plan.md §4.3.
                Job.status.in_(["queued", "running", "waiting_for_review", "editing"]),
            )
        )
        active_count = active_count_result.scalar() or 0
        max_concurrent = plan_info["max_concurrent_jobs"]
        if active_count >= max_concurrent:
            return _error_response(
                409, "concurrent_limit",
                f"已有{active_count}个未完成任务，上限{max_concurrent}个。请先完成或取消。",
                {"active_count": active_count, "max_concurrent": max_concurrent},
            )

    # --- 2b. Free quota check ---
    if user and not is_admin and user_plan == "free":
        has_quota, quota_used, quota_total = await check_quota(db, user)
        if not has_quota:
            return _error_response(
                403, "quota_exhausted",
                f"免费额度已用完（{quota_used}/{quota_total}），请升级套餐。",
                {"free_jobs_quota_used": quota_used, "free_jobs_quota_total": quota_total},
            )

    # --- 3. Validate source ---
    source = request_data.get("source", {})
    source_type = str(source.get("type", "")).strip() if isinstance(source, dict) else ""
    source_value = str(source.get("value", "")).strip() if isinstance(source, dict) else ""
    if not source_type or not source_value:
        return _error_response(
            400, "invalid_source",
            "缺少视频来源信息。",
            {"source_type": source_type or None, "source_value": source_value or None},
        )

    # Normalize frontend "local_file" to the canonical "local_video"
    if source_type == "local_file":
        source_type = "local_video"
        if isinstance(source, dict):
            source["type"] = "local_video"

    # --- 3b. Opaque chunked upload ref（plan 2026-06-11 §3.10）---
    # 分片上传 complete 返回的 ref 是 ``chunked:{upload_id}``（不透明 token，
    # 不是文件路径）。这里按 upload_id + 当前登录 user 查 state==ready 的
    # upload，校验通过才把 source.value 替换为**服务端记录的 final_path**
    # 转发 Job API——前端传回的任何绝对路径不再被信任（分片通道关死
    # "路径作能力凭证" 面；存量单请求路径加固见 plan §7 H1，单独任务）。
    # 不存在 / 非本人 / 非 ready / id 非法 → 同一响应体（无存在性侧信道）。
    chunked_upload_ref_id: str | None = None
    if source_type == "local_video" and source_value.startswith("chunked:"):
        from chunked_upload_store import (
            parse_chunked_source_value,
            resolve_ready_upload,
        )

        _chunked_err = _error_response(
            404, "upload_not_found",
            "上传文件不存在或已过期，请重新上传。",
        )
        if user is None:
            return _chunked_err
        _upload_id = parse_chunked_source_value(source_value)
        _resolved_path = (
            resolve_ready_upload(user_id=str(user.id), upload_id=_upload_id)
            if _upload_id
            else None
        )
        if not _resolved_path:
            return _chunked_err
        chunked_upload_ref_id = _upload_id
        source_value = _resolved_path
        if isinstance(source, dict):
            # source 与 request_data["source"] 是同一对象——这里替换后
            # 下游 upstream forward 拿到的就是服务端 final_path。
            source["value"] = _resolved_path

    source_content_hash = await _compute_source_content_hash(source_type, source_value)

    # --- 4. Duration: probe (YouTube) or accept frontend estimate ---
    estimated_duration_seconds = request_data.get("estimated_duration_seconds")
    if estimated_duration_seconds is not None:
        try:
            estimated_duration_seconds = float(estimated_duration_seconds)
        except (TypeError, ValueError):
            estimated_duration_seconds = None

    # For YouTube URLs, attempt a single yt-dlp probe (5s timeout) for duration
    # (→ quota / plan gating below) and source title metadata. The user-facing
    # display_name starts as a Chinese placeholder and is replaced later by S2
    # review when a content-aware Chinese title is available.
    probed_title: str | None = None
    source_metadata: dict[str, object] = {}
    if source_type == "youtube_url":
        meta = _probe_youtube_metadata(source_value)
        source_metadata = _extract_youtube_source_metadata(meta)
        probed_title = (
            source_metadata.get("source_video_title")
            if isinstance(source_metadata.get("source_video_title"), str)
            else None
        )
        if estimated_duration_seconds is None and isinstance(meta, dict):
            dur = meta.get("duration")
            if dur is not None:
                try:
                    estimated_duration_seconds = float(dur)  # type: ignore[arg-type]
                    logger.info(
                        "yt-dlp probe: %s → %.0fs",
                        source_value, estimated_duration_seconds,
                    )
                except (TypeError, ValueError):
                    pass

    # Duration limit check (if we have an estimate)
    if user and not is_admin and estimated_duration_seconds is not None:
        max_minutes = plan_info["max_duration_minutes"]
        estimated_minutes = estimated_duration_seconds / 60
        if estimated_minutes > max_minutes:
            return _error_response(
                403, "duration_limit",
                f"视频预估时长{estimated_minutes:.0f}分钟，超出套餐上限{max_minutes}分钟。",
                {"estimated_minutes": round(estimated_minutes, 1),
                 "max_minutes": max_minutes, "plan_code": user_plan},
            )

    # --- 5. Compute execution snapshot ---
    policy = compute_job_policy(user, service_mode) if user else {}

    # --- 5a. Language pair resolution + entitlement gate (PR-A part 2 §3) ---
    # Zero-regression default: a request with NO — or INCOMPLETE — language
    # fields locks to the GA pair (en->zh-CN), with no validation and no
    # entitlement check. A direction needs BOTH source and target; a single
    # field is not a complete pair request, so it is ignored rather than
    # rejected (a previously-ignored stray field must never start 400-ing). This
    # is also the anonymous lane's locked path (its payload whitelist strips
    # language fields). Only a COMPLETE source+target request is validated +
    # gated, and only then are canonical values forwarded to the Job API.
    _raw_source_language = str(request_data.get("source_language") or "").strip() or None
    _raw_target_language = str(request_data.get("target_language") or "").strip() or None
    if _raw_source_language is None or _raw_target_language is None:
        resolved_pair = DEFAULT_LANGUAGE_PAIR_PROFILE
    else:
        resolved_pair = resolve_language_pair(_raw_source_language, _raw_target_language)
        if resolved_pair is None:
            return _error_response(
                400,
                "unsupported_language_pair",
                "暂不支持所选的语言方向。",
                {
                    "source_language": _raw_source_language,
                    "target_language": _raw_target_language,
                    "supported_pairs": list(SUPPORTED_LANGUAGE_PAIRS.keys()),
                },
            )
        # Lazy import (mirrors get_effective_allowed_service_modes usage below).
        from entitlements import get_effective_allowed_language_pairs
        if resolved_pair.language_pair not in get_effective_allowed_language_pairs(user):
            return _error_response(
                403,
                "language_pair_not_allowed",
                "当前账号未开通该语言方向（内测中）。",
                {"language_pair": resolved_pair.language_pair},
            )
        # HARD create-block (code-level, independent of the admin flag): even an
        # entitled / admin user cannot create a pair the END-TO-END pipeline
        # can't run yet. `pipeline_ready` is a registry CONSTANT — only a
        # pipeline PR (PR-W/CD/F) flips it, so flipping `language_pairs_enabled`
        # can NEVER produce a broken paid job. zh-CN->en stays blocked here until
        # the execution path is adapted. (The admin flag still controls facts
        # visibility, so the selector can show it as 即将上线 / disabled.)
        if not resolved_pair.pipeline_ready:
            return _error_response(
                409,
                "language_pair_not_yet_available",
                "该语言方向即将上线，敬请期待。",
                {"language_pair": resolved_pair.language_pair},
            )
        # Forward the CANONICAL pair to the Job API so its JobRecord persists the
        # normalized values — the §4 capability gate reads record.language_pair.
        request_data["source_language"] = resolved_pair.source_language
        request_data["target_language"] = resolved_pair.target_language

    # §3 decision D1: the zh-CN->en FIRST-RELEASE lane is Studio non-interactive
    # (requires_review forced False), decoupled from现网 Studio's True. **Scoped
    # to Studio ONLY (codex P2)**: express/free already run requires_review=False
    # (no-op), and clearing it for `smart` would break Smart's review-gated
    # auto-review branch — the job would stay service_mode='smart' for routing/
    # billing but silently skip its voice/translation decisions. Set BEFORE
    # `request_data.update(policy)` so it reaches both the Job API (forwarded
    # body) and the gateway PG row (policy.get below).
    if (
        policy
        and service_mode == _SERVICE_MODE_STUDIO
        and resolved_pair.language_pair in _NON_INTERACTIVE_LANGUAGE_PAIRS
    ):
        policy["requires_review"] = False

    # Ordinary jobs keep a fixed 7-day retention deadline from creation. Admin
    # jobs intentionally have no TTL and are excluded from both cleanup paths.
    job_expires_at = None if is_admin else datetime.now(timezone.utc) + timedelta(days=7)

    # --- 5c. display_name (plan §6.2 + T0-4) ---
    #
    # Task-naming decision tree (pure logic in src.services.jobs.display_name):
    #   Branch 1: YouTube URL → "油管视频 YYYY-MM-DD NNN"
    #   Branch 2: reserved for compatibility with older plan wording
    #   Branch 3: local upload + Chinese filename → truncate stem
    #   Branch 4: otherwise → "上传视频 YYYY-MM-DD NNN"
    # Collision with the user's existing display_names appends _xxxx.
    #
    # Anonymous / legacy paths (``user is None``) skip generation — the Job
    # API will keep display_name=NULL, and ``getJobDisplayTitle`` on the
    # frontend will still fall back to the slug / video-id chain.
    generated_display_name: str | None = None
    if user is not None:
        local_filename_hint: str | None = None
        if source_type == "local_video" and isinstance(source, dict):
            fn = source.get("filename")
            if isinstance(fn, str) and fn.strip():
                local_filename_hint = fn.strip()

        display_name_ctx = DisplayNameContext(
            source_type=source_type,
            source_ref=source_value,
            user_id=str(user.id),
            # Server-side UTC date. A precise per-user local-timezone date
            # would need a frontend signal; until then, the 上传视频
            # YYYY-MM-DD sequence is "UTC day at submit time" — good enough
            # for non-authoritative display.
            user_local_date=datetime.now(timezone.utc).date(),
            youtube_title=probed_title,
            local_filename=local_filename_hint,
        )

        async def _fetch_existing(uid: str, _db=db) -> set[str]:
            return await _fetch_user_existing_display_names(_db, uid)

        async def _fetch_counter(uid: str, d: date, _db=db, _source_type=source_type) -> int:
            return await _fetch_user_branch4_sequence_today(_db, uid, d, _source_type)

        try:
            generated_display_name = await compute_display_name(
                display_name_ctx,
                fetch_existing_names=_fetch_existing,
                fetch_branch4_sequence_today=_fetch_counter,
            )
        except Exception as exc:  # pragma: no cover — defensive
            # Generation is never load-bearing: on failure, leave
            # display_name NULL and let the frontend fallback chain
            # (title → slug → videoId → "未命名视频") take over.
            logger.warning(
                "display_name generation failed for user=%s: %s — proceeding without",
                user.id, exc,
            )
            generated_display_name = None

    # ``display_name`` is the existing user-facing Chinese task title chain:
    # YouTube gets a temporary Chinese placeholder and then S2 can replace it
    # with a content-aware Chinese title; local uploads use a Chinese filename
    # when available, otherwise the upload placeholder. Store it as the fallback
    # source title so non-YouTube voices also carry useful provenance.
    if generated_display_name and not source_metadata.get("source_video_title"):
        source_metadata["source_video_title"] = generated_display_name

    # Inject policy + snapshot fields into upstream request
    if policy:
        request_data.update(policy)
    request_data["estimated_duration_seconds"] = estimated_duration_seconds
    request_data["quota_state"] = "none"
    request_data["create_idempotency_key"] = idempotency_key
    if source_content_hash:
        request_data["source_content_hash"] = source_content_hash
    for key, value in source_metadata.items():
        if value is not None:
            request_data[key] = value
    if job_expires_at is not None:
        request_data["expires_at"] = job_expires_at.isoformat()
    else:
        request_data.pop("expires_at", None)
    if generated_display_name:
        request_data["display_name"] = generated_display_name
    # Inject user_id so Job API can build user-isolated workspace_dir
    if user is not None:
        request_data["user_id"] = str(user.id)
    # PR#3C-b3g: forward smart_consent verbatim so Job API persists it
    # on JobRecord; pipeline reads via _snap("smart_consent"). The
    # smart_consent_payload was already filtered above (only set when
    # service_mode==smart and the body field is a dict) — anything
    # else stays out of the upstream request to keep JobRecord clean.
    if smart_consent_payload is not None:
        request_data["smart_consent"] = smart_consent_payload

    # Phase 4.3a §3.2: forward express_consent + parse_error（Codex GitHub
    # PR #17 复审 P2-1：先清客户端夹带的，再只写回 validated 值）。
    _apply_validated_express_consent(
        request_data,
        express_consent_payload=express_consent_payload,
        express_consent_parse_error=express_consent_parse_error,
    )

    # Phase 2a LAUNCH GATE: forward only the server-validated free_consent (with
    # the authoritative server_confirmed_at); strip any client-embedded value so a
    # forged consent / server_confirmed_at can't reach the pipeline or audit, and
    # a non-free job can't smuggle a free_consent.
    request_data.pop("free_consent", None)
    if free_consent_payload is not None:
        request_data["free_consent"] = free_consent_payload

    _smart_initial_state: dict[str, object] | None = None
    _smart_clone_create_reservation_id: str | None = None

    async def _release_smart_clone_create_reservation(reason: str) -> None:
        nonlocal _smart_clone_create_reservation_id
        if not _smart_clone_create_reservation_id:
            return
        reservation_id = _smart_clone_create_reservation_id
        _smart_clone_create_reservation_id = None
        try:
            outcome = await settle_smart_clone_reservation(
                db,
                reservation_id=reservation_id,
                service_mode="smart",
            )
            logger.info(
                "released smart clone create reservation=%s reason=%s outcome=%s",
                reservation_id,
                reason,
                getattr(outcome, "status", None),
            )
        except Exception:
            logger.warning(
                "failed to release smart clone create reservation=%s reason=%s",
                reservation_id,
                reason,
                exc_info=True,
            )

    if service_mode == "smart" and user is not None:
        request_data["job_id"] = f"job_{_uuid.uuid4().hex}"
        auto_clone_requested = (
            smart_consent_payload is not None
            and smart_consent_payload.get("auto_voice_clone") is True
        )
        consent_allows_clone = auto_clone_requested and smart_paid_clone_confirmed
        try:
            from admin_settings import load_settings

            _admin_for_clone = load_settings()
            admin_clone_enabled = bool(
                getattr(_admin_for_clone, "smart_auto_clone_enabled", True)
            )
            if auto_clone_requested and admin_clone_enabled and not smart_paid_clone_confirmed:
                _smart_initial_state = {
                    "smart_clone_credit_reserved": False,
                    "smart_clone_reservation_deny_reason": (
                        "paid_clone_confirmation_required"
                    ),
                }
            elif consent_allows_clone and admin_clone_enabled:
                _library_cap = (
                    999_999
                    if is_admin
                    else int(
                        getattr(
                            _admin_for_clone,
                            "smart_user_voice_clone_cap",
                            30,
                        )
                        or 30
                    )
                )
                _base_est_min = (
                    estimated_duration_seconds / 60.0
                    if estimated_duration_seconds
                    else None
                )
                _base_credits = estimate_credits(
                    _base_est_min,
                    service_mode=service_mode,
                    quality_tier=policy.get("quality_tier", "standard"),
                )
                _clone_cost_credits = _get_smart_clone_cost_credits()
                await ensure_credit_buckets_for_user(db, user=user)
                _reserve_outcome = await reserve_smart_clone_credit(
                    db,
                    user_id=user.id,
                    task_id=str(request_data["job_id"]),
                    amount_credits=_clone_cost_credits,
                    ttl_minutes=max(
                        _SMART_CLONE_CREATE_RESERVATION_TTL_MINUTES,
                        int(
                            getattr(
                                _admin_for_clone,
                                "smart_clone_create_reservation_ttl_minutes",
                                _SMART_CLONE_CREATE_RESERVATION_TTL_MINUTES,
                            )
                            or _SMART_CLONE_CREATE_RESERVATION_TTL_MINUTES
                        ),
                    ),
                    library_cap=_library_cap,
                    required_available_credits=(
                        int(_base_credits) + _clone_cost_credits
                        if _base_credits > 0
                        else None
                    ),
                )
                if (
                    _reserve_outcome.status == "reserved"
                    and _reserve_outcome.reservation_id
                ):
                    _smart_clone_create_reservation_id = (
                        _reserve_outcome.reservation_id
                    )
                    _smart_initial_state = {
                        "smart_clone_credit_reserved": True,
                        "smart_clone_reservation_id": _reserve_outcome.reservation_id,
                    }
                else:
                    _smart_initial_state = {
                        "smart_clone_credit_reserved": False,
                        "smart_clone_reservation_deny_reason": (
                            _reserve_outcome.deny_reason
                            or _reserve_outcome.status
                        ),
                    }
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                logger.warning(
                    "failed to rollback smart clone reservation failure "
                    "job_id=%s user=%s",
                    request_data.get("job_id"),
                    getattr(user, "id", None),
                    exc_info=True,
                )
            logger.warning(
                "smart clone reservation failed before create; "
                "continuing without paid clone job_id=%s user=%s: %s",
                request_data.get("job_id"),
                getattr(user, "id", None),
                exc,
            )
            _smart_initial_state = {
                "smart_clone_credit_reserved": False,
                "smart_clone_reservation_deny_reason": "reserve_failed",
            }
        if _smart_initial_state is not None:
            request_data["smart_state"] = dict(_smart_initial_state)

    # Phase 2a free tier — atomic daily-quota admission BEFORE the upstream
    # forward (CodeX: before expensive stages; independent of legacy quota).
    # Reserve now; consume on accept / release on reject below. The reserved
    # row has a TTL + inline-expire, so a crashed create frees the slot.
    _free_reserved = False
    if service_mode == "free" and user is not None:
        from free_service_quota import (
            FREE_DAILY_CAP,
            reserve_free_daily,
            shanghai_day_key,
        )
        _free_outcome = await reserve_free_daily(
            db,
            user_id=user.id,
            usage_date=shanghai_day_key(),
            idempotency_key=idempotency_key,
            daily_cap=FREE_DAILY_CAP,
        )
        if _free_outcome.status != "reserved":
            return _error_response(
                403,
                "free_daily_quota_exceeded",
                "免费版每天 1 次，今日已用完，请明天再试或升级。",
                {"requested_mode": "free", "reason": _free_outcome.deny_reason},
            )
        _free_reserved = True

    # P3e-2b: smart 预览克隆 600 点 reserve（Option C：forward 前用**预生成**
    # job_id 做 reserve(task_id=job_id) + 把 reservation marker 塞
    # request_data['smart_state'] 一并 forward → Job API 用预供 job_id（P3e-2a）
    # 并存 smart_state → pipeline `_snap` 读 + mirror→finalizer marker-gate。
    # 同 free-tier reserve 模式：forward 前 reserve、TTL+inline-expire、crash
    # 自动释放（smart_clone_reservation_sweeper 兜底）。
    # **降级一律不阻断**（CLAUDE.md 免费触点不静默降级）：disabled/denied/error
    # → 不写 marker → pipeline 退预设；skip 原因记 `_smart_clone_skipped_reason`
    # （审计可见；前端降级提示响应字段 = P3e-4）。
    # 触发条件 = smart + 用户 consent.auto_voice_clone + admin
    # smart_preview_clone_enabled。reservation 真有效性由 register-billed
    # endpoint 写 billing event 时原子再校验（gateway 不在此扣费，只预留）。
    _smart_clone_skipped_reason: str | None = None
    _smart_clone_reservation_id: str | None = None
    _smart_pre_job_id: str | None = None  # 对抗性复核 P1-C：forward 后校验一致性
    if (
        service_mode == "smart"
        and user is not None
        and isinstance(request_data.get("smart_consent"), dict)
        and request_data["smart_consent"].get("auto_voice_clone") is True
    ):
        from admin_settings import load_settings as _load_admin_for_clone
        _admin_for_clone = _load_admin_for_clone()
        if getattr(_admin_for_clone, "smart_preview_clone_enabled", False) is not True:
            _smart_clone_skipped_reason = "clone_disabled"
        else:
            try:
                from smart_clone_reservation_service import (
                    reserve_smart_clone_credit as _reserve_smart_clone,
                )
                # P1-A + CodeX 终审 P1#2：pre_job_id 决定性派生自
                # **(user.id, idempotency_key)** → 同用户同 key 重试复用同
                # task_id（reserve 幂等，根治双预留）；**含 user.id namespace**
                # 防两用户夹带同 idempotency_key 撞同一 reservation（reservation
                # 幂等查只看 (task_id,purpose)）。sha256 取 32 hex 小写 → 匹配
                # submit_job ^job_[0-9a-f]{32}$。
                _pre_job_id = "job_" + hashlib.sha256(
                    f"{user.id}:{idempotency_key}".encode("utf-8")
                ).hexdigest()[:32]
                # CodeX 终审 P1#3：终态重放幂等——同 (user, key) 派生同
                # _pre_job_id；reserve 前查已有 PG job，存在说明是重放 → **不再
                # reserve**（防 reservation 终态 captured/released 后重放再扣 600；
                # 幂等查只挡 active reserved，挡不住终态后重放）。仍把 deterministic
                # job_id forward 让 Job API existing-check 去重（不建重复 job）。
                _smart_existing_job = (
                    await db.execute(select(Job).where(Job.job_id == _pre_job_id))
                ).scalar_one_or_none()
                if _smart_existing_job is not None:
                    # CodeX 复审 P1#3：重放（同 user+key → 同 _pre_job_id，已有 PG
                    # job）→ skip reserve（防终态后重放再扣 600）。**不**回 supply
                    # _pre_job_id —— 否则 Job API submit_job 会 ``save_job`` 覆盖 +
                    # ``runner.start`` **重启已有 job**（重跑付费 workflow / 污染
                    # 产物状态）。留空 → Job API mint 新 id → 新预设 job（无
                    # reservation → pipeline gate 关 → preset 无克隆）。重复 job 是
                    # 既有"双提交无 create-level idempotency dedup"基线行为，本修
                    # 只确保**不覆盖重启原 job**、**不再扣 600**。
                    _smart_clone_skipped_reason = "duplicate_create"
                else:
                    await ensure_credit_buckets_for_user(db, user=user)
                    _smart_lib_cap = int(
                        getattr(_admin_for_clone, "smart_user_voice_clone_cap", 30) or 30
                    )
                    _smart_resv = await _reserve_smart_clone(
                        db,
                        user_id=user.id,
                        task_id=_pre_job_id,
                        amount_credits=600,
                        ttl_minutes=60,
                        library_cap=_smart_lib_cap,
                    )
                    if _smart_resv.status == "reserved":
                        _smart_clone_reservation_id = _smart_resv.reservation_id
                        _smart_pre_job_id = _pre_job_id
                        # Option C：预生成 job_id + reservation marker 一并 forward。
                        request_data["job_id"] = _pre_job_id
                        request_data["smart_state"] = {
                            "smart_clone_reservation_id": _smart_resv.reservation_id,
                            "smart_clone_credit_reserved": True,
                        }
                    else:
                        # denied(insufficient_credits / voice_library_full) /
                        # user_not_found → 不阻断、走预设。
                        _smart_clone_skipped_reason = (
                            _smart_resv.deny_reason or "user_not_found"
                        )
            except Exception as _smart_resv_exc:  # noqa: BLE001 — reserve 故障不阻断
                logger.warning(
                    "smart clone preview reserve failed user=%s: %s",
                    user.id, _smart_resv_exc,
                )
                _smart_clone_skipped_reason = "reserve_error"
        if _smart_clone_skipped_reason:
            logger.info(
                "smart clone preview reserve skipped user=%s reason=%s",
                user.id, _smart_clone_skipped_reason,
            )

    # Forward to upstream with modified body
    # CodeX 复审 P2：proxy_request 抛异常（httpx timeout / client 故障）会绕过
    # 下方非 2xx/no-job_id/big-except 的 release → 已预留的 smart clone 600 锁到
    # TTL。包 try/except：异常时及时释放后再 re-raise（保持既有错误传播）。
    try:
        upstream_response = await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
            override_body=json.dumps(request_data, ensure_ascii=False).encode("utf-8"),
        )
    except Exception:
        await _release_smart_clone_create_reservation("proxy_exception")
        if _smart_clone_reservation_id:
            await _release_smart_clone_reservation_on_create_failure(
                db, _smart_clone_reservation_id
            )
            _smart_clone_reservation_id = None
        raise

    # --- 6. Record in PostgreSQL ---
    job_id = None
    logger.info("intercept_create_job: upstream status=%s user=%s",
                upstream_response.status_code, user.id if user else None)
    if upstream_response.status_code not in (200, 201, 202):
        await _release_smart_clone_create_reservation("upstream_rejected")
        if _smart_clone_reservation_id:
            await _release_smart_clone_reservation_on_create_failure(
                db, _smart_clone_reservation_id
            )
            _smart_clone_reservation_id = None
    if upstream_response.status_code in (200, 201, 202) and user is not None:
        try:
            raw_body = upstream_response.body
            data = json.loads(raw_body)
            job_data = data.get("job") or data
            job_id = job_data.get("job_id")
            # P3e-2b 对抗性复核 P1-C：Job API 实际用的 job_id 必须 == 预生成的
            # _smart_pre_job_id（reservation.task_id），否则 reservation 关联断裂
            # （register-billed 按 job_id 查 reservation 查不到 → 漏结算 + 孤儿
            # voice）。极低概率（uuid hex 恒匹配 submit_job pattern）；防 proxy/
            # response 异常。不一致 → 释放 + loud error（fail-safe：用户不被扣）。
            if (
                _smart_clone_reservation_id
                and _smart_pre_job_id
                and job_id
                and str(job_id) != str(_smart_pre_job_id)
            ):
                logger.error(
                    "smart clone job_id MISMATCH pre=%s actual=%s — releasing "
                    "reservation %s (linkage broken; user not charged)",
                    _smart_pre_job_id, job_id, _smart_clone_reservation_id,
                )
                await _release_smart_clone_reservation_on_create_failure(
                    db, _smart_clone_reservation_id
                )
                _smart_clone_reservation_id = None
            if job_id:
                existing = await db.execute(select(Job).where(Job.job_id == job_id))
                if existing.scalar_one_or_none() is None:
                    job = Job(
                        job_id=job_id,
                        user_id=user.id,
                        source_type=job_data.get("source_type", "youtube_url"),
                        source_ref=job_data.get("youtube_url") or job_data.get("source_ref", ""),
                        source_content_hash=job_data.get("source_content_hash") or source_content_hash,
                        title=str(
                            job_data.get("source_video_title")
                            or source_metadata.get("source_video_title")
                            or job_data.get("display_name")
                            or generated_display_name
                            or job_data.get("title", "")
                        )[:512],
                        speakers=job_data.get("speakers", "auto"),
                        status=job_data.get("status", "queued"),
                        current_stage=job_data.get("current_stage"),
                        project_dir=job_data.get("project_dir"),
                        # --- Language fields: the create-path resolved pair
                        # (PR-A part 2 §3). Defaults to the GA pair (en->zh-CN)
                        # when the request carries no language fields; otherwise
                        # the validated + entitlement-gated pair. ---
                        source_language=resolved_pair.source_language,
                        target_language=resolved_pair.target_language,
                        language_pair=resolved_pair.language_pair,
                        # --- Full execution snapshot ---
                        service_mode=policy.get("service_mode"),
                        tts_provider=policy.get("tts_provider"),
                        tts_model=policy.get("tts_model"),
                        requires_review=policy.get("requires_review"),
                        voice_clone_enabled=policy.get("voice_clone_enabled"),
                        voice_strategy=policy.get("voice_strategy"),
                        plan_code_snapshot=policy.get("plan_code_snapshot"),
                        role_snapshot=policy.get("role_snapshot"),
                        estimated_duration_seconds=estimated_duration_seconds,
                        source_duration_seconds=None,
                        quota_cost=1,
                        quota_state="none",
                        create_idempotency_key=idempotency_key,
                        # Friendly title produced by display_name_orchestrator;
                        # prefer the upstream echo so we stay in sync even if
                        # Job API ever normalises the value.
                        display_name=job_data.get("display_name") or generated_display_name,
                        expires_at=job_expires_at,
                        smart_state=(
                            job_data.get("smart_state")
                            if isinstance(job_data.get("smart_state"), dict)
                            else request_data.get("smart_state")
                            if isinstance(request_data.get("smart_state"), dict)
                            else None
                        ),
                    )
                    db.add(job)
                    # Reserve quota in the same transaction.
                    # Phase 2a free tier (CodeX P1): free-SERVICE jobs use the
                    # independent daily ledger (reserved before forward), NOT the
                    # legacy free-PLAN quota — skip reserve_quota so they never
                    # consume users.free_jobs_quota_used.
                    reserved = True if service_mode == "free" else await reserve_quota(db, user.id, job)
                    if not reserved and user_plan == "free":
                        # Quota reservation failed — rollback local record
                        await db.rollback()
                        # P3e-2b P1-B：任务没建 → 释放已预留的 smart clone 600。
                        await _release_smart_clone_reservation_on_create_failure(
                            db, _smart_clone_reservation_id
                        )
                        # Compensate: cancel upstream job to prevent orphan
                        await _compensate_upstream_job(job_id)
                        await _release_smart_clone_create_reservation("quota_failed")
                        return _error_response(
                            403, "quota_exhausted",
                            "免费额度已用完，无法创建任务。",
                            {"job_id": job_id},
                        )

                    # Credits are now a live gate for paid work. If we know
                    # the duration at creation time, reserve before returning
                    # success; otherwise update_source_metadata performs the
                    # same hard reserve once the pipeline reports duration.
                    est_min = (estimated_duration_seconds / 60.0) if estimated_duration_seconds else None
                    job.estimated_minutes = est_min
                    _quality_tier = policy.get("quality_tier", "standard")
                    shadow_credits = estimate_credits(
                        est_min, service_mode=service_mode, quality_tier=_quality_tier,
                    )
                    job.metering_snapshot = {
                        "credits_estimated": shadow_credits if shadow_credits > 0 else None,
                        "service_mode": service_mode,
                        "quality_tier": _quality_tier,
                        "tts_provider": policy.get("tts_provider"),
                        "tts_model": policy.get("tts_model"),
                        # Language pair for cost-by-pair aggregation — consumed
                        # by cost_management.py window rollup (PR-A part 2 §6).
                        "language_pair": job.language_pair,
                    }
                    if shadow_credits > 0:
                        try:
                            await ensure_credit_buckets_for_user(db, user=user)
                            await reserve_credits_or_raise(
                                db,
                                user_id=user.id,
                                job_id=job_id,
                                estimated_credits=shadow_credits,
                                service_mode=service_mode,
                            )
                        except InsufficientCreditsError as exc:
                            await db.rollback()
                            # P3e-2b P1-B：分钟点不足任务没建 → 释放 smart clone
                            # 600（最现实的孤儿场景：够克隆 600 但不够分钟点）。
                            await _release_smart_clone_reservation_on_create_failure(
                                db, _smart_clone_reservation_id
                            )
                            await _compensate_upstream_job(job_id)
                            await _release_smart_clone_create_reservation("base_credit_insufficient")
                            return _insufficient_credits_response(exc)
                        except Exception as exc:
                            logger.exception("credit reserve failed for job %s: %s", job_id, exc)
                            await db.rollback()
                            await _release_smart_clone_reservation_on_create_failure(
                                db, _smart_clone_reservation_id
                            )
                            await _compensate_upstream_job(job_id)
                            await _release_smart_clone_create_reservation("base_credit_reserve_failed")
                            return _error_response(
                                500,
                                "credit_reserve_failed",
                                "点数预扣失败，任务已停止。请稍后重试。",
                                {"job_id": job_id},
                            )

                    await db.commit()
                    logger.info("Job %s recorded (mode=%s, plan=%s, quota=%s)",
                                job_id, service_mode, user_plan, job.quota_state)
                else:
                    logger.info("Job %s already in DB, skipping", job_id)
            else:
                logger.warning("No job_id in upstream response")
                await _release_smart_clone_create_reservation("missing_job_id")
                # P3e-2b P2#4：2xx 但无 job_id（异常）→ reservation 关联不上真
                # job → 释放（sweeper 兜底）。
                if _smart_clone_reservation_id:
                    await _release_smart_clone_reservation_on_create_failure(
                        db, _smart_clone_reservation_id
                    )
                    _smart_clone_reservation_id = None
        except Exception as exc:
            logger.exception("Failed to record job %s in DB: %s", job_id, exc)
            try:
                await db.rollback()
            except Exception:
                pass
            await _release_smart_clone_create_reservation("pg_record_failed")
            # P3e-2b P2#4：2xx 后本地解析/PG 记录失败 → 任务未正确入库 → 释放
            # smart clone reservation（避免挂满 60min TTL；sweeper 仍兜底）。
            if _smart_clone_reservation_id:
                await _release_smart_clone_reservation_on_create_failure(
                    db, _smart_clone_reservation_id
                )
                _smart_clone_reservation_id = None

    # Phase 2a free tier — settle the daily reservation: consume on upstream
    # accept, release on reject. TTL + inline-expire is the safety net for
    # rarer paths (forward exception / local DB error after reserve).
    if service_mode == "free" and _free_reserved:
        from free_service_quota import consume_free_daily, release_free_daily

        if upstream_response.status_code in (200, 201, 202) and job_id:
            await consume_free_daily(db, user_id=user.id, idempotency_key=idempotency_key, job_id=job_id)
        else:
            await release_free_daily(db, user_id=user.id, idempotency_key=idempotency_key, reason="upstream_rejected")

    # Chunked upload claim 回写（plan 2026-06-11 §3.8 r3）：job create 成功
    # 后把 job_id 写回 upload state.json。claim 后终文件归现有 uploads 生命
    # 周期管理；未 claim 的 ready 文件由 sweeper 按 ready_ttl 回收。
    # best-effort：claim 失败不影响已创建的任务（终文件已在 final_path，
    # 最坏情况是 sweeper 误回收前用户已无引用——log warning 供排查）。
    if (
        chunked_upload_ref_id
        and user is not None
        and job_id
        and upstream_response.status_code in (200, 201, 202)
    ):
        try:
            from chunked_upload_store import claim_upload

            claimed = await asyncio.to_thread(
                claim_upload,
                user_id=str(user.id),
                upload_id=chunked_upload_ref_id,
                job_id=str(job_id),
            )
            if not claimed:
                logger.warning(
                    "chunked upload claim failed: upload=%.8s job=%s",
                    chunked_upload_ref_id, job_id,
                )
        except Exception:
            logger.warning(
                "chunked upload claim errored: upload=%.8s job=%s",
                chunked_upload_ref_id, job_id, exc_info=True,
            )

    # Wrap upstream conflict/error into structured error
    if upstream_response.status_code == 409:
        try:
            err_body = json.loads(upstream_response.body)
            err_msg = err_body.get("error", "任务冲突")
        except Exception:
            err_msg = "任务创建冲突"
        return _error_response(409, "job_create_conflict", err_msg)

    return upstream_response


def _job_json_record_from_payload(job_id: str, payload: dict) -> JobJsonRecord:
    upstream_edit_generation = payload.get("edit_generation")
    try:
        upstream_edit_generation_int = (
            int(upstream_edit_generation)
            if upstream_edit_generation is not None
            else None
        )
    except (TypeError, ValueError):
        upstream_edit_generation_int = None
    return JobJsonRecord(
        job_id=job_id,
        status=str(payload.get("status") or ""),
        completed_at=parse_iso_timestamp(payload.get("completed_at")),
        project_dir=payload.get("project_dir")
        if isinstance(payload.get("project_dir"), str)
        else None,
        current_stage=payload.get("current_stage")
        if isinstance(payload.get("current_stage"), str)
        else None,
        edit_generation=upstream_edit_generation_int,
        jianying_draft_zip_path=None,
        service_mode=payload.get("service_mode")
        if isinstance(payload.get("service_mode"), str)
        else None,
        # Smart MVP P2 mirror — pipeline writes via [SMART_STATE] marker
        # → JobRecord.smart_state → list-jobs payload → here → DB mirror
        # → settle dispatcher reads job.smart_state.credits_policy.
        smart_state=payload.get("smart_state")
        if isinstance(payload.get("smart_state"), dict)
        else None,
    )


async def intercept_get_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs/{job_id} — verify ownership, then forward. No auto-claim."""
    await _verify_job_ownership(job_id, db, user)
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )
    if not settings.auth_required or user is None:
        return upstream_response
    if not (200 <= upstream_response.status_code < 300):
        return upstream_response
    try:
        payload = json.loads(upstream_response.body)
        if not isinstance(payload, dict):
            return upstream_response
        result = await db.execute(
            select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
        )
        db_job = result.scalar_one_or_none()
        # Plan 2026-05-08 §16: emit notification first while the Gateway row
        # still has the previous status, then route the upstream payload through
        # the same mirror helper used by list-jobs and the R2 sweeper. This
        # keeps terminal status + quota + credit settlement behind one
        # idempotent entrypoint; the notification helper is intentionally
        # notification-only and must not write db_job.status.
        try:
            upstream_status = payload.get("status") if isinstance(payload, dict) else None
            from notifications_helpers import maybe_dispatch_job_transition
            await maybe_dispatch_job_transition(
                db,
                db_job=db_job,
                upstream_status=upstream_status,
            )
            if db_job is not None:
                await mirror_job_terminal_state(
                    db,
                    db_job,
                    _job_json_record_from_payload(job_id, payload),
                )
            # Commit notification rows plus any mirror/settlement changes here
            # because the surrounding handler returns through several paths.
            await db.commit()
        except Exception:
            logger.debug("job detail mirror/notification hook failed", exc_info=True)
        payload = _merge_gateway_job_metadata(payload, db_job)
        # Plan §10.4 deepening: redact progress_message + error_summary.message
        # for non-admin. Admin path is no-op inside the helper.
        _redact_job_record_in_place(payload, user)
        return Response(
            content=json.dumps(payload, ensure_ascii=False),
            status_code=upstream_response.status_code,
            headers={"content-type": "application/json"},
        )
    except Exception:
        logger.exception("Failed to merge gateway metadata for job %s", job_id)
        return upstream_response


async def intercept_job_subresource(
    request: Request,
    job_id: str,
    subpath: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET/POST /job-api/jobs/{job_id}/{subpath} — verify ownership, then forward.

    Covers: logs, artifacts, result-summary, continue, review/*, download/*, etc.
    """
    await _verify_job_ownership(job_id, db, user)

    if subpath == "review/voice/clone":
        raise HTTPException(
            status_code=410,
            detail=(
                "The legacy review voice clone endpoint has been removed. "
                "Use POST /job-api/jobs/{job_id}/voice-clone."
            ),
        )

    if subpath == "review/voice/preview" and request.method == "POST":
        return await _post_edit_voice_preview_with_policy(request, job_id, db, user)

    # --- D25 server-side log redaction for non-admin users ---
    # Intercepts GET /logs BEFORE the generic proxy. Admins pass through
    # unchanged. Non-admins get events[].message + lines[] filtered through
    # the registry-aware redactor so provider names / UUIDs / internal IDs
    # are stripped. Frontend's ``isAdmin`` hide-LogViewer UI is only cosmetic;
    # this is the authoritative enforcement point.
    if subpath == "logs" and request.method == "GET":
        return await _serve_redacted_logs(request, user)

    # T2: state-transition endpoints need concurrency control at Gateway layer.
    # For POST /continue we hold a row lock across the upstream proxy call so
    # that (a) concurrent continues serialize at the DB, and (b) we only
    # promote status to 'running' AFTER we see upstream accepted the continue.
    # If upstream returns 409 / 5xx / times out, the row stays in
    # 'waiting_for_review' — the user can retry without being blocked by a
    # stale 'running' we wrote speculatively.
    if subpath == "continue" and request.method == "POST":
        return await _continue_with_gateway_lock(request, job_id, db)

    # V3-6 fix (2026-04-14): voice-selection/approve 要同步把用户选择的
    # MiniMax 音质档（turbo=高级/hd=旗舰）写回 Gateway DB 的
    # Job.tts_model 和 Job.metering_snapshot.quality_tier。
    # 否则 UI 显示 30/50 点/分钟，但 settle 时读到硬编码 standard=15
    # 就永远按最低档扣点，定价完全失效。
    if subpath == "review/voice-selection/approve" and request.method == "POST":
        return await _approve_voice_selection_with_quality_sync(request, job_id, db)

    # --- Studio post-edit endpoints (plan 2026-04-18 D29) ---
    # Two groups, both gated on the feature flag:
    #   1. State transitions (enter-edit / editing/cancel / editing/commit)
    #      get a FOR UPDATE row lock + conditional Gateway-DB sync.
    #   2. Segment mutations (segments/{sid}/update | /status) are editing-
    #      state job-scoped; no row lock is needed (upstream validates the
    #      editing state and refreshes editing_touched_at). Feature flag
    #      still gates to keep the surface fully dark when disabled.
    if request.method == "POST" and _is_post_edit_mutation_subpath(subpath):
        if not settings.enable_post_edit:
            # D29: refuse at HTTP level so probes can't distinguish "feature
            # disabled" from "endpoint unknown". Frontend learns flag state
            # via entitlements and doesn't expose the call when off.
            return _error_response(
                404,
                "post_edit_disabled",
                "Post-edit workflow is not enabled on this deployment.",
            )
        if subpath in _POST_EDIT_TRANSITION_SUBPATHS:
            return await _editing_transition_with_lock(
                request, job_id, db, user, subpath=subpath,
            )
        return await _post_edit_mutation_with_policy(
            request, job_id, db, user, subpath=subpath,
        )

    # --- Jianying draft endpoints (plan §11.7 K6) ---
    # POST /jobs/{id}/generate-jianying-draft and
    # GET  /jobs/{id}/jianying-draft-status require X-Internal-Key to
    # reach the Job API. Ownership is already verified above by
    # _verify_job_ownership. We inject internal_headers() here; the
    # service-mode (Studio-only) gate lives at the Job API layer (K4/K5).
    if subpath in _JIANYING_DRAFT_SUBPATHS:
        from internal_auth import internal_headers
        return await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
            extra_headers=internal_headers(),
        )

    # --- R2 download redirect (plan 2026-04-23 + plan 2026-05-07 §4.7) ---
    # Surface: GET /download/{key} for any downloadable artifact key, only
    # when AVT_DOWNLOAD_REDIRECT_BACKEND=r2. Any R2 error silently returns
    # None → caller falls through to byte-passthrough so the user never
    # sees an R2-related failure. See gateway/storage/backend_router.py
    # for the fallback contract.
    download_match = (
        _DOWNLOAD_KEY_RE.match(subpath)
        if request.method == "GET"
        else None
    )
    if download_match is not None:
        artifact_key = download_match.group("key")
        redirect_url, redirect_kind = await _resolve_r2_redirect(
            db, job_id, artifact_key=artifact_key,
        )
        if redirect_url is not None:
            # 302 (not 307) — matches the pattern of existing CDN redirects
            # and plays nicely with every <a download> client we've seen.
            _emit_download_event(
                job_id,
                # registry path is the new (Stage A) flow; legacy lazy
                # upload still fires the original event name so the
                # rollout dashboard can split the two populations.
                "download.redirect.r2_registry"
                if redirect_kind == "registry"
                else "download.redirect.r2",
                message="Download redirected to R2",
                payload={"artifact_key": artifact_key, "backend": "r2"},
            )
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=redirect_url, status_code=302)
        try:
            from storage.backend_router import is_r2_enabled
            r2_enabled = is_r2_enabled()
        except Exception:
            r2_enabled = False
        _emit_download_event(
            job_id,
            "download.fallback.local" if r2_enabled else "download.local.direct",
            message=(
                "Download fell back to local source"
                if r2_enabled
                else "Download served from local source"
            ),
            payload={"artifact_key": artifact_key, "backend": "local"},
        )

    # --- /stream/{kind} R2 redirect (plan 2026-05-07 §11.3 C3-C4, Stage C) ---
    # Same fallback contract as /download/{key}: any R2 / DB error returns
    # ``(None, "")`` and we fall through to the local Range-streaming path
    # via proxy_request. Reuses the existing AVT_DOWNLOAD_REDIRECT_BACKEND
    # flag — no separate stream feature flag, because stream and download
    # serve the same underlying artifacts and have the same risk profile.
    stream_match = (
        _STREAM_KIND_RE.match(subpath)
        if request.method == "GET"
        else None
    )
    if stream_match is not None:
        stream_kind = stream_match.group("kind")
        redirect_url, redirect_kind = await _resolve_r2_stream_redirect(
            db, job_id, stream_kind=stream_kind,
        )
        if redirect_url is not None:
            _emit_download_event(
                job_id,
                "stream.redirect.r2_registry"
                if redirect_kind == "registry"
                else "stream.redirect.r2",
                message="Stream redirected to R2",
                payload={"stream_kind": stream_kind, "backend": "r2"},
            )
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=redirect_url, status_code=302)
        try:
            from storage.backend_router import is_r2_enabled
            r2_enabled = is_r2_enabled()
        except Exception:
            r2_enabled = False
        _emit_download_event(
            job_id,
            "stream.fallback.local" if r2_enabled else "stream.local.direct",
            message=(
                "Stream fell back to local source"
                if r2_enabled
                else "Stream served from local source"
            ),
            payload={"stream_kind": stream_kind, "backend": "local"},
        )

    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def _resolve_r2_redirect(
    db: AsyncSession,
    job_id: str,
    *,
    artifact_key: str,
) -> tuple[str | None, str]:
    """Resolve an R2 302 target for any downloadable artifact key.

    Returns ``(url, kind)`` where ``kind`` is:
      - ``"registry"``: served from PG-resident r2_artifacts entry
        (Stage A path; manifest-independent so cleanup-after-push
        deletions don't break downloads).
      - ``"lazy"``: served via the legacy lazy-upload path
        (publish.dubbed_video on edit_generation=0 only).
      - ``""``: caller should fall back to local byte-passthrough.

    Plan: 2026-05-07 §4.7. Never raises — every R2 / DB error is
    swallowed and reported as ``(None, "")``.
    """
    try:
        from storage.backend_router import is_r2_enabled
        from services.r2_publisher_lib.downloadable_keys import (
            download_keys_for,
            effective_policy_mode,
        )
    except Exception as exc:  # pragma: no cover - boto3/storage missing
        logger.warning(
            "storage package import failed (%s); falling back to local", exc,
        )
        return None, ""

    if not is_r2_enabled():
        return None, ""

    try:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "r2 redirect: job lookup failed job=%s (%s); falling back",
            job_id, exc,
        )
        return None, ""

    if job is None or not job.project_dir:
        # Legacy / still-queueing job — let Job API surface the 404.
        return None, ""

    # P2.1: shared allowlist gate. If the requested key isn't in the
    # job's permission set, return None and let Job API produce the
    # canonical 403 / 404. Doing the check here means Gateway never
    # short-circuits a 302 for a key the user shouldn't see.
    # plan 2026-06-12 §C ⑥：策略档经 effective_policy_mode——匿名预览任务
    # （含匿名 express，service_mode=="express"）零下载 key，不进 R2 302。
    if artifact_key not in download_keys_for(
        effective_policy_mode(
            job.service_mode, bool(getattr(job, "is_anonymous_preview", False))
        )
    ):
        return None, ""

    expected_gen = job.edit_generation or 0

    # ---- Registry path (Stage A new) ----
    # Find a matching entry by (artifact_key, edit_generation). The
    # generation guard means an overwrite (gen N → N+1) cannot be served
    # from a stale entry — the registry was reset to NULL by
    # ``_apply_editing_commit_gateway_side`` and the sweeper hasn't
    # repopulated it for the new generation yet.
    registry_entry = None
    if job.r2_artifacts:
        for item in job.r2_artifacts:
            if (
                item.get("artifact_key") == artifact_key
                and item.get("edit_generation") == expected_gen
            ):
                registry_entry = item
                break

    if registry_entry is not None:
        state = registry_entry.get("state")
        if state in ("pushed", "already_present"):
            r2_key = registry_entry.get("r2_key")
            filename = registry_entry.get("filename") or _derive_download_filename(job)
            content_type = registry_entry.get(
                "content_type", "application/octet-stream",
            )
            if r2_key:
                try:
                    from storage import r2_client
                    # Verify R2 object actually exists. R2 lifecycle / manual
                    # delete / pan-backup archive may have removed it after the
                    # registry entry was written. Presign API never checks
                    # existence — skip HEAD and we 302 to a guaranteed 404.
                    if not r2_client.head_artifact(r2_key):
                        logger.warning(
                            "r2 redirect: registry HEAD miss job=%s key=%s r2_key=%s"
                            " — falling back to local",
                            job_id, artifact_key, r2_key,
                        )
                        return None, ""
                    url = r2_client.generate_presigned_download_url(
                        r2_key, filename, content_type=content_type,
                    )
                    return url, "registry"
                except Exception as exc:
                    logger.warning(
                        "r2 redirect: registry presign failed job=%s key=%s (%s); falling back",
                        job_id, artifact_key, exc,
                    )
                    return None, ""
        # state == "skipped_missing" → caller should 404, not lazy-fallback
        # state == "failed"          → caller should 404 too; lazy would
        #                              just fail the same way
        return None, ""

    # ---- Lazy fallback (P1.3 narrowed) ----
    # Only ``publish.dubbed_video`` ever took the legacy lazy path, and
    # the legacy R2 key shape lacks edit_generation. Allowing it for
    # gen > 0 would HEAD-hit the gen-0 object and serve a stale video.
    # For non-zero generations we return None and let Job API serve
    # whatever it has on disk (or 404).
    if artifact_key != "publish.dubbed_video":
        return None, ""
    if expected_gen != 0:
        logger.info(
            "r2 lazy refused: job=%s edit_generation=%d > 0",
            job_id, expected_gen,
        )
        return None, ""

    url = await _legacy_lazy_resolve_publish_dubbed_video(job_id, job)
    return (url, "lazy") if url is not None else (None, "")


async def _legacy_lazy_resolve_publish_dubbed_video(
    job_id: str, job: Job,
) -> str | None:
    """Old Phase 2 lazy-upload path, restricted to publish.dubbed_video.

    Walks ``manifest.json`` for the local artifact path, then defers to
    ``backend_router.resolve_download_target`` which performs the
    HEAD-or-upload-then-presign sequence. Any failure returns None so
    the caller falls through to the byte-passthrough.
    """
    try:
        from storage.backend_router import resolve_download_target
        from services.manifest_reader import resolve_manifest_artifact_path
        local_path = resolve_manifest_artifact_path(
            Path(job.project_dir), "publish.dubbed_video"
        )
    except Exception as exc:
        logger.warning(
            "r2 lazy: manifest resolve failed job=%s (%s); falling back",
            job_id, exc,
        )
        return None

    if local_path is None:
        # Manifest doesn't list the artifact — Job API will 404 same
        # way it always has.
        return None

    download_filename = _derive_download_filename(job)
    try:
        return resolve_download_target(
            job_id=job_id,
            artifact_key="publish.dubbed_video",
            local_path=local_path,
            download_filename=download_filename,
        )
    except Exception as exc:
        # Defensive: resolve_download_target is documented not to raise,
        # but wrap anyway so a future refactor can't take the endpoint down.
        logger.warning(
            "r2 lazy: resolve_download_target raised job=%s (%s); falling back",
            job_id, exc,
        )
        return None


async def _resolve_r2_stream_redirect(
    db: AsyncSession,
    job_id: str,
    *,
    stream_kind: str,
) -> tuple[str | None, str]:
    """Resolve an R2 302 target for /stream/{kind}.

    Plan: 2026-05-07 §11.3 C3 (Stage C). Mirrors ``_resolve_r2_redirect``
    but without the legacy lazy-upload fallback path — stream is a new
    surface so we don't carry pre-Stage-A baggage; if registry doesn't
    have it, we fall through to Job API's local Range stream
    (src/services/jobs/api.py:447-490).

    Returns ``(url, kind)`` where ``kind`` is:
      - ``"registry"``: served from PG-resident r2_artifacts entry.
      - ``""``: caller should fall back to local byte-passthrough.

    Never raises — every R2 / DB error is swallowed and reported as
    ``(None, "")``.
    """
    try:
        from storage.backend_router import is_r2_enabled
        from services.r2_publisher_lib.downloadable_keys import (
            artifact_key_for_stream_kind,
            effective_policy_mode,
            stream_kinds_for,
        )
    except Exception as exc:  # pragma: no cover - boto3/storage missing
        logger.warning(
            "stream r2 redirect: storage package import failed (%s); falling back",
            exc,
        )
        return None, ""

    if not is_r2_enabled():
        return None, ""

    # Translate /stream/{kind} → artifact_key. Unknown kinds (anything
    # not in video/audio/poster) bail out without touching DB — the
    # downstream Job API path will produce its own 404.
    artifact_key = artifact_key_for_stream_kind(stream_kind)
    if artifact_key is None:
        return None, ""

    try:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "stream r2 redirect: job lookup failed job=%s (%s); falling back",
            job_id, exc,
        )
        return None, ""

    if job is None or not job.project_dir:
        return None, ""

    # Service-mode allowlist gate (mirrors _resolve_r2_redirect P2.1).
    # Express jobs requesting /stream/audio land here; we refuse so the
    # Gateway 302 path can't smuggle past the Job API enforcement at
    # src/services/jobs/api.py:459-464.
    # plan 2026-06-12 §C ⑥：策略档经 effective_policy_mode。匿名预览任务
    # **整体**不进 R2 stream redirect（本地 stream-only，AD-6）——注意
    # stream_kinds_for("anonymous_preview")={"video"} 只约束 kind，video
    # 本身在集合内，必须显式短路否则仍会 302；anonymous_preview 档
    # eager-push 集为空、本来不该有 registry 条目，这里是双保险。
    _policy_mode = effective_policy_mode(
        job.service_mode, bool(getattr(job, "is_anonymous_preview", False))
    )
    if _policy_mode == "anonymous_preview":
        return None, ""
    if stream_kind not in stream_kinds_for(_policy_mode):
        return None, ""

    expected_gen = job.edit_generation or 0

    registry_entry = None
    if job.r2_artifacts:
        for item in job.r2_artifacts:
            if (
                item.get("artifact_key") == artifact_key
                and item.get("edit_generation") == expected_gen
            ):
                registry_entry = item
                break

    if registry_entry is None:
        return None, ""

    state = registry_entry.get("state")
    if state not in ("pushed", "already_present"):
        # skipped_missing / failed → fall through to Job API local stream
        # (which will either find the on-disk file or return its 404).
        return None, ""

    r2_key = registry_entry.get("r2_key")
    if not r2_key:
        return None, ""

    # Stream presign is **distinct** from download presign (CodeX P2,
    # plan §11 follow-up 2026-05-12):
    #   - TTL ~30 min (vs 2 min for download) — players seek / pause /
    #     resume over the full play session and need one signature window
    #     to cover it.
    #   - No Content-Disposition: attachment — would force the browser
    #     to download instead of play in-page.
    #   - No filename param — stream URLs never hit the user's Save-As
    #     dialog; download path still issues attachment URLs for that.
    content_type = registry_entry.get(
        "content_type", "application/octet-stream",
    )
    try:
        from storage import r2_client
        # Verify R2 object actually exists. R2 lifecycle / manual
        # delete / pan-backup archive may have removed it after the
        # registry entry was written. Presign API never checks
        # existence — skip HEAD and we 302 to a guaranteed 404.
        if not r2_client.head_artifact(r2_key):
            logger.warning(
                "stream r2 redirect: registry HEAD miss job=%s kind=%s r2_key=%s"
                " — falling back to local",
                job_id, stream_kind, r2_key,
            )
            return None, ""
        url = r2_client.generate_presigned_stream_url(
            r2_key, content_type=content_type,
        )
        return url, "registry"
    except Exception as exc:
        logger.warning(
            "stream r2 redirect: presign failed job=%s kind=%s (%s); falling back",
            job_id, stream_kind, exc,
        )
        return None, ""


def _derive_download_filename(job: Job) -> str:
    """Pick a friendly filename for the user's Save As dialog.

    Priority: ``display_name`` (user-editable) → ``title`` (auto) → job_id.
    Always appends ``.mp4``. Any filesystem-hostile char gets replaced with
    ``_`` so the Content-Disposition value stays header-safe and matches
    what local filesystems will accept without further prompting.
    """
    raw = (job.display_name or job.title or job.job_id or "download").strip()
    # Strip path separators and other troublemakers. We're not trying to
    # be cryptographically safe here — the presigned URL is already
    # authenticated — just producing a sane filename.
    cleaned = []
    for ch in raw:
        if ch in ('"', "\\", "/", ":", "*", "?", "<", ">", "|", "\r", "\n", "\0"):
            cleaned.append("_")
        else:
            cleaned.append(ch)
    name = "".join(cleaned).strip() or "download"
    if not name.lower().endswith(".mp4"):
        name = f"{name}.mp4"
    return name


def _emit_download_event(
    job_id: str,
    event_type: str,
    *,
    message: str,
    payload: dict[str, object],
) -> None:
    """Thin delegator to :func:`gateway.storage.event_log.emit_download_event`.

    The real JSONL writer lives in ``gateway/storage/event_log.py`` so it
    can be unit-tested without dragging the fastapi / sqlalchemy import
    graph of this module into the test process. Keep this wrapper purely
    as a call-site stub — it exists only so existing imports of
    ``_emit_download_event`` from this module keep working, and so the
    call sites in this file stay readable.

    **Routing-decision semantics**: see the module docstring of
    ``event_log.py``. These events fire *before* the downstream proxy /
    redirect response, so they measure routing choices, not successful
    user-visible downloads.
    """
    from storage.event_log import emit_download_event
    emit_download_event(
        job_id,
        event_type,
        message=message,
        payload=payload,
    )


async def _continue_with_gateway_lock(
    request: Request,
    job_id: str,
    db: AsyncSession,
) -> Response:
    """Acquire FOR UPDATE on the Job row, proxy /continue upstream, then
    commit the status transition only if upstream accepted.

    Flow:
      1. SELECT ... FOR UPDATE on Job row (serializes concurrent continues).
         Legacy jobs without a Gateway row skip the lock entirely; upstream
         handles validation for them.
      2. Assert status == 'waiting_for_review'. If not, raise 409 without
         proxying — another continue already committed, or the job isn't
         actually waiting.
      3. Proxy upstream. Lock is still held because we haven't committed yet.
         Concurrent requests block on FOR UPDATE until this function returns.
      4. If upstream returned a 2xx, promote status to 'running' so the next
         request (which will block on the lock and then read fresh state)
         correctly rejects with 409.
      5. If upstream returned a non-2xx, leave status alone — waiting_for_review
         stays, so the user can retry continue without first waiting for
         list_jobs to reconcile.
      6. Commit (releases the lock regardless of upstream outcome).

    Trade-off: the DB row lock is held through the proxy call (typically
    sub-second for /continue). /continue is an infrequent endpoint so this
    is an acceptable cost for correctness. If the proxy hangs, the lock
    holds until that request times out — the failure mode here is isolated
    to that single job_id's continue retries, not system-wide.
    """
    result = await db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is not None and job.status != "waiting_for_review":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not continuable (current status: {job.status})",
        )

    # Lock is held; proxy upstream.
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # Only promote status on upstream success. If upstream rejected (e.g.
    # review not actually approved per service.py:155-168) or blew up, we
    # leave the row in 'waiting_for_review' so retries work.
    if job is not None and 200 <= response.status_code < 300:
        job.status = "running"

    # Commit either way — releases the FOR UPDATE lock. A no-op commit is
    # cheap; the important thing is that no future request is blocked
    # waiting on this txn.
    await db.commit()
    return response


def _aggregate_quality_tier_from_speakers(
    speakers: list[dict],
) -> tuple[str, str | None]:
    """Aggregate per-speaker UI choices into a job-level (quality_tier, tts_model).

    Rules:
    - 任一 minimax speaker 选了 hd → ("flagship", "speech-2.8-hd")
    - 有 minimax speaker 但全部是 turbo → ("high", "speech-2.8-turbo")
    - 完全没有 minimax speaker → ("standard", None)  ← 保留原 tts_model

    The numeric point/minute rate is not hard-coded here. Settlement resolves
    these tiers through Gateway runtime pricing, so admin pricing changes stay
    centralized.
    """
    any_minimax = False
    any_hd = False
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        provider = str(sp.get("tts_provider", "")).strip().lower()
        if provider == "minimax":
            any_minimax = True
            model_hint = str(sp.get("minimax_model", "") or "").strip().lower()
            if model_hint == "hd":
                any_hd = True

    if any_minimax and any_hd:
        return ("flagship", "speech-2.8-hd")
    if any_minimax:
        return ("high", "speech-2.8-turbo")
    return ("standard", None)


async def _record_voice_reuse_events(
    db: AsyncSession,
    *,
    job: Job,
    speakers: list[dict],
) -> None:
    reuse_speakers = [
        sp for sp in speakers
        if isinstance(sp, dict) and sp.get("voice_reuse") is True
    ]
    if not reuse_speakers or not job.user_id:
        return

    snap = dict(job.metering_snapshot or {})
    project_dir_raw = str(snap.get("project_dir") or "").strip()
    if not project_dir_raw:
        return

    try:
        from services.usage_meter import UsageMeter
        meter = UsageMeter(Path(project_dir_raw), job_id=job.job_id)
    except Exception:
        logger.warning("voice reuse audit skipped for %s: UsageMeter unavailable", job.job_id, exc_info=True)
        return

    existing_event_ids = {str(event.get("event_id") or "") for event in meter.events}
    for sp in reuse_speakers:
        speaker_id = str(sp.get("speaker_id") or "").strip()
        voice_id = str(sp.get("voice_id") or "").strip()
        if not speaker_id or not voice_id:
            continue
        event_id = f"voice_reuse:{job.job_id}:{speaker_id}:{voice_id}"
        if event_id in existing_event_ids:
            continue

        try:
            result = await db.execute(
                select(UserVoice).where(
                    UserVoice.user_id == job.user_id,
                    UserVoice.voice_id == voice_id,
                    UserVoice.expired_at.is_(None),
                )
            )
            user_voice = result.scalar_one_or_none()
            if user_voice is None:
                continue
            meter.record_voice_reuse(
                provider=user_voice.provider,
                voice_id=voice_id,
                speaker_id=speaker_id,
                source_voice_id=voice_id,
                match_confidence="user_confirmed",
                match_reason="studio_reuse_confirmed",
                extra={
                    "event_id": event_id,
                    "source_user_voice_id": str(user_voice.id),
                    "source_content_hash": getattr(user_voice, "source_content_hash", None),
                    "source_speaker_id": getattr(user_voice, "source_speaker_id", None),
                },
            )
            existing_event_ids.add(event_id)
        except Exception:
            logger.warning(
                "voice reuse audit failed for %s/%s/%s",
                job.job_id,
                speaker_id,
                voice_id,
                exc_info=True,
            )


async def _record_voice_candidate_rejection_events(
    db: AsyncSession,
    *,
    job: Job,
    speakers: list[dict],
) -> None:
    """Phase 4 (plan 2026-05-17-user-voice-candidate-first §计费和审计
    ``smart_possible_user_voice_match_rejected``): when Smart pipeline
    paused with a possible (non-strong) personal-voice candidate, and
    the user picked a different voice (official catalog or new clone),
    write a non-billable audit event so support / dispute review can
    trace what was offered vs picked.

    Detection rule: per speaker, look up ``smart_offered_candidates``
    in the review_state.json voice_selection_review payload (written
    by the pipeline at pause time). For each offered candidate whose
    ``voice_id`` differs from the picked voice ``sp.get("voice_id")``
    AND the speaker is NOT marked ``voice_reuse: true``, emit the
    rejection event.

    Skips:
      - speakers with ``voice_reuse=true``: that's the confirmation
        path, audited by ``_record_voice_reuse_events`` instead.
      - speakers without offered candidates: there's nothing to
        "reject" against.
      - missing project_dir / missing review_state.json: best-effort,
        log and bail.
    """
    if not speakers or not job.user_id:
        return

    snap = dict(job.metering_snapshot or {})
    project_dir_raw = str(snap.get("project_dir") or "").strip()
    if not project_dir_raw:
        return

    # Read offered candidates from review_state.json — pipeline wrote
    # them under voice_selection_review.payload.speakers[].smart_offered_candidates
    # at pause time (process.py Phase 4 mutation).
    try:
        from services.review_state import (
            VOICE_SELECTION_REVIEW_STAGE,
            ReviewStateManager,
        )
        review_state_path = Path(project_dir_raw) / "review_state.json"
        if not review_state_path.exists():
            return
        manager = ReviewStateManager(review_state_path)
        stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    except Exception:
        logger.warning(
            "voice candidate rejection audit: failed to load review_state for %s",
            job.job_id, exc_info=True,
        )
        return
    if not stage:
        return
    payload = stage.get("payload") or {}
    if not isinstance(payload, dict):
        return
    offered_speakers = payload.get("speakers")
    if not isinstance(offered_speakers, list):
        return
    offered_by_speaker_id: dict[str, list[dict]] = {}
    for offered_sp in offered_speakers:
        if not isinstance(offered_sp, dict):
            continue
        sid = str(offered_sp.get("speaker_id") or "").strip()
        candidates = offered_sp.get("smart_offered_candidates")
        if not sid or not isinstance(candidates, list) or not candidates:
            continue
        offered_by_speaker_id[sid] = [c for c in candidates if isinstance(c, dict)]
    if not offered_by_speaker_id:
        return

    try:
        from services.usage_meter import UsageMeter
        meter = UsageMeter(Path(project_dir_raw), job_id=job.job_id)
    except Exception:
        logger.warning(
            "voice candidate rejection audit skipped for %s: UsageMeter unavailable",
            job.job_id, exc_info=True,
        )
        return

    existing_event_ids = {str(event.get("event_id") or "") for event in meter.events}
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        # Skip reuse path — that's already audited.
        if sp.get("voice_reuse") is True:
            continue
        speaker_id = str(sp.get("speaker_id") or "").strip()
        chosen_voice_id = str(sp.get("voice_id") or "").strip()
        if not speaker_id:
            continue
        offered = offered_by_speaker_id.get(speaker_id)
        if not offered:
            continue
        for offered_candidate in offered:
            offered_voice_id = str(offered_candidate.get("voice_id") or "").strip()
            if not offered_voice_id:
                continue
            # If the chosen voice IS one of the offered candidates,
            # this is a confirmation, not a rejection. The
            # voice_reuse=true short-circuit above should already
            # have caught it, but defensively guard here too.
            if chosen_voice_id and chosen_voice_id == offered_voice_id:
                continue
            event_id = (
                f"voice_candidate_rejected:{job.job_id}:"
                f"{speaker_id}:{offered_voice_id}"
            )
            if event_id in existing_event_ids:
                continue
            try:
                result = await db.execute(
                    select(UserVoice).where(
                        UserVoice.user_id == job.user_id,
                        UserVoice.voice_id == offered_voice_id,
                        UserVoice.expired_at.is_(None),
                    )
                )
                user_voice = result.scalar_one_or_none()
                # If the voice has since been deleted, still emit the
                # audit (provider falls back to the offered metadata),
                # because the rejection happened and we want the
                # ledger record. Plan §计费和审计: the event records
                # the user_action, not the voice's continued existence.
                provider = (
                    user_voice.provider if user_voice is not None
                    else "minimax_voice_clone"
                )
                meter.record_voice_candidate_rejected(
                    provider=provider,
                    rejected_voice_id=offered_voice_id,
                    speaker_id=speaker_id,
                    rejected_match_confidence=str(
                        offered_candidate.get("confidence") or ""
                    ),
                    rejected_match_reason=str(
                        offered_candidate.get("reason")
                        or offered_candidate.get("match_scope")
                        or ""
                    ),
                    chosen_voice_id=chosen_voice_id,
                    extra={
                        "event_id": event_id,
                        "match_scope": offered_candidate.get("match_scope"),
                        "source_user_voice_id": str(
                            user_voice.id if user_voice is not None
                            else offered_candidate.get("user_voice_id") or ""
                        ),
                        "source_content_hash": (
                            getattr(user_voice, "source_content_hash", None)
                            if user_voice is not None
                            else None
                        ),
                        "source_speaker_id": (
                            getattr(user_voice, "source_speaker_id", None)
                            if user_voice is not None
                            else None
                        ),
                    },
                )
                existing_event_ids.add(event_id)
            except Exception:
                logger.warning(
                    "voice candidate rejection audit failed for %s/%s/%s",
                    job.job_id,
                    speaker_id,
                    offered_voice_id,
                    exc_info=True,
                )


async def _fetch_cosyvoice_public_voice_ids(db: AsyncSession) -> set[str]:
    """Phase 4.1 E P1 #2 fix (Codex 2026-05-25 二轮 E review)：直接 async 查
    Gateway 本地 ``voice_catalog`` 表，**不**走 ``services.tts.*`` 里的
    self-HTTP helper（避免 Gateway 同步调自己 + event loop 阻塞 + 静态 fallback
    只覆盖 flash 不覆盖 plus 的多重问题）。

    过滤条件**严格复刻** internal ``/api/internal/voice-catalog`` 的契约
    （Codex E 三轮 P1 #3 fix）—— 必须与 ``voice_catalog_api.py:157-162`` 同源：

    - ``provider == "cosyvoice"``
    - ``matchable IS True``
    - ``archived_at IS NULL``
    - ``_VERIFIED_TRUE_SQL`` —— ``verify_status`` 中至少一个维度
      ``verified=true``。**必须有此条件**，否则 unverified 的 catalog row
      会被当成 public preset，让 orphan clone voice 漂过 ``voice_clone_metadata_missing``
      的 fail-closed 判断。

    返 voice_id set。失败抛 ``RuntimeError`` —— 由调用方（``_enrich_speakers...``）
    决定 fail-closed 还是 degrade（不会静默返空集，避免把所有 cosyvoice 音色
    都误判为 clone candidate）。
    """
    # 从 voice_catalog_api 导入 verified SQL fragment —— **单源**，绝不复制写法，
    # 防止 internal endpoint 改 verified 规则但此 helper 漏改导致 drift
    from voice_catalog_api import _VERIFIED_TRUE_SQL
    from voice_catalog_models import VoiceCatalog

    stmt = select(VoiceCatalog.voice_id).where(
        VoiceCatalog.provider == "cosyvoice",
        VoiceCatalog.matchable.is_(True),
        VoiceCatalog.archived_at.is_(None),
        _VERIFIED_TRUE_SQL,
    )
    result = await db.execute(stmt)
    return {str(row[0]) for row in result.all()}


async def _fetch_known_cosyvoice_clone_voice_ids(
    db: AsyncSession,
    voice_ids: list[str],
) -> set[str]:
    """Return voice ids that are known CosyVoice clone assets in user_voices.

    This intentionally ignores ``user_id`` and ``expired_at``.  It is not used
    for authorization; it is only an identity guard for approve payloads where
    ``tts_provider`` is missing.  A voice id that belongs to another account or
    was expired/deleted must still be treated as clone-like and rejected instead
    of drifting to a legacy/non-worker path.
    """
    distinct_voice_ids = list({vid for vid in voice_ids if vid})
    if not distinct_voice_ids:
        return set()
    stmt = select(UserVoice.voice_id).where(
        UserVoice.voice_id.in_(distinct_voice_ids),
        UserVoice.provider == "cosyvoice_voice_clone",
    )
    result = await db.execute(stmt)
    return {str(row[0]) for row in result.all()}


async def _enrich_speakers_with_clone_routing(
    db: AsyncSession,
    *,
    job_id: str,
    speakers: list,
) -> tuple[list | None, dict | None]:
    """Phase 4.1 E.2 (Codex 2026-05-25 二轮 fix-up)：丰富 approved voice-selection
    payload。

    对每个 ``speakers[i]`` 按 ``voice_id`` 批量查 ``user_voices``（strict
    filter），命中即添加 ``requires_worker`` / ``worker_target_model`` 两
    字段。同时实施 4 项 fail-closed 约束：

    - ``voice_id`` 命中 user_voices 但 payload ``tts_provider != "cosyvoice"``
      → ``voice_clone_provider_mismatch`` 400
    - ``voice_id`` 未命中但形似 CosyVoice clone（payload ``tts_provider ==
      "cosyvoice"`` 且不在 public catalog） → ``voice_clone_metadata_missing`` 400
    - **PR #7 Codex review**：payload 漏写 ``tts_provider`` 时，如果 voice_id
      在任意 user_voices row 中是 CosyVoice clone，也按 clone-like 处理并
      fail-closed，而不是放行到 legacy/non-worker path。
    - **P1 #1 fix**：``lookup_clone_voice_routing_metadata`` 抛异常（DB 短暂
      不可用）→ 对任何 ``tts_provider="cosyvoice"`` 非 public preset voice
      返 ``voice_clone_routing_lookup_failed`` 503。**禁止** degrade 让 clone
      voice 漂到 legacy 国际 DashScope endpoint。
    - 预设公开音色 / MiniMax / VolcEngine / legacy → 不加 routing 保持原状

    Returns
    -------
    (enriched_speakers, error_detail)
        - 成功：``(enriched_list, None)``
        - 拒绝：``(None, {"code": ..., "message": ..., ...})``
        - 不需要 enrichment：``(None, None)``
    """
    # Lazy import 避开 services 命名空间循环
    from user_voice_service import lookup_clone_voice_routing_metadata

    if not speakers:
        return None, None

    # 1) 收集需要查 user_voices 的 voice_id
    voice_ids_to_lookup: list[str] = []
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        vid = str(sp.get("voice_id", "") or "").strip()
        if vid and vid != "auto":
            voice_ids_to_lookup.append(vid)

    if not voice_ids_to_lookup:
        return None, None

    # 2) 查 user_voices 对应 user_id —— 从 job 表反查
    job_row = (
        await db.execute(select(Job.user_id).where(Job.job_id == job_id))
    ).first()
    if job_row is None or job_row.user_id is None:
        known_clone_voice_ids: set[str] = set()
        clone_identity_lookup_failed = False
        try:
            known_clone_voice_ids = await _fetch_known_cosyvoice_clone_voice_ids(
                db, voice_ids_to_lookup,
            )
        except Exception as exc:
            logger.warning(
                "voice-selection/approve: clone identity lookup failed while "
                "job user is unavailable for job=%s: %s",
                job_id, exc,
            )
            clone_identity_lookup_failed = True

        public_catalog_voice_ids: set[str] = set()
        public_catalog_lookup_failed = False
        try:
            public_catalog_voice_ids = await _fetch_cosyvoice_public_voice_ids(db)
        except Exception as exc:
            logger.warning(
                "voice-selection/approve: public catalog lookup failed while "
                "job user is unavailable for job=%s: %s",
                job_id, exc,
            )
            public_catalog_lookup_failed = True

        sanitized: list = []
        for sp in speakers:
            if not isinstance(sp, dict):
                sanitized.append(sp)
                continue
            new_sp = dict(sp)
            new_sp.pop("requires_worker", None)
            new_sp.pop("worker_target_model", None)

            vid = str(new_sp.get("voice_id", "") or "").strip()
            payload_tts_provider = str(new_sp.get("tts_provider", "") or "").strip()
            if not vid or vid == "auto":
                sanitized.append(new_sp)
                continue

            is_public_preset = (
                not public_catalog_lookup_failed
                and vid in public_catalog_voice_ids
            )
            is_known_clone_voice = vid in known_clone_voice_ids
            if (
                is_known_clone_voice
                and payload_tts_provider
                and payload_tts_provider != "cosyvoice"
            ):
                return None, {
                    "code": "voice_clone_provider_mismatch",
                    "message": (
                        f"voice_id={vid!r} is a known CosyVoice clone voice, "
                        f"but submitted tts_provider={payload_tts_provider!r}. "
                        f"Cloned voices can only be approved with cosyvoice provider."
                    ),
                    "voice_id": vid,
                    "submitted_tts_provider": payload_tts_provider,
                }
            is_cosyvoice_clone_candidate = (
                (payload_tts_provider == "cosyvoice" and not is_public_preset)
                or (not payload_tts_provider and is_known_clone_voice)
                or (
                    not payload_tts_provider
                    and clone_identity_lookup_failed
                    and not is_public_preset
                )
            )
            if is_cosyvoice_clone_candidate:
                return None, {
                    "code": "voice_clone_routing_lookup_failed",
                    "message": (
                        f"job={job_id!r} cannot resolve Gateway user_id while "
                        f"approving voice_id={vid!r}; rejecting to prevent "
                        f"CosyVoice clone routing from drifting to a legacy path."
                    ),
                    "voice_id": vid,
                }

            sanitized.append(new_sp)

        return sanitized, None
    user_id = job_row.user_id

    # 3) 批量 strict filter 查询（Codex HC#4）
    routing_lookup_failed = False
    routing_map: dict[str, dict[str, object]] = {}
    try:
        routing_map = await lookup_clone_voice_routing_metadata(
            db, user_id=user_id, voice_ids=voice_ids_to_lookup,
        )
    except Exception as exc:
        # P1 #1 fix：**绝不**静默 degrade 让 clone voice 走 legacy。下面在
        # 单个 speaker 循环里按 voice 形态做 fail-closed 决策。
        logger.warning(
            "voice-selection/approve: routing lookup raised for job=%s: %s "
            "(will fail-closed for cosyvoice non-preset voices)",
            job_id, exc,
        )
        routing_lookup_failed = True

    # 4) 已知 clone voice id 集合（只作身份判断，不授权）。这补齐 payload
    # 缺 tts_provider 时的 fail-closed 缺口：只要 voice_id 在 user_voices 任意
    # row 中是 CosyVoice clone，就不能漂到 legacy/non-worker path。
    known_clone_voice_ids: set[str] = set()
    clone_identity_lookup_failed = False
    try:
        known_clone_voice_ids = await _fetch_known_cosyvoice_clone_voice_ids(
            db, voice_ids_to_lookup,
        )
    except Exception as exc:
        logger.warning(
            "voice-selection/approve: clone identity lookup failed for job=%s: %s "
            "(will fail-closed for blank-provider non-public voices)",
            job_id, exc,
        )
        clone_identity_lookup_failed = True

    # 5) 公开 catalog 集合（P1 #2 fix：直接 Gateway DB async 查，不走 self-HTTP）
    public_catalog_voice_ids: set[str] = set()
    public_catalog_lookup_failed = False
    try:
        public_catalog_voice_ids = await _fetch_cosyvoice_public_voice_ids(db)
    except Exception as exc:
        # public catalog 查询失败也得 fail-closed：因为无法区分 "voice_id 是
        # 公开预设" vs "voice_id 是 orphan clone"，唯一安全的是当作 orphan
        # 处理（拒绝 approve）。
        logger.warning(
            "voice-selection/approve: public catalog DB query failed for job=%s: %s "
            "(will fail-closed for any unmatched cosyvoice voice)",
            job_id, exc,
        )
        public_catalog_lookup_failed = True

    # 6) 逐 speaker 应用 routing + fail-closed 校验
    enriched: list = []
    for sp in speakers:
        if not isinstance(sp, dict):
            enriched.append(sp)
            continue
        new_sp = dict(sp)
        # PR #7 Codex review (2026-05-25): approved payload must only carry
        # worker routing fields that Gateway re-derived from user_voices. A
        # client can resend stale or forged requires_worker / worker_target_model
        # values for public presets or non-CosyVoice voices; strip them before
        # deciding whether this speaker has an authorized routing entry.
        new_sp.pop("requires_worker", None)
        new_sp.pop("worker_target_model", None)
        vid = str(new_sp.get("voice_id", "") or "").strip()
        payload_tts_provider = str(new_sp.get("tts_provider", "") or "").strip()

        if not vid or vid == "auto":
            enriched.append(new_sp)
            continue

        if not routing_lookup_failed and vid in routing_map:
            # 命中 user_voices clone row
            # HC#2: payload tts_provider 必须是 cosyvoice
            if payload_tts_provider and payload_tts_provider != "cosyvoice":
                return None, {
                    "code": "voice_clone_provider_mismatch",
                    "message": (
                        f"voice_id={vid!r} 是 CosyVoice clone 音色，"
                        f"但提交的 tts_provider={payload_tts_provider!r}。"
                        f"克隆音色只能与 cosyvoice provider 配合使用。"
                    ),
                    "voice_id": vid,
                    "submitted_tts_provider": payload_tts_provider,
                }
            new_sp["requires_worker"] = bool(routing_map[vid]["requires_worker"])
            new_sp["worker_target_model"] = str(routing_map[vid]["worker_target_model"])
            new_sp["tts_provider"] = "cosyvoice"
        else:
            # 未命中 user_voices —— 三种可能：
            #   a) 该 voice 是公开预设 → 跳过（合法 legacy 路径）
            #   b) routing lookup 失败但 voice 像 cosyvoice clone → 503 fail-closed
            #   c) voice 像 cosyvoice clone 但 user_voices 真没有 → 400 fail-closed
            is_known_clone_voice = vid in known_clone_voice_ids
            if (
                is_known_clone_voice
                and payload_tts_provider
                and payload_tts_provider != "cosyvoice"
            ):
                return None, {
                    "code": "voice_clone_provider_mismatch",
                    "message": (
                        f"voice_id={vid!r} is a known CosyVoice clone voice, "
                        f"but submitted tts_provider={payload_tts_provider!r}. "
                        f"Cloned voices can only be approved with cosyvoice provider."
                    ),
                    "voice_id": vid,
                    "submitted_tts_provider": payload_tts_provider,
                }
            is_cosyvoice_voice = (
                payload_tts_provider == "cosyvoice"
                or (not payload_tts_provider and is_known_clone_voice)
            )
            is_public_preset = (
                not public_catalog_lookup_failed
                and vid in public_catalog_voice_ids
            )

            if (
                not payload_tts_provider
                and clone_identity_lookup_failed
                and not is_public_preset
            ):
                return None, {
                    "code": "voice_clone_routing_lookup_failed",
                    "message": (
                        f"voice_id={vid!r} 未携带 tts_provider，且用户音色库暂时"
                        f"无法确认它是否为 CosyVoice clone。为防止 clone voice "
                        f"走错路径，已拒绝当前 approve（请稍后重试）。"
                    ),
                    "voice_id": vid,
                }

            if is_cosyvoice_voice and not is_public_preset:
                # 这里 voice **不是** public preset（或我们无法确认它是）
                if routing_lookup_failed:
                    # P1 #1 fix：DB 异常 + 非 preset cosyvoice → 503 fail-closed，
                    # 禁止 fall-open 让 clone voice 漂到 legacy
                    return None, {
                        "code": "voice_clone_routing_lookup_failed",
                        "message": (
                            f"voice_id={vid!r} 是 CosyVoice 非预设音色，"
                            f"但用户音色库暂时无法查询（请稍后重试）。"
                            f"为防止 clone voice 走错路径，已拒绝当前 approve。"
                        ),
                        "voice_id": vid,
                    }
                if public_catalog_lookup_failed:
                    # public catalog 查询失败 → 无法确认是预设 → 当作 orphan 处理
                    return None, {
                        "code": "voice_clone_metadata_missing",
                        "message": (
                            f"voice_id={vid!r} 看似 CosyVoice 克隆音色但公开音色库"
                            f"暂时无法查询，无法确认是否为合法预设。请稍后重试。"
                        ),
                        "voice_id": vid,
                    }
                # routing + public catalog 都查得到 → voice 确实是 orphan clone
                return None, {
                    "code": "voice_clone_metadata_missing",
                    "message": (
                        f"voice_id={vid!r} 看似 CosyVoice 克隆音色但在用户音色库中"
                        f"找不到对应记录（已过期 / 删除 / 跨账户）。请重新选择音色。"
                    ),
                    "voice_id": vid,
                }
            # 公开预设 / MiniMax / VolcEngine / legacy → 不加 routing，保持原状
        enriched.append(new_sp)

    return enriched, None


async def _approve_voice_selection_with_quality_sync(
    request: Request,
    job_id: str,
    db: AsyncSession,
) -> Response:
    """Intercept POST /review/voice-selection/approve to sync quality_tier + tts_model.

    Flow:
    1. Read and parse the request body to extract per-speaker minimax_model.
    2. Forward the body unchanged to the upstream Job API (which writes
       review_state.json for the pipeline).
    3. If upstream returns 2xx, update Gateway DB:
       - Job.tts_model (consumed by TTS generator at S4)
       - Job.metering_snapshot.quality_tier (consumed by settle at capture)
       - Job.metering_snapshot.per_speaker_provider (for provider-breakdown
         audit; tracks the actual per-speaker provider mix)
    4. Commit. If DB update fails, log but do NOT roll back upstream —
       upstream already wrote review_state.json and the pipeline will run
       with the (possibly stale) quality_tier; this is shadow-safe.

    If upstream returns non-2xx, body is returned verbatim and no DB update.
    """
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        payload = {}

    speakers = payload.get("speakers") if isinstance(payload, dict) else None
    if not isinstance(speakers, list):
        speakers = []

    # ===========================================================================
    # Phase 4.1 E.2 (Codex 2026-05-25 E 三轮 review 后)：CosyVoice clone routing
    # 元数据丰富。
    #
    # 在 preflight calibration + proxy 之前，根据 ``speakers[i].voice_id`` 批量
    # 查 ``user_voices`` 表（strict filter），把命中 row 的 ``requires_worker`` /
    # ``worker_target_model`` 添加到 speaker 条目上。Pipeline 在
    # ``process.py:3310`` 读 approved payload 时即取得这些字段，传给
    # ``_apply_runtime_voice_overrides`` 设到 ``DubbingSegment``，最终路由到
    # 武汉 mainland worker（Phase 4.1 D）。
    #
    # 三条硬约束（Codex E v2 HC#1/2/3）+ 二轮 P1 修正：
    #
    #   1. body **必须重新序列化** 再 proxy，否则 upstream Job API 写到
    #      review_state.json 里看不到 routing 字段。
    #   2. 若 ``voice_id`` 命中 clone row 但 payload ``tts_provider != "cosyvoice"``
    #      → 400 ``voice_clone_provider_mismatch``（数据不一致，绝不静默改写）。
    #   3. ``voice_id`` 不在 user_voices 但形似 CosyVoice clone（``tts_provider==
    #      "cosyvoice"`` 且不在 public catalog）→ 400 ``voice_clone_metadata_missing``。
    #
    # 异常处理（E 二轮 P1 #1 fail-closed 修正后）：
    #
    #   - ``lookup_clone_voice_routing_metadata`` 抛异常（DB 短暂不可用） +
    #     voice 是 ``tts_provider="cosyvoice"`` 非 public preset →
    #     **503 ``voice_clone_routing_lookup_failed``**（让用户稍后重试），
    #     **不允许** degrade 让 clone voice 漂到 legacy 国际 DashScope endpoint。
    #   - 公开 catalog 查询异常 + 未确认 voice 为预设 → 400 metadata_missing
    #     (fail-closed，宁愿误判 preset 为 orphan，也不让 orphan 漂走)。
    #   - 公开预设 / MiniMax / VolcEngine / legacy → 允许 degrade（这些 voice
    #     本就不走 worker，没有路由漂移风险）。
    # ===========================================================================
    enriched_speakers, enrichment_error = await _enrich_speakers_with_clone_routing(
        db, job_id=job_id, speakers=speakers,
    )
    if enrichment_error is not None:
        # Phase 4.1 E 三轮 review (Codex 2026-05-25 P2 #1)：错误码→HTTP status 映射。
        # ``voice_clone_routing_lookup_failed`` 是 DB / 路由查询临时不可用——
        # **服务端**问题（用户应稍后重试），返 503；其它（``provider_mismatch`` /
        # ``metadata_missing``）是用户数据状态错误，返 400 让前端引导用户改选。
        error_code = enrichment_error.get("code", "")
        if error_code == "voice_clone_routing_lookup_failed":
            error_status = 503
        else:
            error_status = 400
        return JSONResponse(
            status_code=error_status,
            content={"detail": enrichment_error},
        )
    if enriched_speakers is not None and isinstance(payload, dict):
        payload["speakers"] = enriched_speakers
        speakers = enriched_speakers
        # HC#1: body 必须重新序列化让 upstream / preflight 看到 routing 字段
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # Plan v4.3 §3.2 T2 — voice CPS preflight calibration BEFORE proxy.
    # Hard requirement: this must run before proxy_request so that when
    # the pipeline reads review_state.json it sees calibrated CPS for
    # the chosen voices. Otherwise Pre-TTS rewrite degrades to default
    # CPS on first run.
    #
    # codex F-v4.1-2: route ``db`` was already used by _verify_job_ownership
    # earlier in intercept_job_subresource. We rollback BEFORE the up-to-50s
    # preflight wait so SQLAlchemy returns the connection to the pool.
    # Without this, concurrent submits would exhaust the connection pool.
    preflight_outcomes: list[dict] = []
    try:
        from voice_calibration_review_preflight import (
            pre_flight_calibrate_voices,
            review_preflight_enabled,
        )
        if review_preflight_enabled() and speakers:
            try:
                await db.rollback()
            except Exception:
                # rollback shouldn't realistically raise; if it does,
                # we still proceed — proxy + DB sync below own their
                # own commit semantics so the route doesn't deadlock.
                logger.exception(
                    "[t2-preflight] route db.rollback() raised job=%s — proceeding",
                    job_id,
                )
            try:
                preflight_outcomes = await pre_flight_calibrate_voices(
                    job_id=job_id,
                    speakers=speakers,
                )
            except Exception:
                # The preflight module is built to never raise (all errors
                # surface as outcome dicts). This catch is a paranoia net
                # for unforeseen import / asyncio glitches — proxy must
                # never block on preflight.
                logger.exception(
                    "[t2-preflight] pre_flight_calibrate_voices raised job=%s — degrading",
                    job_id,
                )
                preflight_outcomes = []

            # codex v4.4 P2: emit a summary log so operators can track
            # T2 hit rate (already_calibrated vs calibrated vs
            # not_started_timeout vs still_running vs not_found etc.)
            # without grepping individual voice events. preflight_outcomes
            # is otherwise currently consumed only by this log; future
            # work will surface it in the response for frontend tooltip
            # rendering.
            if preflight_outcomes:
                _counts: dict[str, int] = {}
                for _o in preflight_outcomes:
                    _s = str(_o.get("status") or "unknown")
                    _counts[_s] = _counts.get(_s, 0) + 1
                logger.info(
                    "[t2-preflight] summary job=%s total=%d %s",
                    job_id, len(preflight_outcomes),
                    " ".join(f"{k}={v}" for k, v in sorted(_counts.items())),
                )
    except Exception:
        # Module import or any wiring error — log and proceed without
        # preflight. Outermost soft-fail guard.
        logger.exception(
            "[t2-preflight] outer wiring error job=%s — proceeding without preflight",
            job_id,
        )

    # Forward upstream with the possibly enriched body (Phase 4.1 E.2
    # re-serializes after adding worker routing fields). Upstream doesn't
    # need to understand `minimax_model` / `requires_worker` / `worker_target_model`
    # itself —— it writes the body verbatim into review_state.json, and the
    # pipeline reads those fields back when constructing DubbingSegment.
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
        override_body=body_bytes,
    )

    if not (200 <= response.status_code < 300):
        return response

    # Aggregate job-level quality_tier + tts_model from per-speaker choices.
    tier, model = _aggregate_quality_tier_from_speakers(speakers)

    try:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            logger.info("voice-selection/approve: job %s not in Gateway DB, skip sync", job_id)
            return response

        snap = dict(job.metering_snapshot or {})
        snap["quality_tier"] = tier
        # Record per-speaker provider/model mix for audit (provider-breakdown
        # today only shows job-default provider; this future-proofs the
        # execution-provider view without requiring a schema change).
        per_speaker_mix = []
        for sp in speakers:
            if not isinstance(sp, dict):
                continue
            per_speaker_mix.append({
                "speaker_id": str(sp.get("speaker_id", "")),
                "tts_provider": str(sp.get("tts_provider", "")),
                "minimax_model": str(sp.get("minimax_model", "") or "") or None,
            })
        if per_speaker_mix:
            snap["per_speaker_provider"] = per_speaker_mix
        job.metering_snapshot = snap

        await _record_voice_reuse_events(db, job=job, speakers=speakers)
        # Phase 4 (plan 2026-05-17-user-voice-candidate-first §计费和审计
        # ``smart_possible_user_voice_match_rejected``): when Smart
        # paused on a possible candidate and the user picked a
        # different voice, write a non-billable audit event. This is
        # the sibling of voice_reuse audit on the rejection branch.
        await _record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        )

        # Only overwrite tts_model when a minimax speaker explicitly chose
        # turbo/hd. For jobs using only CosyVoice/VolcEngine, keep whatever
        # job_intercept.py:120 wrote at create time.
        if model is not None:
            job.tts_model = model

        await db.commit()
        logger.info(
            "voice-selection/approve: job=%s tier=%s tts_model=%s speakers=%d",
            job_id, tier, model or job.tts_model, len(speakers),
        )
    except Exception as exc:
        logger.warning(
            "voice-selection/approve: DB sync failed for %s: %s (non-fatal, upstream already accepted)",
            job_id, exc,
        )
        await db.rollback()

    return response


# ---------------------------------------------------------------------------
# JobRecord-level redaction (plan §10.4 deepening, 2026-04-21).
#
# ``GET /jobs/{id}/logs`` already redacts events/lines for non-admin
# (see _serve_redacted_logs below). But two other JobRecord-shaped
# fields surface in the workspace UI and were leaking infra detail
# (provider names / file paths / UUIDs / URLs) to non-admin users:
#
#   - ``progress_message`` rendered in the "正在处理" big card subtitle
#   - ``error_summary.message`` rendered in the failed-state card
#
# Storage layer (JobRecord JSON, events.jsonl) is intentionally
# unchanged — admin's GET still sees raw text. Redaction is response-
# only and admin pass-through, mirroring the _serve_redacted_logs
# pattern so the two stay in lock-step on the role check.
# ---------------------------------------------------------------------------


def _is_admin_user(user: User | None) -> bool:
    """Single source of truth for 'is this user allowed to see raw
    infra detail in API responses'. Mirrors _serve_redacted_logs's
    role check exactly so the two paths can never drift apart."""
    if user is None:
        return False
    role = getattr(user, "role", None) or "user"
    return role == "admin"


def _redact_job_record_in_place(
    record: dict,
    user: User | None,
    *,
    redactor=None,
) -> None:
    """Mutate a JobRecord-shaped dict to strip provider names / paths /
    UUIDs / URLs from user-facing message fields, for non-admin users.

    Admin: no-op (pass-through). Non-admin and ``user is None`` (auth
    disabled / corrupted session): redact. The fail-closed default is
    deliberate — leaking by accident is worse than over-redacting.

    Targets:
      - ``progress_message`` (top-level string)
      - ``error_summary.message`` (nested string under the dict-typed
        ``error_summary``)

    Tolerated:
      - missing fields, ``None`` values, non-dict ``error_summary``
        (returns silently — no AttributeError)
      - sibling fields (``error_summary.stage`` / ``error_type`` /
        ``review_gate`` / ``current_stage`` / etc.) untouched

    The optional ``redactor`` arg lets list endpoints build the
    Redactor once and reuse it across rows — cheaper than rebuilding
    per record.
    """
    if _is_admin_user(user):
        return  # admin pass-through, exactly like _serve_redacted_logs

    if redactor is None:
        redactor = build_default_redactor()
        if redactor is None:
            # Loader already logged the underlying failure (file missing /
            # spec load error). Nothing actionable left here — return without
            # mutation. Pass-through is safer than blanking the message.
            return

    # progress_message at the top level
    pm = record.get("progress_message")
    if isinstance(pm, str) and pm:
        record["progress_message"] = redactor.redact(pm)

    # error_summary.message nested
    es = record.get("error_summary")
    if isinstance(es, dict):
        es_msg = es.get("message")
        if isinstance(es_msg, str) and es_msg:
            es["message"] = redactor.redact(es_msg)


async def _serve_redacted_logs(
    request: Request,
    user: User | None,
) -> Response:
    """Proxy GET /logs and, for non-admin users, strip sensitive fragments
    from ``events[].message`` and ``lines[]`` before returning.

    Failure modes:
    - Upstream non-200: return verbatim (nothing to redact).
    - Response body is not valid JSON or not the expected shape: return
      verbatim — we prefer "fail open" on unexpected schema changes rather
      than 500'ing the logs endpoint.
    """
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )
    if response.status_code != 200:
        return response

    role = (getattr(user, "role", None) or "user") if user is not None else "user"
    if role == "admin":
        return response

    try:
        body = json.loads(response.body.decode("utf-8"))
    except Exception:
        logger.warning("redacted_logs: upstream response was not JSON; returning verbatim")
        return response

    if not isinstance(body, dict):
        return response

    redactor = build_default_redactor()
    if redactor is None:
        # Loader already logged. Return verbatim — admin path also returns
        # verbatim, so callers see the same unredacted body the upstream
        # produced. Worse than redacted, but no worse than pre-D25 behaviour.
        return response

    events = body.get("events")
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict):
                msg = ev.get("message")
                if isinstance(msg, str) and msg:
                    ev["message"] = redactor.redact(msg)

    lines = body.get("lines")
    if isinstance(lines, list):
        body["lines"] = [
            redactor.redact(ln) if isinstance(ln, str) else ln
            for ln in lines
        ]

    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=200,
        media_type="application/json",
    )


# Jianying draft endpoints (plan §11.7 K6).
# Both require X-Internal-Key forwarding; ownership is verified by the
# intercept_job_subresource caller. No feature flag — always enabled when
# the Job API supports the route.
_JIANYING_DRAFT_SUBPATHS: frozenset[str] = frozenset({
    "generate-jianying-draft",   # POST — trigger on-demand draft generation
    "jianying-draft-status",     # GET  — poll draft generation status
})


# Plan 2026-05-07 §4.7: download path matcher. Matches any single-segment
# artifact key after ``download/``. The actual permission gate (Express vs
# Studio) lives inside ``_resolve_r2_redirect`` via the shared allowlist
# in services/r2_publisher_lib/downloadable_keys.py.
import re as _re

_DOWNLOAD_KEY_RE = _re.compile(r"^download/(?P<key>[a-z0-9_.\-]+)$")

# Plan 2026-05-07 §11.3 C3 (Stage C, 2026-05-12): /stream/{kind} matcher.
# Kinds are explicitly enumerated (video|audio|poster) to mirror
# src/services/jobs/api.py:447-490 and to keep unknown kinds out of the
# Gateway intercept entirely — they pass through to proxy_request and
# Job API returns its own 404. Without this enumeration, the broader
# ``[a-z]+`` pattern would let unknown kinds emit a
# ``stream.fallback.local`` event (CodeX P2 finding, 2026-05-12) and
# pollute the rollout dashboard's fallback-rate metric.
_STREAM_KIND_RE = _re.compile(r"^stream/(?P<kind>video|audio|poster)$")


# Set of subpaths that represent editing STATE TRANSITIONS (need FOR UPDATE
# lock + Gateway DB status sync). Segments mutations are covered by the
# broader _is_post_edit_mutation_subpath check below.
_POST_EDIT_TRANSITION_SUBPATHS: frozenset[str] = frozenset({
    "enter-edit",
    "editing/cancel",
    "editing/commit",
})

# Direct post-edit mutation subpaths (no templating). Union with the
# per-segment action allowlist in ``_is_post_edit_mutation_subpath``.
_POST_EDIT_SIMPLE_MUTATION_SUBPATHS: frozenset[str] = frozenset({
    "regenerate-all-tts",           # T1-6 batch (async start)
    "regenerate-selected-tts",      # targeted batch after bulk terminology replace
    "regenerate-all-tts/cancel",    # 2026-04-21 D39 user-initiated cancel
    "editing/voice-map",            # T1-6 set/clear voice override (POST only)
    "editing/revert-unsynced-text", # discard text edits without matching TTS
    "editing/bulk-replace/preview", # confirmation preview before text mutation
    "editing/bulk-replace/apply",   # confirmed literal text mutation
    "editing/speakers",             # Task 3 (plan 2026-05-04): create new
                                    # editing-mode speaker (POST only; GET is
                                    # read-only and falls through to proxy).
})

# Per-segment action allowlist (templated as ``segments/{sid}/{action}``).
# Promoted from an inline set inside ``_is_post_edit_mutation_subpath`` so
# the test suite can assert set-equality against an EXPECTED list — catches
# both directions of drift (gateway adds without test update, or test
# expects without gateway implementation). Audit P2-21 (2026-05-07).
#
# Kept as an explicit allowlist rather than "any segments/*" so that
# future non-post-edit segment actions are not silently gated off when
# the AVT_ENABLE_POST_EDIT flag is disabled.
_POST_EDIT_SEGMENT_ACTIONS: frozenset[str] = frozenset({
    "update", "status", "regenerate-tts",
    "accept-draft", "discard-draft",
    # 2026-04-21 plan §7.4: editing-mode segment split + source audio
    # preview. Both are editing-gated mutations / reads; keeping them
    # on this allowlist ties them to the feature flag + editing
    # lock dispatch rather than leaking through the generic proxy.
    "split", "preview-source",
    # 2026-05-17 plan §5.6 / Phase 2a: atomic multi-cut split. Same
    # editing-gate semantics as single split; backend kernel
    # (split_editing_segment_many) uses a write-ahead journal for
    # all-or-nothing semantics across segments.json + segment_status
    # + voice_map. Routed through this allowlist for feature-flag
    # parity and lock dispatch alignment with peer mutations.
    "split-many",
    # 2026-05-17 plan §5.4 v2 / Phase 2b v2: LLM-backed split
    # suggestion. User-explicit trigger (paid Gemini call). Rate
    # limit enforced inside the kernel (per-segment 1, per-job cap
    # = MAX(MIN(0.2 × N, anomaly_count), 5)).
    "suggest-split",
})


def _is_post_edit_mutation_subpath(subpath: str) -> bool:
    """Decide whether a job subresource subpath belongs to the post-edit
    surface (both state transitions and segment mutations). Used only for
    the feature flag gate + lock dispatch; ownership is verified separately
    for every subpath via ``_verify_job_ownership``."""
    if subpath in _POST_EDIT_TRANSITION_SUBPATHS:
        return True
    if subpath in _POST_EDIT_SIMPLE_MUTATION_SUBPATHS:
        return True
    parts = subpath.split("/")
    if (
        len(parts) == 3
        and parts[0] == "segments"
        and parts[2] in _POST_EDIT_SEGMENT_ACTIONS
    ):
        return True
    # editing/speakers/{speaker_id}/retry-profile — Task 5 (plan 2026-05-09).
    # Dynamic path (speaker_id is variable) so it can't live in the static
    # _POST_EDIT_SIMPLE_MUTATION_SUBPATHS frozenset. POST mutates per-speaker
    # state (resets profile_status + re-schedules Pass 3 inference), so it
    # MUST be on the editing-gated allowlist.
    if (
        len(parts) == 4
        and parts[0] == "editing"
        and parts[1] == "speakers"
        and parts[3] == "retry-profile"
    ):
        return True
    return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _post_edit_policy_key(user: User | None) -> str:
    if user is None:
        return "admin"
    role = getattr(user, "role", "user") or "user"
    if role == "admin":
        return "admin"
    plan = (getattr(user, "plan_code", "free") or "free").strip().lower()
    if plan in {"plus", "pro"}:
        return plan
    try:
        from plan_catalog import is_user_in_active_trial

        if is_user_in_active_trial(user):
            return "trial"
    except Exception:
        logger.warning("post-edit policy: failed to resolve trial state", exc_info=True)
    return "free"


def _post_edit_limits_for_user(user: User | None) -> dict[str, int | None] | None:
    key = _post_edit_policy_key(user)
    return POST_EDIT_LIMITS.get(key)


def _should_shadow_settle_job_credits(job: Job) -> bool:
    if getattr(job, "copy_of_job_id", None):
        return False
    try:
        if int(getattr(job, "edit_generation", 0) or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _post_edit_root_id(job: Job) -> str:
    return str(job.root_job_id or job.job_id)


def _post_edit_usage(job: Job) -> dict:
    raw_snapshot = getattr(job, "metering_snapshot", None)
    snap = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    usage = snap.get(POST_EDIT_USAGE_KEY)
    return dict(usage) if isinstance(usage, dict) else {}


def _save_post_edit_usage(job: Job, usage: dict) -> None:
    raw_snapshot = getattr(job, "metering_snapshot", None)
    snap = dict(raw_snapshot or {}) if isinstance(raw_snapshot, dict) else {}
    snap[POST_EDIT_USAGE_KEY] = usage
    job.metering_snapshot = snap


async def _post_edit_root_job_for_update(db: AsyncSession, job: Job) -> Job:
    root_id = _post_edit_root_id(job)
    if root_id == job.job_id:
        return job
    result = await db.execute(
        select(Job).where(
            Job.job_id == root_id,
            Job.user_id == job.user_id,
        ).with_for_update()
    )
    return result.scalar_one_or_none() or job


def _post_edit_job_expires_at(job: Job) -> datetime | None:
    if getattr(job, "role_snapshot", None) == "admin":
        return None
    explicit = _as_aware_utc(getattr(job, "expires_at", None))
    if explicit is not None:
        return explicit
    created = _as_aware_utc(getattr(job, "created_at", None))
    if created is None:
        return None
    return created + timedelta(days=7)


def _post_edit_limit_exceeded(
    usage: dict,
    field: str,
    add: int,
    limit: int | None,
) -> bool:
    if limit is None:
        return False
    current = int(usage.get(field) or 0)
    return current + int(add) > int(limit)


def _post_edit_increment(usage: dict, field: str, add: int = 1) -> None:
    usage[field] = int(usage.get(field) or 0) + int(add)


def _post_edit_daily_counter(usage: dict, field: str, today: str) -> dict:
    current = usage.get(field)
    if not isinstance(current, dict) or current.get("date") != today:
        current = {"date": today, "count": 0}
    return dict(current)


async def _post_edit_existing_copy_count(db: AsyncSession, job: Job) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Job)
        .where(
            Job.user_id == job.user_id,
            Job.root_job_id == _post_edit_root_id(job),
            Job.copy_of_job_id.isnot(None),
        )
    )
    return int(result.scalar() or 0)


def _read_post_edit_segments(project_dir: str | None) -> list[dict]:
    if not project_dir:
        return []
    path = Path(project_dir) / "editor" / "editing" / "segments.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict)]


def _segment_cn_chars(project_dir: str | None, segment_id: str) -> int:
    target = str(segment_id)
    for segment in _read_post_edit_segments(project_dir):
        if str(segment.get("segment_id")) == target:
            return len(str(segment.get("cn_text") or "").strip())
    raise HTTPException(status_code=409, detail=f"segment {segment_id!r} not found in editing buffer")


def _batch_regen_scope(project_dir: str | None) -> tuple[int, int]:
    if not project_dir:
        return 0, 0
    status_path = Path(project_dir) / "editor" / "editing" / "segment_status.json"
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        status_data = {}
    if not isinstance(status_data, dict):
        status_data = {}
    eligible = {
        str(sid)
        for sid, status in status_data.items()
        if str(status) in POST_EDIT_BATCH_TRIGGER_STATUSES
    }
    if not eligible:
        return 0, 0
    total_chars = 0
    for segment in _read_post_edit_segments(project_dir):
        sid = str(segment.get("segment_id"))
        if sid in eligible:
            total_chars += len(str(segment.get("cn_text") or "").strip())
    return len(eligible), total_chars


def _selected_regen_scope(
    project_dir: str | None,
    segment_ids: list[str],
) -> tuple[int, int]:
    if not project_dir:
        return 0, 0
    requested: list[str] = []
    seen: set[str] = set()
    for raw_sid in segment_ids:
        sid = str(raw_sid).strip()
        if not sid or sid in seen:
            continue
        requested.append(sid)
        seen.add(sid)
    if not requested:
        return 0, 0
    status_path = Path(project_dir) / "editor" / "editing" / "segment_status.json"
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        status_data = {}
    if not isinstance(status_data, dict):
        status_data = {}
    eligible = {
        sid
        for sid in requested
        if str(status_data.get(sid)) in POST_EDIT_BATCH_TRIGGER_STATUSES
    }
    if not eligible:
        return 0, 0
    total_chars = 0
    for segment in _read_post_edit_segments(project_dir):
        sid = str(segment.get("segment_id"))
        if sid in eligible:
            total_chars += len(str(segment.get("cn_text") or "").strip())
    return len(eligible), total_chars


async def _read_json_body(request: Request) -> dict:
    try:
        raw = await request.body()
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def _enforce_post_edit_access(
    db: AsyncSession,
    job: Job,
    user: User | None,
    *,
    subpath: str,
    now_utc: datetime,
) -> tuple[Job, dict[str, int | None]]:
    limits = _post_edit_limits_for_user(user)
    if limits is None:
        raise HTTPException(
            status_code=403,
            detail="当前套餐不支持工作台修改流程。",
        )
    expires_at = _post_edit_job_expires_at(job)
    if expires_at is not None and now_utc >= expires_at:
        raise HTTPException(
            status_code=410,
            detail="项目已超过 7 天保存期，不能继续修改。",
        )
    root_job = await _post_edit_root_job_for_update(db, job)
    return root_job, limits


async def _check_post_edit_commit_limit(
    db: AsyncSession,
    job: Job,
    user: User | None,
    *,
    strategy: str,
    now_utc: datetime,
) -> None:
    root_job, limits = await _enforce_post_edit_access(
        db, job, user, subpath="editing/commit", now_utc=now_utc,
    )
    usage = _post_edit_usage(root_job)
    if strategy == "overwrite":
        if _post_edit_limit_exceeded(
            usage, "overwrite_commits", 1, limits["overwrite_commits"]
        ):
            raise HTTPException(status_code=403, detail="本项目免费覆盖保存次数已用完。")
        return
    if strategy == "copy_as_new":
        existing_copies = await _post_edit_existing_copy_count(db, job)
        used = max(int(usage.get("copy_as_new") or 0), existing_copies)
        if limits["copy_as_new"] is not None and used + 1 > int(limits["copy_as_new"]):
            raise HTTPException(status_code=403, detail="本项目免费另存副本次数已用完。")
        return


async def _record_post_edit_commit_usage(
    db: AsyncSession,
    source_job: Job,
    *,
    strategy: str,
) -> None:
    root_job = await _post_edit_root_job_for_update(db, source_job)
    usage = _post_edit_usage(root_job)
    if strategy == "overwrite":
        _post_edit_increment(usage, "overwrite_commits")
    elif strategy == "copy_as_new":
        _post_edit_increment(usage, "copy_as_new")
    _save_post_edit_usage(root_job, usage)


async def _check_post_edit_tts_usage(
    db: AsyncSession,
    job: Job,
    user: User | None,
    *,
    segments: int,
    chars: int,
    batch_start: bool,
    now_utc: datetime,
) -> None:
    root_job, limits = await _enforce_post_edit_access(
        db, job, user, subpath="regenerate-tts", now_utc=now_utc,
    )
    usage = _post_edit_usage(root_job)
    if segments <= 0:
        return
    if batch_start and _post_edit_limit_exceeded(
        usage, "batch_regenerates", 1, limits["batch_regenerates"]
    ):
        raise HTTPException(status_code=403, detail="本项目免费批量重合成次数已用完。")
    if _post_edit_limit_exceeded(usage, "tts_segments", segments, limits["tts_segments"]):
        raise HTTPException(status_code=403, detail="本项目免费重合成段数已用完。")
    if _post_edit_limit_exceeded(usage, "tts_chars", chars, limits["tts_chars"]):
        raise HTTPException(status_code=403, detail="本项目免费重合成字数已用完。")


async def _consume_post_edit_tts_usage(
    db: AsyncSession,
    job: Job,
    user: User | None,
    *,
    segments: int,
    chars: int,
    batch_start: bool,
    now_utc: datetime,
) -> None:
    root_job, limits = await _enforce_post_edit_access(
        db, job, user, subpath="regenerate-tts", now_utc=now_utc,
    )
    usage = _post_edit_usage(root_job)
    if segments <= 0:
        return
    if batch_start and _post_edit_limit_exceeded(
        usage, "batch_regenerates", 1, limits["batch_regenerates"]
    ):
        raise HTTPException(status_code=403, detail="本项目免费批量重合成次数已用完。")
    if _post_edit_limit_exceeded(usage, "tts_segments", segments, limits["tts_segments"]):
        raise HTTPException(status_code=403, detail="本项目免费重合成段数已用完。")
    if _post_edit_limit_exceeded(usage, "tts_chars", chars, limits["tts_chars"]):
        raise HTTPException(status_code=403, detail="本项目免费重合成字数已用完。")
    if batch_start:
        _post_edit_increment(usage, "batch_regenerates")
    _post_edit_increment(usage, "tts_segments", segments)
    _post_edit_increment(usage, "tts_chars", chars)
    _save_post_edit_usage(root_job, usage)


async def _consume_post_edit_preview_usage(
    db: AsyncSession,
    job: Job,
    user: User | None,
    *,
    now_utc: datetime,
) -> None:
    root_job, limits = await _enforce_post_edit_access(
        db, job, user, subpath="review/voice/preview", now_utc=now_utc,
    )
    usage = _post_edit_usage(root_job)
    today = now_utc.date().isoformat()
    counter = _post_edit_daily_counter(usage, "preview_voice_daily", today)
    limit = limits["preview_voice_daily"]
    if limit is not None and int(counter.get("count") or 0) + 1 > int(limit):
        raise HTTPException(status_code=403, detail="今日免费试听次数已用完。")
    counter["count"] = int(counter.get("count") or 0) + 1
    usage["preview_voice_daily"] = counter
    _save_post_edit_usage(root_job, usage)


async def _enrich_editing_voice_map_routing(
    db: AsyncSession,
    *,
    user_id: object,
    payload: dict,
) -> tuple[dict | None, dict | None]:
    """Phase 4.2 E.1 PR #15 P1 三轮 fix (Codex 2026-05-27): mirror of
    ``_enrich_speakers_with_clone_routing`` but for the editing/voice-map
    POST single-segment override path.

    The frontend ``setVoiceOverride`` only sends
    ``{segment_id, provider, voice_id, tts_model_key?, voice_reuse?}``.
    For a CosyVoice user clone voice this would persist into
    voice_map.json without ``requires_worker`` / ``worker_target_model``,
    causing the editor commit / regenerate-tts paths to fall back to
    legacy CosyVoice (builtin catalog) and the clone voice silently
    doesn't take effect.

    This enrichment intercepts the payload before forwarding to the
    upstream Job API:

    - Always strips client-supplied ``requires_worker`` /
      ``worker_target_model`` (defense in depth — server is the only
      authority).
    - For non-cosyvoice / non-clone paths: passes through unchanged.
    - For CosyVoice clone voice (matches user_voices row): injects
      ``requires_worker=True`` + ``worker_target_model=<target_model>``.
    - For voice_id that looks like a CosyVoice clone (cosyvoice provider
      but not in public catalog) but no user_voices match: 400
      ``voice_clone_metadata_missing`` fail-closed (don't let an orphan
      clone id drift through and downgrade to legacy CosyVoice).
    - DB lookup transient failure for cosyvoice path: 503
      ``voice_clone_routing_lookup_failed``.

    Args
    ----
    user_id: caller's user_id (used for the user_voices ownership filter).
    payload: parsed editing/voice-map POST body. Mutated in place is OK
        but we return a fresh dict to make the caller's re-serialize
        explicit.

    Returns
    -------
    (enriched_payload, error_detail)
        - ``(new_dict, None)``: payload was mutated (or strip-only); caller
          MUST re-serialize body.
        - ``(None, None)``: no enrichment needed (clear action, no
          provider, MiniMax / VolcEngine).
        - ``(None, {code, message})``: fail-closed; caller returns 400/503.
    """
    from user_voice_service import lookup_clone_voice_routing_metadata

    if not isinstance(payload, dict):
        return None, None

    # ``clear`` actions don't write a voice_id, no enrichment needed.
    action = str(payload.get("action") or "set").strip()
    if action == "clear":
        return None, None

    # Always strip client-supplied routing fields BEFORE doing our own
    # lookup. Even if we return early below, the upstream Job API must
    # never see a forged ``requires_worker``.
    new_payload = dict(payload)
    new_payload.pop("requires_worker", None)
    new_payload.pop("worker_target_model", None)

    provider = str(new_payload.get("provider") or "").strip()
    # ``tts_provider`` is a synonym older callers sometimes use; we accept
    # either but treat absence as "unspecified" rather than auto-routing.
    tts_provider = str(
        new_payload.get("tts_provider") or new_payload.get("provider") or ""
    ).strip()
    voice_id = str(new_payload.get("voice_id") or "").strip()

    if not voice_id:
        # Storage layer will reject this with a 400; nothing to enrich.
        return new_payload, None

    # Only CosyVoice-flavoured overrides need enrichment. MiniMax /
    # VolcEngine paths leave the stripped payload alone.
    if provider != "cosyvoice" and tts_provider != "cosyvoice":
        return new_payload, None

    # Cosyvoice path — do the DB lookups.
    try:
        routing_map = await lookup_clone_voice_routing_metadata(
            db, user_id=user_id, voice_ids=[voice_id],
        )
    except Exception as exc:
        logger.warning(
            "editing/voice-map: clone routing lookup failed for "
            "user=%s voice=%s: %s",
            user_id, voice_id, exc,
        )
        return None, {
            "code": "voice_clone_routing_lookup_failed",
            "message": (
                "音色路由查询暂时不可用，请稍后重试。"
                "（CosyVoice 克隆音色需要查证后才能写入编辑覆盖）"
            ),
            "voice_id": voice_id,
        }

    if voice_id in routing_map:
        new_payload["requires_worker"] = True
        target_model = str(routing_map[voice_id].get("worker_target_model") or "").strip()
        if target_model:
            new_payload["worker_target_model"] = target_model
        # Force tts_provider canonical value for downstream sanity (storage
        # layer + voice_map.json normalisation).
        new_payload["provider"] = "cosyvoice"
        return new_payload, None

    # Not in user_voices. Could be a public builtin → allow; otherwise
    # fail-closed (don't let an orphan clone-shaped id drift to legacy).
    try:
        public_voice_ids = await _fetch_cosyvoice_public_voice_ids(db)
    except Exception as exc:
        logger.warning(
            "editing/voice-map: public catalog lookup failed for voice=%s: %s",
            voice_id, exc,
        )
        return None, {
            "code": "voice_clone_routing_lookup_failed",
            "message": (
                "音色路由查询暂时不可用，请稍后重试。"
                "（无法核对 CosyVoice 是否为公开预设）"
            ),
            "voice_id": voice_id,
        }

    if voice_id in public_voice_ids:
        # Builtin / public preset — no routing needed.
        return new_payload, None

    return None, {
        "code": "voice_clone_metadata_missing",
        "message": (
            f"voice_id={voice_id!r} 是 CosyVoice 但既不在公开音色库，"
            f"也不在你的克隆音色库中。请重新选择音色。"
        ),
        "voice_id": voice_id,
    }


async def _post_edit_mutation_with_policy(
    request: Request,
    job_id: str,
    db: AsyncSession,
    user: User | None,
    *,
    subpath: str,
) -> Response:
    result = await db.execute(select(Job).where(Job.job_id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    if job is None:
        return await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
        )
    if job.status != "editing":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not in editing state: {job.status}",
        )

    now_utc = _utc_now()
    await _enforce_post_edit_access(db, job, user, subpath=subpath, now_utc=now_utc)

    parts = subpath.split("/")
    if len(parts) == 3 and parts[0] == "segments" and parts[2] == "update":
        payload = await _read_json_body(request)
        cn_text = payload.get("cn_text")
        if cn_text is not None and len(str(cn_text).strip()) > POST_EDIT_MAX_SEGMENT_SAVE_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"单段译文字数不能超过 {POST_EDIT_MAX_SEGMENT_SAVE_CHARS} 字，请拆分后再保存。",
            )

    if subpath == "editing/bulk-replace/apply":
        payload = await _read_json_body(request)
        find_text = str(payload.get("find") or "")
        replace_text = str(payload.get("replace") or "")
        if find_text:
            for segment in _read_post_edit_segments(job.project_dir):
                current = str(segment.get("cn_text") or "")
                if find_text not in current:
                    continue
                after = current.replace(find_text, replace_text)
                if len(after.strip()) > POST_EDIT_MAX_SEGMENT_SAVE_CHARS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"单段译文字数不能超过 {POST_EDIT_MAX_SEGMENT_SAVE_CHARS} 字，请拆分后再保存。",
                    )

    if len(parts) == 3 and parts[0] == "segments" and parts[2] == "regenerate-tts":
        chars = _segment_cn_chars(job.project_dir, parts[1])
        await _consume_post_edit_tts_usage(
            db, job, user, segments=1, chars=chars, batch_start=False, now_utc=now_utc,
        )

    if subpath == "regenerate-all-tts":
        segments, chars = _batch_regen_scope(job.project_dir)
        batch_segment_limit = (
            POST_EDIT_BATCH_MAX_SEGMENTS_PRO
            if _post_edit_policy_key(user) in {"pro", "admin"}
            else POST_EDIT_BATCH_MAX_SEGMENTS_DEFAULT
        )
        if segments > batch_segment_limit:
            raise HTTPException(
                status_code=400,
                detail=f"单次批量重合成最多支持 {batch_segment_limit} 段。",
            )
        await _consume_post_edit_tts_usage(
            db, job, user, segments=segments, chars=chars, batch_start=True, now_utc=now_utc,
        )

    selected_regen_usage: tuple[int, int] | None = None
    selected_regen_body: bytes | None = None

    if subpath == "regenerate-selected-tts":
        payload = await _read_json_body(request)
        raw_segment_ids = payload.get("segment_ids")
        if not isinstance(raw_segment_ids, list):
            raise HTTPException(status_code=400, detail="segment_ids must be a list")
        segment_ids = [
            str(item).strip()
            for item in raw_segment_ids
            if str(item).strip()
        ]
        segments, chars = _selected_regen_scope(job.project_dir, segment_ids)
        batch_segment_limit = (
            POST_EDIT_BATCH_MAX_SEGMENTS_PRO
            if _post_edit_policy_key(user) in {"pro", "admin"}
            else POST_EDIT_BATCH_MAX_SEGMENTS_DEFAULT
        )
        if segments > batch_segment_limit:
            raise HTTPException(
                status_code=400,
                detail=f"单次批量重合成最多支持 {batch_segment_limit} 段。",
            )
        await _check_post_edit_tts_usage(
            db, job, user, segments=segments, chars=chars, batch_start=True, now_utc=now_utc,
        )
        selected_regen_usage = (segments, chars)
        selected_regen_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # Phase 4.2 E.1 PR #15 P1 三轮 fix (Codex 2026-05-27): editing/voice-map
    # CosyVoice clone routing enrichment. We intercept ONLY the voice-map
    # POST (not GET, not other editing subpaths) and ONLY when the body
    # carries a cosyvoice voice. Other paths fall through unchanged.
    forwarded_body: bytes | None = None
    if subpath == "editing/voice-map" or subpath == "voice-map":
        # Note: subpath here comes from the call site which already
        # stripped the ``editing/`` prefix in some routes. Be tolerant
        # of either shape so future routing refactors don't break the
        # enrichment. The actual subpath used by the existing tests is
        # ``editing/voice-map`` (full path under /job-api/jobs/{id}/).
        try:
            payload = await _read_json_body(request)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            user_id_for_lookup = getattr(user, "id", None) if user else None
            enriched, error = await _enrich_editing_voice_map_routing(
                db, user_id=user_id_for_lookup, payload=payload,
            )
            if error is not None:
                error_status = (
                    503
                    if error.get("code") == "voice_clone_routing_lookup_failed"
                    else 400
                )
                return JSONResponse(
                    status_code=error_status,
                    content={"detail": error},
                )
            if enriched is not None:
                # Re-serialize so upstream Job API sees the routing
                # fields (and the stripped-client-supplied state).
                forwarded_body = json.dumps(enriched, ensure_ascii=False).encode("utf-8")

    if selected_regen_usage is not None:
        response = await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
            override_body=selected_regen_body,
        )
        if 200 <= response.status_code < 300:
            segments, chars = selected_regen_usage
            await _consume_post_edit_tts_usage(
                db,
                job,
                user,
                segments=segments,
                chars=chars,
                batch_start=True,
                now_utc=now_utc,
            )
            await db.commit()
        else:
            await db.rollback()
        return response

    await db.commit()
    if forwarded_body is not None:
        return await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
            override_body=forwarded_body,
        )
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def _enrich_voice_preview_routing(
    db: AsyncSession,
    *,
    user_id: object,
    payload: dict,
) -> tuple[dict | None, dict | None]:
    """Inject worker routing into review voice preview for CosyVoice clones.

    The frontend sends only ``voice_id`` + ``tts_provider``.  For cloned
    CosyVoice voices, the target model is stored in ``user_voices`` and must
    be resolved by Gateway, not trusted from the client.

    Returns ``(enriched_payload, error_detail)``. ``enriched_payload`` is
    ``None`` when the original body can pass through unchanged.
    """
    new_payload = dict(payload)
    had_client_routing = (
        "requires_worker" in new_payload or "worker_target_model" in new_payload
    )
    new_payload.pop("requires_worker", None)
    new_payload.pop("worker_target_model", None)

    voice_id = str(new_payload.get("voice_id", "") or "").strip()
    if not voice_id:
        return (new_payload if had_client_routing else None), None

    tts_provider = str(new_payload.get("tts_provider", "") or "").strip().lower()
    if user_id is None:
        return (new_payload if had_client_routing else None), None

    from user_voice_service import lookup_clone_voice_routing_metadata

    try:
        routing_map = await lookup_clone_voice_routing_metadata(
            db,
            user_id=user_id,
            voice_ids=[voice_id],
        )
    except Exception as exc:
        if tts_provider == "cosyvoice":
            return None, {
                "code": "voice_clone_routing_lookup_failed",
                "message": (
                    "Could not verify CosyVoice clone routing metadata for "
                    f"voice_id={voice_id!r}; refusing to preview via legacy path."
                ),
                "voice_id": voice_id,
            }
        logger.warning(
            "voice preview: clone routing lookup failed for voice_id=%s: %s",
            voice_id,
            exc,
        )
        return (new_payload if had_client_routing else None), None

    routing = routing_map.get(voice_id)
    if not routing:
        return (new_payload if had_client_routing else None), None

    if tts_provider and tts_provider != "cosyvoice":
        return None, {
            "code": "voice_clone_provider_mismatch",
            "message": (
                f"voice_id={voice_id!r} is a CosyVoice clone voice, but "
                f"submitted tts_provider={tts_provider!r}."
            ),
            "voice_id": voice_id,
            "submitted_tts_provider": tts_provider,
        }

    new_payload["tts_provider"] = "cosyvoice"
    new_payload["requires_worker"] = True
    target_model = str(routing.get("worker_target_model") or "").strip()
    if target_model:
        new_payload["worker_target_model"] = target_model
    return new_payload, None


async def _post_edit_voice_preview_with_policy(
    request: Request,
    job_id: str,
    db: AsyncSession,
    user: User | None,
) -> Response:
    result = await db.execute(select(Job).where(Job.job_id == job_id).with_for_update())
    job = result.scalar_one_or_none()
    forwarded_body: bytes | None = None
    if job is not None:
        try:
            payload = await _read_json_body(request)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            user_id_for_lookup = getattr(job, "user_id", None) or (
                getattr(user, "id", None) if user else None
            )
            enriched, error = await _enrich_voice_preview_routing(
                db, user_id=user_id_for_lookup, payload=payload,
            )
            if error is not None:
                error_status = (
                    503
                    if error.get("code") == "voice_clone_routing_lookup_failed"
                    else 400
                )
                return JSONResponse(
                    status_code=error_status,
                    content={"detail": error},
                )
            if enriched is not None:
                forwarded_body = json.dumps(enriched, ensure_ascii=False).encode("utf-8")
    if job is not None and job.status == "editing":
        await _consume_post_edit_preview_usage(db, job, user, now_utc=_utc_now())
        await db.commit()
    if forwarded_body is not None:
        return await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
            override_body=forwarded_body,
        )
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


def _accepted_overwrite_commit_response(job: Job) -> Response | None:
    status = getattr(job, "status", None)
    if status not in {"running", "succeeded"}:
        return None
    try:
        edit_generation = int(getattr(job, "edit_generation", 0) or 0)
    except (TypeError, ValueError):
        return None
    if edit_generation <= 0:
        return None
    if getattr(job, "editing_touched_at", None) is not None:
        return None
    project_dir = getattr(job, "project_dir", None)
    if not project_dir:
        return None
    if (Path(project_dir) / "editor" / "editing").exists():
        return None
    body = {
        "success": True,
        "strategy": "overwrite",
        "job_id": getattr(job, "job_id", None),
        "edit_generation": edit_generation,
        "current_stage": getattr(job, "current_stage", None) or "alignment",
        "already_started": True,
        "already_completed": status == "succeeded",
    }
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=200,
        headers={"content-type": "application/json"},
    )


async def _editing_transition_with_lock(
    request: Request,
    job_id: str,
    db: AsyncSession,
    user: User | None,
    *,
    subpath: str,
) -> Response:
    """FOR UPDATE lock + pre-condition check + proxy + conditional DB sync.

    Per-subpath behaviour:

    - ``enter-edit``     : expect status='succeeded'; on upstream 2xx set
      status='editing' + editing_touched_at=now.
    - ``editing/cancel`` : expect status='editing'; on upstream 2xx set
      status='succeeded' + editing_touched_at=NULL.
    - ``editing/commit`` : expect status='editing'. Upstream T1-9 returns
      200 with a dict whose shape depends on strategy:
        overwrite     → {strategy, job_id, edit_generation, ...}
                        Gateway flips source row to running + bumps
                        edit_generation + clears editing_touched_at +
                        stamps current_stage='alignment'.
        copy_as_new   → {strategy, source_job_id, new_job_id,
                         new_project_dir, new_display_name, ...}
                        Gateway:
                          1. Resets source row: status='succeeded',
                             editing_touched_at=NULL (Phase B mirror).
                          2. INSERTs a new Jobs row carrying most fields
                             from source + new IDs + copy lineage +
                             expires_at computed via the same rule the
                             Job-API store uses.

    Legacy jobs without a Gateway row skip the lock (same as ``continue``);
    upstream handles validation for them.
    """
    expected_status_by_subpath = {
        "enter-edit": "succeeded",
        "editing/cancel": "editing",
        "editing/commit": "editing",
    }
    expected = expected_status_by_subpath[subpath]

    result = await db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is not None and job.status != expected:
        if subpath == "editing/commit":
            payload = await _read_json_body(request)
            strategy = str(payload.get("strategy") or "").strip()
            if strategy == "overwrite":
                accepted_response = _accepted_overwrite_commit_response(job)
                if accepted_response is not None:
                    await db.commit()
                    return accepted_response
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is not in the expected state for {subpath!r}: "
                f"expected {expected!r}, got {job.status!r}"
            ),
        )

    now_utc = _utc_now()
    if job is not None and subpath == "enter-edit":
        await _enforce_post_edit_access(
            db, job, user, subpath=subpath, now_utc=now_utc,
        )
    elif job is not None and subpath == "editing/commit":
        payload = await _read_json_body(request)
        strategy = str(payload.get("strategy") or "").strip()
        if strategy:
            await _check_post_edit_commit_limit(
                db, job, user, strategy=strategy, now_utc=now_utc,
            )

    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    if job is not None and 200 <= response.status_code < 300:
        if subpath == "enter-edit":
            job.status = "editing"
            job.editing_touched_at = now_utc
        elif subpath == "editing/cancel":
            job.status = "succeeded"
            job.editing_touched_at = None
        elif subpath == "editing/commit":
            await _apply_editing_commit_gateway_side(
                db, job, response, now_utc=now_utc,
            )

    await db.commit()
    return response


async def _apply_editing_commit_gateway_side(
    db: AsyncSession,
    source_job: Job,
    upstream_response: Response,
    *,
    now_utc,
) -> None:
    """After Job-API's editing/commit returns 2xx, sync Gateway DB.

    Reads the upstream body to decide which strategy was executed:

    - overwrite: promote source row status → running, edit_generation += 1,
      editing_touched_at cleared, current_stage stamped 'alignment'. Same
      row is re-used; no INSERT.
    - copy_as_new: reset source → succeeded (Phase B mirror) + INSERT a
      fresh Jobs row for the copy with lineage fields populated.

    Failure modes are soft: if parse / INSERT fails we log prominently but
    do not revert the upstream response — it already succeeded at Job-API
    layer, and flipping the source back would create a messier state. An
    admin can reconcile via list_jobs / PG direct edit.
    """
    from datetime import timedelta as _td

    try:
        body = json.loads(upstream_response.body.decode("utf-8"))
    except Exception:
        logger.warning(
            "editing/commit gateway-side: upstream body not JSON; skipping sync"
        )
        return

    strategy = body.get("strategy")
    if strategy == "overwrite":
        await _record_post_edit_commit_usage(db, source_job, strategy="overwrite")
        source_job.status = "running"
        source_job.current_stage = "alignment"
        source_job.edit_generation = (source_job.edit_generation or 0) + 1
        source_job.editing_touched_at = None
        # Plan 2026-05-07 §4.7 + §2.4: clear the R2 publish registry so
        # the sweeper re-pushes under the new edit_generation. Without
        # this, the bumped generation would have no registry entries
        # (download path returns None → byte-passthrough), and old g{N-1}
        # entries (with the previous generation stamp) would never be
        # served because the download path matches by edit_generation.
        # Resetting r2_push_retry_after lets sweeper pick the row up
        # immediately on its next pass instead of waiting for any
        # leftover backoff to expire.
        source_job.r2_artifacts = None
        source_job.r2_push_retry_after = None
        # Invalidate any pre-edit materials_pack — its zip captures the
        # pre-edit SRT / audio / caption text, which becomes stale once
        # alignment+publish re-runs against the just-applied edits. The
        # editing_commit Job-API helper already invalidates the Jianying
        # draft (it lives on JobRecord + project_dir); materials_pack
        # lives in Gateway DB, so this is the only seam where we can
        # reach it. No-op for jobs that never packed.
        from background_task_queue import invalidate_materials_pack_for_job
        invalidated = await invalidate_materials_pack_for_job(
            db, job_id=source_job.job_id, now=now_utc,
        )
        if invalidated:
            logger.info(
                "editing/commit overwrite: invalidated %d materials_pack "
                "row(s) for job %s (zips unlinked / pending tasks failed)",
                invalidated, source_job.job_id,
            )
        return

    if strategy != "copy_as_new":
        logger.info(
            "editing/commit gateway-side: unknown strategy=%r; no DB mutation",
            strategy,
        )
        return

    # copy_as_new Phase B mirror
    await _record_post_edit_commit_usage(db, source_job, strategy="copy_as_new")
    new_job_id = str(body.get("new_job_id") or "").strip()
    new_display_name = str(body.get("new_display_name") or "").strip()
    new_project_dir = body.get("new_project_dir")
    if not new_job_id:
        logger.warning(
            "editing/commit copy_as_new: upstream response missing new_job_id; "
            "source job will still be reset to succeeded but new row will NOT "
            "be inserted into Gateway DB — admin must reconcile"
        )
    # Reset source row (Phase B)
    source_job.status = "succeeded"
    source_job.editing_touched_at = None

    if not new_job_id:
        return

    # Idempotency: if a prior run already inserted this row (retry), skip
    existing = await db.execute(
        select(Job).where(Job.job_id == new_job_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info(
            "editing/commit copy_as_new: job_id=%s already in Gateway DB; skipping INSERT",
            new_job_id,
        )
        return

    # TTL for the copy — plan §5.1 simplified form:
    #   min(now + 7d, most_recent_live_sibling.expires_at + 24h)
    # We scope by (user_id, root_job_id). If no live sibling exists,
    # fall back to now + 7d (same as first-copy rule).
    seven_days_later = now_utc + _td(days=7)
    source_root_id = source_job.root_job_id or source_job.job_id
    sibling_q = await db.execute(
        select(Job.expires_at)
        .where(
            Job.user_id == source_job.user_id,
            Job.root_job_id == source_root_id,
            Job.expires_at.isnot(None),
            Job.expires_at > now_utc,
            Job.job_id != source_job.job_id,
        )
        .order_by(Job.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    sibling_expires = sibling_q.scalar_one_or_none()
    if getattr(source_job, "role_snapshot", None) == "admin":
        copy_expires = None
    elif sibling_expires is not None:
        copy_expires = min(seven_days_later, sibling_expires + _td(hours=24))
    else:
        copy_expires = seven_days_later

    copy_row = Job(
        job_id=new_job_id,
        user_id=source_job.user_id,
        source_type=source_job.source_type,
        source_ref=source_job.source_ref,
        title=source_job.title,
        speakers=source_job.speakers,
        status="running",  # runner has already accepted the new job
        current_stage="alignment",
        project_dir=str(new_project_dir) if new_project_dir else None,
        review_gate=None,
        error_summary=None,
        service_mode=source_job.service_mode,
        tts_provider=source_job.tts_provider,
        tts_model=source_job.tts_model,
        requires_review=source_job.requires_review,
        voice_clone_enabled=source_job.voice_clone_enabled,
        voice_strategy=source_job.voice_strategy,
        plan_code_snapshot=source_job.plan_code_snapshot,
        role_snapshot=source_job.role_snapshot,
        source_duration_seconds=source_job.source_duration_seconds,
        quota_cost=0,
        quota_state="none",
        estimated_duration_seconds=source_job.estimated_duration_seconds,
        create_idempotency_key=None,
        # Post-edit lineage
        display_name=new_display_name or None,
        expires_at=copy_expires,
        editing_touched_at=None,
        copy_of_job_id=source_job.job_id,
        root_job_id=source_root_id,
        edit_generation=0,
        source_content_hash=source_job.source_content_hash,
        # --- Language fields: copy verbatim from the source row so the copy
        # preserves the original pair (feedback_copy_as_new_invariants —
        # copy_as_new must explicitly list every identity field). ---
        source_language=source_job.source_language,
        target_language=source_job.target_language,
        language_pair=source_job.language_pair,
    )
    db.add(copy_row)
    logger.info(
        "editing/commit copy_as_new: mirrored new job %s → Gateway DB (copy_of=%s, root=%s)",
        new_job_id, source_job.job_id, source_root_id,
    )


async def intercept_delete_job_v2(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """DELETE /job-api/jobs/{job_id} — verify ownership, forward to Job API, then cleanup quota + PostgreSQL.

    Phase 3: replaces the old POST /api/job/delete flow for active callers.
    """
    await _verify_job_ownership(job_id, db, user)

    # Forward DELETE to Job API
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If upstream succeeded, release quota then remove from PostgreSQL
    if upstream_response.status_code == 200:
        try:
            result = await db.execute(select(Job).where(Job.job_id == job_id))
            job_row = result.scalar_one_or_none()
            if job_row is not None:
                from quota import release_quota as _release_quota
                await _release_quota(db, job_row)
            await db.execute(delete(Job).where(Job.job_id == job_id))
            await db.commit()
            logger.info("Deleted job %s from PostgreSQL (quota released)", job_id)
        except Exception:
            logger.exception("Failed to delete job %s from PostgreSQL", job_id)

    return upstream_response


async def _verify_job_ownership(
    job_id: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Check that authenticated user owns the job. Raises 403 if not."""
    if not settings.auth_required or user is None:
        return
    result = await db.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
    )
    if result.scalar_one_or_none() is None:
        result2 = await db.execute(select(Job).where(Job.job_id == job_id))
        if result2.scalar_one_or_none() is not None:
            raise HTTPException(status_code=403, detail="无权访问此任务")
        else:
            logger.warning("Job %s not found in DB — denying access", job_id)
            raise HTTPException(status_code=404, detail="任务不存在")


# ---------------------------------------------------------------------------
# Rename endpoint — plan §6.5 / D16
# ---------------------------------------------------------------------------

# Forbidden characters in a user-provided display_name (plan §6.5):
# ``< > " / \ \0``. Path separators + shell special chars + null byte.
_FORBIDDEN_DISPLAY_NAME_CHARS = re.compile(r'[<>"/\\\x00]')

# Display-width budget for a renamed title. Matches the pure algorithm's
# MAX_TOTAL_WIDTH (24 for title + 5 for ``_xxxx`` suffix = 29). Anything
# wider is rejected up-front so the user sees a clean error rather than
# silent server-side truncation.
_MAX_RENAME_DISPLAY_WIDTH = 29


async def intercept_rename_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """PATCH /gateway/jobs/{job_id} — user-initiated rename (plan §6.5 / D16).

    Body: ``{"display_name": "新名"}``.

    Flow:
      1. Ownership check (403 if the job is someone else's).
      2. Validate the new name (non-empty, width ≤ 29, no forbidden chars).
      3. Resolve collisions against the user's *other* jobs — renaming to
         one's own current value is a no-op, not a suffix trigger.
      4. Forward to Job API ``PATCH /jobs/{id}`` so the JSON store stays
         authoritative.
      5. Mirror the persisted name into gateway PostgreSQL.
    """
    await _verify_job_ownership(job_id, db, user)

    try:
        body = await request.body()
        data = json.loads(body) if body else {}
    except Exception:
        return _error_response(400, "invalid_json", "请求体必须是 JSON。")

    if "display_name" not in data:
        return _error_response(
            400, "missing_display_name",
            "请求体必须包含 display_name 字段。",
        )

    raw_name = data.get("display_name")
    if raw_name is None:
        return _error_response(
            400, "invalid_display_name", "display_name 不能为 null。",
        )
    stripped = str(raw_name).strip()
    if not stripped:
        return _error_response(
            400, "invalid_display_name", "display_name 不能为空。",
        )
    if _FORBIDDEN_DISPLAY_NAME_CHARS.search(stripped):
        return _error_response(
            400, "invalid_display_name_chars",
            '任务名不能包含 < > " / \\ 或空字符。',
        )

    # Width check uses the same CJK-aware helper the pure naming algorithm
    # relies on. Import-style matches the rest of gateway: the container
    # has ``src/`` on sys.path (not project root), so we use the unprefixed
    # ``services.*`` / ``utils.*`` forms.
    from utils.text_width import display_width
    from services.jobs.display_name import resolve_collision

    if display_width(stripped) > _MAX_RENAME_DISPLAY_WIDTH:
        return _error_response(
            400, "display_name_too_long",
            "任务名超过长度上限（约 12 个中文字符）。",
        )

    # Collision pool = this user's OTHER jobs. Excluding self lets the user
    # submit their current name unchanged (e.g. "reconfirming" a rename
    # through the same dialog) without getting an ``_xxxx`` suffix.
    existing_names: set[str] = set()
    if user is not None:
        existing_result = await db.execute(
            select(Job.display_name).where(
                Job.user_id == user.id,
                Job.job_id != job_id,
                Job.display_name.is_not(None),
            )
        )
        existing_names = {row[0] for row in existing_result.all() if row[0]}

    final_name = resolve_collision(stripped, existing_names)[:60]

    # Forward to Job API. We do a direct httpx call (not proxy_request)
    # because we want the *resolved* name in the body, not the user's raw
    # input — proxy_request would faithfully forward the original body.
    import httpx
    upstream_url = f"{settings.job_api_upstream.rstrip('/')}/jobs/{job_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.patch(
                upstream_url,
                json={"display_name": final_name},
                headers={"content-type": "application/json"},
            )
    except Exception as exc:
        logger.error("rename upstream call failed for %s: %s", job_id, exc)
        return _error_response(
            502, "upstream_failed",
            "Job API 调用失败，重命名未保存。",
        )

    if upstream.status_code != 200:
        # Pass through upstream error body unchanged so the user sees the
        # real reason (e.g. 404 if the job was deleted between ownership
        # check and PATCH — a race we tolerate rather than add another
        # lock for).
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={"content-type": "application/json"},
        )

    # Mirror into gateway PostgreSQL. We don't roll back the Job API write
    # on gateway-DB failure — the JSON store is authoritative; gateway DB
    # will re-sync on next list_jobs pass.
    if user is not None:
        try:
            await db.execute(
                update(Job)
                .where(Job.job_id == job_id, Job.user_id == user.id)
                .values(display_name=final_name)
            )
            await db.commit()
        except Exception as exc:
            logger.warning(
                "gateway DB mirror failed for rename %s: %s (upstream still OK)",
                job_id, exc,
            )

    return Response(
        content=upstream.content,
        status_code=200,
        headers={"content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Suggested copy-name — plan §6.4 / D17
# ---------------------------------------------------------------------------

# Column-limit budget. Column is VARCHAR(60); the " · 副本 N" suffix consumes
# roughly 7-10 chars depending on N. We reserve a fixed suffix budget then
# fit the source-name portion inside the remainder.
_COPY_NAME_MAX_LEN = 60


def _fallback_copy_source_name(job) -> str:
    """Best-effort derivation of a title for a job that has no ``display_name``.

    Falls back through job source_ref → job_id so the suggestion never
    reads like ``None · 副本 N``."""
    name = getattr(job, "display_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    source_ref = getattr(job, "source_ref", None)
    if isinstance(source_ref, str) and source_ref.strip():
        return source_ref.strip()[:30]
    return str(getattr(job, "job_id", "任务") or "任务")


async def intercept_suggested_copy_name(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /gateway/jobs/{job_id}/suggested-copy-name — pre-fill for the
    "save as new copy" modal on the edit page (plan §6.4 / D17).

    Returns ``{"suggested_name": "<源名> · 副本 N"}`` where N = one more
    than the number of existing copies of this source. The user is free
    to edit the suggestion before submitting ``editing/commit``; collision
    resolution happens there."""
    del request  # unused — standard fastapi signature
    await _verify_job_ownership(job_id, db, user)

    source_result = await db.execute(
        select(Job).where(Job.job_id == job_id)
    )
    source_job = source_result.scalar_one_or_none()
    if source_job is None:
        return _error_response(404, "job_not_found", f"任务不存在: {job_id}")

    # Count existing copies. Fresh jobs without any copy yet return 0 →
    # suggestion becomes "... · 副本 1".
    count_result = await db.execute(
        select(func.count()).select_from(Job).where(
            Job.copy_of_job_id == job_id
        )
    )
    n = int(count_result.scalar() or 0) + 1

    source_name = _fallback_copy_source_name(source_job)
    suffix = f" · 副本 {n}"

    # If the total exceeds the column budget, sacrifice the source-name
    # tail so the suffix stays intact (plan §6.4 explicit rule).
    if len(source_name) + len(suffix) > _COPY_NAME_MAX_LEN:
        source_name = source_name[: _COPY_NAME_MAX_LEN - len(suffix)]

    suggested = f"{source_name}{suffix}"
    return Response(
        content=json.dumps({"suggested_name": suggested}, ensure_ascii=False),
        status_code=200,
        headers={"content-type": "application/json"},
    )


async def update_source_metadata(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /job-api/jobs/{job_id}/source-metadata — internal callback from Pipeline.

    Allows the pipeline to report source duration/title after S0 and a
    content-aware Chinese display_name after S2 review.
    """
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    actual_duration = data.get("source_duration_seconds")
    title = data.get("title")
    raw_display_name = (
        data.get("display_name")
        if "display_name" in data
        else data.get("display_title_zh")
    )
    s2_display_name = _sanitize_s2_display_name(raw_display_name)

    if actual_duration is None and title is None and s2_display_name is None:
        return Response(
            content=json.dumps({"error": "no_update_fields", "message": "至少提供 source_duration_seconds、title 或有效中文 display_name"}),
            status_code=400,
            headers={"content-type": "application/json"},
        )

    result = await db.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        return Response(
            content=json.dumps({"ok": True, "note": "job not in gateway DB, skipped"}),
            status_code=200,
            headers={"content-type": "application/json"},
        )

    if actual_duration is not None:
        try:
            dur_float = float(actual_duration)
            job.source_duration_seconds = dur_float
            # V3-0: write actual source duration to actual_minutes.
            # estimated_minutes is preserved as the original pre-download estimate
            # so we can later compare estimate vs. actual for calibration.
            job.actual_minutes = dur_float / 60.0

            # If create-time had no estimated_duration, reserve credits as soon
            # as the pipeline reports the real source duration.
            snap = getattr(job, "metering_snapshot", None) or {}
            if dur_float > 0:
                try:
                    from models import CreditsLedger

                    existing_reserve = await db.execute(
                        select(CreditsLedger).where(
                            CreditsLedger.related_job_id == job_id,
                            CreditsLedger.direction == "reserve",
                            CreditsLedger.reason_code == "job_reserve",
                        ).limit(1)
                    )
                    already_reserved = existing_reserve.scalar_one_or_none() is not None

                    if not already_reserved:
                        _quality_tier = snap.get("quality_tier", "standard")
                        _svc_mode = snap.get("service_mode") or job.service_mode or "express"
                        late_credits = estimate_credits(
                            dur_float / 60.0, service_mode=_svc_mode, quality_tier=_quality_tier,
                        )
                        if late_credits > 0:
                            snap["credits_estimated"] = late_credits
                            job.metering_snapshot = dict(snap)
                            await ensure_credit_buckets_for_user(
                                db,
                                user_id=job.user_id,
                                role=getattr(job, "role_snapshot", None),
                            )
                            await reserve_credits_or_raise(
                                db, user_id=job.user_id, job_id=job_id,
                                estimated_credits=late_credits,
                                service_mode=_svc_mode,
                            )
                            logger.info("V3 late credit reserve for %s: %d credits", job_id, late_credits)
                except InsufficientCreditsError as exc:
                    job.status = "failed"
                    job.current_stage = "failed"
                    job.error_summary = {
                        "error_code": "insufficient_credits",
                        "message": (
                            f"点数不足：本次预计需要 {exc.required} 点，"
                            f"当前可用 {exc.available} 点。"
                        ),
                    }
                    try:
                        await db.commit()
                    except Exception:
                        await db.rollback()
                    await _settle_smart_clone_reservation_from_job_state(
                        db,
                        job,
                        reason="late_base_credit_insufficient",
                    )
                    await _compensate_upstream_job(job_id)
                    return _insufficient_credits_response(exc)
                except Exception as _e:
                    logger.warning("V3 late credit reserve failed for %s: %s", job_id, _e)
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    await _settle_smart_clone_reservation_from_job_state(
                        db,
                        job,
                        reason="late_base_credit_reserve_failed",
                    )
                    await _compensate_upstream_job(job_id)
                    return _error_response(
                        500,
                        "credit_reserve_failed",
                        "点数预扣失败，任务已停止。请稍后重试。",
                        {"job_id": job_id},
                    )
        except (TypeError, ValueError):
            pass
    if title is not None:
        job.title = str(title)[:512]
    display_name_updated = False
    if s2_display_name is not None and _should_replace_display_name_from_s2(job):
        job.display_name = s2_display_name
        display_name_updated = True

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    # 2026-05-06: when S2 auto-renames placeholder → Chinese title, the
    # PG row above gets updated. Mirror the change into the Job-API
    # JSON store so downstream artifacts (剪映 zip filename via
    # _resolve_zip_basename, materials_pack download_filename, future
    # consumers) see the same name. Without this mirror, a job
    # triggered AFTER S2 finishes would still have the placeholder
    # display_name in its JSON record and produce a placeholder-named
    # zip even after my 2026-05-06 jianying fingerprint fix.
    #
    # Best-effort: failure to mirror is logged but does NOT roll back
    # the PG update. The JSON store can be reconciled later (worst
    # case: one stale-named zip until next user-initiated rename).
    if display_name_updated:
        try:
            import httpx
            upstream_url = f"{settings.job_api_upstream.rstrip('/')}/jobs/{job_id}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(
                    upstream_url,
                    json={"display_name": s2_display_name},
                    headers={"content-type": "application/json"},
                )
            if resp.status_code != 200:
                logger.warning(
                    "S2 display_name mirror failed for %s: HTTP %s — JSON "
                    "store will keep placeholder until next manual rename",
                    job_id, resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001 — mirror is best-effort
            logger.warning(
                "S2 display_name mirror failed for %s: %s — JSON store "
                "will keep placeholder until next manual rename",
                job_id, exc,
            )

    logger.info(
        "source-metadata updated for %s: duration=%s title=%s display_name_updated=%s",
        job_id, actual_duration, title, display_name_updated,
    )
    return Response(
        content=json.dumps({"ok": True, "display_name_updated": display_name_updated}),
        status_code=200,
        headers={"content-type": "application/json"},
    )


async def update_job_metering(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /job-api/jobs/{job_id}/metering — internal callback from Pipeline.

    Allows the pipeline to report metering fields after TTS/alignment completion:
    - final_cn_chars: total Chinese characters in final translation
    - rewrite_triggered: whether any segment was rewritten
    - rewrite_count: total rewrite operations performed
    - tts_billed_chars: total characters sent to TTS provider

    These fields are merged into Job.metering_snapshot (JSONB).
    Best-effort: failures do not affect job status.
    """
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    if not data:
        return Response(
            content=json.dumps({"error": "empty_body", "message": "请提供 metering 字段"}),
            status_code=400,
            headers={"content-type": "application/json"},
        )

    result = await db.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        return Response(
            content=json.dumps({"ok": True, "note": "job not in gateway DB, skipped"}),
            status_code=200,
            headers={"content-type": "application/json"},
        )

    # Merge incoming fields into existing metering_snapshot
    snapshot = dict(job.metering_snapshot or {})
    allowed_keys = {
        # V3-4 baseline
        "final_cn_chars", "rewrite_triggered", "rewrite_count",
        # V3-5 partial
        "tts_billed_chars",
        # Phase 2 Task 0 — translation-duration-alignment metrics
        "total_segments",
        "catalog_hit_count", "catalog_hit_rate", "skip_probe",
        "needs_review_count", "needs_review_rate",
        "alignment_method_distribution", "speed_param_distribution",
        "first_pass_error_pct_avg", "first_pass_error_pct_p50",
        "first_pass_error_pct_p90", "first_pass_error_pct_n",
        "glossary_total_terms", "glossary_preserved_terms",
        "term_preservation_rate", "missing_glossary_terms",
        # P0 quality benchmark audit fields
        "pre_tts_rewrite_count", "pre_tts_contradiction_count",
        "pre_tts_contradiction_rate", "pre_tts_rewrite_events",
        "pre_tts_rewrite_rejected_count",
        "pre_tts_rewrite_rejected_reason_distribution",
        "pre_tts_rewrite_rejected_events",
        "pre_tts_rewrite_retry_attempt_count",
        "pre_tts_rewrite_retry_accepted_count",
        "harmful_pre_tts_contradiction_count",
        "harmful_pre_tts_contradiction_rate",
        "micro_segment_count", "short_segment_count",
        "short_segment_needs_review_count", "short_segment_force_dsp_count",
        "capped_dsp_overflow_count", "short_segment_capped_dsp_overflow_count",
        "force_dsp_severity_distribution", "force_dsp_review_suppressed_count",
        "short_merge_candidate_count", "short_merge_blocked_cross_speaker_count",
        "short_merge_applied_count", "short_merge_absorbed_count",
        "speaker_count", "speaker_role_distribution", "speaker_primary_count",
        "speaker_incidental_count", "speaker_fragmented_count",
        "speaker_incidental_duration_share", "speaker_structure_profiles",
        "voice_speed_profile_candidate_count", "voice_speed_profile_sent_count",
        "voice_speed_profile_updated_count", "voice_speed_profile_skipped_count",
        "voice_speed_profile_skipped_reason_distribution",
        # Job-level LLM/TTS/voice-clone cost metering sidecar
        "usage_metering_version", "usage_events_count",
        "transcription_method", "asr_provider", "asr_provider_cost_status",
        "legacy_gemini_transcription_call_count",
        "first_tts_billed_chars", "first_tts_call_count",
        "probe_tts_billed_chars", "probe_tts_call_count",
        "post_tts_resynth_billed_chars", "post_tts_resynth_call_count",
        "post_edit_resynth_billed_chars", "post_edit_resynth_call_count",
        "post_edit_resynth_tts_billed_chars", "post_edit_resynth_tts_call_count",
        "interactive_preview_billed_chars", "interactive_preview_call_count",
        "interactive_preview_tts_billed_chars", "interactive_preview_tts_call_count",
        "tts_billed_chars_by_bucket", "tts_call_count_by_bucket",
        "tts_billed_chars_by_provider", "tts_call_count_by_provider",
        "tts_billed_chars_by_provider_model", "tts_call_count_by_provider_model",
        "tts_call_count",
        "voice_clone_call_count", "voice_clone_success_call_count",
        "voice_clone_billable_count", "voice_clone_count_by_provider",
        "voice_clone_source_audio_seconds",
        "llm_call_count", "llm_input_tokens", "llm_output_tokens",
        "llm_total_tokens", "llm_audio_input_bytes", "llm_audio_input_seconds",
        "llm_task_call_distribution", "llm_model_call_distribution",
        "s1_gemini_transcribe_llm_calls", "s1_gemini_transcribe_llm_input_tokens",
        "s1_gemini_transcribe_llm_output_tokens", "s1_gemini_transcribe_llm_tokens",
        "s2_pass1_llm_calls", "s2_pass1_llm_input_tokens",
        "s2_pass1_llm_output_tokens", "s2_pass1_llm_tokens",
        "s2_pass2_llm_calls", "s2_pass2_llm_input_tokens",
        "s2_pass2_llm_output_tokens", "s2_pass2_llm_tokens",
        "s2_pass3_llm_calls", "s2_pass3_llm_input_tokens",
        "s2_pass3_llm_output_tokens", "s2_pass3_llm_tokens",
        "s2_speaker_verifier_llm_calls", "s2_speaker_verifier_llm_input_tokens",
        "s2_speaker_verifier_llm_output_tokens", "s2_speaker_verifier_llm_tokens",
        "s2_review_llm_calls", "s2_review_llm_input_tokens",
        "s2_review_llm_output_tokens", "s2_review_llm_tokens",
        "s2_infer_llm_calls", "s2_infer_llm_input_tokens",
        "s2_infer_llm_output_tokens", "s2_infer_llm_tokens",
        "s3_translate_llm_calls", "s3_translate_llm_input_tokens",
        "s3_translate_llm_output_tokens", "s3_translate_llm_tokens",
        "s5_rewrite_llm_calls", "s5_rewrite_llm_input_tokens",
        "s5_rewrite_llm_output_tokens", "s5_rewrite_llm_tokens",
        "s5_rewrite_strict_llm_calls", "s5_rewrite_strict_llm_input_tokens",
        "s5_rewrite_strict_llm_output_tokens", "s5_rewrite_strict_llm_tokens",
        "s5_short_content_compact_llm_calls",
        "s5_short_content_compact_llm_input_tokens",
        "s5_short_content_compact_llm_output_tokens",
        "s5_short_content_compact_llm_tokens",
        "probe_translate_llm_calls", "probe_translate_llm_input_tokens",
        "probe_translate_llm_output_tokens", "probe_translate_llm_tokens",
        "pre_tts_rewrite_llm_calls", "pre_tts_rewrite_llm_input_tokens",
        "pre_tts_rewrite_llm_output_tokens", "pre_tts_rewrite_llm_tokens",
        "post_tts_rewrite_llm_calls", "post_tts_rewrite_llm_input_tokens",
        "post_tts_rewrite_llm_output_tokens", "post_tts_rewrite_llm_tokens",
    }
    updated_keys = []
    for key in allowed_keys:
        if key in data:
            snapshot[key] = data[key]
            updated_keys.append(key)

    # F1 (Smart MVP P2 skeleton, plan 2026-05-13 §4.2 末段): mirror
    # smart_state from pipeline into the dedicated Job.smart_state column
    # (separate JSONB column, NOT inside metering_snapshot — the two have
    # different concerns: metering = counters, smart_state = state machine).
    # Last-write merge so partial pipeline updates don't drop earlier keys
    # (e.g. reserved_credits_per_minute set at create time + status set
    # later at handoff). Empty/None payloads are skipped to avoid clobber.
    smart_state_update = data.get("smart_state")
    smart_state_changed = False
    if isinstance(smart_state_update, dict) and smart_state_update:
        merged_smart_state = dict(job.smart_state or {})
        merged_smart_state.update(smart_state_update)
        job.smart_state = merged_smart_state
        smart_state_changed = True

    if not updated_keys and not smart_state_changed:
        return Response(
            content=json.dumps({"ok": True, "note": "no recognized metering keys"}),
            status_code=200,
            headers={"content-type": "application/json"},
        )

    if updated_keys:
        job.metering_snapshot = snapshot

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    logger.info(
        "metering updated for %s: keys=%s smart_state=%s",
        job_id, updated_keys, smart_state_changed,
    )
    return Response(
        content=json.dumps({"ok": True}),
        status_code=200,
        headers={"content-type": "application/json"},
    )
