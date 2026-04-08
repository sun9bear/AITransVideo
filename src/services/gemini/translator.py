from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import statistics
import time
from typing import Any

from services.assemblyai.transcriber import TranscriptLine
from services.llm import LLMProviderError, LLMRouter


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_MODEL_NAME = "gemini-3.1-pro-preview"
DEFAULT_SDK_BACKEND = "google-genai"
LEGACY_SDK_BACKEND = "google-generativeai"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_OUTPUT_TOKENS = 8192
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
DEFAULT_DYNAMIC_DENSITY_MIN = 0.65
DEFAULT_DYNAMIC_DENSITY_MAX = 1.50
DEFAULT_SPEAKER_REFERENCE_MIN_SAMPLES = 3
DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT_FACTOR = 0.5
DEFAULT_TRANSLATION_LENGTH_OVERSHOOT_FACTOR = 2.0
DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK = 1
SPEAKER_INFER_PROMPT_TEMPLATE_CONTEXT_TOKEN = "__CONTEXT_EXCERPT__"
SPEAKER_INFER_PROMPT_TEMPLATE_EXPECTED_OUTPUT_TOKEN = "__EXPECTED_OUTPUT_JSON__"
TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN = "__GROUPS_JSON__"
TRANSLATION_PROMPT_TEMPLATE_VIDEO_TITLE_TOKEN = "__VIDEO_TITLE__"
TRANSLATION_PROMPT_TEMPLATE_YOUTUBE_URL_TOKEN = "__YOUTUBE_URL__"
TRANSLATION_PROMPT_TEMPLATE_SPEAKER_INSTRUCTION_TOKEN = "__SPEAKER_INSTRUCTION__"
TRANSLATION_PROMPT_TEMPLATE_STRICT_LENGTH_TOKEN = "__STRICT_LENGTH_INSTRUCTION__"
TRANSLATION_PROMPT_TEMPLATE_GLOSSARY_TOKEN = "__GLOSSARY_SECTION__"
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
__SPEAKER_INSTRUCTION____STRICT_LENGTH_INSTRUCTION__补充要求：在不影响自然度的前提下，可适度保留原文中的口语连接词、语气词和缓冲表达，以维持更接近原说话节奏；但不要为了凑字数生硬添加无意义填充词。
9. 每个 segment 独立翻译，但要保持上下文连贯。
10. 只输出 JSON，不要任何其他文字。

每个 segment 都提供了：
- target_duration_seconds：原文段落时长（秒），中文配音时长应尽量接近
- min_chars ~ max_chars：建议中文字数范围（仅供参考，不是硬性约束）

输入（JSON数组）：
__GROUPS_JSON__

请输出JSON数组，格式如下（只输出JSON，不要markdown代码块）：
[
  {
    "segment_id": 1,
    "cn_text": "翻译后的中文文本"
  }
]"""
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

_STRONG_LINE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
_WEAK_LINE_SPLIT_PATTERN = re.compile(r"(?<=[,;])\s+")


class TranslationError(Exception):
    pass


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
    tts_cn_text: str = ""
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
        self.max_output_tokens = int(max_output_tokens)
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
    ) -> TranslationResult:
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
        groups = _build_groups(lines, max_segment_duration_ms=max_segment_duration_ms)

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
            return result

        fingerprint = self._build_translation_fingerprint(
            groups,
            video_title=video_title,
            youtube_url=youtube_url,
        )
        translated_items = self._load_translation_checkpoint(
            checkpoint_path,
            expected_fingerprint=fingerprint,
        )
        if len(translated_items) > len(groups):
            translated_items = []

        if translated_items:
            print(f"[S3] 检测到翻译断点，恢复 {len(translated_items)}/{len(groups)} 段")

        for batch_start in range(len(translated_items), len(groups), DEFAULT_BATCH_SIZE):
            batch = groups[batch_start:batch_start + DEFAULT_BATCH_SIZE]
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
                total_groups=len(groups),
            )
            completed_count = min(batch_start + DEFAULT_BATCH_SIZE, len(groups))
            print(f"[S3] 翻译进度：{completed_count}/{len(groups)} 段")

        for index, item in enumerate(translated_items, start=1):
            item["segment_id"] = index

        segments: list[DubbingSegment] = []
        for index, (group, translated_item) in enumerate(zip(groups, translated_items), start=1):
            start_ms = int(group["start_ms"])
            end_ms = int(group["end_ms"])
            speaker_id = str(group["speaker_id"])
            normalized_cn_text = str(translated_item["cn_text"]).strip()
            segment_voice_id, segment_display_name = _resolve_segment_voice_assignment(
                speaker_id=speaker_id,
                voice_id=normalized_voice_id,
                display_name=normalized_display_name,
                voice_id_b=normalized_voice_id_b,
                display_name_b=normalized_display_name_b,
                speaker_voices=speaker_voices,
            )
            segments.append(
                DubbingSegment(
                    segment_id=index,
                    speaker_id=speaker_id,
                    display_name=segment_display_name,
                    voice_id=segment_voice_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    target_duration_ms=max(0, end_ms - start_ms),
                    source_text=str(group["source_text"]),
                    cn_text=normalized_cn_text,
                    tts_cn_text=normalized_cn_text,
                )
            )

        result = TranslationResult(
            segments=segments,
            total_segments=len(segments),
            output_path=str(output_path),
        )
        _write_json(output_path, asdict(result))
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        return result

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
                }
                for group in groups
            ],
        }
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

        normalized_items: list[dict[str, object]] = []
        for expected_segment_id, item in enumerate(translated_items, start=1):
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

    def _call_task_with_fallback(
        self,
        task: str,
        prompt: str,
        *,
        json_mode: bool = False,
        validator: Any | None = None,
    ) -> str:
        route = self.llm_router.get_route(task) if self.llm_router is not None else ["default_llm"]
        if not route:
            route = ["default_llm"]

        last_error: Exception | None = None
        for index, alias in enumerate(route):
            model_config = self.llm_router.get_model_config(alias) if self.llm_router is not None else {}
            provider_name = _normalize_optional_text(model_config.get("provider"))
            override_model_name = _normalize_optional_text(model_config.get("model_name"))
            allow_same_alias_retry = (
                provider_name not in {None, "gemini"}
                and alias not in {"gemini_current", "default_llm"}
            )
            for retry_attempt in range(DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK + 1):
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
                        print(f"[LLM] {task} using {alias}")
                        response_text = self.llm_router.generate_via_alias(
                            alias,
                            prompt=prompt,
                            json_mode=json_mode,
                        )

                    if validator is not None:
                        validator(response_text)
                    return response_text
                except (TranslationError, LLMProviderError) as exc:
                    last_error = exc
                    should_retry_same_alias = (
                        allow_same_alias_retry
                        and retry_attempt < DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK
                        and _should_retry_same_alias_before_fallback(exc)
                    )
                    if should_retry_same_alias:
                        wait_seconds = 3 * (retry_attempt + 1)
                        print(
                            f"[LLM] {task} {alias} transient failure, retrying same model "
                            f"in {wait_seconds}s ({retry_attempt + 1}/{DEFAULT_ALIAS_RETRY_ATTEMPTS_BEFORE_FALLBACK}): {exc}"
                        )
                        time.sleep(wait_seconds)
                        continue
                    break
            if index >= len(route) - 1:
                break
            next_alias = route[index + 1]
            print(f"[LLM] {task} {alias} failed, falling back to {next_alias}: {last_error}")

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

    def _build_prompt(
        self,
        groups: list[dict],
        *,
        video_title: str = "",
        youtube_url: str = "",
        glossary: dict[str, str] | None = None,
        strict_length_control: bool = False,
    ) -> str:
        groups_json = json.dumps(groups, ensure_ascii=False, indent=2)
        strict_length_instruction = (
            "12. Length reminder: the previous attempt missed the requested range. Keep this retry much closer to min_chars ~ max_chars.\n"
            if strict_length_control
            else ""
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
            self.translation_prompt_template
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
                "cn_text": _normalize_optional_text(item.get("cn_text")) or "",
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
        lower_bound = max(1, int(min_chars * DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT_FACTOR))
        upper_bound = max(lower_bound, int(max_chars * DEFAULT_TRANSLATION_LENGTH_OVERSHOOT_FACTOR))
        return actual_chars < lower_bound or actual_chars > upper_bound

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
        "max_output_tokens": _coerce_int(section.get("max_output_tokens"), default=DEFAULT_MAX_OUTPUT_TOKENS),
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
        return DEFAULT_TRANSLATION_PROMPT_TEMPLATE
    return validate_translation_prompt_template(normalized_template)


def validate_translation_prompt_template(template: str) -> str:
    return _validate_prompt_template_tokens(
        template,
        required_tokens=(TRANSLATION_PROMPT_TEMPLATE_GROUPS_TOKEN,),
        label="S3 翻译提示词",
    )


def get_effective_rewrite_prompt_template(template: object | None = None) -> str:
    normalized_template = _normalize_optional_text(template)
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


def _build_groups(lines: list[TranscriptLine], *, max_segment_duration_ms: int) -> list[dict[str, object]]:
    """Build translation groups from transcript lines.

    1:1 mapping — each transcript line becomes one translation group.
    No merging. Transcript stage (5-layer split) already handles segmentation.
    AssemblyAI utterances are already "one person's continuous speech",
    so even short segments like "Yeah, sure." are complete utterances.
    """
    if not lines:
        return []

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
            }
        )
    if not groups:
        return []

    for group in groups:
        source_text = str(group["source_text"])
        source_word_count = _count_source_words(source_text)
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
        target_chars = _estimate_dynamic_target_chars(
            target_duration_ms=target_duration_ms,
            density_factor=density_factor,
        )
        min_chars, max_chars = _estimate_target_char_range(target_chars)
        group["reference_words_per_second"] = round(reference_words_per_second, 3)
        group["density_factor_source"] = density_factor_source
        group["density_factor"] = round(density_factor, 3)
        group["dynamic_target_chars"] = target_chars
        group["target_chars"] = target_chars
        group["min_chars"] = min_chars
        group["max_chars"] = max_chars
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


def _estimate_target_chars(target_duration_ms: int) -> int:
    return max(1, int(target_duration_ms / 1000 * DEFAULT_ESTIMATED_TTS_CHARS_PER_SECOND))


def _count_source_words(source_text: str) -> int:
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
    if source_words_per_second <= 0 or reference_words_per_second <= 0:
        return 1.0, "global"

    density_factor = source_words_per_second / reference_words_per_second
    density_factor = min(
        DEFAULT_DYNAMIC_DENSITY_MAX,
        max(DEFAULT_DYNAMIC_DENSITY_MIN, density_factor),
    )
    return density_factor, reference_source


def _estimate_dynamic_target_chars(
    *,
    target_duration_ms: int,
    density_factor: float,
) -> int:
    base_target_chars = _estimate_target_chars(target_duration_ms)
    return max(1, int(base_target_chars * density_factor))


def _estimate_target_char_range(target_chars: int) -> tuple[int, int]:
    normalized_target_chars = max(1, int(target_chars))
    min_chars = max(1, int(normalized_target_chars * 0.85))
    max_chars = max(min_chars, int(normalized_target_chars * 1.15))
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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {key: _to_jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_sdk_backend(value: object) -> str:
    normalized = (_normalize_optional_text(value) or DEFAULT_SDK_BACKEND).lower()
    if normalized in {"legacy", "old", LEGACY_SDK_BACKEND}:
        return LEGACY_SDK_BACKEND
    return DEFAULT_SDK_BACKEND


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object, *, default: int) -> int:
    coerced = _coerce_optional_int(value)
    return default if coerced is None else coerced


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
