"""System announcement composer + audience resolver + send orchestrator.

Plan 2026-05-08 §16.7 follow-up.

The two halves of this module:

1. **Audience resolution** — given an ``audience_kind`` and optional
   ``audience_params``, return a list of ``user_id`` matching the
   predicate. Used both for the "preview count" UI and the actual
   fan-out at send time.
2. **Send orchestration** — copy the announcement into one
   ``user_notifications`` row per recipient, capture the count,
   transition the announcement to ``status="sent"``, and record a
   ``sent_at`` timestamp.

The full set of audience kinds (14 total in P1) is declared in
``AUDIENCE_KINDS`` so the admin UI / API can render a dropdown without
hardcoding strings on the frontend.

Non-goals in P1:
- Scheduled / time-windowed audiences (no background worker).
- Live audiences (announcement persists; new users entering the
  audience after send do NOT get back-filled). See plan §16.7 for the
  rationale: snapshot-at-send is simpler and matches admin's mental
  model ("I sent this batch at this moment").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Job,
    SupportConversation,
    SystemAnnouncement,
    User,
    UserNotification,
)

logger = logging.getLogger(__name__)


AudienceKind = Literal[
    "all",
    "registered_within_days",
    "plan_free",
    "plan_plus",
    "plan_pro",
    "plan_paid",
    "trial_active",
    "trial_ending_within_days",
    "trial_ended_within_days",
    "paid_no_jobs",
    "inactive_for_days",
    "active_with_jobs_within_days",
    "had_failures_within_days",
    "admin_only",
]


# Catalog: machine name → human label + parameter schema. Used by the
# admin frontend to render the audience picker without hardcoding
# strings on both sides.
AUDIENCE_KINDS: list[dict[str, Any]] = [
    {"kind": "all", "label": "全体用户", "params": [], "group": "broad"},
    {
        "kind": "admin_only",
        "label": "仅管理员（用于测试发送）",
        "params": [],
        "group": "broad",
    },
    {
        "kind": "registered_within_days",
        "label": "N 天内注册",
        "params": [{"key": "days", "type": "int", "default": 7, "min": 1, "max": 365}],
        "group": "lifecycle",
    },
    {"kind": "plan_free", "label": "Free 套餐", "params": [], "group": "subscription"},
    {"kind": "plan_plus", "label": "Plus 订阅", "params": [], "group": "subscription"},
    {"kind": "plan_pro", "label": "Pro 订阅", "params": [], "group": "subscription"},
    {
        "kind": "plan_paid",
        "label": "全部付费用户（Plus + Pro）",
        "params": [],
        "group": "subscription",
    },
    {
        "kind": "trial_active",
        "label": "试用中",
        "params": [],
        "group": "lifecycle",
    },
    {
        "kind": "trial_ending_within_days",
        "label": "试用即将到期（N 天内）",
        "params": [{"key": "days", "type": "int", "default": 3, "min": 1, "max": 30}],
        "group": "lifecycle",
    },
    {
        "kind": "trial_ended_within_days",
        "label": "试用刚结束（N 天内）",
        "params": [{"key": "days", "type": "int", "default": 7, "min": 1, "max": 90}],
        "group": "lifecycle",
    },
    {
        "kind": "paid_no_jobs",
        "label": "已订阅但未发任务",
        "params": [],
        "group": "behavior",
    },
    {
        "kind": "inactive_for_days",
        "label": "沉睡用户（N 天无任务）",
        "params": [{"key": "days", "type": "int", "default": 30, "min": 7, "max": 365}],
        "group": "behavior",
    },
    {
        "kind": "active_with_jobs_within_days",
        "label": "活跃用户（N 天内 ≥ M 个任务）",
        "params": [
            {"key": "days", "type": "int", "default": 30, "min": 1, "max": 90},
            {"key": "min_jobs", "type": "int", "default": 5, "min": 1, "max": 100},
        ],
        "group": "behavior",
    },
    {
        "kind": "had_failures_within_days",
        "label": "近 N 天有过失败任务",
        "params": [{"key": "days", "type": "int", "default": 7, "min": 1, "max": 90}],
        "group": "behavior",
    },
]


def _validate_params(kind: str, params: dict | None) -> dict:
    """Validate and normalize ``audience_params`` for the given kind.

    Returns a dict with the same key set as the kind's ``params``
    schema (filling defaults for missing keys, clamping out-of-range).
    Raises ``ValueError`` for unknown kinds.
    """
    spec = next((k for k in AUDIENCE_KINDS if k["kind"] == kind), None)
    if spec is None:
        raise ValueError(f"unknown audience_kind: {kind!r}")
    out: dict[str, Any] = {}
    src = params or {}
    for p in spec["params"]:
        key = p["key"]
        ptype = p["type"]
        if ptype == "int":
            try:
                v = int(src.get(key, p["default"]))
            except (TypeError, ValueError):
                v = int(p["default"])
            v = max(int(p["min"]), min(int(p["max"]), v))
            out[key] = v
        else:
            out[key] = src.get(key, p["default"])
    return out


def _build_audience_filter(kind: str, params: dict):
    """Return a SQLAlchemy WHERE clause for the audience predicate.

    Each branch returns an expression usable in
    ``select(User).where(<expr>)``. The caller wraps with the
    ``is_active=True`` and admin-exclusion filter as appropriate.
    """
    now = datetime.now(timezone.utc)

    if kind == "all":
        # Active users only — banned/disabled accounts don't get
        # broadcast notifications.
        return User.is_active.is_(True)

    if kind == "admin_only":
        return and_(User.is_active.is_(True), User.role == "admin")

    if kind == "registered_within_days":
        cutoff = now - timedelta(days=int(params["days"]))
        return and_(User.is_active.is_(True), User.created_at >= cutoff)

    if kind == "plan_free":
        return and_(User.is_active.is_(True), User.plan_code == "free")

    if kind == "plan_plus":
        return and_(User.is_active.is_(True), User.plan_code == "plus")

    if kind == "plan_pro":
        return and_(User.is_active.is_(True), User.plan_code == "pro")

    if kind == "plan_paid":
        return and_(
            User.is_active.is_(True),
            User.plan_code.in_(("plus", "pro")),
        )

    if kind == "trial_active":
        return and_(
            User.is_active.is_(True),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at > now,
        )

    if kind == "trial_ending_within_days":
        cutoff = now + timedelta(days=int(params["days"]))
        return and_(
            User.is_active.is_(True),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at > now,
            User.trial_ends_at <= cutoff,
        )

    if kind == "trial_ended_within_days":
        floor = now - timedelta(days=int(params["days"]))
        return and_(
            User.is_active.is_(True),
            User.trial_ends_at.is_not(None),
            User.trial_ends_at <= now,
            User.trial_ends_at >= floor,
        )

    if kind == "paid_no_jobs":
        # Paid users who have NEVER created a job. Sub-query for
        # "has at least one job" → invert.
        has_job = (
            select(Job.id)
            .where(Job.user_id == User.id)
            .exists()
        )
        return and_(
            User.is_active.is_(True),
            User.plan_code.in_(("plus", "pro")),
            ~has_job,
        )

    if kind == "inactive_for_days":
        cutoff = now - timedelta(days=int(params["days"]))
        # No job created in the last N days. Users with zero jobs ever
        # are also "inactive" by this definition (no created_at >= cutoff).
        recent = (
            select(Job.id)
            .where(and_(Job.user_id == User.id, Job.created_at >= cutoff))
            .exists()
        )
        return and_(User.is_active.is_(True), ~recent)

    if kind == "active_with_jobs_within_days":
        cutoff = now - timedelta(days=int(params["days"]))
        min_jobs = int(params["min_jobs"])
        # Subquery: count jobs per user in window. We can't put the
        # full HAVING into a WHERE; build a select(User.id).join with
        # group_by + having, then fold via in_().
        subq = (
            select(Job.user_id)
            .where(Job.created_at >= cutoff)
            .group_by(Job.user_id)
            .having(func.count(Job.id) >= min_jobs)
            .subquery()
        )
        return and_(User.is_active.is_(True), User.id.in_(select(subq.c.user_id)))

    if kind == "had_failures_within_days":
        cutoff = now - timedelta(days=int(params["days"]))
        had_fail = (
            select(Job.id)
            .where(
                and_(
                    Job.user_id == User.id,
                    Job.status == "failed",
                    Job.created_at >= cutoff,
                )
            )
            .exists()
        )
        return and_(User.is_active.is_(True), had_fail)

    raise ValueError(f"audience_kind {kind!r} has no SQL filter")


async def resolve_audience_user_ids(
    db: AsyncSession,
    *,
    kind: str,
    params: dict | None,
    limit: int | None = None,
) -> list[uuid.UUID]:
    """Return user_ids matching the audience predicate."""
    normalized = _validate_params(kind, params)
    where = _build_audience_filter(kind, normalized)
    stmt = select(User.id).where(where)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def count_audience(
    db: AsyncSession,
    *,
    kind: str,
    params: dict | None,
) -> int:
    """Cheap COUNT(*) for the preview UI ("将发送给 N 名用户")."""
    normalized = _validate_params(kind, params)
    where = _build_audience_filter(kind, normalized)
    stmt = select(func.count()).select_from(User).where(where)
    return int((await db.execute(stmt)).scalar() or 0)


# ---------------------------------------------------------------------------
# Send orchestration
# ---------------------------------------------------------------------------


# Hard rate limit: same admin can't send more than 1 announcement per
# minute. Defends against accidental double-clicks blasting 10k users
# twice. Captured here as a constant so the test can reference it.
SEND_COOLDOWN_SECONDS = 60


async def send_announcement(
    db: AsyncSession,
    *,
    announcement: SystemAnnouncement,
    admin_id: uuid.UUID,
) -> dict[str, Any]:
    """Fan out one user_notifications row per audience member.

    Idempotent at best-effort: re-sending the same announcement skips
    users who already have a notification with
    ``related_id=announcement.id``. So a re-send pulls in only newly-
    matching users (typically zero for snapshot audiences).

    Raises ``ValueError`` if rate limit hit, audience invalid, or
    announcement is already sent (clone first instead of re-sending).
    """
    if announcement.status == "sent":
        raise ValueError("announcement already sent — clone for re-send")

    # Rate limit: latest sent by THIS admin within cooldown.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SEND_COOLDOWN_SECONDS)
    recent = await db.execute(
        select(SystemAnnouncement.id)
        .where(
            and_(
                SystemAnnouncement.created_by_admin_id == admin_id,
                SystemAnnouncement.status == "sent",
                SystemAnnouncement.sent_at != None,  # noqa: E711
                SystemAnnouncement.sent_at >= cutoff,
            )
        )
        .limit(1)
    )
    if recent.scalar_one_or_none() is not None:
        raise ValueError(
            f"rate limited — same admin sent another announcement "
            f"within {SEND_COOLDOWN_SECONDS}s. Wait a minute and retry."
        )

    user_ids = await resolve_audience_user_ids(
        db,
        kind=announcement.audience_kind,
        params=announcement.audience_params,
    )

    # Skip users who already have a notification for this announcement
    # (idempotency for retries / re-sends).
    already_notified_stmt = select(UserNotification.user_id).where(
        and_(
            UserNotification.related_type == "system_announcement",
            UserNotification.related_id == str(announcement.id),
        )
    )
    already = set(
        (await db.execute(already_notified_stmt)).scalars().all()
    )

    targets = [uid for uid in user_ids if uid not in already]

    now = datetime.now(timezone.utc)
    for uid in targets:
        row = UserNotification(
            scope="system",
            topic=announcement.topic,
            user_id=uid,
            title=announcement.title[:255],
            body=announcement.body,
            severity=announcement.severity,
            related_type="system_announcement",
            related_id=str(announcement.id),
            action_url=announcement.action_url,
            popup=bool(announcement.popup),
            metadata_json={
                "announcement_id": str(announcement.id),
                "audience_kind": announcement.audience_kind,
                "popup": bool(announcement.popup),
            },
        )
        db.add(row)

    announcement.status = "sent"
    announcement.sent_at = now
    announcement.recipient_count = len(user_ids)  # full audience size, not delta
    announcement.updated_at = now

    await db.flush()

    return {
        "announcement_id": str(announcement.id),
        "audience_size": len(user_ids),
        "newly_notified": len(targets),
        "skipped_already_notified": len(user_ids) - len(targets),
    }


async def recall_announcement(
    db: AsyncSession,
    *,
    announcement: SystemAnnouncement,
) -> int:
    """Hard-delete all user_notifications fanned out from this announcement.

    Returns the number of rows deleted. Sets the announcement to
    ``status="archived"`` so it's hidden from the default sent list
    but still visible in the audit log.
    """
    from sqlalchemy import delete as sa_delete

    stmt = sa_delete(UserNotification).where(
        and_(
            UserNotification.related_type == "system_announcement",
            UserNotification.related_id == str(announcement.id),
        )
    )
    result = await db.execute(stmt)
    deleted = int(result.rowcount or 0)
    announcement.status = "archived"
    announcement.updated_at = datetime.now(timezone.utc)
    return deleted


async def stats_for(
    db: AsyncSession,
    *,
    announcement_id: uuid.UUID,
) -> dict[str, int]:
    """Read counts: total fanned-out, read, archived."""
    stmt = select(
        func.count(),
        func.count().filter(UserNotification.read_at.is_not(None)),
        func.count().filter(UserNotification.archived_at.is_not(None)),
    ).where(
        and_(
            UserNotification.related_type == "system_announcement",
            UserNotification.related_id == str(announcement_id),
        )
    )
    row = (await db.execute(stmt)).one()
    return {
        "total": int(row[0] or 0),
        "read": int(row[1] or 0),
        "archived": int(row[2] or 0),
    }


async def clone_for_resend(
    db: AsyncSession,
    *,
    source: SystemAnnouncement,
    admin_id: uuid.UUID,
) -> SystemAnnouncement:
    """Create a new draft announcement copied from a sent one.

    The clone is a fresh draft (not yet sent). Admin can edit any
    field before pressing send. ``parent_id`` carries the lineage for
    audit / "show all variants" UI.
    """
    clone = SystemAnnouncement(
        title=source.title,
        body=source.body,
        topic=source.topic,
        severity=source.severity,
        action_url=source.action_url,
        audience_kind=source.audience_kind,
        audience_params=dict(source.audience_params or {}),
        popup=bool(source.popup),
        created_by_admin_id=admin_id,
        parent_id=source.id,
        status="draft",
    )
    db.add(clone)
    await db.flush()
    return clone
