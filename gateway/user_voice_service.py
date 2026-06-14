"""Personal voice library service — per-user CRUD for cloned voices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
import re
import unicodedata
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserVoice

logger = logging.getLogger(__name__)


# Phase 1 (plan 2026-05-17-user-voice-candidate-first):
# ``match_scope`` is the new fine-grained taxonomy that lets Studio/
# Post-edit/Smart distinguish "auto-reusable" from "needs user
# confirmation". Defaulted so existing callers constructing
# ``UserVoiceMatch`` directly (older test fixtures) keep working —
# the default is derived from ``confidence`` in ``__post_init__``.
_CONFIDENCE_TO_DEFAULT_SCOPE: dict[str, str] = {
    "strong": "same_source_strong",
    # 2026-05-21 spec: cross-source named unique-in-library matches get
    # auto-reuse via the new "strong_named" tier — score 60 (below
    # same-source-strong 100 / same-source-named-medium 70) but high
    # enough to bypass the user pause-confirm step. Promotion happens
    # at match_user_voices() post-processing, not _score_cross_source_match.
    "strong_named": "cross_source_named_unique",
    "medium": "same_source_named",
    "weak": "same_source_speaker_id_changed",
}


@dataclass(frozen=True)
class UserVoiceMatch:
    voice: UserVoice
    confidence: str
    reason: str
    score: int
    match_scope: str | None = None

    def __post_init__(self) -> None:
        if not self.match_scope:
            # ``frozen=True`` blocks attribute assignment; reach in via
            # object.__setattr__ for the lazy default.
            object.__setattr__(
                self,
                "match_scope",
                _CONFIDENCE_TO_DEFAULT_SCOPE.get(self.confidence, "same_source_named"),
            )

    @property
    def auto_reuse_allowed(self) -> bool:
        # 2026-05-21: ``strong_named`` joins ``strong`` as auto-reuse
        # tier. ``strong_named`` only fires when the user library has
        # EXACTLY ONE voice with the matching non-generic name_key —
        # deterministic uniqueness, not name-length heuristic. 2+
        # same-name candidates stay weak so smart pauses for user
        # to pick which one.
        return self.confidence in {"strong", "strong_named"}


class VoiceNotFoundError(LookupError):
    """Raised by ``update_user_voice_speed_calibration`` /
    ``update_catalog_voice_speed_calibration`` when the (user_id, voice_id)
    or (provider, voice_id) row is gone (deleted between the calling code
    fetching it and the writer's SELECT FOR UPDATE)."""


def _merged_by_model(
    existing: dict | None, *, model_key: str, cps: float
) -> dict[str, float]:
    """Read-modify-write merge of ``chars_per_second_by_model``.

    Done inside a SELECT FOR UPDATE row lock by the helpers below so two
    concurrent calibrations (e.g. T1's parallel turbo + hd) cannot lose
    each other's keys (plan v4.1 codex F-v4.1-1).
    """
    merged = dict(existing or {})
    merged[model_key] = float(cps)
    return merged


def normalize_speaker_name_key(speaker_name: str | None) -> str | None:
    """Build the conservative comparison key used for future voice reuse."""
    if not speaker_name:
        return None
    normalized = unicodedata.normalize("NFKC", speaker_name)
    normalized = " ".join(normalized.split()).strip(" \t\r\n·-_\u00b7")
    normalized = normalized.lower()
    return normalized or None


# Cross-source weak matching blacklist. Inputs are expected to have
# already passed through ``normalize_speaker_name_key`` so they're
# lowercase, NFKC-normalised, single-space-collapsed, edge-punct
# stripped. Covers the common Chinese + English placeholder names
# we see in ASR/speaker-diarisation output today; future languages
# (jp/kr/fr/es) can be added if false-positives become an issue.
_GENERIC_SPEAKER_NAME_KEYS: frozenset[str] = frozenset({
    "speaker_a", "speaker_b", "speaker_c", "speaker_d", "speaker_e",
    "speaker a", "speaker b", "speaker c", "speaker d", "speaker e",
    "speaker", "speakers",
    "unknown", "unknown speaker", "unknown_speaker",
    "未知说话人",
    "未知说话人1",
    "未知说话人2",
    "未知说话人3",
    "未知",
    "说话人",
    "说话人1",
    "说话人2",
    "说话人3",
    "男声",
    "女声",
    "主持人",
    "嘉宾",
    "采访者",
    "受访者",
    "旁白",
    "narrator", "host", "guest", "interviewer", "interviewee",
    "voice", "person", "anonymous",
    "话者",
    "话者1",
    "话者2",
    "话者3",
    "人物",
    "人物1",
    "人物2",
    "人物3",
})

_GENERIC_NUMBERED_RE = re.compile(
    r"^(speaker|unknown|voice|person|话者|人物|说话人)[ _]?[0-9]+$"
)
_PURE_DIGITS_RE = re.compile(r"^[0-9]+$")
_SINGLE_ASCII_RE = re.compile(r"^[a-z]$")


def is_generic_speaker_name_key(key: str | None) -> bool:
    """Return True when a normalized speaker name is too generic for
    cross-source matching.

    Accepts ``None`` and returns ``False`` (caller may pass DB-nullable
    column directly). Input is expected to already be normalized via
    :func:`normalize_speaker_name_key`.

    Filters:
    - Hardcoded blacklist of zh/en placeholder names.
    - Length < 2 chars.
    - Single ASCII letter or pure-digit names.
    - "<role>[ _]<digits>" pattern (e.g. ``speaker_1``, ``话者 2``).

    Whitespace-only input is treated like ``None`` and returns
    ``False``: it carries no information about whether the speaker is
    a placeholder, so the cross-source filter should not artificially
    flip to generic on what is effectively a missing value.
    """
    if not key:
        return False
    k = key.strip()
    if not k:
        return False
    if len(k) < 2:
        return True
    if _SINGLE_ASCII_RE.match(k):
        return True
    if _PURE_DIGITS_RE.match(k):
        return True
    if _GENERIC_NUMBERED_RE.match(k):
        return True
    return k in _GENERIC_SPEAKER_NAME_KEYS


def build_cloned_voice_label(
    speaker_name: str | None,
    *,
    cloned_at: datetime | None = None,
) -> str:
    """Human-readable cloned-voice label: ``{speaker_name} · {clone_time}``."""
    name = (speaker_name or "").strip() or "Speaker"
    when = cloned_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        when = when.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        when = when.astimezone(timezone(timedelta(hours=8)))
    timestamp = when.strftime("%Y-%m-%d %H:%M")
    max_name_len = max(1, 200 - len(" · ") - len(timestamp))
    if len(name) > max_name_len:
        name = name[:max_name_len].rstrip()
    return f"{name} · {timestamp}"


def _set_if_empty(
    obj: object,
    attr: str,
    value: object | None,
    *,
    voice_id: str | None = None,
) -> None:
    if value is None:
        return
    existing = getattr(obj, attr, None)
    if existing is None or existing == "":
        setattr(obj, attr, value)
        return
    if existing != value:
        logger.warning(
            "user_voice immutable source metadata conflict: attr=%s existing=%r incoming=%r voice_id=%s",
            attr,
            existing,
            value,
            voice_id,
        )


async def list_user_voices(
    db: AsyncSession,
    user_id: object,
    *,
    include_expired: bool = False,
    include_temporary: bool = False,
) -> list[UserVoice]:
    """List a user's voices.

    Phase 4.3a §6.4 (Codex P1-2): ``include_temporary`` defaults to
    ``False`` so the user-facing "我的音色" list does NOT show Express
    auto-clone temporary voices (``is_temporary=True``). Those are
    task-scoped — surfacing them in the long-term library would confuse
    users (they'd see voices about to be swept). Callers that explicitly
    need temporary rows (e.g. future Phase 4.3b sweeper) pass True.

    NB: this is independent of routing decisions — segment TTS routing
    reads ``lookup_clone_voice_routing_metadata`` which deliberately
    INCLUDES temporary voices (the just-cloned temp voice is exactly what
    the current job's segments need). Do NOT add an is_temporary filter
    there.
    """
    stmt = select(UserVoice).where(UserVoice.user_id == user_id)
    if not include_expired:
        stmt = stmt.where(UserVoice.expired_at.is_(None))
    if not include_temporary:
        # Phase 4.3a §6.4: hide Express auto-clone temporary voices from
        # the long-term library list by default.
        stmt = stmt.where(UserVoice.is_temporary.is_(False))
    stmt = stmt.order_by(UserVoice.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_active_voices_for_user_and_provider(
    db: AsyncSession,
    user_id: object,
    *,
    provider: str,
    include_temporary: bool = False,
) -> int:
    """统计某 user 当前 active（``expired_at IS NULL``） + 指定 ``provider`` 的音色数。

    Phase 4.1 C.2 二轮 review（Codex 2026-05-25）：CosyVoice clone endpoint
    在调用付费 worker 前用此计数实施 ``cosyvoice_clone_max_voices_per_user``
    quota，避免灰度用户反复触发 ¥0.01/次 clone。

    Phase 4.3a §6.4 (Codex P1-2)：``include_temporary`` 默认 ``False`` ——
    Express 自动 clone 出的临时音色（``is_temporary=True``）**不**计入
    长期音色库配额 ``cosyvoice_clone_max_voices_per_user``。临时音色有
    自己的成本闸 ``express_cosyvoice_auto_clone_per_user_active_temp_cap``
    （spec §2.5），不该挤占用户长期 Studio 手动 clone 的额度。
    """
    where_clauses = [
        UserVoice.user_id == user_id,
        UserVoice.expired_at.is_(None),
        UserVoice.provider == provider,
    ]
    if not include_temporary:
        # Phase 4.3a §6.4: exclude temporary voices from the long-term
        # library quota count.
        where_clauses.append(UserVoice.is_temporary.is_(False))
    result = await db.execute(
        select(func.count())
        .select_from(UserVoice)
        .where(*where_clauses)
    )
    return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Phase 4.3a §2.5 成本闸 — Express auto-clone budget counters
# ---------------------------------------------------------------------------


async def count_express_auto_clones_today(
    db: AsyncSession,
    user_id: object,
) -> int:
    """统计某 user **今天**（UTC 自然日）发生过的 Express auto-clone 次数。

    spec §2.5 daily_cap 查询语义（Codex E2 review 重点）：

        provider = 'cosyvoice_voice_clone'
        AND created_from = 'express_auto'
        AND created_at >= today_start (UTC 00:00:00)
        AND user_id = :user_id

    **不**过滤 ``expired_at`` / ``is_temporary`` —— daily_cap 是"今天**曾经
    发生过**多少次付费 clone"的计数，软删了又跑、临时还是长期都算。这样
    用户无法通过删除临时音色绕过每日限额（防成本失控）。

    与 ``count_active_voices_for_user_and_provider``（active 库存计数，过滤
    expired_at IS NULL）语义**故意不同**：那个是"现在有多少"，这个是
    "今天发生过多少"。
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = await db.execute(
        select(func.count())
        .select_from(UserVoice)
        .where(
            UserVoice.user_id == user_id,
            UserVoice.provider == "cosyvoice_voice_clone",
            UserVoice.created_from == "express_auto",
            UserVoice.created_at >= today_start,
            # NB: 故意不过滤 expired_at / is_temporary
        )
    )
    return int(result.scalar() or 0)


async def count_active_temporary_voices(
    db: AsyncSession,
    user_id: object,
) -> int:
    """统计某 user 当前 active 的临时音色数。

    spec §2.5 active_temp_cap 查询语义：

        is_temporary = TRUE
        AND expired_at IS NULL
        AND user_id = :user_id

    用于防止用户多任务并发把临时音色表撑爆（每用户 active 临时音色上限
    ``express_cosyvoice_auto_clone_per_user_active_temp_cap``，默认 3）。

    soft-deleted（``expired_at`` 非空）的临时音色**不**计入 —— 它们已经
    被 sweeper / 用户删除，不再占额度。spec §2.5 未限 provider（任何
    is_temporary=true 都算），与 spec 文字一致。
    """
    result = await db.execute(
        select(func.count())
        .select_from(UserVoice)
        .where(
            UserVoice.user_id == user_id,
            UserVoice.is_temporary.is_(True),
            UserVoice.expired_at.is_(None),
        )
    )
    return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Phase 4.1 E.1: worker routing 元数据查询（Codex 2026-05-25 E v2 三签字版本）
# ---------------------------------------------------------------------------

# 路由字段白名单（Codex E v2 invariant #4）：
# E 在 approved review payload speakers[i] 上**只新增** 这 2 个字段，
# 而 ``tts_provider`` 已是既有字段（由 voice_selection_review 路径写入）。
# 任何不在此集合内的 user_voices 列 **不允许** 流入 review payload / job spec。
ROUTING_METADATA_FIELDS = frozenset({"requires_worker", "worker_target_model"})


class CloneVoiceRoutingError(ValueError):
    """E.6 fail-closed：voice_id 命中数据状态不一致（缺 user_voices row 但
    形似 CosyVoice clone / payload tts_provider 与 row 不匹配）.

    携带 HTTP 400 友好的 error code，供 gateway/job_intercept.py 的 approve
    拦截器映射成 ``HTTPException(400, ...)``，而不是 500。
    """
    def __init__(self, message: str, *, code: str, voice_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.voice_id = voice_id


async def lookup_clone_voice_routing_metadata(
    db: AsyncSession,
    *,
    user_id: object,
    voice_ids: list[str],
) -> dict[str, dict[str, object]]:
    """批量查 user_voices 拿 CosyVoice clone routing 元数据（Phase 4.1 E.1）。

    **strict filter**（Codex 2026-05-25 E v2 P1 #4）：

    - ``user_id`` 等于 caller（防跨账号偷别人 voice）
    - ``voice_id`` ∈ 入参列表（批量 IN 单次查询，避免 N+1）
    - ``expired_at IS NULL``（不返已删除）
    - ``provider == "cosyvoice_voice_clone"``
    - ``tts_provider == "cosyvoice"``
    - ``requires_worker == True``
    - ``target_model`` 非空非 NULL

    任一条件不匹配 → row 不返回。

    Returns
    -------
    dict[voice_id, {requires_worker, worker_target_model}]
        只含命中 row 的 voice_id。**不返**任何非白名单字段（``label`` /
        ``source_speaker_name`` / ``billing_sku`` / ``clone_provider_request_id``
        / ... 一律不携带，防止泄漏到 review payload）。

    Notes
    -----
    单次 SQL ``IN`` 查询 + dict 映射；N speakers → 1 次 DB 查询。
    """
    if not voice_ids:
        return {}
    # 去重防同一 voice_id 重复进 IN clause
    distinct_voice_ids = list({vid for vid in voice_ids if vid})
    if not distinct_voice_ids:
        return {}

    stmt = select(
        UserVoice.voice_id,
        UserVoice.target_model,
    ).where(
        UserVoice.user_id == user_id,
        UserVoice.voice_id.in_(distinct_voice_ids),
        UserVoice.expired_at.is_(None),
        UserVoice.provider == "cosyvoice_voice_clone",
        UserVoice.tts_provider == "cosyvoice",
        UserVoice.requires_worker.is_(True),
        UserVoice.target_model.isnot(None),
        UserVoice.target_model != "",
    )
    result = await db.execute(stmt)

    routing: dict[str, dict[str, object]] = {}
    for row in result.all():
        voice_id, target_model = row.voice_id, row.target_model
        # 白名单输出：只两个字段，绝不携带 label / billing_sku / request_id 等
        routing[voice_id] = {
            "requires_worker": True,
            "worker_target_model": str(target_model),
        }
    return routing


def _clean_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _voice_provider_compatible(
    voice: UserVoice,
    *,
    provider: str | None,
    tts_provider: str | None,
    platform: str | None,
) -> bool:
    if provider and voice.provider != provider:
        return False
    if tts_provider and voice.tts_provider != tts_provider:
        return False
    if platform and voice.platform != platform:
        return False
    return True


def _score_user_voice_match(
    voice: UserVoice,
    *,
    source_content_hash: str | None,
    source_speaker_id: str | None,
    source_speaker_name_key: str | None,
) -> UserVoiceMatch | None:
    voice_hash = _clean_optional_text(getattr(voice, "source_content_hash", None))
    if not source_content_hash or not voice_hash or voice_hash != source_content_hash:
        return None

    voice_speaker_id = _clean_optional_text(getattr(voice, "source_speaker_id", None))
    if source_speaker_id and voice_speaker_id and voice_speaker_id == source_speaker_id:
        return UserVoiceMatch(
            voice=voice,
            confidence="strong",
            reason="same_source_content_hash_and_speaker_id",
            score=100,
            match_scope="same_source_strong",
        )

    voice_name_key = _clean_optional_text(getattr(voice, "source_speaker_name_key", None))
    # Plan §匹配等级 §same_source_named (line 124) requires the speaker
    # name to NOT be a generic placeholder. Without this filter a
    # job whose speaker is labelled "Speaker A" would mass-collide
    # against every other "Speaker A" the user has cloned, blurring
    # the auto-vs-confirm boundary the candidate UX depends on. Fall
    # through to the weak ``same_source_speaker_id_changed`` branch
    # below — that one is honest about its low confidence.
    if (
        source_speaker_name_key
        and voice_name_key
        and voice_name_key == source_speaker_name_key
        and not is_generic_speaker_name_key(source_speaker_name_key)
    ):
        return UserVoiceMatch(
            voice=voice,
            confidence="medium",
            reason="same_source_content_hash_and_speaker_name",
            score=70,
            match_scope="same_source_named",
        )

    if source_speaker_id and voice_speaker_id and voice_speaker_id != source_speaker_id:
        return UserVoiceMatch(
            voice=voice,
            confidence="weak",
            reason="same_source_content_hash_different_speaker_id",
            score=30,
            match_scope="same_source_speaker_id_changed",
        )

    return None


def _score_cross_source_match(
    voice: UserVoice,
    *,
    source_speaker_name_key: str | None,
) -> UserVoiceMatch | None:
    """Cross-source weak candidate: same normalized speaker name across
    a different source video.

    Phase 1 §"cross_source_named_person": only triggers when caller
    explicitly opts into ``include_cross_source=True`` in
    :func:`match_user_voices`. Never auto-reuse-allowed.
    """
    if not source_speaker_name_key:
        return None
    if is_generic_speaker_name_key(source_speaker_name_key):
        return None
    voice_name_key = _clean_optional_text(getattr(voice, "source_speaker_name_key", None))
    if not voice_name_key or voice_name_key != source_speaker_name_key:
        return None
    if is_generic_speaker_name_key(voice_name_key):
        return None
    return UserVoiceMatch(
        voice=voice,
        confidence="weak",
        reason="cross_source_same_speaker_name_key",
        score=20,
        match_scope="cross_source_named_person",
    )


def voice_evidence_dict(voice) -> dict:
    """Curated provenance fields for the candidate UI.

    Surface human-readable fields only — never IDs / hashes — so the
    candidate widget can render "what you cloned from" without leaking
    cross-row identifiers. Shared between the internal
    ``/api/internal/user-voices/candidates`` and public
    ``/job-api/jobs/{id}/voice-candidates`` endpoints.
    """
    created_at = getattr(voice, "created_at", None)
    return {
        "source_video_title": getattr(voice, "source_video_title", None),
        "source_speaker_name": getattr(voice, "source_speaker_name", None),
        "clone_sample_seconds": getattr(voice, "clone_sample_seconds", None),
        "created_at": created_at.isoformat() if created_at else None,
    }


def candidate_to_dict(match: "UserVoiceMatch") -> dict:
    """Serialize a :class:`UserVoiceMatch` for the Phase 1 unified
    candidate envelope.

    Output shape mirrors what both the internal candidate route and
    the public Studio/Post-edit ``voice-candidates`` route emit —
    they share this helper so the two never drift.

    Includes ``requires_user_confirmation`` (inverse of
    ``auto_reuse_allowed``) and the curated ``evidence`` block from
    :func:`voice_evidence_dict`.
    """
    voice = match.voice
    return {
        "voice_id": getattr(voice, "voice_id", None),
        "user_voice_id": str(getattr(voice, "id", "") or ""),
        "label": getattr(voice, "label", None),
        "confidence": match.confidence,
        "match_scope": getattr(match, "match_scope", None),
        "requires_user_confirmation": not match.auto_reuse_allowed,
        "score": match.score,
        "reason": match.reason,
        "evidence": voice_evidence_dict(voice),
    }


def auto_reuse_summary_dict(match: "UserVoiceMatch") -> dict:
    """Minimal envelope describing the top strong-match auto-reuse
    target. Mirrors the inline structure consumed by Smart's
    pipeline path so callers don't have to peek into the full
    candidate dict. Shared by the internal + public candidate
    endpoints."""
    voice = match.voice
    return {
        "voice_id": getattr(voice, "voice_id", None),
        "user_voice_id": str(getattr(voice, "id", "") or ""),
        "label": getattr(voice, "label", None),
        "confidence": match.confidence,
        "match_scope": getattr(match, "match_scope", None),
        "auto_reuse_allowed": True,
        "reason": match.reason,
    }


async def match_user_voices(
    db: AsyncSession,
    *,
    user_id: object,
    source_content_hash: str | None,
    source_speaker_id: str | None = None,
    source_speaker_name: str | None = None,
    source_speaker_name_key: str | None = None,
    provider: str | None = None,
    tts_provider: str | None = None,
    platform: str | None = None,
    limit: int = 5,
    include_cross_source: bool = False,
    include_temporary: bool = False,
) -> list[UserVoiceMatch]:
    """Find personal voice candidates for a user.

    Same-source matching is conservative: same user, same provider
    triplet, same ``source_content_hash``, scored by speaker_id /
    speaker_name_key / speaker_id-changed.

    Phase 1 (plan 2026-05-17): when ``include_cross_source=True``, also
    matches cross-source rows by normalized speaker name (gated by
    :func:`is_generic_speaker_name_key`). Cross-source matches are
    weak by definition — they cannot auto-reuse.

    Default ``include_cross_source=False`` preserves the legacy
    behaviour for the old ``voice-match`` and ``internal match``
    endpoints.

    Phase 4.3a §6.4 (Codex P1-2): ``include_temporary`` defaults to
    ``False`` so Smart auto-reuse / candidate-first selection NEVER picks
    up Express auto-clone temporary voices (``is_temporary=True``) across
    jobs. A temporary voice belongs to ONE job; reusing it in another
    job would make a 7-day sweep (Phase 4.3b) silently break the second
    job's audio. Both the same-source and cross-source SELECT clauses
    enforce this. Callers needing temporary rows pass True explicitly.
    """
    clean_hash = _clean_optional_text(source_content_hash)
    clean_speaker_id = _clean_optional_text(source_speaker_id)
    clean_name_key = (
        _clean_optional_text(source_speaker_name_key)
        or normalize_speaker_name_key(source_speaker_name)
    )
    clean_provider = _clean_optional_text(provider)
    clean_tts_provider = _clean_optional_text(tts_provider)
    clean_platform = _clean_optional_text(platform)
    max_results = max(1, min(int(limit or 5), 20))

    # Legacy contract: same-source matching requires non-empty hash.
    # When the caller doesn't ask for cross-source, return [] now to
    # avoid issuing a useless query.
    if not clean_hash and not include_cross_source:
        return []

    matches: list[UserVoiceMatch] = []
    seen_voice_ids: set[str] = set()

    if clean_hash:
        same_source_where = [
            UserVoice.user_id == user_id,
            UserVoice.expired_at.is_(None),
            UserVoice.source_content_hash == clean_hash,
        ]
        if not include_temporary:
            # Phase 4.3a §6.4: never reuse a temporary Express clone
            # voice across jobs.
            same_source_where.append(UserVoice.is_temporary.is_(False))
        result = await db.execute(
            select(UserVoice).where(*same_source_where)
        )
        for voice in result.scalars().all():
            if getattr(voice, "expired_at", None) is not None:
                continue
            if not _voice_provider_compatible(
                voice,
                provider=clean_provider,
                tts_provider=clean_tts_provider,
                platform=clean_platform,
            ):
                continue
            match = _score_user_voice_match(
                voice,
                source_content_hash=clean_hash,
                source_speaker_id=clean_speaker_id,
                source_speaker_name_key=clean_name_key,
            )
            if match is not None:
                matches.append(match)
                vid = getattr(voice, "voice_id", None)
                if vid:
                    seen_voice_ids.add(vid)

    if include_cross_source and clean_name_key and not is_generic_speaker_name_key(clean_name_key):
        # Cross-source: same speaker name across a DIFFERENT source video.
        # Filter via Python so we keep provider compatibility + expired_at
        # checks in one place; the DB just narrows by name_key + user_id.
        #
        # 2026-05-21 spec change: previously excluded NULL-hash legacy
        # rows here via ``source_content_hash.is_not(None)`` (per old
        # plan §兼容性和历史音色). User feedback after Stanford job
        # (job_f2abf73878b...) — Matt voice from 2026-04-26 with NULL
        # hash but matching name_key was silently excluded, smart
        # then fresh-cloned again creating library duplicates. The
        # filter is now removed so 100+ legacy named voices become
        # cross-source candidates.
        #
        # Safety net: NULL-hash voices still need a non-generic
        # name_key (filtered by ``is_generic_speaker_name_key`` above)
        # to surface, so old "speaker_a" / "主持人" placeholder voices
        # don't pollute candidates.
        cross_where = [
            UserVoice.user_id == user_id,
            UserVoice.expired_at.is_(None),
            UserVoice.source_speaker_name_key == clean_name_key,
        ]
        if not include_temporary:
            # Phase 4.3a §6.4: cross-source candidates also exclude
            # temporary Express clones.
            cross_where.append(UserVoice.is_temporary.is_(False))
        if clean_hash:
            # Only exclude same-source rows when we have a current hash;
            # NULL-hash voices pass through (they came from a different
            # source by definition — we just don't know which).
            cross_where.append(
                (UserVoice.source_content_hash != clean_hash)
                | (UserVoice.source_content_hash.is_(None))
            )
        cross_result = await db.execute(select(UserVoice).where(*cross_where))
        for voice in cross_result.scalars().all():
            if getattr(voice, "expired_at", None) is not None:
                continue
            vid = getattr(voice, "voice_id", None)
            if vid and vid in seen_voice_ids:
                continue
            if not _voice_provider_compatible(
                voice,
                provider=clean_provider,
                tts_provider=clean_tts_provider,
                platform=clean_platform,
            ):
                continue
            cross_match = _score_cross_source_match(
                voice,
                source_speaker_name_key=clean_name_key,
            )
            if cross_match is not None:
                matches.append(cross_match)
                if vid:
                    seen_voice_ids.add(vid)

        # 2026-05-21 spec: promote unique cross-source named match to
        # ``strong_named`` so smart auto-reuses without pausing for
        # user confirmation. Uniqueness gates it — if user library has
        # 2+ voices with the same name (e.g., re-cloned same speaker
        # from different videos creating duplicate entries), all stay
        # weak so smart pauses and lets user pick which one.
        #
        # Rationale: a non-generic speaker name (filtered by
        # ``is_generic_speaker_name_key`` above) is strong evidence
        # the cloned voice IS the same person — celebrity name + only
        # one in user's library = high-confidence auto-reuse. The
        # uniqueness criterion is more conservative than "name length
        # heuristic" because it adapts to each user's actual library.
        cross_source_matches = [
            m for m in matches
            if m.match_scope == "cross_source_named_person"
        ]
        if len(cross_source_matches) == 1:
            only = cross_source_matches[0]
            idx = matches.index(only)
            matches[idx] = UserVoiceMatch(
                voice=only.voice,
                confidence="strong_named",
                reason="cross_source_unique_specific_name",
                score=60,
                match_scope="cross_source_named_unique",
            )

    matches.sort(
        key=lambda item: (
            item.score,
            getattr(item.voice, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return matches[:max_results]


async def add_user_voice(
    db: AsyncSession,
    *,
    user_id: object,
    voice_id: str,
    label: str,
    provider: str = "minimax_voice_clone",
    tts_provider: str | None = "minimax_tts",
    platform: str | None = "minimax_domestic",
    source_speaker_id: str | None = None,
    source_job_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    source_content_hash: str | None = None,
    source_upload_md5: str | None = None,
    source_video_title: str | None = None,
    source_speaker_name: str | None = None,
    source_speaker_name_key: str | None = None,
    source_published_at: datetime | None = None,
    source_content_summary: str | None = None,
    source_content_era: str | None = None,
    source_content_tags: object | None = None,
    clone_sample_seconds: float | None = None,
    clone_sample_segment_ids: object | None = None,
    created_from: str | None = None,
    notes: str | None = None,
    # ---- Phase 4.1 (migration 030): CosyVoice worker dispatch + audit anchors ----
    region_constraint: str = "overseas_ok",
    requires_worker: bool = False,
    target_model: str | None = None,
    worker_provider: str | None = None,
    worker_region: str | None = None,
    clone_api_model: str | None = None,
    billing_sku: str | None = None,
    clone_provider_request_id: str | None = None,
    clone_worker_request_id: str | None = None,
    # ---- Phase 4.3a (migration 031): 临时音色生命周期（spec §6.3.1） ----
    is_temporary: bool = False,
    temporary_expires_at: datetime | None = None,
    # ---- P3b (CodeX 钱-正确性 #2): commit=False 让调用方控制完整事务 ----
    # smart 预览克隆的 register+bill 须在**单一事务**内同时写 billing event +
    # 入 user_voices（钱的事实与音色入库原子）。默认 True 保持既有调用方不变。
    commit: bool = True,
) -> UserVoice:
    if source_speaker_name_key is None:
        source_speaker_name_key = normalize_speaker_name_key(source_speaker_name)

    # Phase 4.3a §6.3.1 (Codex 三轮 P1-2): non-temporary writes MUST clear
    # temporary_expires_at. This guards against:
    #   - caller bug passing is_temporary=False + a stale ts
    #   - existing-revive path where the row was previously temporary
    #     (is_temporary=True + ts) and is now being upgraded to long-term
    #     (user clicked "保存到我的音色库" in a future Phase 4.3b flow)
    # The rule is one line, applied to both insert + revive branches below.
    effective_temporary_expires_at = (
        temporary_expires_at if is_temporary else None
    )

    # Check existing (including expired — revive if re-cloned)
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.label = label
        existing.provider = provider
        existing.tts_provider = tts_provider
        existing.platform = platform
        existing.notes = notes
        existing.expired_at = None
        existing.updated_at = datetime.now(timezone.utc)
        _set_if_empty(existing, "source_speaker_id", source_speaker_id, voice_id=voice_id)
        _set_if_empty(existing, "source_job_id", source_job_id, voice_id=voice_id)
        _set_if_empty(existing, "source_type", source_type, voice_id=voice_id)
        _set_if_empty(existing, "source_ref", source_ref, voice_id=voice_id)
        _set_if_empty(existing, "source_content_hash", source_content_hash, voice_id=voice_id)
        _set_if_empty(existing, "source_upload_md5", source_upload_md5, voice_id=voice_id)
        _set_if_empty(existing, "source_video_title", source_video_title, voice_id=voice_id)
        _set_if_empty(existing, "source_speaker_name", source_speaker_name, voice_id=voice_id)
        _set_if_empty(existing, "source_speaker_name_key", source_speaker_name_key, voice_id=voice_id)
        _set_if_empty(existing, "source_published_at", source_published_at, voice_id=voice_id)
        _set_if_empty(existing, "source_content_summary", source_content_summary, voice_id=voice_id)
        _set_if_empty(existing, "source_content_era", source_content_era, voice_id=voice_id)
        _set_if_empty(existing, "source_content_tags", source_content_tags, voice_id=voice_id)
        _set_if_empty(existing, "clone_sample_seconds", clone_sample_seconds, voice_id=voice_id)
        _set_if_empty(existing, "clone_sample_segment_ids", clone_sample_segment_ids, voice_id=voice_id)
        _set_if_empty(existing, "created_from", created_from, voice_id=voice_id)
        # ---- Phase 4.1：刷新 worker dispatch / audit anchors（每次 clone 重新填）----
        # 这些字段在每次 clone 都会变（新的 worker_request_id / provider_request_id），
        # 所以不用 _set_if_empty 而是直接覆盖。requires_worker / region_constraint
        # 重置成最新决策，避免旧 row 看起来仍是 overseas_ok 但实际是 mainland clone。
        existing.region_constraint = region_constraint
        existing.requires_worker = requires_worker
        existing.target_model = target_model
        existing.worker_provider = worker_provider
        existing.worker_region = worker_region
        existing.clone_api_model = clone_api_model
        existing.billing_sku = billing_sku
        existing.clone_provider_request_id = clone_provider_request_id
        existing.clone_worker_request_id = clone_worker_request_id
        # ---- Phase 4.3a §6.3.1：临时音色字段每次显式覆盖（不走 _set_if_empty）----
        # 这两个字段表达"本次 clone 决定该 voice 是否临时"，不是 once-and-for-all。
        # revive 时必须能 long-term↔temporary 双向切换；非 temp 强制清 ts 防 stale。
        existing.is_temporary = is_temporary
        existing.temporary_expires_at = effective_temporary_expires_at
        if commit:
            await db.commit()
        else:
            await db.flush()  # 让变更可见但由调用方控制事务
        return existing

    voice = UserVoice(
        user_id=user_id,
        voice_id=voice_id,
        label=label,
        provider=provider,
        tts_provider=tts_provider,
        platform=platform,
        source_speaker_id=source_speaker_id,
        source_job_id=source_job_id,
        source_type=source_type,
        source_ref=source_ref,
        source_content_hash=source_content_hash,
        source_upload_md5=source_upload_md5,
        source_video_title=source_video_title,
        source_speaker_name=source_speaker_name,
        source_speaker_name_key=source_speaker_name_key,
        source_published_at=source_published_at,
        source_content_summary=source_content_summary,
        source_content_era=source_content_era,
        source_content_tags=source_content_tags,
        clone_sample_seconds=clone_sample_seconds,
        clone_sample_segment_ids=clone_sample_segment_ids,
        created_from=created_from,
        notes=notes,
        # Phase 4.1 worker dispatch + audit anchors
        region_constraint=region_constraint,
        requires_worker=requires_worker,
        target_model=target_model,
        worker_provider=worker_provider,
        worker_region=worker_region,
        clone_api_model=clone_api_model,
        billing_sku=billing_sku,
        clone_provider_request_id=clone_provider_request_id,
        clone_worker_request_id=clone_worker_request_id,
        # Phase 4.3a §6.3.1：临时音色生命周期（非 temp 强制清 ts 防 stale）
        is_temporary=is_temporary,
        temporary_expires_at=effective_temporary_expires_at,
    )
    db.add(voice)
    if commit:
        await db.commit()
        await db.refresh(voice)
    else:
        await db.flush()  # 拿到 DB 默认值/PK，但事务由调用方 commit
    return voice


async def delete_user_voice(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> bool:
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        return False
    voice.expired_at = datetime.now(timezone.utc)
    voice.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def mark_voice_expired(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> bool:
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    voice = result.scalar_one_or_none()
    if voice is None:
        return False
    voice.expired_at = datetime.now(timezone.utc)
    voice.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def update_user_voice_label(
    db: AsyncSession,
    voice: UserVoice,
    *,
    label: str,
) -> UserVoice:
    """Update an already-fetched UserVoice's display label.

    Caller must pass the row (e.g. from :func:`fetch_user_voice`) so
    there's no double-SELECT on PATCH.
    """
    now = datetime.now(timezone.utc)
    voice.label = label
    voice.updated_at = now
    await db.commit()
    await db.refresh(voice)
    return voice


async def fetch_user_voice(
    db: AsyncSession,
    user_id: object,
    voice_id: str,
) -> UserVoice | None:
    """Look up a single voice owned by ``user_id``. Returns None if not found
    or if the voice has been expired (mark_voice_expired)."""
    result = await db.execute(
        select(UserVoice).where(
            UserVoice.user_id == user_id,
            UserVoice.voice_id == voice_id,
            UserVoice.expired_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def update_user_voice_speed_calibration(
    db: AsyncSession,
    *,
    voice_id: str,
    user_id: object,
    cps: float,
    model_key: str,
) -> UserVoice:
    """Atomically persist a per-model calibration result onto a user voice.

    Plan v4.1 codex F-v4.1-1 fix for the JSONB-key-loss race: when T1
    parallel-calibrates ``speech-2.8-turbo`` and ``speech-2.8-hd`` against
    the same row, the legacy implementation read the dict, set its key,
    and replaced the whole field — the second commit overwrote the first
    commit's key. This helper holds a ``SELECT ... FOR UPDATE`` row lock
    across the read-modify-write so concurrent tasks serialize and merge.

    Plan v4.2 codex F-v4.2-1 fix: query MUST use ``UserVoice.voice_id``
    (provider-side string), NOT ``UserVoice.id`` (UUID primary key).
    Together with ``user_id`` they form the uniqueness constraint
    ``uq_user_voices_user_voice``.

    Plan v4.2 codex F-v4.2-3 fix: helper takes primitive (voice_id, user_id)
    NOT a caller-fetched row object. The caller's row may be stale; we
    re-fetch under the lock so the merge sees the freshest dict.

    Parameters
    ----------
    db:
        AsyncSession. Helper opens its own ``async with db.begin()`` block;
        caller MUST NOT have an open transaction or the begin nests.
    voice_id:
        Provider-side voice id (e.g. MiniMax voice id string), NOT the
        UUID primary key.
    user_id:
        Owner user_id (UUID); accepts ``str`` or ``uuid.UUID`` since the
        SQLAlchemy column auto-coerces.
    cps:
        Calibrated chars-per-second value for ``model_key``.
    model_key:
        Canonical model id (e.g. ``"speech-2.8-turbo"``). Required —
        the per-model JSONB is the only authoritative storage; the scalar
        ``chars_per_second`` field becomes the cross-model mean for
        tooltip display only.

    Raises
    ------
    VoiceNotFoundError
        When no row matches ``(user_id, voice_id)`` — usually means the
        voice was deleted between the caller's intent and our SELECT.
    """
    if not model_key:
        raise ValueError("model_key is required (plan v4 T0-D)")

    async with db.begin():
        # codex v4.4 P1-2: filter expired_at IS NULL so we never write
        # calibration back to a soft-deleted row. This is a defense-
        # in-depth pair with the resolve-time filter in
        # voice_calibration_review_preflight._resolve_targets_user_first;
        # protects against the race where a voice expires between
        # T2's read snapshot and the write.
        result = await db.execute(
            select(UserVoice)
              .where(
                  UserVoice.voice_id == voice_id,    # F-v4.2-1: provider id, NOT UUID PK
                  UserVoice.user_id == user_id,
                  UserVoice.expired_at.is_(None),
              )
              .with_for_update()
        )
        voice = result.scalar_one_or_none()
        if voice is None:
            raise VoiceNotFoundError(f"user_voices missing/expired: voice_id={voice_id!r} user_id={user_id!r}")

        merged = _merged_by_model(voice.chars_per_second_by_model, model_key=model_key, cps=cps)
        voice.chars_per_second_by_model = merged
        # Cross-model mean for tooltip display only — Pre-TTS rewrite
        # reads chars_per_second_by_model[tts_model] preferentially via
        # voice_speed_catalog.resolve_chars_per_second; the scalar is
        # only the fallback for that resolver.
        voice.chars_per_second = sum(merged.values()) / len(merged)
        now = datetime.now(timezone.utc)
        voice.speed_calibrated_at = now
        voice.updated_at = now
    return voice


# Backward-compat alias for the legacy single-helper signature. The manual
# /calibrate-speed endpoint and any pre-T0 callers still import the old
# name; the new behaviour (atomic merge under FOR UPDATE) applies regardless.
#
# v4.3 follow-up: callers should migrate to ``update_user_voice_speed_calibration``
# directly, which makes the (voice_id, user_id) primitive contract explicit.
# This wrapper accepts the legacy "voice row passed in" form and delegates.
async def update_voice_speed_calibration(
    db: AsyncSession,
    voice: UserVoice,
    *,
    cps: float,
    model_key: str | None = None,
) -> UserVoice:
    """Legacy wrapper kept for the manual endpoint's existing call site
    until the T0 endpoint refactor lands. New callers should use
    :func:`update_user_voice_speed_calibration` directly.
    """
    if not model_key:
        # Pre-T0-D callers that didn't track model — set scalar only.
        # This path is unreachable post-T0-D; kept for migration safety.
        now = datetime.now(timezone.utc)
        voice.chars_per_second = float(cps)
        voice.speed_calibrated_at = now
        voice.updated_at = now
        await db.commit()
        await db.refresh(voice)
        return voice

    # New atomic path
    return await update_user_voice_speed_calibration(
        db,
        voice_id=voice.voice_id,
        user_id=voice.user_id,
        cps=cps,
        model_key=model_key,
    )
