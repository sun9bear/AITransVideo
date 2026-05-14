#!/usr/bin/env python3
"""R2 灰度观察助手 — 聚合 events.jsonl 里的 download.* / stream.* 事件。

用途
----
Stage A/B/C 灰度期间快速看 R2 命中率 / fallback 次数,避免每次都要登
生产手动 grep .events.jsonl。Phase 2b 触发判据(§11.5)也用这个脚本
出的数据驱动决策。

用法
----
本地(扫一份 jobs/ 目录):

    python scripts/r2_observability.py --jobs-dir D:/path/to/jobs --since 24h

容器内(jobs 目录是 bind-mount,gateway / app 都能访问):

    docker exec aivideotrans-gateway python /opt/aivideotrans/app/scripts/r2_observability.py --since 7d

也可以通过 SSH-US-Via-154.cmd 进生产跑。Output 默认是 human-friendly
text;`--format json` 走机读管道(给 Uptime Kuma / future dashboard)。

设计约束
--------
- **纯 stdlib**。Gateway 容器没装 pydub,所以不能 `from services.jobs.events
  import SUPPORTED_EVENT_TYPES` — 那会触发 services.jobs.__init__ 的传染
  式导入链(见 CLAUDE.md「Phase 2 下载后端」一节)。SUPPORTED_EVENT_TYPES
  的子集在这里**内联复写**,有回归测试守住和源头一致。
- **容错读 JSONL**。匹配 JobStore.load_events 的 fail-open 语义(CodeX P1
  follow-up 2026-05-12):坏 JSON / 未知 event_type 静默跳过,不让一行脏
  数据让整个统计崩。
- **时区健壮**。所有比较都在 UTC,event.created_at 用 ISO-8601 自带 offset
  解析。

退出码
------
- 0  正常输出
- 2  参数 / 路径错(jobs-dir 不存在等)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# 事件分类常量 — 与 src/services/jobs/events.py:SUPPORTED_EVENT_TYPES 严格
# 对齐。任何新增 download.* / stream.* 类型(plan §11 续作可能加 publish.r2.*
# 之类)必须同时改这里;tests/test_r2_observability.py 的契约测试会守。
# ---------------------------------------------------------------------------

DOWNLOAD_EVENT_TYPES = frozenset({
    "download.redirect.r2",
    "download.redirect.r2_registry",
    "download.fallback.local",
    "download.local.direct",
})

STREAM_EVENT_TYPES = frozenset({
    "stream.redirect.r2",
    "stream.redirect.r2_registry",
    "stream.fallback.local",
    "stream.local.direct",
})

# 命中 R2 = 走了 302 重定向(无论 registry 还是 lazy)。
DOWNLOAD_R2_SERVED = frozenset({
    "download.redirect.r2",
    "download.redirect.r2_registry",
})
STREAM_R2_SERVED = frozenset({
    "stream.redirect.r2",
    "stream.redirect.r2_registry",
})

# 走本地字节流 = R2 backend 关 / R2 路径失败回落。
DOWNLOAD_LOCAL_SERVED = frozenset({
    "download.fallback.local",
    "download.local.direct",
})
STREAM_LOCAL_SERVED = frozenset({
    "stream.fallback.local",
    "stream.local.direct",
})

ALL_TRACKED = DOWNLOAD_EVENT_TYPES | STREAM_EVENT_TYPES


# ---------------------------------------------------------------------------
# --since 解析
# ---------------------------------------------------------------------------

SINCE_RE = re.compile(r"^(\d+)([mhdw])$")


def parse_since(arg: str) -> datetime | None:
    """Parse '24h' / '7d' / '30m' / '2w' / 'all' → UTC cutoff datetime.

    Returns ``None`` for ``"all"`` meaning no cutoff applied.
    """
    if arg == "all":
        return None
    m = SINCE_RE.match(arg.lower())
    if not m:
        raise SystemExit(
            f"--since must be 'all' or N{{m|h|d|w}} (got {arg!r}). "
            "Examples: 30m, 24h, 7d, 2w"
        )
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]
    return datetime.now(timezone.utc) - delta


def parse_event_time(raw: str) -> datetime | None:
    """Parse ``event['created_at']`` (ISO-8601 with offset). Returns ``None``
    on failure so the row is filtered out by the cutoff comparison rather
    than crashing the run.
    """
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def iter_events(
    jobs_dir: Path,
    cutoff: datetime | None,
) -> tuple[Counter, set[str], int, int, int]:
    """Walk ``*.events.jsonl`` in ``jobs_dir`` and aggregate.

    Returns a 5-tuple: ``(event_type_counter, job_ids_seen,
    files_scanned, files_failed, rows_skipped)``. ``rows_skipped`` covers
    both unparseable JSON and rows with no usable event_type field.
    Rows with an unrecognized but parseable event_type are NOT counted as
    skipped — they're just outside our tracked vocabulary (e.g. ``log``,
    ``status``).
    """
    counter: Counter = Counter()
    job_ids: set[str] = set()
    files_scanned = 0
    files_failed = 0
    rows_skipped = 0

    for path in jobs_dir.glob("*.events.jsonl"):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            files_failed += 1
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                rows_skipped += 1
                continue
            if not isinstance(event, dict):
                rows_skipped += 1
                continue
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type:
                rows_skipped += 1
                continue
            if event_type not in ALL_TRACKED:
                continue  # log / status / unknown — not our concern
            if cutoff is not None:
                ts = parse_event_time(event.get("created_at", ""))
                if ts is None or ts < cutoff:
                    continue
            counter[event_type] += 1
            job_id = event.get("job_id")
            if isinstance(job_id, str) and job_id:
                job_ids.add(job_id)

    return counter, job_ids, files_scanned, files_failed, rows_skipped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "  —  "
    return f"{100 * n / total:5.1f}%"


def render_text(
    counter: Counter,
    job_ids: set[str],
    since_label: str,
    files_scanned: int,
    files_failed: int,
    rows_skipped: int,
) -> str:
    dl_total = sum(counter[t] for t in DOWNLOAD_EVENT_TYPES)
    st_total = sum(counter[t] for t in STREAM_EVENT_TYPES)
    dl_r2 = sum(counter[t] for t in DOWNLOAD_R2_SERVED)
    dl_local = sum(counter[t] for t in DOWNLOAD_LOCAL_SERVED)
    st_r2 = sum(counter[t] for t in STREAM_R2_SERVED)
    st_local = sum(counter[t] for t in STREAM_LOCAL_SERVED)

    lines = []
    lines.append(f"=== R2 灰度观察 (since={since_label}) ===")
    lines.append(
        f"事件文件: 扫描 {files_scanned} / 失败 {files_failed} / "
        f"坏行 {rows_skipped}  |  涉及任务 {len(job_ids)} 个"
    )
    lines.append("")

    lines.append("--- Download (下载链路) ---")
    lines.append(f"  总数:           {dl_total}")
    lines.append(f"  R2 命中:        {dl_r2:>6}  {_pct(dl_r2, dl_total)}")
    lines.append(f"  本地兜底:       {dl_local:>6}  {_pct(dl_local, dl_total)}")
    for t in sorted(DOWNLOAD_EVENT_TYPES):
        lines.append(
            f"    {t:<35} {counter[t]:>6}  {_pct(counter[t], dl_total)}"
        )
    lines.append("")

    lines.append("--- Stream (在线播放链路) ---")
    lines.append(f"  总数:           {st_total}")
    lines.append(f"  R2 命中:        {st_r2:>6}  {_pct(st_r2, st_total)}")
    lines.append(f"  本地兜底:       {st_local:>6}  {_pct(st_local, st_total)}")
    for t in sorted(STREAM_EVENT_TYPES):
        lines.append(
            f"    {t:<35} {counter[t]:>6}  {_pct(counter[t], st_total)}"
        )
    lines.append("")

    # Highlight fallback events — these are what drive Phase 2b decisions.
    dl_fallback = counter["download.fallback.local"]
    st_fallback = counter["stream.fallback.local"]
    if dl_fallback or st_fallback:
        lines.append("--- 关注 (R2 路径失败后回落 local) ---")
        if dl_fallback:
            lines.append(
                f"  download.fallback.local: {dl_fallback} 次 "
                f"— 检查 R2 HEAD/presign 失败原因 (gateway logs grep WARNING)"
            )
        if st_fallback:
            lines.append(
                f"  stream.fallback.local:   {st_fallback} 次 "
                f"— 检查 registry 缺失 / edit_gen drift / R2 网络抖动"
            )
        # Phase 2b threshold reminder (plan §11.5).
        st_fallback_pct = 100 * st_fallback / st_total if st_total else 0
        if st_fallback_pct >= 5:
            lines.append(
                f"  ⚠️ stream fallback {st_fallback_pct:.1f}% ≥ 5% — "
                f"接近 Phase 2b CF Custom Domain 触发判据"
            )
    else:
        lines.append("--- 无 R2 fallback (路径都按预期工作) ---")

    return "\n".join(lines)


def render_json(
    counter: Counter,
    job_ids: set[str],
    since_label: str,
    files_scanned: int,
    files_failed: int,
    rows_skipped: int,
) -> str:
    dl_total = sum(counter[t] for t in DOWNLOAD_EVENT_TYPES)
    st_total = sum(counter[t] for t in STREAM_EVENT_TYPES)
    payload = {
        "since": since_label,
        "files": {
            "scanned": files_scanned,
            "failed": files_failed,
            "rows_skipped": rows_skipped,
        },
        "jobs_observed": len(job_ids),
        "download": {
            "total": dl_total,
            "r2_served": sum(counter[t] for t in DOWNLOAD_R2_SERVED),
            "local_served": sum(counter[t] for t in DOWNLOAD_LOCAL_SERVED),
            "by_event_type": {
                t: counter[t] for t in sorted(DOWNLOAD_EVENT_TYPES)
            },
        },
        "stream": {
            "total": st_total,
            "r2_served": sum(counter[t] for t in STREAM_R2_SERVED),
            "local_served": sum(counter[t] for t in STREAM_LOCAL_SERVED),
            "by_event_type": {
                t: counter[t] for t in sorted(STREAM_EVENT_TYPES)
            },
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="R2 灰度观察助手 — 聚合 events.jsonl 里的 download.* / stream.* 事件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/r2_observability.py --since 24h\n"
            "  python scripts/r2_observability.py --since 7d --format json\n"
            "  python scripts/r2_observability.py --jobs-dir D:/local/jobs --since 1h\n"
            "  docker exec aivideotrans-gateway python "
            "/opt/aivideotrans/app/scripts/r2_observability.py --since 7d\n"
        ),
    )
    p.add_argument(
        "--jobs-dir",
        default="/opt/aivideotrans/app/jobs",
        type=Path,
        help="event JSONL directory (default: /opt/aivideotrans/app/jobs)",
    )
    p.add_argument(
        "--since",
        default="24h",
        help="time window: 30m / 24h / 7d / 2w / all (default: 24h)",
    )
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    args = p.parse_args(argv)

    if not args.jobs_dir.is_dir():
        print(
            f"ERROR: --jobs-dir not a directory: {args.jobs_dir}",
            file=sys.stderr,
        )
        return 2

    cutoff = parse_since(args.since)
    counter, job_ids, files_scanned, files_failed, rows_skipped = iter_events(
        args.jobs_dir, cutoff,
    )

    if args.format == "text":
        print(render_text(
            counter, job_ids, args.since,
            files_scanned, files_failed, rows_skipped,
        ))
    else:
        print(render_json(
            counter, job_ids, args.since,
            files_scanned, files_failed, rows_skipped,
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
