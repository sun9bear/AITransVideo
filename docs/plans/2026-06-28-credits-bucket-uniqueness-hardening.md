# Credits bucket uniqueness 加固（跟进单元）— 2026-06-28

> 从 **PR #77 拆出**。#77 已落「**防御性代码修复**」（`84217e0d` ensure_* 容忍多行 → 消除 `MultipleResultsFound`，**零回归、无新约束**）。本单元做「防未来 dup」的 alembic 044 unique index，但 **CodeX 二轮复核指出两个必须先解决的 P2**，否则索引会引入并发回归。**不得跳过 P2 直接上索引。**
> 关联：PR #77、[[project_usd_recurring_subscription_plan]] 无关（这是 credits 根治线）。

## 背景

- 现状：`ensure_subscription_bucket_from_v2` 等 ensure_* 存在性检查曾用 `scalar_one_or_none()`，遇多个合法 per-order bucket 抛 `MultipleResultsFound`（非致命 shadow，只刷日志）。#77 已改成 `.order_by(created_at.desc()).limit(1).first()` 容忍多行 → 消除异常。
- 本单元目标：加 alembic 044 三个 partial unique index（per-order `(user_id, related_order_id)` / free·trial `(user_id, bucket_type)` / no-order backfill `(user_id, related_subscription_id)`）从 DB 层杜绝并发写 dup。
- 生产 2026-06-27 巡检：**0 幻影**（044 当前可直接建，无需先去重）。

## 必修 1（P2#2，并发回归，blocker）— 并发安全插入

**问题**：`gateway/credits_service.py` 的 `shadow_grant`（~701-735）是 `try: db.add+flush ... except Exception: log; return None`，**无 savepoint**。加 unique index 后，两个并发 ensure 同时观察到无 bucket → 都 `shadow_grant` → 一个 `flush()` 抛 `IntegrityError` → except 兜住但 **AsyncSession 进入 aborted**，同 session 后续 `ensure_credit_buckets_for_user` → `reserve_credits_or_raise`（job / voice-clone 创建路径）的下一条 DB 语句变成 500 式失败。**这是 044 索引引入的用户可见并发回归。**

**修法**（二选一，推荐 A）：
- **A. savepoint + re-read**：插入包 `async with db.begin_nested():`（savepoint），`IntegrityError` 只回滚 savepoint（outer session 不脏）→ 然后**按对应 unique key re-SELECT 已存在 bucket 并返回**（让出给并发赢家）。
- **B. ON CONFLICT**：PG `INSERT ... ON CONFLICT DO NOTHING RETURNING` + 若无返回则 re-SELECT 赢家。
- 覆盖三场景：free/trial（key=`user_id,bucket_type`）、subscription per-order（`user_id,related_order_id`）、no-order backfill（`user_id,related_subscription_id`）。shadow_grant 是通用函数 → 需让调用方传入「冲突时按哪个 key re-read」或在各 ensure_* 包装。

**测试**：模拟并发插入两条相同 key → 一个 IntegrityError → **session 不脏** + 返回赢家 bucket + 下游 `reserve_credits_or_raise` 不 500。

## 必修 2（P2#1，去重脚本正确性）— 结构化去重

**问题**：`gateway/scripts/dedupe_subscription_buckets.py` 对幻影 bucket 只 `shadow_rollback`（清 remaining/reserved + 写 ledger 条目），**不改 key 字段、不删行** → 幻影行仍满足 unique-index 谓词 → 若真存在 dup，alembic 044 建索引仍会 duplicate-key 失败。脚本"能修复以便建索引"的说法不成立。

**修法**：幻影 bucket 要么（a）把其 `credits_ledger` 条目 re-point 到保留的赢家 bucket 后**物理删除**幻影行（绕开 `credits_ledger.bucket_id` NOT NULL FK），要么（b）改其某个 key 字段使其不再匹配 unique 谓词（但会改身份，慎）。或在迁移内置一个结构化 dedup step（merge ledger + delete loser）。**推荐 (a) merge-then-delete**。

## 然后才上 044

两个 P2 修完 + 测试绿 → 再加：alembic 044 三 index + 模型 `__table_args__` 同步（含 `sqlite_where`）+ 契约测试（三 index 模型/迁移双声明）+ `credits_service.py`/`models.py` 基线 bump。生产 `alembic upgrade head` 走维护窗口。

## 红线 / 经验

- 付费 API 硬约束；金融 schema migration 按 credits 044 纪律。
- **新文件必须 ruff-clean**（CI 只对新增 .py 阻断）：新 migration **别照搬 043 的 `from typing import Sequence, Union` / `Union[str, None]`**（触 UP035/UP007），改用 `from collections.abc import Sequence` + `str | None`；新文件 datetime 用 `datetime.UTC`（UP017），不用 `timezone.utc`（仅历史文件 grandfather）。
