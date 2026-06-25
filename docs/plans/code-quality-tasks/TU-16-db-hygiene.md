# TU-16 · 数据库 / 模型 / 迁移卫生

- **目标 / 价值**：消除 8 个数据库层隐患：Alembic heads 断言防止未来双分叉、`CreditsLedger.direction` CHECK 约束防止无效账务值静默落库、`GET /users` 加分页防止全表扫打崩 Admin、连接池加 `pool_pre_ping` + `statement_timeout` 防止僵尸连接堆积、`FreeServiceDailyUsage`/`BackupRecord`/`PanOauthState` 补 `__table_args__` 防止 autogenerate 误删已有索引、公告 fan-out 批量化防止大受众持锁超时、匿名上传的独立 sync psycopg2 引擎收口到连接池、`SupportAIUsage` 成本列从 `Float` 改 `Numeric` 防止累积浮点漂移。
- **关联发现**：DB-001 · DB-002 · DB-003 · DB-004 · DB-005 · DB-006 · DB-007 · DB-009 · DB-010
- **前置依赖**：无（可并行启动）
- **建议分支**：`quality/db-hygiene`
- **预估工时**：M（估 2–3 天；Step 3 需要新建 migration 且要求生产可回滚，其余均 S）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`tail`→`Select-Object -Last`、`test -f`→`Test-Path`、`wc -l`→`(Get-Content file | Measure-Object -Line).Lines`；避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **PR 拆分为两批**：PR1（非 migration / 低风险）含 `pool_pre_ping`、`statement_timeout`、`GET /users` 分页、ORM default 对齐（`UserNotification.id` 补 `server_default=text("gen_random_uuid()")`）；migration PR（DB-003、DB-009）单独排维护窗口。
- **`CreditsLedger.direction` CHECK 白名单双重确认**：白名单必须由"代码实际方向值 + 生产 `SELECT DISTINCT direction FROM credits_ledger`"双重确认得出，至少覆盖 `grant/reserve/capture/release/revoke/rollback`；**禁止照模型注释或文档草稿直接抄写**（原文档注释列的 `refund` 需在生产确认是否实际存在）。
- **`UserNotification.id` 补 `server_default`**：纳入 PR1 ORM 对齐改动，添加 `server_default=text("gen_random_uuid()")`，消除批量 insert 手动填 `id` 的歧义。
- **`SupportAIUsage` Float→Numeric 暂缓**：属独立 migration，不与 DB-003 合并，单独排维护窗口，不阻塞 PR1 合入。
- **migration PR 需维护窗口**：DB-003（042）和 DB-009（043）涉及 CHECK 约束与列类型变更，生产部署须在维护窗口执行，并以 Step 3b 脏数据 SQL 确认为前置动作。
- **`pool_pre_ping` 对齐**：anonymous_preview_api.py 单例引擎同步追加 `pool_pre_ping=True`，与主池策略一致（已在 Step 7 代码示例中体现）。
- **方向白名单以生产实查为准**：`revoke` 是已确认合法值；`refund` 须生产 SQL 实查后决定是否纳入，不得从注释推断。

---

## 不在本单元范围（out-of-scope）

- DB-008（`models.py` 按域拆分为多文件）——规模大、风险高，属独立重构单元，需先补 contract 测试。
- `anonymous_preview_api.py` 的完整 async 重写（DB-007 这里只做最小收口：消除每次请求新建引擎，改用单例复用）。
- ASYNC 系列问题（SMS/CAPTCHA 同步阻塞等），属 TU 其他单元。
- 生产环境 `alembic upgrade head` 的实际执行——需 ⚠️ 项目主在维护窗口操作。

---

## 必守不变量

- **付费 API 红线**：本单元所有改动均为 DB 层卫生（连接池 / 迁移 / ORM 模型），不涉及 MiniMax 克隆 / 付费 TTS / 付费 LLM / 付费 ASR 调用路径；不新增任何付费调用点。
- **金融数据不改语义**：`CreditsLedger.direction` CHECK 约束只新增合法值白名单，已有合法行零影响；`SupportAIUsage` Float→Numeric 迁移只影响新写入精度，不改历史行。
- **Gateway 是 plan/pricing/entitlement 唯一真源**：本单元不下沉任何定价逻辑。
- **默认测试不接真实外部服务**：新增测试全部使用 AST/静态检查或 in-memory mock，不连接真实 PostgreSQL。
- **回滚安全性**：每个 migration `downgrade()` 必须能将 DB 恢复至改动前状态；CHECK 约束和列类型变更需验证 downgrade 路径。

---

## Step 0 · 确认现状

```bash
git switch -c quality/db-hygiene

# 1. 确认当前 heads 唯一（预期：1 行，且末尾含 "(head)"）
cd gateway && python -m alembic heads
# 预期输出：041_payment_order_reconcile (head)

# 2. 核对关键 file:line（行号以实际为准）
grep -n "create_async_engine" gateway/database.py             # 预期 ≈ L36
grep -n "class CreditsLedger" gateway/models.py               # 预期 ≈ L653
grep -n "direction.*Mapped\|direction.*String" gateway/models.py  # 预期 ≈ L681
grep -n "class FreeServiceDailyUsage" gateway/models.py       # 预期 ≈ L1089
grep -n "class BackupRecord" gateway/models.py                # 预期 ≈ L1660
grep -n "class PanOauthState" gateway/models.py               # 预期 ≈ L1698
grep -n "class SupportAIUsage" gateway/models.py              # 预期 ≈ L1361
grep -n "input_usd_per_1m\|output_usd_per_1m\|estimated_cost" gateway/models.py  # 预期 ≈ L1396–1404
grep -n "def list_users" gateway/admin_settings.py            # 预期 ≈ L1843
grep -n "select(User).*order_by" gateway/admin_settings.py    # 预期 ≈ L1851
grep -n "_make_sync_intake_session\|pool_size=1" gateway/anonymous_preview_api.py  # 预期 ≈ L221,236
grep -n "for uid in targets" gateway/system_announcements_service.py  # 预期 ≈ L408
```

> 若某处行号偏移，以 `grep` 实际结果为准，文档注释处标注"实际位置 L<N>"。

---

## Step 1 · DB-001：Alembic heads 单头断言测试（防双分叉回归）

**背景**：036 曾产生两个分叉（`036_job_language_fields` 和 `036_payment_order_last_reconciled_at`），041 已通过 merge migration 收拢为单头。需在测试层加永久断言，防止未来再次出现双分叉。

**文件**：`tests/test_db_migration_hygiene.py`（新建）

**改法**：新增一个只读 AST+文件扫描的测试，不需要真实数据库连接：

```python
"""DB-001: Alembic heads 单头断言 — 防双分叉回归。

不连接真实数据库；纯静态扫描 gateway/alembic/versions/*.py 的 down_revision 图，
重建 head 集合（= 没有任何其他 migration 以它为 down_revision 的节点）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "gateway" / "alembic" / "versions"


def _parse_revision_fields(path: Path) -> tuple[str | None, tuple[str, ...]]:
    """返回 (revision, down_revision_tuple)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rev = None
    down: tuple[str, ...] = ()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if t.id == "revision" and isinstance(value, ast.Constant):
                rev = value.value
            if t.id == "down_revision":
                if isinstance(value, ast.Constant) and value.value is not None:
                    down = (value.value,)
                elif isinstance(value, (ast.Tuple, ast.List)):
                    down = tuple(
                        e.value for e in value.elts if isinstance(e, ast.Constant) and e.value
                    )
    return rev, down


def test_alembic_single_head() -> None:
    """gateway/alembic/versions/ 有且仅有一个 head（无双分叉）。"""
    revisions: dict[str, tuple[str, ...]] = {}  # rev_id -> down_revision tuple
    for path in _VERSIONS_DIR.glob("*.py"):
        rev, down = _parse_revision_fields(path)
        if rev:
            revisions[rev] = down

    # head = 没有其他节点把它列入自己的 down_revision
    all_parents: set[str] = set()
    for parents in revisions.values():
        all_parents.update(parents)

    heads = [r for r in revisions if r not in all_parents]
    assert len(heads) == 1, (
        f"Alembic heads 应恰好 1 个，实际 {len(heads)} 个: {heads}\n"
        "请用 merge migration 收拢分叉，参考 041_payment_order_last_reconciled_at.py。"
    )
```

**该步验收**：

```bash
python -m pytest tests/test_db_migration_hygiene.py::test_alembic_single_head -v
# 预期：1 passed
```

---

## Step 2 · DB-002：`GET /users` 加分页 LIMIT（`admin_settings.py:1851`）

**背景**：当前 `list_users()` 做无条件 `select(User).order_by(User.created_at.desc())`，用户量增长后会全表扫。加可选分页参数，默认 500 封顶。

**文件**：`gateway/admin_settings.py:1842–1887`

**改法**：为 `list_users` 增加 `limit`（默认 500，上限 1000）和 `offset`（默认 0）查询参数：

```python
# gateway/admin_settings.py — list_users 函数签名改为：
@router.get("/users")
async def list_users(
    user: User | None = Depends(get_current_user),
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """List users with role, plan, quota, and active job count.

    默认返回最新 500 条。大用户量场景请传 limit/offset 分页。
    """
    _require_admin(user)

    async with async_session() as db:
        users_result = await db.execute(
            select(User)
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        users = users_result.scalars().all()

        # 其余 active_counts / total_counts 查询保持不变 ...
```

同时在 return dict 里增加分页元数据：

```python
    return {
        "users": [...],          # 原有字段不变
        "limit": limit,
        "offset": offset,
        "count": len(users),     # 本页返回条数（方便前端判断是否还有下页）
    }
```

> 注意：`from fastapi import Query` 是否已在文件顶部 import — 若无，需补加。

**该步验收**：

```bash
# 1. 确认 .limit( 已进入代码
grep -n "\.limit(limit)" gateway/admin_settings.py    # 命中 ≥1

# 2. 确认 Query 参数已声明
grep -n "limit.*Query\|offset.*Query" gateway/admin_settings.py  # 命中 ≥2

# 3. 新增测试（在 test_db_migration_hygiene.py 或单独文件）
python -m pytest tests/test_db_migration_hygiene.py -v -k "list_users"
# 若测试写在独立文件：
python -m pytest tests/test_admin_list_users_pagination.py -v
```

**测试要点**（新增 `tests/test_admin_list_users_pagination.py` 或在 `test_db_migration_hygiene.py` 追加）：

- Mock `async_session` 返回固定用户列表，断言响应 JSON 包含 `limit`/`offset`/`count` 字段。
- 传 `limit=2&offset=0` 时，SQLAlchemy `select(User).limit(2).offset(0)` 被调用（用 `unittest.mock.patch` 捕获 `db.execute` 调用参数）。

---

## Step 3 · DB-003：`CreditsLedger.direction` 加 CHECK 约束（`models.py:681`）

**背景**：`direction` 注释里列出的合法值仅供参考，但无数据库级约束，非法值可静默落库，导致账务对账偏差。

✅ 已决策（CodeX 2026-06-25）：此改动纳入 migration PR，单独排维护窗口执行。**白名单必须由"代码实际写入路径 + 生产 `SELECT DISTINCT direction FROM credits_ledger` 双重确认"得出**，至少覆盖 `grant/reserve/capture/release/revoke/rollback`（`refund` 需生产实查确认是否存在，不得从注释推断）；migration PR 在执行时 Step 3b 的脏数据 SQL 检查是强制前置动作（见下）。

### Step 3a · ORM 模型加 `CheckConstraint`

**文件**：`gateway/models.py:664–683`（`__table_args__` 元组）

```python
# gateway/models.py — CreditsLedger.__table_args__ 改为：
# ⚠️ 执行时前置动作（已定方向）：白名单在写入 migration 前必须先运行：
#   SELECT DISTINCT direction FROM credits_ledger;
# 并对照代码实际写入路径确认合法值集合。
# 至少覆盖: grant/reserve/capture/release/revoke/rollback
# refund 须生产实查后决定是否纳入，禁止照注释抄写。
# 下方示例以已确认的最小覆盖集为占位，执行者务必用实查结果替换。
__table_args__ = (
    Index("idx_credits_ledger_user_id", "user_id"),
    Index("idx_credits_ledger_bucket_id", "bucket_id"),
    Index("idx_credits_ledger_direction", "direction"),
    Index("idx_credits_ledger_created_at", "created_at"),
    CheckConstraint(
        "direction IN ('grant','reserve','capture','release','revoke','rollback')",
        # ^ 执行时用生产 SELECT DISTINCT 结果替换此字符串；至少含上述 6 值
        name="ck_credits_ledger_direction_valid",
    ),
)
```

确认 `CheckConstraint` 已在文件顶部 import（与 `Index`、`ForeignKey` 等同模块）：

```python
from sqlalchemy import CheckConstraint, Index, ...
```

### Step 3b · 新建 Alembic migration 042

**文件**：`gateway/alembic/versions/042_credits_ledger_direction_check.py`（新建）

```python
"""Add CHECK constraint to credits_ledger.direction.

Revision ID: 042_credits_ledger_direction_check
Revises: 041_payment_order_reconcile
Create Date: 2026-06-xx

金融安全：防止无效 direction 值静默落库。

⚠️ 执行时前置动作（已定方向）：
  在生产库运行以下 SQL，用实际结果替换 _VALID_DIRECTIONS：
    SELECT DISTINCT direction FROM credits_ledger ORDER BY direction;
  再对照代码写入路径（reserve/capture/release/grant/revoke/rollback）确认。
  至少覆盖: grant | reserve | capture | release | revoke | rollback
  refund 须实查后决定是否纳入，禁止照注释抄写。

脏数据检查（upgrade 前必须输出空集）：
  SELECT direction, COUNT(*) FROM credits_ledger
  WHERE direction NOT IN (<实查白名单>)
  GROUP BY direction;
"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "042_credits_ledger_direction_check"
down_revision: Union[str, None] = "041_payment_order_reconcile"
branch_labels = None
depends_on = None

# ⚠️ 执行时前置动作（已定方向）：以生产 SELECT DISTINCT direction FROM credits_ledger
# 的实际结果 + 代码写入路径双重确认后，替换此字符串。
# 示例覆盖已确认的 6 值；若生产存在 refund 等额外值，须一并纳入。
_VALID_DIRECTIONS = "('grant','reserve','capture','release','revoke','rollback')"
_CONSTRAINT_NAME = "ck_credits_ledger_direction_valid"


def upgrade() -> None:
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "credits_ledger",
        f"direction IN {_VALID_DIRECTIONS}",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "credits_ledger", type_="check")
```

**该步验收**：

```bash
# 1. ORM 模型含 CheckConstraint
grep -n "CheckConstraint\|ck_credits_ledger" gateway/models.py   # 命中 ≥2

# 2. migration 文件存在且 down_revision 正确
grep -n "down_revision\|revision" gateway/alembic/versions/042_credits_ledger_direction_check.py
# 预期: revision = "042_credits_ledger_direction_check", down_revision = "041_payment_order_reconcile"

# 3. heads 单头断言仍通过（042 接在 041 后，无分叉）
cd gateway && python -m alembic heads
# 预期: 042_credits_ledger_direction_check (head)

# 4. 已有 heads 单头测试自动覆盖（重新运行）
python -m pytest tests/test_db_migration_hygiene.py::test_alembic_single_head -v
# 预期：1 passed

# 5. 新增测试：断言 migration 042 upgrade/downgrade 语法合法（AST 检查）
python -m pytest tests/test_db_migration_hygiene.py -v -k "direction_check"
```

---

## Step 4 · DB-004：连接池加 `pool_pre_ping` + `statement_timeout`（`database.py:36`）

**背景**：当前 `create_async_engine(resolved, echo=False, pool_size=5, max_overflow=10)` 缺少 `pool_pre_ping`，僵尸连接复用会导致 `OperationalError`。同时缺少语句级超时，慢查询会阻塞事件循环。

**文件**：`gateway/database.py:36`

**改法**：

```python
# gateway/database.py:36 — init_db 内改为：
_engine = create_async_engine(
    resolved,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,           # DB-004: 复用前 ping，防僵尸连接
    connect_args={
        "server_settings": {
            "statement_timeout": "30000",   # 30s，单位毫秒（asyncpg 语法）
        }
    },
)
```

> **asyncpg 注意**：asyncpg 通过 `server_settings` 传 PostgreSQL `GUC` 参数。`statement_timeout` 值为字符串格式的毫秒数。30 秒是覆盖绝大多数合法查询的安全上限；长跑批量任务（如 fan-out、analytics）如需更长超时，应在独立连接/session 里 `SET statement_timeout` 覆盖。

**该步验收**：

```bash
grep -n "pool_pre_ping" gateway/database.py          # 命中 1，值为 True
grep -n "statement_timeout" gateway/database.py      # 命中 1
python -m pytest tests/test_gateway_lazy_init_smoke.py -v   # 预期：全部 passed（不回归现有 DB init 测试）
```

---

## Step 5 · DB-005 / DB-010：`FreeServiceDailyUsage` / `BackupRecord` / `PanOauthState` 补 `__table_args__`

**背景**：
- `FreeServiceDailyUsage`（`models.py:1089`）缺 `__table_args__`，但 migration 034 已建了 3 个索引（`uq_free_daily_active_idem`、`idx_free_daily_user_date_status`、`idx_free_daily_ttl_pending`）。autogenerate 时 ORM 不知道这些索引存在，会生成 `drop_index` 指令。
- `BackupRecord`（`models.py:1660`）缺 `__table_args__`，但 migration 029 建了 4 个索引（`uniq_backup_in_flight`、`idx_backup_user_status`、`idx_backup_user_job_gen`、`idx_backup_heartbeat`）。
- `PanOauthState`（`models.py:1698`）无额外索引（只有主键 `token`），补空 `__table_args__` 是防御性声明。

**文件**：`gateway/models.py`

### FreeServiceDailyUsage（`models.py:1101` 处，在 `__tablename__` 之后插入）

```python
class FreeServiceDailyUsage(Base):
    __tablename__ = "free_service_daily_usage"
    __table_args__ = (
        # migration 034 建立的索引 — 声明于此防 autogenerate 误删
        Index(
            "uq_free_daily_active_idem",
            "user_id",
            "create_idempotency_key",
            unique=True,
            postgresql_where=text("status = 'reserved'"),
        ),
        Index("idx_free_daily_user_date_status", "user_id", "usage_date", "status"),
        Index(
            "idx_free_daily_ttl_pending",
            "expires_at",
            postgresql_where=text("status = 'reserved'"),
        ),
    )
    # 其余列定义不变 ...
```

### BackupRecord（`models.py:1661` 处，在 `__tablename__` 之后插入）

```python
class BackupRecord(Base):
    __tablename__ = "backup_records"
    __table_args__ = (
        # migration 029 建立的索引 — 声明于此防 autogenerate 误删
        Index(
            "uniq_backup_in_flight",
            "user_id", "job_id", "provider", "job_edit_generation",
            unique=True,
            postgresql_where=text("status IN ('uploading', 'restoring')"),
        ),
        Index("idx_backup_user_status", "user_id", "status"),
        Index("idx_backup_user_job_gen", "user_id", "job_id", "job_edit_generation"),
        Index(
            "idx_backup_heartbeat",
            "heartbeat_at",
            postgresql_where=text("status IN ('uploading', 'restoring')"),
        ),
    )
    # 其余列定义不变 ...
```

### PanOauthState（`models.py:1699` 处，在 `__tablename__` 之后插入）

```python
class PanOauthState(Base):
    __tablename__ = "pan_oauth_states"
    __table_args__ = ()   # 无额外索引；声明防 autogenerate 扫描漏报
    # 其余列定义不变 ...
```

**该步验收**：

```bash
# 1. 三个类均有 __table_args__
grep -n "__table_args__" gateway/models.py | grep -E "1[01][0-9][0-9]|16[0-9][0-9]|17[0-9][0-9]"
# 预期：FreeServiceDailyUsage (≈L1102)、BackupRecord (≈L1662)、PanOauthState (≈L1700) 均命中

# 2. 确认 Index 名称与 migration 里一致（防拼写错误）
grep -n "uq_free_daily_active_idem\|idx_free_daily_user_date_status\|idx_free_daily_ttl_pending" gateway/models.py
grep -n "uniq_backup_in_flight\|idx_backup_user_status\|idx_backup_user_job_gen\|idx_backup_heartbeat" gateway/models.py
# 每个预期命中 1 次

# 3. 新增静态守卫测试（追加到 test_db_migration_hygiene.py）
python -m pytest tests/test_db_migration_hygiene.py::test_orm_models_declare_table_args -v
# 预期：1 passed
```

**新增测试**（追加到 `tests/test_db_migration_hygiene.py`）：

```python
def test_orm_models_declare_table_args() -> None:
    """DB-005/010: 指定模型必须有 __table_args__ 以防 autogenerate 误删迁移索引。"""
    import importlib
    models = importlib.import_module("models")
    for cls_name in ("FreeServiceDailyUsage", "BackupRecord", "PanOauthState"):
        cls = getattr(models, cls_name)
        assert hasattr(cls, "__table_args__"), (
            f"{cls_name} 缺少 __table_args__，autogenerate 可能误删 migration 创建的索引"
        )
```

---

## Step 6 · DB-006：公告 fan-out 批量写（`system_announcements_service.py:408`）

**背景**：`send_announcement()` 在单事务里用 Python `for` 循环逐条 `db.add(row)` 建 `UserNotification`，最后一次性 `flush()`。当受众为全量用户（数千条）时，该事务持锁时间过长，存在超时风险。

**改法**：将逐条 `db.add()` 替换为 `db.execute(insert(...).values([...]))` 批量写，利用 `postgresql_insert_many`（asyncpg 原生支持）或 SQLAlchemy Core `insert().values(list)`：

**文件**：`gateway/system_announcements_service.py:407–426`

```python
# 改前（逐条 add）：
for uid in targets:
    row = UserNotification(...)
    db.add(row)

# 改后（批量 insert）：
from sqlalchemy.dialects.postgresql import insert as pg_insert

if targets:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "scope": "system",
            "topic": announcement.topic,
            "user_id": uid,
            "title": announcement.title[:255],
            "body": announcement.body,
            "severity": announcement.severity,
            "related_type": "system_announcement",
            "related_id": str(announcement.id),
            "action_url": announcement.action_url,
            "popup": bool(announcement.popup),
            "metadata_json": {
                "announcement_id": str(announcement.id),
                "audience_kind": announcement.audience_kind,
                "popup": bool(announcement.popup),
            },
        }
        for uid in targets
    ]
    # 每批 500 条，防止单次 VALUES 子句过大
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        await db.execute(
            pg_insert(UserNotification).values(rows[i : i + BATCH])
        )
```

> 注意：如果 `UserNotification` 已有 `created_at` 默认值（`server_default` 或 ORM `default`），批量 insert 时需确认列定义不需要手动填充。若 `created_at` 是纯 ORM-side `default=lambda: datetime.now()`，batch insert 时需显式传入（SQLAlchemy Core insert 不触发 ORM default）——可在 rows 列表里增加 `"created_at": now`。

✅ 已决策（CodeX 2026-06-25）：`UserNotification.id` 在 PR1（ORM default 对齐）中补加 `server_default=text("gen_random_uuid()")`，批量 insert 无需手动填 `id`。在 PR1 合入后，本步骤可直接按批量 insert 模板实现，无需手动传 `id` 字段。执行前用 `grep -n "class UserNotification" gateway/models.py` 确认 `server_default` 已到位。

**该步验收**：

```bash
# 1. 循环 db.add(row) 已消失（fan-out 路径）
grep -n "db\.add(row)" gateway/system_announcements_service.py
# 预期：0 条（若其他地方还有 db.add，这里改只影响 send_announcement 函数内的那一处）

# 2. pg_insert 批量写已加入
grep -n "pg_insert\|BATCH\|values(rows" gateway/system_announcements_service.py  # 命中 ≥2

# 3. 跑现有通知测试确认不回归
python -m pytest tests/ -k "announcement" -v
# 预期：所有 announcement 相关测试 passed
```

---

## Step 7 · DB-007：`anonymous_preview_api.py` 独立引擎收口（`anonymous_preview_api.py:221–238`）

**背景**：`_make_sync_intake_session()` 在每次 `run_intake_and_save`（即每次匿名视频上传）时新建一个完整的 `create_engine(...)` + `sessionmaker` + `session()`，绕过了 `gateway/database.py` 的统一连接池（`pool_size=5, max_overflow=10`）。高并发时会开出过多独立连接。

**改法**：将 `_make_sync_intake_session` 改为单例引擎（模块级懒初始化，只建一次），复用连接池：

**文件**：`gateway/anonymous_preview_api.py:221–238`

```python
# 模块级单例 — 只在首次调用时初始化（import-safe）
_sync_engine = None
_sync_session_factory = None


def _make_sync_intake_session():
    """返回可复用连接池的同步 Session（单例引擎，首次调用时初始化）。

    使用 pool_size=2, max_overflow=3 独立池（匿名上传专用，与主 async 池隔离），
    避免高并发时与主 gateway 竞争 DB 连接配额。
    """
    global _sync_engine, _sync_session_factory
    if _sync_engine is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from config import resolve_database_url
        url = resolve_database_url(settings)
        sync_url = url.replace("postgresql+asyncpg://", "postgresql://")
        _sync_engine = create_engine(
            sync_url,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,      # 与主池保持一致
        )
        _sync_session_factory = sessionmaker(bind=_sync_engine)
    return _sync_session_factory()
```

> 若未来 `anonymous_preview_api` 有重置测试隔离需求，可暴露 `_reset_sync_engine_for_test()` 把全局置 None。

**该步验收**：

```bash
# 1. create_engine 不再在函数体内每次调用
grep -n "create_engine" gateway/anonymous_preview_api.py
# 预期：命中在 "if _sync_engine is None:" 分支内（不在每次 _make_sync_intake_session 调用路径的顶层）

# 2. 模块级全局变量存在
grep -n "_sync_engine\|_sync_session_factory" gateway/anonymous_preview_api.py  # ≥4 处命中

# 3. pool_pre_ping 已加
grep -n "pool_pre_ping" gateway/anonymous_preview_api.py  # 命中 1

# 4. 现有 anonymous_preview 测试不回归
python -m pytest tests/ -k "anonymous_preview" -v --tb=short 2>&1 | tail -20
# 预期：无新 FAILED
```

---

## Step 8 · DB-009：`SupportAIUsage` Float → Numeric（`models.py:1396–1404`）

**背景**：`input_usd_per_1m_tokens`、`output_usd_per_1m_tokens`、`estimated_cost_usd` 三列用 `Float`，货币/单价累加会有浮点漂移（如 0.1 + 0.2 ≠ 0.3）。改为 `Numeric(precision=18, scale=8)` 与财务精度对齐。

**⚠️ 需项目主确认**：
1. `SupportAIUsage` 是只读成本审计表还是参与累加运算？如果仅作存储（读取时 Python float 运算），Float 漂移影响有限——项目主需确认是否接受此改动的 migration 代价。
2. 生产库中该表体量（`SELECT COUNT(*) FROM support_ai_usage`）——若超过 10 万行，`ALTER COLUMN` 可能需要较长锁时间，考虑 `ALTER TABLE ... ALTER COLUMN ... TYPE NUMERIC USING ...` 加 `CONCURRENTLY` 辅助索引策略。

### Step 8a · ORM 模型改 `Numeric`

**文件**：`gateway/models.py:1396–1404`

```python
# gateway/models.py — SupportAIUsage 三列改为：
from sqlalchemy import Numeric  # 确认已 import（与 Float 同模块）

input_usd_per_1m_tokens: Mapped[Decimal] = mapped_column(
    Numeric(precision=18, scale=8), nullable=False, server_default="0"
)
output_usd_per_1m_tokens: Mapped[Decimal] = mapped_column(
    Numeric(precision=18, scale=8), nullable=False, server_default="0"
)
estimated_cost_usd: Mapped[Decimal] = mapped_column(
    Numeric(precision=18, scale=8), nullable=False, server_default="0"
)
```

同时在文件顶部确认：

```python
from decimal import Decimal   # 若尚未 import
```

> `Mapped[Decimal]` 需要 `from decimal import Decimal`；`Numeric` 来自 `sqlalchemy`。

### Step 8b · 新建 migration 043（或 042 若 Step 3 已用 042）

> **顺序**：若 Step 3（DB-003）已建了 `042_credits_ledger_direction_check.py`，则本 migration 编号为 043；若 DB-003 跳过，则编号 042。以下以 **043** 为例。

**文件**：`gateway/alembic/versions/043_support_ai_usage_numeric.py`（新建）

```python
"""SupportAIUsage 成本列 Float → Numeric(18,8).

Revision ID: 043_support_ai_usage_numeric
Revises: 042_credits_ledger_direction_check
Create Date: 2026-06-xx

DB-009: 防止货币/单价浮点累积漂移。影响三列：
  input_usd_per_1m_tokens, output_usd_per_1m_tokens, estimated_cost_usd
"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "043_support_ai_usage_numeric"
down_revision: Union[str, None] = "042_credits_ledger_direction_check"
branch_labels = None
depends_on = None

_TABLE = "support_ai_usage"
_COLS = [
    "input_usd_per_1m_tokens",
    "output_usd_per_1m_tokens",
    "estimated_cost_usd",
]
_NUMERIC = sa.Numeric(precision=18, scale=8)
_FLOAT = sa.Float()


def upgrade() -> None:
    for col in _COLS:
        op.alter_column(
            _TABLE,
            col,
            type_=_NUMERIC,
            postgresql_using=f"{col}::numeric",
        )


def downgrade() -> None:
    for col in _COLS:
        op.alter_column(
            _TABLE,
            col,
            type_=_FLOAT,
            postgresql_using=f"{col}::float",
        )
```

**该步验收**：

```bash
# 1. ORM 模型三列改为 Numeric
grep -n "Numeric\|Decimal" gateway/models.py | grep -i "support\|usd_per\|cost_usd"  # ≥3 命中

# 2. Float 不再出现在这三列
grep -n "Float" gateway/models.py | grep -E "usd_per|cost_usd"  # 预期：0 命中

# 3. migration 文件存在且 down_revision 正确
grep -n "down_revision" gateway/alembic/versions/043_support_ai_usage_numeric.py
# 预期：042_credits_ledger_direction_check

# 4. heads 仍单头
cd gateway && python -m alembic heads
# 预期：043_support_ai_usage_numeric (head)

# 5. 单头断言测试通过
python -m pytest tests/test_db_migration_hygiene.py::test_alembic_single_head -v
```

---

## 测试计划

### 新增测试文件

| 文件 | 覆盖 | 说明 |
|---|---|---|
| `tests/test_db_migration_hygiene.py` | DB-001 · DB-005 · DB-010 · DB-003（migration 静态） | 纯 AST/静态扫描，不需要真实 DB |
| `tests/test_admin_list_users_pagination.py` | DB-002 | Mock `async_session`，断言 `limit`/`offset` 参数传入 |

### 测试内容清单

1. `test_alembic_single_head` — 静态扫 `versions/*.py`，断言 head 集合大小 == 1。
2. `test_alembic_direction_check_migration_syntax` — 断言 `042_credits_ledger_direction_check.py` 存在、`down_revision` 正确、`upgrade`/`downgrade` 函数存在。
3. `test_orm_models_declare_table_args` — 断言 `FreeServiceDailyUsage`/`BackupRecord`/`PanOauthState` 均有 `__table_args__` 属性。
4. `test_list_users_respects_limit_param` — FastAPI TestClient，传 `limit=2` 时 SQL 构造包含 `.limit(2)`（mock db.execute）。
5. `test_list_users_response_has_pagination_fields` — 断言响应 JSON 有 `limit`、`offset`、`count` 字段。

### 回归测试

```bash
# 运行与本单元相关的已有测试（不要全量跑，只跑相关域）
python -m pytest tests/test_gateway_lazy_init_smoke.py -v
python -m pytest tests/test_alembic_041_payment_order_reconcile.py -v
python -m pytest tests/ -k "announcement" -v
python -m pytest tests/ -k "anonymous_preview" -v --tb=short
```

---

## 回滚方案

| Step | 回滚范围 | 操作 |
|---|---|---|
| Step 1（DB-001 测试） | `tests/test_db_migration_hygiene.py` | `git revert` 该文件的 commit |
| Step 2（DB-002 分页） | `gateway/admin_settings.py` | `git revert` 该 commit；无 migration |
| Step 3（DB-003 CHECK） | `gateway/models.py` + `042_*`.py | `alembic downgrade 041_payment_order_reconcile`（DB 侧），`git revert` 两文件的 commit（代码侧） |
| Step 4（DB-004 连接池） | `gateway/database.py` | `git revert` 该 commit；无 migration |
| Step 5（DB-005/010 __table_args__） | `gateway/models.py` | `git revert` 该 commit；无 migration（只改 ORM 声明，不新建 DB 对象） |
| Step 6（DB-006 fan-out 批量） | `gateway/system_announcements_service.py` | `git revert` 该 commit |
| Step 7（DB-007 单例引擎） | `gateway/anonymous_preview_api.py` | `git revert` 该 commit |
| Step 8（DB-009 Numeric） | `gateway/models.py` + `043_*`.py | `alembic downgrade 042_credits_ledger_direction_check`（DB 侧），`git revert` 两文件（代码侧） |

**Commit 边界原则**：每个 Step 对应一个独立 commit，使得任意单步可以 `git revert` 而不影响其他 Step。migration 文件与对应 ORM 模型改动放在**同一个 commit** 里，保证代码与迁移始终同步。

---

## 完成定义（DoD）

- [ ] `cd gateway && python -m alembic heads` 输出恰好一行 `(head)`
- [ ] `python -m pytest tests/test_db_migration_hygiene.py -v` 全部 passed
- [ ] `python -m pytest tests/test_admin_list_users_pagination.py -v` 全部 passed
- [ ] `grep -n "pool_pre_ping" gateway/database.py` 命中且值为 `True`
- [ ] `grep -n "statement_timeout" gateway/database.py` 命中
- [ ] `grep -n "CheckConstraint\|ck_credits_ledger" gateway/models.py` 命中 ≥2
- [ ] `grep -n "__table_args__" gateway/models.py` 在 `FreeServiceDailyUsage`/`BackupRecord`/`PanOauthState` 三处均命中
- [ ] `grep -n "pg_insert\|values(rows" gateway/system_announcements_service.py` 命中 ≥2
- [ ] `grep -n "pool_pre_ping" gateway/anonymous_preview_api.py` 命中（DB-007 单例引擎）
- [ ] `grep -n "Numeric" gateway/models.py | grep -i "usd_per\|cost_usd"` 命中 ≥3（DB-009）
- [ ] migration 042 和 043 各自的 `downgrade()` 语法经人工审核（或本地测试库验证）可回滚
- [ ] 与本单元相关的已有测试（`test_alembic_041_*`、`test_gateway_lazy_init_smoke`、announcement、anonymous_preview 域）无新增 FAILED
- [ ] **各步独立 commit、显式 pathspec（`git commit -- <files>`）、未用 `git add .`**
