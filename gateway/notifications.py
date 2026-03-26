"""邮件通知模块。使用 Resend API 发送任务完成/失败通知。"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("NOTIFICATION_FROM", "noreply@aivideotrans.site")
SITE_URL = os.getenv("SITE_URL", "https://us.aivideotrans.site")


async def send_email(to: str, subject: str, html: str) -> bool:
    """发送邮件。返回是否成功。"""
    if not RESEND_API_KEY:
        logger.debug("RESEND_API_KEY 未配置，跳过邮件发送")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html},
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("邮件已发送: to=%s subject=%s", to, subject)
            return True
    except Exception:
        logger.exception("邮件发送失败: to=%s", to)
        return False


async def notify_job_completed(user_email: str, job_title: str, job_id: str) -> bool:
    url = f"{SITE_URL}/workspace/{job_id}"
    return await send_email(
        user_email,
        f"任务完成: {job_title}",
        f"<div style='font-family:sans-serif;max-width:480px;margin:0 auto;padding:20px'>"
        f"<h2 style='color:#8B5CF6'>任务完成</h2>"
        f"<p>你的翻译任务 <b>{job_title}</b> 已完成。</p>"
        f"<p><a href='{url}' style='display:inline-block;padding:10px 20px;background:#8B5CF6;color:white;text-decoration:none;border-radius:8px'>查看结果</a></p>"
        f"<p style='color:#888;font-size:12px'>AIVideoTrans · AI 视频翻译配音工作台</p>"
        f"</div>",
    )


async def notify_job_failed(user_email: str, job_title: str, job_id: str) -> bool:
    url = f"{SITE_URL}/workspace/{job_id}"
    return await send_email(
        user_email,
        f"任务失败: {job_title}",
        f"<div style='font-family:sans-serif;max-width:480px;margin:0 auto;padding:20px'>"
        f"<h2 style='color:#EF4444'>任务处理失败</h2>"
        f"<p>你的翻译任务 <b>{job_title}</b> 处理失败。</p>"
        f"<p><a href='{url}' style='display:inline-block;padding:10px 20px;background:#8B5CF6;color:white;text-decoration:none;border-radius:8px'>查看详情</a></p>"
        f"<p style='color:#888;font-size:12px'>AIVideoTrans · AI 视频翻译配音工作台</p>"
        f"</div>",
    )
