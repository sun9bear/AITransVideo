"""Phase 4.3b-D — manual temporary-voice cleanup CLI (server-side ops script).

复用 4.3b 清理核心 + sweeper（worker fail-fast + audit）做一次性手动清理：
灰度观察（dry-run 预览）/ give-up 行人工兜底 / 紧急回收。

**用法**（在 gateway 工作目录跑，与 gateway 同环境/同 env）：

    python cleanup_temp_voices_cli.py                       # 默认 DRY-RUN（只报告）
    python cleanup_temp_voices_cli.py --execute             # 真删
    python cleanup_temp_voices_cli.py --execute --limit 20
    python cleanup_temp_voices_cli.py --execute --include-give-up
    python cleanup_temp_voices_cli.py --execute --reset-attempts

**边界（Codex 4.3b-D）**：

- **默认 dry-run**：不传 ``--execute`` 只报告，不删、不认领、不改 DB。
- ``--include-give-up`` / ``--reset-attempts`` 仅在 ``--execute`` 下生效（dry-run
  下 reset 被忽略，不 mutate）。两者都是显式 opt-in，自动 sweeper 永不带。
- **server-side script**，直接走 DB + worker 配置（``database`` / ``mainland_voice_worker``）；
  **不持** browser session / internal API key（不是 HTTP / 浏览器客户端）。
- audit 复用 ``emit_voice_cleanup_audit``（与自动 sweeper 同一套字段）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger("cleanup_temp_voices_cli")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cleanup_temp_voices",
        description=(
            "Phase 4.3b manual temporary-voice cleanup. DEFAULT DRY-RUN — pass "
            "--execute to actually delete DashScope voices + soft-delete rows."
        ),
    )
    p.add_argument(
        "--execute", action="store_true",
        help="actually delete (default: dry-run, report only)",
    )
    p.add_argument(
        "--limit", type=int, default=50,
        help="max rows to process this run (default 50)",
    )
    p.add_argument(
        "--include-give-up", action="store_true",
        help="also process rows that hit the give-up attempt cap (manual retry)",
    )
    p.add_argument(
        "--reset-attempts", action="store_true",
        help="reset cleanup_attempts/retry/error on stuck rows first "
        "(requires --execute; ignored in dry-run)",
    )
    return p


async def run_cleanup_cli(args, *, session_factory=None):
    """执行 CLI 清理。返回 ``(report_or_None, reset_count)``。

    ``session_factory`` 仅供测试注入 sqlite；生产用 ``database.async_session``。
    """
    import express_voice_cleanup_service as svc
    import express_voice_cleanup_sweeper as swp

    dry_run = not args.execute
    if session_factory is None:
        from database import async_session
        session_factory = async_session

    reset_count = 0
    if args.reset_attempts:
        if dry_run:
            logger.warning(
                "--reset-attempts ignored in dry-run (no DB mutation); pass --execute to reset"
            )
        else:
            async with session_factory() as db:
                reset_count = await svc.reset_cleanup_state(db, limit=args.limit)
            logger.info("reset cleanup state on %d stuck row(s)", reset_count)

    report = await swp.sweep_once(
        session_factory=session_factory,
        dry_run=dry_run,
        include_give_up=args.include_give_up,
    )
    return report, reset_count


def _print_summary(report, reset_count: int) -> None:
    if report is None:
        print("[cleanup] skipped — mainland worker not configured (execute mode needs it)")
        return
    if report.dry_run:
        print(
            f"[cleanup] DRY-RUN: would delete {len(report.selected)} voice(s): "
            f"{report.selected}"
        )
        return
    print(
        f"[cleanup] EXECUTE: reset={reset_count} deleted={report.deleted} "
        f"failed={report.failed} gave_up={report.gave_up} "
        f"run_id_conflict={report.run_id_conflict}"
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_arg_parser().parse_args(argv)
    # server-side：直接初始化 DB engine（与 gateway 同 env）。不走 HTTP / internal-key。
    from database import init_db

    init_db()
    report, reset_count = asyncio.run(run_cleanup_cli(args))
    _print_summary(report, reset_count)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
