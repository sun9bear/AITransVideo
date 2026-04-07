"""Gemini multimodal video transcriber.

Uses Gemini's video understanding to transcribe YouTube videos directly,
producing speaker-attributed transcript with timestamps. No audio upload needed.

Limitations:
- Short/medium videos only (< 30 minutes recommended)
- Timestamps are approximate (Gemini's video understanding, not ASR-grade)
- Speaker identification is context-based, not acoustic
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
from typing import Any

from services.assemblyai.transcriber import TranscriptLine, TranscriptResult


DEFAULT_GEMINI_TRANSCRIPTION_MODEL = "gemini-3.1-flash-lite-preview"

AVAILABLE_TRANSCRIPTION_MODELS = [
    {"alias": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash Lite", "input_price_per_mtok": 0.15, "output_price_per_mtok": 0.60},
    {"alias": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "input_price_per_mtok": 1.25, "output_price_per_mtok": 10.0},
]
DEFAULT_MAX_OUTPUT_TOKENS = 65536
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT_MS = 300_000  # 5 minutes for long videos
MAX_RECOMMENDED_DURATION_SECONDS = 1800  # 30 minutes

TRANSCRIPTION_PROMPT = """你是专业的视频转录专家。请观看这个视频，输出完整的英文转录稿，包含说话人标注和时间戳。

要求：
1. 识别视频中所有说话人，为每人分配 speaker_1, speaker_2 等编号
2. 如果能从上下文（如字幕、介绍）识别出说话人的真实姓名，请在 speakers 中填写
3. 按发言顺序输出每段话，每段包含精确的开始和结束时间（毫秒）
4. 转录必须是英文原文，不要翻译
5. 包含口语填充词（um, uh, you know 等）
6. 每段不要太长，按自然停顿分段，通常每段 5-30 秒

请严格按以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块或其他文字）：
{
  "speakers": [
    {"id": "speaker_1", "name": "Speaker Name or Unknown"}
  ],
  "segments": [
    {
      "speaker_id": "speaker_1",
      "start_ms": 0,
      "end_ms": 5200,
      "text": "The actual spoken English text..."
    }
  ],
  "total_duration_ms": 187000,
  "language": "en"
}"""


class GeminiTranscriptionError(Exception):
    pass


class GeminiTranscriber:
    """Transcribe YouTube videos using Gemini's multimodal video understanding."""

    def __init__(
        self,
        api_key: str,
        model_name: str = DEFAULT_GEMINI_TRANSCRIPTION_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        normalized_api_key = (api_key or "").strip()
        if not normalized_api_key:
            raise GeminiTranscriptionError("Gemini API key is required.")

        self.api_key = normalized_api_key
        self.model_name = model_name
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature

        from services.gemini.client_factory import create_gemini_client
        genai_module = _load_genai_sdk()
        self.client = create_gemini_client(api_key=normalized_api_key)
        self._genai = genai_module
        self._types = _load_genai_types()

    def transcribe(
        self,
        youtube_url: str,
        output_dir: str,
        speaker_labels: bool = True,
        speakers_expected: int | None = None,
    ) -> TranscriptResult:
        """Transcribe a YouTube video using Gemini multimodal API.

        Args:
            youtube_url: Public YouTube URL
            output_dir: Directory to write transcript output files
            speaker_labels: Whether to include speaker diarization
            speakers_expected: Hint for expected number of speakers

        Returns:
            TranscriptResult compatible with the existing pipeline
        """
        normalized_url = (youtube_url or "").strip()
        if not normalized_url:
            raise GeminiTranscriptionError("YouTube URL is required.")

        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        print(f"[S1] Gemini 多模态转录：{normalized_url}")
        print(f"[S1] 使用模型：{self.model_name}")

        prompt = self._build_prompt(speaker_labels, speakers_expected)
        video_part = self._build_video_part(normalized_url)

        raw_response = self._call_gemini(video_part, prompt)

        raw_response_path = output_root / "raw_gemini_transcript.json"
        raw_response_path.write_text(raw_response, encoding="utf-8")
        print(f"[S1] Gemini 原始响应已保存：{raw_response_path}")

        parsed = self._parse_response(raw_response)
        lines = self._build_transcript_lines(parsed)

        if not lines:
            raise GeminiTranscriptionError(
                "Gemini 返回了空的转录结果。可能视频无法访问或不包含可识别的语音内容。"
            )

        total_duration_ms = parsed.get("total_duration_ms", 0)
        if not total_duration_ms and lines:
            total_duration_ms = max(line.end_ms for line in lines)

        language = parsed.get("language", "en")

        result = TranscriptResult(
            lines=lines,
            total_duration_ms=int(total_duration_ms),
            language=language,
            raw_response_path=str(raw_response_path),
            structured_transcript_path=str(output_root / "transcript.json"),
        )

        structured_path = output_root / "transcript.json"
        structured_path.write_text(
            json.dumps(
                {"lines": [asdict(line) for line in lines]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(f"[S1] Gemini 转录完成：共 {len(lines)} 条，总时长 {total_duration_ms}ms")
        return result

    def _build_prompt(
        self,
        speaker_labels: bool,
        speakers_expected: int | None,
    ) -> str:
        prompt = TRANSCRIPTION_PROMPT
        if speakers_expected:
            prompt += f"\n\n提示：这个视频中预计有 {speakers_expected} 位说话人。"
        if not speaker_labels:
            prompt += "\n\n注意：不需要区分说话人，所有内容归属 speaker_1 即可。"
        return prompt

    def _build_video_part(self, youtube_url: str) -> Any:
        """Create a Gemini Part referencing a YouTube URL."""
        return self._types.Part(
            file_data=self._types.FileData(
                file_uri=youtube_url,
            )
        )

    def _call_gemini(self, video_part: Any, prompt: str) -> str:
        """Send video + prompt to Gemini and return raw response text."""
        config_class = getattr(
            self._types, "GenerateContentConfig", None
        )
        config_kwargs: dict[str, object] = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "response_mime_type": "application/json",
            "http_options": {"timeout": DEFAULT_TIMEOUT_MS},
        }
        config = config_class(**config_kwargs) if config_class else None

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[video_part, prompt],
                config=config,
            )
        except Exception as exc:
            raise GeminiTranscriptionError(
                f"Gemini 转录请求失败：{exc}"
            ) from exc

        text = _extract_response_text(response)
        if not text:
            raise GeminiTranscriptionError(
                "Gemini 返回了空响应。可能是视频时长超限或内容无法处理。"
            )
        return text

    def _parse_response(self, raw_response: str) -> dict[str, Any]:
        """Parse Gemini's JSON response into a structured dict."""
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiTranscriptionError(
                f"Gemini 返回的 JSON 格式无效：{exc}\n原始响应前 500 字：{raw_response[:500]}"
            ) from exc

        if not isinstance(parsed, dict):
            raise GeminiTranscriptionError(
                f"Gemini 返回格式不符合预期（应为 JSON 对象）：{type(parsed)}"
            )
        return parsed

    def _build_transcript_lines(self, parsed: dict[str, Any]) -> list[TranscriptLine]:
        """Convert parsed Gemini response to TranscriptLine list."""
        segments = parsed.get("segments", [])
        if not isinstance(segments, list):
            return []

        speakers_map = {}
        for speaker in parsed.get("speakers", []):
            if isinstance(speaker, dict):
                sid = str(speaker.get("id", "")).strip()
                name = str(speaker.get("name", "")).strip()
                if sid:
                    speakers_map[sid] = name or sid

        lines: list[TranscriptLine] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue

            text = str(segment.get("text", "")).strip()
            if not text:
                continue

            speaker_id_raw = str(segment.get("speaker_id", "speaker_1")).strip()
            speaker_label = speakers_map.get(speaker_id_raw, speaker_id_raw)

            normalized_speaker_id = _normalize_speaker_id(speaker_id_raw)

            lines.append(
                TranscriptLine(
                    index=index + 1,
                    start_ms=_coerce_int(segment.get("start_ms", 0)),
                    end_ms=_coerce_int(segment.get("end_ms", 0)),
                    speaker_id=normalized_speaker_id,
                    speaker_label=speaker_label,
                    source_text=text,
                )
            )

        return lines


def _normalize_speaker_id(raw_id: str) -> str:
    """Normalize speaker IDs to match AssemblyAI convention (speaker_a, speaker_b, ...)."""
    normalized = raw_id.lower().strip()
    match = re.search(r"(\d+)", normalized)
    if match:
        number = int(match.group(1))
        if number <= 26:
            letter = chr(ord("a") + number - 1)
            return f"speaker_{letter}"
    if normalized in {"speaker_a", "speaker_b", "speaker_c"}:
        return normalized
    return "speaker_a"


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _extract_response_text(response: Any) -> str:
    """Extract text from Gemini response object."""
    if hasattr(response, "text"):
        return str(response.text or "")
    if hasattr(response, "candidates") and response.candidates:
        candidate = response.candidates[0]
        if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
            parts = candidate.content.parts
            if parts:
                return str(parts[0].text or "")
    return ""


def _load_genai_sdk() -> Any:
    try:
        import google.genai as genai
        return genai
    except ImportError as exc:
        raise GeminiTranscriptionError(
            "google-genai SDK is required. Install with: pip install google-genai"
        ) from exc


def _load_genai_types() -> Any:
    try:
        import google.genai.types as types
        return types
    except ImportError as exc:
        raise GeminiTranscriptionError(
            "google-genai types module not found."
        ) from exc
