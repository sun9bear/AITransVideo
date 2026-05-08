"""Tests for the popup-on-login announcement feature.

Plan 2026-05-08 §16.7 follow-up §"管理员发通知的时候，加一个选项：是否
弹窗".

Three layers covered without spinning up Postgres:

1. **Migration 024 shape** — revision id ≤ 32 chars, chains to 023,
   adds the three columns + the partial index.
2. **Pydantic schemas** — AnnouncementInput / AnnouncementView /
   PopupNotificationView / DismissPopupResponse carry the right
   fields with the right defaults.
3. **Service + API source AST** — send_announcement copies
   ``announcement.popup`` into each fan-out row; clone_for_resend
   copies it; ``GET /popups`` filters on popup=true AND
   popup_dismissed_at IS NULL.

Live integration (real Postgres + actual fan-out) is out of scope
here; the production verify step in deploy covers it end-to-end.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Migration 024
# ---------------------------------------------------------------------------


def test_migration_024_revision_id_under_32_chars():
    src = (
        REPO / "gateway" / "alembic" / "versions" / "024_announcement_popup.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    rev: str | None = None
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "revision"
            and isinstance(n.value, ast.Constant)
        ):
            rev = n.value.value
            break
    assert rev is not None
    assert len(rev) <= 32, f"revision {rev!r} too long ({len(rev)})"


def test_migration_024_chains_to_023():
    src = (
        REPO / "gateway" / "alembic" / "versions" / "024_announcement_popup.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "023_system_announcements"' in src


def test_migration_024_adds_required_columns():
    src = (
        REPO / "gateway" / "alembic" / "versions" / "024_announcement_popup.py"
    ).read_text(encoding="utf-8")
    # Three columns added, all explicit:
    assert "system_announcements" in src
    assert "user_notifications" in src
    assert '"popup"' in src
    assert '"popup_dismissed_at"' in src
    # Partial index for fast active-popup lookup.
    assert "idx_user_notifications_active_popups" in src
    assert "popup = true AND popup_dismissed_at IS NULL" in src


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


def test_announcement_input_has_popup_flag_default_false():
    from gateway.support_models import AnnouncementInput

    fields = AnnouncementInput.model_fields
    assert "popup" in fields
    a = AnnouncementInput(title="x", body="y", audience_kind="all")
    assert a.popup is False


def test_announcement_input_accepts_popup_true():
    from gateway.support_models import AnnouncementInput

    a = AnnouncementInput(
        title="x", body="y", audience_kind="all", popup=True
    )
    assert a.popup is True


def test_announcement_view_carries_popup():
    from gateway.support_models import AnnouncementView

    fields = AnnouncementView.model_fields
    assert "popup" in fields


def test_popup_notification_view_shape():
    from gateway.support_models import PopupNotificationView

    fields = PopupNotificationView.model_fields
    expected = {"id", "title", "body", "severity", "topic", "action_url", "created_at"}
    assert expected.issubset(set(fields.keys()))


def test_dismiss_popup_response_shape():
    from gateway.support_models import DismissPopupResponse

    fields = DismissPopupResponse.model_fields
    assert "notification_id" in fields
    assert "dismissed_at" in fields
    assert "also_marked_read" in fields


# ---------------------------------------------------------------------------
# Service AST: send_announcement copies popup
# ---------------------------------------------------------------------------


def test_send_announcement_copies_popup_into_user_notifications():
    """The fan-out loop must set ``popup=announcement.popup`` on each
    UserNotification row, otherwise the modal feature silently
    degrades to "always quiet bell only"."""
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # The line is: ``popup=bool(announcement.popup),``
    assert "popup=bool(announcement.popup)" in src, (
        "send_announcement must propagate ``popup`` to user_notifications "
        "rows; without this the feature silently no-ops."
    )


def test_clone_for_resend_copies_popup():
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    assert "popup=bool(source.popup)" in src, (
        "clone_for_resend must copy ``popup`` so re-sending preserves "
        "the modal flag."
    )


# ---------------------------------------------------------------------------
# notifications_api: popups endpoints exist with admin-free auth
# ---------------------------------------------------------------------------


def test_notifications_api_has_popups_endpoint():
    src = (
        REPO / "gateway" / "notifications_api.py"
    ).read_text(encoding="utf-8")
    # The list endpoint
    assert '@router.get("/popups"' in src
    # The dismiss endpoint
    assert "/popups/{notification_id}/dismiss" in src
    # Filter must be popup=true AND popup_dismissed_at IS NULL.
    assert "UserNotification.popup.is_(True)" in src
    assert "UserNotification.popup_dismissed_at.is_(None)" in src


# ---------------------------------------------------------------------------
# Admin API: create/patch propagate popup
# ---------------------------------------------------------------------------


def test_admin_api_creates_announcement_with_popup():
    src = (
        REPO / "gateway" / "admin_support_api.py"
    ).read_text(encoding="utf-8")
    # Both create_announcement and update_announcement assign popup.
    occurrences = src.count("popup=bool(body.popup)")
    assert occurrences >= 1, "create_announcement must set popup=bool(body.popup)"
    occurrences_patch = src.count("row.popup = bool(body.popup)")
    assert occurrences_patch >= 1, (
        "update_announcement must update row.popup from body.popup"
    )


def test_admin_api_serializer_emits_popup():
    src = (
        REPO / "gateway" / "admin_support_api.py"
    ).read_text(encoding="utf-8")
    assert "popup=bool(row.popup)" in src, (
        "_serialize_announcement must include popup in the response"
    )
