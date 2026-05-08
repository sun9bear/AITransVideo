"""Tests for the notification dispatch map + payload sanitizer."""
from __future__ import annotations


def test_dispatch_map_has_terminal_job_events():
    from gateway.notification_dispatch_map import (
        DISPATCH_MAP,
        EVENT_JOB_FAILED,
        EVENT_JOB_SUCCEEDED,
    )

    assert EVENT_JOB_SUCCEEDED in DISPATCH_MAP
    assert EVENT_JOB_FAILED in DISPATCH_MAP
    succ = DISPATCH_MAP[EVENT_JOB_SUCCEEDED]
    assert succ["scope"] == "job"
    assert succ["topic"] == "artifact"
    assert succ["severity"] in {"info", "success", "warning", "error"}


def test_get_recipe_unknown_returns_none():
    from gateway.notification_dispatch_map import get_recipe

    assert get_recipe("nonexistent.event") is None


def test_payload_sanitizer_drops_unknown_keys():
    from gateway.notifications_service import _sanitized_payload

    raw = {
        "display_name": "测试",
        "job_id": "abc",
        "extra_key": "should be dropped",
        "project_dir": "/opt/aivideotrans/internal",
    }
    safe = _sanitized_payload(raw)
    assert "display_name" in safe
    assert "job_id" in safe
    assert "extra_key" not in safe
    assert "project_dir" not in safe


def test_payload_sanitizer_handles_none():
    from gateway.notifications_service import _sanitized_payload

    assert _sanitized_payload(None) == {}
    assert _sanitized_payload({}) == {}


def test_dispatch_map_has_artifact_events():
    from gateway.notification_dispatch_map import (
        EVENT_ARTIFACT_JIANYING_DRAFT_READY,
        EVENT_ARTIFACT_MATERIALS_PACK_READY,
        get_recipe,
    )

    for ev in (
        EVENT_ARTIFACT_JIANYING_DRAFT_READY,
        EVENT_ARTIFACT_MATERIALS_PACK_READY,
    ):
        recipe = get_recipe(ev)
        assert recipe is not None
        assert recipe["scope"] == "job"
        assert recipe["topic"] == "artifact"


def test_dispatch_map_has_support_events():
    from gateway.notification_dispatch_map import (
        EVENT_SUPPORT_HANDOFF_CLOSED,
        EVENT_SUPPORT_HUMAN_REPLIED,
        get_recipe,
    )

    for ev in (
        EVENT_SUPPORT_HUMAN_REPLIED,
        EVENT_SUPPORT_HANDOFF_CLOSED,
    ):
        r = get_recipe(ev)
        assert r is not None
        assert r["scope"] == "user"
        assert r["topic"] == "support"
