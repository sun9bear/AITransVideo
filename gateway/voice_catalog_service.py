"""Business logic for voice catalog — Phase 2: verify, CRUD, import.

Verification uses Provider Adapters: each TTS provider implements a minimal
``verify(voice_id, provider_config)`` that synthesises a test sentence and
checks the result.  Only VolcEngine is implemented in Phase 2.

The VolcEngine verify implementation mirrors the production V3 provider
(``volcengine_tts_provider.py``) exactly — same endpoint, headers, and
payload structure — so that a verify-pass guarantees runtime compatibility.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from internal_auth import internal_headers as _internal_headers

from voice_catalog_models import VoiceCatalog, VoiceLabel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — aligned with volcengine_tts_provider.py
# ---------------------------------------------------------------------------

VERIFY_TEST_TEXT_ZH = "这是一段用于验证音色可用性的测试文本。"
VERIFY_TEST_TEXT_EN = "This is a test sentence to verify voice availability."
VERIFY_MIN_PCM_BYTES = 1000  # ~20ms of 24kHz 16-bit mono — any real speech exceeds this
VERIFY_TIMEOUT_SECONDS = 15.0

# VolcEngine V3 API — identical to volcengine_tts_provider.py
_VOLC_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_VOLC_CODE_AUDIO = 0
_VOLC_CODE_FINISH = 20000000
_VOLC_PCM_SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# VolcEngine verify adapter
# ---------------------------------------------------------------------------

async def verify_volcengine(
    voice_id: str,
    provider_config: dict,
    language: str = "zh",
) -> dict[str, Any]:
    """Verify a VolcEngine voice by synthesising the test text.

    Returns a single-dimension result::

        {"default": {"verified": bool, "at": str, "error": str | None}}

    When ``resource_id`` is unknown, tries 2.0 first then 1.0.  On success
    the detected resource_id is returned via a ``_detected_resource_id`` key
    so the caller can persist it back to ``provider_config``.
    """
    test_text = VERIFY_TEST_TEXT_EN if language == "en" else VERIFY_TEST_TEXT_ZH

    resource_id = provider_config.get("resource_id")
    if not resource_id:
        # Auto-detect: try 2.0 first, then 1.0
        for rid in ("seed-tts-2.0", "seed-tts-1.0"):
            result = await _volc_synthesize_check(voice_id, rid, test_text=test_text)
            if result.pop("_skip_db_update", False):
                return {"default": result, "_skip_db_update": True}
            if result["verified"]:
                return {"default": result, "_detected_resource_id": rid}
        return {"default": result}  # type: ignore[possibly-undefined]

    result = await _volc_synthesize_check(voice_id, resource_id, test_text=test_text)
    # Propagate _skip_db_update from inner result to top level
    skip = result.pop("_skip_db_update", False)
    out: dict[str, Any] = {"default": result}
    if skip:
        out["_skip_db_update"] = True
    return out


async def _volc_synthesize_check(
    voice_id: str,
    resource_id: str,
    *,
    test_text: str = "",
) -> dict[str, Any]:
    """Call VolcEngine TTS V3 API and return verification result.

    Protocol is identical to ``volcengine_tts_provider.synthesize()``:
    - Endpoint: POST …/api/v3/tts/unidirectional
    - Headers:  X-Api-App-Id, X-Api-Access-Key, X-Api-Resource-Id
    - Payload:  user.uid, req_params.text, req_params.speaker,
                req_params.audio_params.format="pcm",
                req_params.audio_params.sample_rate=24000
    """
    if not test_text:
        test_text = VERIFY_TEST_TEXT_ZH
    app_id = (
        os.environ.get("VOLCENGINE_TTS_APP_ID", "").strip()
        or os.environ.get("VOLCENGINE_TTS_APPID", "").strip()
    )
    access_key = (
        os.environ.get("VOLCENGINE_TTS_ACCESS_KEY", "").strip()
        or os.environ.get("VOLCENGINE_TTS_ACCESS_TOKEN", "").strip()
    )
    now = datetime.now(timezone.utc).isoformat()

    if not app_id or not access_key:
        return {
            "verified": False,
            "at": now,
            "error": "缺少 VolcEngine 凭据 (VOLCENGINE_TTS_APP_ID / VOLCENGINE_TTS_ACCESS_KEY)",
            "_skip_db_update": True,
        }

    # --- Headers: exactly match _build_headers() in volcengine_tts_provider ---
    headers = {
        "X-Api-App-Id": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": uuid.uuid4().hex,
        "Content-Type": "application/json",
    }

    # --- Payload: exactly match _build_payload() in volcengine_tts_provider ---
    payload = {
        "user": {"uid": "aivideotrans"},
        "req_params": {
            "speaker": voice_id,
            "text": test_text,
            "audio_params": {
                "format": "pcm",
                "sample_rate": _VOLC_PCM_SAMPLE_RATE,
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _VOLC_ENDPOINT,
                headers=headers,
                json=payload,
            )

        # --- Non-2xx: auth failure, 404, server error ---
        if resp.status_code != 200:
            return {
                "verified": False,
                "at": now,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }

        # --- Parse streaming JSON lines (same as _iter_chunk_events) ---
        pcm_total = 0
        parsed_any_event = False
        for line in resp.text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            parsed_any_event = True
            code = event.get("code", -1)

            if code == _VOLC_CODE_AUDIO:
                data_b64 = event.get("data", "")
                if data_b64:
                    pcm_total += len(base64.b64decode(data_b64))
            elif code == _VOLC_CODE_FINISH:
                break
            elif code > 0:
                msg = event.get("message", "")
                if code == 55000000:
                    error_detail = f"resource mismatch ({resource_id}): {msg}"
                elif code == 45000000:
                    error_detail = f"音色不存在 ({voice_id}): {msg}"
                else:
                    error_detail = f"code={code}, message={msg}"
                return {"verified": False, "at": now, "error": error_detail}

        # --- Validate we got a real response ---
        if not parsed_any_event:
            return {
                "verified": False,
                "at": now,
                "error": "非预期响应格式（无可解析事件）",
            }

        if pcm_total >= VERIFY_MIN_PCM_BYTES:
            return {"verified": True, "at": now, "error": None}

        return {
            "verified": False,
            "at": now,
            "error": f"音频太短 ({pcm_total} bytes PCM)",
        }

    except httpx.TimeoutException:
        return {"verified": False, "at": now, "error": "timeout"}
    except Exception as exc:
        return {"verified": False, "at": now, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

async def create_voice(db: AsyncSession, data: dict[str, Any]) -> VoiceCatalog:
    """Insert a new voice into voice_catalog."""
    voice = VoiceCatalog(
        voice_id=data["voice_id"],
        provider=data["provider"],
        provider_config=data.get("provider_config", {}),
        display_name=data["display_name"],
        gender=data.get("gender"),
        language=data.get("language", "zh"),
        scene=data.get("scene"),
        matchable=data.get("matchable", True),
        verify_status=data.get("verify_status", {}),
        source=data.get("source", "manual"),
        notes=data.get("notes"),
    )
    db.add(voice)
    await db.flush()
    return voice


async def update_voice(
    db: AsyncSession,
    voice: VoiceCatalog,
    data: dict[str, Any],
) -> VoiceCatalog:
    """Partial update of voice metadata.  Only touches provided fields."""
    allowed = {
        "display_name", "gender", "language", "scene", "matchable",
        "provider_config", "notes",
    }
    for key in allowed:
        if key in data:
            setattr(voice, key, data[key])
    voice.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return voice


async def soft_delete_voice(db: AsyncSession, voice: VoiceCatalog) -> VoiceCatalog:
    """Soft-delete by setting archived_at."""
    voice.archived_at = datetime.now(timezone.utc)
    voice.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return voice


class VerifySkipped(Exception):
    """Raised when verify cannot run (unsupported provider, missing creds).

    Crucially, this means the DB verify_status must NOT be overwritten —
    doing so would destroy seed trust for voices that were never actually
    tested against the TTS API.
    """


# App internal API upstream: read from settings.job_api_upstream at call time
# (env var AVT_JOB_API_UPSTREAM). X-Internal-Key header comes from the shared
# gateway/internal_auth.py helper imported at the top of this module.


async def verify_cosyvoice(voice_id: str, language: str = "zh") -> dict[str, Any]:
    """Verify a CosyVoice voice by calling app internal TTS synthesis endpoint.

    CosyVoice requires DashScope WebSocket SDK which only exists in the app
    container, so we proxy the synthesis check through an internal endpoint.
    """
    test_text = (
        "This is a test sentence to verify voice availability."
        if language == "en"
        else "这是一段用于验证音色可用性的测试文本。"
    )
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_internal_headers()) as client:
            resp = await client.post(
                f"{settings.job_api_upstream}/internal/voice-verify/cosyvoice",
                json={"voice_id": voice_id, "test_text": test_text},
            )
        data = resp.json()

        if data.get("ok"):
            return {"default": {"verified": True, "at": now, "error": None}}
        else:
            return {"default": {"verified": False, "at": now, "error": data.get("error", "未知错误")}}

    except httpx.TimeoutException:
        return {"default": {"verified": False, "at": now, "error": "timeout"}}
    except Exception as exc:
        return {"default": {"verified": False, "at": now, "error": str(exc)[:200]}}


async def run_verify(
    db: AsyncSession,
    voice: VoiceCatalog,
) -> dict[str, Any]:
    """Run verification for a single voice, update DB only on real result.

    Raises VerifySkipped if the provider is unsupported or credentials are
    missing — the caller should catch this and return the error message
    without committing a DB change.
    """
    provider = voice.provider
    language = voice.language or "zh"

    if provider == "volcengine":
        result = await verify_volcengine(
            voice.voice_id, voice.provider_config or {},
            language=language,
        )
    elif provider == "cosyvoice":
        result = await verify_cosyvoice(voice.voice_id, language=language)
    else:
        raise VerifySkipped(f"verify 暂不支持 provider={provider}")

    # Check if the adapter flagged "don't persist" (e.g. missing credentials)
    if result.get("_skip_db_update"):
        result.pop("_skip_db_update", None)
        error_msg = result.get("default", {}).get("error", "未知错误")
        raise VerifySkipped(error_msg)

    # Handle auto-detected resource_id
    detected_rid = result.pop("_detected_resource_id", None)
    if detected_rid:
        config = dict(voice.provider_config or {})
        config["resource_id"] = detected_rid
        voice.provider_config = config

    voice.verify_status = result
    voice.verify_attempts = (voice.verify_attempts or 0) + 1
    voice.updated_at = datetime.now(timezone.utc)
    await db.flush()

    return result


# ---------------------------------------------------------------------------
# Batch import — parse CSV/text lines
# ---------------------------------------------------------------------------

def parse_import_lines(text: str, provider: str) -> list[dict[str, Any]]:
    """Parse CSV/tab-separated import text into voice dicts.

    Expected columns (header optional):
        voice_id, display_name, gender, scene, resource_id (volcengine only)

    Returns list of dicts ready for create_voice().
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return []

    # Detect separator
    sep = "\t" if "\t" in lines[0] else ","

    # Check if first line is header
    first_lower = lines[0].lower()
    start = 1 if ("voice_id" in first_lower or "display_name" in first_lower) else 0

    results: list[dict[str, Any]] = []
    for line in lines[start:]:
        parts = [p.strip() for p in line.split(sep)]
        if len(parts) < 2:
            continue

        voice_id = parts[0]
        display_name = parts[1]
        gender = parts[2] if len(parts) > 2 else None
        scene = parts[3] if len(parts) > 3 else None
        resource_id = parts[4] if len(parts) > 4 else None

        entry: dict[str, Any] = {
            "voice_id": voice_id,
            "provider": provider,
            "display_name": display_name,
            "gender": gender,
            "scene": scene,
            "source": "csv_import",
        }
        if resource_id and provider == "volcengine":
            entry["provider_config"] = {"resource_id": resource_id}

        results.append(entry)

    return results


async def import_voices(
    db: AsyncSession,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Batch import: insert new voices, skip duplicates.

    Returns summary: {created: [...], skipped: [...], errors: [...]}.
    """
    created: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []

    for entry in entries:
        vid = entry.get("voice_id", "")
        if not vid:
            errors.append({"voice_id": "", "error": "voice_id 为空"})
            continue

        # Check for existing
        existing = await db.execute(
            select(VoiceCatalog).where(VoiceCatalog.voice_id == vid)
        )
        if existing.scalar_one_or_none() is not None:
            skipped.append(vid)
            continue

        try:
            await create_voice(db, entry)
            created.append(vid)
        except Exception as exc:
            errors.append({"voice_id": vid, "error": str(exc)[:200]})

    return {"created": created, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Phase 4: Label management
# ---------------------------------------------------------------------------

# Valid label types
_VALID_LABEL_TYPES = {"text", "audio_round1", "audio_round2", "audio_round3", "final"}

# Fields by label type
_TEXT_FIELDS = {"age_group", "persona_style", "energy_level"}
_AUDIO_FIELDS = {
    "pitch_level", "warmth", "authority", "intimacy", "brightness",
    "maturity", "delivery_style", "texture_tags", "childlike",
    "energy_level",
}
_FINAL_FIELDS = _TEXT_FIELDS | _AUDIO_FIELDS


async def write_label(
    db: AsyncSession,
    voice_id: str,
    label_type: str,
    data: dict[str, Any],
    labeled_by: str,
    source_run_id: str | None = None,
) -> VoiceLabel:
    """Write a new label, superseding any existing is_current label of same type.

    This implements the audit trail: old labels are never deleted, just
    marked is_current=False + superseded_at=now().
    """
    if label_type not in _VALID_LABEL_TYPES:
        raise ValueError(f"Invalid label_type: {label_type}")

    # Verify voice exists
    voice_result = await db.execute(
        select(VoiceCatalog).where(VoiceCatalog.voice_id == voice_id)
    )
    if voice_result.scalar_one_or_none() is None:
        raise ValueError(f"音色 {voice_id} 不存在")

    now = datetime.now(timezone.utc)

    # Supersede existing current label of same type
    existing = await db.execute(
        select(VoiceLabel)
        .where(VoiceLabel.voice_id == voice_id)
        .where(VoiceLabel.label_type == label_type)
        .where(VoiceLabel.is_current == True)  # noqa: E712
    )
    for old_label in existing.scalars().all():
        old_label.is_current = False
        old_label.superseded_at = now

    # Create new label
    label = VoiceLabel(
        voice_id=voice_id,
        label_type=label_type,
        source_run_id=source_run_id,
        is_current=True,
        age_group=data.get("age_group"),
        persona_style=data.get("persona_style"),
        energy_level=data.get("energy_level"),
        pitch_level=data.get("pitch_level"),
        warmth=data.get("warmth"),
        authority=data.get("authority"),
        intimacy=data.get("intimacy"),
        brightness=data.get("brightness"),
        maturity=data.get("maturity"),
        delivery_style=data.get("delivery_style"),
        texture_tags=data.get("texture_tags"),
        childlike=data.get("childlike"),
        labeled_by=labeled_by,
        labeled_at=now,
    )
    db.add(label)
    await db.flush()
    return label


async def write_labels_batch(
    db: AsyncSession,
    labels: list[dict[str, Any]],
    label_type: str,
    labeled_by: str,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    """Write multiple labels in one transaction.

    Each entry: {"voice_id": "...", "age_group": "...", ...}
    Returns: {written: [...], errors: [...]}
    """
    written: list[str] = []
    errors: list[dict[str, str]] = []

    for entry in labels:
        vid = entry.get("voice_id", "")
        if not vid:
            errors.append({"voice_id": "", "error": "voice_id 为空"})
            continue
        try:
            await write_label(db, vid, label_type, entry, labeled_by, source_run_id)
            written.append(vid)
        except Exception as exc:
            errors.append({"voice_id": vid, "error": str(exc)[:200]})

    return {"written": written, "errors": errors}


async def generate_final_label(
    db: AsyncSession,
    voice_id: str,
    source_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Generate a 'final' label by merging available labels.

    Priority: 3 audio rounds → majority vote; 1-2 rounds → last round;
    text only → text label; nothing → None.
    """
    # Get all current labels for this voice
    result = await db.execute(
        select(VoiceLabel)
        .where(VoiceLabel.voice_id == voice_id)
        .where(VoiceLabel.is_current == True)  # noqa: E712
        .order_by(VoiceLabel.label_type)
    )
    labels = {lbl.label_type: lbl for lbl in result.scalars().all()}

    audio_rounds = []
    for rt in ("audio_round1", "audio_round2", "audio_round3"):
        if rt in labels:
            audio_rounds.append(labels[rt])

    if len(audio_rounds) == 3:
        # Majority vote across 3 rounds
        merged = _majority_vote_merge(audio_rounds)
    elif audio_rounds:
        # Use the last available round
        merged = _label_to_dict(audio_rounds[-1])
    elif "text" in labels:
        # Text-only fallback
        merged = _label_to_dict(labels["text"])
    else:
        return None

    # Write final label
    await write_label(db, voice_id, "final", merged, "system-finalize", source_run_id)
    return merged


def _majority_vote_merge(labels: list[VoiceLabel]) -> dict[str, Any]:
    """Merge 3 audio round labels via majority voting."""
    from collections import Counter

    result: dict[str, Any] = {}

    # String fields: pick most frequent
    for field in ("pitch_level", "warmth", "authority", "intimacy", "brightness",
                  "energy_level", "maturity", "delivery_style", "age_group",
                  "persona_style"):
        values = [getattr(lbl, field) for lbl in labels if getattr(lbl, field)]
        if values:
            result[field] = Counter(values).most_common(1)[0][0]

    # Boolean: majority vote
    childlike_vals = [getattr(lbl, "childlike") for lbl in labels if getattr(lbl, "childlike") is not None]
    if childlike_vals:
        result["childlike"] = sum(1 for v in childlike_vals if v) > len(childlike_vals) / 2

    # texture_tags: union of tags in ≥2/3 rounds, sorted by frequency, max 3
    all_tags: list[str] = []
    for lbl in labels:
        tags = getattr(lbl, "texture_tags") or []
        if isinstance(tags, list):
            all_tags.extend(tags)
    if all_tags:
        tag_counts = Counter(all_tags)
        threshold = len(labels) / 2  # ≥2 out of 3
        result["texture_tags"] = [
            tag for tag, count in tag_counts.most_common()
            if count >= threshold
        ][:3]

    return result


def _label_to_dict(label: VoiceLabel) -> dict[str, Any]:
    """Convert a VoiceLabel to a plain dict of label fields."""
    return {
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
    }


async def get_label_status(db: AsyncSession) -> dict[str, Any]:
    """Get labeling progress overview across active (non-archived) voices.

    Both ``total_voices`` and each ``label_counts`` entry use the same
    population: ``voice_catalog.archived_at IS NULL``.  This ensures the
    ``coverage`` fractions are always consistent.
    """
    # Count active voices
    total_result = await db.execute(
        select(func.count()).select_from(VoiceCatalog).where(VoiceCatalog.archived_at.is_(None))
    )
    total = total_result.scalar() or 0

    # Count active voices with each label type (join to exclude archived)
    label_counts: dict[str, int] = {}
    for lt in _VALID_LABEL_TYPES:
        count_result = await db.execute(
            select(func.count(func.distinct(VoiceLabel.voice_id)))
            .join(VoiceCatalog, VoiceLabel.voice_id == VoiceCatalog.voice_id)
            .where(VoiceCatalog.archived_at.is_(None))
            .where(VoiceLabel.label_type == lt)
            .where(VoiceLabel.is_current == True)  # noqa: E712
        )
        label_counts[lt] = count_result.scalar() or 0

    return {
        "total_voices": total,
        "label_counts": label_counts,
        "coverage": {
            lt: f"{count}/{total}" for lt, count in label_counts.items()
        },
    }
