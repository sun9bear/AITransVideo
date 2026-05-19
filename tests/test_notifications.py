"""Tests for the notification dispatch map + payload sanitizer."""
from __future__ import annotations


def test_dispatch_map_has_terminal_job_events():
    from gateway.notification_dispatch_map import (
        DISPATCH_MAP,
        EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE,
        EVENT_JOB_FAILED,
        EVENT_JOB_SUCCEEDED,
    )

    assert EVENT_JOB_SUCCEEDED in DISPATCH_MAP
    assert EVENT_JOB_FAILED in DISPATCH_MAP
    assert EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE in DISPATCH_MAP
    succ = DISPATCH_MAP[EVENT_JOB_SUCCEEDED]
    assert succ["scope"] == "job"
    assert succ["topic"] == "artifact"
    assert succ["severity"] in {"info", "success", "warning", "error"}
    compliance = DISPATCH_MAP[EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE]
    assert compliance["scope"] == "job"
    assert compliance["severity"] == "warning"
    assert compliance["popup"] is True


def test_get_recipe_unknown_returns_none():
    from gateway.notification_dispatch_map import get_recipe

    assert get_recipe("nonexistent.event") is None


def test_payload_sanitizer_drops_unknown_keys():
    from gateway.notifications_service import _sanitized_payload

    raw = {
        "display_name": "测试",
        "job_id": "abc",
        "summary": "合规提醒",
        "extra_key": "should be dropped",
        "project_dir": "/opt/aivideotrans/internal",
    }
    safe = _sanitized_payload(raw)
    assert "display_name" in safe
    assert "job_id" in safe
    assert "summary" in safe
    assert "extra_key" not in safe
    assert "project_dir" not in safe


def test_dispatch_event_sets_popup_from_recipe(monkeypatch):
    import asyncio
    import uuid

    from gateway.notification_dispatch_map import (
        EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE,
    )
    import gateway.notifications_service as notifications_service

    class FakeNotification:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeDb:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            return None

    monkeypatch.setattr(notifications_service, "UserNotification", FakeNotification)

    async def run():
        db = FakeDb()
        notif = await notifications_service.dispatch_event(
            db,
            event_type=EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE,
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            job_id="job_admin",
            payload={
                "display_name": "Admin Task",
                "job_id": "job_admin",
                "summary": "Sensitive content warning",
            },
            dedupe_key="content-compliance-admin-override:job_admin",
        )
        assert notif is db.rows[0]
        assert notif.popup is True
        assert notif.scope == "job"
        assert notif.severity == "warning"

    asyncio.run(run())


def test_payload_sanitizer_handles_none():
    from gateway.notifications_service import _sanitized_payload

    assert _sanitized_payload(None) == {}


# ---------------------------------------------------------------------------
# CodeX 2026-05-19 P1a: 'reason' must be in _PAYLOAD_ALLOWLIST so the
# pan.backup.failed / pan.restore.failed recipes' "{reason}" template
# token actually interpolates. Without it, _sanitized_payload drops
# reason → format() raises KeyError → dispatcher falls back to raw
# template → users would see literal "「X」备份到网盘失败:{reason}".
# ---------------------------------------------------------------------------


def test_payload_sanitizer_keeps_reason_key():
    """Phase 9 §T9.3 + CodeX P1a regression."""
    from gateway.notifications_service import _sanitized_payload

    safe = _sanitized_payload({
        "display_name": "测试任务",
        "reason": "Baidu API 429 rate limited",
    })
    assert safe.get("reason") == "Baidu API 429 rate limited"
    assert safe.get("display_name") == "测试任务"


def test_dispatch_event_interpolates_reason_in_pan_backup_failed(monkeypatch):
    """End-to-end: dispatching pan.backup.failed with {reason} payload
    must produce a body containing the actual reason string — not the
    literal template token. Catches the bug where someone removes
    'reason' from _PAYLOAD_ALLOWLIST."""
    import asyncio
    import uuid

    from gateway.notification_dispatch_map import EVENT_PAN_BACKUP_FAILED
    import gateway.notifications_service as notifications_service

    class FakeNotification:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeDb:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            return None

    monkeypatch.setattr(
        notifications_service, "UserNotification", FakeNotification,
    )

    async def run():
        db = FakeDb()
        notif = await notifications_service.dispatch_event(
            db,
            event_type=EVENT_PAN_BACKUP_FAILED,
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            job_id="job_pan_fail",
            payload={
                "display_name": "测试任务",
                "reason": "Baidu API 429 rate limited",
            },
        )
        # Body must contain the substituted reason — NOT the literal
        # "{reason}" token.
        assert notif is db.rows[0]
        assert "Baidu API 429 rate limited" in notif.body
        assert "{reason}" not in notif.body
        assert "测试任务" in notif.body
        assert "{display_name}" not in notif.body
        # Sanity: severity + scope from the recipe.
        assert notif.severity == "error"
        assert notif.scope == "job"

    asyncio.run(run())


def test_dispatch_event_interpolates_reason_in_pan_restore_failed(monkeypatch):
    """Same contract for pan.restore.failed — covered separately so
    each recipe gets its own regression."""
    import asyncio
    import uuid

    from gateway.notification_dispatch_map import EVENT_PAN_RESTORE_FAILED
    import gateway.notifications_service as notifications_service

    class FakeNotification:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeDb:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            return None

    monkeypatch.setattr(
        notifications_service, "UserNotification", FakeNotification,
    )

    async def run():
        db = FakeDb()
        notif = await notifications_service.dispatch_event(
            db,
            event_type=EVENT_PAN_RESTORE_FAILED,
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            job_id="job_pan_restore",
            payload={
                "display_name": "恢复任务",
                "reason": "tar manifest sha256 mismatch",
            },
        )
        assert "tar manifest sha256 mismatch" in notif.body
        assert "{reason}" not in notif.body
        assert "恢复任务" in notif.body
        assert notif.severity == "error"

    asyncio.run(run())


def test_payload_sanitizer_handles_empty_dict():
    """Restored from original test_payload_sanitizer_handles_none —
    splitting None / {} into separate test functions for clarity
    (after CodeX P1a edit, 2026-05-19)."""
    from gateway.notifications_service import _sanitized_payload

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
