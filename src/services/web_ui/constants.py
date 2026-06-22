from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from services.gemini.translator import (
    get_effective_rewrite_prompt_template,
    get_effective_speaker_infer_prompt_template,
    get_effective_translation_prompt_template,
    validate_rewrite_prompt_template,
    validate_speaker_infer_prompt_template,
    validate_translation_prompt_template,
)
from services.jobs.api import JOB_API_DEFAULT_HOST, JOB_API_DEFAULT_PORT

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAIN_PY_PATH = PROJECT_ROOT / "main.py"
WEB_UI_DEFAULT_HOST = "127.0.0.1"
# WEB_UI_DEFAULT_PORT (= 8876) removed in 2026-04-17 T1.6a — the standalone
# Web UI server on that port is retired. JobAPIBackedJobManager uses the
# Job API base URL directly; nothing here needs the 8876 default anymore.
WEB_UI_DEFAULT_JOB_API_BASE_URL = f"http://{JOB_API_DEFAULT_HOST}:{JOB_API_DEFAULT_PORT}"
WEB_UI_TITLE = "AIVideoTrans Web UI"
MAX_LOG_LINES = 500
PROCESS_RUN_TIMEOUT_SECONDS = 60 * 60 * 6
RESULT_PAGE_SIZE_OPTIONS = (20, 50, 100)
DEFAULT_RESULT_PAGE_SIZE = 20
RESULT_DOWNLOAD_KEY_MANIFEST = "manifest.file"
PUBLIC_RESULT_DOWNLOAD_KEYS = frozenset(
    {
        RESULT_DOWNLOAD_KEY_MANIFEST,
        "translation.segments",
        "editor.subtitles",
        "editor.subtitles_en",
        "editor.subtitles_bilingual",
        # PR-F: script-neutral subtitle keys (target=dub, source=original) so a
        # non-default pair can download the correct-language subtitle by an honest name.
        "editor.subtitles_target",
        "editor.subtitles_source",
        "editor.dubbed_audio_complete",
        "editor.jianying_draft_zip",
        "publish.dubbed_video",
    }
)
PROJECT_AUDIO_FILE_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
STAGE_LOG_PATTERN = re.compile(r"^\[(S\d+)\]\s*(.*)$")
DOWNLOAD_PROGRESS_PATTERN = re.compile(r"^\[download\]\s*(.+)$", re.IGNORECASE)
WINDOWS_PATH_PATTERN = re.compile(r"([A-Za-z]:[\\/][^\r\n]+)")
RESULT_SOURCE_LABELS = {
    "matched_youtube_url": "按当前任务 URL 匹配到项目",
    "log_path": "从运行日志识别到项目目录",
    "latest_project": "回退到最近项目目录",
    "no_projects_root": "尚未发现 projects 目录",
    "no_project_match": "未匹配到可用项目结果",
}
PROVIDER_DISPLAY_NAMES = {
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}
SPEAKER_OPTIONS = (
    {"value": "auto", "label": "自动"},
    {"value": "1", "label": "1 人"},
    {"value": "2", "label": "2 人"},
)
PROMPT_TEMPLATE_LOADERS: dict[str, tuple[Callable[[object | None], str], Callable[[str], str]]] = {
    "s2_infer": (
        get_effective_speaker_infer_prompt_template,
        validate_speaker_infer_prompt_template,
    ),
    "s3_translate": (
        get_effective_translation_prompt_template,
        validate_translation_prompt_template,
    ),
    "s5_rewrite": (
        get_effective_rewrite_prompt_template,
        validate_rewrite_prompt_template,
    ),
}
