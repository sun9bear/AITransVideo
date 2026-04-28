from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_LANGUAGE_CODE = "en"
DEFAULT_SPEAKER_ID = "speaker_a"
DEFAULT_SPEAKER_LABEL = "A"
DEFAULT_MAX_RETRIES = 5
DEFAULT_HTTP_TIMEOUT_SECONDS = 900.0
DEFAULT_UPLOAD_OPTIMIZATION_THRESHOLD_MB = 50
DEFAULT_UPLOAD_MP3_BITRATE = "64k"
DEFAULT_UPLOAD_MP3_FRAME_RATE = 16_000
COMPRESSED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac"}
DEFAULT_SPEECH_MODELS = ["universal-3-pro", "universal-2"]
DEFAULT_TRANSCRIPTION_PROMPT = (
    'Include spoken filler words like "um," "uh," "you know," and "like" when they are clearly spoken. '
    "Preserve disfluencies, hesitations, repetitions, false starts, and self-corrections when they are audible."
)
SENTENCE_END_PATTERN = re.compile(r"[.?!;][\"')\]]*$")
ATTACHED_PUNCTUATION = {".", ",", "!", "?", ";", ":", ")", "]", "}", "%"}


class TranscriptionError(Exception):
    pass


@dataclass(slots=True)
class TranscriptLine:
    index: int
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_label: str
    source_text: str
    dubbing_mode: str = "dub"


@dataclass(slots=True)
class TranscriptResult:
    lines: list[TranscriptLine]
    total_duration_ms: int
    language: str
    raw_response_path: str
    structured_transcript_path: str


class AssemblyAITranscriber:
    def __init__(self, api_key: str, http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS):
        normalized_api_key = _normalize_optional_text(api_key)
        if normalized_api_key is None:
            raise TranscriptionError("AssemblyAI api_key is required.")

        self.api_key = normalized_api_key
        self._aai = _load_assemblyai_sdk()
        self._aai.settings.api_key = normalized_api_key
        self._aai.settings.http_timeout = _coerce_positive_float(
            http_timeout_seconds,
            default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )

    def transcribe(
        self,
        audio_path: str,
        output_dir: str,
        speaker_labels: bool = False,
        speakers_expected: int | None = None,
    ) -> TranscriptResult:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        config = _build_transcription_config(
            self._aai,
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
        )
        transcriber = self._aai.Transcriber()
        resolved_audio_path = str(Path(audio_path).resolve(strict=False))
        upload_path = self._resolve_upload_path(resolved_audio_path)

        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            try:
                transcript = self._submit_and_wait_for_transcript(
                    transcriber=transcriber,
                    upload_path=upload_path,
                    config=config,
                )
                break
            except Exception as exc:
                if attempt < DEFAULT_MAX_RETRIES:
                    wait_seconds = _retry_wait_seconds(attempt)
                    print(
                        f"[S1] AssemblyAI请求失败，{wait_seconds}秒后重试"
                        f"（{attempt + 1}/{DEFAULT_MAX_RETRIES}）: {exc}"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise TranscriptionError(
                    f"AssemblyAI转录失败（已重试{DEFAULT_MAX_RETRIES}次）: {exc}"
                ) from exc

        if str(getattr(transcript, "status", "")).strip().lower() == "error":
            error_message = _normalize_optional_text(getattr(transcript, "error", None)) or "unknown error"
            raise TranscriptionError(f"AssemblyAI transcription failed: {error_message}")

        raw_response_path = (output_root / "raw_assemblyai.json").resolve(strict=False)
        raw_payload = _extract_raw_payload(transcript)
        _write_json(raw_response_path, raw_payload)

        lines = _build_transcript_lines(transcript, speaker_labels=speaker_labels)
        structured_transcript_path = (output_root / "transcript.json").resolve(strict=False)
        result = TranscriptResult(
            lines=lines,
            total_duration_ms=_extract_total_duration_ms(transcript, raw_payload, lines),
            language=_extract_language(transcript, raw_payload),
            raw_response_path=str(raw_response_path),
            structured_transcript_path=str(structured_transcript_path),
        )
        _write_json(structured_transcript_path, asdict(result))
        return result

    def _submit_and_wait_for_transcript(
        self,
        *,
        transcriber: Any,
        upload_path: str,
        config: Any,
    ) -> Any:
        submit = getattr(transcriber, "submit", None)
        if callable(submit):
            print("[S1] 正在上传音频到 AssemblyAI...")
            transcript = submit(upload_path, config=config)
            transcript_id = _normalize_optional_text(getattr(transcript, "id", None))
            if transcript_id is None:
                print("[S1] 上传完成，正在等待转录结果...")
            else:
                print(f"[S1] 上传完成，任务ID={transcript_id}，正在等待转录结果...")

            wait_for_completion = getattr(transcript, "wait_for_completion", None)
            if callable(wait_for_completion):
                wait_for_completion()
                print("[S1] 转录结果已返回，正在整理文本...")
                return transcript

        print("[S1] 正在提交转录请求到 AssemblyAI...")
        transcript = transcriber.transcribe(
            upload_path,
            config=config,
        )
        print("[S1] 转录结果已返回，正在整理文本...")
        return transcript

    def _resolve_upload_path(self, audio_path: str) -> str:
        resolved_audio_path = str(Path(audio_path).resolve(strict=False))
        source_path = Path(resolved_audio_path)
        if not source_path.exists():
            return resolved_audio_path

        if source_path.suffix.casefold() in COMPRESSED_AUDIO_EXTENSIONS:
            return resolved_audio_path

        file_size_mb = source_path.stat().st_size / (1024 * 1024)
        if file_size_mb <= DEFAULT_UPLOAD_OPTIMIZATION_THRESHOLD_MB:
            return resolved_audio_path

        upload_path = source_path.with_name("original_upload.mp3")
        if upload_path.exists() and upload_path.stat().st_mtime >= source_path.stat().st_mtime:
            print("[S1] 使用已有MP3上传优化文件")
            return str(upload_path.resolve(strict=False))

        try:
            print(f"[S1] 音频文件较大（{file_size_mb:.0f}MB），生成MP3上传优化文件")
            subprocess.run(
                [
                    "ffmpeg", "-i", str(source_path),
                    "-ac", "1",
                    "-ar", str(DEFAULT_UPLOAD_MP3_FRAME_RATE),
                    "-b:a", DEFAULT_UPLOAD_MP3_BITRATE,
                    "-f", "mp3", str(upload_path), "-y",
                ],
                check=True,
                capture_output=True,
            )
            return str(upload_path.resolve(strict=False))
        except Exception as exc:
            print(f"[S1] 生成MP3上传优化文件失败，回退原始音频上传：{exc}")
            return resolved_audio_path


def _retry_wait_seconds(attempt: int) -> int:
    return min(10 * (2 ** attempt), 60)


def load_assemblyai_config() -> dict[str, object]:
    config_path = DEFAULT_AUTODUB_LOCAL_CONFIG_PATH.resolve(strict=False)
    payload: dict[str, object] = {}

    if config_path.exists():
        try:
            loaded_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranscriptionError(f"Failed to load AssemblyAI config from {config_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise TranscriptionError(
                f"AssemblyAI config file must contain a top-level JSON object: {config_path}"
            )
        payload = loaded_payload

    section = payload.get("assemblyai", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise TranscriptionError("assemblyai config section must be a JSON object.")

    api_key_env_var = _normalize_optional_text(section.get("api_key_env_var")) or "ASSEMBLYAI_API_KEY"
    api_key = _normalize_optional_text(section.get("api_key"))
    if api_key is None:
        api_key = _normalize_optional_text(os.getenv(api_key_env_var))
    if api_key is None:
        raise TranscriptionError(
            f"AssemblyAI API key is required via autodub.local.json or env {api_key_env_var}."
        )

    return {
        "api_key": api_key,
        "api_key_env_var": api_key_env_var,
        "language_code": _normalize_optional_text(section.get("language_code")) or DEFAULT_LANGUAGE_CODE,
        "speaker_labels": _coerce_bool(section.get("speaker_labels"), default=False),
        "http_timeout_seconds": _coerce_positive_float(
            section.get("http_timeout_seconds"),
            default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        ),
    }


def _load_assemblyai_sdk() -> Any:
    try:
        import assemblyai as aai
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise TranscriptionError("AssemblyAI SDK is not installed.") from exc
    return aai


def _coerce_positive_float(value: object, *, default: float) -> float:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return float(default)
    if numeric_value <= 0:
        return float(default)
    return numeric_value


def _build_transcription_config(
    aai: Any,
    *,
    speaker_labels: bool,
    speakers_expected: int | None,
) -> Any:
    kwargs: dict[str, Any] = {
        "language_code": DEFAULT_LANGUAGE_CODE,
        "speaker_labels": speaker_labels,
        "disfluencies": True,
        "prompt": DEFAULT_TRANSCRIPTION_PROMPT,
    }
    if speaker_labels and speakers_expected is not None:
        kwargs["speakers_expected"] = int(speakers_expected)

    try:
        return aai.TranscriptionConfig(
            **kwargs,
            speech_models=list(DEFAULT_SPEECH_MODELS),
        )
    except TypeError:
        return aai.TranscriptionConfig(**kwargs)


_MAX_SINGLE_UTTERANCE_DURATION_MS = 45_000  # 45 seconds — split overlong utterances
_MERGE_MAX_DURATION_MS = 30_000  # merge sentences until 30s
_MERGE_PAUSE_THRESHOLD_MS = 1_500  # split on pauses > 1.5s


def _build_transcript_lines(transcript: Any, *, speaker_labels: bool) -> list[TranscriptLine]:
    if speaker_labels:
        utterances = list(getattr(transcript, "utterances", []) or [])
        if utterances:
            # 多说话人检测
            speaker_ids = set()
            for utt in utterances:
                spk = _normalize_optional_text(getattr(utt, "speaker", None))
                if spk:
                    speaker_ids.add(spk)
            is_multi_speaker = len(speaker_ids) > 1

            if is_multi_speaker:
                # 多说话人：始终使用 utterances 保留说话人信息
                # 对超长 utterance 做机械拆分，但不丢弃 speaker 标签
                return _build_lines_from_utterances_with_split(utterances)

            if _utterances_well_segmented(utterances):
                return _build_lines_from_utterances(utterances)

    # 单说话人或无 utterances — build sentence-level lines, then 3-layer split
    words = list(getattr(transcript, "words", []) or [])
    sentences = list(getattr(transcript, "sentences", []) or [])

    raw_lines: list[TranscriptLine] = []
    if sentences:
        raw_lines = _build_lines_from_sentences(sentences, speaker_labels=speaker_labels)
    if not raw_lines:
        raw_lines, _ = _build_lines_from_words(words, speaker_labels=speaker_labels)

    if len(raw_lines) <= 1:
        return raw_lines

    # Merge sentences into reasonable segments using _merge_short_lines first,
    # then apply 3-layer mechanical split (same as multi-speaker path)
    merged_lines = _merge_short_lines(raw_lines)

    # Build global word index for 3-layer split
    all_words = [
        {
            "text": getattr(w, "text", "") or (w.get("text", "") if isinstance(w, dict) else ""),
            "start": _coerce_int(getattr(w, "start", None) if hasattr(w, "start") else w.get("start"), default=0),
            "end": _coerce_int(getattr(w, "end", None) if hasattr(w, "end") else w.get("end"), default=0),
        }
        for w in words
    ]
    result = _apply_3layer_split(merged_lines, all_words)
    print(f"[S1] 单说话人 3 层拆分: {len(raw_lines)} sentences → {len(merged_lines)} merged → {len(result)} lines")
    return result


## _try_llm_segmentation removed — LLM semantic split now handled by
## unified transcript reviewer (src/services/transcript_reviewer.py) in S2.


def _apply_3layer_split(lines: list[TranscriptLine], all_words: list[dict]) -> list[TranscriptLine]:
    """Apply 3-layer hierarchical splitting using word-level pause detection.

    Shared by both multi-speaker and single-speaker paths.

    Layers (applied sequentially):
      Layer 1: >15s + pauses ≥3s → split at ALL ≥3s pauses
      Layer 2: >45s + pauses ≥2s → split at the LONGEST ≥2s pause (bisect)
      Layer 3: >90s + pauses ≥1.5s → split at the LONGEST ≥1.5s pause (bisect again)
    """
    if not lines:
        return lines

    def _get_words_for_line(line: TranscriptLine) -> list[dict]:
        return [
            w for w in all_words
            if w["start"] >= line.start_ms - 100 and w["end"] <= line.end_ms + 100
        ]

    # --- Layer 1: >15s + pauses ≥3s → split at ALL ≥3s pauses ---
    lines_l1: list[TranscriptLine] = []
    for line in lines:
        dur = line.end_ms - line.start_ms
        if dur <= 15_000:
            lines_l1.append(line)
            continue
        words = _get_words_for_line(line)
        if len(words) < 4:
            lines_l1.append(line)
            continue
        chunks = _split_at_all_pauses(words, line, min_pause_ms=3000)
        if len(chunks) > 1:
            lines_l1.extend(chunks)
        else:
            lines_l1.append(line)

    # --- Layer 2: >45s + pauses ≥2s → split at LONGEST ≥2s pause ---
    lines_l2: list[TranscriptLine] = []
    for line in lines_l1:
        dur = line.end_ms - line.start_ms
        if dur <= 45_000:
            lines_l2.append(line)
            continue
        words = _get_words_for_line(line)
        if len(words) < 4:
            lines_l2.append(line)
            continue
        chunks = _split_at_longest_pause(words, line, min_pause_ms=2000)
        lines_l2.extend(chunks)

    # --- Layer 3: >90s + pauses ≥1.5s → split at LONGEST ≥1.5s pause ---
    lines_l3: list[TranscriptLine] = []
    for line in lines_l2:
        dur = line.end_ms - line.start_ms
        if dur <= 90_000:
            lines_l3.append(line)
            continue
        words = _get_words_for_line(line)
        if len(words) < 4:
            lines_l3.append(line)
            continue
        chunks = _split_at_longest_pause(words, line, min_pause_ms=1500)
        lines_l3.extend(chunks)

    # Re-index
    result: list[TranscriptLine] = []
    for line in lines_l3:
        result.append(TranscriptLine(
            index=len(result) + 1,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=line.speaker_id,
            speaker_label=line.speaker_label,
            source_text=line.source_text,
        ))

    return result


def _build_lines_from_utterances_with_split(utterances: list[Any]) -> list[TranscriptLine]:
    """Build transcript lines from utterances with 3-layer hierarchical splitting.

    For multi-speaker videos: preserves speaker info from every utterance.
    Uses word-level timestamps for precise pause-based splitting.
    """
    # Build flat word list from all utterances
    all_words: list[dict] = []
    for utt in utterances:
        words = list(getattr(utt, "words", []) or [])
        for w in words:
            all_words.append({
                "text": getattr(w, "text", "") or "",
                "start": _coerce_int(getattr(w, "start", None), default=0),
                "end": _coerce_int(getattr(w, "end", None), default=0),
            })

    raw_lines = _build_lines_from_utterances(utterances)
    if not raw_lines:
        return raw_lines

    result = _apply_3layer_split(raw_lines, all_words)
    print(f"[S1] 多说话人 3 层拆分: {len(utterances)} utterances → {len(result)} lines")
    long_remaining = sum(1 for l in result if (l.end_ms - l.start_ms) > 300_000)
    if long_remaining:
        print(f"[S1] ⚠ 仍有 {long_remaining} 段超过 300 秒")
    return result


def _split_at_all_pauses(
    words: list[dict],
    line: TranscriptLine,
    min_pause_ms: int,
) -> list[TranscriptLine]:
    """Split words at ALL pauses >= min_pause_ms. Returns list of TranscriptLine."""
    if len(words) < 2:
        return [line]

    split_indices: list[int] = []
    for i in range(len(words) - 1):
        gap = max(0, words[i + 1]["start"] - words[i]["end"])
        if gap >= min_pause_ms:
            split_indices.append(i)

    if not split_indices:
        return [line]

    return _build_lines_from_split_indices(words, split_indices, line)


def _split_at_longest_pause(
    words: list[dict],
    line: TranscriptLine,
    min_pause_ms: int,
) -> list[TranscriptLine]:
    """Split words at the single LONGEST pause >= min_pause_ms. Bisects the line."""
    if len(words) < 2:
        return [line]

    best_idx = -1
    best_gap = 0
    for i in range(len(words) - 1):
        gap = max(0, words[i + 1]["start"] - words[i]["end"])
        if gap >= min_pause_ms and gap > best_gap:
            best_gap = gap
            best_idx = i

    if best_idx < 0:
        return [line]

    return _build_lines_from_split_indices(words, [best_idx], line)


def _build_lines_from_split_indices(
    words: list[dict],
    split_indices: list[int],
    template_line: TranscriptLine,
) -> list[TranscriptLine]:
    """Build TranscriptLine list from words split at given indices."""
    result: list[TranscriptLine] = []
    prev = 0
    for idx in sorted(split_indices):
        chunk = words[prev:idx + 1]
        if chunk:
            result.append(TranscriptLine(
                index=0,  # will be re-indexed later
                start_ms=chunk[0]["start"],
                end_ms=chunk[-1]["end"],
                speaker_id=template_line.speaker_id,
                speaker_label=template_line.speaker_label,
                source_text=" ".join(w["text"] for w in chunk),
            ))
        prev = idx + 1
    # Remaining
    if prev < len(words):
        chunk = words[prev:]
        if chunk:
            result.append(TranscriptLine(
                index=0,
                start_ms=chunk[0]["start"],
                end_ms=chunk[-1]["end"],
                speaker_id=template_line.speaker_id,
                speaker_label=template_line.speaker_label,
                source_text=" ".join(w["text"] for w in chunk),
            ))
    return result if result else [template_line]


def _utterances_well_segmented(utterances: list[Any]) -> bool:
    """Utterances are fine unless any single one exceeds the duration threshold."""
    for utt in utterances:
        start = _coerce_int(getattr(utt, "start", None), default=0)
        end = _coerce_int(getattr(utt, "end", None), default=start)
        if (end - start) > _MAX_SINGLE_UTTERANCE_DURATION_MS:
            return False
    return True


def _merge_short_lines(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    """Merge consecutive same-speaker lines into chunks based on duration and pauses."""
    if not lines:
        return lines

    merged: list[TranscriptLine] = []
    group_start_ms = lines[0].start_ms
    group_end_ms = lines[0].end_ms
    group_speaker_id = lines[0].speaker_id
    group_speaker_label = lines[0].speaker_label
    group_texts: list[str] = [lines[0].source_text]

    def flush_group() -> None:
        nonlocal group_start_ms, group_end_ms, group_speaker_id, group_speaker_label, group_texts
        if group_texts:
            merged.append(TranscriptLine(
                index=len(merged) + 1,
                start_ms=group_start_ms,
                end_ms=group_end_ms,
                speaker_id=group_speaker_id,
                speaker_label=group_speaker_label,
                source_text=" ".join(group_texts),
            ))
        group_texts = []

    for line in lines[1:]:
        gap_ms = max(0, line.start_ms - group_end_ms)
        group_duration_ms = line.end_ms - group_start_ms
        speaker_changed = line.speaker_id != group_speaker_id

        # Split conditions: speaker change, long pause, or duration exceeded
        if speaker_changed or gap_ms >= _MERGE_PAUSE_THRESHOLD_MS or group_duration_ms >= _MERGE_MAX_DURATION_MS:
            flush_group()
            group_start_ms = line.start_ms
            group_speaker_id = line.speaker_id
            group_speaker_label = line.speaker_label

        group_texts.append(line.source_text)
        group_end_ms = line.end_ms

    flush_group()
    return merged


def _build_lines_from_sentences(sentences: list[Any], *, speaker_labels: bool) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = []
    for sentence in sentences:
        source_text = _normalize_optional_text(getattr(sentence, "text", None))
        if source_text is None:
            continue
        speaker_label = (
            _normalize_optional_text(getattr(sentence, "speaker", None)) or DEFAULT_SPEAKER_LABEL
            if speaker_labels
            else DEFAULT_SPEAKER_LABEL
        )
        start_ms = _coerce_int(getattr(sentence, "start", None), default=0)
        end_ms = _coerce_int(getattr(sentence, "end", None), default=start_ms)
        lines.append(
            TranscriptLine(
                index=len(lines) + 1,
                start_ms=start_ms,
                end_ms=max(start_ms, end_ms),
                speaker_id=_speaker_id_from_label(speaker_label) if speaker_labels else DEFAULT_SPEAKER_ID,
                speaker_label=speaker_label,
                source_text=source_text,
            )
        )
    return lines


def _build_lines_from_utterances(utterances: list[Any]) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = []
    for utterance in utterances:
        source_text = _normalize_optional_text(getattr(utterance, "text", None))
        if source_text is None:
            continue
        speaker_label = _normalize_optional_text(getattr(utterance, "speaker", None)) or DEFAULT_SPEAKER_LABEL
        start_ms = _coerce_int(getattr(utterance, "start", None), default=0)
        end_ms = _coerce_int(getattr(utterance, "end", None), default=start_ms)
        lines.append(
            TranscriptLine(
                index=len(lines) + 1,
                start_ms=start_ms,
                end_ms=max(start_ms, end_ms),
                speaker_id=_speaker_id_from_label(speaker_label),
                speaker_label=speaker_label,
                source_text=source_text,
            )
        )
    return lines


def _build_lines_from_words(words: list[Any], *, speaker_labels: bool) -> tuple[list[TranscriptLine], bool]:
    if not words:
        return [], False

    lines: list[TranscriptLine] = []
    buffer_tokens: list[str] = []
    start_ms: int | None = None
    end_ms: int | None = None
    current_speaker_label: str | None = None
    saw_sentence_punctuation = False

    def flush() -> None:
        nonlocal buffer_tokens, start_ms, end_ms, current_speaker_label
        if not buffer_tokens:
            return
        source_text = _join_tokens(buffer_tokens)
        if source_text:
            normalized_start_ms = 0 if start_ms is None else start_ms
            normalized_end_ms = normalized_start_ms if end_ms is None else max(normalized_start_ms, end_ms)
            speaker_label = (
                current_speaker_label or DEFAULT_SPEAKER_LABEL
                if speaker_labels
                else DEFAULT_SPEAKER_LABEL
            )
            lines.append(
                TranscriptLine(
                    index=len(lines) + 1,
                    start_ms=normalized_start_ms,
                    end_ms=normalized_end_ms,
                    speaker_id=_speaker_id_from_label(speaker_label) if speaker_labels else DEFAULT_SPEAKER_ID,
                    speaker_label=speaker_label,
                    source_text=source_text,
                )
            )
        buffer_tokens = []
        start_ms = None
        end_ms = None
        current_speaker_label = None

    for word in words:
        token = _normalize_optional_text(getattr(word, "text", None))
        if token is None:
            continue
        word_start = _coerce_int(getattr(word, "start", None), default=0)
        word_end = _coerce_int(getattr(word, "end", None), default=word_start)
        word_speaker_label = (
            _normalize_optional_text(getattr(word, "speaker", None)) or DEFAULT_SPEAKER_LABEL
            if speaker_labels
            else DEFAULT_SPEAKER_LABEL
        )

        if not buffer_tokens:
            start_ms = word_start
            current_speaker_label = word_speaker_label
        elif speaker_labels and word_speaker_label != current_speaker_label:
            flush()
            start_ms = word_start
            current_speaker_label = word_speaker_label

        buffer_tokens.append(token)
        end_ms = word_end
        if _ends_sentence(token):
            saw_sentence_punctuation = True
            flush()

    flush()
    return lines, saw_sentence_punctuation


def _ends_sentence(token: str) -> bool:
    return bool(SENTENCE_END_PATTERN.search(token))


def _join_tokens(tokens: list[str]) -> str:
    if not tokens:
        return ""

    assembled = tokens[0]
    for token in tokens[1:]:
        if token in ATTACHED_PUNCTUATION or token.startswith("'"):
            assembled += token
            continue
        if assembled.endswith(("(", "[", "{", '"')):
            assembled += token
            continue
        assembled += f" {token}"
    return re.sub(r"\s+", " ", assembled).strip()


def _speaker_id_from_label(speaker_label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", speaker_label.strip().lower()).strip("_")
    if not normalized:
        return DEFAULT_SPEAKER_ID
    if normalized.startswith("speaker_"):
        return normalized
    if normalized == "speaker":
        return DEFAULT_SPEAKER_ID
    return f"speaker_{normalized}"


def _extract_language(transcript: Any, raw_payload: Any) -> str:
    for candidate in (
        getattr(transcript, "language_code", None),
        getattr(transcript, "language", None),
    ):
        normalized = _normalize_optional_text(candidate)
        if normalized is not None:
            return normalized

    if isinstance(raw_payload, dict):
        for key in ("language_code", "language"):
            normalized = _normalize_optional_text(raw_payload.get(key))
            if normalized is not None:
                return normalized

    return DEFAULT_LANGUAGE_CODE


def _extract_total_duration_ms(
    transcript: Any,
    raw_payload: Any,
    lines: list[TranscriptLine],
) -> int:
    reference_end_ms = max((line.end_ms for line in lines), default=0)
    for candidate in (
        getattr(transcript, "audio_duration", None),
        getattr(transcript, "audio_duration_ms", None),
    ):
        duration_ms = _coerce_optional_int(candidate)
        if duration_ms is not None and duration_ms >= 0:
            return _normalize_duration_ms(duration_ms, reference_end_ms=reference_end_ms)

    if isinstance(raw_payload, dict):
        for key in ("audio_duration", "audio_duration_ms"):
            duration_ms = _coerce_optional_int(raw_payload.get(key))
            if duration_ms is not None and duration_ms >= 0:
                return _normalize_duration_ms(duration_ms, reference_end_ms=reference_end_ms)

    end_candidates = [line.end_ms for line in lines]
    for attribute_name in ("utterances", "sentences", "words"):
        for item in list(getattr(transcript, attribute_name, []) or []):
            end_ms = _coerce_optional_int(getattr(item, "end", None))
            if end_ms is not None:
                end_candidates.append(end_ms)
    return max(end_candidates, default=0)


def _normalize_duration_ms(duration_value: int, *, reference_end_ms: int) -> int:
    if duration_value < 0:
        return duration_value
    if reference_end_ms <= 0:
        return duration_value

    direct_diff = abs(duration_value - reference_end_ms)
    scaled_value = duration_value * 1000
    scaled_diff = abs(scaled_value - reference_end_ms)
    tolerance_ms = max(5_000, int(reference_end_ms * 0.1))

    if duration_value < 10_000 and scaled_diff < direct_diff and scaled_diff <= tolerance_ms:
        return scaled_value
    return duration_value


def _extract_raw_payload(transcript: Any) -> Any:
    json_response = getattr(transcript, "json_response", None)
    if json_response is not None:
        if isinstance(json_response, str):
            try:
                return json.loads(json_response)
            except json.JSONDecodeError:
                return {"json_response": json_response}
        return _to_jsonable(json_response)

    to_dict = getattr(transcript, "to_dict", None)
    if callable(to_dict):
        try:
            return _to_jsonable(to_dict())
        except Exception:
            pass

    return _to_jsonable(
        {
            "status": getattr(transcript, "status", None),
            "error": getattr(transcript, "error", None),
            "language_code": getattr(transcript, "language_code", None),
            "audio_duration": getattr(transcript, "audio_duration", None),
            "words": getattr(transcript, "words", None),
            "sentences": getattr(transcript, "sentences", None),
            "utterances": getattr(transcript, "utterances", None),
        }
    )


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


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


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
