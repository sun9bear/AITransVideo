"""T1-7 — Gateway server-side log redaction integration.

Tests the ``_serve_redacted_logs`` helper logic directly (no httpx mock /
TestClient required) — wire: upstream response → redactor → final body.

The helper is an async FastAPI handler in ``gateway/job_intercept.py``.
We drive it via a minimal mocked proxy to isolate the redaction branch
from real DB / auth plumbing. Full HTTP-level coverage happens manually
in §17.4 smoke.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import Response

_GATEWAY_DIR = Path(__file__).resolve().parents[1] / "gateway"
_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
for _cand in (_GATEWAY_DIR, _SRC_DIR):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

# Import after sys.path adjustment. job_intercept brings in FastAPI etc.;
# this is heavy but only happens once per test session.
import job_intercept  # type: ignore[import-not-found]


class _FakeUser:
    def __init__(self, role: str) -> None:
        self.role = role


def _body_for(events: list[dict[str, Any]], lines: list[str]) -> bytes:
    return json.dumps({
        "job_id": "job_abc",
        "events": events,
        "lines": lines,
    }, ensure_ascii=False).encode("utf-8")


async def _invoke(monkeypatch, *, user: _FakeUser | None, upstream_body: bytes, status: int = 200) -> Response:
    """Run ``_serve_redacted_logs`` with a mocked ``proxy_request``."""

    async def fake_proxy(**kwargs):  # type: ignore[no-untyped-def]
        return Response(
            content=upstream_body,
            status_code=status,
            media_type="application/json",
        )

    monkeypatch.setattr(job_intercept, "proxy_request", fake_proxy)

    class _FakeRequest:
        method = "GET"

    return await job_intercept._serve_redacted_logs(_FakeRequest(), user)


def _parse(resp: Response) -> dict:
    return json.loads(resp.body.decode("utf-8"))


# ---------------------------------------------------------------------------
# admin bypass
# ---------------------------------------------------------------------------


def test_admin_sees_raw_message_with_provider_names(monkeypatch) -> None:
    events = [
        {"message": "[S1] Uploading audio to AssemblyAI...",
         "event_type": "log", "stage": "media_understanding"},
    ]
    lines = ["calling MiniMax with voice_a"]
    body = _body_for(events, lines)

    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("admin"), upstream_body=body))

    assert resp.status_code == 200
    data = _parse(resp)
    # No redaction for admins
    assert "AssemblyAI" in data["events"][0]["message"]
    assert "MiniMax" in data["lines"][0]


def test_non_admin_message_provider_names_stripped(monkeypatch) -> None:
    events = [
        {"message": "[S1] Uploading audio to AssemblyAI...",
         "event_type": "log", "stage": "media_understanding"},
        {"message": "[S3] Gemini translated 42 segments",
         "event_type": "log", "stage": "translation"},
    ]
    lines = ["calling MiniMax with voice_a", "CosyVoice rendered segment"]
    body = _body_for(events, lines)

    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))

    data = _parse(resp)
    for ev in data["events"]:
        assert "AssemblyAI" not in ev["message"]
        assert "Gemini" not in ev["message"]
    assert all("MiniMax" not in ln and "CosyVoice" not in ln for ln in data["lines"])


def test_non_admin_task_id_uuid_stripped(monkeypatch) -> None:
    events = [
        {"message": "任务ID=11111111-2222-3333-4444-555555555555 started",
         "event_type": "status"},
    ]
    body = _body_for(events, [])

    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))

    data = _parse(resp)
    assert "1111" not in data["events"][0]["message"]
    assert "任务ID" not in data["events"][0]["message"]


def test_null_user_treated_as_non_admin(monkeypatch) -> None:
    """When auth is disabled (``user is None``), we default to redaction —
    safer than leaking by accident."""
    events = [{"message": "calling AssemblyAI", "event_type": "log"}]
    body = _body_for(events, [])

    resp = asyncio.run(_invoke(monkeypatch, user=None, upstream_body=body))

    data = _parse(resp)
    assert "AssemblyAI" not in data["events"][0]["message"]


# ---------------------------------------------------------------------------
# Upstream failures pass through verbatim
# ---------------------------------------------------------------------------


def test_non_200_upstream_passes_through(monkeypatch) -> None:
    body = b"{\"error\": \"not found\"}"
    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body, status=404))
    assert resp.status_code == 404
    assert resp.body == body


def test_non_json_upstream_passes_through(monkeypatch) -> None:
    """Fail open on unexpected body shape — we never 500 the logs endpoint."""
    body = b"not json at all"
    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))
    assert resp.status_code == 200
    assert resp.body == body


def test_json_without_events_or_lines_untouched(monkeypatch) -> None:
    body = json.dumps({"job_id": "x"}).encode("utf-8")
    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))
    assert resp.status_code == 200
    # Shape preserved; no crash even though events/lines are absent
    assert json.loads(resp.body.decode())["job_id"] == "x"


# ---------------------------------------------------------------------------
# Schema preservation — everything except message/lines is unchanged
# ---------------------------------------------------------------------------


def test_schema_preserved_except_message_field(monkeypatch) -> None:
    events = [
        {
            "message": "calling AssemblyAI",
            "event_type": "log",
            "stage": "media_understanding",
            "status": "running",
            "level": "info",
            "created_at": "2026-04-19T10:00:00Z",
        },
    ]
    body = _body_for(events, [])

    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))

    data = _parse(resp)
    ev = data["events"][0]
    assert ev["event_type"] == "log"
    assert ev["stage"] == "media_understanding"
    assert ev["status"] == "running"
    assert ev["level"] == "info"
    assert ev["created_at"] == "2026-04-19T10:00:00Z"
    # Only message was redacted
    assert "AssemblyAI" not in ev["message"]


def test_events_with_non_string_message_handled(monkeypatch) -> None:
    events = [
        {"message": None, "event_type": "status"},
        {"message": 42, "event_type": "log"},
        {"message": "calling MiniMax", "event_type": "log"},
    ]
    body = _body_for(events, [])
    resp = asyncio.run(_invoke(monkeypatch, user=_FakeUser("user"), upstream_body=body))
    data = _parse(resp)
    # Non-string messages pass through; only the third was redacted
    assert data["events"][0]["message"] is None
    assert data["events"][1]["message"] == 42
    assert "MiniMax" not in data["events"][2]["message"]
