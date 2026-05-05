"""Read-only website traffic analytics for admin ops pages.

The source of truth is Caddy's JSON access log.  This module deliberately keeps
the first version as an on-demand parser with a short in-process cache: no new
database tables, no external analytics vendor, and no user-facing tracking
script in the default path.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from database import get_db
from models import (
    BillingInvoice,
    Job,
    PaymentOrder,
    PhoneVerificationChallenge,
    Session as UserSession,
    Subscription,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/traffic", tags=["admin-traffic"])

_CACHE_TTL_SECONDS = 60
_CACHE: dict[tuple[str, int, int, bool], tuple[float, dict[str, Any]]] = {}
_SECURITY_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_DISCOVERY_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}

_CATEGORY_LABELS = {
    "likely_human_browser": "可能真实用户",
    "search_engine": "搜索引擎",
    "ai_crawler": "AI 爬虫",
    "automation_or_probe": "工具/探针",
    "scanner": "攻击扫描",
    "unknown": "未知",
}

_SEARCH_ENGINE_RE = re.compile(
    r"(googlebot|bingbot|baiduspider|sogou|360spider|yisouspider|"
    r"duckduckbot|yandexbot|applebot|petalbot|slurp)",
    re.I,
)
_AI_CRAWLER_RE = re.compile(
    r"(gptbot|chatgpt-user|oai-searchbot|claudebot|claude-searchbot|"
    r"claude-user|anthropic-ai|perplexitybot|perplexity-user|ccbot|"
    r"amazonbot|bytespider|diffbot|omgili|meta-externalagent|google-extended)",
    re.I,
)
_AUTOMATION_RE = re.compile(
    r"(curl|wget|python-requests|go-http-client|httpx|aiohttp|okhttp|"
    r"java/|node-fetch|axios|postmanruntime|insomnia)",
    re.I,
)
_SCANNER_UA_RE = re.compile(
    r"(zgrab|masscan|nmap|sqlmap|nikto|acunetix|nessus|gobuster|dirbuster|"
    r"wpscan|fuzzer|censysinspect|internetmeasurement|expanse)",
    re.I,
)
_BROWSER_RE = re.compile(r"mozilla/|chrome/|safari/|firefox/|edg/|mobile", re.I)
_SUSPICIOUS_PATH_RE = re.compile(
    r"("
    r"/xmlrpc\.php|/wp-admin|/wp-login|/wp-includes|wlwmanifest\.xml|"
    r"/\.env|/\.git|/phpmyadmin|/pma/|/adminer|"
    r"vendor/phpunit|/actuator|/server-status|/config\.php|"
    r"sftp-config\.json|/owa/|/ecp/|/cgi-bin/"
    r")",
    re.I,
)
_STATIC_EXT_RE = re.compile(
    r"\.(?:js|css|png|jpg|jpeg|gif|svg|ico|webp|avif|woff|woff2|ttf|map|"
    r"txt|xml|json|mp4|webm|m4a|mp3|zip|gz|br)$",
    re.I,
)
_AUTH_PATH_PREFIXES = (
    "/auth/login",
    "/auth/register",
    "/auth/phone/",
    "/api/captcha/",
)
_API_PATH_PREFIXES = (
    "/api/",
    "/auth/",
    "/job-api/",
    "/gateway/",
)
_SEVERITY_RANK = {
    "ok": 0,
    "notice": 1,
    "warning": 2,
    "critical": 3,
}
_PUBLIC_MARKETING_PATHS = (
    "/",
    "/pricing",
    "/trial",
    "/contact",
    "/terms",
    "/privacy",
    "/refund",
)
_BLOCKED_CRAWLER_PREFIXES = (
    "/api",
    "/job-api",
    "/gateway",
    "/admin",
    "/workspace",
    "/projects",
    "/settings",
    "/tasks",
    "/notifications",
    "/usage",
    "/voices",
)
_CRAWLER_FAMILIES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("googlebot", "Googlebot", "search", re.compile(r"googlebot", re.I)),
    ("bingbot", "Bingbot", "search", re.compile(r"bingbot", re.I)),
    ("baiduspider", "Baiduspider", "search", re.compile(r"baiduspider", re.I)),
    ("sogou", "Sogou Spider", "search", re.compile(r"sogou", re.I)),
    ("360spider", "360 Spider", "search", re.compile(r"360spider", re.I)),
    ("yisouspider", "Yisou Spider", "search", re.compile(r"yisouspider", re.I)),
    ("duckduckbot", "DuckDuckBot", "search", re.compile(r"duckduckbot", re.I)),
    ("applebot", "Applebot", "search", re.compile(r"applebot", re.I)),
    ("petalbot", "PetalBot", "search", re.compile(r"petalbot", re.I)),
    ("oai-searchbot", "OAI-SearchBot", "ai", re.compile(r"oai-searchbot", re.I)),
    ("chatgpt-user", "ChatGPT-User", "ai", re.compile(r"chatgpt-user", re.I)),
    ("gptbot", "GPTBot", "ai", re.compile(r"gptbot", re.I)),
    ("claudebot", "ClaudeBot", "ai", re.compile(r"claudebot", re.I)),
    ("claude-searchbot", "Claude-SearchBot", "ai", re.compile(r"claude-searchbot", re.I)),
    ("perplexitybot", "PerplexityBot", "ai", re.compile(r"perplexitybot", re.I)),
    ("ccbot", "CCBot", "ai", re.compile(r"ccbot", re.I)),
    ("bytespider", "Bytespider", "ai", re.compile(r"bytespider", re.I)),
)


def _is_admin(user: User | None) -> bool:
    return bool(user and getattr(user, "role", None) == "admin")


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _runtime_logs_dir() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR")
        or getattr(settings, "runtime_logs_dir", "")
        or "/opt/aivideotrans/data/runtime_logs"
    )


def _iter_log_files(log_dir: Path) -> list[Path]:
    files: list[Path] = []
    if not log_dir.exists() or not log_dir.is_dir():
        return files
    for path in log_dir.iterdir():
        name = path.name
        if path.is_file() and name.startswith("public-entry.access") and (
            name.endswith(".log") or name.endswith(".log.gz") or ".log." in name
        ):
            files.append(path)
    return sorted(files, key=lambda item: item.stat().st_mtime)


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _first_header(headers: dict[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() != wanted:
            continue
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value or "")
    return ""


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


def _path_from_uri(uri: str) -> str:
    try:
        return urlsplit(uri or "/").path or "/"
    except ValueError:
        return uri or "/"


def _mask_ip(ip: str) -> str:
    if not ip:
        return ""
    if ":" in ip:
        parts = ip.split(":")
        visible = ":".join(parts[:3]).strip(":")
        return f"{visible}::/64" if visible else "::/64"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["xxx"])
    return "masked"


def _classify_request(user_agent: str, path: str) -> str:
    ua = user_agent or ""
    if _SUSPICIOUS_PATH_RE.search(path) or _SCANNER_UA_RE.search(ua):
        return "scanner"
    if _AI_CRAWLER_RE.search(ua):
        return "ai_crawler"
    if _SEARCH_ENGINE_RE.search(ua):
        return "search_engine"
    if _AUTOMATION_RE.search(ua):
        return "automation_or_probe"
    if _BROWSER_RE.search(ua):
        return "likely_human_browser"
    return "unknown"


def _is_page_view(method: str, path: str, status: int) -> bool:
    if method.upper() != "GET":
        return False
    if status >= 500:
        return False
    if path.startswith(("/api/", "/job-api/", "/gateway/", "/_next/", "/materials-api/")):
        return False
    if path in {"/favicon.ico", "/robots.txt", "/sitemap.xml", "/health"}:
        return False
    return not _STATIC_EXT_RE.search(path)


def _counter_rows(counter: Counter[str], total: int, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": _CATEGORY_LABELS.get(key, key),
            "count": count,
            "share": round(count / total, 4) if total else 0,
        }
        for key, count in counter.most_common(limit)
    ]


def _ranked_rows(rows: list[tuple[Any, int]], total: int, limit: int) -> list[dict[str, Any]]:
    counter = Counter({str(key or "unknown"): int(count or 0) for key, count in rows})
    return _counter_rows(counter, total, limit)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _conversion_row(key: str, label: str, numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "from_count": denominator,
        "to_count": numerator,
        "rate": _rate(numerator, denominator),
    }


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D+", "", phone)
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "****"


def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = f"{local[:2]}***"
    return f"{masked}@{domain}"


def _mask_identity(email: str | None, phone: str | None) -> str:
    return _mask_phone(phone) or _mask_email(email) or "unknown"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def _scalar_int(db: AsyncSession, stmt) -> int:
    value = (await db.execute(stmt)).scalar()
    return int(value or 0)


async def _scalar_float(db: AsyncSession, stmt) -> float:
    value = (await db.execute(stmt)).scalar()
    return float(value or 0)


async def _group_counts(db: AsyncSession, stmt) -> list[tuple[Any, int]]:
    result = await db.execute(stmt)
    return [(row[0], int(row[1] or 0)) for row in result.all()]


async def _daily_counts(
    db: AsyncSession,
    column,
    since: datetime,
    extra_where: list[Any] | None = None,
) -> dict[str, int]:
    day_expr = func.date(column)
    where = [column >= since]
    if extra_where:
        where.extend(extra_where)
    result = await db.execute(
        select(day_expr.label("day"), func.count().label("count"))
        .where(*where)
        .group_by(day_expr)
        .order_by(day_expr)
    )
    return {str(row.day): int(row.count or 0) for row in result.all()}


def _merge_behavior_daily(
    *,
    since: datetime,
    now: datetime,
    traffic_daily: list[dict[str, Any]],
    registrations: dict[str, int],
    logins: dict[str, int],
    sms_sent: dict[str, int],
    jobs: dict[str, int],
    paid_orders: dict[str, int],
) -> list[dict[str, Any]]:
    traffic_by_day = {row["date"]: row for row in traffic_daily}
    rows: list[dict[str, Any]] = []
    cursor = since.date()
    end = now.date()
    while cursor <= end:
        key = cursor.isoformat()
        traffic = traffic_by_day.get(key, {})
        rows.append(
            {
                "date": key,
                "human_page_visitors": int(traffic.get("human_page_visitors") or 0),
                "page_views": int(traffic.get("page_views") or 0),
                "registrations": registrations.get(key, 0),
                "login_users": logins.get(key, 0),
                "sms_sent": sms_sent.get(key, 0),
                "jobs_created": jobs.get(key, 0),
                "paid_orders": paid_orders.get(key, 0),
            }
        )
        cursor += timedelta(days=1)
    return rows


def _empty_behavior(reason: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "error": reason or "行为数据暂不可用",
        "totals": {
            "registrations": 0,
            "phone_verified_users": 0,
            "login_sessions": 0,
            "logged_in_users": 0,
            "active_session_users": 0,
            "sms_sent": 0,
            "sms_consumed": 0,
            "sms_expired_unused": 0,
            "jobs_created": 0,
            "job_users": 0,
            "new_user_job_users": 0,
            "jobs_succeeded": 0,
            "jobs_failed": 0,
            "payment_orders_created": 0,
            "payment_orders_paid": 0,
            "paid_order_users": 0,
            "paid_amount_cny": 0,
            "active_subscriptions": 0,
            "new_subscriptions": 0,
        },
        "conversion": [],
        "daily": [],
        "plans": [],
        "job_statuses": [],
        "job_modes": [],
        "job_sources": [],
        "payment_providers": [],
        "payment_plans": [],
        "recent_users": [],
        "recent_jobs": [],
        "recent_paid_orders": [],
        "methodology": _behavior_methodology_notes(),
    }


def _empty_response(log_dir: Path, window_days: int, limit: int, *, reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    return {
        "available": False,
        "error": reason,
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(log_dir),
            "files": [],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": 0,
            "page_views": 0,
            "estimated_unique_visitors_ip_ua": 0,
            "estimated_human_visitors_ip_ua": 0,
            "estimated_human_page_visitors_ip_ua": 0,
            "malformed_rows": 0,
        },
        "categories": [],
        "countries": [],
        "human_countries": [],
        "statuses": [],
        "methods": [],
        "top_paths": [],
        "top_page_views": [],
        "top_crawler_user_agents": [],
        "top_scanner_paths": [],
        "daily": [],
        "examples": {},
        "behavior": _empty_behavior("访问日志不可用时不计算转化漏斗"),
        "recommendations": ["请确认 gateway 容器已只读挂载 Caddy public-entry.access 日志目录。"],
        "methodology": _methodology_notes(),
    }


def _methodology_notes() -> list[str]:
    return [
        "真实用户为启发式估算：按 Cloudflare IP + User-Agent 去重，并过滤常见爬虫、工具和扫描路径。",
        "地区来自 Cloudflare Cf-Ipcountry 请求头；未启用更细的省市级定位。",
        "搜索引擎/AI 爬虫按 User-Agent 识别，第一版未做反向 DNS 验证。",
        "API 返回的样本 IP 默认脱敏，避免把访问监控变成用户隐私明细表。",
    ]


def _behavior_methodology_notes() -> list[str]:
    return [
        "注册、短信、登录会话、任务和支付数据来自 gateway PostgreSQL 业务表。",
        "登录量按 session 创建记录估算；没有新增前端埋点，所以不追踪未登录用户的逐页行为。",
        "转化率把 access log 的估算真实页面访客作为入口，后续步骤使用业务表中的真实用户事件。",
        "手机号和邮箱在监控样本中默认脱敏；需要排查单个用户时仍应去用户管理页查看。",
    ]


def _make_recommendations(
    categories: Counter[str],
    scanner_paths: Counter[str],
    crawler_uas: Counter[str],
) -> list[str]:
    recommendations: list[str] = []
    if categories.get("scanner", 0):
        recommendations.append(
            "已发现 WordPress、.env、.git 等通用扫描路径；建议在 Cloudflare WAF 或 Caddy 层直接拦截这些路径。"
        )
    if categories.get("automation_or_probe", 0):
        recommendations.append(
            "存在 curl/httpx/requests 等工具流量；上线监控告警时可对非静态路径增加速率限制。"
        )
    if categories.get("search_engine", 0) or categories.get("ai_crawler", 0):
        recommendations.append(
            "搜索引擎和 AI 爬虫已有访问；建议继续维护 robots.txt、sitemap 和页面 canonical，后续再做反向 DNS 校验。"
        )
    if scanner_paths.get("/xmlrpc.php", 0):
        recommendations.append("`/xmlrpc.php` 扫描量较高，这不是本项目路径，可以作为第一批 WAF 精准拦截规则。")
    if crawler_uas:
        recommendations.append(
            "下一阶段可将 verified bot、登录用户会话和任务创建事件合并，区分 SEO 爬取、真实试用和异常自动化。"
        )
    if not recommendations:
        recommendations.append("当前窗口未看到明显扫描流量；建议保留 7/30 天趋势，便于之后做阈值告警。")
    return recommendations


def _crawler_family(user_agent: str) -> dict[str, str] | None:
    ua = user_agent or ""
    for key, label, kind, pattern in _CRAWLER_FAMILIES:
        if pattern.search(ua):
            return {"key": key, "label": label, "kind": kind}
    return None


def _domain_from_referer(referer: str) -> str:
    if not referer:
        return ""
    try:
        domain = urlsplit(referer).netloc.lower()
    except ValueError:
        return ""
    if domain.startswith("www."):
        return domain[4:]
    return domain


def _referrer_kind(domain: str) -> str | None:
    if not domain:
        return None
    if any(
        token in domain
        for token in (
            "google.",
            "bing.com",
            "baidu.com",
            "sogou.com",
            "so.com",
            "duckduckgo.com",
            "yandex.",
            "search.yahoo.",
        )
    ):
        return "search"
    if any(
        token in domain
        for token in (
            "chatgpt.com",
            "openai.com",
            "perplexity.ai",
            "claude.ai",
            "poe.com",
            "you.com",
        )
    ):
        return "ai"
    return None


def _is_blocked_surface(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _BLOCKED_CRAWLER_PREFIXES)


def _crawler_family_rows(
    counter: Counter[str],
    family_meta: dict[str, dict[str, str]],
    total: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, count in counter.most_common(limit):
        meta = family_meta.get(key, {})
        rows.append(
            {
                "key": key,
                "label": meta.get("label") or key,
                "kind": meta.get("kind") or "unknown",
                "count": count,
                "share": round(count / total, 4) if total else 0,
            }
        )
    return rows


def _discovery_methodology_notes() -> list[str]:
    return [
        "SEO/AI 发现监控来自 Caddy access log：按 User-Agent 识别搜索引擎和 AI crawler，按 Referer 估算搜索/AI 入口。",
        "robots.txt 和 sitemap.xml 状态以访问日志中的实际请求为准；这里不主动访问公网，也不调用搜索引擎后台 API。",
        "Crawler 识别是启发式，第一版未做反向 DNS 或 Cloudflare verified bot 校验；因此适合看趋势和拦截误伤，不作为精确计费口径。",
        "公开页面白名单来自当前营销路由设计：/、/pricing、/trial、/contact、/terms、/privacy、/refund；登录后的 app/admin/API 不应进入 sitemap。",
    ]


def _empty_discovery_response(log_dir: Path, window_days: int, limit: int, *, reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    return {
        "available": False,
        "error": reason,
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(log_dir),
            "files": [],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": 0,
            "crawler_requests": 0,
            "search_engine_requests": 0,
            "ai_crawler_requests": 0,
            "crawler_page_fetches": 0,
            "crawler_successful_page_fetches": 0,
            "blocked_crawler_requests": 0,
            "crawler_error_requests": 0,
            "robots_requests": 0,
            "robots_successes": 0,
            "sitemap_requests": 0,
            "sitemap_successes": 0,
            "search_referrals": 0,
            "ai_referrals": 0,
            "public_paths_seen": 0,
            "blocked_surface_hits": 0,
            "malformed_rows": 0,
        },
        "crawler_families": [],
        "crawler_paths": [],
        "public_paths": [],
        "crawler_statuses": [],
        "crawler_countries": [],
        "search_referrers": [],
        "ai_referrers": [],
        "blocked_paths": [],
        "daily": [],
        "checks": [],
        "recommendations": ["请确认 gateway 容器已只读挂载 Caddy public-entry.access 日志目录。"],
        "allowlist_guidance": _crawler_allowlist_guidance(),
        "methodology": _discovery_methodology_notes(),
    }


def _crawler_allowlist_guidance() -> list[dict[str, str]]:
    return [
        {
            "crawler": "Googlebot / Bingbot / Baiduspider",
            "recommendation": "允许",
            "reason": "基础搜索发现入口，应通过 sitemap 和公开页面正常抓取。",
        },
        {
            "crawler": "OAI-SearchBot / ChatGPT-User",
            "recommendation": "允许",
            "reason": "面向 ChatGPT 搜索和用户触发浏览，适合你的曝光目标。",
        },
        {
            "crawler": "ClaudeBot / PerplexityBot",
            "recommendation": "先允许并观察",
            "reason": "有助于 AI 搜索/问答引用；如流量异常再按 User-Agent 或路径限速。",
        },
        {
            "crawler": "GPTBot / CCBot / Bytespider",
            "recommendation": "按业务取舍",
            "reason": "更偏训练或大规模抓取；当前可先允许，后续按带宽和引用收益调整。",
        },
    ]


def _discovery_checks(
    *,
    robots_successes: int,
    sitemap_successes: int,
    blocked_crawler_requests: int,
    ai_crawler_requests: int,
    public_paths_seen: int,
    blocked_surface_hits: int,
) -> list[dict[str, Any]]:
    return [
        {
            "key": "robots_observed",
            "label": "robots.txt 被成功读取",
            "status": "ok" if robots_successes else "notice",
            "detail": "日志中看到 /robots.txt 返回成功。" if robots_successes else "当前窗口没有看到 crawler 成功读取 /robots.txt。",
        },
        {
            "key": "sitemap_observed",
            "label": "sitemap.xml 被成功读取",
            "status": "ok" if sitemap_successes else "notice",
            "detail": "日志中看到 /sitemap.xml 返回成功。" if sitemap_successes else "当前窗口没有看到 crawler 成功读取 /sitemap.xml，建议主动提交 sitemap。",
        },
        {
            "key": "crawler_not_blocked",
            "label": "Crawler 未被安全规则误伤",
            "status": "ok" if blocked_crawler_requests == 0 else "warning",
            "detail": "未发现 crawler 收到 401/403/429。" if blocked_crawler_requests == 0 else "有 crawler 收到 401/403/429，需要检查 WAF/AI Crawl Control 规则。",
        },
        {
            "key": "ai_crawler_allowed",
            "label": "AI crawler 有访问信号",
            "status": "ok" if ai_crawler_requests else "notice",
            "detail": "日志中已看到 AI crawler 访问。" if ai_crawler_requests else "当前窗口未看到 AI crawler，可确认 Cloudflare AI Crawl Control 未拦截。",
        },
        {
            "key": "public_pages_discovered",
            "label": "公开营销页面被抓取",
            "status": "ok" if public_paths_seen >= 2 else "notice",
            "detail": f"当前窗口 crawler 访问了 {public_paths_seen} 个公开营销路径。",
        },
        {
            "key": "private_surfaces_hidden",
            "label": "受限页面未进入发现面",
            "status": "ok" if blocked_surface_hits == 0 else "notice",
            "detail": "未看到 crawler 抓 app/admin/API 受限面。" if blocked_surface_hits == 0 else "Crawler 访问过 app/admin/API 受限面；robots 已阻止，但可继续观察是否有外部链接泄漏。",
        },
    ]


def _discovery_recommendations(
    *,
    crawler_requests: int,
    ai_crawler_requests: int,
    blocked_crawler_requests: int,
    robots_successes: int,
    sitemap_successes: int,
    public_paths_seen: int,
    search_referrals: int,
    ai_referrals: int,
) -> list[str]:
    recommendations: list[str] = []
    if blocked_crawler_requests:
        recommendations.append(
            "先禁用或调整 `AI Crawl Control - Block AI bots by User Agent` 这类硬拦截规则，避免 OAI-SearchBot、ClaudeBot、PerplexityBot 被误伤。"
        )
    if not sitemap_successes:
        recommendations.append(
            "把 `https://aitrans.video/sitemap.xml` 提交到 Google Search Console 和 Bing Webmaster Tools；百度/搜狗也可手动提交首页和定价页。"
        )
    if not robots_successes:
        recommendations.append("确认 `https://aitrans.video/robots.txt` 能公开访问且不会跳转到登录页。")
    if crawler_requests and public_paths_seen < 2:
        recommendations.append(
            "Crawler 已来过但公开页面覆盖少；建议增加 sitemap 中的指南页、案例页、对比页，让 AI/搜索引擎有更多可引用内容。"
        )
    if ai_crawler_requests == 0:
        recommendations.append(
            "当前窗口未看到 AI crawler；在 Cloudflare AI Crawl Control 中将 OAI-SearchBot、ChatGPT-User、ClaudeBot、PerplexityBot 设为 Allow 后继续观察。"
        )
    if search_referrals == 0 and ai_referrals == 0:
        recommendations.append(
            "尚未看到搜索/AI 推荐带来的真实入口；下一步应补内容型页面，而不是只优化技术配置。"
        )
    if not recommendations:
        recommendations.append("搜索/AI 发现基础信号正常；下一阶段重点是新增高意图内容页并观察入口转化。")
    return recommendations


def _merge_discovery_daily(
    *,
    since: datetime,
    now: datetime,
    daily: dict[str, Counter[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = since.date()
    end = now.date()
    while cursor <= end:
        key = cursor.isoformat()
        counts = daily.get(key, Counter())
        rows.append(
            {
                "date": key,
                "search_engine_requests": int(counts.get("search_engine_requests", 0)),
                "ai_crawler_requests": int(counts.get("ai_crawler_requests", 0)),
                "crawler_page_fetches": int(counts.get("crawler_page_fetches", 0)),
                "crawler_errors": int(counts.get("crawler_errors", 0)),
                "robots_requests": int(counts.get("robots_requests", 0)),
                "sitemap_requests": int(counts.get("sitemap_requests", 0)),
                "search_referrals": int(counts.get("search_referrals", 0)),
                "ai_referrals": int(counts.get("ai_referrals", 0)),
            }
        )
        cursor += timedelta(days=1)
    return rows


def analyze_discovery_access_logs(
    *,
    log_dir: Path | None = None,
    window_days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    """Analyze search-engine and AI discovery signals for phase 4."""

    resolved_log_dir = log_dir or _runtime_logs_dir()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    log_files = _iter_log_files(resolved_log_dir)
    if not log_files:
        return _empty_discovery_response(
            resolved_log_dir,
            window_days,
            limit,
            reason="访问日志目录不存在或没有 public-entry.access 日志",
        )

    crawler_families: Counter[str] = Counter()
    crawler_family_meta: dict[str, dict[str, str]] = {}
    crawler_paths: Counter[str] = Counter()
    public_paths: Counter[str] = Counter()
    crawler_statuses: Counter[str] = Counter()
    crawler_countries: Counter[str] = Counter()
    search_referrers: Counter[str] = Counter()
    ai_referrers: Counter[str] = Counter()
    blocked_paths: Counter[str] = Counter()
    daily: dict[str, Counter[str]] = defaultdict(Counter)

    malformed_rows = 0
    total = 0
    crawler_requests = 0
    search_engine_requests = 0
    ai_crawler_requests = 0
    crawler_page_fetches = 0
    crawler_successful_page_fetches = 0
    blocked_crawler_requests = 0
    crawler_error_requests = 0
    robots_requests = 0
    robots_successes = 0
    sitemap_requests = 0
    sitemap_successes = 0
    search_referrals = 0
    ai_referrals = 0
    blocked_surface_hits = 0

    for log_path in log_files:
        try:
            with _open_text(log_path) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_rows += 1
                        continue

                    ts = _parse_ts(row.get("ts"))
                    if ts is None:
                        malformed_rows += 1
                        continue
                    if ts < since or ts > now + timedelta(minutes=5):
                        continue

                    request = row.get("request") or {}
                    headers = request.get("headers") or {}
                    uri = str(request.get("uri") or "/")
                    request_path = _path_from_uri(uri)
                    method = str(request.get("method") or "")
                    status = int(row.get("status") or 0)
                    user_agent = _first_header(headers, "User-Agent")
                    country = _first_header(headers, "Cf-Ipcountry") or "UNKNOWN"
                    referer = _first_header(headers, "Referer")
                    day_key = ts.date().isoformat()
                    family = _crawler_family(user_agent)
                    category = _classify_request(user_agent, request_path)
                    is_crawler = family is not None or category in {"search_engine", "ai_crawler"}

                    total += 1
                    ref_domain = _domain_from_referer(referer)
                    ref_kind = _referrer_kind(ref_domain)
                    if ref_kind == "search":
                        search_referrals += 1
                        search_referrers[ref_domain] += 1
                        daily[day_key]["search_referrals"] += 1
                    elif ref_kind == "ai":
                        ai_referrals += 1
                        ai_referrers[ref_domain] += 1
                        daily[day_key]["ai_referrals"] += 1

                    if not is_crawler:
                        continue

                    crawler_requests += 1
                    crawler_paths[request_path] += 1
                    crawler_statuses[str(status)] += 1
                    crawler_countries[country] += 1

                    if family is None:
                        family_key = category
                        family = {
                            "key": family_key,
                            "label": _CATEGORY_LABELS.get(category, category),
                            "kind": "search" if category == "search_engine" else "ai",
                        }
                    family_key = family["key"]
                    crawler_families[family_key] += 1
                    crawler_family_meta[family_key] = family

                    if family["kind"] == "search":
                        search_engine_requests += 1
                        daily[day_key]["search_engine_requests"] += 1
                    elif family["kind"] == "ai":
                        ai_crawler_requests += 1
                        daily[day_key]["ai_crawler_requests"] += 1

                    if request_path == "/robots.txt":
                        robots_requests += 1
                        daily[day_key]["robots_requests"] += 1
                        if 200 <= status < 400:
                            robots_successes += 1
                    elif request_path == "/sitemap.xml":
                        sitemap_requests += 1
                        daily[day_key]["sitemap_requests"] += 1
                        if 200 <= status < 400:
                            sitemap_successes += 1

                    if _is_page_view(method, request_path, status):
                        crawler_page_fetches += 1
                        daily[day_key]["crawler_page_fetches"] += 1
                        if 200 <= status < 400:
                            crawler_successful_page_fetches += 1
                        if request_path in _PUBLIC_MARKETING_PATHS:
                            public_paths[request_path] += 1

                    if status in {401, 403, 429}:
                        blocked_crawler_requests += 1
                        blocked_paths[request_path] += 1
                        daily[day_key]["crawler_errors"] += 1
                    elif status >= 400:
                        crawler_error_requests += 1
                        daily[day_key]["crawler_errors"] += 1

                    if _is_blocked_surface(request_path):
                        blocked_surface_hits += 1
        except OSError as exc:
            logger.warning("failed to read discovery log %s: %s", log_path, exc)

    public_paths_seen = len(public_paths)
    checks = _discovery_checks(
        robots_successes=robots_successes,
        sitemap_successes=sitemap_successes,
        blocked_crawler_requests=blocked_crawler_requests,
        ai_crawler_requests=ai_crawler_requests,
        public_paths_seen=public_paths_seen,
        blocked_surface_hits=blocked_surface_hits,
    )

    return {
        "available": True,
        "error": None,
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(resolved_log_dir),
            "files": [file.name for file in log_files],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": total,
            "crawler_requests": crawler_requests,
            "search_engine_requests": search_engine_requests,
            "ai_crawler_requests": ai_crawler_requests,
            "crawler_page_fetches": crawler_page_fetches,
            "crawler_successful_page_fetches": crawler_successful_page_fetches,
            "blocked_crawler_requests": blocked_crawler_requests,
            "crawler_error_requests": crawler_error_requests,
            "robots_requests": robots_requests,
            "robots_successes": robots_successes,
            "sitemap_requests": sitemap_requests,
            "sitemap_successes": sitemap_successes,
            "search_referrals": search_referrals,
            "ai_referrals": ai_referrals,
            "public_paths_seen": public_paths_seen,
            "blocked_surface_hits": blocked_surface_hits,
            "malformed_rows": malformed_rows,
        },
        "crawler_families": _crawler_family_rows(
            crawler_families,
            crawler_family_meta,
            crawler_requests,
            limit,
        ),
        "crawler_paths": _counter_rows_labeled(crawler_paths, crawler_requests, limit),
        "public_paths": _counter_rows_labeled(public_paths, crawler_page_fetches, limit),
        "crawler_statuses": _counter_rows_labeled(crawler_statuses, crawler_requests, limit),
        "crawler_countries": _counter_rows_labeled(crawler_countries, crawler_requests, limit),
        "search_referrers": _counter_rows_labeled(search_referrers, search_referrals, limit),
        "ai_referrers": _counter_rows_labeled(ai_referrers, ai_referrals, limit),
        "blocked_paths": _counter_rows_labeled(blocked_paths, blocked_crawler_requests, limit),
        "daily": _merge_discovery_daily(since=since, now=now, daily=daily),
        "checks": checks,
        "recommendations": _discovery_recommendations(
            crawler_requests=crawler_requests,
            ai_crawler_requests=ai_crawler_requests,
            blocked_crawler_requests=blocked_crawler_requests,
            robots_successes=robots_successes,
            sitemap_successes=sitemap_successes,
            public_paths_seen=public_paths_seen,
            search_referrals=search_referrals,
            ai_referrals=ai_referrals,
        ),
        "allowlist_guidance": _crawler_allowlist_guidance(),
        "methodology": _discovery_methodology_notes(),
    }


def _is_auth_endpoint(path: str) -> bool:
    return path.startswith(_AUTH_PATH_PREFIXES)


def _is_api_endpoint(path: str) -> bool:
    return path.startswith(_API_PATH_PREFIXES)


def _counter_rows_labeled(
    counter: Counter[str],
    total: int,
    limit: int,
    *,
    labeler=None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, count in counter.most_common(limit):
        label = labeler(key) if labeler else key
        rows.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "share": round(count / total, 4) if total else 0,
            }
        )
    return rows


def _severity_level(alerts: list[dict[str, Any]]) -> str:
    level = "ok"
    for alert in alerts:
        severity = str(alert.get("severity") or "ok")
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK[level]:
            level = severity
    return level


def _security_alert(
    *,
    key: str,
    title: str,
    severity: str,
    count: int,
    threshold: int,
    detail: str,
    recommendation: str,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "severity": severity,
        "count": count,
        "threshold": threshold,
        "detail": detail,
        "recommendation": recommendation,
        "evidence": evidence or [],
    }


def _sort_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        alerts,
        key=lambda item: (
            _SEVERITY_RANK.get(str(item.get("severity") or "ok"), 0),
            int(item.get("count") or 0),
        ),
        reverse=True,
    )


def _waf_candidate_for_path(path: str, count: int) -> dict[str, Any] | None:
    lower = path.lower()
    if lower == "/xmlrpc.php" or lower.startswith(("/wp-admin", "/wp-login", "/wp-includes")):
        return {
            "path": path,
            "count": count,
            "action": "block",
            "reason": "WordPress 通用扫描路径，当前项目不使用。",
        }
    if lower.startswith(("/.env", "/.git")):
        return {
            "path": path,
            "count": count,
            "action": "block",
            "reason": "配置或源码探测路径，不应对公网开放。",
        }
    if any(token in lower for token in ("/phpmyadmin", "/pma/", "/adminer", "vendor/phpunit")):
        return {
            "path": path,
            "count": count,
            "action": "block",
            "reason": "常见 PHP 管理工具或 PHPUnit 漏洞扫描，非本项目业务路径。",
        }
    if any(token in lower for token in ("/actuator", "/server-status", "/owa/", "/ecp/", "/cgi-bin/")):
        return {
            "path": path,
            "count": count,
            "action": "block",
            "reason": "通用服务器探测路径，当前站点不需要暴露。",
        }
    if _SUSPICIOUS_PATH_RE.search(path):
        return {
            "path": path,
            "count": count,
            "action": "review",
            "reason": "疑似扫描路径，建议确认是否为业务必需后再拦截。",
        }
    return None


def _security_methodology_notes() -> list[str]:
    return [
        "安全监控为规则型启发式判断：第一版只读 Caddy access log 和 gateway 业务表，不新增埋点或外部依赖。",
        "攻击扫描按通用高危路径和扫描器 User-Agent 识别；适合作为 WAF 候选规则，不等同于已成功入侵。",
        "认证失败按登录、注册、短信、验证码相关路径的 4xx/429 聚合；需要结合短信发送与登录成功率一起看。",
        "短信验证码风险来自 phone_verification_challenges 表，IP 和手机号默认脱敏，只做异常模式统计。",
        "第一版未做 Cloudflare verified bot、反向 DNS、ASN 或省市级地理定位；后续可以接入 Cloudflare Logpush/WAF 事件做更精确归因。",
    ]


def _empty_security_response(log_dir: Path, window_days: int, limit: int, *, reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    return {
        "available": False,
        "error": reason,
        "level": "ok",
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(log_dir),
            "files": [],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": 0,
            "scanner_requests": 0,
            "automation_requests": 0,
            "suspicious_path_requests": 0,
            "unique_scanner_ips": 0,
            "auth_error_requests": 0,
            "api_error_requests": 0,
            "server_error_requests": 0,
            "malformed_rows": 0,
            "sms_sent": 0,
            "sms_consumed": 0,
            "sms_expired_unused": 0,
            "sms_distinct_ips": 0,
            "sms_distinct_phones": 0,
        },
        "alerts": [],
        "waf_candidates": [],
        "scanner_paths": [],
        "scanner_countries": [],
        "scanner_ips": [],
        "automation_user_agents": [],
        "auth_error_paths": [],
        "api_error_paths": [],
        "server_error_paths": [],
        "sms_ips": [],
        "sms_phones": [],
        "daily": [],
        "methodology": _security_methodology_notes(),
    }


def _security_daily_rows(
    *,
    since: datetime,
    now: datetime,
    access_daily: dict[str, Counter[str]],
    sms_daily: Counter[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = since.date()
    end = now.date()
    while cursor <= end:
        key = cursor.isoformat()
        counts = access_daily.get(key, Counter())
        rows.append(
            {
                "date": key,
                "scanner_requests": int(counts.get("scanner_requests", 0)),
                "auth_errors": int(counts.get("auth_errors", 0)),
                "server_errors": int(counts.get("server_errors", 0)),
                "automation_requests": int(counts.get("automation_requests", 0)),
                "sms_sent": int(sms_daily.get(key, 0)),
            }
        )
        cursor += timedelta(days=1)
    return rows


def _security_access_alerts(
    *,
    scanner_requests: int,
    suspicious_path_requests: int,
    automation_requests: int,
    auth_error_requests: int,
    server_error_requests: int,
    scanner_paths: Counter[str],
    auth_error_paths: Counter[str],
    server_error_paths: Counter[str],
    automation_user_agents: Counter[str],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if scanner_requests:
        severity = "critical" if scanner_requests >= 2000 else "warning" if scanner_requests >= 500 else "notice"
        alerts.append(
            _security_alert(
                key="scanner_requests",
                title="发现通用攻击扫描",
                severity=severity,
                count=scanner_requests,
                threshold=1,
                detail="检测到 WordPress、.env、.git、phpMyAdmin 等非业务路径探测。",
                recommendation="把高频扫描路径加入 Cloudflare WAF 或 Caddy 精准拦截规则，先从请求量最高的路径开始。",
                evidence=[row[0] for row in scanner_paths.most_common(5)],
            )
        )
    if scanner_paths.get("/xmlrpc.php", 0) >= 100:
        count = scanner_paths["/xmlrpc.php"]
        alerts.append(
            _security_alert(
                key="xmlrpc_scan",
                title="/xmlrpc.php 扫描偏高",
                severity="critical" if count >= 1000 else "warning",
                count=count,
                threshold=100,
                detail="该路径属于 WordPress 常见攻击入口，当前项目不使用。",
                recommendation="可直接在 WAF 层对 /xmlrpc.php 返回 403 或挑战，不影响业务。",
                evidence=["/xmlrpc.php"],
            )
        )
    elif suspicious_path_requests:
        alerts.append(
            _security_alert(
                key="suspicious_paths",
                title="存在可拦截的可疑路径",
                severity="notice",
                count=suspicious_path_requests,
                threshold=1,
                detail="检测到非业务路径探测，但频率暂未达到高危阈值。",
                recommendation="保留趋势观察；若路径反复出现，可加入 WAF 候选规则。",
                evidence=[row[0] for row in scanner_paths.most_common(5)],
            )
        )
    if auth_error_requests:
        severity = "critical" if auth_error_requests >= 50 else "warning" if auth_error_requests >= 10 else "notice"
        alerts.append(
            _security_alert(
                key="auth_errors",
                title="认证相关失败请求",
                severity=severity,
                count=auth_error_requests,
                threshold=10,
                detail="登录、注册、短信或验证码相关接口出现 4xx/429。",
                recommendation="若同一 IP 或手机号集中出现，需要增加登录/短信速率限制，并检查验证码服务是否稳定。",
                evidence=[row[0] for row in auth_error_paths.most_common(5)],
            )
        )
    if server_error_requests:
        severity = "critical" if server_error_requests >= 10 else "warning"
        alerts.append(
            _security_alert(
                key="server_errors",
                title="服务端 5xx 错误",
                severity=severity,
                count=server_error_requests,
                threshold=1,
                detail="API 或页面请求返回 5xx，可能是后端异常、上游超时或部署问题。",
                recommendation="优先查看 gateway/next 容器日志，确认是否由异常请求触发。",
                evidence=[row[0] for row in server_error_paths.most_common(5)],
            )
        )
    if automation_requests >= 100:
        severity = "warning" if automation_requests >= 1000 else "notice"
        alerts.append(
            _security_alert(
                key="automation_requests",
                title="工具或脚本访问较多",
                severity=severity,
                count=automation_requests,
                threshold=100,
                detail="检测到 curl、httpx、requests、wget 等工具类 User-Agent。",
                recommendation="对非静态业务路径启用速率限制；对健康检查和内部回源路径单独白名单处理。",
                evidence=[row[0] for row in automation_user_agents.most_common(5)],
            )
        )
    return alerts


def analyze_security_access_logs(
    *,
    log_dir: Path | None = None,
    window_days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    """Build phase-3 security signals from access logs only."""

    resolved_log_dir = log_dir or _runtime_logs_dir()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    log_files = _iter_log_files(resolved_log_dir)
    if not log_files:
        return _empty_security_response(
            resolved_log_dir,
            window_days,
            limit,
            reason="访问日志目录不存在或没有 public-entry.access 日志",
        )

    scanner_paths: Counter[str] = Counter()
    scanner_countries: Counter[str] = Counter()
    scanner_ips: Counter[str] = Counter()
    automation_user_agents: Counter[str] = Counter()
    auth_error_paths: Counter[str] = Counter()
    api_error_paths: Counter[str] = Counter()
    server_error_paths: Counter[str] = Counter()
    access_daily: dict[str, Counter[str]] = defaultdict(Counter)

    scanner_ip_keys: set[str] = set()
    malformed_rows = 0
    total = 0
    scanner_requests = 0
    automation_requests = 0
    suspicious_path_requests = 0
    auth_error_requests = 0
    api_error_requests = 0
    server_error_requests = 0

    for log_path in log_files:
        try:
            with _open_text(log_path) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_rows += 1
                        continue

                    ts = _parse_ts(row.get("ts"))
                    if ts is None:
                        malformed_rows += 1
                        continue
                    if ts < since or ts > now + timedelta(minutes=5):
                        continue

                    request = row.get("request") or {}
                    headers = request.get("headers") or {}
                    uri = str(request.get("uri") or "/")
                    request_path = _path_from_uri(uri)
                    status = int(row.get("status") or 0)
                    user_agent = _first_header(headers, "User-Agent")
                    country = _first_header(headers, "Cf-Ipcountry") or "UNKNOWN"
                    ip = (
                        _first_header(headers, "Cf-Connecting-Ip")
                        or str(request.get("client_ip") or request.get("remote_ip") or "")
                    )
                    day_key = ts.date().isoformat()
                    category = _classify_request(user_agent, request_path)
                    suspicious_path = bool(_SUSPICIOUS_PATH_RE.search(request_path))

                    total += 1
                    if category == "scanner":
                        scanner_requests += 1
                        scanner_paths[request_path] += 1
                        scanner_countries[country] += 1
                        masked_ip = _mask_ip(ip)
                        scanner_ips[masked_ip or "unknown"] += 1
                        scanner_ip_keys.add(ip or masked_ip or "unknown")
                        access_daily[day_key]["scanner_requests"] += 1
                    if suspicious_path:
                        suspicious_path_requests += 1
                    if category == "automation_or_probe":
                        automation_requests += 1
                        automation_user_agents[user_agent or "(empty user-agent)"] += 1
                        access_daily[day_key]["automation_requests"] += 1
                    if 400 <= status < 500 and _is_auth_endpoint(request_path):
                        auth_error_requests += 1
                        auth_error_paths[request_path] += 1
                        access_daily[day_key]["auth_errors"] += 1
                    if status >= 400 and _is_api_endpoint(request_path):
                        api_error_requests += 1
                        api_error_paths[request_path] += 1
                    if status >= 500:
                        server_error_requests += 1
                        server_error_paths[request_path] += 1
                        access_daily[day_key]["server_errors"] += 1
        except OSError as exc:
            logger.warning("failed to read security log %s: %s", log_path, exc)

    waf_candidates = [
        candidate
        for path, count in scanner_paths.most_common(limit)
        if (candidate := _waf_candidate_for_path(path, count)) is not None
    ]
    alerts = _security_access_alerts(
        scanner_requests=scanner_requests,
        suspicious_path_requests=suspicious_path_requests,
        automation_requests=automation_requests,
        auth_error_requests=auth_error_requests,
        server_error_requests=server_error_requests,
        scanner_paths=scanner_paths,
        auth_error_paths=auth_error_paths,
        server_error_paths=server_error_paths,
        automation_user_agents=automation_user_agents,
    )

    return {
        "available": True,
        "error": None,
        "level": _severity_level(alerts),
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(resolved_log_dir),
            "files": [file.name for file in log_files],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": total,
            "scanner_requests": scanner_requests,
            "automation_requests": automation_requests,
            "suspicious_path_requests": suspicious_path_requests,
            "unique_scanner_ips": len(scanner_ip_keys),
            "auth_error_requests": auth_error_requests,
            "api_error_requests": api_error_requests,
            "server_error_requests": server_error_requests,
            "malformed_rows": malformed_rows,
            "sms_sent": 0,
            "sms_consumed": 0,
            "sms_expired_unused": 0,
            "sms_distinct_ips": 0,
            "sms_distinct_phones": 0,
        },
        "alerts": _sort_alerts(alerts),
        "waf_candidates": waf_candidates,
        "scanner_paths": _counter_rows_labeled(scanner_paths, scanner_requests, limit),
        "scanner_countries": _counter_rows_labeled(scanner_countries, scanner_requests, limit),
        "scanner_ips": _counter_rows_labeled(scanner_ips, scanner_requests, limit),
        "automation_user_agents": _counter_rows_labeled(
            automation_user_agents,
            automation_requests,
            limit,
        ),
        "auth_error_paths": _counter_rows_labeled(auth_error_paths, auth_error_requests, limit),
        "api_error_paths": _counter_rows_labeled(api_error_paths, api_error_requests, limit),
        "server_error_paths": _counter_rows_labeled(server_error_paths, server_error_requests, limit),
        "sms_ips": [],
        "sms_phones": [],
        "daily": _security_daily_rows(
            since=since,
            now=now,
            access_daily=access_daily,
            sms_daily=Counter(),
        ),
        "methodology": _security_methodology_notes(),
    }


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _analyze_security_sms(
    *,
    db: AsyncSession,
    since: datetime,
    now: datetime,
    limit: int,
) -> dict[str, Any]:
    result = await db.execute(
        select(
            PhoneVerificationChallenge.client_ip,
            PhoneVerificationChallenge.phone_number,
            PhoneVerificationChallenge.consumed_at,
            PhoneVerificationChallenge.expires_at,
            PhoneVerificationChallenge.created_at,
        ).where(PhoneVerificationChallenge.created_at >= since)
    )

    sms_ips: Counter[str] = Counter()
    sms_phones: Counter[str] = Counter()
    sms_daily: Counter[str] = Counter()
    distinct_ips: set[str] = set()
    distinct_phones: set[str] = set()
    sms_sent = 0
    sms_consumed = 0
    sms_expired_unused = 0

    for row in result.all():
        created_at = _utc_datetime(row.created_at)
        consumed_at = _utc_datetime(row.consumed_at)
        expires_at = _utc_datetime(row.expires_at)
        client_ip = row.client_ip or ""
        phone = row.phone_number or ""
        masked_ip = _mask_ip(client_ip) or "unknown"
        masked_phone = _mask_phone(phone) or "unknown"

        sms_sent += 1
        sms_ips[masked_ip] += 1
        sms_phones[masked_phone] += 1
        distinct_ips.add(client_ip or masked_ip)
        distinct_phones.add(phone or masked_phone)
        if created_at:
            sms_daily[created_at.date().isoformat()] += 1
        if consumed_at is not None:
            sms_consumed += 1
        elif expires_at is not None and expires_at < now:
            sms_expired_unused += 1

    alerts: list[dict[str, Any]] = []
    if sms_sent >= 5 and sms_expired_unused > max(3, sms_consumed * 2):
        alerts.append(
            _security_alert(
                key="sms_expired_unused",
                title="短信验证码过期未用偏高",
                severity="warning",
                count=sms_expired_unused,
                threshold=max(3, sms_consumed * 2),
                detail="较多验证码发送后没有完成验证，可能是误操作、网络问题，也可能是短信接口被探测。",
                recommendation="结合同 IP 和同手机号发送次数判断；若集中在少数来源，应加发送频控。",
                evidence=[row[0] for row in sms_ips.most_common(5)],
            )
        )

    if sms_ips:
        top_ip, top_count = sms_ips.most_common(1)[0]
        if top_count >= 10:
            alerts.append(
                _security_alert(
                    key="sms_top_ip",
                    title="单个 IP 短信请求较多",
                    severity="critical" if top_count >= 30 else "warning",
                    count=top_count,
                    threshold=10,
                    detail="同一脱敏 IP 在当前窗口内多次请求验证码。",
                    recommendation="对短信发送接口按 IP、手机号、设备指纹做组合频控，并保留人工白名单。",
                    evidence=[top_ip],
                )
            )
    if sms_phones:
        top_phone, top_count = sms_phones.most_common(1)[0]
        if top_count >= 4:
            alerts.append(
                _security_alert(
                    key="sms_top_phone",
                    title="单个手机号验证码请求较多",
                    severity="warning" if top_count >= 8 else "notice",
                    count=top_count,
                    threshold=4,
                    detail="同一脱敏手机号在当前窗口内多次请求验证码。",
                    recommendation="对同手机号增加冷却时间，避免用户重复点击或自动化请求消耗额度。",
                    evidence=[top_phone],
                )
            )

    return {
        "totals": {
            "sms_sent": sms_sent,
            "sms_consumed": sms_consumed,
            "sms_expired_unused": sms_expired_unused,
            "sms_distinct_ips": len(distinct_ips),
            "sms_distinct_phones": len(distinct_phones),
        },
        "alerts": alerts,
        "sms_ips": _counter_rows_labeled(sms_ips, sms_sent, limit),
        "sms_phones": _counter_rows_labeled(sms_phones, sms_sent, limit),
        "sms_daily": sms_daily,
    }


async def analyze_security_signals(
    *,
    db: AsyncSession,
    log_dir: Path | None = None,
    window_days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    """Build phase-3 security signals from logs plus business tables."""

    payload = analyze_security_access_logs(log_dir=log_dir, window_days=window_days, limit=limit)
    try:
        since = datetime.fromisoformat(payload["window"]["from_utc"])
        now = datetime.fromisoformat(payload["window"]["to_utc"])
        sms = await _analyze_security_sms(db=db, since=since, now=now, limit=limit)
    except Exception as exc:
        logger.warning("failed to analyze sms security signals: %s", exc, exc_info=True)
        payload["error"] = payload["error"] or f"短信安全信号暂不可用：{exc}"
        return payload

    payload["totals"].update(sms["totals"])
    payload["sms_ips"] = sms["sms_ips"]
    payload["sms_phones"] = sms["sms_phones"]
    access_daily: dict[str, Counter[str]] = defaultdict(Counter)
    for row in payload.get("daily", []):
        key = str(row.get("date") or "")
        if not key:
            continue
        access_daily[key]["scanner_requests"] = int(row.get("scanner_requests") or 0)
        access_daily[key]["auth_errors"] = int(row.get("auth_errors") or 0)
        access_daily[key]["server_errors"] = int(row.get("server_errors") or 0)
        access_daily[key]["automation_requests"] = int(row.get("automation_requests") or 0)
    payload["daily"] = _security_daily_rows(
        since=since,
        now=now,
        access_daily=access_daily,
        sms_daily=sms["sms_daily"],
    )
    payload["alerts"] = _sort_alerts(list(payload.get("alerts", [])) + sms["alerts"])
    payload["level"] = _severity_level(payload["alerts"])
    return payload


async def analyze_behavior_funnel(
    *,
    db: AsyncSession,
    since: datetime,
    now: datetime,
    traffic_totals: dict[str, Any],
    traffic_daily: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    """Aggregate business events for the traffic dashboard phase 2."""

    try:
        paid_order_filter = or_(
            PaymentOrder.paid_at >= since,
            and_(
                PaymentOrder.paid_at.is_(None),
                PaymentOrder.status == "paid",
                PaymentOrder.updated_at >= since,
            ),
        )

        registrations = await _scalar_int(
            db, select(func.count()).select_from(User).where(User.created_at >= since)
        )
        phone_verified_users = await _scalar_int(
            db,
            select(func.count()).select_from(User).where(User.phone_verified_at >= since),
        )
        login_sessions = await _scalar_int(
            db,
            select(func.count()).select_from(UserSession).where(UserSession.created_at >= since),
        )
        logged_in_users = await _scalar_int(
            db,
            select(func.count(distinct(UserSession.user_id))).select_from(UserSession).where(
                UserSession.created_at >= since
            ),
        )
        active_session_users = await _scalar_int(
            db,
            select(func.count(distinct(UserSession.user_id))).select_from(UserSession).where(
                UserSession.expires_at > now
            ),
        )

        sms_sent = await _scalar_int(
            db,
            select(func.count()).select_from(PhoneVerificationChallenge).where(
                PhoneVerificationChallenge.created_at >= since
            ),
        )
        sms_consumed = await _scalar_int(
            db,
            select(func.count()).select_from(PhoneVerificationChallenge).where(
                PhoneVerificationChallenge.created_at >= since,
                PhoneVerificationChallenge.consumed_at.is_not(None),
            ),
        )
        sms_expired_unused = await _scalar_int(
            db,
            select(func.count()).select_from(PhoneVerificationChallenge).where(
                PhoneVerificationChallenge.created_at >= since,
                PhoneVerificationChallenge.consumed_at.is_(None),
                PhoneVerificationChallenge.expires_at < now,
            ),
        )

        jobs_created = await _scalar_int(
            db, select(func.count()).select_from(Job).where(Job.created_at >= since)
        )
        job_users = await _scalar_int(
            db,
            select(func.count(distinct(Job.user_id))).select_from(Job).where(Job.created_at >= since),
        )
        new_user_job_users = await _scalar_int(
            db,
            select(func.count(distinct(Job.user_id))).select_from(Job).join(
                User, Job.user_id == User.id
            ).where(
                Job.created_at >= since,
                User.created_at >= since,
            ),
        )
        jobs_succeeded = await _scalar_int(
            db,
            select(func.count()).select_from(Job).where(
                Job.created_at >= since,
                Job.status.in_(["succeeded", "completed"]),
            ),
        )
        jobs_failed = await _scalar_int(
            db,
            select(func.count()).select_from(Job).where(
                Job.created_at >= since,
                Job.status == "failed",
            ),
        )

        payment_orders_created = await _scalar_int(
            db,
            select(func.count()).select_from(PaymentOrder).where(PaymentOrder.created_at >= since),
        )
        payment_orders_paid = await _scalar_int(
            db,
            select(func.count()).select_from(PaymentOrder).where(paid_order_filter),
        )
        paid_order_users = await _scalar_int(
            db,
            select(func.count(distinct(PaymentOrder.user_id))).select_from(PaymentOrder).where(
                paid_order_filter
            ),
        )
        paid_amount_cny = await _scalar_float(
            db,
            select(func.coalesce(func.sum(PaymentOrder.amount_cny), 0)).select_from(PaymentOrder).where(
                paid_order_filter
            ),
        ) / 100
        active_subscriptions = await _scalar_int(
            db,
            select(func.count()).select_from(Subscription).where(Subscription.status == "active"),
        )
        new_subscriptions = await _scalar_int(
            db,
            select(func.count()).select_from(Subscription).where(Subscription.created_at >= since),
        )

        plan_rows = await _group_counts(
            db,
            select(User.plan_code, func.count()).select_from(User).group_by(User.plan_code),
        )
        job_status_rows = await _group_counts(
            db,
            select(Job.status, func.count()).select_from(Job).where(Job.created_at >= since).group_by(Job.status),
        )
        job_mode_rows = await _group_counts(
            db,
            select(Job.service_mode, func.count()).select_from(Job).where(Job.created_at >= since).group_by(Job.service_mode),
        )
        job_source_rows = await _group_counts(
            db,
            select(Job.source_type, func.count()).select_from(Job).where(Job.created_at >= since).group_by(Job.source_type),
        )
        payment_provider_rows = await _group_counts(
            db,
            select(PaymentOrder.provider, func.count()).select_from(PaymentOrder).where(paid_order_filter).group_by(PaymentOrder.provider),
        )
        payment_plan_rows = await _group_counts(
            db,
            select(PaymentOrder.target_plan_code, func.count()).select_from(PaymentOrder).where(paid_order_filter).group_by(PaymentOrder.target_plan_code),
        )

        registrations_daily = await _daily_counts(db, User.created_at, since)
        logins_daily = await _daily_counts(db, UserSession.created_at, since)
        sms_daily = await _daily_counts(db, PhoneVerificationChallenge.created_at, since)
        jobs_daily = await _daily_counts(db, Job.created_at, since)
        paid_daily = await _daily_counts(
            db,
            PaymentOrder.paid_at,
            since,
            [PaymentOrder.status == "paid"],
        )

        recent_user_result = await db.execute(
            select(User.email, User.phone_number, User.plan_code, User.role, User.created_at)
            .where(User.created_at >= since)
            .order_by(User.created_at.desc())
            .limit(10)
        )
        recent_users = [
            {
                "identity": _mask_identity(row.email, row.phone_number),
                "plan_code": row.plan_code or "free",
                "role": row.role or "user",
                "created_at": _iso(row.created_at),
            }
            for row in recent_user_result.all()
        ]

        recent_job_result = await db.execute(
            select(
                Job.job_id,
                Job.title,
                Job.status,
                Job.service_mode,
                Job.source_type,
                Job.created_at,
                User.email,
                User.phone_number,
            )
            .join(User, Job.user_id == User.id)
            .where(Job.created_at >= since)
            .order_by(Job.created_at.desc())
            .limit(10)
        )
        recent_jobs = [
            {
                "job_id": row.job_id,
                "title": (row.title or "")[:80],
                "status": row.status or "unknown",
                "service_mode": row.service_mode or "unknown",
                "source_type": row.source_type or "unknown",
                "owner": _mask_identity(row.email, row.phone_number),
                "created_at": _iso(row.created_at),
            }
            for row in recent_job_result.all()
        ]

        recent_paid_result = await db.execute(
            select(
                PaymentOrder.provider,
                PaymentOrder.target_plan_code,
                PaymentOrder.billing_period,
                PaymentOrder.amount_cny,
                PaymentOrder.paid_at,
                PaymentOrder.updated_at,
                User.email,
                User.phone_number,
            )
            .join(User, PaymentOrder.user_id == User.id)
            .where(paid_order_filter)
            .order_by(PaymentOrder.updated_at.desc())
            .limit(10)
        )
        recent_paid_orders = [
            {
                "provider": row.provider,
                "plan_code": row.target_plan_code,
                "billing_period": row.billing_period,
                "amount_cny": round(float(row.amount_cny or 0) / 100, 2),
                "paid_at": _iso(row.paid_at or row.updated_at),
                "user": _mask_identity(row.email, row.phone_number),
            }
            for row in recent_paid_result.all()
        ]

        paid_invoices = await _scalar_int(
            db,
            select(func.count()).select_from(BillingInvoice).where(
                BillingInvoice.status == "paid",
                BillingInvoice.paid_at >= since,
            ),
        )

        visitors = int(traffic_totals.get("estimated_human_page_visitors_ip_ua") or 0)
        conversion = [
            _conversion_row("visitor_to_register", "访问 -> 注册", registrations, visitors),
            _conversion_row("register_to_phone_verified", "注册 -> 手机验证", phone_verified_users, registrations),
            _conversion_row("register_to_task", "注册 -> 创建任务", new_user_job_users, registrations),
            _conversion_row("login_to_task", "登录用户 -> 创建任务", job_users, logged_in_users),
            _conversion_row("task_user_to_paid", "任务用户 -> 支付", paid_order_users, job_users),
        ]

        totals = {
            "registrations": registrations,
            "phone_verified_users": phone_verified_users,
            "login_sessions": login_sessions,
            "logged_in_users": logged_in_users,
            "active_session_users": active_session_users,
            "sms_sent": sms_sent,
            "sms_consumed": sms_consumed,
            "sms_expired_unused": sms_expired_unused,
            "jobs_created": jobs_created,
            "job_users": job_users,
            "new_user_job_users": new_user_job_users,
            "jobs_succeeded": jobs_succeeded,
            "jobs_failed": jobs_failed,
            "payment_orders_created": payment_orders_created,
            "payment_orders_paid": payment_orders_paid,
            "paid_order_users": paid_order_users,
            "paid_amount_cny": round(paid_amount_cny, 2),
            "paid_invoices": paid_invoices,
            "active_subscriptions": active_subscriptions,
            "new_subscriptions": new_subscriptions,
        }

        return {
            "available": True,
            "error": None,
            "totals": totals,
            "conversion": conversion,
            "daily": _merge_behavior_daily(
                since=since,
                now=now,
                traffic_daily=traffic_daily,
                registrations=registrations_daily,
                logins=logins_daily,
                sms_sent=sms_daily,
                jobs=jobs_daily,
                paid_orders=paid_daily,
            ),
            "plans": _ranked_rows(plan_rows, sum(count for _key, count in plan_rows), limit),
            "job_statuses": _ranked_rows(job_status_rows, jobs_created, limit),
            "job_modes": _ranked_rows(job_mode_rows, jobs_created, limit),
            "job_sources": _ranked_rows(job_source_rows, jobs_created, limit),
            "payment_providers": _ranked_rows(payment_provider_rows, payment_orders_paid, limit),
            "payment_plans": _ranked_rows(payment_plan_rows, payment_orders_paid, limit),
            "recent_users": recent_users,
            "recent_jobs": recent_jobs,
            "recent_paid_orders": recent_paid_orders,
            "methodology": _behavior_methodology_notes(),
        }
    except Exception as exc:
        logger.warning("failed to analyze behavior funnel: %s", exc, exc_info=True)
        return _empty_behavior(str(exc))


def analyze_access_logs(
    *,
    log_dir: Path | None = None,
    window_days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    """Analyze Caddy access logs for the admin traffic dashboard."""

    resolved_log_dir = log_dir or _runtime_logs_dir()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    log_files = _iter_log_files(resolved_log_dir)
    if not log_files:
        return _empty_response(
            resolved_log_dir,
            window_days,
            limit,
            reason="访问日志目录不存在或没有 public-entry.access 日志",
        )

    categories: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    human_countries: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    page_views: Counter[str] = Counter()
    crawler_uas: Counter[str] = Counter()
    scanner_paths: Counter[str] = Counter()
    daily: dict[str, Counter[str]] = defaultdict(Counter)
    daily_page_views: Counter[str] = Counter()
    daily_human_page_visitors: dict[str, set[tuple[str, str]]] = defaultdict(set)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unique_visitors: set[tuple[str, str]] = set()
    human_visitors: set[tuple[str, str]] = set()
    human_page_visitors: set[tuple[str, str]] = set()
    malformed_rows = 0
    total = 0
    page_view_total = 0

    for path in log_files:
        try:
            with _open_text(path) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_rows += 1
                        continue

                    ts = _parse_ts(row.get("ts"))
                    if ts is None:
                        malformed_rows += 1
                        continue
                    if ts < since or ts > now + timedelta(minutes=5):
                        continue

                    request = row.get("request") or {}
                    headers = request.get("headers") or {}
                    uri = str(request.get("uri") or "/")
                    request_path = _path_from_uri(uri)
                    method = str(request.get("method") or "")
                    status = int(row.get("status") or 0)
                    user_agent = _first_header(headers, "User-Agent")
                    country = _first_header(headers, "Cf-Ipcountry") or "UNKNOWN"
                    ip = (
                        _first_header(headers, "Cf-Connecting-Ip")
                        or str(request.get("client_ip") or request.get("remote_ip") or "")
                    )
                    referer = _first_header(headers, "Referer")
                    category = _classify_request(user_agent, request_path)
                    visitor_key = (ip, user_agent)

                    total += 1
                    categories[category] += 1
                    countries[country] += 1
                    statuses[str(status)] += 1
                    methods[method.upper() or "UNKNOWN"] += 1
                    paths[request_path] += 1
                    day_key = ts.date().isoformat()
                    daily[day_key][category] += 1
                    unique_visitors.add(visitor_key)

                    is_human = category == "likely_human_browser"
                    if is_human:
                        human_visitors.add(visitor_key)
                        human_countries[country] += 1
                    if category in {"search_engine", "ai_crawler", "automation_or_probe"}:
                        crawler_uas[user_agent or "(empty user-agent)"] += 1
                    if category == "scanner":
                        scanner_paths[request_path] += 1

                    is_page = category != "scanner" and _is_page_view(method, request_path, status)
                    if is_page:
                        page_view_total += 1
                        page_views[request_path] += 1
                        daily_page_views[day_key] += 1
                        if is_human:
                            human_page_visitors.add(visitor_key)
                            daily_human_page_visitors[day_key].add(visitor_key)

                    if len(examples[category]) < 8:
                        examples[category].append(
                            {
                                "time_utc": ts.isoformat(),
                                "ip": _mask_ip(ip),
                                "country": country,
                                "method": method.upper() or "UNKNOWN",
                                "path": request_path,
                                "status": status,
                                "user_agent": user_agent[:180],
                                "referer": referer[:180],
                            }
                        )
        except OSError as exc:
            logger.warning("failed to read traffic log %s: %s", path, exc)

    daily_rows = []
    for date_key in sorted(daily):
        category_counts = dict(daily[date_key])
        daily_rows.append(
            {
                "date": date_key,
                "total": sum(category_counts.values()),
                "page_views": daily_page_views.get(date_key, 0),
                "human_page_visitors": len(daily_human_page_visitors.get(date_key, set())),
                "categories": category_counts,
            }
        )

    return {
        "available": True,
        "error": None,
        "window": {
            "days": window_days,
            "from_utc": since.isoformat(),
            "to_utc": now.isoformat(),
        },
        "source": {
            "log_dir": str(resolved_log_dir),
            "files": [file.name for file in log_files],
            "generated_at_utc": now.isoformat(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        },
        "totals": {
            "requests": total,
            "page_views": page_view_total,
            "estimated_unique_visitors_ip_ua": len(unique_visitors),
            "estimated_human_visitors_ip_ua": len(human_visitors),
            "estimated_human_page_visitors_ip_ua": len(human_page_visitors),
            "malformed_rows": malformed_rows,
        },
        "categories": _counter_rows(categories, total, limit),
        "countries": _counter_rows(countries, total, limit),
        "human_countries": _counter_rows(human_countries, sum(human_countries.values()), limit),
        "statuses": _counter_rows(statuses, total, limit),
        "methods": _counter_rows(methods, total, limit),
        "top_paths": _counter_rows(paths, total, limit),
        "top_page_views": _counter_rows(page_views, page_view_total, limit),
        "top_crawler_user_agents": _counter_rows(crawler_uas, sum(crawler_uas.values()), limit),
        "top_scanner_paths": _counter_rows(scanner_paths, sum(scanner_paths.values()), limit),
        "daily": daily_rows,
        "examples": dict(examples),
        "recommendations": _make_recommendations(categories, scanner_paths, crawler_uas),
        "methodology": _methodology_notes(),
    }


@router.get("/summary")
async def traffic_summary(
    window: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=5, le=50),
    force: bool = Query(False),
    include_behavior: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    log_dir = _runtime_logs_dir()
    cache_key = (str(log_dir), window, limit, include_behavior)
    now = time.monotonic()
    if not force:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            payload = dict(cached[1])
            payload["cached"] = True
            return payload

    payload = analyze_access_logs(log_dir=log_dir, window_days=window, limit=limit)
    if include_behavior:
        window_from = datetime.fromisoformat(payload["window"]["from_utc"])
        window_to = datetime.fromisoformat(payload["window"]["to_utc"])
        payload["behavior"] = await analyze_behavior_funnel(
            db=db,
            since=window_from,
            now=window_to,
            traffic_totals=payload.get("totals", {}),
            traffic_daily=payload.get("daily", []),
            limit=limit,
        )
    else:
        payload["behavior"] = _empty_behavior("本次请求未包含行为漏斗")
    payload["cached"] = False
    _CACHE[cache_key] = (now, payload)
    return payload


@router.get("/security")
async def traffic_security(
    window: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=5, le=50),
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    log_dir = _runtime_logs_dir()
    cache_key = (str(log_dir), window, limit)
    now = time.monotonic()
    if not force:
        cached = _SECURITY_CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            payload = dict(cached[1])
            payload["cached"] = True
            return payload

    payload = await analyze_security_signals(
        db=db,
        log_dir=log_dir,
        window_days=window,
        limit=limit,
    )
    payload["cached"] = False
    _SECURITY_CACHE[cache_key] = (now, payload)
    return payload


@router.get("/discovery")
async def traffic_discovery(
    window: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=5, le=50),
    force: bool = Query(False),
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    log_dir = _runtime_logs_dir()
    cache_key = (str(log_dir), window, limit)
    now = time.monotonic()
    if not force:
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            payload = dict(cached[1])
            payload["cached"] = True
            return payload

    payload = analyze_discovery_access_logs(
        log_dir=log_dir,
        window_days=window,
        limit=limit,
    )
    payload["cached"] = False
    _DISCOVERY_CACHE[cache_key] = (now, payload)
    return payload
