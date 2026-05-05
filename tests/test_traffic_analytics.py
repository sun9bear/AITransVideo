from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

from traffic_analytics import (
    _conversion_row,
    _mask_identity,
    analyze_access_logs,
    analyze_discovery_access_logs,
    analyze_security_access_logs,
)


def _row(
    *,
    uri: str,
    user_agent: str,
    country: str = "CN",
    ip: str = "203.0.113.88",
    method: str = "GET",
    status: int = 200,
    referer: str | None = None,
) -> dict:
    headers = {
        "User-Agent": [user_agent],
        "Cf-Ipcountry": [country],
        "Cf-Connecting-Ip": [ip],
    }
    if referer:
        headers["Referer"] = [referer]
    return {
        "ts": datetime.now(timezone.utc).timestamp(),
        "request": {
            "method": method,
            "uri": uri,
            "remote_ip": "127.0.0.1",
            "headers": headers,
        },
        "status": status,
    }


def _write_log(log_dir: Path, rows: list[dict]) -> None:
    log_dir.mkdir(exist_ok=True)
    (log_dir / "public-entry.access.log").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )


def test_analyze_access_logs_classifies_humans_bots_and_scanners(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _row(uri="/auth/login", user_agent="Mozilla/5.0 Chrome/124.0", country="CN"),
            _row(uri="/robots.txt", user_agent="Googlebot/2.1", country="US", ip="66.249.66.1"),
            _row(uri="/", user_agent="ClaudeBot/1.0", country="US", ip="198.51.100.2"),
            _row(uri="/xmlrpc.php", user_agent="Mozilla/5.0 Chrome/124.0", country="RU", status=404),
            _row(uri="/api/health", user_agent="curl/8.5.0", country="US", ip="198.51.100.3"),
        ],
    )

    payload = analyze_access_logs(log_dir=tmp_path, window_days=7, limit=10)

    assert payload["available"] is True
    assert payload["totals"]["requests"] == 5
    assert payload["totals"]["page_views"] == 2
    assert payload["totals"]["estimated_human_page_visitors_ip_ua"] == 1
    assert payload["daily"][0]["page_views"] == 2
    assert payload["daily"][0]["human_page_visitors"] == 1

    categories = {row["key"]: row["count"] for row in payload["categories"]}
    assert categories["likely_human_browser"] == 1
    assert categories["search_engine"] == 1
    assert categories["ai_crawler"] == 1
    assert categories["scanner"] == 1
    assert categories["automation_or_probe"] == 1

    scanner_paths = {row["key"]: row["count"] for row in payload["top_scanner_paths"]}
    assert scanner_paths["/xmlrpc.php"] == 1


def test_analyze_access_logs_masks_sample_ips(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _row(uri="/projects", user_agent="Mozilla/5.0 Chrome/124.0", ip="203.0.113.88"),
        ],
    )

    payload = analyze_access_logs(log_dir=tmp_path, window_days=7, limit=10)

    example = payload["examples"]["likely_human_browser"][0]
    assert example["ip"] == "203.0.113.xxx"


def test_analyze_access_logs_returns_empty_when_logs_missing(tmp_path: Path):
    payload = analyze_access_logs(log_dir=tmp_path, window_days=7, limit=10)

    assert payload["available"] is False
    assert payload["totals"]["requests"] == 0
    assert "访问日志" in payload["error"]
    assert payload["behavior"]["available"] is False


def test_conversion_row_handles_zero_denominator():
    row = _conversion_row("register_to_task", "注册 -> 创建任务", 3, 0)

    assert row["rate"] is None
    assert row["to_count"] == 3
    assert row["from_count"] == 0


def test_mask_identity_prefers_phone_and_masks_email():
    assert _mask_identity("someone@example.com", "18672925519") == "186****5519"
    assert _mask_identity("someone@example.com", None) == "so***@example.com"


def test_analyze_security_access_logs_flags_operational_risks(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _row(uri="/xmlrpc.php", user_agent="Mozilla/5.0 Chrome/124.0", country="RU", status=404),
            _row(
                uri="/auth/login",
                user_agent="Mozilla/5.0 Chrome/124.0",
                method="POST",
                status=401,
            ),
            _row(
                uri="/api/admin/traffic/summary",
                user_agent="Mozilla/5.0 Chrome/124.0",
                status=500,
            ),
            _row(uri="/api/health", user_agent="curl/8.5.0", country="US"),
        ],
    )

    payload = analyze_security_access_logs(log_dir=tmp_path, window_days=7, limit=10)

    assert payload["available"] is True
    assert payload["totals"]["requests"] == 4
    assert payload["totals"]["scanner_requests"] == 1
    assert payload["totals"]["suspicious_path_requests"] == 1
    assert payload["totals"]["auth_error_requests"] == 1
    assert payload["totals"]["api_error_requests"] == 2
    assert payload["totals"]["server_error_requests"] == 1
    assert payload["totals"]["automation_requests"] == 1
    assert payload["level"] in {"notice", "warning", "critical"}

    scanner_paths = {row["key"]: row["count"] for row in payload["scanner_paths"]}
    assert scanner_paths["/xmlrpc.php"] == 1
    assert payload["waf_candidates"][0]["path"] == "/xmlrpc.php"
    assert {alert["key"] for alert in payload["alerts"]} >= {
        "scanner_requests",
        "auth_errors",
        "server_errors",
    }


def test_analyze_discovery_access_logs_tracks_search_and_ai_crawlers(tmp_path: Path):
    _write_log(
        tmp_path,
        [
            _row(uri="/robots.txt", user_agent="Googlebot/2.1", country="US", ip="66.249.66.1"),
            _row(uri="/sitemap.xml", user_agent="Bingbot/2.0", country="US", ip="40.77.167.1"),
            _row(uri="/pricing", user_agent="Googlebot/2.1", country="US", ip="66.249.66.1"),
            _row(uri="/", user_agent="OAI-SearchBot/1.0", country="US", ip="198.51.100.10"),
            _row(uri="/pricing", user_agent="ChatGPT-User/1.0", country="US", ip="198.51.100.11", status=403),
            _row(
                uri="/",
                user_agent="Mozilla/5.0 Chrome/124.0",
                referer="https://www.google.com/search?q=AITrans.Video",
            ),
        ],
    )

    payload = analyze_discovery_access_logs(log_dir=tmp_path, window_days=7, limit=10)

    assert payload["available"] is True
    assert payload["totals"]["crawler_requests"] == 5
    assert payload["totals"]["search_engine_requests"] == 3
    assert payload["totals"]["ai_crawler_requests"] == 2
    assert payload["totals"]["robots_successes"] == 1
    assert payload["totals"]["sitemap_successes"] == 1
    assert payload["totals"]["blocked_crawler_requests"] == 1
    assert payload["totals"]["search_referrals"] == 1
    assert payload["totals"]["public_paths_seen"] == 2

    families = {row["key"]: row["count"] for row in payload["crawler_families"]}
    assert families["googlebot"] == 2
    assert families["oai-searchbot"] == 1
    assert families["chatgpt-user"] == 1

    check_statuses = {row["key"]: row["status"] for row in payload["checks"]}
    assert check_statuses["robots_observed"] == "ok"
    assert check_statuses["sitemap_observed"] == "ok"
    assert check_statuses["crawler_not_blocked"] == "warning"
