from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import statistics
import time
from typing import Any

from services.assemblyai.transcriber import TranscriptLine
from services.language_registry import (
    DEFAULT_LANGUAGE_PAIR_PROFILE,
    get_language_descriptor,
    resolve_language_pair,
)
from services.llm import LLMProviderError, LLMRouter
from services.llm_registry import (
    MODEL_REGISTRY as _MODEL_REGISTRY,
    get_api_key as _get_model_api_key,
    get_prompt_model as _get_prompt_model,
    resolve_model_id as _resolve_model_id,
    get_fallback_candidates as _get_fallback_candidates,
    normalize_openai_usage as _normalize_openai_usage_shared,
)
from utils.coerce import (
    coerce_optional_int as _coerce_optional_int,
    normalize_optional_text as _normalize_optional_text,
)
from utils.json_helpers import write_json as _write_json

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_MODEL_NAME = "gemini-3.1-pro-preview"
DEFAULT_SDK_BACKEND = "google-genai"
LEGACY_SDK_BACKEND = "google-generativeai"
DEFAULT_TEMPERATURE = 0.3
# Gemini accepts maxOutputTokens in the half-open range [1, 65536).
# Keep the 64K intent, but never send the exclusive upper bound.
GEMINI_MAX_OUTPUT_TOKENS_EXCLUSIVE_UPPER_BOUND = 65536
DEFAULT_MAX_OUTPUT_TOKENS = GEMINI_MAX_OUTPUT_TOKENS_EXCLUSIVE_UPPER_BOUND - 1
DEFAULT_BATCH_SIZE = 15
PARALLEL_WORKERS = 3
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 2
DEFAULT_PRE_SPLIT_MAX_LINE_DURATION_MS = 60_000
DEFAULT_PRE_SPLIT_MAX_LINE_CHARS = 2_000
DEFAULT_MIN_SUBLINE_DURATION_MS = 1_500
DEFAULT_MAX_SEGMENT_DURATION_MS = 45_000  # 不超过 45 秒/段（与转录拆分阈值一致）
DEFAULT_SAME_SPEAKER_PAUSE_SPLIT_MS = 1_500  # 同 speaker 停顿 ≥1.5s 也分段（保留转录拆分）
DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND = 4.5
TRANSLATION_CHECKPOINT_VERSION = 1
DEFAULT_SPEAKER_REFERENCE_MIN_SAMPLES = 3
DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK = 1
SPEAKER_INFER_PROMPT_TEMPLATE_CONTEXT_TOKEN = "__CONTEXT_EXCERPT__"
SPEAKER_INFER_PROMPT_TEMPLATE_EXPECTED_OUTPUT_TOKEN = "__EXPECTED_OUTPUT_JSON__"
TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN = "__GROUPS_JSON__"
TRANSLATION_PROMPT_TEMPLATE_VIDEO_TITLE_TOKEN = "__VIDEO_TITLE__"
TRANSLATION_PROMPT_TEMPLATE_YOUTUBE_URL_TOKEN = "__YOUTUBE_URL__"
TRANSLATION_PROMPT_TEMPLATE_SPEAKER_INSTRUCTION_TOKEN = "__SPEAKER_INSTRUCTION__"
TRANSLATION_PROMPT_TEMPLATE_STRICT_LENGTH_TOKEN = "__STRICT_LENGTH_INSTRUCTION__"
TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN = "__GLOSSARY_SECTION__"
PROBE_TRANSLATION_PROMPT_TEMPLATE = """你是专业的视频配音翻译专家。任务是把英文视频转录稿翻译成自然流畅的中文口播文本。

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
]"""

# zh-CN -> en probe-translation variant (by-feel, no min/max char constraints).
# Same token contract as PROBE_TRANSLATION_PROMPT_TEMPLATE; output stays cn_text
# (canonical container, holds English here). lang_pair_marker: zh-CN->en
_PROBE_TRANSLATION_PROMPT_TEMPLATE_ZH_EN = """You are a professional video dubbing translator. Translate the Chinese video transcript into natural, fluent English voice-over text.

视频信息：
- 标题：__VIDEO_TITLE__
- 来源：__YOUTUBE_URL__
__GLOSSARY_SECTION__
These translations feed an English TTS dub; the goal is for the English dub duration to roughly match the original Chinese duration. Note:
1. Each segment has target_duration_seconds (original duration); translate so the spoken English length naturally approaches it.
2. Judge the English length from the source pace and information density, not a character formula.
3. Prefer concise, idiomatic English over literal translation that overruns.
4. Translate Chinese names to their common English forms; keep already-English names.
5. The result is for voice-over: natural, spoken English.
__SPEAKER_INSTRUCTION__6. Keep natural spoken fillers when present to preserve the original rhythm.
9. Translate each segment independently but keep coherence.
10. Output JSON only, no other text.

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "translated English text"
  }
]"""

#: Probe-translation template per language pair. Default en->zh-CN resolves via
#: get_effective_probe_translation_prompt_template (override-or-PROBE, byte-identical).
_PROBE_TEMPLATE_BY_PAIR: dict[tuple[str, str], str] = {
    ("zh-CN", "en"): _PROBE_TRANSLATION_PROMPT_TEMPLATE_ZH_EN,
}

REWRITE_PROMPT_TEMPLATE_TEXT_TOKEN = "__TTS_CN_TEXT__"
REWRITE_PROMPT_TEMPLATE_SOURCE_TEXT_TOKEN = "__SOURCE_TEXT__"
REWRITE_PROMPT_TEMPLATE_DIRECTION_TOKEN = "__DIRECTION_DESC__"
REWRITE_PROMPT_TEMPLATE_DIRECTION_INSTRUCTION_TOKEN = "__DIRECTION_INSTRUCTION__"
REWRITE_PROMPT_TEMPLATE_CURRENT_CHARS_TOKEN = "__CURRENT_CHARS__"
REWRITE_PROMPT_TEMPLATE_TARGET_CHARS_TOKEN = "__TARGET_CHARS__"
REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_CHARS_TOKEN = "__TARGET_LOWER_CHARS__"
REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_CHARS_TOKEN = "__TARGET_UPPER_CHARS__"
REWRITE_PROMPT_TEMPLATE_TARGET_LOWER_RATIO_PCT_TOKEN = "__TARGET_LOWER_RATIO_PCT__"
REWRITE_PROMPT_TEMPLATE_TARGET_UPPER_RATIO_PCT_TOKEN = "__TARGET_UPPER_RATIO_PCT__"
REWRITE_PROMPT_TEMPLATE_CHANGE_PCT_TOKEN = "__CHANGE_PCT__"
DEFAULT_SPEAKER_INFER_PROMPT_TEMPLATE = """以下是一段英文访谈视频转录稿的前几分钟内容。请根据上下文推断每个说话人的身份。

转录内容：
__CONTEXT_EXCERPT__

请只输出JSON，格式如下：
__EXPECTED_OUTPUT_JSON__

如果无法确定真实姓名，用描述性角色（如"主持人"、"嘉宾"）代替。"""
DEFAULT_TRANSLATION_PROMPT_TEMPLATE = """你是专业的视频配音翻译专家。任务是把英文视频转录稿翻译成自然流畅的中文口播文本。

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
__SPEAKER_INSTRUCTION____STRICT_LENGTH_INSTRUCTION__8. **保留口语化节奏（重要，影响 TTS 时长匹配）**：当原文有较多口头禅、语气词、思考停顿、重复表达时（典型如 "well..." / "you know" / "I mean" / "I would say" / "uh, um" / "kind of" / "sort of" / "you see" / 同义反复），中文译文应**适度保留**这些口语化元素，对应翻译为：
   - 思考类 → "嗯"、"呃"、"我想说"、"这个嘛"
   - 缓冲类 → "你知道的"、"我是说"、"怎么讲呢"、"应该说"
   - 强调类 → "可以说"、"应该这么说"、"我是这么觉得的"
   - 同义反复 → 适当保留"换句话说……"或者用近义词复述

   **判断原则**：当 source_words_per_second 偏低（< 2.0 词/秒）或 target_duration_seconds 较长但原文信息量不大时，说明原说话人节奏慢、用了大量口语缓冲。这种情况下精简翻译会让 TTS 时长**远短于**目标，反而需要事后强制拉伸，破坏听感。**宁可让中文带点"啰嗦感"贴近原节奏，也不要为了"流畅"过度精简。**
9. 每个 segment 独立翻译，但要保持上下文连贯。
10. 只输出 JSON，不要任何其他文字。

每个 segment 都提供了以下元数据（按约束强度排序）：

**硬约束**（请严格遵守，超出会导致译文与原文长度严重失配）：
- target_duration_seconds：原文段落时长（秒）
- target_chars：本段建议的中文字数 = 原文英文词数 × 1.8（中英自然翻译比）。这是「保留原文信息密度的自然中文长度」。
- min_chars ~ max_chars：target_chars 的 ±15% 浮动区间。译文字数请落在此区间内。

**参考信息**（帮助你判断详略策略，不直接约束字数）：
- source_words_per_second：原说话人的英文语速（词/秒）。值大表示信息密集；值小表示节奏宽松、口语词多。
- voice_chars_per_second：选定中文音色的合成语速（字/秒）。**仅供参考**：当 voice_chars_per_second × target_duration_seconds 与 target_chars 接近时，TTS 合成时长会自然贴近目标；差距大时由后续阶段调整音色 speed 或拉伸对齐，无需你为此修改字数。
- target_chars_hint：与 target_chars 同值（保留以兼容旧 prompt 自定义）。

如何使用这些字段：
- 原文信息密度高（source_words_per_second 偏高、或内容含大量数字/术语/专有名词）：译文字数可接近 max_chars，优先保留信息。
- 原文节奏宽松（source_words_per_second 偏低、口语化连词多）：译文字数可接近 min_chars，但**仍要保留所有口头禅和语气词**（参见上面第 8 条），让译文自然铺满 target_duration。
- 信息密度正常：按 target_chars 落笔，落在 min/max 区间内即可。

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "翻译后的中文文本"
  }
]"""

# zh-CN -> en translation prompt. Mirrors DEFAULT_TRANSLATION_PROMPT_TEMPLATE's
# token contract (__VIDEO_TITLE__ / __YOUTUBE_URL__ / __GLOSSARY_SECTION__ /
# __SPEAKER_INSTRUCTION__ / __STRICT_LENGTH_INSTRUCTION__ / __GROUPS_JSON__) so
# _build_prompt works unchanged, but translates Chinese -> English dubbing text.
# The output field stays ``cn_text`` (the canonical target-text container, plan
# v3 §4.3 — here it holds English). Per-segment length budget values
# (target_chars/min_chars/max_chars) come from the groups JSON (set by the
# language-pair length profile, PR-CD slice 3). lang_pair_marker: zh-CN->en
_TRANSLATION_PROMPT_TEMPLATE_ZH_EN = """You are a professional video dubbing translator. Translate the Chinese video transcript into natural, fluent English voice-over text.

视频信息：
- 标题：__VIDEO_TITLE__
- 来源：__YOUTUBE_URL__
__GLOSSARY_SECTION__
These translations feed an English TTS dub; the core goal is for the English dub duration to roughly match the original Chinese segment duration. Note:
1. Each segment is tagged with target_duration_seconds (original duration); translate so the spoken English length naturally approaches it.
2. Do not pad to hit a character count mechanically — judge the English length from the source pace and information density.
3. Prefer concise, idiomatic English over literal word-for-word translation that overruns the duration.
4. For dense source content, use compact English that preserves the core information.
5. The result is for voice-over: write spoken, natural English, not written-subtitle style.
6. Translate Chinese personal names to their common English forms (e.g. 埃隆·马斯克 -> Elon Musk, 萨姆·奥特曼 -> Sam Altman); keep already-English names as-is.
7. For companies / products / models, use the established English name.
__SPEAKER_INSTRUCTION____STRICT_LENGTH_INSTRUCTION__8. Preserve spoken rhythm (matters for TTS duration matching): when the source has fillers, hesitations or repetition, keep natural English equivalents ("well", "you know", "I mean", "I'd say", "uh", "kind of") rather than stripping them — over-tightening makes the dub far shorter than target and forces stretching later.
9. Translate each segment independently but keep cross-segment coherence.
10. Output JSON only, no other text.

Per-segment metadata (by constraint strength). IMPORTANT: for this English voice-over the *_chars fields are ENGLISH WORD counts (spoken units), NOT characters — keep your output within the WORD band:
**Hard constraints**: target_duration_seconds; target_chars (suggested target WORD count for this segment); min_chars ~ max_chars (the ±15% band, in ENGLISH WORDS — keep the translation within it).
**Reference**: source_words_per_second (source pace); voice_chars_per_second (chosen voice synthesis speed, reference only); target_chars_hint (== target_chars, in words).

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "translated English text"
  }
]"""

#: Translation prompt template per language pair. Default (en->zh-CN) is the exact
#: legacy DEFAULT_TRANSLATION_PROMPT_TEMPLATE; zh-CN->en translates to English.
_TRANSLATION_TEMPLATE_BY_PAIR: dict[tuple[str, str], str] = {
    ("en", "zh-CN"): DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
    ("zh-CN", "en"): _TRANSLATION_PROMPT_TEMPLATE_ZH_EN,
}

DEFAULT_REWRITE_PROMPT_TEMPLATE = """你是专业的中文配音文本改写专家。

任务：对当前文本进行__DIRECTION_DESC__，使其更适合目标配音时长。

当前文本（__CURRENT_CHARS__字）：
__TTS_CN_TEXT__

英文原文（参考，不要直接翻译）：
__SOURCE_TEXT__

目标字数：约__TARGET_CHARS__字（建议控制在__TARGET_LOWER_CHARS__~__TARGET_UPPER_CHARS__字之间）
目标时长区间：尽量落在目标时长的__TARGET_LOWER_RATIO_PCT__%~__TARGET_UPPER_RATIO_PCT__%
当前需要__DIRECTION_DESC__约__CHANGE_PCT__%

要求：
1. 保持原意不变，不要增删核心信息
2. __DIRECTION_INSTRUCTION__
3. 保持自然口语化，适合视频配音
4. 参考英文原文确保核心信息不被删减
5. 扩充时可从英文原文中找回已被省略但仍重要的细节
6. 本任务是调整当前中文文本长度，不是重新翻译，不要偏离当前中文文本的核心语义
7. 只输出改写后的中文文本，不要任何解释

改写后的文本："""

# English-target rewrite variant (zh-CN->en). Same token contract as
# DEFAULT_REWRITE_PROMPT_TEMPLATE; rewrites the English dub text for duration fit.
# The "字" (Chinese-char) wording becomes spoken length / words. lang_pair_marker: zh-CN->en
_REWRITE_PROMPT_TEMPLATE_ZH_EN = """You are a professional video-dubbing rewrite editor for English voice-over.

Task: __DIRECTION_DESC__ the current text so it better fits the target dub duration.

Current text (__CURRENT_CHARS__ units):
__TTS_CN_TEXT__

Source transcript (reference, do NOT re-translate):
__SOURCE_TEXT__

Target length: about __TARGET_CHARS__ units (aim for __TARGET_LOWER_CHARS__~__TARGET_UPPER_CHARS__).
Target duration band: aim for __TARGET_LOWER_RATIO_PCT__%~__TARGET_UPPER_RATIO_PCT__% of the target duration.
You currently need to __DIRECTION_DESC__ by about __CHANGE_PCT__%.

Rules:
1. Keep the meaning unchanged; do not add or drop core information.
2. __DIRECTION_INSTRUCTION__
3. Keep it natural and spoken, suitable for voice-over.
4. Use the source transcript to make sure no core information is lost.
5. When expanding, you may recover important details that were omitted.
6. This adjusts the length of the current English text, not a re-translation — stay close to its core meaning.
7. Output only the rewritten English text, no explanation.

Rewritten text:"""

#: Rewrite prompt template per TARGET language (the dub text being rewritten).
#: Default zh-CN → the exact legacy Chinese template (byte-identical).
_REWRITE_TEMPLATE_BY_TARGET: dict[str, str] = {
    "zh-CN": DEFAULT_REWRITE_PROMPT_TEMPLATE,
    "en": _REWRITE_PROMPT_TEMPLATE_ZH_EN,
}

_STRONG_LINE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
_WEAK_LINE_SPLIT_PATTERN = re.compile(r"(?<=[,;])\s+")


class TranslationError(Exception):
    pass


# ---------------------------------------------------------------------------
# LLM error classification (plan 2026-05-03 §B6).
#
# Pure heuristic over the message text. Used as a *fallback* when structural
# context (provider_response_received) doesn't already pin down the class.
# Returns (error_class, error_code).
# ---------------------------------------------------------------------------

LLM_ERROR_CLASS_PROVIDER = "provider_error"
LLM_ERROR_CLASS_INVALID_OUTPUT = "invalid_output"
LLM_ERROR_CLASS_AUTH = "auth_error"
LLM_ERROR_CLASS_LENGTH = "length_constraint_failed"
LLM_ERROR_CLASS_CONFIG = "configuration_error"
LLM_ERROR_CLASS_UNKNOWN = "unknown_error"


def classify_llm_error(exc: Exception) -> tuple[str, str]:
    """Return (error_class, error_code) for an LLM exception.

    Note: callers that already know the structural context (e.g. validator
    raised AFTER provider returned text) should override the class to
    ``invalid_output`` rather than relying on this heuristic.
    """
    msg = str(exc).lower()
    if "api key" in msg or "api_key" in msg or "credential" in msg or "unauthorized" in msg:
        return (LLM_ERROR_CLASS_AUTH, "auth_invalid")
    if "unknown model" in msg or "no models available" in msg or "no llm route" in msg:
        return (LLM_ERROR_CLASS_CONFIG, "configuration_missing")
    if "invalid json" in msg or "json parse" in msg or "must be a json" in msg:
        return (LLM_ERROR_CLASS_INVALID_OUTPUT, "json_parse_failed")
    if "rate limit" in msg or "quota" in msg or "429" in msg:
        return (LLM_ERROR_CLASS_PROVIDER, "rate_limit")
    if "timeout" in msg or "timed out" in msg or "connection" in msg or "ssl" in msg:
        return (LLM_ERROR_CLASS_PROVIDER, "transient_network")
    if "char" in msg and ("min" in msg or "max" in msg or "exceed" in msg):
        return (LLM_ERROR_CLASS_LENGTH, "length_constraint")
    if isinstance(exc, TranslationError):
        return (LLM_ERROR_CLASS_INVALID_OUTPUT, "translation_error")
    return (LLM_ERROR_CLASS_UNKNOWN, "unknown")


DUBBING_MODE_DUB = "dub"
DUBBING_MODE_KEEP_ORIGINAL = "keep_original"
VALID_DUBBING_MODES = {DUBBING_MODE_DUB, DUBBING_MODE_KEEP_ORIGINAL}


def normalize_dubbing_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_DUBBING_MODES:
        return normalized
    return DUBBING_MODE_DUB


def is_keep_original_dubbing_mode(value: object) -> bool:
    return normalize_dubbing_mode(value) == DUBBING_MODE_KEEP_ORIGINAL


@dataclass(slots=True)
class DubbingSegment:
    segment_id: int
    speaker_id: str
    display_name: str
    voice_id: str
    start_ms: int
    end_ms: int
    target_duration_ms: int
    source_text: str
    cn_text: str
    # Dub target language (PR-E slice 6). None → zh-CN (the language-aware TTS hooks
    # in tts_generator / fallback all treat None and "zh-CN" identically → byte-identical
    # default). Populated by the pipeline from its resolved language profile.
    target_language: str | None = None
    tts_audio_path: str | None = None
    aligned_audio_path: str | None = None
    actual_duration_ms: int = 0
    alignment_ratio: float = 0.0
    alignment_method: str = ""
    rewrite_count: int = 0
    needs_review: bool = False
    voice_description: str = ""
    gender: str = ""           # "male" / "female" — from transcript reviewer
    age_group: str = ""        # "young" / "middle" / "elderly" — from transcript reviewer
    persona_style: str = ""    # "professional" / "warm" / "serious" / "energetic"
    energy_level: str = ""     # "low" / "medium" / "high"
    selected_voice: str = ""   # actual voice used for TTS (populated after TTS generation)
    match_confidence: str = "" # "high" / "medium" / "low" (populated after TTS generation)
    tts_provider: str = ""     # per-speaker TTS provider override (set by voice_selection_review)
    tts_model_key: str = ""    # actual TTS model/resource key used for speed profiling
    # Phase 2 Task 0 metrics — translation-duration-alignment
    catalog_hit: bool = False           # speaker's voice_id was found in voice_catalog with chars_per_second
    first_pass_duration_ms: int = 0     # raw TTS output duration BEFORE any rewrite/DSP (snapshot at S5 entry)
    first_pass_cn_text: str = ""        # text used for that first-pass TTS, before any post-TTS rewrite
    # 2026-05-04 P0a — exact text that was fed to TTS for the CURRENT
    # ``aligned_audio_path``. Re-stamped on EVERY successful TTS synthesis
    # (initial pipeline, pre-TTS rewrite, post-TTS rewrite, single-segment
    # regen-tts on draft accept, batch regen-all-dirty). NEVER mutated by
    # user text edits — that's the whole point: ``cn_text != tts_input_cn_text``
    # is how downstream cue generation detects "subtitle text changed but
    # audio is still old" drift and falls back to safe proportional layout
    # instead of producing timestamps from mismatched audio.
    tts_input_cn_text: str = ""
    first_pass_error_pct: float = 0.0   # (first_pass_duration_ms - target_duration_ms) / target_duration_ms (sign preserved)
    dsp_speed_param: float = 1.0        # Task 1 will populate; 1.0 means "no speed adjustment / not yet implemented"
    # CodeX P2-3: target Chinese chars/sec for the speaker, derived from
    # source_words_per_second × 1.8.  Phase 2 voice match consumes this on
    # the runtime auto-match path (tts_generator.py per-segment matching);
    # 0.0 disables the speed dimension.
    target_chars_per_second: float = 0.0
    # T7: when non-None, the primary TTS provider failed and this fallback
    # provider produced the audio instead (e.g. "cosyvoice" when MiniMax
    # exhausted). Mirrors TTSResult.fallback_used_provider and ends up in
    # the segment manifest so users can audit voice substitutions.
    fallback_used_provider: str | None = None
    # P0 observability: pre-TTS rewrite audit fields. These do not affect
    # alignment behavior; they make duration-gate contradictions measurable.
    pre_tts_rewrite_direction: str = ""
    pre_tts_estimate_ms: int = 0
    pre_tts_target_ms: int = 0
    pre_tts_pre_chars: int = 0
    pre_tts_post_chars: int = 0
    pre_tts_post_tts_first_pass_ms: int = 0
    pre_tts_contradiction: bool = False
    pre_tts_harmful_contradiction: bool = False
    pre_tts_rewrite_task: str = ""
    pre_tts_rewrite_retry_attempted: bool = False
    pre_tts_rewrite_retry_accepted: bool = False
    pre_tts_rewrite_initial_rejected_reason: str = ""
    pre_tts_rewrite_rejected: bool = False
    pre_tts_rewrite_rejected_reason: str = ""
    pre_tts_rewrite_rejected_direction: str = ""
    pre_tts_rewrite_rejected_estimate_ms: int = 0
    pre_tts_rewrite_rejected_target_ms: int = 0
    pre_tts_rewrite_rejected_pre_chars: int = 0
    pre_tts_rewrite_rejected_post_chars: int = 0
    pre_tts_rewrite_rejected_lower_chars: int = 0
    pre_tts_rewrite_rejected_upper_chars: int = 0
    # P1-i/P1-j: deterministic short-segment handling audit fields.
    force_dsp_severity: str = ""              # low / medium / high for final force_dsp
    force_dsp_review_suppressed: bool = False # true when a short backchannel is auto-denoised
    force_dsp_review_reason: str = ""
    # P1-m: main alignment DSP fit audit. Underflow caps slow down short audio
    # and pad the remaining slot with silence instead of extreme slow-mo.
    dsp_speed_ratio_used: float = 1.0
    dsp_silence_padded_ms: int = 0
    dsp_truncated_ms: int = 0
    dsp_initial_duration_ms: int = 0
    dsp_trimmed_duration_ms: int = 0
    dsp_stretched_duration_ms: int = 0
    short_merge_candidate: bool = False       # safe same-speaker merge candidate
    short_merge_target_segment_id: int = 0
    short_merge_reason: str = ""
    short_merge_blocked_reason: str = ""
    short_merge_applied: bool = False
    short_merge_absorbed_segment_ids: str = "" # comma-separated original segment ids
    # P2 speaker-structure audit fields. These are deterministic metadata from
    # transcript durations; they do not change speaker assignment by themselves.
    speaker_role: str = ""              # primary / incidental / fragmented
    speaker_role_label: str = ""
    speaker_duration_ms: int = 0
    speaker_duration_share: float = 0.0
    speaker_segment_count: int = 0
    speaker_short_segment_count: int = 0
    speaker_short_segment_rate: float = 0.0
    speaker_structure_reason: str = ""
    speaker_review_hint: str = ""
    # Segment-level user intent from voice-selection review.
    # "dub" follows normal translate/TTS/alignment; "keep_original" skips
    # translation/TTS and overlays the matching original-audio slice.
    dubbing_mode: str = DUBBING_MODE_DUB
    # P4-lite: deterministic low-information cue routing. Some timer/filler
    # cues are too short to dub naturally into long action slots; after capped
    # underflow DSP, the pipeline may preserve the original slice instead.
    auto_keep_original_reason: str = ""
    auto_keep_original_source: str = ""
    # P1-n: compact spoken rewrite for short, contentful Q&A segments that
    # would otherwise enter force-DSP because literal Chinese is too long.
    short_content_compact_attempted: bool = False
    short_content_compact_accepted: bool = False
    short_content_compact_rejected_reason: str = ""
    short_content_compact_class: str = ""
    short_content_compact_lower_chars: int = 0
    short_content_compact_upper_chars: int = 0
    short_content_compact_pre_chars: int = 0
    short_content_compact_post_chars: int = 0
    # ---- Phase 4.1 D: CosyVoice mainland worker routing（2026-05-25 Codex v3 签字） ----
    # **D 只是 plumbing**：这两个字段由 Phase 4.1 E (Gateway segment producer)
    # 从 ``user_voices`` 的同名列填写；E 落地前默认值 False/"" 保证既有路径不变。
    #
    # ``requires_worker``：True 表示该段必须走武汉 mainland worker（即不能用
    # 国际版 DashScope endpoint）。TTSGenerator / segment_regenerate 看到 True
    # 时切到 worker path，**绝不静默 fallback** 到 MiniMax / 其它 provider
    # （CLAUDE.md 付费 API 硬约束）。
    #
    # ``worker_target_model``：调 worker /synthesize-batch 时透传的 target_model
    # 字符串（e.g. ``"cosyvoice-v3.5-flash"`` / ``"cosyvoice-v3.5-plus"``）。
    # 来源是 ``user_voices.target_model``，clone 时锁定，TTS 必须用同一模型——
    # 命名上避免 ``clone_target_model``（会和 ``voice-enrollment`` clone API
    # 模型混淆）。**严禁** TTSGenerator hardcode；worker 路径头部硬校验非空。
    requires_worker: bool = False
    worker_target_model: str = ""
    # Phase 2a free tier — per-speaker MiMo voiceclone reference clip path,
    # stamped by the pipeline (cut from speech_for_asr.wav) BEFORE TTS. When
    # set, tts_generator routes this segment through _generate_one_mimo_voiceclone
    # (clone original speaker); None → base MiMo preset. Only free jobs stamp it.
    voiceclone_reference_path: str | None = None


@dataclass(slots=True)
class TranslationResult:
    segments: list[DubbingSegment]
    total_segments: int
    output_path: str


class _FallbackGenerateContentConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class GeminiTranslator:
    def __init__(
        self,
        api_key: str,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        sdk_backend: str = DEFAULT_SDK_BACKEND,
        llm_router: LLMRouter | None = None,
        speaker_infer_prompt_template: str | None = None,
        translation_prompt_template: str | None = None,
        _skip_init: bool = False,
    ):
        normalized_api_key = _normalize_optional_text(api_key)
        if normalized_api_key is None:
            raise TranslationError("Gemini api_key is required.")

        self.api_key = normalized_api_key
        self.model_name = _normalize_optional_text(model_name) or DEFAULT_MODEL_NAME
        self.temperature = float(temperature)
        self.max_output_tokens = _normalize_max_output_tokens(max_output_tokens)
        self.sdk_backend = _normalize_sdk_backend(sdk_backend)
        self.llm_router = llm_router
        self.speaker_infer_prompt_template = get_effective_speaker_infer_prompt_template(
            speaker_infer_prompt_template
        )
        self.translation_prompt_template = get_effective_translation_prompt_template(
            translation_prompt_template
        )

        self.client: Any | None = None
        self.model: Any | None = None
        self._types_module: Any | None = None
        self._legacy_sdk: Any | None = None
        self._usage_meter: Any | None = None
        self._metering_usage_context = ""
        # PR 2 (plan 2026-05-27): per-call provider usage captured by the
        # OpenAI-compatible path (mimo/deepseek/openai). None on Gemini SDK
        # path or when usage parsing fails (best-effort).
        self._last_call_usage: dict[str, Any] | None = None

        if _skip_init:
            return

        if self.sdk_backend == LEGACY_SDK_BACKEND:
            legacy_sdk = _load_legacy_gemini_sdk()
            legacy_sdk.configure(api_key=normalized_api_key)
            self._legacy_sdk = legacy_sdk
            self.model = legacy_sdk.GenerativeModel(self.model_name)
            return

        from services.gemini.client_factory import create_gemini_client
        self.client = create_gemini_client(api_key=normalized_api_key)
        self._types_module = _load_google_genai_types()

    def set_usage_meter(self, usage_meter: Any | None) -> None:
        self._usage_meter = usage_meter

    def _record_llm_usage(
        self,
        *,
        task: str,
        model_name: str,
        prompt: str,
        response_text: str,
        attempt_label: str = "",
        provider: str | None = None,
        model_id: str | None = None,
        success: bool = True,
        error: str = "",
        extra: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Record one LLM attempt (success or failure) into the project's
        UsageMeter. Best-effort: write failures only log a warning so the
        translator's main response path stays intact (plan §B3).

        ``usage`` (PR 2): provider-reported token counts from the
        OpenAI-compatible path (normalized by ``_normalize_openai_usage``).
        When present, real tokens are recorded instead of text estimates;
        when None/empty, ``record_llm`` falls back to estimating from text.
        """
        meter = getattr(self, "_usage_meter", None)
        if meter is None:
            return
        try:
            info = _MODEL_REGISTRY.get(model_name, {})
            provider_name = provider or str(info.get("provider", "gemini"))
            resolved_model_id = model_id or (
                _resolve_model_id(model_name)
                if model_name in _MODEL_REGISTRY
                else model_name
            )
            u = usage or {}
            meter.record_llm(
                task=task,
                phase=getattr(self, "_metering_usage_context", "") or "",
                provider=provider_name,
                model=model_name,
                model_id=resolved_model_id,
                input_text=prompt,
                output_text=response_text,
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=u.get("cached_input_tokens"),
                input_audio_tokens=u.get("input_audio_tokens"),
                attempt_label=attempt_label,
                success=success,
                error=error,
                extra=extra,
            )
        except Exception as exc:
            logger.warning("llm_metering_skip error=%s", exc)

    def translate(
        self,
        lines: list[TranscriptLine],
        output_dir: str,
        voice_id: str | None,
        display_name: str = "Speaker A",
        max_segment_duration_ms: int = DEFAULT_MAX_SEGMENT_DURATION_MS,
        voice_id_b: str | None = None,
        display_name_b: str | None = None,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
        speaker_voices: dict[str, str] | None = None,
        chars_per_second: float = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
        chars_per_second_by_speaker: dict[str, float] | None = None,
        source_language: str = "en",
        target_language: str = "zh-CN",
    ) -> TranslationResult:
        # Resolve to CANONICAL codes first, then stash them, so _build_prompt /
        # _build_translation_fingerprint / template selection (which compare exact
        # canonical strings) dispatch correctly even when a caller passes an alias
        # ("中文" / "English" / "EN"). resolve_language_pair fail-closes to None for
        # an unsupported pair → default profile keeps the byte-identical en->zh-CN path.
        _seg_lp_profile = resolve_language_pair(source_language, target_language) or DEFAULT_LANGUAGE_PAIR_PROFILE
        self._translate_source_language = _seg_lp_profile.source_language
        self._translate_target_language = _seg_lp_profile.target_language
        # Per-pair length ratio + whether the target is CJK (drives the voice-cps
        # metadata). Default en->zh-CN → ratio 1.8 / target CJK → byte-identical.
        _seg_target_cps_ratio = _seg_lp_profile.natural_length_ratio
        _seg_tgt_desc = get_language_descriptor(self._translate_target_language)
        _seg_target_is_cjk = _seg_tgt_desc is not None and _seg_tgt_desc.script_family == "cjk"
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = (output_root / "segments.json").resolve(strict=False)
        checkpoint_path = (output_root / "segments.checkpoint.json").resolve(strict=False)

        # voice_id is only used as passthrough metadata to DubbingSegment.voice_id.
        # When absent (e.g. studio flow where voice matching is deferred to TTS time),
        # use "auto" placeholder so TTS stage can resolve via the matcher.
        normalized_voice_id = _normalize_optional_text(voice_id) or "auto"

        normalized_display_name = _normalize_optional_text(display_name) or "Speaker A"
        normalized_voice_id_b = _normalize_optional_text(voice_id_b)
        normalized_display_name_b = _normalize_optional_text(display_name_b) or "Speaker B"
        # _pre_split_long_lines removed — S1 3-layer split + S2 LLM review
        # already ensures segments are ≤45s. No need for translator to re-split.
        groups = _build_groups(
            lines,
            max_segment_duration_ms=max_segment_duration_ms,
            chars_per_second=chars_per_second,
            chars_per_second_by_speaker=chars_per_second_by_speaker,
            source_language=source_language,
            target_language=target_language,
        )

        # Save glossary to translation/glossary.json for reference
        effective_glossary = glossary if glossary else {}
        if effective_glossary:
            glossary_path = output_root / "glossary.json"
            _write_json(glossary_path, effective_glossary)
            print(f"[S3] 术语表已保存：{len(effective_glossary)} 条 -> {glossary_path}")

        if not groups:
            result = TranslationResult(segments=[], total_segments=0, output_path=str(output_path))
            if checkpoint_path.exists():
                checkpoint_path.unlink()
            _write_json(output_path, asdict(result))
            _maybe_write_translation_quality_report(
                output_root,
                result,
                glossary=effective_glossary,
                target_language=self._translate_target_language,
            )
            return result

        translatable_groups = [
            group for group in groups
            if not is_keep_original_dubbing_mode(group.get("dubbing_mode"))
        ]
        fingerprint = self._build_translation_fingerprint(
            groups,
            video_title=video_title,
            youtube_url=youtube_url,
        )
        translated_items = self._load_translation_checkpoint(
            checkpoint_path,
            expected_fingerprint=fingerprint,
            expected_segment_ids=[int(group["segment_id"]) for group in translatable_groups],
        )
        if len(translated_items) > len(translatable_groups):
            translated_items = []

        if translated_items:
            print(f"[S3] 检测到翻译断点，恢复 {len(translated_items)}/{len(translatable_groups)} 段")

        for batch_start in range(len(translated_items), len(translatable_groups), DEFAULT_BATCH_SIZE):
            batch = translatable_groups[batch_start:batch_start + DEFAULT_BATCH_SIZE]
            translated_items.extend(
                self._translate_batch_with_length_retry(
                    batch,
                    video_title=video_title,
                    youtube_url=youtube_url,
                    glossary=effective_glossary,
                )
            )
            self._write_translation_checkpoint(
                checkpoint_path,
                fingerprint=fingerprint,
                translated_items=translated_items,
                total_groups=len(translatable_groups),
            )
            completed_count = min(batch_start + DEFAULT_BATCH_SIZE, len(translatable_groups))
            print(f"[S3] 翻译进度：{completed_count}/{len(translatable_groups)} 段")

        translated_by_id = {
            int(item["segment_id"]): item
            for item in translated_items
            if isinstance(item, dict)
        }

        segments: list[DubbingSegment] = []
        for group in groups:
            segment_id = int(group["segment_id"])
            start_ms = int(group["start_ms"])
            end_ms = int(group["end_ms"])
            speaker_id = str(group["speaker_id"])
            dubbing_mode = normalize_dubbing_mode(group.get("dubbing_mode"))
            translated_item = translated_by_id.get(segment_id)
            normalized_cn_text = (
                ""
                if dubbing_mode == DUBBING_MODE_KEEP_ORIGINAL
                else str((translated_item or {}).get("cn_text", "")).strip()
            )
            segment_voice_id, segment_display_name = _resolve_segment_voice_assignment(
                speaker_id=speaker_id,
                voice_id=normalized_voice_id,
                display_name=normalized_display_name,
                voice_id_b=normalized_voice_id_b,
                display_name_b=normalized_display_name_b,
                speaker_voices=speaker_voices,
            )
            # CodeX P2-3: stamp target_chars_per_second onto each segment so
            # the runtime auto-match path in tts_generator can pass it into
            # VoiceMatchRequest. Falls back to 0.0 when source_wps is unknown.
            _src_wps = float(group.get("source_words_per_second") or 0.0)
            # voice cps is comparable to the (hanzi/sec) catalog only when the
            # target is CJK; disable it for a non-CJK target (v3 Phase 4.2).
            _target_cps = (
                round(_src_wps * _seg_target_cps_ratio, 3)
                if (_src_wps > 0 and _seg_target_is_cjk)
                else 0.0
            )
            segments.append(
                DubbingSegment(
                    segment_id=segment_id,
                    speaker_id=speaker_id,
                    display_name=segment_display_name,
                    voice_id=segment_voice_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    target_duration_ms=max(0, end_ms - start_ms),
                    source_text=str(group["source_text"]),
                    cn_text=normalized_cn_text,
                    target_chars_per_second=_target_cps,
                    dubbing_mode=dubbing_mode,
                )
            )

        result = TranslationResult(
            segments=segments,
            total_segments=len(segments),
            output_path=str(output_path),
        )
        _write_json(output_path, asdict(result))
        _maybe_write_translation_quality_report(
            output_root,
            result,
            glossary=effective_glossary,
            target_language=self._translate_target_language,
        )
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        return result

    def translate_probe(
        self,
        lines: list[TranscriptLine],
        *,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
        speaker_voices: dict[str, str] | None = None,
        voice_id: str | None = None,
        display_name: str = "Speaker A",
        voice_id_b: str | None = None,
        display_name_b: str | None = None,
        source_language: str = "en",
        target_language: str = "zh-CN",
    ) -> list[DubbingSegment]:
        """Translate probe segments without char constraints for TTS calibration.

        Uses PROBE_TRANSLATION_PROMPT_TEMPLATE which omits min_chars/max_chars,
        so the LLM translates by feel guided only by target_duration_seconds.
        No checkpointing, no length retry — probe batches are small (≤10 segments).
        """
        # Probe may run before translate(); stash the CANONICAL pair so prompt
        # selection dispatches correctly even for aliases. Fail-closed to default.
        # Default en->zh-CN → byte-identical.
        _probe_lp_profile = (
            resolve_language_pair(source_language, target_language)
            or DEFAULT_LANGUAGE_PAIR_PROFILE
        )
        self._translate_source_language = _probe_lp_profile.source_language
        self._translate_target_language = _probe_lp_profile.target_language
        groups = _build_probe_groups(lines)
        if not groups:
            return []

        effective_glossary = glossary if glossary else {}
        prompt = self._build_probe_prompt(
            groups,
            video_title=video_title,
            youtube_url=youtube_url,
            glossary=effective_glossary,
        )
        response_text = self._call_task_with_fallback(
            "s3_translate",
            prompt,
            json_mode=False,
            validator=lambda text: self._parse_response(text, groups),
        )
        parsed_items = self._parse_response(response_text, groups)

        normalized_voice_id = _normalize_optional_text(voice_id) or "auto"
        normalized_display_name = _normalize_optional_text(display_name) or "Speaker A"
        normalized_voice_id_b = _normalize_optional_text(voice_id_b)
        normalized_display_name_b = _normalize_optional_text(display_name_b) or "Speaker B"

        segments: list[DubbingSegment] = []
        for group, item in zip(groups, parsed_items):
            speaker_id = str(group["speaker_id"])
            segment_voice_id, segment_display_name = _resolve_segment_voice_assignment(
                speaker_id=speaker_id,
                voice_id=normalized_voice_id,
                display_name=normalized_display_name,
                voice_id_b=normalized_voice_id_b,
                display_name_b=normalized_display_name_b,
                speaker_voices=speaker_voices,
            )
            line = lines[int(item["segment_id"]) - 1]
            segments.append(
                DubbingSegment(
                    segment_id=int(item["segment_id"]),
                    speaker_id=speaker_id,
                    display_name=segment_display_name,
                    voice_id=segment_voice_id,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    target_duration_ms=max(0, line.end_ms - line.start_ms),
                    source_text=str(group["source_text"]),
                    cn_text=str(item["cn_text"]).strip(),
                )
            )
        return segments

    def _select_probe_template(self, source_language: str, target_language: str) -> str:
        """Pick the probe-translation template. Default en->zh-CN → the configured
        template (admin override or PROBE, byte-identical). Non-default → honor the
        override only when it declares the pair (§2.3 fail-closed), else registry."""
        configured = get_effective_probe_translation_prompt_template()
        if source_language == "en" and target_language == "zh-CN":
            return configured
        if (
            configured != PROBE_TRANSLATION_PROMPT_TEMPLATE
            and f"{source_language}->{target_language}" in configured
        ):
            return configured
        return _PROBE_TEMPLATE_BY_PAIR.get(
            (source_language, target_language), PROBE_TRANSLATION_PROMPT_TEMPLATE
        )

    def _build_probe_prompt(
        self,
        groups: list[dict],
        *,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
    ) -> str:
        groups_json = json.dumps(groups, ensure_ascii=False, indent=2)
        speaker_ids = {str(group.get("speaker_id", "")).strip() for group in groups}
        speaker_instruction = (
            "9. 这是双人访谈，请区分两个说话人的语气、措辞和交流关系。\n"
            if len(speaker_ids) > 1
            else ""
        )
        glossary_section = ""
        if glossary:
            glossary_lines = "\n".join(f"{k} → {v}" for k, v in glossary.items())
            glossary_section = f"\n术语表（请严格遵循以下翻译）：\n{glossary_lines}\n"
        normalized_video_title = _normalize_optional_text(video_title) or "未提供"
        normalized_youtube_url = _normalize_optional_text(youtube_url) or "未提供"
        effective_template = self._select_probe_template(
            getattr(self, "_translate_source_language", "en"),
            getattr(self, "_translate_target_language", "zh-CN"),
        )
        return (
            effective_template
            .replace(TRANSLATION_PROMPT_TEMPLATE_VIDEO_TITLE_TOKEN, normalized_video_title)
            .replace(TRANSLATION_PROMPT_TEMPLATE_YOUTUBE_URL_TOKEN, normalized_youtube_url)
            .replace(TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN, glossary_section)
            .replace(TRANSLATION_PROMPT_TEMPLATE_SPEAKER_INSTRUCTION_TOKEN, speaker_instruction)
            .replace(TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN, groups_json)
        )

    def _build_translation_fingerprint(
        self,
        groups: list[dict],
        *,
        video_title: str,
        youtube_url: str,
    ) -> str:
        payload = {
            "model_name": self.model_name,
            "video_title": _normalize_optional_text(video_title) or "",
            "youtube_url": _normalize_optional_text(youtube_url) or "",
            "groups": [
                {
                    "segment_id": int(group["segment_id"]),
                    "speaker_id": str(group["speaker_id"]),
                    "start_ms": int(group["start_ms"]),
                    "end_ms": int(group["end_ms"]),
                    "target_duration_ms": int(group["target_duration_ms"]),
                    "target_duration_seconds": float(group["target_duration_seconds"]),
                    "source_word_count": int(group["source_word_count"]),
                    "source_words_per_second": float(group["source_words_per_second"]),
                    "reference_words_per_second": float(group.get("reference_words_per_second") or 0.0),
                    "density_factor_source": str(group.get("density_factor_source") or ""),
                    "density_factor": float(group["density_factor"]),
                    "dynamic_target_chars": int(group["dynamic_target_chars"]),
                    "target_chars": int(group["target_chars"]),
                    "min_chars": int(group["min_chars"]),
                    "max_chars": int(group["max_chars"]),
                    "source_text": str(group["source_text"]),
                    "dubbing_mode": str(group.get("dubbing_mode") or DUBBING_MODE_DUB),
                }
                for group in groups
            ],
        }
        # v3 §2.5/F: the DEFAULT pair adds NOTHING to the payload, so its
        # fingerprint is byte-identical to the pre-multilingual one (no spurious
        # cache miss → no paid re-translation of existing en->zh checkpoints).
        # A non-default pair appends its key to avoid cross-direction cache reuse.
        _fp_src = getattr(self, "_translate_source_language", "en")
        _fp_tgt = getattr(self, "_translate_target_language", "zh-CN")
        if not (_fp_src == "en" and _fp_tgt == "zh-CN"):
            payload["language_pair"] = f"{_fp_src}->{_fp_tgt}"
        serialized_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()

    def _load_translation_checkpoint(
        self,
        checkpoint_path: Path,
        *,
        expected_fingerprint: str,
        expected_segment_ids: list[int] | None = None,
    ) -> list[dict[str, object]]:
        if not checkpoint_path.exists():
            return []

        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(payload, dict):
            return []
        if payload.get("version") != TRANSLATION_CHECKPOINT_VERSION:
            return []
        if payload.get("input_fingerprint") != expected_fingerprint:
            return []

        translated_items = payload.get("translated_items")
        if not isinstance(translated_items, list):
            return []

        expected_ids = expected_segment_ids or list(range(1, len(translated_items) + 1))
        normalized_items: list[dict[str, object]] = []
        if len(translated_items) > len(expected_ids):
            return []
        for expected_segment_id, item in zip(expected_ids, translated_items):
            if not isinstance(item, dict):
                return []
            try:
                segment_id = int(item["segment_id"])
            except (KeyError, TypeError, ValueError):
                return []
            cn_text = _normalize_optional_text(item.get("cn_text"))
            if segment_id != expected_segment_id or cn_text is None:
                return []
            normalized_items.append(
                {
                    "segment_id": segment_id,
                    "cn_text": cn_text,
                }
            )
        return normalized_items

    def _write_translation_checkpoint(
        self,
        checkpoint_path: Path,
        *,
        fingerprint: str,
        translated_items: list[dict[str, object]],
        total_groups: int,
    ) -> None:
        payload = {
            "version": TRANSLATION_CHECKPOINT_VERSION,
            "input_fingerprint": fingerprint,
            "translated_items": [
                {
                    "segment_id": int(item["segment_id"]),
                    "cn_text": str(item["cn_text"]).strip(),
                }
                for item in translated_items
            ],
            "completed_batches": _ceil_div(len(translated_items), DEFAULT_BATCH_SIZE),
            "total_groups": int(total_groups),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_json(checkpoint_path, payload)

    def _pre_split_long_lines(
        self,
        lines: list[TranscriptLine],
        max_line_duration_ms: int = DEFAULT_PRE_SPLIT_MAX_LINE_DURATION_MS,
        max_line_chars: int = DEFAULT_PRE_SPLIT_MAX_LINE_CHARS,
        min_subline_duration_ms: int = DEFAULT_MIN_SUBLINE_DURATION_MS,
    ) -> list[TranscriptLine]:
        return _pre_split_long_lines(
            lines,
            max_line_duration_ms=max_line_duration_ms,
            max_line_chars=max_line_chars,
            min_subline_duration_ms=min_subline_duration_ms,
        )

    def infer_speaker_names(
        self,
        lines: list[TranscriptLine],
        num_speakers: int = 2,
        *,
        video_title: str = "",
        youtube_url: str = "",
        video_description: str = "",
    ) -> dict[str, str]:
        defaults = _default_speaker_names(num_speakers)
        preview_lines = [line for line in lines if line.start_ms < 300_000]
        if not preview_lines:
            preview_lines = lines[:50]
        if not preview_lines:
            return defaults

        transcript_excerpt = "\n".join(
            f"[{_speaker_label_from_id(line.speaker_id)}]: {line.source_text}"
            for line in preview_lines
            if line.source_text.strip()
        )
        if not transcript_excerpt:
            return defaults

        video_context: list[str] = []
        normalized_video_title = _normalize_optional_text(video_title)
        normalized_youtube_url = _normalize_optional_text(youtube_url)
        normalized_video_description = _normalize_optional_text(video_description)
        if normalized_video_title is not None:
            video_context.append(f"[Video title]: {normalized_video_title}")
        if normalized_youtube_url is not None:
            video_context.append(f"[Video URL]: {normalized_youtube_url}")
        if normalized_video_description is not None:
            video_context.append(f"[Video description]:\n{normalized_video_description}")
        context_excerpt = "\n".join([*video_context, transcript_excerpt]) if video_context else transcript_excerpt

        prompt = self._build_infer_speaker_prompt(
            context_excerpt=context_excerpt,
            num_speakers=num_speakers,
        )

        try:
            response_text = self._call_task_with_fallback(
                "s2_infer",
                prompt,
                json_mode=False,
                validator=self._validate_infer_speaker_response,
            )
            payload = json.loads(_strip_markdown_code_fence(response_text))
        except Exception:
            return defaults

        if not isinstance(payload, dict):
            return defaults

        inferred_names = dict(defaults)
        for speaker_id in defaults:
            normalized_name = _normalize_optional_text(payload.get(speaker_id))
            if normalized_name is not None:
                inferred_names[speaker_id] = normalized_name
        return inferred_names

    def review_speaker_labels(
        self,
        lines: list[TranscriptLine],
        speaker_names: dict[str, str],
        video_title: str = "",
        youtube_url: str = "",
    ) -> list[TranscriptLine]:
        speaker_ids = {line.speaker_id for line in lines}
        if len(lines) == 0 or len(speaker_ids) <= 1:
            return lines

        # 分批审核（每批最多 50 行，避免 prompt 过长导致 Gemini 忽略后面的内容）
        REVIEW_BATCH_SIZE = 50
        all_corrections: list[dict] = []

        for batch_start in range(0, len(lines), REVIEW_BATCH_SIZE):
            batch = lines[batch_start:batch_start + REVIEW_BATCH_SIZE]
            review_input = [
                {
                    "index": line.index,
                    "speaker_id": line.speaker_id,
                    "start_ms": line.start_ms,
                    "end_ms": line.end_ms,
                    "text": line.source_text[:300],  # 截断超长文本，减少 tokens
                }
                for line in batch
            ]
            prompt = self._build_review_prompt(review_input, speaker_names, video_title, youtube_url)

            try:
                response_text = self._call_task_with_fallback(
                    "s2_review",
                    prompt,
                    json_mode=True,
                    validator=self._validate_review_response,
                )
                batch_corrections = self._parse_review_response(response_text, batch)
                if batch_corrections:
                    all_corrections.extend(batch_corrections)
            except Exception as exc:
                print(f"[S2] Gemini审核第 {batch_start//REVIEW_BATCH_SIZE + 1} 批失败：{exc}")
                continue

        if not all_corrections:
            return lines

        corrected_lines = list(lines)
        for correction in all_corrections:
            index = correction.get("index")
            new_speaker_id = correction.get("corrected_speaker_id", "")
            if not isinstance(index, int):
                continue
            for line_index, line in enumerate(corrected_lines):
                if line.index == index and new_speaker_id in {"speaker_a", "speaker_b"}:
                    if line.speaker_id != new_speaker_id:
                        corrected_lines[line_index] = TranscriptLine(
                            index=line.index,
                            start_ms=line.start_ms,
                            end_ms=line.end_ms,
                            speaker_id=new_speaker_id,
                            speaker_label=new_speaker_id.replace("speaker_", "").upper(),
                            source_text=line.source_text,
                        )
                    break
        return corrected_lines

    def _build_infer_speaker_prompt(
        self,
        *,
        context_excerpt: str,
        num_speakers: int,
    ) -> str:
        expected_output_json = (
            '{"speaker_a": "推断的姓名或角色"}'
            if num_speakers <= 1
            else '{"speaker_a": "推断的姓名或角色", "speaker_b": "推断的姓名或角色"}'
        )
        return (
            self.speaker_infer_prompt_template
            .replace(SPEAKER_INFER_PROMPT_TEMPLATE_CONTEXT_TOKEN, context_excerpt)
            .replace(SPEAKER_INFER_PROMPT_TEMPLATE_EXPECTED_OUTPUT_TOKEN, expected_output_json)
        )

    def _build_review_prompt(
        self,
        review_input: list[dict],
        speaker_names: dict[str, str],
        video_title: str,
        youtube_url: str,
    ) -> str:
        speaker_a_name = speaker_names.get("speaker_a", "Speaker A")
        speaker_b_name = speaker_names.get("speaker_b", "Speaker B")
        return (
            "你是一位专业的访谈转录审核专家。以下是一段两人访谈的转录稿片段，由语音识别系统自动标注了说话人身份。\n\n"
            "视频信息：\n"
            f"- 标题：{video_title}\n"
            f"- 来源：{youtube_url}\n\n"
            "说话人信息：\n"
            f"- speaker_a: {speaker_a_name}\n"
            f"- speaker_b: {speaker_b_name}\n\n"
            "请仔细逐条检查每句话的说话人标注是否正确。常见错误包括：\n"
            "1. 短促回应（如 Yeah, Right, Sure, Mm-hmm, Of course）经常被错误分配\n"
            "2. 长段落中混合了两个人的话（旁白+提问，或回答+追问）\n"
            "3. 回答被错误标成提问者，或反之\n"
            "4. 同一人的连续发言被拆给两个人\n\n"
            "判断技巧：\n"
            "- 采访中主持人提问（短句，问号），嘉宾回答（长段，陈述句）\n"
            "- 旁白介绍通常是主持人的声音\n"
            "- 利用你对该节目或说话人的了解来判断\n\n"
            "输入转录稿：\n"
            f"{json.dumps(review_input, ensure_ascii=False, indent=2)}\n\n"
            "请输出需要纠正的条目，JSON 数组格式：\n"
            "[\n"
            '  {"index": 3, "corrected_speaker_id": "speaker_b", "reason": "简短回应，应属于回答者"}\n'
            "]\n\n"
            "如果所有标注都正确，输出空数组 []\n"
            "只输出 JSON 数组，不要其他文字。请务必仔细检查每一条。"
        )

    def _call_gemini_with_retry(
        self,
        prompt: str,
        json_mode: bool = False,
        *,
        model_name: str | None = None,
    ) -> str:
        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            try:
                if self.sdk_backend == LEGACY_SDK_BACKEND:
                    return self._call_legacy_sdk(prompt, json_mode=json_mode, model_name=model_name)
                return self._call_google_genai(prompt, json_mode=json_mode, model_name=model_name)
            except Exception as exc:
                if attempt < DEFAULT_MAX_RETRIES:
                    wait_seconds = min(60, 5 * (2 ** attempt))  # 5s, 10s, 20s, 40s, 60s
                    print(
                        f"[S3] Gemini请求失败，{wait_seconds}秒后重试"
                        f"（{attempt + 1}/{DEFAULT_MAX_RETRIES}）: {exc}"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise TranslationError(
                    f"Gemini请求失败（已重试{DEFAULT_MAX_RETRIES}次）: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # llm_registry-based dispatch (replaces LLMRouter route decisions)
    # ------------------------------------------------------------------

    def _call_by_model(
        self,
        model_name: str,
        prompt: str,
        *,
        json_mode: bool = False,
    ) -> str:
        """Call a specific model by logical name, dispatching by provider.

        - gemini → existing _call_gemini_with_retry
        - deepseek/openai → OpenAI-compatible HTTP call
        - mimo → OpenAI-compatible HTTP call
        """
        # Reset per-call usage capture. Only the OpenAI-compatible branches
        # below populate it; the Gemini SDK path and any error leave it None,
        # so _record_llm_usage falls back to estimated tokens (PR 2).
        self._last_call_usage = None

        info = _MODEL_REGISTRY.get(model_name)
        if info is None:
            raise TranslationError(f"Unknown model: {model_name}")
        provider = info["provider"]
        api_model_id = info["api_model_id"]

        if provider == "gemini":
            return self._call_gemini_with_retry(prompt, json_mode=json_mode, model_name=api_model_id)

        # Non-Gemini: use LLMRouter provider layer if available
        api_key = _get_model_api_key(model_name)
        if not api_key:
            env_var = info.get("api_key_env", "")
            raise TranslationError(
                f"{model_name} API key not configured (check {env_var} or admin settings)"
            )

        if provider == "mimo":
            text, usage = self._call_mimo_text(
                api_key=api_key, model_id=api_model_id, prompt=prompt, json_mode=json_mode
            )
            self._last_call_usage = usage or None
            return text

        # deepseek / openai — direct HTTP call (no LLMRouter indirection)
        text, usage = self._call_openai_compatible(
            api_key=api_key,
            model_id=api_model_id,
            provider=provider,
            prompt=prompt,
            json_mode=json_mode,
            request_overrides=info.get("request_overrides"),
        )
        self._last_call_usage = usage or None
        return text

    @staticmethod
    def _call_mimo_text(
        *,
        api_key: str,
        model_id: str,
        prompt: str,
        json_mode: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Call MiMo via OpenAI-compatible HTTP API. Returns (text, usage)."""
        import urllib.request
        import urllib.error
        url = "https://api.xiaomimimo.com/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 8192,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return (
                body["choices"][0]["message"]["content"],
                GeminiTranslator._normalize_openai_usage(body),
            )
        except Exception as exc:
            raise TranslationError(f"MiMo API call failed: {exc}") from exc

    @staticmethod
    def _call_openai_compatible(
        *,
        api_key: str,
        model_id: str,
        provider: str,
        prompt: str,
        json_mode: bool = False,
        request_overrides: object | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Direct call to OpenAI-compatible API (DeepSeek / OpenAI). Returns (text, usage)."""
        import urllib.request
        import urllib.error
        base_urls = {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com",
        }
        base_url = base_urls.get(provider, base_urls["openai"])
        url = f"{base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 8192,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if isinstance(request_overrides, dict):
            payload.update(request_overrides)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return (
                body["choices"][0]["message"]["content"],
                GeminiTranslator._normalize_openai_usage(body),
            )
        except Exception as exc:
            raise TranslationError(f"{provider} API call failed: {exc}") from exc

    @staticmethod
    def _normalize_openai_usage(body: dict[str, Any]) -> dict[str, Any]:
        """Delegates to the shared normalizer (single source of truth in
        ``llm_registry.normalize_openai_usage``). Kept as a thin wrapper so
        existing call sites and tests keep their import surface (PR 2)."""
        return _normalize_openai_usage_shared(body)

    def _call_task_with_fallback(
        self,
        task: str,
        prompt: str,
        *,
        json_mode: bool = False,
        validator: Any | None = None,
    ) -> str:
        # --- New path: llm_registry-based model selection ---
        # Only active when _service_mode is explicitly set (by process.py).
        # Without it, fall through to legacy LLMRouter path for backward compat.
        prompt_key_map = {
            "s3_translate": "translate",
            "s5_rewrite": "rewrite",
            "s5_rewrite_strict": "rewrite",
            "s5_short_content_compact": "rewrite",
            "s2_infer": "translate",  # speaker inference uses same model as translate
            "s2_review": "translate",  # legacy 2-speaker review fallback (process.py:_legacy_speaker_inference_and_review)
            "content_compliance": "content_compliance",
        }
        prompt_key = prompt_key_map.get(task)
        mode = getattr(self, "_service_mode", None)

        if prompt_key and mode is not None:
            model_name = _get_prompt_model(mode, prompt_key)
            # Try primary model, then fallback candidates
            models_to_try = [model_name] + _get_fallback_candidates(model_name, requires_audio=False)
            last_error: Exception | None = None
            for i, m in enumerate(models_to_try):
                attempt_label = "primary" if i == 0 else f"fallback_{i}"
                provider_response_received = False
                response_text = ""
                attempt_start_ms = int(time.time() * 1000)
                try:
                    logger.info("llm_attempt_start task=%s model=%s model_id=%s", task, m, _resolve_model_id(m))
                    response_text = self._call_by_model(m, prompt, json_mode=json_mode)
                    provider_response_received = True
                    if validator is not None:
                        validator(response_text)
                    # Success — record a clean attempt and return.
                    self._record_llm_usage(
                        task=task,
                        model_name=m,
                        prompt=prompt,
                        response_text=response_text,
                        attempt_label=attempt_label,
                        success=True,
                        usage=self._last_call_usage,
                        extra={
                            "provider_response_received": True,
                            "duration_ms": int(time.time() * 1000) - attempt_start_ms,
                            "fallback_policy_source": "llm_registry_defaults",
                        },
                    )
                    return response_text
                except (TranslationError, LLMProviderError) as exc:
                    last_error = exc
                    # Plan §B5/§B6: structural class wins over heuristic — if
                    # the provider already returned text and we failed AFTER
                    # that, it's a validator failure (provider was paid).
                    if provider_response_received:
                        error_class = LLM_ERROR_CLASS_INVALID_OUTPUT
                        error_code = "validator_failed"
                    else:
                        error_class, error_code = classify_llm_error(exc)
                    next_m = models_to_try[i + 1] if i < len(models_to_try) - 1 else None
                    self._record_llm_usage(
                        task=task,
                        model_name=m,
                        prompt=prompt,
                        response_text=response_text,
                        attempt_label=attempt_label,
                        success=False,
                        # Audit-safe error: type + short message only. Full
                        # exception text may carry provider response or user
                        # subtitle fragments — those don't belong on disk.
                        # error_class / error_code in extra carry the
                        # machine-readable signal callers should use.
                        error=f"{type(exc).__name__}: {str(exc)[:200]}",
                        extra={
                            "provider_response_received": provider_response_received,
                            "error_class": error_class,
                            "error_code": error_code,
                            "duration_ms": int(time.time() * 1000) - attempt_start_ms,
                            "fallback_from": m if next_m else None,
                            "fallback_to": next_m,
                            "fallback_policy_source": "llm_registry_defaults",
                        },
                    )
                    if next_m is not None:
                        logger.warning(
                            "llm_fallback_triggered task=%s model=%s fallback=%s error=%s",
                            task, m, next_m, exc,
                        )
                    continue
            if last_error is not None:
                raise TranslationError(str(last_error)) from last_error
            raise TranslationError(f"No models available for task '{task}'.")

        # --- Legacy path: LLMRouter fallback chain (for unmapped tasks) ---
        # DEPRECATION OBSERVATION (2026-05-02 重启): 上一轮 2026-04-17~2026-05-01
        # 观察因 docker json-file 不持久 + container 2026-05-01 recreate 而证据
        # 全失。本轮新增 prompt_key_map 的 s2_review 映射 + 持久化 audit log，
        # 重新观察 2 周（直到 2026-05-16），零命中再执行 §5 11 步清理。
        # 观察期计划：docs/plans/2026-04-17-llmrouter-deprecation.md
        _legacy_path_msg = (
            f"[LLM-ROUTER-LEGACY] hit task={task} prompt_key={prompt_key!r} "
            f"service_mode={mode!r} has_router={self.llm_router is not None}"
        )
        logger.warning("llm_router_legacy_path %s", _legacy_path_msg)
        try:
            _runtime_logs_dir = os.environ.get(
                "AIVIDEOTRANS_RUNTIME_LOGS_DIR",
                "/opt/aivideotrans/data/runtime_logs",
            )
            os.makedirs(_runtime_logs_dir, exist_ok=True)
            _audit_log_path = os.path.join(_runtime_logs_dir, "llm-router-legacy.log")
            with open(_audit_log_path, "a", encoding="utf-8") as _f:
                _f.write(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {_legacy_path_msg}\n"
                )
        except OSError:
            pass
        route = self.llm_router.get_route(task) if self.llm_router is not None else ["default_llm"]
        if not route:
            route = ["default_llm"]

        last_error = None
        for index, alias in enumerate(route):
            model_config = self.llm_router.get_model_config(alias) if self.llm_router is not None else {}
            provider_name = _normalize_optional_text(model_config.get("provider"))
            override_model_name = _normalize_optional_text(model_config.get("model_name"))
            allow_same_alias_retry = (
                provider_name not in {None, "gemini"}
                and alias not in {"gemini_current", "default_llm"}
            )
            for retry_attempt in range(DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK + 1):
                provider_response_received = False
                response_text = ""
                attempt_start_ms = int(time.time() * 1000)
                # Resolve recording identity once per attempt so failure path
                # has the same model_name/model_id keys as the success path.
                record_model_name = override_model_name or alias
                record_model_id = override_model_name or alias
                if alias in {"gemini_current", "default_llm"} or provider_name == "gemini":
                    record_model_name = override_model_name or self.model_name
                    record_model_id = override_model_name or self.model_name
                attempt_label = (
                    alias if retry_attempt == 0 else f"{alias}_retry_{retry_attempt}"
                )
                try:
                    if alias in {"gemini_current", "default_llm"} or provider_name == "gemini":
                        if override_model_name is None:
                            response_text = self._call_gemini_with_retry(prompt, json_mode=json_mode)
                        else:
                            response_text = self._call_gemini_with_retry(
                                prompt,
                                json_mode=json_mode,
                                model_name=override_model_name,
                            )
                    else:
                        logger.info("llm_attempt_start task=%s alias=%s", task, alias)
                        response_text = self.llm_router.generate_via_alias(
                            alias,
                            prompt=prompt,
                            json_mode=json_mode,
                        )

                    provider_response_received = True
                    if validator is not None:
                        validator(response_text)
                    self._record_llm_usage(
                        task=task,
                        model_name=record_model_name,
                        model_id=record_model_id,
                        provider=provider_name or "gemini",
                        prompt=prompt,
                        response_text=response_text,
                        attempt_label=attempt_label,
                        success=True,
                        extra={
                            "provider_response_received": True,
                            "duration_ms": int(time.time() * 1000) - attempt_start_ms,
                            "fallback_policy_source": "legacy_router",
                        },
                    )
                    return response_text
                except (TranslationError, LLMProviderError) as exc:
                    last_error = exc
                    if provider_response_received:
                        error_class = LLM_ERROR_CLASS_INVALID_OUTPUT
                        error_code = "validator_failed"
                    else:
                        error_class, error_code = classify_llm_error(exc)
                    should_retry_same_alias = (
                        allow_same_alias_retry
                        and retry_attempt < DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK
                        and _should_retry_same_alias_before_fallback(exc)
                    )
                    next_alias = (
                        route[index + 1] if index < len(route) - 1 else None
                    )
                    self._record_llm_usage(
                        task=task,
                        model_name=record_model_name,
                        model_id=record_model_id,
                        provider=provider_name or "gemini",
                        prompt=prompt,
                        response_text=response_text,
                        attempt_label=attempt_label,
                        success=False,
                        error=f"{type(exc).__name__}: {str(exc)[:200]}",
                        extra={
                            "provider_response_received": provider_response_received,
                            "error_class": error_class,
                            "error_code": error_code,
                            "duration_ms": int(time.time() * 1000) - attempt_start_ms,
                            "fallback_from": alias if (next_alias and not should_retry_same_alias) else None,
                            "fallback_to": next_alias if not should_retry_same_alias else None,
                            "fallback_policy_source": "legacy_router",
                        },
                    )
                    if should_retry_same_alias:
                        wait_seconds = 3 * (retry_attempt + 1)
                        logger.warning(
                            "llm_transient_retry task=%s alias=%s attempt=%d/%d wait_s=%s error=%s",
                            task, alias, retry_attempt + 1, DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK, wait_seconds, exc,
                        )
                        time.sleep(wait_seconds)
                        continue
                    break
            if index >= len(route) - 1:
                break
            next_alias = route[index + 1]
            logger.warning(
                "llm_fallback_triggered task=%s alias=%s fallback=%s error=%s",
                task, alias, next_alias, last_error,
            )

        if last_error is None:
            raise TranslationError(f"No LLM route is configured for task '{task}'.")
        raise TranslationError(str(last_error)) from last_error

    def _validate_infer_speaker_response(self, response_text: str) -> None:
        payload = json.loads(_strip_markdown_code_fence(response_text))
        if not isinstance(payload, dict):
            raise TranslationError("Speaker inference response must be a JSON object.")

    def _validate_review_response(self, response_text: str) -> None:
        payload = json.loads(_strip_markdown_code_fence(response_text))
        if not isinstance(payload, list):
            raise TranslationError("Speaker review response must be a JSON array.")

    def _call_google_genai(self, prompt: str, *, json_mode: bool, model_name: str | None = None) -> str:
        if self.client is None:
            raise TranslationError("Gemini client is not initialized.")

        config_kwargs: dict[str, object] = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "http_options": {"timeout": DEFAULT_REQUEST_TIMEOUT_SECONDS * 1000},
        }
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        config_factory = getattr(self._types_module, "GenerateContentConfig", _FallbackGenerateContentConfig)
        response = self.client.models.generate_content(
            model=model_name or self.model_name,
            contents=prompt,
            config=config_factory(**config_kwargs),
        )
        return _extract_response_text(response)

    def _call_legacy_sdk(self, prompt: str, *, json_mode: bool, model_name: str | None = None) -> str:
        if self.model is None:
            raise TranslationError("Legacy Gemini model is not initialized.")

        request_kwargs: dict[str, object] = {
            "request_options": {"timeout": DEFAULT_REQUEST_TIMEOUT_SECONDS},
        }
        generation_config_factory = getattr(self._legacy_sdk, "GenerationConfig", None)
        if callable(generation_config_factory):
            config_kwargs: dict[str, object] = {
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            }
            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"
            request_kwargs["generation_config"] = generation_config_factory(**config_kwargs)

        model = self.model
        if model_name and model_name != self.model_name:
            legacy_sdk = self._legacy_sdk or _load_legacy_gemini_sdk()
            model = legacy_sdk.GenerativeModel(model_name)

        response = model.generate_content(prompt, **request_kwargs)
        return _extract_response_text(response)

    # Fields sent to LLM; everything else is internal/cache-only.
    # v2.1 (translation-duration-alignment Phase 1): added natural-length
    # hint + source/voice speed context so the LLM can reason about why the
    # min/max envelope is what it is and how much slack it has.
    # Explicitly NOT forwarded: start_ms / end_ms / target_duration_ms
    # (redundant with target_duration_seconds), reference_words_per_second /
    # density_factor / density_factor_source (internal derivation state).
    _LLM_GROUP_FIELDS = frozenset({
        "segment_id", "speaker_id", "source_text",
        "target_duration_seconds", "min_chars", "max_chars",
        # --- v2.1 additions ---
        "target_chars",
        "target_chars_hint",
        "source_word_count",
        "source_words_per_second",
        "voice_chars_per_second",
    })

    def _select_translation_template(self, source_language: str, target_language: str) -> str:
        """Pick the translation prompt template for the job's language pair.

        Default pair (en->zh-CN): the configured template (admin override or
        DEFAULT) — byte-identical to the legacy path. Non-default pair: honor the
        admin override ONLY when it declares the canonical ``"src->tgt"`` key
        (§2.3 fail-closed), otherwise the per-pair registry template — never reuse
        the en->zh override/default for a different direction.
        """
        if source_language == "en" and target_language == "zh-CN":
            return self.translation_prompt_template
        override = self.translation_prompt_template
        if (
            override != DEFAULT_TRANSLATION_PROMPT_TEMPLATE
            and f"{source_language}->{target_language}" in override
        ):
            return override
        return _TRANSLATION_TEMPLATE_BY_PAIR.get(
            (source_language, target_language), DEFAULT_TRANSLATION_PROMPT_TEMPLATE
        )

    def _build_prompt(
        self,
        groups: list[dict],
        *,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
        strict_length_control: bool = False,
    ) -> str:
        # Strip internal fields before sending to LLM (saves tokens, reduces noise).
        # _build_translation_fingerprint uses the full groups, so caching is unaffected.
        llm_groups = [
            {k: v for k, v in g.items() if k in self._LLM_GROUP_FIELDS}
            for g in groups
        ]
        groups_json = json.dumps(llm_groups, ensure_ascii=False, indent=2)
        _strict_tgt_desc = get_language_descriptor(
            getattr(self, "_translate_target_language", "zh-CN")
        )
        _strict_target_is_latin = (
            _strict_tgt_desc is not None and _strict_tgt_desc.script_family == "latin"
        )
        if not strict_length_control:
            strict_length_instruction = ""
        elif _strict_target_is_latin:
            # For an English target the *_chars budget is measured in WORDS; keep the
            # strict reminder English + word-framed so it matches the word validator.
            strict_length_instruction = (
                "12. Length reminder: the previous translation missed the min_chars ~ "
                "max_chars budget, which for this English voice-over is counted in WORDS. "
                "If it was too long, tighten and drop redundancy; if too short, add detail. "
                "Keep the English word count strictly within min_chars ~ max_chars.\n"
            )
        else:
            strict_length_instruction = (
                "12. 字数提醒：上一次翻译未达到 min_chars ~ max_chars 的字数要求。"
                "如果偏长，请精简表达、删除冗余修饰；如果偏短，请适度补充细节、展开表述。"
                "请严格将译文字数控制在 min_chars 到 max_chars 范围内。\n"
            )
        speaker_ids = {str(group.get("speaker_id", "")).strip() for group in groups}
        speaker_instruction = (
            "9. 这是双人访谈，请区分两个说话人的语气、措辞和交流关系。\n"
            if len(speaker_ids) > 1
            else ""
        )
        glossary_section = ""
        if glossary:
            glossary_lines = "\n".join(f"{k} → {v}" for k, v in glossary.items())
            glossary_section = f"\n术语表（请严格遵循以下翻译）：\n{glossary_lines}\n"
        normalized_video_title = _normalize_optional_text(video_title) or "未提供"
        normalized_youtube_url = _normalize_optional_text(youtube_url) or "未提供"
        return (
            self._select_translation_template(
                getattr(self, "_translate_source_language", "en"),
                getattr(self, "_translate_target_language", "zh-CN"),
            )
            .replace(TRANSLATION_PROMPT_TEMPLATE_VIDEO_TITLE_TOKEN, normalized_video_title)
            .replace(TRANSLATION_PROMPT_TEMPLATE_YOUTUBE_URL_TOKEN, normalized_youtube_url)
            .replace(TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN, glossary_section)
            .replace(TRANSLATION_PROMPT_TEMPLATE_SPEAKER_INSTRUCTION_TOKEN, speaker_instruction)
            .replace(TRANSLATION_PROMPT_TEMPLATE_STRICT_LENGTH_TOKEN, strict_length_instruction)
            .replace(TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN, groups_json)
        )

    def _parse_response(self, response_text: str, groups: list[dict]) -> list[dict]:
        normalized_response_text = _strip_markdown_code_fence(response_text)
        try:
            payload = json.loads(normalized_response_text)
        except json.JSONDecodeError as exc:
            raise TranslationError("Gemini returned invalid JSON.") from exc

        if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
            payload = payload["segments"]
        if not isinstance(payload, list):
            raise TranslationError("Gemini response must be a JSON array.")

        expected_segment_ids = [int(group["segment_id"]) for group in groups]
        translated_by_id: dict[int, dict[str, str]] = {}
        for item in payload:
            if not isinstance(item, dict):
                raise TranslationError("Gemini response items must be JSON objects.")
            segment_id = _coerce_optional_int(item.get("segment_id"))
            if segment_id is None:
                raise TranslationError("Gemini response items require segment_id.")
            if segment_id in translated_by_id:
                raise TranslationError(f"Gemini response contains duplicate segment_id: {segment_id}")
            translated_by_id[segment_id] = {
                "segment_id": segment_id,
                # Accept ``target_text`` as an alias for ``cn_text`` (plan v3 §4.5);
                # cn_text stays the canonical container (§4.3). en->zh-CN output
                # uses cn_text → byte-identical.
                "cn_text": _normalize_optional_text(item.get("target_text") or item.get("cn_text")) or "",
            }

        if set(translated_by_id) != set(expected_segment_ids):
            raise TranslationError("Gemini response segment_id set does not match requested groups.")

        return [translated_by_id[segment_id] for segment_id in expected_segment_ids]

    def _translate_batch_with_length_retry(
        self,
        batch: list[dict],
        *,
        video_title: str,
        youtube_url: str,
        glossary: dict[str, str] | None = None,
    ) -> list[dict]:
        prompt = self._build_prompt(
            batch,
            video_title=video_title,
            youtube_url=youtube_url,
            glossary=glossary,
        )
        response_text = self._call_task_with_fallback(
            "s3_translate",
            prompt,
            json_mode=False,
            validator=lambda text: self._parse_response(text, batch),
        )
        parsed_items = self._parse_response(response_text, batch)
        if not self._batch_needs_length_retry(parsed_items, batch):
            return parsed_items

        print("[S3] 长度校验未通过，当前批次重翻 1 次...")
        retry_prompt = self._build_prompt(
            batch,
            video_title=video_title,
            youtube_url=youtube_url,
            glossary=glossary,
            strict_length_control=True,
        )
        retry_response_text = self._call_task_with_fallback(
            "s3_translate",
            retry_prompt,
            json_mode=False,
            validator=lambda text: self._parse_response(text, batch),
        )
        return self._parse_response(retry_response_text, batch)

    def _batch_needs_length_retry(
        self,
        parsed_items: list[dict],
        groups: list[dict],
    ) -> bool:
        group_by_segment_id = {int(group["segment_id"]): group for group in groups}
        for item in parsed_items:
            segment_id = int(item["segment_id"])
            group = group_by_segment_id.get(segment_id)
            if group is None:
                continue
            if self._needs_translation_retry_for_length(
                cn_text=str(item["cn_text"]),
                min_chars=int(group["min_chars"]),
                max_chars=int(group["max_chars"]),
            ):
                return True
        return False

    def _count_cn_chars(self, text: str) -> int:
        # Length-gate unit must match the target unit of the budget (min/max_chars).
        # CJK target (default): per-char count (byte-identical legacy). Latin
        # target: word count, since the budget is target_chars = source \u00d7 ratio in
        # English *words* \u2014 counting letters would be ~5x off and always retry.
        target_language = getattr(self, "_translate_target_language", "zh-CN")
        desc = get_language_descriptor(target_language)
        if desc is not None and desc.script_family == "latin":
            return len(re.findall(r"[A-Za-z0-9']+", text or ""))
        clean = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]", "", text)
        return len(clean)

    def _needs_translation_retry_for_length(
        self,
        *,
        cn_text: str,
        min_chars: int,
        max_chars: int,
    ) -> bool:
        actual_chars = self._count_cn_chars(cn_text)
        # With probe-calibrated chars/sec, min_chars/max_chars are accurate.
        # Retry if translation falls outside the target range.
        return actual_chars < min_chars or actual_chars > max_chars

    def _parse_review_response(
        self,
        response_text: str,
        original_lines: list[TranscriptLine],
    ) -> list[dict]:
        try:
            normalized_response_text = _strip_markdown_code_fence(response_text).strip()
            payload = json.loads(normalized_response_text)
        except Exception:
            return []

        if not isinstance(payload, list):
            return []

        valid_indexes = {line.index for line in original_lines}
        original_speakers = {line.index: line.speaker_id for line in original_lines}
        filtered_corrections: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            corrected_speaker_id = item.get("corrected_speaker_id")
            if not isinstance(index, int):
                continue
            if index not in valid_indexes:
                continue
            if corrected_speaker_id not in {"speaker_a", "speaker_b"}:
                continue
            if original_speakers.get(index) == corrected_speaker_id:
                continue
            filtered_corrections.append(
                {
                    "index": index,
                    "corrected_speaker_id": corrected_speaker_id,
                    "reason": _normalize_optional_text(item.get("reason")) or "",
                }
            )
        return filtered_corrections


def load_gemini_config() -> dict[str, object]:
    config_path = DEFAULT_AUTODUB_LOCAL_CONFIG_PATH.resolve(strict=False)
    payload: dict[str, object] = {}

    if config_path.exists():
        try:
            loaded_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationError(f"Failed to load Gemini config from {config_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise TranslationError(f"Gemini config file must contain a top-level JSON object: {config_path}")
        payload = loaded_payload

    section = payload.get("gemini", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise TranslationError("gemini config section must be a JSON object.")

    api_key_env_var = _normalize_optional_text(section.get("api_key_env_var")) or "GEMINI_API_KEY"
    api_key = _normalize_optional_text(section.get("api_key"))
    if api_key is None:
        api_key = _normalize_optional_text(os.getenv(api_key_env_var))
    if api_key is None:
        raise TranslationError(
            f"Gemini API key is required via autodub.local.json or env {api_key_env_var}."
        )

    sdk_backend = (
        _normalize_optional_text(section.get("sdk_backend"))
        or _normalize_optional_text(section.get("sdk"))
        or _normalize_optional_text(os.getenv("AUTODUB_GEMINI_SDK"))
        or DEFAULT_SDK_BACKEND
    )

    speaker_infer_prompt_template = _read_prompt_template(payload, "s2_infer")
    if speaker_infer_prompt_template is not None:
        speaker_infer_prompt_template = validate_speaker_infer_prompt_template(
            speaker_infer_prompt_template
        )

    translation_prompt_template = _read_prompt_template(payload, "s3_translate")
    if translation_prompt_template is not None:
        translation_prompt_template = validate_translation_prompt_template(
            translation_prompt_template
        )

    rewrite_prompt_template = _read_prompt_template(payload, "s5_rewrite")
    if rewrite_prompt_template is not None:
        rewrite_prompt_template = validate_rewrite_prompt_template(
            rewrite_prompt_template
        )

    return {
        "api_key": api_key,
        "api_key_env_var": api_key_env_var,
        "model_name": _normalize_optional_text(section.get("model_name")) or DEFAULT_MODEL_NAME,
        "temperature": _coerce_float(section.get("temperature"), default=DEFAULT_TEMPERATURE),
        "max_output_tokens": _normalize_max_output_tokens(
            section.get("max_output_tokens")
        ),
        "sdk_backend": _normalize_sdk_backend(sdk_backend),
        "speaker_infer_prompt_template": speaker_infer_prompt_template,
        "translation_prompt_template": translation_prompt_template,
        "rewrite_prompt_template": rewrite_prompt_template,
    }


def get_effective_speaker_infer_prompt_template(template: object | None = None) -> str:
    normalized_template = _normalize_optional_text(template)
    if normalized_template is None:
        return DEFAULT_SPEAKER_INFER_PROMPT_TEMPLATE
    return validate_speaker_infer_prompt_template(normalized_template)


def validate_speaker_infer_prompt_template(template: str) -> str:
    return _validate_prompt_template_tokens(
        template,
        required_tokens=(SPEAKER_INFER_PROMPT_TEMPLATE_CONTEXT_TOKEN,),
        label="S2 说话人识别提示词",
    )


def get_effective_translation_prompt_template(template: object | None = None) -> str:
    normalized_template = _normalize_optional_text(template)
    if normalized_template is None:
        # Check admin override
        try:
            from services.transcript_reviewer import _get_admin_prompt_override
            admin = _get_admin_prompt_override("translate")
            if admin:
                normalized_template = admin
        except Exception:
            pass
    if normalized_template is None:
        return DEFAULT_TRANSLATION_PROMPT_TEMPLATE
    return validate_translation_prompt_template(normalized_template)


def validate_translation_prompt_template(template: str) -> str:
    return _validate_prompt_template_tokens(
        template,
        required_tokens=(TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN,),
        label="S3 翻译提示词",
    )


def get_effective_probe_translation_prompt_template(template: object | None = None) -> str:
    normalized_template = _normalize_optional_text(template)
    if normalized_template is None:
        try:
            from services.transcript_reviewer import _get_admin_prompt_override
            admin = _get_admin_prompt_override("probe_translate")
            if admin:
                normalized_template = admin
        except Exception:
            pass
    if normalized_template is None:
        return PROBE_TRANSLATION_PROMPT_TEMPLATE
    return _validate_prompt_template_tokens(
        normalized_template,
        required_tokens=(TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN,),
        label="探针翻译提示词",
    )


def get_effective_rewrite_prompt_template(template: object | None = None) -> str:
    normalized_template = _normalize_optional_text(template)
    if normalized_template is None:
        # Check admin override
        try:
            from services.transcript_reviewer import _get_admin_prompt_override
            admin = _get_admin_prompt_override("rewrite")
            if admin:
                normalized_template = admin
        except Exception:
            pass
    if normalized_template is None:
        return DEFAULT_REWRITE_PROMPT_TEMPLATE
    return validate_rewrite_prompt_template(normalized_template)


def validate_rewrite_prompt_template(template: str) -> str:
    return _validate_prompt_template_tokens(
        template,
        required_tokens=(
            REWRITE_PROMPT_TEMPLATE_TEXT_TOKEN,
            REWRITE_PROMPT_TEMPLATE_DIRECTION_TOKEN,
            REWRITE_PROMPT_TEMPLATE_DIRECTION_INSTRUCTION_TOKEN,
            REWRITE_PROMPT_TEMPLATE_TARGET_CHARS_TOKEN,
        ),
        label="S5 重写提示词",
    )


def _validate_prompt_template_tokens(
    template: str,
    *,
    required_tokens: tuple[str, ...],
    label: str,
) -> str:
    normalized_template = _normalize_optional_text(template)
    if normalized_template is None:
        raise TranslationError(f"{label}不能为空。")
    missing_tokens = [token for token in required_tokens if token not in normalized_template]
    if missing_tokens:
        missing_tokens_text = "、".join(missing_tokens)
        raise TranslationError(f"{label}必须包含 {missing_tokens_text} 占位符。")
    return normalized_template


def _read_prompt_template(payload: dict[str, object], key: str) -> str | None:
    prompts_section = payload.get("prompts")
    if not isinstance(prompts_section, dict):
        return None
    return _normalize_optional_text(prompts_section.get(key))


def _load_google_genai_sdk() -> Any:
    try:
        return importlib.import_module("google.genai")
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise TranslationError("google-genai SDK is not installed.") from exc


def _load_google_genai_types() -> Any:
    try:
        return importlib.import_module("google.genai.types")
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise TranslationError("google-genai types module is not installed.") from exc


def _load_legacy_gemini_sdk() -> Any:
    try:
        return importlib.import_module("google.generativeai")
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise TranslationError("Legacy Gemini SDK is not installed.") from exc


def _pre_split_long_lines(
    lines: list[TranscriptLine],
    *,
    max_line_duration_ms: int,
    max_line_chars: int,
    min_subline_duration_ms: int,
) -> list[TranscriptLine]:
    if not lines:
        return []

    split_lines: list[TranscriptLine] = []
    for line in lines:
        normalized_text = re.sub(r"\s+", " ", line.source_text).strip()
        line_duration_ms = max(0, line.end_ms - line.start_ms)
        if (
            not normalized_text
            or (
                line_duration_ms <= max_line_duration_ms
                and len(normalized_text) <= max_line_chars
            )
        ):
            split_lines.append(
                TranscriptLine(
                    index=0,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    speaker_id=line.speaker_id,
                    speaker_label=line.speaker_label,
                    source_text=normalized_text or line.source_text,
                )
            )
            continue

        chunks = _split_transcript_text_recursively(
            normalized_text,
            total_duration_ms=line_duration_ms,
            max_line_duration_ms=max_line_duration_ms,
            max_line_chars=max_line_chars,
        )
        chunks = _merge_short_transcript_chunks(
            chunks,
            total_duration_ms=line_duration_ms,
            min_subline_duration_ms=min_subline_duration_ms,
        )
        boundaries = _allocate_chunk_boundaries(
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            chunks=chunks,
        )
        for chunk_text, start_ms, end_ms in boundaries:
            split_lines.append(
                TranscriptLine(
                    index=0,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=line.speaker_id,
                    speaker_label=line.speaker_label,
                    source_text=chunk_text,
                )
            )

    return [
        TranscriptLine(
            index=index,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=line.speaker_id,
            speaker_label=line.speaker_label,
            source_text=line.source_text,
        )
        for index, line in enumerate(split_lines, start=1)
    ]


def _split_transcript_text_recursively(
    text: str,
    *,
    total_duration_ms: int,
    max_line_duration_ms: int,
    max_line_chars: int,
) -> list[str]:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return []
    if total_duration_ms <= max_line_duration_ms and len(normalized_text) <= max_line_chars:
        return [normalized_text]

    for pattern in (_STRONG_LINE_SPLIT_PATTERN, _WEAK_LINE_SPLIT_PATTERN):
        pieces = _split_text_with_pattern(normalized_text, pattern)
        if len(pieces) <= 1:
            continue

        total_weight = sum(_text_weight(piece) for piece in pieces) or len(pieces)
        split_chunks: list[str] = []
        for piece in pieces:
            piece_weight = _text_weight(piece)
            piece_duration_ms = 0
            if total_duration_ms > 0 and total_weight > 0:
                piece_duration_ms = round(total_duration_ms * piece_weight / total_weight)
            split_chunks.extend(
                _split_transcript_text_recursively(
                    piece,
                    total_duration_ms=piece_duration_ms,
                    max_line_duration_ms=max_line_duration_ms,
                    max_line_chars=max_line_chars,
                )
            )
        return split_chunks

    return _hard_split_transcript_text(
        normalized_text,
        max_line_chars=max_line_chars,
        total_duration_ms=total_duration_ms,
        max_line_duration_ms=max_line_duration_ms,
    )


def _split_text_with_pattern(text: str, pattern: re.Pattern[str]) -> list[str]:
    return [piece.strip() for piece in pattern.split(text) if piece.strip()]


def _hard_split_transcript_text(
    text: str,
    *,
    max_line_chars: int,
    total_duration_ms: int,
    max_line_duration_ms: int,
) -> list[str]:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return []

    target_chunk_count = max(
        1,
        _ceil_div(len(normalized_text), max_line_chars),
        _ceil_div(max(total_duration_ms, 1), max_line_duration_ms),
    )
    target_chunk_chars = max(1, _ceil_div(len(normalized_text), target_chunk_count))

    words = normalized_text.split(" ")
    if len(words) <= 1:
        return [
            normalized_text[index:index + target_chunk_chars].strip()
            for index in range(0, len(normalized_text), target_chunk_chars)
            if normalized_text[index:index + target_chunk_chars].strip()
        ]

    chunks: list[str] = []
    current_words: list[str] = []
    current_length = 0
    for word in words:
        additional_length = len(word) if not current_words else len(word) + 1
        if current_words and current_length + additional_length > target_chunk_chars:
            chunks.append(" ".join(current_words))
            current_words = [word]
            current_length = len(word)
            continue
        current_words.append(word)
        current_length += additional_length

    if current_words:
        chunks.append(" ".join(current_words))
    return chunks


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 1
    return max(1, (numerator + denominator - 1) // denominator)


def _merge_short_transcript_chunks(
    chunks: list[str],
    *,
    total_duration_ms: int,
    min_subline_duration_ms: int,
) -> list[str]:
    merged_chunks = [chunk for chunk in chunks if chunk.strip()]
    if len(merged_chunks) <= 1 or total_duration_ms <= 0:
        return merged_chunks

    while len(merged_chunks) > 1:
        weights = [_text_weight(chunk) for chunk in merged_chunks]
        total_weight = sum(weights) or len(merged_chunks)
        durations = [
            round(total_duration_ms * weight / total_weight)
            for weight in weights
        ]
        short_index = next(
            (index for index, duration_ms in enumerate(durations) if duration_ms < min_subline_duration_ms),
            None,
        )
        if short_index is None:
            break
        if short_index == 0:
            merged_chunks[1] = _merge_text_chunks(merged_chunks[0], merged_chunks[1])
            del merged_chunks[0]
            continue
        merged_chunks[short_index - 1] = _merge_text_chunks(
            merged_chunks[short_index - 1],
            merged_chunks[short_index],
        )
        del merged_chunks[short_index]
    return merged_chunks


def _merge_text_chunks(left: str, right: str) -> str:
    return re.sub(r"\s+", " ", f"{left.strip()} {right.strip()}").strip()


def _allocate_chunk_boundaries(
    *,
    start_ms: int,
    end_ms: int,
    chunks: list[str],
) -> list[tuple[str, int, int]]:
    if not chunks:
        return []
    if len(chunks) == 1:
        return [(chunks[0], start_ms, end_ms)]

    total_duration_ms = max(0, end_ms - start_ms)
    weights = [_text_weight(chunk) for chunk in chunks]
    total_weight = sum(weights) or len(chunks)
    allocated: list[tuple[str, int, int]] = []
    current_start_ms = start_ms
    consumed_weight = 0

    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        if index == len(chunks) - 1:
            current_end_ms = end_ms
        else:
            consumed_weight += weight
            current_end_ms = start_ms + round(total_duration_ms * consumed_weight / total_weight)
            current_end_ms = max(current_end_ms, current_start_ms)
        allocated.append((chunk, current_start_ms, current_end_ms))
        current_start_ms = current_end_ms
    return allocated


def _text_weight(text: str) -> int:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return 1
    alnum_weight = len(re.sub(r"[^A-Za-z0-9]+", "", normalized_text))
    return max(1, alnum_weight or len(normalized_text))


def _build_probe_groups(lines: list[TranscriptLine]) -> list[dict[str, object]]:
    """Build simplified translation groups for probe segments (no char estimates).

    Probe groups deliberately omit min_chars/max_chars to avoid the 4.5 chars/sec
    assumption polluting the calibration. The LLM translates by feel, guided only
    by target_duration_seconds.
    """
    groups: list[dict[str, object]] = []
    for segment_id, line in enumerate(lines, start=1):
        target_duration_ms = max(0, line.end_ms - line.start_ms)
        groups.append(
            {
                "segment_id": segment_id,
                "speaker_id": line.speaker_id,
                "target_duration_seconds": round(target_duration_ms / 1000, 1),
                "source_text": line.source_text,
            }
        )
    return groups


def _build_groups(
    lines: list[TranscriptLine],
    *,
    max_segment_duration_ms: int,
    chars_per_second: float = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
    chars_per_second_by_speaker: dict[str, float] | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> list[dict[str, object]]:
    """Build translation groups from transcript lines.

    1:1 mapping — each transcript line becomes one translation group.
    No merging. Transcript stage (5-layer split) already handles segmentation.
    AssemblyAI utterances are already "one person's continuous speech",
    so even short segments like "Yeah, sure." are complete utterances.

    When chars_per_second / chars_per_second_by_speaker are provided (from probe
    TTS calibration), they replace the default 4.5 assumption for target char
    estimation.
    """
    if not lines:
        return []

    # Language-pair length profile (default en->zh-CN → ratio 1.8 / Latin source,
    # i.e. the exact legacy numbers, so target_chars/min/max and the translation
    # fingerprint are byte-identical for the default pair).
    _lp_profile = resolve_language_pair(source_language, target_language) or DEFAULT_LANGUAGE_PAIR_PROFILE
    _length_ratio = _lp_profile.natural_length_ratio
    _src_desc = get_language_descriptor(source_language)
    _source_script = _src_desc.script_family if _src_desc is not None else "latin"

    groups: list[dict[str, object]] = []
    for segment_id, line in enumerate(lines, start=1):
        start_ms = line.start_ms
        end_ms = line.end_ms
        target_duration_ms = max(0, end_ms - start_ms)
        groups.append(
            {
                "segment_id": segment_id,
                "speaker_id": line.speaker_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "target_duration_ms": target_duration_ms,
                "target_duration_seconds": round(target_duration_ms / 1000, 1),
                "source_text": line.source_text,
                "dubbing_mode": normalize_dubbing_mode(
                    getattr(line, "dubbing_mode", DUBBING_MODE_DUB)
                ),
            }
        )
    if not groups:
        return []

    effective_cps_by_speaker = chars_per_second_by_speaker or {}

    for group in groups:
        source_text = str(group["source_text"])
        source_word_count = _count_source_words(source_text, _source_script)
        target_duration_ms = int(group["target_duration_ms"])
        source_words_per_second = 0.0
        if target_duration_ms > 0 and source_word_count > 0:
            source_words_per_second = source_word_count / (target_duration_ms / 1000)
        group["source_word_count"] = source_word_count
        group["source_words_per_second"] = round(source_words_per_second, 3)

    global_reference_words_per_second = _estimate_reference_words_per_second(groups)
    speaker_reference_words_per_second = _estimate_speaker_reference_words_per_second(groups)

    for group in groups:
        source_words_per_second = float(group["source_words_per_second"])
        target_duration_ms = int(group["target_duration_ms"])
        speaker_id = str(group["speaker_id"])
        reference_words_per_second = speaker_reference_words_per_second.get(
            speaker_id,
            global_reference_words_per_second,
        )
        reference_source = "speaker" if speaker_id in speaker_reference_words_per_second else "global"
        density_factor, density_factor_source = _estimate_density_factor(
            source_words_per_second=source_words_per_second,
            reference_words_per_second=reference_words_per_second,
            reference_source=reference_source,
        )
        # Use speaker-specific calibrated chars/sec if available, else global
        effective_cps = effective_cps_by_speaker.get(speaker_id, chars_per_second)
        # Plan C: target_chars uses source_word_count × 1.8 (natural CN length),
        # not voice_cps × duration. The voice_cps is still passed so the
        # legacy fallback works when source_word_count is missing.
        source_word_count = int(group.get("source_word_count") or 0)
        target_chars = _estimate_dynamic_target_chars(
            target_duration_ms=target_duration_ms,
            density_factor=density_factor,
            chars_per_second=effective_cps,
            source_word_count=source_word_count,
            ratio=_length_ratio,
        )
        min_chars, max_chars = _estimate_target_char_range(target_chars)
        group["reference_words_per_second"] = round(reference_words_per_second, 3)
        group["density_factor_source"] = density_factor_source
        group["density_factor"] = round(density_factor, 3)
        group["dynamic_target_chars"] = target_chars
        group["target_chars"] = target_chars
        group["min_chars"] = min_chars
        group["max_chars"] = max_chars
        # Phase 1 (translation-duration-alignment): natural-length hint.
        # target_chars_hint = English word count × 1.8 (empirical zh/en ratio).
        # This is a SOFT reference for "natural Chinese length given the
        # original content density", NOT a hard constraint. min/max_chars
        # remain the binding duration envelope.
        source_word_count = int(group.get("source_word_count") or 0)
        group["target_chars_hint"] = max(1, int(round(source_word_count * _length_ratio)))
        # Record the effective chars/sec used to derive min/max, so the LLM
        # can see why the envelope is what it is. Comes from either the
        # voice_catalog pre-calibrated value (Phase 1) or probe calibration.
        group["voice_chars_per_second"] = round(float(effective_cps), 3)
    return groups


def _light_merge_short_segments(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    """Merge very short segments (<5s) into adjacent same-speaker segments.

    Only merges if:
    - The short segment has the same speaker as its neighbor
    - The merged result would not exceed 45 seconds
    Normal segments (≥5s) are never touched.
    """
    if len(lines) <= 1:
        return list(lines)

    result: list[TranscriptLine] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        dur = line.end_ms - line.start_ms

        # If this segment is short and same speaker as previous, merge into previous
        if (
            dur < _MIN_STANDALONE_SEGMENT_MS
            and result
            and result[-1].speaker_id == line.speaker_id
            and (result[-1].end_ms - result[-1].start_ms + dur) <= 45_000
        ):
            prev = result[-1]
            result[-1] = TranscriptLine(
                index=prev.index,
                start_ms=prev.start_ms,
                end_ms=line.end_ms,
                speaker_id=prev.speaker_id,
                speaker_label=prev.speaker_label,
                source_text=prev.source_text + " " + line.source_text,
            )
        # If this segment is short and same speaker as NEXT, merge into next
        elif (
            dur < _MIN_STANDALONE_SEGMENT_MS
            and i + 1 < len(lines)
            and lines[i + 1].speaker_id == line.speaker_id
            and (dur + lines[i + 1].end_ms - lines[i + 1].start_ms) <= 45_000
        ):
            next_line = lines[i + 1]
            result.append(TranscriptLine(
                index=len(result) + 1,
                start_ms=line.start_ms,
                end_ms=next_line.end_ms,
                speaker_id=line.speaker_id,
                speaker_label=line.speaker_label,
                source_text=line.source_text + " " + next_line.source_text,
            ))
            i += 2  # skip next line (already merged)
            continue
        else:
            result.append(line)
        i += 1

    # Re-index
    for idx, line in enumerate(result):
        result[idx] = TranscriptLine(
            index=idx + 1,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=line.speaker_id,
            speaker_label=line.speaker_label,
            source_text=line.source_text,
        )
    return result


def _split_lines_by_speaker_and_pause(
    lines: list[TranscriptLine],
    *,
    same_speaker_pause_split_ms: int,
) -> list[list[TranscriptLine]]:
    if not lines:
        return []

    current_group: list[TranscriptLine] = []
    speaker_groups: list[list[TranscriptLine]] = []

    normalized_pause_split_ms = max(0, int(same_speaker_pause_split_ms))

    for line in lines:
        if not current_group:
            current_group.append(line)
            continue

        previous_line = current_group[-1]
        pause_ms = max(0, line.start_ms - previous_line.end_ms)
        if (
            line.speaker_id != previous_line.speaker_id
            or pause_ms >= normalized_pause_split_ms
        ):
            speaker_groups.append(current_group)
            current_group = [line]
            continue
        current_group.append(line)
    if current_group:
        speaker_groups.append(current_group)
    return speaker_groups


def _split_group_by_duration(
    lines: list[TranscriptLine],
    *,
    max_segment_duration_ms: int,
) -> list[list[TranscriptLine]]:
    if not lines:
        return []

    chunks: list[list[TranscriptLine]] = []
    current_chunk: list[TranscriptLine] = []

    for line in lines:
        if not current_chunk:
            current_chunk.append(line)
            continue

        projected_duration_ms = max(0, line.end_ms - current_chunk[0].start_ms)
        if projected_duration_ms > max_segment_duration_ms:
            chunks.append(current_chunk)
            current_chunk = [line]
            continue
        current_chunk.append(line)

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _merge_source_text(lines: list[TranscriptLine]) -> str:
    return " ".join(line.source_text.strip() for line in lines if line.source_text.strip()).strip()


def _estimate_target_chars(
    target_duration_ms: int,
    chars_per_second: float = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
) -> int:
    return max(1, int(target_duration_ms / 1000 * chars_per_second))


def _count_source_words(source_text: str, source_script: str = "latin") -> int:
    """Count source spoken units. Latin (default) → word-like tokens
    (byte-identical to the legacy English count); CJK → per-ideograph count, so a
    Chinese source produces a non-zero count instead of ~0 (which would collapse
    the whole length budget)."""
    if source_script == "cjk":
        han = sum(1 for ch in (source_text or "") if "一" <= ch <= "鿿")
        # Mixed-script CJK transcripts carry Latin terms / numbers (OpenAI, GPT-4,
        # an "OK" backchannel) that are spoken source units too. Count them so the
        # English word budget isn't undercounted, and a Latin-only segment isn't ~0
        # (which would collapse the budget to the duration fallback). (re-CodeX P2)
        latin_tokens = len(re.findall(r"[A-Za-z0-9']+", source_text or ""))
        return han + latin_tokens
    return len(re.findall(r"[A-Za-z0-9']+", source_text))


def _estimate_reference_words_per_second(groups: list[dict[str, object]]) -> float:
    words_per_second_values: list[float] = []
    for group in groups:
        source_words_per_second = float(group.get("source_words_per_second") or 0.0)
        if source_words_per_second > 0:
            words_per_second_values.append(source_words_per_second)
    if not words_per_second_values:
        return 0.0
    return float(statistics.median(words_per_second_values))


def _estimate_speaker_reference_words_per_second(
    groups: list[dict[str, object]],
    *,
    minimum_samples: int = DEFAULT_SPEAKER_REFERENCE_MIN_SAMPLES,
) -> dict[str, float]:
    speaker_to_values: dict[str, list[float]] = {}
    for group in groups:
        speaker_id = str(group.get("speaker_id") or "").strip()
        source_words_per_second = float(group.get("source_words_per_second") or 0.0)
        if not speaker_id or source_words_per_second <= 0:
            continue
        speaker_to_values.setdefault(speaker_id, []).append(source_words_per_second)

    speaker_references: dict[str, float] = {}
    for speaker_id, values in speaker_to_values.items():
        if len(values) < minimum_samples:
            continue
        speaker_references[speaker_id] = float(statistics.median(values))
    return speaker_references


def _estimate_density_factor(
    *,
    source_words_per_second: float,
    reference_words_per_second: float,
    reference_source: str,
) -> tuple[float, str]:
    """Plan C (2026-04-15): density adjustment is deprecated.

    Why: Old behaviour scaled char budget by source_wps / reference_wps and
    clamped to [0.65, 1.50].  The intent was "match information density",
    but this conflicts with the dub-time-alignment hard constraint —
    slow segments would get their char budget cut to 65-75% (Munger
    segment_029: 126 → 95 chars), making TTS finish 24% short and
    triggering pre-TTS rewrite that just expanded back to ~130 chars.
    A wasted LLM round-trip with no real benefit.

    The function still exists for compatibility but always returns 1.0.
    Caller's ``reference_source`` is preserved for telemetry.
    """
    return 1.0, reference_source


# Empirical English-word → Chinese-hanzi ratio for natural translation length.
# Spans most factual/conversational content; Plan C uses this directly as the
# canonical char budget instead of duration × voice_cps × density.
_ENGLISH_TO_CHINESE_CHAR_RATIO: float = 1.8


def _estimate_dynamic_target_chars(
    *,
    target_duration_ms: int,
    density_factor: float,
    chars_per_second: float = DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND,
    source_word_count: int = 0,
    ratio: float = _ENGLISH_TO_CHINESE_CHAR_RATIO,
) -> int:
    """Plan C: target_chars = source_word_count × 1.8 (×density), independent of voice_cps.

    The ``chars_per_second`` argument is kept in the signature for backwards
    compatibility (probe path, legacy callers) and is used as the fallback
    basis when ``source_word_count`` is unknown / zero.

    Under Plan C, production callers always pass ``source_word_count`` from
    the source line, and ``density_factor`` is always 1.0 — yielding the
    "natural Chinese length" that tracks information content rather than
    voice physical speed.
    """
    if source_word_count > 0:
        natural_chars = source_word_count * ratio
        return max(1, int(round(natural_chars * density_factor)))
    # Fallback: probe groups (no source_word_count yet) or empty text.
    base_target_chars = _estimate_target_chars(target_duration_ms, chars_per_second)
    return max(1, int(base_target_chars * density_factor))


def _get_char_range_factors() -> tuple[float, float]:
    """Load char range factors from admin settings, with defaults 0.85 / 1.15."""
    try:
        import os as _os
        settings_path = str(
            Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config"))
            / "admin_settings.json"
        )
        if _os.path.exists(settings_path):
            with open(settings_path) as f:
                settings = json.load(f)
            min_f = float(settings.get("translation_char_range_min_factor", 0.85))
            max_f = float(settings.get("translation_char_range_max_factor", 1.15))
            if 0.5 <= min_f <= 1.0 and 1.0 <= max_f <= 2.0:
                return min_f, max_f
    except Exception:
        pass
    return 0.85, 1.15


def _estimate_target_char_range(target_chars: int) -> tuple[int, int]:
    normalized_target_chars = max(1, int(target_chars))
    min_factor, max_factor = _get_char_range_factors()
    min_chars = max(1, int(normalized_target_chars * min_factor))
    max_chars = max(min_chars, int(normalized_target_chars * max_factor))
    return min_chars, max_chars


def _resolve_segment_voice_assignment(
    *,
    speaker_id: str,
    voice_id: str,
    display_name: str,
    voice_id_b: str | None,
    display_name_b: str,
    speaker_voices: dict[str, str] | None = None,
) -> tuple[str, str]:
    normalized_speaker_id = speaker_id.strip().lower()
    # N-speaker support: if speaker_voices dict provided, look up by speaker_id
    if speaker_voices and normalized_speaker_id in speaker_voices:
        resolved_voice = speaker_voices[normalized_speaker_id]
        # Generate display name from speaker_id (speaker_c → Speaker C)
        suffix = normalized_speaker_id.replace("speaker_", "")
        resolved_display = f"Speaker {suffix.upper()}" if len(suffix) == 1 else speaker_id
        return resolved_voice, resolved_display
    # Legacy 2-speaker path
    if normalized_speaker_id == "speaker_b":
        return (voice_id_b or "auto"), display_name_b
    return voice_id, display_name


def _speaker_label_from_id(speaker_id: str) -> str:
    normalized = speaker_id.strip().lower()
    if normalized == "speaker_a":
        return "Speaker A"
    if normalized == "speaker_b":
        return "Speaker B"
    return speaker_id


def _default_speaker_names(num_speakers: int) -> dict[str, str]:
    normalized_num_speakers = max(1, int(num_speakers))
    speaker_names: dict[str, str] = {}
    for index in range(normalized_num_speakers):
        suffix = chr(ord("a") + index)
        speaker_names[f"speaker_{suffix}"] = f"Speaker {suffix.upper()}"
    return speaker_names


def _extract_response_text(response: Any) -> str:
    response_text = _normalize_optional_text(getattr(response, "text", None))
    if response_text is None:
        raise TranslationError("Gemini returned an empty response.")
    return response_text


def _strip_markdown_code_fence(response_text: str) -> str:
    stripped = response_text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _should_retry_same_alias_before_fallback(exc: Exception) -> bool:
    message = str(exc).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "connectionpool",
        "ssl",
        "eof occurred",
        "temporarily unavailable",
        "server disconnected",
        "remote end closed connection",
        "503",
        "502",
        "504",
        "429",
        "rate limit",
        "too many requests",
    )
    return any(marker in message for marker in transient_markers)


def _maybe_write_translation_quality_report(
    output_root: Path,
    result: TranslationResult,
    *,
    glossary: dict[str, str],
    target_language: str | None = None,
) -> None:
    project_dir = output_root.parent if output_root.name == "translation" else output_root
    try:
        from services.translation_quality import write_translation_quality_report

        write_translation_quality_report(
            project_dir,
            segments=result.segments,
            glossary=glossary,
            target_language=target_language,
        )
    except Exception as exc:
        print(
            f"[S3] translation quality report write skipped (non-fatal): {exc}",
            flush=True,
        )


def _normalize_sdk_backend(value: object) -> str:
    normalized = (_normalize_optional_text(value) or DEFAULT_SDK_BACKEND).lower()
    if normalized in {"legacy", "old", LEGACY_SDK_BACKEND}:
        return LEGACY_SDK_BACKEND
    return DEFAULT_SDK_BACKEND


def _normalize_max_output_tokens(value: object) -> int:
    coerced = _coerce_optional_int(value)
    normalized = DEFAULT_MAX_OUTPUT_TOKENS if coerced is None else coerced
    return min(
        max(1, normalized),
        GEMINI_MAX_OUTPUT_TOKENS_EXCLUSIVE_UPPER_BOUND - 1,
    )


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Phase 2 Task 0 — glossary preservation check
# ---------------------------------------------------------------------------

def check_glossary_preservation(
    segments: list[DubbingSegment],
    glossary: dict[str, str] | None,
) -> dict[str, object]:
    """Verify that each glossary value (Chinese term) appears at least once
    across the translated cn_text of all segments.

    The S3 prompt injects glossary as a hard constraint, but the LLM can
    drop or alter terms. This post-translation check produces a metric
    rather than blocking the pipeline — operators can spot regressions
    via the admin job monitor.

    Returns a dict with three keys:
      - total_terms (int): how many terms the glossary contains
      - preserved_terms (int): how many of those Chinese values appear
          in at least one segment's cn_text
      - missing_terms (list[str]): the Chinese values that never showed up
          (capped at first 20 to keep payload small)

    When the glossary is missing/empty, returns total=0 / preserved=0 /
    missing=[] so the caller can compute a safe rate (1.0).
    """
    if not glossary:
        return {"total_terms": 0, "preserved_terms": 0, "missing_terms": []}

    # Collect Chinese values (the right-hand side of "English term → 中文译名")
    targets = [str(v).strip() for v in glossary.values() if v and str(v).strip()]
    if not targets:
        return {"total_terms": 0, "preserved_terms": 0, "missing_terms": []}

    # Concatenate all cn_text for whole-document scanning. Cheap O(N*M) where
    # N=#segments, M=#terms; both small in practice (<100 each).
    all_cn = "".join((seg.cn_text or "") for seg in segments)

    preserved = 0
    missing: list[str] = []
    for term in targets:
        if term in all_cn:
            preserved += 1
        else:
            missing.append(term)

    return {
        "total_terms": len(targets),
        "preserved_terms": preserved,
        "missing_terms": missing[:20],
    }
