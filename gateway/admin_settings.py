"""Admin settings API: read/write global platform configuration + job management."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

# Make src/ importable so we can reuse llm_registry (single source of truth).
# In Docker, gateway runs in /opt/gateway/ while app code is in /opt/aivideotrans/app/src/.
# Try multiple candidate paths; if none work, the import below will fall back gracefully.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",          # local dev: repo_root/src
    Path("/opt/aivideotrans/app/src"),                       # Docker container
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, delete as sa_delete

from auth import get_current_user
from config import settings as app_settings
from database import async_session, get_db
from internal_auth import internal_headers

# P0-5 (audit 2026-05-07): admin_settings.json was previously written via
# raw SETTINGS_FILE.write_text without a load→modify→save lock or atomic
# rename, so concurrent admin writes (e.g. two browser tabs both saving
# /review-prompts) could either corrupt the file on crash or silently
# overwrite each other. Both helpers below come from src/.
from services._file_lock import file_lock  # noqa: E402  # sys.path tweak above
from utils.atomic_io import atomic_write_json  # noqa: E402
from models import AdminAuditLog, Job, User
from project_cleanup import _is_safe_project_dir

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "admin_settings.json"

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Settings schema ---

_VALID_ENDPOINT_MODES = {"international", "mainland"}

# Phase D-5 (2026-05-05) — whisper-alignment field whitelists. Same
# whitelist as ``services/admin_settings.py::_parse_whisper_settings``;
# duplicated here because Gateway can't import from src/services
# (separate Python image). Any change must be mirrored both sides —
# enforced as a contract by tests that round-trip a sample JSON.
_VALID_WHISPER_TRIGGERS = {"publish", "deliverable", "manual"}
_VALID_WHISPER_MODELS = {"tiny", "base", "small", "medium", "large-v3"}


class AdminSettings(BaseModel):
    tts_provider: str = "minimax"          # "minimax" or "mimo"
    # DEPRECATED (2026-04-09 prompt-model-management plan §8.1): these two globals were
    # replaced by per-prompt + per-mode selection in ``prompt_models`` (studio / express).
    # Fields retained only so admin_settings.json can be rolled back to pre-2026-04-09
    # Gateway without breaking serialization. Neither GET /api/admin/review-prompts nor
    # POST /api/admin/review-prompts reads or writes these fields anymore.
    # Safe to remove after 2026-06 if no rollback to pre-prompt-models Gateway is planned.
    review_model: str = "gemini_pro"       # DEPRECATED — see comment above
    translation_model: str = "deepseek"    # DEPRECATED — see comment above
    skip_translation_config_for_users: bool = True  # Normal users skip translation config step
    skip_all_reviews_for_free_users: bool = True   # Free users: fully automatic pipeline
    free_user_max_duration_minutes: float = 10.0   # Max video duration for free users (minutes)
    enable_pre_tts_rewrite: bool = True            # Pre-TTS rewrite to match target duration
    express_tts_provider: str = "cosyvoice"        # Default TTS provider for express mode
    studio_tts_provider: str = "minimax"           # Default TTS provider for studio mode
    cosyvoice_runtime_endpoint_mode: str = "international"  # CosyVoice runtime: "international" or "mainland"
    cosyvoice_offline_endpoint_mode: str = "mainland"       # CosyVoice offline: "international" or "mainland"
    translation_char_range_min_factor: float = 0.85         # min_chars = target_chars * this
    translation_char_range_max_factor: float = 1.15         # max_chars = target_chars * this
    voice_clone_cost_credits: int = 500  # DEPRECATED: migrated to pricing_runtime. Kept for compat.
    # --- Phase 2 Task 1 — translation-duration-alignment ---
    # When enabled, MiniMax TTS calls receive a per-segment `speed` parameter
    # in voice_setting (instead of the hardcoded 1.0). The decision is made
    # in tts_generator from the segment's predicted vs target duration, with
    # a hard clamp to [0.92, 1.08] (default mode) or [0.85, 1.15] (aggressive).
    # Disabled by default until baseline metrics confirm we want it on.
    tts_speed_adjustment_enabled: bool = False              # Task 1 master switch (MiniMax only for now)
    tts_speed_mode: str = "default"                         # "default" / "aggressive" / "extreme" / "unlimited"
    # CodeX P2-4: Task 2 voice-match speed dimension (W_SPEED in reranker).
    # When OFF, combined_rerank ignores target_chars_per_second and falls
    # back to the legacy 8-dimension persona/age/pitch scoring.  Default OFF
    # for canary rollout — turn ON after observing speed_param_distribution
    # and Munger-style backup-promotion is acceptable.
    voice_match_speed_dimension_enabled: bool = False
    # When True, S5 alignment skips rewrite entirely and runs DSP atempo on
    # every TTS segment to force-match the original English duration.  Trades
    # listening quality for guaranteed time alignment.  Useful for tightly
    # synced content (subtitles, lip-sync) when LLM translation length
    # control is unreliable.
    force_dsp_alignment: bool = False
    # --- Phase D — Whisper subtitle alignment (2026-05-05) ---
    # Master switch + sub-policy fields. The runtime additionally requires
    # ``AVT_WHISPER_ALIGN_ENABLED=1`` env var to be set (ops capability
    # switch); admin policy here is necessary but not sufficient. See
    # ``src/modules/subtitles/cue_pipeline._whisper_align_enabled``.
    #
    # ``whisper_alignment_enabled``    — admin master switch
    # ``whisper_alignment_trigger``    — when does whisper actually run?
    #   "publish"     every task, at publish stage (slowest UX, best 1st delivery)
    #   "deliverable" only when user clicks Jianying / 素材包 (default; fast publish)
    #   "manual"      no auto-trigger anywhere (admin-only via dedicated endpoint)
    # ``whisper_alignment_skip_cache`` — bypass per-WAV cache (force fresh)
    # ``whisper_alignment_model``      — faster-whisper model size
    whisper_alignment_enabled: bool = False
    whisper_alignment_trigger: str = "deliverable"
    whisper_alignment_skip_cache: bool = False
    whisper_alignment_model: str = "small"
    # --- Smart MVP P2 (PR#3C-b3e, 2026-05-14) ---
    # Per-user cap on cloned voices stored in MiniMax personal library.
    # Drives the `voice_library_quota_remaining` snapshot Smart's
    # auto_voice_review consumes (plan §7.3 N=3 water mark). MiniMax's
    # actual account-level limit is opaque to us; this is an admin-
    # tunable soft cap so smart auto-clone refuses to keep filling the
    # user's library beyond a chosen threshold. Default 30 mirrors
    # MiniMax's commonly-stated per-account voice quota.
    smart_user_voice_clone_cap: int = 30

    @field_validator("whisper_alignment_trigger")
    @classmethod
    def validate_whisper_alignment_trigger(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_WHISPER_TRIGGERS:
            raise ValueError(
                f"whisper_alignment_trigger 必须是 "
                f"{sorted(_VALID_WHISPER_TRIGGERS)} 之一，收到: {v!r}"
            )
        return normalized

    @field_validator("whisper_alignment_model")
    @classmethod
    def validate_whisper_alignment_model(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_WHISPER_MODELS:
            raise ValueError(
                f"whisper_alignment_model 必须是 "
                f"{sorted(_VALID_WHISPER_MODELS)} 之一，收到: {v!r}"
            )
        return normalized

    @field_validator("tts_speed_mode")
    @classmethod
    def validate_tts_speed_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"default", "aggressive", "extreme", "unlimited"}:
            raise ValueError(
                f"tts_speed_mode 必须是 'default' / 'aggressive' / 'extreme' / 'unlimited' 之一，收到: {v!r}"
            )
        return normalized

    @field_validator("cosyvoice_runtime_endpoint_mode", "cosyvoice_offline_endpoint_mode")
    @classmethod
    def validate_endpoint_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_ENDPOINT_MODES:
            raise ValueError(f"端点模式必须是 {sorted(_VALID_ENDPOINT_MODES)} 之一，收到: {v!r}")
        return normalized


# --- Helpers ---

def _is_admin(user: User) -> bool:
    """Check admin via role field only.

    After running Alembic 002, all users get role='user' by default.
    To bootstrap an admin: UPDATE users SET role='admin' WHERE email='your-admin@example.com';
    """
    return (getattr(user, "role", None) or "user") == "admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def load_settings() -> AdminSettings:
    """Load settings from JSON file, returning defaults if missing."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return AdminSettings(**data)
        except Exception:
            logger.warning("Failed to parse %s, using defaults", SETTINGS_FILE)
    return AdminSettings()


def save_settings(s: AdminSettings) -> None:
    """Persist settings to JSON file, merging with existing data.

    Only overwrites the fields defined in AdminSettings.  Other keys
    (review_prompts, prompt_models, provider_api_keys) are preserved.
    """
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # P0-5 (audit 2026-05-07): wrap the load→merge→write sequence in
    # file_lock + atomic_write_json so two concurrent admin saves don't
    # both observe an old snapshot and last-write-wins, and a crash
    # mid-write doesn't leave a half-written admin_settings.json.
    with file_lock(SETTINGS_FILE):
        # Load existing data to preserve non-AdminSettings fields
        existing: dict = {}
        if SETTINGS_FILE.exists():
            try:
                existing = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Merge: AdminSettings fields overwrite, other keys preserved
        existing.update(s.model_dump())
        atomic_write_json(str(SETTINGS_FILE), existing)
        logger.info("Admin settings saved to %s", SETTINGS_FILE)


# --- Endpoints ---

@router.get("/settings")
async def get_admin_settings(
    user: User | None = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    return {"settings": load_settings().model_dump()}


@router.post("/settings")
async def update_admin_settings(
    body: AdminSettings,
    user: User | None = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    save_settings(body)
    return {"settings": body.model_dump()}


# ---------------------------------------------------------------------------
# Review prompts management
# ---------------------------------------------------------------------------

_PROMPT_KEYS = (
    "pass1",
    "pass2",
    "pass3",
    "translate",
    "rewrite",
    "probe_translate",
    "content_compliance",
)
_MODE_KEYS = ("studio", "express")

# ---------------------------------------------------------------------------
# Model metadata — single source of truth from llm_registry
# ---------------------------------------------------------------------------
from services.llm_registry import (  # noqa: E402
    MODEL_REGISTRY as _MODEL_REGISTRY,
    get_available_models_for_prompt as _available_models_for_prompt,
    get_all_models_with_status as _get_all_models_with_status,
    invalidate_cache as _invalidate_llm_cache,
)

# Derive _ALL_MODELS from the shared registry (no second copy)
_ALL_MODELS = [
    {
        "value": name,
        "label": info["label"],
        "cost_hint": info.get("cost_hint", ""),
        "cost_rank": info.get("cost_rank", 99),
        "supports_audio": info.get("supports_audio", False),
    }
    for name, info in _MODEL_REGISTRY.items()
]

_DEFAULT_MODELS = {
    "studio": {
        "pass1": "gemini_pro",
        "pass2": "gemini",
        "pass3": "gemini_pro",
        "translate": "deepseek",
        "rewrite": "deepseek",
        "probe_translate": "deepseek",
        "content_compliance": "gemini_31_flash_lite",
    },
    "express": {
        "pass1": "gemini",
        "pass2": "gemini",
        "pass3": "gemini",
        "translate": "deepseek",
        "rewrite": "deepseek",
        "probe_translate": "deepseek",
        "content_compliance": "gemini_31_flash_lite",
    },
}

_PROVIDER_KEY_ENVS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "mimo": "MIMO_API_KEY",
}

# Gemini auth: managed by client_factory, not via provider_api_keys
_GEMINI_AUTH_ENVS = ["GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_AI_EXPRESS_KEY", "GEMINI_API_KEY"]


def _mask_api_key(key: str) -> str:
    """Return masked key: '****xxxx' or empty string."""
    if not key or len(key) < 4:
        return ""
    return f"****{key[-4:]}"


def _is_masked_key(value: str) -> bool:
    """Detect if value looks like a masked key (e.g. '****abcd')."""
    return bool(value) and value.startswith("****")
_PROMPT_HISTORY_FILE = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "review_prompt_history.json"


def _load_prompt_history() -> list[dict]:
    """Load prompt version history.

    P0-5 follow-up (audit 2026-05-07): read-only, but callers that follow
    up with _save_prompt_history MUST hold file_lock(_PROMPT_HISTORY_FILE)
    around the load→modify→save pair to avoid lost-update races between
    update_review_prompts and delete_prompt_history.
    """
    if _PROMPT_HISTORY_FILE.exists():
        try:
            return json.loads(_PROMPT_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_prompt_history(history: list[dict]) -> None:
    """Persist prompt version history.

    P0-5 follow-up (audit 2026-05-07): file_lock + atomic_write_json so
    a partial write or a concurrent writer cannot corrupt the file or
    drop entries. Callers that do load→modify→save (update_review_prompts,
    delete_prompt_history) MUST also hold file_lock(_PROMPT_HISTORY_FILE)
    around the whole sequence — the lock here is a defense-in-depth
    backstop that protects the single write but doesn't protect the
    load→modify gap.
    """
    _PROMPT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(_PROMPT_HISTORY_FILE):
        atomic_write_json(str(_PROMPT_HISTORY_FILE), history)
    # NOTE: legacy non-atomic write removed; the inline call above replaces
    # _PROMPT_HISTORY_FILE.write_text(json.dumps(...), encoding="utf-8").


_DEFAULT_PROMPTS: dict[str, str] = {
    "pass1": """\
你是转录审校专家。根据音频和上下文，完成以下任务：

1. **识别每个说话人的身份**：姓名和角色
2. **纠正 ASR 的说话人标注错误**：听音频判断，标错的用 `correct_speaker` 修正
3. **拆分混合发言段落**：如果一段音频中包含多个说话人，用 `split` 在切换点拆开，并用 `speaker_after` 标注后半段的说话人

视频标题：{video_title}
视频链接：{video_url}

格式要求：
- 姓名使用中文（如 Warren Buffett → 沃伦·巴菲特）
- 多个未知说话人按编号区分：未知说话人1、未知说话人2
- 保留所有已有的 speaker_id，不要删除
- 只允许输出 `correct_speaker` 和 `split`，不要输出 `fix_text` / `merge`
- 不要输出 gender、age_group、style（由后续音色分析阶段处理）

输出 JSON，且只能输出 JSON：

{{
  "speakers": {{
    "speaker_a": {{"name": "中文姓名", "role": "角色"}},
    "speaker_b": {{"name": "中文姓名", "role": ""}}
  }},
  "corrections": [
    {{"action": "correct_speaker", "index": 12, "to": "speaker_b", "reason": "原因"}},
    {{"action": "split", "index": 2, "at_text": "切换点文本", "speaker_after": "speaker_a", "reason": "原因"}}
  ]
}}

转录稿（{line_count} 行）：

{transcript_body}""",

    "pass2": """\
你正在执行视频转录审校的 Pass 2。Pass 1 已经完成 speaker 识别与 speaker 纠正。
你的唯一目标是：
1. 修正文本文字错误
2. 对过长段落做语义拆分
3. 提取术语表

你不是在做 speaker 重分配，不是在做音色描述，不是在做身份识别。

输入信息：
- 视频标题：{video_title}
- 已校正 speaker 的转录文本：{transcript_body}
- speakers 信息：{speakers_json}

必须遵守的规则：
1. 绝对不要修改任何 speaker_id
2. 绝对不要输出 `correct_speaker`
3. 不要输出 `merge`
4. 只允许输出：
   - `fix_text`
   - `split`
   - `glossary`
5. `fix_text` 只修正明显 ASR 错误、重复、漏词、错词
6. 不要改写语气，不要润色，不要重写内容
7. 不要改变原文核心含义
8. `split` 只用于过长段落（>60s），并且必须在自然语义断点切开
9. 如果某段并不适合拆分，就不要强行拆分
10. glossary 只收录稳定、值得后续翻译统一的专名、机构名、术语、人名

输出 JSON，且只能输出 JSON：

{{
  "corrections": [
    {{
      "action": "fix_text",
      "index": 5,
      "old": "原错误文本",
      "new": "修正后文本",
      "reason": "简短说明"
    }},
    {{
      "action": "split",
      "index": 18,
      "at_text": "建议切分点附近的文本",
      "reason": "该段过长，需要在自然断点拆分"
    }}
  ],
  "glossary": {{
    "Berkshire Hathaway": "伯克希尔·哈撒韦",
    "Greg Abel": "格雷格·艾贝尔"
  }}
}}

转录稿（{line_count} 行）：

{transcript_body}""",

    "pass3": """\
你正在执行视频音色画像分析的 Pass 3。
前两个阶段已经完成 speaker 识别、speaker 纠正、文本修正与术语表提取。
你的唯一目标是：根据每个 speaker 的代表性音频片段，生成适合 TTS 选音匹配的音色画像。

你不是在做 speaker 纠正，不是在做文本修正，不是在做术语表。

输入信息：
- 视频标题：{video_title}
- speaker 基础信息：{speakers_json}
- 当前 speaker 列表：{speaker_ids}
- 每个 speaker 的代表音频片段（单独提供）

必须遵守的规则：
1. 不要输出 corrections
2. 不要输出 glossary
3. 只输出每个 speaker 的音色画像
4. voice_description 要面向 TTS 匹配，描述声音特征，不要写成人物背景介绍
5. style 描述说话风格（如"专业、稳重"、"信息丰富、分析性强"）
6. gender 只能是：male / female / unknown
7. age_group 只能是：young / middle / elderly / unknown
8. persona_style 尽量从以下集合中选最接近者：
   - professional
   - warm
   - serious
   - energetic
   - calm
9. energy_level 只能是：low / medium / high
10. 不确定时可以输出 `unknown`，不要强猜

输出 JSON，且只能输出 JSON：

{{
  "speaker_profiles": {{
    "speaker_a": {{
      "voice_description": "声音清晰、语速中等偏快、音高偏中高，整体专业且稳定",
      "style": "专业、稳重",
      "gender": "female",
      "age_group": "middle",
      "persona_style": "professional",
      "energy_level": "medium"
    }},
    "speaker_b": {{
      "voice_description": "声音偏低沉，语速较慢，带停顿感，整体沉稳",
      "style": "沉稳、分析性强",
      "gender": "male",
      "age_group": "elderly",
      "persona_style": "calm",
      "energy_level": "low"
    }}
  }}
}}""",

    "translate": """\
你是专业的视频配音翻译专家。任务是把英文视频转录稿翻译成自然流畅的中文口播文本。

视频信息：
- 标题：__VIDEO_TITLE__
- 来源：__YOUTUBE_URL__
__GLOSSARY_SECTION__
核心目标是让中文配音时长与原英文段落时长大致一致。
每段都标注了 target_duration_seconds，翻译时请控制中文长度使配音时长接近该目标。
翻译结果将用于配音，要适合人声朗读，不要书面字幕腔。
所有人物姓名必须优先使用中文常见译名。
__SPEAKER_INSTRUCTION____STRICT_LENGTH_INSTRUCTION__
输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {"segment_id": 1, "cn_text": "翻译后的中文文本"}
]""",

    "rewrite": """\
你是专业的中文配音文本改写专家。

任务：对当前文本进行__DIRECTION_DESC__，使其更适合目标配音时长。

当前文本（__CURRENT_CHARS__字）：
__TTS_CN_TEXT__

英文原文（参考，不要直接翻译）：
__SOURCE_TEXT__

目标字数：约__TARGET_CHARS__字
当前需要__DIRECTION_DESC__约__CHANGE_PCT__%

要求：
1. 保持原意不变
2. __DIRECTION_INSTRUCTION__
3. 保持自然口语化，适合视频配音
4. 只输出改写后的中文文本，不要任何解释

改写后的文本：""",

    "probe_translate": """你是专业的视频配音翻译专家。任务是把英文视频转录稿翻译成自然流畅的中文口播文本。

视频信息：
- 标题：__VIDEO_TITLE__
- 来源：__YOUTUBE_URL__
__GLOSSARY_SECTION__
这些翻译将直接用于中文 TTS 配音，核心目标是让中文配音时长与原英文段落时长大致一致。请特别注意：
1. 每段都标注了 target_duration_seconds（原文段落时长），翻译时请自然地控制中文长度，使配音时长接近该目标。
2. 不要机械地按字数公式凑字，而是根据原文的语速节奏、信息密度来判断中文应该翻多长。
3. 宁可适度意译、精简表达，也不要逐字直译导致配音明显超时。
4. 如果原文信息密度高，可用更紧凑的中文表达方式保留核心信息。
5. 翻译结果将用于配音，不要写成书面字幕腔，要适合人声朗读。
6. 所有人物姓名必须优先使用中文常见译名，不要保留英文人名。
   例如：Elon Musk -> 埃隆·马斯克，Sam Altman -> 萨姆·奥特曼，Naval Ravikant -> 纳瓦尔·拉维坎特。
7. 公司、产品、品牌、模型名称若已有常见中文译法，优先使用中文；若没有稳定中文译法，可保留原文。
__SPEAKER_INSTRUCTION__补充要求：在不影响自然度的前提下，可适度保留原文中的口语连接词、语气词和缓冲表达，以维持更接近原说话节奏；但不要为了凑字数生硬添加无意义填充词。
9. 每个 segment 独立翻译，但要保持上下文连贯。
10. 只输出 JSON，不要任何其他文字。

每个 segment 提供了 target_duration_seconds（原文段落时长），请凭语感自然翻译，使配音时长接近该目标。

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "翻译后的中文文本"
  }
]""",

    "content_compliance": """你是中国大陆网络视频内容合规审核员。请基于视频标题、简介和转录稿判断是否存在违法或不良信息风险。
只输出 JSON，字段包括 decision、confidence、reason、categories。

视频信息：
- 标题：__VIDEO_TITLE__
- 简介：__VIDEO_DESCRIPTION__
- 来源类型：__SOURCE_TYPE__
- 来源标识：__SOURCE_REF__

第一层本地规则结果：
__LOCAL_FINDINGS_JSON__

转录稿：
__TRANSCRIPT_BODY__""",
}


def _load_default_prompts() -> dict[str, str]:
    """Return the runtime-authoritative default prompts.

    Runtime prompts live inside the pipeline modules (``translator.py`` for
    translate/rewrite/probe_translate, ``transcript_reviewer.py`` for
    pass1/2/3).  To avoid the "two copies of default drift apart" bug
    (documented in plans/translation-duration-alignment), this function
    imports those modules at call time and overrides the gateway-local
    ``_DEFAULT_PROMPTS`` copy with whatever the runtime is actually using.

    If imports fail (e.g. missing dependencies inside the gateway container),
    fall back to the gateway-local copy so the admin UI stays functional.
    """
    merged = dict(_DEFAULT_PROMPTS)
    try:
        from services.gemini.translator import (  # noqa: WPS433 — lazy import
            DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
            DEFAULT_REWRITE_PROMPT_TEMPLATE,
            PROBE_TRANSLATION_PROMPT_TEMPLATE,
        )
        merged["translate"] = DEFAULT_TRANSLATION_PROMPT_TEMPLATE
        merged["rewrite"] = DEFAULT_REWRITE_PROMPT_TEMPLATE
        merged["probe_translate"] = PROBE_TRANSLATION_PROMPT_TEMPLATE
    except Exception as exc:  # pragma: no cover — import-guard only
        logger.warning(
            "Failed to import runtime translate/rewrite prompts — falling back "
            "to gateway-local copy: %s",
            exc,
        )
    try:
        from services.transcript_reviewer import (  # noqa: WPS433 — lazy import
            _PASS1_PROMPT,
            _PASS2_PROMPT,
            _PASS3_PROMPT,
        )
        merged["pass1"] = _PASS1_PROMPT
        merged["pass2"] = _PASS2_PROMPT
        merged["pass3"] = _PASS3_PROMPT
    except Exception as exc:  # pragma: no cover — import-guard only
        logger.warning(
            "Failed to import runtime pass1/2/3 prompts — falling back to "
            "gateway-local copy: %s",
            exc,
        )
    try:
        from services.content_compliance import (  # noqa: WPS433 — lazy import
            DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT,
        )
        merged["content_compliance"] = DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT
    except Exception as exc:  # pragma: no cover — import-guard only
        logger.warning(
            "Failed to import runtime content compliance prompt — falling back "
            "to gateway-local copy: %s",
            exc,
        )
    return merged


@router.get("/review-prompts")
async def get_review_prompts(
    user: User | None = Depends(get_current_user),
) -> dict:
    """Get current review prompt overrides + defaults + models + keys + version history."""
    _require_admin(user)
    full_data: dict = {}
    if SETTINGS_FILE.exists():
        try:
            full_data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    prompts = full_data.get("review_prompts", {})
    prompt_models = full_data.get("prompt_models", {})
    provider_api_keys = full_data.get("provider_api_keys", {})

    # API key status (env vars)
    api_key_status: dict[str, bool] = {}
    for env in _GEMINI_AUTH_ENVS:
        api_key_status[env] = bool(os.environ.get(env, "").strip())
    for provider, env in _PROVIDER_KEY_ENVS.items():
        api_key_status[env] = bool(os.environ.get(env, "").strip())

    # Gemini auth: check if any of the three cred sources is configured
    gemini_configured = any(api_key_status.get(e, False) for e in _GEMINI_AUTH_ENVS)

    history = _load_prompt_history()
    return {
        "prompts": {k: prompts.get(k, "") for k in _PROMPT_KEYS},
        "defaults": _load_default_prompts(),
        "models": {
            mode: {k: prompt_models.get(mode, {}).get(k, _DEFAULT_MODELS.get(mode, {}).get(k, ""))
                   for k in _PROMPT_KEYS}
            for mode in _MODE_KEYS
        },
        "default_models": _DEFAULT_MODELS,
        "provider_api_keys": {
            provider: _mask_api_key(provider_api_keys.get(provider, ""))
            for provider in _PROVIDER_KEY_ENVS
        },
        "api_key_status": api_key_status,
        "gemini_configured": gemini_configured,
        "available_models": {
            k: _available_models_for_prompt(k) for k in _PROMPT_KEYS
        },
        "all_models": _get_all_models_with_status(),
        "history": history,
    }


@router.post("/review-prompts")
async def update_review_prompts(
    body: dict,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Update review prompt overrides, models, and provider API keys.

    Body::

        {
          "prompts": {"pass1": "...", ...},
          "models": {"studio": {"pass1": "gemini_pro", ...}, "express": {...}},
          "provider_api_keys": {"deepseek": "sk-xxx", ...},
          "label": "版本标签"
        }

    Key write protocol for ``provider_api_keys``:
    - field absent / null → keep current value
    - "" (empty string) → clear override (revert to env var)
    - "****xxxx" (masked) → rejected with 400
    - non-empty string → set as new key
    """
    _require_admin(user)
    incoming_prompts = body.get("prompts", {})
    incoming_models = body.get("models")
    incoming_keys = body.get("provider_api_keys")
    label = body.get("label", "")
    if not isinstance(incoming_prompts, dict):
        raise HTTPException(status_code=400, detail="prompts must be a dict")

    # Reject masked keys
    if isinstance(incoming_keys, dict):
        for provider, val in incoming_keys.items():
            if isinstance(val, str) and _is_masked_key(val):
                raise HTTPException(status_code=400, detail=f"拒绝：provider_api_keys.{provider} 包含脱敏值，请勿回传 '****...' 格式")

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # P0-5 (audit 2026-05-07): same lock + atomic write as save_settings.
    # Two admins clicking "save prompts" simultaneously previously could
    # both load full_data, both append a history entry, and only the
    # last one's prompts persisted (the other's history line referenced
    # prompts that were never actually saved).
    with file_lock(SETTINGS_FILE):
        # Load existing settings
        full_data: dict = {}
        if SETTINGS_FILE.exists():
            try:
                full_data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

        old_prompts = dict(full_data.get("review_prompts", {}))
        old_models = dict(full_data.get("prompt_models", {}))

        # Save current version to history — prompts + models, NOT keys
        has_content = any(old_prompts.get(k) for k in _PROMPT_KEYS) or bool(old_models)
        if has_content:
            from datetime import datetime, timezone
            # P0-5 follow-up (audit 2026-05-07, codex review 2026-05-07):
            # nest the prompt_history lock INSIDE the SETTINGS_FILE lock.
            # Lock order is fixed (settings → history) and the only other
            # caller of file_lock(_PROMPT_HISTORY_FILE) — delete_prompt_history
            # — never touches SETTINGS_FILE, so the two are independent and
            # cannot circular-acquire.
            with file_lock(_PROMPT_HISTORY_FILE):
                history = _load_prompt_history()
                history.append({
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "label": label or f"版本 {len(history) + 1}",
                    "prompts": {k: old_prompts.get(k, "") for k in _PROMPT_KEYS},
                    "models": old_models,
                })
                _save_prompt_history(history)

        # Update prompt overrides
        review_prompts = full_data.get("review_prompts", {})
        if not isinstance(review_prompts, dict):
            review_prompts = {}
        for key in _PROMPT_KEYS:
            val = incoming_prompts.get(key)
            if val is not None:
                if isinstance(val, str) and val.strip():
                    review_prompts[key] = val.strip()
                else:
                    review_prompts.pop(key, None)
        full_data["review_prompts"] = review_prompts

        # Update model selections (with server-side capability validation)
        if isinstance(incoming_models, dict):
            _audio_only_prompts = {"pass1", "pass3"}
            _audio_model_values = {m["value"] for m in _ALL_MODELS if m["supports_audio"]}
            _all_model_values = {m["value"] for m in _ALL_MODELS}
            for mode_key, mode_models in incoming_models.items():
                if not isinstance(mode_models, dict):
                    continue
                for prompt_key, model_val in mode_models.items():
                    if model_val and model_val not in _all_model_values:
                        raise HTTPException(status_code=400, detail=f"未知模型: {model_val}")
                    if prompt_key in _audio_only_prompts and model_val and model_val not in _audio_model_values:
                        raise HTTPException(
                            status_code=400,
                            detail=f"{prompt_key} 需要支持音频的模型，{model_val} 不支持音频输入",
                        )
            full_data["prompt_models"] = incoming_models

        # Update provider API keys (keep/replace/clear protocol)
        if isinstance(incoming_keys, dict):
            current_keys = full_data.get("provider_api_keys", {})
            if not isinstance(current_keys, dict):
                current_keys = {}
            for provider in _PROVIDER_KEY_ENVS:
                val = incoming_keys.get(provider)
                if val is None:
                    continue  # keep current
                if val == "":
                    current_keys.pop(provider, None)  # clear
                else:
                    current_keys[provider] = val  # replace
            full_data["provider_api_keys"] = current_keys

        atomic_write_json(str(SETTINGS_FILE), full_data)
    logger.info("Review prompts/models updated by admin %s", getattr(user, "email", "?"))

    return {
        "prompts": {k: review_prompts.get(k, "") for k in _PROMPT_KEYS},
        "models": full_data.get("prompt_models", {}),
        "history": _load_prompt_history(),
    }


@router.post("/model-toggle")
async def toggle_model(
    body: dict,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Enable or disable a model. Not stored in history.

    Body: ``{"model": "mimo_omni", "enabled": false}``
    """
    _require_admin(user)
    model_name = body.get("model", "")
    enabled = body.get("enabled", True)
    all_model_names = set(_MODEL_REGISTRY.keys())
    if model_name not in all_model_names:
        raise HTTPException(status_code=400, detail=f"未知模型: {model_name}")

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # P0-5 (audit 2026-05-07): same lock + atomic write as save_settings.
    # Toggle is a small payload but two admins toggling adjacent models in
    # parallel previously could lose one toggle (lost update on the
    # disabled_models list).
    with file_lock(SETTINGS_FILE):
        # Load existing settings
        full_data: dict = {}
        if SETTINGS_FILE.exists():
            try:
                full_data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

        disabled: list = full_data.get("disabled_models", [])
        if not isinstance(disabled, list):
            disabled = []

        if enabled:
            disabled = [m for m in disabled if m != model_name]
        else:
            if model_name not in disabled:
                disabled.append(model_name)

        full_data["disabled_models"] = disabled
        atomic_write_json(str(SETTINGS_FILE), full_data)
    _invalidate_llm_cache()
    logger.info("Model %s %s by admin %s", model_name, "enabled" if enabled else "disabled",
                getattr(user, "email", "?"))
    return {"all_models": _get_all_models_with_status()}


@router.post("/review-prompts/restore")
async def restore_review_prompts(
    body: dict,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Restore prompts from a history version by index."""
    _require_admin(user)
    idx = body.get("index")
    if idx is None or not isinstance(idx, int):
        raise HTTPException(status_code=400, detail="index is required (integer)")

    history = _load_prompt_history()
    if idx < 0 or idx >= len(history):
        raise HTTPException(status_code=404, detail=f"版本 {idx} 不存在")

    version = history[idx]
    # Restore prompts + models (if present in history), NOT keys
    restore_body: dict = {
        "prompts": version.get("prompts", {}),
        "label": f"还原: {version.get('label', '')}",
    }
    if "models" in version:
        restore_body["models"] = version["models"]
    return await update_review_prompts(restore_body, user=user)


@router.delete("/review-prompts/history/{index}")
async def delete_prompt_history(
    index: int,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Delete a specific history version."""
    _require_admin(user)
    # P0-5 follow-up (audit 2026-05-07, codex review 2026-05-07): wrap
    # load→pop→save in file_lock(_PROMPT_HISTORY_FILE) so a concurrent
    # update_review_prompts (which appends to history under the same lock)
    # cannot lose the new append.
    with file_lock(_PROMPT_HISTORY_FILE):
        history = _load_prompt_history()
        if index < 0 or index >= len(history):
            raise HTTPException(status_code=404, detail=f"版本 {index} 不存在")
        deleted = history.pop(index)
        _save_prompt_history(history)
    logger.info("Prompt history version %d deleted by admin %s", index, getattr(user, "email", "?"))
    return {"deleted": deleted, "history": history}


# ---------------------------------------------------------------------------
# Job management endpoints
# ---------------------------------------------------------------------------

# Job API upstream URL comes from gateway/config.py (settings.job_api_upstream,
# env var AVT_JOB_API_UPSTREAM). No module-level constant — always read
# app_settings.job_api_upstream at call time so tests can monkeypatch.
JOBS_STORE_DIR = Path("/opt/aivideotrans/data/jobs")


@router.get("/jobs")
async def list_all_jobs(
    user: User | None = Depends(get_current_user),
) -> dict:
    """List ALL jobs across all users (admin only)."""
    _require_admin(user)

    # Fetch jobs from upstream Job API
    async with httpx.AsyncClient(timeout=15, headers=internal_headers()) as client:
        try:
            resp = await client.get(f"{app_settings.job_api_upstream}/jobs")
            resp.raise_for_status()
            data = resp.json()
            upstream_jobs: list[dict] = data.get("jobs", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch jobs from Job API: %s", exc)
            raise HTTPException(status_code=502, detail="无法获取任务列表")

    # Build a lookup of user info from PostgreSQL
    # Phase 2 Task 0 — also surface metering_snapshot so the admin frontend
    # can show per-job catalog hit / rewrite / first-pass error metrics
    # without an extra round-trip per job.
    async with async_session() as db:
        rows = (await db.execute(
            select(Job.job_id, Job.user_id, Job.status, Job.project_dir,
                   Job.metering_snapshot,
                   User.email, User.display_name)
            .outerjoin(User, Job.user_id == User.id)
        )).all()

    db_lookup: dict[str, dict] = {}
    for row in rows:
        db_lookup[row.job_id] = {
            "user_id": str(row.user_id),
            "db_status": row.status,
            "project_dir": row.project_dir,
            "owner_email": row.email,
            "owner_display_name": row.display_name,
            "metering_snapshot": row.metering_snapshot or {},
        }

    # Merge upstream job data with owner info
    result = []
    for job in upstream_jobs:
        jid = job.get("job_id") or job.get("id", "")
        owner = db_lookup.get(jid, {})
        result.append({**job, **owner})

    return {"jobs": result}


def _remove_project_dir_if_safe(project_dir: str | None, *, job_id: str) -> bool:
    """Remove a job project directory only if it is under the project roots."""
    if not project_dir:
        return False

    project_path = Path(project_dir)
    if not project_path.is_dir():
        return False

    if not _is_safe_project_dir(project_path):
        logger.warning(
            "Refusing to remove unsafe project dir for job %s: %s",
            job_id,
            project_path,
        )
        return False

    try:
        shutil.rmtree(project_path)
    except OSError as exc:
        logger.warning(
            "Failed to remove project dir for job %s: %s: %s",
            job_id,
            project_path,
            exc,
        )
        return False

    logger.info("Removed project dir for job %s: %s", job_id, project_path)
    return True


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Cancel a job: stop processing, clean up files, mark cancelled in DB."""
    _require_admin(user)

    async with httpx.AsyncClient(timeout=30, headers=internal_headers()) as client:
        # (a) Cancel via Job API
        try:
            await client.post(f"{app_settings.job_api_upstream}/jobs/{job_id}/cancel")
        except Exception as exc:
            logger.warning("Job API cancel call failed for %s: %s", job_id, exc)

        # (b) Get project_dir from Job API
        project_dir: str | None = None
        try:
            resp = await client.get(f"{app_settings.job_api_upstream}/jobs/{job_id}")
            if resp.status_code == 200:
                project_dir = resp.json().get("project_dir")
        except Exception as exc:
            logger.warning("Failed to fetch job info for %s: %s", job_id, exc)

    # (c) Delete project directory if it exists and passes the whitelist guard.
    _remove_project_dir_if_safe(project_dir, job_id=job_id)

    # (d) Update status in PostgreSQL + release quota
    async with async_session() as db:
        row = (await db.execute(
            select(Job).where(Job.job_id == job_id)
        )).scalar_one_or_none()
        if row:
            row.status = "cancelled"
            # Release reserved quota
            from quota import release_quota
            await release_quota(db, row)
            await db.commit()

    # (e) Delete job JSON file from Job API store
    job_file = JOBS_STORE_DIR / f"{job_id}.json"
    if job_file.exists():
        job_file.unlink(missing_ok=True)
        logger.info("Removed job file: %s", job_file)

    return {"success": True, "job_id": job_id}


@router.post("/jobs/{job_id}/delete")
async def delete_job(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Fully delete a job: cancel + remove PostgreSQL record."""
    _require_admin(user)

    async with httpx.AsyncClient(timeout=30, headers=internal_headers()) as client:
        # Cancel via Job API then delete
        try:
            await client.post(f"{app_settings.job_api_upstream}/jobs/{job_id}/cancel")
        except Exception as exc:
            logger.warning("Job API cancel call failed for %s: %s", job_id, exc)

        try:
            await client.delete(f"{app_settings.job_api_upstream}/jobs/{job_id}")
        except Exception as exc:
            logger.warning("Job API delete call failed for %s: %s", job_id, exc)

        # Get project_dir from Job API
        project_dir: str | None = None
        try:
            resp = await client.get(f"{app_settings.job_api_upstream}/jobs/{job_id}")
            if resp.status_code == 200:
                project_dir = resp.json().get("project_dir")
        except Exception as exc:
            logger.warning("Failed to fetch job info for %s: %s", job_id, exc)

    # Delete project directory if it passes the whitelist guard.
    _remove_project_dir_if_safe(project_dir, job_id=job_id)

    # Delete job JSON file
    job_file = JOBS_STORE_DIR / f"{job_id}.json"
    if job_file.exists():
        job_file.unlink(missing_ok=True)
        logger.info("Removed job file: %s", job_file)

    # Release quota then delete PostgreSQL record
    async with async_session() as db:
        row = (await db.execute(
            select(Job).where(Job.job_id == job_id)
        )).scalar_one_or_none()
        if row:
            from quota import release_quota
            await release_quota(db, row)
        await db.execute(sa_delete(Job).where(Job.job_id == job_id))
        await db.commit()

    return {"success": True, "deleted": True}


# ---------------------------------------------------------------------------
# User management endpoints
# ---------------------------------------------------------------------------

VALID_ROLES = {"user", "admin"}
VALID_PLAN_CODES = {"free", "plus", "pro"}


@router.get("/users")
async def list_users(
    user: User | None = Depends(get_current_user),
) -> dict:
    """List all users with role, plan, quota, and active job count."""
    _require_admin(user)

    async with async_session() as db:
        # Users
        users_result = await db.execute(select(User).order_by(User.created_at.desc()))
        users = users_result.scalars().all()

        # Active job counts per user
        from sqlalchemy import func
        active_counts_result = await db.execute(
            select(Job.user_id, func.count())
            .where(Job.status.in_(["queued", "running", "waiting_for_review"]))
            .group_by(Job.user_id)
        )
        active_counts = {str(row[0]): row[1] for row in active_counts_result.all()}

        # Total job counts per user
        total_counts_result = await db.execute(
            select(Job.user_id, func.count())
            .group_by(Job.user_id)
        )
        total_counts = {str(row[0]): row[1] for row in total_counts_result.all()}

    return {
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "display_name": u.display_name,
                "role": u.role,
                "plan_code": u.plan_code,
                "free_jobs_quota_total": u.free_jobs_quota_total,
                "free_jobs_quota_used": u.free_jobs_quota_used,
                "is_active": u.is_active,
                "active_jobs": active_counts.get(str(u.id), 0),
                "total_jobs": total_counts.get(str(u.id), 0),
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


class UpdateEntitlementsRequest(BaseModel):
    role: str | None = None
    plan_code: str | None = None
    free_jobs_quota_total: int | None = None
    free_jobs_quota_used: int | None = None


@router.patch("/users/{user_id}/entitlements")
async def update_user_entitlements(
    user_id: str,
    body: UpdateEntitlementsRequest,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Update a user's role, plan_code, or quota. Writes audit log."""
    admin = _require_admin(user)

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        target = result.scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        changes = []

        # --- Role change ---
        if body.role is not None and body.role != target.role:
            if body.role not in VALID_ROLES:
                raise HTTPException(status_code=400, detail=f"无效的 role: {body.role}")
            # Guard: prevent demoting the last admin
            if target.role == "admin" and body.role != "admin":
                from sqlalchemy import func as sa_func
                admin_count_result = await db.execute(
                    select(sa_func.count()).where(User.role == "admin")
                )
                admin_count = admin_count_result.scalar() or 0
                if admin_count <= 1:
                    raise HTTPException(
                        status_code=409,
                        detail="无法降级：系统中至少需要保留一个管理员。"
                    )
            old_val = target.role
            target.role = body.role
            changes.append(("update_role", "role", old_val, body.role))

        # --- Plan change ---
        if body.plan_code is not None and body.plan_code != target.plan_code:
            if body.plan_code not in VALID_PLAN_CODES:
                raise HTTPException(status_code=400, detail=f"无效的 plan_code: {body.plan_code}")
            old_val = target.plan_code
            target.plan_code = body.plan_code
            changes.append(("update_plan_code", "plan_code", old_val, body.plan_code))

        # --- Quota adjustments with boundary validation ---
        new_total = body.free_jobs_quota_total if body.free_jobs_quota_total is not None else target.free_jobs_quota_total
        new_used = body.free_jobs_quota_used if body.free_jobs_quota_used is not None else target.free_jobs_quota_used
        if new_total < 0:
            raise HTTPException(status_code=400, detail="free_jobs_quota_total 不能为负数")
        if new_used < 0:
            raise HTTPException(status_code=400, detail="free_jobs_quota_used 不能为负数")
        if new_used > new_total:
            raise HTTPException(
                status_code=400,
                detail=f"free_jobs_quota_used ({new_used}) 不能大于 free_jobs_quota_total ({new_total})"
            )

        if body.free_jobs_quota_total is not None and body.free_jobs_quota_total != target.free_jobs_quota_total:
            old_val = str(target.free_jobs_quota_total)
            target.free_jobs_quota_total = body.free_jobs_quota_total
            changes.append(("adjust_quota", "free_jobs_quota_total", old_val, str(body.free_jobs_quota_total)))

        if body.free_jobs_quota_used is not None and body.free_jobs_quota_used != target.free_jobs_quota_used:
            old_val = str(target.free_jobs_quota_used)
            target.free_jobs_quota_used = body.free_jobs_quota_used
            changes.append(("adjust_quota", "free_jobs_quota_used", old_val, str(body.free_jobs_quota_used)))

        if not changes:
            return {"updated": False, "message": "无变更"}

        # Write audit log entries
        for action, field, old_v, new_v in changes:
            db.add(AdminAuditLog(
                admin_user_id=admin.id,
                target_user_id=target.id,
                action=action,
                field_name=field,
                old_value=old_v,
                new_value=new_v,
            ))

        await db.commit()
        logger.info("Admin %s updated user %s: %s", admin.email, target.email,
                     "; ".join(f"{f}: {o}->{n}" for _, f, o, n in changes))

        return {
            "updated": True,
            "user": {
                "id": str(target.id),
                "email": target.email,
                "role": target.role,
                "plan_code": target.plan_code,
                "free_jobs_quota_total": target.free_jobs_quota_total,
                "free_jobs_quota_used": target.free_jobs_quota_used,
            },
            "changes": [
                {"field": f, "old": o, "new": n}
                for _, f, o, n in changes
            ],
        }


@router.get("/users/{user_id}/audit-log")
async def get_user_audit_log(
    user_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Get audit log entries for a specific user."""
    _require_admin(user)

    async with async_session() as db:
        result = await db.execute(
            select(AdminAuditLog, User.email.label("admin_email"))
            .outerjoin(User, AdminAuditLog.admin_user_id == User.id)
            .where(AdminAuditLog.target_user_id == user_id)
            .order_by(AdminAuditLog.created_at.desc())
            .limit(50)
        )
        rows = result.all()

    return {
        "entries": [
            {
                "id": str(entry.id),
                "admin_email": admin_email,
                "action": entry.action,
                "field_name": entry.field_name,
                "old_value": entry.old_value,
                "new_value": entry.new_value,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            }
            for entry, admin_email in rows
        ]
    }
