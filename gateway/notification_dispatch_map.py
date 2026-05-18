"""Hardcoded event → notification template map.

Plan §16.5 / §16.7 — notification P1 does NOT ship an admin editor for
this map. Every event that should produce a user-visible notification
is listed here as a Python dict; changes go via PR review, not runtime
config.

Each entry maps a stable ``event_type`` string to a recipe used by
``notifications_service.dispatch_event``:

- ``scope`` — "user" or "job".
- ``topic`` — billing / account / artifact / support / maintenance.
- ``severity`` — info / success / warning / error.
- ``title`` / ``body`` — Python format strings; the dispatch helper
  passes a sanitized payload as keyword args.
- ``action_url`` — optional, also a format string.
- ``related_type`` — optional, e.g. "billing_order".

Adding a new event: pick a stable name, add an entry below, and call
``notifications_service.dispatch_event(event_type, payload, ...)``
from the appropriate place. AST guard tests do NOT enforce coverage of
events; the codebase chooses which events deserve a notification.
"""

from __future__ import annotations

from typing import Any


# Job lifecycle
EVENT_JOB_SUCCEEDED = "job.succeeded"
EVENT_JOB_FAILED = "job.failed"
EVENT_JOB_PUBLISHED = "job.published"
EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE = "job.content_compliance_admin_override"
EVENT_ARTIFACT_JIANYING_DRAFT_READY = "artifact.jianying_draft_ready"
EVENT_ARTIFACT_MATERIALS_PACK_READY = "artifact.materials_pack_ready"

# Account / billing
EVENT_TRIAL_GRANTED = "account.trial_granted"
EVENT_PAYMENT_SETTLED = "billing.payment_settled"
EVENT_SUBSCRIPTION_ACTIVATED = "billing.subscription_activated"

# Support
EVENT_SUPPORT_HUMAN_REPLIED = "support.human_replied"
EVENT_SUPPORT_HANDOFF_CLOSED = "support.handoff_closed"

# Pan backup (Phase 6 §T6.3-T6.4; CodeX 2026-05-18 P1-2: previously
# auth.py dispatched 'pan_credentials_revoked' but no recipe existed —
# silent drop). The token_refresh background task fires this when a
# refresh attempt fails (network / Baidu rejection / token rotation
# race that lost), marking the credential 'revoked' in PG.
EVENT_PAN_TOKEN_REVOKED = "pan.token_revoked"


DISPATCH_MAP: dict[str, dict[str, Any]] = {
    EVENT_JOB_SUCCEEDED: {
        "scope": "job",
        "topic": "artifact",
        "severity": "success",
        "title": "任务已完成",
        "body": "「{display_name}」处理完成，可以下载中文配音视频和字幕。",
        "action_url": "/workspace/{job_id}",
        "artifact_key": "dubbed_video",
    },
    EVENT_JOB_FAILED: {
        "scope": "job",
        "topic": "artifact",
        "severity": "error",
        "title": "任务处理失败",
        "body": "「{display_name}」处理失败。失败任务不会扣除额度，可在工作台重新提交。",
        "action_url": "/workspace/{job_id}",
    },
    EVENT_JOB_PUBLISHED: {
        "scope": "job",
        "topic": "artifact",
        "severity": "success",
        "title": "新版本已发布",
        "body": "「{display_name}」修改后的新版本已生成，可下载或继续编辑。",
        "action_url": "/workspace/{job_id}",
        "artifact_key": "dubbed_video",
    },
    EVENT_JOB_CONTENT_COMPLIANCE_ADMIN_OVERRIDE: {
        "scope": "job",
        "topic": "artifact",
        "severity": "warning",
        "title": "内容审核提醒",
        "body": "「{display_name}」疑似包含敏感内容，已按管理员特权继续翻译流程。{summary}",
        "action_url": "/workspace/{job_id}",
        "related_type": "content_compliance",
        "popup": True,
    },
    EVENT_ARTIFACT_JIANYING_DRAFT_READY: {
        "scope": "job",
        "topic": "artifact",
        "severity": "success",
        "title": "剪映草稿已生成",
        "body": "「{display_name}」的剪映草稿工程已就绪，可下载继续精剪。",
        "action_url": "/workspace/{job_id}",
        "artifact_key": "jianying_draft",
    },
    EVENT_ARTIFACT_MATERIALS_PACK_READY: {
        "scope": "job",
        "topic": "artifact",
        "severity": "success",
        "title": "素材包已生成",
        "body": "「{display_name}」的原始素材包已就绪，可下载备份或二次创作。",
        "action_url": "/workspace/{job_id}",
        "artifact_key": "materials_pack",
    },
    EVENT_TRIAL_GRANTED: {
        "scope": "user",
        "topic": "account",
        "severity": "success",
        "title": "已开通试用",
        "body": "你的试用已开通：{trial_days} 天免费体验，含 {trial_minutes} 分钟视频处理额度。",
        "action_url": "/pricing",
    },
    EVENT_PAYMENT_SETTLED: {
        "scope": "user",
        "topic": "billing",
        "severity": "success",
        "title": "支付已到账",
        "body": "{plan_name} 套餐订阅成功，新额度已发放。",
        "action_url": "/account/billing",
        "related_type": "billing_order",
    },
    EVENT_SUBSCRIPTION_ACTIVATED: {
        "scope": "user",
        "topic": "billing",
        "severity": "info",
        "title": "套餐已生效",
        "body": "你的 {plan_name} 套餐已激活，可在工作台开始使用。",
        "action_url": "/workspace",
    },
    EVENT_SUPPORT_HUMAN_REPLIED: {
        "scope": "user",
        "topic": "support",
        "severity": "info",
        "title": "客服已回复",
        "body": "你的客服工单有新的人工回复，点击查看。",
        "action_url": "/notifications",
        "related_type": "support_handoff",
    },
    EVENT_SUPPORT_HANDOFF_CLOSED: {
        "scope": "user",
        "topic": "support",
        "severity": "info",
        "title": "客服工单已关闭",
        "body": "你的客服工单已处理完成。如还有问题可重新发起咨询。",
        "action_url": "/notifications",
        "related_type": "support_handoff",
    },
    EVENT_PAN_TOKEN_REVOKED: {
        "scope": "user",
        "topic": "account",
        "severity": "warning",
        "title": "网盘授权已失效",
        "body": "你的网盘授权已失效,需要重新连接才能继续备份/恢复。",
        "action_url": "/admin/pan/dashboard",
    },
}


def get_recipe(event_type: str) -> dict[str, Any] | None:
    return DISPATCH_MAP.get(event_type)
