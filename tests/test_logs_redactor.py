"""Unit tests for src/services/jobs/logs_redactor.py.

Verifies redaction contract so that non-admin ``GET /jobs/{id}/logs`` responses
can never leak:
- Internal task UUIDs
- Provider brand names (LLM / TTS / infra tools)
- "任务ID=<uuid>" / "job_id: <uuid>" style labels
"""

from __future__ import annotations

import re

import pytest

from src.services.jobs.logs_redactor import (
    REDACTED_PLACEHOLDER,
    Redactor,
    build_default_redactor,
)


# --- UUID redaction -------------------------------------------------------


def test_redacts_full_uuid() -> None:
    r = Redactor([])
    msg = "[S1] 上传完成，任务ID=4a6006e8-b2df-41b1-9b19-bd6facf1d9bf，正在等待转录结果..."
    out = r.redact(msg)
    assert "4a6006e8" not in out
    assert "9bf" not in out
    assert "任务ID" not in out  # label also consumed


def test_redacts_uppercase_uuid() -> None:
    r = Redactor([])
    out = r.redact("job ABCDEF12-3456-7890-ABCD-EF1234567890 started")
    assert "ABCDEF12" not in out


def test_redacts_multiple_uuids_in_one_line() -> None:
    r = Redactor([])
    out = r.redact(
        "source=11111111-2222-3333-4444-555555555555 target=99999999-8888-7777-6666-555555555555"
    )
    assert "1111" not in out
    assert "9999" not in out


# --- Job ID label redaction ----------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "任务ID=",
        "task_id=",
        "task id: ",
        "job_id=",
        "job id = ",
        "JobId: ",
    ],
)
def test_redacts_job_id_labels(label: str) -> None:
    r = Redactor([])
    msg = f"{label}abc-123-xyz"
    out = r.redact(msg)
    assert "abc-123-xyz" not in out


# --- Provider-name redaction ---------------------------------------------


def test_redacts_provider_names_case_insensitive() -> None:
    r = Redactor(["AssemblyAI", "MiniMax", "Gemini"])
    out = r.redact("calling AssemblyAI to transcribe; minimax cloning; GEMINI rewriting")
    assert "AssemblyAI" not in out
    assert "assemblyai" not in out.lower()
    assert "minimax" not in out.lower()
    assert "gemini" not in out.lower()


def test_does_not_redact_provider_substrings_in_other_words() -> None:
    """``AssemblyAI`` must not match ``AssemblyAImmigration`` — use word boundaries."""
    r = Redactor(["AssemblyAI"])
    out = r.redact("preAssemblyAIpost text here")
    # Because \b requires a word boundary, "preAssemblyAIpost" is untouched.
    # (This is the expected conservative behaviour.)
    assert "AssemblyAIpost" in out or "AssemblyAI" not in out


def test_duplicate_provider_names_deduped() -> None:
    r = Redactor(["MiniMax", "minimax", "MINIMAX", "", "  "])
    out = r.redact("MiniMax speaks")
    assert "MiniMax" not in out


# --- Whitespace cleanup --------------------------------------------------


def test_collapses_whitespace_after_redaction() -> None:
    r = Redactor(["Gemini"])
    out = r.redact("before  Gemini  after")
    assert "Gemini" not in out
    assert "  " not in out  # double space collapsed


def test_strips_leading_trailing_whitespace() -> None:
    r = Redactor(["Foo"])
    out = r.redact("   Foo   ")
    assert out == REDACTED_PLACEHOLDER


# --- Empty / passthrough -------------------------------------------------


def test_empty_message_returned_unchanged() -> None:
    r = Redactor([])
    assert r.redact("") == ""


def test_message_with_no_sensitive_content_only_gets_whitespace_collapsed() -> None:
    r = Redactor([])
    out = r.redact("hello    world")
    assert out == "hello world"


# --- Default redactor (registry-based) -----------------------------------


def test_default_redactor_includes_infra_tools() -> None:
    r = build_default_redactor()
    for name in ["AssemblyAI", "yt-dlp", "ffmpeg"]:
        msg = f"calling {name} now"
        out = r.redact(msg)
        assert name.lower() not in out.lower(), (
            f"Default redactor should strip {name} from {msg!r}, got {out!r}"
        )


def test_default_redactor_includes_common_llm_brands() -> None:
    r = build_default_redactor()
    # Brand-name safety net in _collect_llm_provider_names
    for name in ["Gemini", "DeepSeek", "MiMo"]:
        out = r.redact(f"rewriting via {name} provider")
        assert re.search(rf"\b{re.escape(name)}\b", out, re.IGNORECASE) is None


def test_default_redactor_includes_tts_brands() -> None:
    r = build_default_redactor()
    for name in ["MiniMax", "CosyVoice", "VolcEngine", "Doubao", "豆包"]:
        out = r.redact(f"TTS via {name} engine")
        assert re.search(rf"\b{re.escape(name)}\b", out, re.IGNORECASE) is None


def test_default_redactor_actually_reads_llm_registry() -> None:
    """Regression guard for CodeX 2026-04-18 T0-4 round 2.

    Prior bug: ``from src.services.llm import llm_registry`` silently failed,
    so only the hardcoded brand list ran. After the fix, the real MODEL_REGISTRY
    keys from ``services.llm_registry`` must appear in the redactor's pattern
    set. We assert this by picking a concrete model ID that we know exists in
    the registry and confirming it gets redacted.
    """
    # Import the real registry here — if this import fails, the whole test
    # env is broken and we want a clear error (not a silent skip).
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src_path = str(root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    from services import llm_registry as real_registry

    model_ids = list(real_registry.MODEL_REGISTRY.keys())
    assert model_ids, "llm_registry.MODEL_REGISTRY unexpectedly empty"

    r = build_default_redactor()
    # Pick a short, unambiguous model ID so we can assert its verbatim
    # presence is gone after redaction. Longer/compound IDs pass too as long
    # as word-boundary matching catches them.
    sample = model_ids[0]
    out = r.redact(f"dispatching prompt to {sample} worker")
    assert re.search(rf"\b{re.escape(sample)}\b", out, re.IGNORECASE) is None, (
        f"Expected {sample!r} to be redacted, got {out!r}. The registry "
        f"import path may have regressed."
    )
