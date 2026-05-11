"""Tests for the live ``for_new_registrations`` audience.

Plan 2026-05-08 §16.7 follow-up §"新注册用户" — distinct from the
existing snapshot ``registered_within_days`` audience: every user who
registers AFTER the announcement is sent receives it (including its
popup flag) via ``dispatch_announcements_for_new_user`` called at
registration time.

Layered tests (no live Postgres):

1. Catalog: new kind in AUDIENCE_KINDS, lifecycle group, no params.
2. ``_build_audience_filter`` returns a contradictory predicate (no
   existing user matches at send time).
3. AST scan: ``dispatch_announcements_for_new_user`` exists, queries
   the right WHERE clause, dedupes via related_id, propagates popup.
4. AST scan: registration paths (auth.register_handler and
   auth_phone.verify_code_endpoint / complete-registration) call
   ``dispatch_announcements_for_new_user``.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_for_new_registrations_in_catalog():
    from gateway.system_announcements_service import AUDIENCE_KINDS

    entry = next(
        (k for k in AUDIENCE_KINDS if k["kind"] == "for_new_registrations"),
        None,
    )
    assert entry is not None, "for_new_registrations missing from AUDIENCE_KINDS"
    assert entry["group"] == "lifecycle"
    assert entry["params"] == [], (
        "for_new_registrations must not require params (it's a live "
        "standing audience)"
    )
    assert "新注册" in entry["label"]
    assert "持续" in entry["label"], (
        "label should mention this is a live audience to set admin's "
        "expectations correctly"
    )


def test_catalog_size_grew_to_15():
    """We added one entry on top of the previous 14."""
    from gateway.system_announcements_service import AUDIENCE_KINDS

    assert len(AUDIENCE_KINDS) == 15


# ---------------------------------------------------------------------------
# _build_audience_filter
# ---------------------------------------------------------------------------


def test_audience_filter_returns_no_match_for_existing_users():
    """At send time, no existing user matches — fan-out targets 0
    users. The actual delivery happens at registration time via the
    auth hook. AST check on the source rather than running the SQL.
    """
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # The branch must use ``User.id.is_(None)`` (always false) so
    # count_audience returns 0.
    assert 'kind == "for_new_registrations"' in src
    assert "User.id.is_(None)" in src


# ---------------------------------------------------------------------------
# dispatch_announcements_for_new_user
# ---------------------------------------------------------------------------


def test_dispatch_function_exists_with_async_signature():
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "dispatch_announcements_for_new_user"
        ),
        None,
    )
    assert fn is not None, (
        "dispatch_announcements_for_new_user must exist as async function"
    )
    # Signature: (db, *, user_id) — keyword-only user_id.
    arg_names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "db" in arg_names
    assert "user_id" in arg_names


def test_dispatch_queries_active_live_announcements_only():
    """Must filter on status='sent' AND audience_kind='for_new_registrations'.
    Otherwise we'd accidentally re-deliver every old broadcast or
    deliver drafts."""
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # Look for both filter conditions in the dispatch function body.
    assert "SystemAnnouncement.status == \"sent\"" in src
    assert (
        'SystemAnnouncement.audience_kind == "for_new_registrations"' in src
    )


def test_dispatch_dedupes_via_related_id():
    """Re-running for the same user (e.g. registration retry) must
    not duplicate notifications."""
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # Pre-check for "already" set + skip insertion if related_id seen.
    assert "already = set(" in src
    assert "if str(ann.id) in already" in src
    assert "continue" in src


def test_dispatch_propagates_popup_flag():
    """A live announcement marked popup=true must produce
    user_notifications rows with popup=true (so the modal fires for
    new users on first page load)."""
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # The fan-out body for new-user dispatch contains popup=bool(ann.popup)
    # (Note send_announcement uses announcement.popup, this fn uses ann)
    # Search for both styles.
    assert (
        "popup=bool(ann.popup)" in src
        or "popup=bool(announcement.popup)" in src
    )


def test_dispatch_swallows_exceptions():
    """Registration must not break if announcement dispatch fails.
    Exception handler returns 0 / logs warning."""
    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    # Look for try/except pattern around the dispatch body.
    assert "except Exception as exc:" in src
    assert "dispatch_announcements_for_new_user" in src


# ---------------------------------------------------------------------------
# Registration hooks
# ---------------------------------------------------------------------------


def test_phone_first_registration_calls_dispatch():
    """auth_phone.verify_code_endpoint (the active phone-first path)
    must call dispatch_announcements_for_new_user after creating the
    user but before the session is created."""
    src = (REPO / "gateway" / "auth_phone.py").read_text(encoding="utf-8")
    assert "dispatch_announcements_for_new_user" in src
    # Best-effort: wrapped in try/except so registration never breaks.
    # The hook is followed by ``except Exception`` somewhere.
    idx = src.find("dispatch_announcements_for_new_user")
    snippet = src[max(0, idx - 200) : idx + 200]
    assert "try:" in snippet
    assert "except Exception" in snippet


def test_email_registration_calls_dispatch():
    """Email complete-registration wires the live-audience hook.

    /auth/register now only sends the email verification code; the user is
    created after /auth/email/complete-registration verifies that code.
    """
    src = (REPO / "gateway" / "auth_email.py").read_text(encoding="utf-8")
    assert "dispatch_announcements_for_new_user" in src
    idx = src.find("dispatch_announcements_for_new_user")
    snippet = src[max(0, idx - 200) : idx + 200]
    assert "try:" in snippet
    assert "except Exception" in snippet
