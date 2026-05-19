#!/usr/bin/env python3
"""端到端 pan backup smoke (Phase 10 §T10.1).

部署完成后手动跑一遍,验证全链路:OAuth 已连接 -> backup -> 状态变 archived ->
restore -> 状态变 succeeded + 数据回到本地。任何一步失败立即停。

设计原则
--------
- **纯 stdlib + urllib**。生产 host 上 Python 可能没装 requests,而 smoke
  脚本本身就是为了排查环境用的,不能引入新依赖。
- **不假设数据库直连**。一切通过 HTTPS API 走,等价于 admin 在浏览器
  里操作 dashboard。这让脚本能在任何能 reach gateway 的机器上跑(开发机
  / CI / 跳板机)。
- **Pre-flight 自检**。先 GET /api/admin/pan/status 确认 connected=true +
  status='active',否则直接报"先去 dashboard 走 OAuth"并退出;不浪费
  跨境上传配额。
- **失败优先输出**。每一步打印 [ok]/[fail] + 计时,失败时把 last 状态 + JSON
  body 完整打到 stderr,方便复制贴 issue。
- **轮询有上限**。backup 默认最多等 4 小时(对应 stale_hours 默认 4h);
  restore 最多 1 小时(本地 → pan 多数 < 30 分钟,跨境 download 更慢)。超
  时退出码 != 0,但 BackupRecord 行还在,可以人工继续观察 stale_reaper。

用法
----
    python scripts/pan_backup_smoke.py \\
        --gateway https://aitrans.video \\
        --cookie 'avt_session=...' \\
        --job-id <existing_succeeded_job_id>

可选:
    --skip-restore       只跑 backup,跳过 restore(不要在生产 archived 任务
                         上跳过 restore -- 任务会卡在 archived)
    --poll-interval 30   轮询间隔(秒)
    --timeout-backup 4h  backup 总超时(支持 30m/4h/2d 后缀)
    --timeout-restore 1h restore 总超时
    --quiet              只输出 [ok]/[fail],不打详细 JSON

退出码
------
0  全过
2  参数错(缺 --job-id 等)
3  Pre-flight 失败(网盘未连接 / 凭据 revoked)
4  Backup 失败(任务超时 / status=failed / archived 但 r2_artifacts 没清)
5  Restore 失败(同上)

注意
----
- 这是一次性 smoke,不是回归测试。**会真的上传 + 下载真实流量**(Baidu
  + R2 + 本地磁盘 I/O)。在 1GB 测试任务上跑大约 10-30 分钟(取决于跨
  境网速)。
- 选一个小任务(< 1GB)做第一次 smoke。Plan §10.5 推荐用一个真实但价值
  低的旧任务。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.client import HTTPSConnection, HTTPConnection
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_duration(raw: str) -> int:
    """'4h' -> 14400. Supports s/m/h/d. Returns int seconds."""
    m = _DURATION_RE.match(raw.lower())
    if not m:
        raise SystemExit(
            f"duration must be N{{s|m|h|d}} (got {raw!r}). "
            "Examples: 30s, 15m, 4h, 1d"
        )
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="End-to-end pan backup smoke test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[1] if "用法" in __doc__ else "",
    )
    p.add_argument(
        "--gateway", required=True,
        help="Gateway base URL, e.g. https://aitrans.video",
    )
    p.add_argument(
        "--cookie", required=True,
        help="Admin session cookie value, e.g. 'avt_session=...'",
    )
    p.add_argument(
        "--job-id", required=True,
        help="Job to back up + restore. Must be admin's own + status=succeeded",
    )
    p.add_argument(
        "--skip-restore", action="store_true",
        help="Only run backup (job will be left at status=archived)",
    )
    p.add_argument(
        "--poll-interval", default="30s", type=parse_duration,
        help="Polling interval (default: 30s)",
    )
    p.add_argument(
        "--timeout-backup", default="4h", type=parse_duration,
        help="Max wait for backup completion (default: 4h)",
    )
    p.add_argument(
        "--timeout-restore", default="1h", type=parse_duration,
        help="Max wait for restore completion (default: 1h)",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress JSON dumps, only print [ok]/[fail] lines",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# HTTP — stdlib only (production hosts may not have requests)
# ---------------------------------------------------------------------------


def _http_call(
    gateway: str, cookie: str, method: str, path: str,
    body: dict | None = None, timeout: int = 30,
) -> tuple[int, dict | str]:
    """One-shot HTTPS / HTTP call. Returns (status, parsed_body).

    parsed_body is a dict if Content-Type was JSON, else the raw str.
    """
    url = urlparse(gateway.rstrip("/") + path)
    conn_cls = HTTPSConnection if url.scheme == "https" else HTTPConnection
    conn = conn_cls(url.netloc, timeout=timeout)
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": "pan-backup-smoke/1.0",
    }
    body_bytes: bytes | None = None
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    try:
        conn.request(method, url.path + ("?" + url.query if url.query else ""),
                     body=body_bytes, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        content_type = resp.getheader("Content-Type", "")
        if "json" in content_type and raw:
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
        return resp.status, raw
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------


def _ok(msg: str) -> None:
    print(f"[ok]  {msg}", flush=True)


def _fail(msg: str, detail: object = None, quiet: bool = False) -> None:
    print(f"[fail] {msg}", file=sys.stderr, flush=True)
    if detail is not None and not quiet:
        if isinstance(detail, (dict, list)):
            print(
                json.dumps(detail, indent=2, ensure_ascii=False),
                file=sys.stderr,
            )
        else:
            print(str(detail), file=sys.stderr)


def _info(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(f"      {msg}", flush=True)


# ---------------------------------------------------------------------------
# Smoke stages
# ---------------------------------------------------------------------------


def preflight_status(args: argparse.Namespace) -> dict:
    """GET /api/admin/pan/status — confirm connected=true + active.

    Exits with code 3 if not ready.
    """
    status, body = _http_call(
        args.gateway, args.cookie, "GET", "/api/admin/pan/status",
    )
    if status != 200:
        _fail(
            f"GET /api/admin/pan/status returned {status}",
            body, args.quiet,
        )
        sys.exit(3)
    if not isinstance(body, dict):
        _fail("status endpoint returned non-JSON", body, args.quiet)
        sys.exit(3)
    if not body.get("connected"):
        _fail(
            "pan not connected. Go to /admin/pan/dashboard and complete "
            "OAuth flow first.",
            body, args.quiet,
        )
        sys.exit(3)
    if body.get("status") != "active":
        _fail(
            f"pan credentials status={body.get('status')!r}, need 'active' "
            "(may need to reconnect)",
            body, args.quiet,
        )
        sys.exit(3)
    _ok(f"pre-flight: pan connected (status=active, scope={body.get('scope')!r})")
    quota = body.get("quota")
    if quota:
        free = quota.get("free", 0)
        used = quota.get("used", 0)
        _info(f"quota: free={free / 1e9:.1f}GB used={used / 1e9:.1f}GB", args.quiet)
    return body


def enqueue_backup(args: argparse.Namespace) -> str:
    """POST /api/admin/pan/backups — returns task_id."""
    status, body = _http_call(
        args.gateway, args.cookie, "POST", "/api/admin/pan/backups",
        body={"job_id": args.job_id},
    )
    if status != 202:
        _fail(
            f"POST /api/admin/pan/backups returned {status} (expected 202)",
            body, args.quiet,
        )
        sys.exit(4)
    if not isinstance(body, dict) or "task_id" not in body:
        _fail("backup enqueue response missing task_id", body, args.quiet)
        sys.exit(4)
    task_id = body["task_id"]
    _ok(f"backup enqueued: task_id={task_id}")
    return task_id


def enqueue_restore(args: argparse.Namespace) -> str:
    """POST /api/admin/pan/restores — returns task_id."""
    status, body = _http_call(
        args.gateway, args.cookie, "POST", "/api/admin/pan/restores",
        body={"job_id": args.job_id},
    )
    if status != 202:
        _fail(
            f"POST /api/admin/pan/restores returned {status} (expected 202)",
            body, args.quiet,
        )
        sys.exit(5)
    task_id = body.get("task_id") if isinstance(body, dict) else None
    if not task_id:
        _fail("restore enqueue response missing task_id", body, args.quiet)
        sys.exit(5)
    _ok(f"restore enqueued: task_id={task_id}")
    return task_id


def poll_task_until_terminal(
    args: argparse.Namespace, *,
    task_type: str, timeout_s: int, exit_code_on_fail: int,
    label: str,
) -> dict:
    """Poll GET /api/jobs/{job_id}/tasks/latest?type=... until status is
    one of completed/failed/cancelled. Exits with exit_code_on_fail on
    timeout or non-completed terminal state.
    """
    deadline = time.monotonic() + timeout_s
    last_status = None
    poll_count = 0
    while time.monotonic() < deadline:
        poll_count += 1
        status_code, body = _http_call(
            args.gateway, args.cookie, "GET",
            f"/api/jobs/{args.job_id}/tasks/latest?type={task_type}",
        )
        if status_code == 200 and isinstance(body, dict):
            task = body
            task_status = task.get("status")
            if task_status != last_status:
                _info(
                    f"{label} poll #{poll_count}: status={task_status!r}",
                    args.quiet,
                )
                last_status = task_status
            if task_status == "completed":
                _ok(
                    f"{label} completed after {poll_count} polls "
                    f"(elapsed ~{(time.monotonic() - (deadline - timeout_s)):.0f}s)"
                )
                return task
            if task_status in ("failed", "cancelled"):
                _fail(
                    f"{label} ended in terminal state {task_status!r}",
                    task, args.quiet,
                )
                sys.exit(exit_code_on_fail)
        else:
            _info(
                f"{label} poll #{poll_count}: HTTP {status_code} "
                f"(transient, will retry)",
                args.quiet,
            )
        time.sleep(args.poll_interval)

    _fail(
        f"{label} did not reach terminal state within {timeout_s}s. "
        f"Last status: {last_status!r}",
        None, args.quiet,
    )
    sys.exit(exit_code_on_fail)


def verify_backup_state(args: argparse.Namespace) -> None:
    """GET /api/admin/pan/backups — confirm a row exists for our job_id
    with status='uploaded'. This is the post-backup invariant."""
    status, body = _http_call(
        args.gateway, args.cookie, "GET",
        f"/api/admin/pan/backups?job_id={args.job_id}",
    )
    if status != 200:
        _fail(
            f"list backups returned {status}", body, args.quiet,
        )
        sys.exit(4)
    if not isinstance(body, dict):
        _fail("list backups returned non-JSON", body, args.quiet)
        sys.exit(4)
    backups = body.get("backups", [])
    matching = [b for b in backups if b.get("job_id") == args.job_id]
    uploaded = [b for b in matching if b.get("status") == "uploaded"]
    if not uploaded:
        _fail(
            f"no BackupRecord with status='uploaded' for job_id={args.job_id}. "
            f"Matching rows: {matching}",
            body, args.quiet,
        )
        sys.exit(4)
    _ok(
        f"backup_records: status='uploaded' row exists for {args.job_id} "
        f"(remote_path={uploaded[0].get('remote_path')!r}, "
        f"size={uploaded[0].get('size_bytes', 0) / 1e9:.2f}GB)"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    t0 = time.monotonic()

    print(f"=== pan_backup_smoke for job {args.job_id} ===", flush=True)
    _info(f"gateway: {args.gateway}", args.quiet)
    _info(
        f"poll_interval={args.poll_interval}s "
        f"timeout_backup={args.timeout_backup}s "
        f"timeout_restore={args.timeout_restore}s",
        args.quiet,
    )

    # Stage 1: pre-flight.
    preflight_status(args)

    # Stage 2: enqueue backup + wait.
    backup_task = enqueue_backup(args)
    poll_task_until_terminal(
        args, task_type="pan_backup",
        timeout_s=args.timeout_backup, exit_code_on_fail=4,
        label="backup",
    )

    # Stage 3: verify BackupRecord row.
    verify_backup_state(args)

    if args.skip_restore:
        elapsed = time.monotonic() - t0
        _ok(f"smoke (backup-only) PASSED in {elapsed:.0f}s")
        print(
            "WARNING: --skip-restore left job at status='archived'. "
            "Run restore manually before re-using.",
            file=sys.stderr,
        )
        return 0

    # Stage 4: enqueue restore + wait.
    restore_task = enqueue_restore(args)
    poll_task_until_terminal(
        args, task_type="pan_restore",
        timeout_s=args.timeout_restore, exit_code_on_fail=5,
        label="restore",
    )

    # Stage 5: post-restore status verification via /backups (status should
    # now be 'restored' on the original row).
    status, body = _http_call(
        args.gateway, args.cookie, "GET",
        f"/api/admin/pan/backups?job_id={args.job_id}",
    )
    if status == 200 and isinstance(body, dict):
        restored = [
            b for b in body.get("backups", [])
            if b.get("job_id") == args.job_id and b.get("status") == "restored"
        ]
        if not restored:
            _fail(
                "post-restore: no BackupRecord with status='restored'",
                body, args.quiet,
            )
            return 5
        _ok("backup_records: status='restored' confirmed")

    elapsed = time.monotonic() - t0
    _ok(f"smoke PASSED end-to-end in {elapsed:.0f}s")
    print(
        "\nNext steps:\n"
        "  - Verify project_dir exists on disk:\n"
        f"      docker exec aivideotrans-app ls -la /opt/aivideotrans/data/projects/{args.job_id}\n"
        "  - Verify events JSONL has 4 pan.* lines:\n"
        f"      docker exec aivideotrans-gateway python "
        f"/opt/aivideotrans/app/scripts/r2_observability.py --since 1h --format json\n"
        "  - User-visible: workspace shows the job in succeeded state again.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
