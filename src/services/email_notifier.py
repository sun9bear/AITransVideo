"""Email notification via Resend API.

Sends task completion/failure notifications to users.
Requires RESEND_API_KEY environment variable.
"""

from __future__ import annotations

import json
import logging
import os
from urllib import error, request

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM_EMAIL = "AIVideoTrans <noreply@aivideotrans.site>"

EMAIL_TEMPLATES = {
    "job_completed": {
        "subject": "任务完成：{job_title}",
        "html": """\
<div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px;">
  <div style="text-align: center; margin-bottom: 24px;">
    <div style="display: inline-block; background: linear-gradient(135deg, #8B5CF6, #06B6D4); color: white; width: 48px; height: 48px; border-radius: 12px; line-height: 48px; font-size: 18px; font-weight: bold;">AI</div>
  </div>
  <h2 style="color: #1a1a2e; text-align: center; margin: 0 0 8px;">任务已完成</h2>
  <p style="color: #666; text-align: center; margin: 0 0 24px; font-size: 14px;">{job_title}</p>
  <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 16px; margin-bottom: 24px;">
    <p style="color: #166534; margin: 0; font-size: 14px;">您的视频翻译配音任务已完成，可以下载结果文件。</p>
  </div>
  <div style="text-align: center;">
    <a href="{workspace_url}" style="display: inline-block; background: linear-gradient(135deg, #8B5CF6, #7C3AED); color: white; padding: 12px 32px; border-radius: 24px; text-decoration: none; font-size: 14px; font-weight: 600;">查看结果</a>
  </div>
  <p style="color: #999; font-size: 12px; text-align: center; margin-top: 32px;">AIVideoTrans · AI 视频翻译配音工作台</p>
</div>""",
    },
    "job_failed": {
        "subject": "任务失败：{job_title}",
        "html": """\
<div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px;">
  <div style="text-align: center; margin-bottom: 24px;">
    <div style="display: inline-block; background: linear-gradient(135deg, #8B5CF6, #06B6D4); color: white; width: 48px; height: 48px; border-radius: 12px; line-height: 48px; font-size: 18px; font-weight: bold;">AI</div>
  </div>
  <h2 style="color: #1a1a2e; text-align: center; margin: 0 0 8px;">任务处理失败</h2>
  <p style="color: #666; text-align: center; margin: 0 0 24px; font-size: 14px;">{job_title}</p>
  <div style="background: #fef2f2; border: 1px solid #fecaca; border-radius: 12px; padding: 16px; margin-bottom: 24px;">
    <p style="color: #991b1b; margin: 0; font-size: 14px;">任务处理过程中出现错误，请查看详情并重试。</p>
  </div>
  <div style="text-align: center;">
    <a href="{workspace_url}" style="display: inline-block; background: #6b7280; color: white; padding: 12px 32px; border-radius: 24px; text-decoration: none; font-size: 14px; font-weight: 600;">查看详情</a>
  </div>
  <p style="color: #999; font-size: 12px; text-align: center; margin-top: 32px;">AIVideoTrans · AI 视频翻译配音工作台</p>
</div>""",
    },
}


def send_notification(
    *,
    to_email: str,
    event_type: str,
    job_title: str = "",
    job_id: str = "",
    base_url: str = "https://us.aivideotrans.site",
) -> bool:
    """Send an email notification. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning("RESEND_API_KEY not set, skipping email notification")
        return False

    if not to_email or not to_email.strip():
        return False

    template = EMAIL_TEMPLATES.get(event_type)
    if not template:
        logger.warning("Unknown email event type: %s", event_type)
        return False

    workspace_url = f"{base_url}/workspace/{job_id}" if job_id else base_url
    display_title = job_title or "未命名任务"

    subject = template["subject"].format(job_title=display_title)
    html = template["html"].format(
        job_title=display_title,
        workspace_url=workspace_url,
    )

    payload = {
        "from": DEFAULT_FROM_EMAIL,
        "to": [to_email.strip()],
        "subject": subject,
        "html": html,
    }

    try:
        req = request.Request(
            RESEND_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger.info("Email sent to %s for event %s", to_email, event_type)
        return True
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        logger.warning("Resend API error %d: %s", exc.code, body[:200])
        return False
    except Exception:
        logger.exception("Failed to send email notification")
        return False
