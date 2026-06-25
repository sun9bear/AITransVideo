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
from pydantic import BaseModel, StrictBool, field_validator
from sqlalchemy import select, delete as sa_delete

from admin_auth import _require_admin
from auth import get_current_user
from config import settings as app_settings
from csrf import require_same_origin_state_change
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

# Phase 4.1 + Codex 2026-05-25 决策：flash 默认，plus 同时支持
_VALID_CLONE_TARGET_MODELS = frozenset({"cosyvoice-v3.5-flash", "cosyvoice-v3.5-plus"})

# APF 限制旋钮边界（2026-06-11）：{field: (min, max)}。
# 下界 ≥1 防误设 0（等效误关停且难排查——紧急关停用 anonymous_free_preview_enabled
# 主开关）；上界防天文数字。max_upload_mb 上界 2048 对齐登录用户 2GB 上限。
_APF_LIMIT_BOUNDS = {
    "anonymous_preview_max_upload_mb": (10, 2048),
    "anonymous_preview_max_seconds": (30, 7200),
    # 源上传时长上限：下界 180s（=teaser 长度，源短于此则预览=整段，无意义）、
    # 上界 10800s=180min（2026-06-16 项目主：对齐最高自助套餐 Pro=180min）。默认 30min。
    "anonymous_preview_max_source_seconds": (180, 10800),
    "anonymous_preview_cap_global_per_day": (1, 100000),
    "anonymous_preview_cap_per_ip": (1, 1000),
    "anonymous_preview_cap_per_device": (1, 100),
    "anonymous_preview_cap_per_source": (1, 100),
}

# APF 匿名 Express lane 子闸边界（plan 2026-06-12 anonymous-express-preview T0）。
# 刻意不进 _APF_LIMIT_BOUNDS——那张表被 test_anonymous_preview_limits_knobs
# 钉死为 free 6 旋钮契约（dict 整表相等断言），扩表会破坏既有契约。
_ANON_EXPRESS_CAP_BOUNDS = (1, 100000)

# APF per-mode 三维度配额旋钮边界（2026-06-13 项目主裁定，原硬编码常量改旋钮）。
# 同样不进 _APF_LIMIT_BOUNDS（保持那张表的 free 6 旋钮契约不破）。上界 1000：
# 高于此对单 IP/设备/视频已无防滥用意义，且 express 另有 50/日全局子闸兜底。
_ANON_PER_MODE_CAP_BOUNDS = (1, 1000)

# 匿名/快捷 CosyVoice + 智能版 MiniMax 克隆 cap 边界（plan 2026-06-14 §4.1）。
# 同样不进 _APF_LIMIT_BOUNDS（保持那张表的 free 6 旋钮契约不破）。下界 ≥1：
# 紧急关停走克隆主开关（anonymous_express_cosyvoice_clone_enabled /
# smart_preview_clone_enabled），不靠 cap=0（等效误关停且难排查，与
# anonymous_express_daily_global_cap 同哲学）。
_CLONE_CAP_BOUNDS = {
    "anonymous_clone_daily_global_cap": (1, 100000),
    "anonymous_clone_active_cap": (1, 100000),
    "smart_preview_clone_daily_global_cap": (1, 100000),
    "smart_preview_clone_inflight_cap": (1, 10000),
}

# 分片上传旋钮边界（plan 2026-06-11 §3.7）：{field: (min, max)}。
# chunk_mb 上界 80 是硬约束（CF 免费版单请求体 100MB，留余量）；
# 其余下界 ≥1 防误设 0（等效误关停难排查——紧急关停用 chunked_upload_enabled
# 主开关），上界防天文数字（磁盘 / 带宽失控）。
_CHUNKED_UPLOAD_BOUNDS = {
    "chunked_upload_max_file_mb": (100, 8192),
    "chunked_upload_chunk_mb": (8, 80),
    "chunked_upload_per_user_active": (1, 20),
    "chunked_upload_per_user_inflight_gb": (1, 100),
    "chunked_upload_global_inflight_gb": (1, 1000),
    "chunked_upload_daily_per_user_gb": (1, 500),
    "chunked_upload_disk_floor_gb": (1, 500),
    "chunked_upload_ttl_hours": (1, 168),
    "chunked_upload_ready_ttl_hours": (1, 72),
    # 匿名档分片 TTL（plan §9 r1 评审裁定默认 6h）：上界 24 = 不得长于注册档
    # 默认——匿名身份可重置，TTL 是 init-abandon 滥用的资源占用窗口。
    "chunked_upload_anonymous_ttl_hours": (1, 24),
    # 匿名档每日声明配额（2026-06-12 项目主裁定）：声明即计+放弃不退的语义
    # 下收太紧会被失败重试烧完锁死一天；对滥用者本就无效（清 cookie 重置）。
    "chunked_upload_anonymous_daily_gb": (1, 100),
}


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
    # --- Phase 2a free tier (Task 6, gate #6) — MiMo voiceclone kill-switch ---
    # Master toggle for the free tier's MiMo voiceclone TTS path. Default True.
    # When turned OFF (admin emergency switch — e.g. MiMo promo ends or quality
    # regresses), free jobs DEGRADE to the cheapest preset engine (CosyVoice
    # preset_mapping) and the free tier KEEPS RUNNING (credits still 0). Plain
    # bool (not StrictBool) on purpose: the dangerous direction is the OPPOSITE of
    # the clone-GA flags — turning this OFF only downgrades to a free preset and
    # never opens a paid API. MiMo voiceclone itself is the free promotional path
    # (inline reference, no MiniMax/CosyVoice paid clone).
    free_tier_voiceclone_enabled: bool = True
    # Runtime rollout switches for visible task-plan cards. Smart reuses
    # smart_mode_enabled below because it already is the global kill switch.
    service_mode_express_enabled: StrictBool = True
    service_mode_free_enabled: StrictBool = False
    service_mode_studio_enabled: StrictBool = True
    enable_pre_tts_rewrite: bool = True            # Pre-TTS rewrite to match target duration
    express_tts_provider: str = "cosyvoice"        # Default TTS provider for express mode
    studio_tts_provider: str = "minimax"           # Default TTS provider for studio mode
    cosyvoice_runtime_endpoint_mode: str = "international"  # CosyVoice runtime: "international" or "mainland"
    cosyvoice_offline_endpoint_mode: str = "mainland"       # CosyVoice offline: "international" or "mainland"
    translation_char_range_min_factor: float = 0.85         # min_chars = target_chars * this
    translation_char_range_max_factor: float = 1.15         # max_chars = target_chars * this
    voice_clone_cost_credits: int = 600  # DEPRECATED: migrated to pricing_runtime. Kept for compat (plan 2026-06-14 §4.2: 500→600).
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
    # --- PR-E matchable migration — language-aware voice catalog query (kill switch) ---
    # When True, the internal voice-catalog query additionally filters by
    # compatible_target_languages @> [target_language], so a zh dub never returns en
    # voices (and vice versa). Default OFF → legacy matchable-only query
    # (byte-identical). Turn ON only after migration 042 backfilled
    # compatible_target_languages AND the "zh target returns 0 en" assertion passes;
    # turning OFF instantly reverts to the legacy query (the kill switch). See
    # plan 2026-06-13 ...-v3.md Phase 5 (B).
    voice_catalog_target_language_filter_enabled: bool = False
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
    # --- Smart Auto Pipeline kill switch — Layer 2 (P2 launch blocker #1) ---
    # Admin runtime toggle for the smart kill switch. False (default) means
    # smart is removed from every user's allowed_service_modes even if the
    # env-level Settings.enable_smart_mode is True. Both layers must be
    # True for smart to appear. Hot-reloadable via admin UI mtime poll —
    # use this as the emergency-stop switch (no gateway restart needed).
    # Spec: docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3 +
    #       docs/plans/2026-05-24-smart-auto-pipeline-rebaseline.md §3.1
    smart_mode_enabled: bool = False
    # --- Phase 3 (plan 2026-05-17-user-voice-candidate-first §后台策略字段) ---
    # Smart MVP candidate-first voice policy. These 3 toggles control how
    # Smart treats personal-voice candidates and new clones independently.
    # Plan §Consent × Admin 决策矩阵 documents the 8-row truth table.
    #
    # Defaults preserve existing Smart behavior:
    #   smart_auto_clone_enabled=True    — Smart may auto-clone when
    #     consent allows. Set False to disable new clone for ALL users
    #     (e.g. when MiniMax account is low on credits).
    #   smart_reuse_user_voice_enabled=True — Smart queries personal-
    #     voice candidates before clone. Set False to skip reuse path
    #     entirely (e.g. for debugging or per-user library lockdown).
    #   smart_pause_on_possible_user_voice_match=False — weak/medium
    #     matches do NOT pause smart pipeline (Phase 4 enforces this;
    #     Phase 3 only reserves the field). Default off so existing
    #     Smart users don't get surprise pauses.
    #   smart_auto_reuse_on_possible_user_voice_match=True — Phase 5
    #     (2026-05-24 P5 data analysis follow-up): when speaker has
    #     non-strong personal voice candidates, auto-promote the top
    #     candidate to REUSED instead of pausing. NO paid API call
    #     (uses existing user_voice). Wins over pause flag when both
    #     are True. Default True so the 3 production handoffs observed
    #     in 90-day analytics stop reoccurring.
    #
    # Plan §核心不变量: smart_auto_clone_enabled=False only blocks new
    # clones; strong-match reuse still fires when reuse is enabled.
    smart_auto_clone_enabled: bool = True
    smart_reuse_user_voice_enabled: bool = True
    smart_pause_on_possible_user_voice_match: bool = False
    smart_auto_reuse_on_possible_user_voice_match: bool = True
    # --- Phase 1b report-driven rollout flags (2026-05-24) ---
    # These fields are written by /api/admin/smart-analytics/phase1b-flags.
    # Missing keys fall back to env vars in app-side runtime_flags.py; explicit
    # bools here override env so ops can change report/shadow rollout without a
    # container rebuild. Behavior flags remain OFF by default.
    phase1b_translation_script_gate_shadow: bool = False
    phase1b_voice_sample_scoring_shadow: bool = False
    phase1b_translation_script_gate_enabled: bool = False
    phase1b_voice_sample_scoring_enabled: bool = False
    phase1b_audio_tail_trim_enabled: bool = False
    phase1b_whisper_quality_gate_enabled: bool = False

    # --- Phase 4.1 CosyVoice clone (Codex 2026-05-25 决策落地) ---
    # 与 GatewaySettings.mainland_voice_worker_* 字段不同：这里的 6 项是
    # **业务策略**（功能开关 / 默认模型 / allowlist / GA 灰度），可由 admin
    # 后台修改并持久化到 admin_settings.json；secret / worker URL 仍在 env。
    #
    # 授权规则（Phase 4.2 A.2c review 收紧）：
    #   authorized =
    #       is_admin(user)
    #       OR (user.id in cosyvoice_clone_user_allowlist)         # beta 灰度
    #       OR cosyvoice_clone_general_availability_enabled        # 全用户 GA
    cosyvoice_clone_worker_enabled: bool = False
    cosyvoice_clone_default_target_model: str = "cosyvoice-v3.5-flash"
    cosyvoice_clone_user_allowlist: list[str] = []      # user_id 字符串数组（beta）
    # Phase 4.2 A.2c：全用户 GA 灰度开关。**默认 False** —— deploy 后保持
    # admin-only，直到 admin 在 admin 后台手动翻 True。
    # 这是**唯一**安全边界（plan v4-followup §8.1 + Codex 2026-05-26
    # PR #11 wrap-up）；前端展示层只是 UX 便利，不是 gate。
    # 翻 True 后 endpoint Layer 1 ``_check_authorized`` 对**任何**已登录用户
    # 放行（仍保留 401 拒未登录、worker_enabled / quota / ownership 等其它层）。
    #
    # **类型用 ``StrictBool``**（Codex 2026-05-27 PR #12 review P1）：
    # 普通 ``bool`` 在 Pydantic 下宽松解析 —— ``"1"`` / ``"on"`` / ``"yes"``
    # / ``"true"`` 等字符串都会被转 ``True``。Admin UI / JSON marshalling
    # 任何 bug 把"1"传到这里都会**意外打开 GA → 全用户付费 API**。
    # ``StrictBool`` 只接受 Python ``True`` / ``False``，所有 string / int
    # 输入都 raise ValidationError，从 schema 层把这个攻击向量关掉。
    cosyvoice_clone_general_availability_enabled: StrictBool = False
    cosyvoice_clone_max_voices_per_user: int = 3        # 灰度期严控（C.2 已生效）
    # ⚠️ Phase 4.2 占位字段 —— 尚未实现 ⚠️
    # Codex 2026-05-25 C.2 二轮 review 部署前项 #B：此字段定义了"全局
    # 并发上限"，但 endpoint 当前未读取。要做并发 gate 需要 Redis / DB
    # counter（``cosyvoice_clone_in_progress`` table 或 atomic increment），
    # 不在 Phase 4.1 范围内。
    #
    # 当前行为：
    #   - 字段存在 admin_settings.json（schema 不破坏向后兼容）
    #   - admin UI 可见、可改，但 **改了不会有任何运行时效果**
    #   - 实际灰度并发由 ``cosyvoice_clone_max_voices_per_user`` 间接限制：
    #     每用户最多 N 个 active 音色 → 单用户最多同时 N 次 clone-in-progress
    #
    # Phase 4.2 真正实现时需要：
    #   1. ``cosyvoice_clone_in_progress`` 计数（Redis INCR / DB SELECT FOR UPDATE）
    #   2. endpoint 在 Layer 7 后再加一道全局并发 gate
    #   3. clone 完成 / 失败时 decrement
    cosyvoice_clone_max_concurrent_jobs: int = 2        # Phase 4.2 占位（未生效）

    # --- Phase 4.3a Express CosyVoice 自动 clone canary（2026-05-28） ---
    # spec docs/plans/2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md
    #
    # 9 个字段为 Express 快捷版自动 clone 提供：admin 主开关 + 灰度
    # allowlist + 主说话人筛选阈值 + 样本长度 cap + 模型名 + 成本闸
    # （per-user daily / active-temp）+ reservation TTL（PR2-A 加）。
    #
    # 与 Phase 4.2 ``cosyvoice_clone_*`` 字段平行存在：那 6 个字段服务
    # Studio 手动 clone（用户在选音色页点"克隆音色"按钮），这里 9 个字段
    # 服务 Express pipeline 内部自动触发的 canary 路径。两套策略独立可调。
    #
    # 授权层（spec §2 Layer 顺序）：
    #   Layer 1: ``express_cosyvoice_auto_clone_enabled``（admin 主开关）
    #   Layer 2: worker env 就绪（``is_worker_enabled_in_env()``）—— pipeline 层判断
    #   Layer 3: 用户在 ``express_cosyvoice_auto_clone_user_allowlist`` OR admin
    #   Layer 4: 用户 consent ``auto_voice_clone is True``（C 阶段已落地）
    #   Layer 5/6/7（业务参数 + 成本闸）：本节字段配合 pipeline 实现
    #
    # **类型用 ``StrictBool``**（与 cosyvoice_clone_general_availability_enabled 同模式）：
    # 普通 ``bool`` Pydantic 宽松解析下 ``"1"`` / ``"on"`` / ``"true"`` 都会
    # 变 True，导致 admin UI bug 把字符串传到这里**意外打开 Express 自动 clone
    # → 全部 allowlist 用户付费 API**。``StrictBool`` 只接受 Python True/False。
    express_cosyvoice_auto_clone_enabled: StrictBool = False
    express_cosyvoice_auto_clone_allowlist_enabled: StrictBool = True

    # Beta 灰度白名单：``user_id`` 字符串数组（UUID 字符串形态）。
    # admin 自动 bypass（不需进 allowlist）。空数组（默认） + enabled=True
    # = 只有 admin 能触发，与 enabled=False 在普通用户视角等效（双保险）。
    express_cosyvoice_auto_clone_user_allowlist: list[str] = []

    # 主说话人筛选阈值（pipeline `identify_express_main_speaker` 使用）：
    # 占比 < min_ratio 或 lines < min_lines → 不触发 clone，走预设音色。
    # 默认 30% / 5 行（spec §4.2，覆盖独白 / 1对1 / 多人会议场景）。
    # admin 可在 canary 期间根据 audit JSONL reason_code 分布调整。
    express_cosyvoice_auto_clone_main_speaker_min_ratio: float = 0.30
    express_cosyvoice_auto_clone_main_speaker_min_lines: int = 5

    # 样本最大长度 cap（秒）：spec §4.3 CosyVoice v3.5-flash 推荐 10-20s
    # prompt；超过 20s 收益递减且 OSS PUT 流量浪费。
    express_cosyvoice_auto_clone_sample_max_seconds: float = 20.0

    # 目标 model 硬编码（Phase 4.3a 范围）：spec §1.1 G1 + admin UI 提示
    # 文字 "Phase 4.3a 固定，不可改"；Phase 4.3 全量时再开下拉。
    express_cosyvoice_auto_clone_target_model: str = "cosyvoice-v3.5-flash"

    # --- 成本闸（spec §2.5 / Codex 二轮 P1-2 fix）---
    # 临时音色不计入 ``cosyvoice_clone_max_voices_per_user``（长期库配额）
    # 因 ``is_temporary=true`` 在 D1 阶段 user_voice_service 隔离矩阵。
    # 但完全不计任何配额 = canary 用户可无限刷付费 API。本节两 cap 单独
    # 防 Phase 4.3a 灰度期间成本失控。
    #
    # daily_cap：当日内（UTC）express_auto created clone 行数上限。
    # 查询过滤 ``provider='cosyvoice_voice_clone' AND created_from='express_auto'``，
    # **不**过滤 ``expired_at`` / ``is_temporary``——"曾经发生过即算"。
    # 默认 5（spec §2.5；canary Stage 1 临时调到 1 看 audit 再放）。
    express_cosyvoice_auto_clone_per_user_daily_cap: int = 5

    # active_temp_cap：当前 user 持有的 ``is_temporary=true AND expired_at IS NULL``
    # 临时音色数量上限。防多任务并发把临时表撑爆。默认 3（spec §2.5）。
    express_cosyvoice_auto_clone_per_user_active_temp_cap: int = 3

    # Phase 4.3a PR2 §3：reservation TTL（分钟）。reserve 时应用层填
    # expires_at = now + 这个值。覆盖单次 clone 全程（upload+worker < 1 分钟）
    # + 崩溃回收冗余。validator 5-120（防误设 0 = 立即过期永远 reserve 不到 /
    # 误设 7 天 = 崩溃的 reserved 名额长期占 cap）。
    express_cosyvoice_auto_clone_reservation_ttl_minutes: int = 30

    # --- APF 匿名 Free 预览 (plan 2026-06-10 T1) ---
    # 运行时可调开关（admin 热翻，不重启 gateway）。
    # 与 GatewaySettings.enable_anonymous_preview 的关系：
    #   两者必须同时为 True 才放行匿名预览（双门）。
    #   env flag 是部署级开关，此字段是运行时熔断开关。
    #
    # **类型用 StrictBool**（同 cosyvoice_clone_general_availability_enabled）：
    # 普通 bool 下 "1" / "on" / "true" 字符串会被 Pydantic 宽松解析为 True，
    # admin UI bug 或 JSON marshalling 问题可能意外开启匿名预览面。
    # StrictBool 只接受 Python True / False。
    anonymous_free_preview_enabled: StrictBool = False

    # 非终态匿名预览任务的最大并发数（AD-8 in-flight gate）。
    # ≥ 此值 → 429（匿名不饿死付费任务）。admin 可在灰度期调大。
    # 默认 2：500/天 ÷ 2 并发槽位，灰度初期保守。
    anonymous_preview_max_in_flight: int = 2

    # --- APF 限制旋钮（2026-06-11，原 env-only 限制搬进 admin 热配置）---
    # 6 项与 GatewaySettings 同名 env 字段一一对应（默认值严格一致）；
    # admin 改完即时生效（gateway 每请求经 anonymous_preview_limits.
    # resolve_apf_limits() 重读），无需重启容器。env 字段保留作 fail-safe
    # fallback：admin_settings.json 读取/解析任何异常 → resolver 回落 env 值。
    #
    # ⚠️ 单位差异：``anonymous_preview_max_upload_mb`` 存 **MB**（admin UI
    # 友好），消费侧由 resolver 统一 ×1024×1024 转字节；env 对应字段是
    # ``anonymous_preview_max_upload_bytes``（字节）。其余 5 项单位同 env。
    anonymous_preview_max_upload_mb: int = 200
    anonymous_preview_max_seconds: int = 180
    # 匿名**源视频上传**时长上限（秒）——与预览长度（上行）**解耦**（2026-06-16）。
    # 默认 30min；上界 180min（_APF_LIMIT_BOUNDS）。源可比预览长，预览仍恒 3min teaser。
    anonymous_preview_max_source_seconds: int = 1800
    anonymous_preview_cap_global_per_day: int = 500
    anonymous_preview_cap_per_ip: int = 3
    anonymous_preview_cap_per_device: int = 1
    anonymous_preview_cap_per_source: int = 1

    # --- APF per-mode 三维度配额旋钮（2026-06-13 项目主裁定）---
    # 原为硬编码常量 PER_SCOPE_PER_MODE_DAILY_CAP=1（T2），现改 admin 旋钮，
    # 可热调。语义：在既有 legacy per-scope cap 之上，对 {free,express} 每个
    # lane 各自再限 ip/device/source 每日次数。判定顺序：总闸 → express 子闸
    # → legacy per-scope → 本 per-mode 层（任一拒即拒）。
    #
    # 默认全 1 = 保持 T2 上线后的现行为（不静默改 prod 行为）。调参指引：
    #   - per_ip_per_mode 是免费档"同 IP 每日次数"的实际绑定闸——调高即放宽
    #     免费试用（如设 3 ≈ 恢复 T2 前的 1/cookie+3/IP 体验）。注意它同时
    #     作用于 express（贵 lane），express 另有 anonymous_express_daily_global_cap
    #     50/日全局子闸兜底成本。
    #   - per_device / per_source 默认 1 防同 cookie / 同视频刷量。
    # 消费侧：resolve_per_mode_caps()（wiring）每请求重读，热生效。
    anonymous_preview_cap_per_ip_per_mode: int = 1
    anonymous_preview_cap_per_device_per_mode: int = 1
    anonymous_preview_cap_per_source_per_mode: int = 1

    # --- APF 匿名 Express 预览 lane（plan 2026-06-12 anonymous-express-preview T0）---
    # express lane 主开关：lane resolver express 优先于 free。开启后匿名预览
    # 走真实快捷版管线（Pass 1-3 + CosyVoice TTS），单次成本远高于 free lane。
    #
    # **类型用 StrictBool**（同 anonymous_free_preview_enabled）：宽松 bool 下
    # "1" / "on" / "true" 字符串会被解析为 True，admin UI bug 可能意外打开
    # 匿名 express 面（Gemini + CosyVoice 真实成本）。
    #
    # 与 express_tts_provider="mimo" 互斥：保存时 422
    # （validate_anonymous_express_tts_exclusion，POST /settings 调用）；
    # 手改 admin_settings.json 绕过保存校验的情形由 runtime lane resolver
    # 防御纵深兜底（解析到 express 时若 provider=mimo → 拒绝 express lane）。
    anonymous_express_enabled: StrictBool = False
    # express lane 每日全局子闸：独立于 anonymous_preview_cap_global_per_day
    # 总闸（500，跨 lane）。判定顺序：总闸 → 本子闸 → per-scope per-mode。
    # 默认 50 控制灰度期日成本敞口。
    anonymous_express_daily_global_cap: int = 50

    # --- 分片上传（plan 2026-06-11 §3.7，独立命名空间 chunked_upload_*）---
    # 注册用户大文件（>95MB）经 CF Tunnel 的应用层分片上传通道。
    # 主开关默认 False：部署后休眠，admin 账号灰度验证再打开。
    #
    # **类型用 StrictBool**（同 anonymous_free_preview_enabled 先例）：
    # 普通 bool 下 "1" / "on" / "true" 字符串会被 Pydantic 宽松解析为 True，
    # admin UI bug 可能意外打开大文件上传面。StrictBool 只接受 Python
    # True / False。
    #
    # 消费侧：gateway chunked_upload_api.resolve_chunked_limits() 每请求
    # 重读（热生效）；读取任何异常 → 整通道 fail-closed（enabled=False）。
    chunked_upload_enabled: StrictBool = False
    chunked_upload_max_file_mb: int = 2048      # 单文件上限（MB）
    chunked_upload_chunk_mb: int = 64           # 建议切片大小（MB，硬上限 80）
    chunked_upload_per_user_active: int = 2     # per-user 并发活跃 upload 数
    chunked_upload_per_user_inflight_gb: int = 4   # per-user in-flight 声明字节
    chunked_upload_global_inflight_gb: int = 20    # 全局 in-flight 声明字节
    chunked_upload_daily_per_user_gb: int = 8      # 每日 per-user 声明 GB 配额
    chunked_upload_disk_floor_gb: int = 20         # 磁盘保底（reserve 公式扣除项）
    chunked_upload_ttl_hours: int = 24             # 未完成上传清扫 TTL
    chunked_upload_ready_ttl_hours: int = 6        # ready 未被 job claim 的终文件 TTL

    # --- 匿名档分片扩展（plan §9 r1，2026-06-12）---
    # 独立熔断：与注册档 chunked_upload_enabled 互不影响；三与门之一
    # （env enable_anonymous_preview AND anonymous_free_preview_enabled AND
    # 本开关）。StrictBool 理由同上。默认 False 休眠上线，项目主自行灰度。
    chunked_upload_anonymous_enabled: StrictBool = False
    # 匿名未 complete 分片 TTL（小时）。注册档维持 chunked_upload_ttl_hours。
    chunked_upload_anonymous_ttl_hours: int = 6
    # 匿名档 per-session 每日声明配额（GB/天）。弱约束（session 可重置），
    # 主要防单会话 init-spam；真正滥用锚点是 per-IP in-flight gate。
    chunked_upload_anonymous_daily_gb: int = 5

    # --- 多语言互翻 language pairs（plan 2026-06-13 v3 PR-A part 2 / §5 Phase 1）---
    # 非默认 language pair（首发 zh-CN->en）的运行时灰度闸。默认 pair en->zh-CN
    # **恒可用**（零回归），不受这些开关影响——判定见
    # entitlements.get_effective_allowed_language_pairs。
    #
    # 授权规则（镜像 express_cosyvoice_auto_clone_* 三件套）：
    #   非默认 pair 可用 = language_pairs_enabled（主开关）
    #     AND (language_pairs_user_allowlist_enabled=False → 所有登录用户
    #          OR user 是 admin
    #          OR user.id ∈ language_pairs_allowlist)
    #
    # **类型用 StrictBool**（同 cosyvoice_clone_general_availability_enabled）：
    # 宽松 bool 下 "1"/"on"/"true" 字符串会被 Pydantic 解析为 True，admin UI
    # bug 可能意外开启非默认 pair（付费 LLM/TTS 真实成本）。StrictBool 只接受
    # Python True/False。空 allowlist + enabled=True + allowlist_enabled=True
    # = 仅 admin 可用（与 enabled=False 在普通用户视角等效，双保险）。
    language_pairs_enabled: StrictBool = False
    language_pairs_user_allowlist_enabled: StrictBool = True
    language_pairs_allowlist: list[str] = []  # user_id 字符串数组（beta 灰度）

    # --- 匿名/快捷版 CosyVoice 免费克隆 (plan 2026-06-14 §3.4/§4.1) ---
    # 让免费/快捷预览走真实克隆流程：匿名 express 经 maybe_run_express_auto_clone
    # 走 CosyVoice 国内 v3.5 临时克隆（注册零费用、合成是管线既有步骤），失败
    # 回 CosyVoice 预设。**绝不** MiniMax（扣账户余额）。
    #
    # 授权层（plan §3.4，匿名版镜像 express_cosyvoice_auto_clone_* 三件套，
    # 但 L3 不用 user allowlist——匿名无 user role——改用全局 fail-closed cap）：
    #   L1' anonymous_express_cosyvoice_clone_enabled（本主开关，默认 False）
    #   L2  worker env 就绪（is_worker_enabled_in_env，pipeline 判断）
    #   L3' 全局 cap：anonymous_clone_daily_global_cap + anonymous_clone_active_cap
    #   L4  consent auto_voice_clone is True（express_consent 经 create 注入）
    #
    # **类型用 StrictBool**（同 express_cosyvoice_auto_clone_enabled）：宽松 bool
    # 下 "1"/"on"/"true" 会被解析为 True，admin UI bug 可能意外打开匿名真克隆。
    anonymous_express_cosyvoice_clone_enabled: StrictBool = False
    # 匿名克隆每日全局上限（fail-closed）：sentinel user 作全局 owner，故 cap 是
    # 全局语义（非 per-anonymous）。计数存储不可用时拒绝克隆回预设。默认 100。
    anonymous_clone_daily_global_cap: int = 100
    # 匿名活跃临时克隆上限（当前 is_temporary=true AND expired_at IS NULL）。
    # 防并发把临时音色表撑爆。sentinel 全局 owner → 全局语义。默认 20。
    anonymous_clone_active_cap: int = 20

    # --- 智能版 MiniMax 克隆预览 (plan 2026-06-14 §5) ---
    # 登录智能版预览：用户显式 consent + 预扣 600 点（知情付费路径，符合
    # CLAUDE.md「✅ 用户显式触发」例外）。克隆成功入个人音色库（source=
    # smart_preview）；失败/激活失败退点 + 清理 voice_id。默认 OFF。
    #
    # ⚠️ **占位旋钮，对应"smart 预览 lane"特性 P3 故意延后（2026-06-14，CodeX
    # 最终复核校正）**：本旋钮 + 下方两 cap 是**未来 smart 3 分钟预览 lane**（含
    # 预扣 600 + 退还 + 库容门）落地后的 gate 占位，**当前不 gate 任何运行时路径**。
    #
    # **重要——勿误判 smart 克隆现状**：`process.py` 的 `build_smart_clone_provider()`
    # （真 MiniMax-capable provider）在 **smart 全量任务** 路径**已接线**（process.py
    # ~4468-4472，条件=`smart_auto_clone_enabled` 默认 True + 用户 `smart_consent.
    # auto_voice_clone` opt-in + 有 main speaker + quota 可用 时调用；条件不满足才
    # 回 `_build_b2_not_wired_clone_provider` stub → PRESET）。即既有 smart 全量
    # auto-clone **能**调真 MiniMax 克隆——这是 **pre-existing、用户经 smart_consent
    # 显式 opt-in、CLAUDE.md「✅ 用户显式触发」合规**的路径，**本任务未改动它**。
    # **与 ``smart_auto_clone_enabled`` 的关系**：那是控制既有 smart 全量 auto-clone
    # 的真开关（默认 True）；要停既有 smart 克隆须用它，**不是**本 `smart_preview_
    # clone_enabled`。两者作用于**不同流**、**不互相 AND**（避免误关既有 smart 行为）。
    # 延后理由 + "智能版扣600点克隆已由既有路径交付"说明见 plan §5。
    smart_preview_clone_enabled: StrictBool = False
    # Smart 预览克隆每日全局上限 + 并发上限（fail-closed）。默认 200 / 5。
    smart_preview_clone_daily_global_cap: int = 200
    smart_preview_clone_inflight_cap: int = 5

    # --- 智能版克隆 reservation 收紧 rollout 闸 (plan 2026-06-14 §2/P3e) ---
    # **pipeline 侧**读的 rollout 开关（区别于上方 create 侧的 smart_preview_clone_
    # enabled）。控制 plan §2「所有 smart MiniMax 克隆必须 reservation-gated」收紧：
    #   - False（默认）→ 既有 smart auto-clone 行为**完全不变**（consent + admin +
    #     speaker 满足即可克隆，不要求 reservation）→ 零回归、既有 smart 测试全绿。
    #   - True → `process.py` 选 provider 时**额外**要求 JobRecord 带有效
    #     `smart_clone_reservation_id`（create 端预扣 600 时 stamp）；无 reservation
    #     → 一律不接真 MiniMax provider，只 preset / reuse。**顺带封死现在 full
    #     smart 无 reservation 调付费 provider 的漏收**（plan §2）。
    # ⚠️ 翻 True **必须**在 create endpoint（预扣 + stamp marker）+ 预览 lane 落地
    # **之后**——否则既有 smart 用户的 auto-clone 会因无 reservation 全部退预设。
    # 与本项目所有 P3 钱-核心一致：默认 OFF、inert、由项目主显式翻开激活。
    smart_clone_requires_reservation: StrictBool = False

    # --- 匿名预览 → 登录认领 (plan 2026-06-15-anonymous-preview-claim-binding) ---
    # 登录用户凭 avt_anon HttpOnly cookie 把匿名预览 record/session 绑定到账户
    # （Model A 元数据桥，不改 jobs.user_id / 不触结算 / 不触发 clone）。
    # **默认 OFF（plan v3.1 #4）**：认领会延长媒体保留（CLAIM_RETENTION 7d）+ 新增
    # 一个认证写端点（POST /gateway/anonymous-preview/claim），故默认关、admin 灰度
    # 确认后再开。flag OFF 时端点存在但返 200 {claimed:false} no-op（inert）。
    # **类型用 StrictBool**：宽松 bool 下 "1"/"on"/"true" 会被解析为 True，admin UI
    # marshalling bug 可能意外打开。纯 bool 旗无需 field_validator（同两个匿名主开关）。
    anonymous_preview_claim_enabled: StrictBool = False

    @field_validator(
        "anonymous_preview_max_upload_mb",
        "anonymous_preview_max_seconds",
        "anonymous_preview_max_source_seconds",
        "anonymous_preview_cap_global_per_day",
        "anonymous_preview_cap_per_ip",
        "anonymous_preview_cap_per_device",
        "anonymous_preview_cap_per_source",
    )
    @classmethod
    def validate_apf_limit_bounds(cls, v: int, info) -> int:
        """APF 限制旋钮统一边界校验（同 reservation_ttl 5-120 validator 模式）。

        下界防误设 0/负数（max_upload=0 → 所有上传 413；cap=0 → 全量 429，
        等效误关停但比主开关难排查）；上界防天文数字（带宽/磁盘/成本失控）。
        各字段边界见模块级 ``_APF_LIMIT_BOUNDS``。
        """
        low, high = _APF_LIMIT_BOUNDS[info.field_name]
        if not (low <= int(v) <= high):
            raise ValueError(
                f"{info.field_name} 必须在 [{low}, {high}]，收到 {v!r}"
            )
        return int(v)

    @field_validator("anonymous_express_daily_global_cap")
    @classmethod
    def validate_anonymous_express_cap_bounds(cls, v: int) -> int:
        """express 子闸边界（plan 2026-06-12 T0）：[1, 100000]。

        下界 ≥1 防误设 0（等效误关停且难排查——紧急关停用
        anonymous_express_enabled 主开关）；上界防天文数字（成本敞口失控）。
        """
        low, high = _ANON_EXPRESS_CAP_BOUNDS
        if not (low <= int(v) <= high):
            raise ValueError(
                f"anonymous_express_daily_global_cap 必须在 [{low}, {high}]，收到 {v!r}"
            )
        return int(v)

    @field_validator(
        "anonymous_clone_daily_global_cap",
        "anonymous_clone_active_cap",
        "smart_preview_clone_daily_global_cap",
        "smart_preview_clone_inflight_cap",
    )
    @classmethod
    def validate_clone_cap_bounds(cls, v: int, info) -> int:
        """克隆 cap 统一边界校验（plan 2026-06-14 §4.1）。

        下界 ≥1 防误设 0（cap=0 等效误关停且难排查——紧急关停用克隆主开关
        anonymous_express_cosyvoice_clone_enabled / smart_preview_clone_enabled）；
        上界防天文数字（成本敞口失控）。各字段边界见 ``_CLONE_CAP_BOUNDS``。
        """
        low, high = _CLONE_CAP_BOUNDS[info.field_name]
        if not (low <= int(v) <= high):
            raise ValueError(
                f"{info.field_name} 必须在 [{low}, {high}]，收到 {v!r}"
            )
        return int(v)

    @field_validator(
        "anonymous_preview_cap_per_ip_per_mode",
        "anonymous_preview_cap_per_device_per_mode",
        "anonymous_preview_cap_per_source_per_mode",
    )
    @classmethod
    def validate_anonymous_per_mode_cap_bounds(cls, v: int, info) -> int:
        """per-mode 三维度旋钮边界（2026-06-13）：[1, 1000]。

        下界 ≥1 防误设 0（cap=0 → count>=0 恒真 → 该维度拒死所有 intake，
        等效误关停且极难排查——要关 lane 用主开关）；上界 1000 见
        ``_ANON_PER_MODE_CAP_BOUNDS`` 注释。
        """
        low, high = _ANON_PER_MODE_CAP_BOUNDS
        if not (low <= int(v) <= high):
            raise ValueError(
                f"{info.field_name} 必须在 [{low}, {high}]，收到 {v!r}"
            )
        return int(v)

    @field_validator(
        "chunked_upload_max_file_mb",
        "chunked_upload_chunk_mb",
        "chunked_upload_per_user_active",
        "chunked_upload_per_user_inflight_gb",
        "chunked_upload_global_inflight_gb",
        "chunked_upload_daily_per_user_gb",
        "chunked_upload_disk_floor_gb",
        "chunked_upload_ttl_hours",
        "chunked_upload_ready_ttl_hours",
        "chunked_upload_anonymous_ttl_hours",
        "chunked_upload_anonymous_daily_gb",
    )
    @classmethod
    def validate_chunked_upload_bounds(cls, v: int, info) -> int:
        """分片上传旋钮统一边界校验（plan 2026-06-11 §3.7）。

        chunk_mb ≤ 80 是 CF 免费版单请求体 100MB 的硬性余量约束；其余
        边界见模块级 ``_CHUNKED_UPLOAD_BOUNDS``。
        """
        low, high = _CHUNKED_UPLOAD_BOUNDS[info.field_name]
        if not (low <= int(v) <= high):
            raise ValueError(
                f"{info.field_name} 必须在 [{low}, {high}]，收到 {v!r}"
            )
        return int(v)

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

    @field_validator("express_cosyvoice_auto_clone_target_model")
    @classmethod
    def validate_express_auto_clone_target_model(cls, v: str) -> str:
        """Phase 4.3a：仅允许 cosyvoice-v3.5-flash / plus（同 Phase 4.1 白名单）。

        Phase 4.3a 在 spec §1.1 G1 硬编码 flash，但 admin_settings 字段允许
        将来 Phase 4.3 全量时打开 plus；validator 复用 Phase 4.1 白名单
        避免接受任意字符串绕过 worker 端的 model 验证。
        """
        normalized = v.strip()
        if normalized not in _VALID_CLONE_TARGET_MODELS:
            raise ValueError(
                f"express_cosyvoice_auto_clone_target_model 必须是 "
                f"{sorted(_VALID_CLONE_TARGET_MODELS)} 之一，收到 {v!r}"
            )
        return normalized

    @field_validator("express_cosyvoice_auto_clone_main_speaker_min_ratio")
    @classmethod
    def validate_express_auto_clone_min_ratio(cls, v: float) -> float:
        """Phase 4.3a §4.2：[0.10, 1.0] 区间。

        < 0.10 = 噪音 speaker 也可能触发（spec 反向不健康）；
        > 1.0 = 数学不可能。
        """
        if not (0.10 <= float(v) <= 1.0):
            raise ValueError(
                f"express_cosyvoice_auto_clone_main_speaker_min_ratio 必须在 "
                f"[0.10, 1.0]，收到 {v!r}"
            )
        return float(v)

    @field_validator("express_cosyvoice_auto_clone_main_speaker_min_lines")
    @classmethod
    def validate_express_auto_clone_min_lines(cls, v: int) -> int:
        """Phase 4.3a §4.2：[1, 100] 行。"""
        if not (1 <= int(v) <= 100):
            raise ValueError(
                f"express_cosyvoice_auto_clone_main_speaker_min_lines 必须在 "
                f"[1, 100]，收到 {v!r}"
            )
        return int(v)

    @field_validator("express_cosyvoice_auto_clone_sample_max_seconds")
    @classmethod
    def validate_express_auto_clone_sample_max_seconds(cls, v: float) -> float:
        """Phase 4.3a §4.3：[10.0, 60.0] 秒，与 sample_uploader 的硬上限一致。"""
        if not (10.0 <= float(v) <= 60.0):
            raise ValueError(
                f"express_cosyvoice_auto_clone_sample_max_seconds 必须在 "
                f"[10.0, 60.0]，收到 {v!r}"
            )
        return float(v)

    @field_validator("express_cosyvoice_auto_clone_per_user_daily_cap")
    @classmethod
    def validate_express_auto_clone_daily_cap(cls, v: int) -> int:
        """Phase 4.3a §2.5：[0, 1000]。0 = 全用户硬禁（紧急降级）；
        admin 可在 canary 期间临时把 cap 调到 0 关停 daily clone 流量。
        """
        if not (0 <= int(v) <= 1000):
            raise ValueError(
                f"express_cosyvoice_auto_clone_per_user_daily_cap 必须在 "
                f"[0, 1000]，收到 {v!r}"
            )
        return int(v)

    @field_validator("express_cosyvoice_auto_clone_per_user_active_temp_cap")
    @classmethod
    def validate_express_auto_clone_active_temp_cap(cls, v: int) -> int:
        """Phase 4.3a §2.5：[0, 100]。"""
        if not (0 <= int(v) <= 100):
            raise ValueError(
                f"express_cosyvoice_auto_clone_per_user_active_temp_cap 必须在 "
                f"[0, 100]，收到 {v!r}"
            )
        return int(v)

    @field_validator("express_cosyvoice_auto_clone_reservation_ttl_minutes")
    @classmethod
    def validate_express_auto_clone_reservation_ttl(cls, v: int) -> int:
        """Phase 4.3a PR2 §3：[5, 120] 分钟。

        下界 5 防误设 0（reservation 立即过期 → 永远 reserve 不到名额）；
        上界 120 防误设 7 天（崩溃的 reserved 名额长期占 cap，用户被锁死）。
        """
        if not (5 <= int(v) <= 120):
            raise ValueError(
                f"express_cosyvoice_auto_clone_reservation_ttl_minutes 必须在 "
                f"[5, 120]，收到 {v!r}"
            )
        return int(v)

    @field_validator("cosyvoice_clone_default_target_model")
    @classmethod
    def validate_clone_default_target_model(cls, v: str) -> str:
        """Phase 4.1 + Codex 2026-05-25 决策：flash / plus 之一。"""
        normalized = v.strip()
        if normalized not in _VALID_CLONE_TARGET_MODELS:
            raise ValueError(
                f"cosyvoice_clone_default_target_model 必须是 "
                f"{sorted(_VALID_CLONE_TARGET_MODELS)} 之一，收到 {v!r}"
            )
        return normalized


# --- Helpers ---

def validate_anonymous_express_tts_exclusion(s: AdminSettings) -> None:
    """MiMo 组合硬拒（plan 2026-06-12 §E 双层之一：admin 保存校验 422）。

    ``anonymous_express_enabled=True`` ⇄ ``express_tts_provider="mimo"``
    互斥。POST /settings 是 full-body 语义，本检查看终态——无论本次保存
    翻的是哪个字段，组合命中即拒，天然覆盖双向。

    背景：MiMo 海外端点恒定 mia 音色（gender 不参与选音），匿名 express
    用它必然音色错配，违背"免费触点必须体验真实管线效果"最高指导原则
    （US prod 实证 job_0d71b65d594e410d9716a77507619d45 全员女声错配）。

    手改 admin_settings.json 绕过本校验的情形由 runtime lane resolver
    防御纵深兜底（plan §E②）。
    """
    if s.anonymous_express_enabled and s.express_tts_provider.strip().lower() == "mimo":
        raise HTTPException(
            status_code=422,
            detail=(
                "匿名 Express 预览与 MiMo TTS 互斥（MiMo 恒定单音色会导致"
                "预览音色错配）：请先切换 express TTS provider（如 cosyvoice）"
                "再开启匿名 Express lane。"
            ),
        )


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


@router.post("/settings", dependencies=[Depends(require_same_origin_state_change)])
async def update_admin_settings(
    body: AdminSettings,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Update admin settings — FULL BODY SEMANTICS.

    The request body is parsed as a complete ``AdminSettings`` Pydantic
    model. Any field absent from the request body is populated with the
    Pydantic default, then ``save_settings`` merges all fields (defaults
    included) into ``admin_settings.json``. As a result, a stale admin
    form / API caller that doesn't know about newly-added fields will
    silently RESET those fields to their Pydantic defaults.

    Operational implication: Phase 3 (plan 2026-05-17-user-voice-
    candidate-first §后台策略字段) added three smart-voice toggles —
    ``smart_auto_clone_enabled`` / ``smart_reuse_user_voice_enabled`` /
    ``smart_pause_on_possible_user_voice_match``. Any admin tool that
    POSTs a partial body without these fields WILL reset them. Until a
    PATCH endpoint or admin UI sync lands, admin operators MUST send
    the full ``AdminSettings`` shape including new fields.

    See plan ``docs/plans/2026-05-17-user-voice-candidate-first-plan.md``
    §Phase 3 finding P2 for the rationale and ``tests/
    test_smart_user_voice_quota_endpoint.py::
    test_post_settings_with_missing_phase3_fields_resets_them_to_defaults``
    for the contract-lock regression.
    """
    _require_admin(user)
    # plan 2026-06-12 T0：匿名 express lane 与 MiMo provider 互斥（双向 422，
    # 命中即拒、不落盘）。详见 validate_anonymous_express_tts_exclusion docstring。
    validate_anonymous_express_tts_exclusion(body)
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
_MODE_KEYS = ("studio", "express", "smart")

# ---------------------------------------------------------------------------
# Model metadata — single source of truth from llm_registry
# ---------------------------------------------------------------------------
from services.llm_registry import (  # noqa: E402
    MODEL_REGISTRY as _MODEL_REGISTRY,
    _MODE_DEFAULTS as _REGISTRY_MODE_DEFAULTS,
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
    # Smart mode defaults — single source of truth lives in
    # ``services.llm_registry._MODE_DEFAULTS["smart"]``. UI dropdown
    # default pre-selection MUST match what the runtime resolver picks
    # (``get_prompt_model("smart", key)``) so admins don't see one model
    # in the UI and another in the pipeline log. Codex 第四十一轮
    # (2026-05-16) — all stages default to ``gemini_pro`` per user
    # request "默认都用 Gemini 3.1 Pro".
    "smart": dict(_REGISTRY_MODE_DEFAULTS.get("smart", {})),
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


@router.post("/review-prompts", dependencies=[Depends(require_same_origin_state_change)])
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


@router.post("/model-toggle", dependencies=[Depends(require_same_origin_state_change)])
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


@router.post("/review-prompts/restore", dependencies=[Depends(require_same_origin_state_change)])
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


@router.delete(
    "/review-prompts/history/{index}",
    dependencies=[Depends(require_same_origin_state_change)],
)
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


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_same_origin_state_change)])
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


@router.post("/jobs/{job_id}/delete", dependencies=[Depends(require_same_origin_state_change)])
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


@router.patch(
    "/users/{user_id}/entitlements",
    dependencies=[Depends(require_same_origin_state_change)],
)
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
