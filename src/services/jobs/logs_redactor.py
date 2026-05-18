"""Server-side log redaction for non-admin users (D25 / D31).

When ``GET /jobs/{id}/logs`` is called, the response payload is normally the
raw event stream, which contains internal identifiers (task UUIDs, provider
names like ``AssemblyAI`` / ``MiniMax`` / ``Gemini``) that leak implementation
details and vendor choices. This module strips such fragments when the caller
is not an admin.

Design notes:

- Provider name list is assembled **dynamically** from the LLM / TTS
  registries at startup. Hardcoding it (D31) would silently stop redacting
  when a new provider is added. The module still carries a small fallback
  tuple for test environments and for provider modules that don't publish
  a registry symbol.
- The redactor is intentionally conservative: it replaces sensitive tokens
  with an empty string and collapses resulting runs of whitespace. It does
  NOT attempt to understand log structure (stage names / levels), so admins
  who want the untouched event stream must continue to call with admin role
  (the API handler is the one that decides whether to redact).
- Frontend "``isAdmin`` hides the LogViewer" is only cosmetic. Authority for
  redaction lives here, on the backend, so that curl ``/jobs/{id}/logs``
  without admin role cannot bypass it.

T1-7 will wire this into the API handler. Phase 0 only builds the module +
tests so the redaction contract is locked in before any caller depends on it.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "Redactor",
    "build_default_redactor",
    "REDACTED_PLACEHOLDER",
]


# Default placeholder used to replace sensitive tokens. Intentionally empty so
# the log line becomes slightly shorter rather than louder — the goal is to
# not-leak, not to highlight the redaction.
REDACTED_PLACEHOLDER = ""

# Always-redact tool / infra names that are not provider SDKs but still leak
# implementation details. These stay hardcoded because they aren't part of any
# provider registry.
_INFRA_TOOL_NAMES: tuple[str, ...] = (
    "AssemblyAI",
    "yt-dlp",
    "ytdlp",
    "ffmpeg",
    "FFmpeg",
    "docker",
    "Docker",
)

# UUID pattern — catches any 8-4-4-4-12 hex UUID, including "任务ID=<uuid>" forms.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Explicit "任务ID=..." / "job id: ..." patterns — catches even non-UUID internal IDs.
_JOB_ID_LABEL_RE = re.compile(
    r"(?:任务ID|task[_ ]?id|job[_ ]?id)\s*[=:]\s*\S+",
    re.IGNORECASE,
)

_WHITESPACE_RUN_RE = re.compile(r"\s+")

# Sensitive key=value / key: value patterns whose *values* must be redacted.
# Covers JSON ("key": "value"), form-encoded (key=value), and plain-text
# (key=value or key: value) shapes.  Value ends at whitespace, quote,
# comma, or end-of-string.  Keys added here (pan-backup T2.4):
#   access_token, refresh_token — Baidu Pan OAuth bearer/refresh credentials
#   appsecret                   — our config field name for the OAuth app secret
#   client_secret               — OAuth standard field name for the app secret
_SENSITIVE_KV_KEYS = (
    "access_token",
    "refresh_token",
    "appsecret",
    "client_secret",
)
_SENSITIVE_KV_RE = re.compile(
    r'(?<!\w)('
    + "|".join(re.escape(k) for k in _SENSITIVE_KV_KEYS)
    # Optional closing quote of JSON key, then separator (= or :), then
    # optional opening quote of value.  Handles:
    #   JSON:         "access_token": "value"
    #   form-encoded: access_token=value
    #   plain text:   access_token: value
    + r')(["\']?\s*[:=]\s*["\']?)([^"\',\s}{&]+)',
    re.IGNORECASE,
)


class Redactor:
    """Redacts provider / tool / UUID tokens from a log message.

    Instances are immutable and safe to share across requests. Construct via
    :func:`build_default_redactor` unless you have a specific provider list to inject.
    """

    __slots__ = ("_patterns",)

    def __init__(self, provider_names: Iterable[str]) -> None:
        # Build a single regex of the form ``\b(name1|name2|...)\b`` with
        # case-insensitive match. Escape each name so dots etc. in
        # hypothetical provider names don't become regex operators.
        unique: list[str] = []
        seen: set[str] = set()
        for name in provider_names:
            norm = name.strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(re.escape(norm))
        patterns: list[re.Pattern[str]] = [_UUID_RE, _JOB_ID_LABEL_RE]
        if unique:
            patterns.append(
                re.compile(r"\b(?:" + "|".join(unique) + r")\b", re.IGNORECASE)
            )
        self._patterns = tuple(patterns)

    def redact(self, message: str) -> str:
        """Apply all patterns to ``message`` and return the cleaned string."""
        if not message:
            return message
        out = message
        # Redact key=value / key: value sensitive credential pairs first so
        # that token-level patterns below don't leave the key name orphaned.
        out = _SENSITIVE_KV_RE.sub(r"\1\2" + REDACTED_PLACEHOLDER, out)
        for pattern in self._patterns:
            out = pattern.sub(REDACTED_PLACEHOLDER, out)
        return _WHITESPACE_RUN_RE.sub(" ", out).strip()


def build_default_redactor() -> Redactor:
    """Construct a redactor using dynamically-sourced provider names.

    Tries to pull names from the LLM and TTS registries; falls back to the
    hardcoded tool-name list if the registries are unavailable (e.g. tests
    that don't want to import the whole app).
    """
    names: list[str] = list(_INFRA_TOOL_NAMES)
    names.extend(_collect_llm_provider_names())
    names.extend(_collect_tts_provider_names())
    return Redactor(names)


def _collect_llm_provider_names() -> list[str]:
    # Always start with the brand-name safety net so even a minimal / test
    # environment (no LLM registry import path) still redacts the obvious
    # provider names. Registry contents are internal model IDs like
    # ``gemini-2.5-flash`` or ``deepseek-v3-0324`` which may appear verbatim
    # in logs alongside the brand; redacting both layers is the goal.
    names: list[str] = ["Gemini", "DeepSeek", "MiMo"]
    # The project layout puts ``src/`` on sys.path at runtime, so the
    # single-source-of-truth registry is reachable as ``services.llm_registry``
    # (same import path used by ``src/pipeline/process.py`` and
    # ``src/services/gemini/translator.py``). Attempting ``src.services.llm``
    # silently falls back to brand names — see CodeX review 2026-04-18 T0-4
    # round 2. Wrap in try/except because test / minimal environments may
    # not put src/ on sys.path.
    try:
        from services import llm_registry  # type: ignore
    except Exception:
        return names
    for attr in ("iter_provider_names", "PROVIDERS", "MODEL_REGISTRY"):
        target = getattr(llm_registry, attr, None)
        if target is None:
            continue
        try:
            if callable(target):
                names.extend(str(n) for n in target())
            elif hasattr(target, "keys"):
                names.extend(str(n) for n in target.keys())
            elif hasattr(target, "__iter__"):
                names.extend(str(n) for n in target)
        except Exception:
            continue
    return names


def _collect_tts_provider_names() -> list[str]:
    # TTS providers are intentionally hardcoded for now: the TTS registry is
    # fragmented across multiple modules (CosyVoice / MiniMax / VolcEngine) and
    # each has a different public API. We include all three + legacy names.
    return ["MiniMax", "CosyVoice", "VolcEngine", "Volcengine", "Doubao", "豆包"]
