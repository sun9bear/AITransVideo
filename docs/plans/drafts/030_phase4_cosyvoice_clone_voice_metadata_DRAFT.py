"""Phase 4.1 schema draft: CosyVoice clone voice metadata + worker dispatch fields.

Revision ID: 030_phase4_cosyvoice_clone_voice_metadata
Revises: 029_pan_backup
Create Date: 2026-05-25

⚠️ **此文件不是真实 Alembic migration**——故意放在
``docs/plans/drafts/`` 而**不是** ``gateway/alembic/versions/``，目的是
让 ``alembic upgrade head`` **永远不会** 自动执行本草案（Codex 2026-05-25
二轮 P1 finding：先前位置在 versions/ 但靠 docstring 警告无法阻止自动
执行，现已物理移出）。

进入 Phase 4.1 编码 PR 时的迁移步骤：

1. 把本文件复制到 ``gateway/alembic/versions/030_phase4_cosyvoice_clone_voice_metadata.py``
2. 删除本顶部 "⚠️ 此文件不是真实 Alembic migration" 段
3. **同一 PR** 给 ``gateway/models.py::UserVoice`` 加 9 个 ORM 字段，
   字段名 / 类型 / 默认值与本 migration 完全对齐，避免下次 autogenerate
   提议 ``DROP COLUMN``
4. ``alembic upgrade 029_pan_backup:030 --sql`` 再 review 一次 SQL
5. 业务接线时再 ``alembic upgrade head`` 实跑

Plan：``docs/plans/2026-05-24-cosyvoice-phase4-go-live-plan.md`` §User
Voice Library Schema。本 migration 给 ``user_voices`` 表增加 9 个字段
支撑 Phase 4.1 业务接线：

1. ``region_constraint`` — overseas_ok / mainland_only 派生 worker 路由
2. ``requires_worker`` — 显式标记是否走武汉 worker（派生自 region_constraint）
3. ``target_model`` — CosyVoice TTS 模型锁定（clone 时绑定，后续 TTS 必须用同一模型）
4. ``worker_provider`` — "cosyvoice" 当前唯一值，预留 "doubao" 扩展
5. ``worker_region`` — "cn-wuhan" 当前唯一值，预留多 region
6. ``clone_api_model`` — CosyVoice 官方 API 模型名 "voice-enrollment"
7. ``billing_sku`` — 阿里云后台账单 SKU（Codex 三轮提示：首次实账单后回填，
   **不要在 Phase 4.1 编码时写死**——账单核对发现 voice-enrollment 没有
   独立计费行的可能性存在）
8. ``clone_provider_request_id`` — DashScope SDK request id（**永久 nullable**：
   Codex 2026-05-25 账单核对发现 ``consumedetailbillv2.csv`` 不返此列，
   request_id 只用于客服追溯，不再是计费对账主键）
9. ``clone_worker_request_id`` — worker 端 UUID（audit trail 主锚点）

旧 row（MiniMax / VolcEngine 现存音色）兜底：

- ``region_constraint`` 默认 ``"overseas_ok"`` — 现存 voice 都是海外 endpoint 可达
- ``requires_worker`` 默认 ``False``
- 其它 7 个字段 nullable，CosyVoice clone 才填

索引：

- ``idx_user_voices_phase4_region``: ``(user_id, region_constraint)``——
  让"列出用户的某 region 音色"快
- ``idx_user_voices_clone_provider_request_id``: 给客服 / 支持工单追溯
  单条调用用（哪怕账单不 join，单条调用查询仍要快）

**Codex 2026-05-25 提醒（保留项）**：

- ``billing_sku`` 暂时不要默认写"voice-enrollment-domestic" 类字符串：
  Phase 2 真实联调时阿里云账单只产生 5 条 TTS 字数用量行，**没有声音
  复刻独立计费行**。Phase 4.1 首次真实 clone 后才能确认 SKU 名（或者
  发现复刻仍是免费 / 没单独 SKU）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "030_phase4_cosyvoice_clone_voice_metadata"
down_revision: Union[str, None] = "029_pan_backup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 默认值常量，与 ``src/services/mainland_worker/types.py`` 保持一致
_REGION_CONSTRAINT_OVERSEAS_OK = "overseas_ok"


def upgrade() -> None:
    # ---- 路由决策字段 ----
    op.add_column(
        "user_voices",
        sa.Column(
            "region_constraint",
            sa.String(length=20),
            nullable=False,
            server_default=_REGION_CONSTRAINT_OVERSEAS_OK,
        ),
    )
    op.add_column(
        "user_voices",
        sa.Column(
            "requires_worker",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ---- TTS 模型锁定 ----
    op.add_column(
        "user_voices",
        sa.Column("target_model", sa.String(length=50), nullable=True),
    )

    # ---- Worker 路由元数据 ----
    op.add_column(
        "user_voices",
        sa.Column("worker_provider", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("worker_region", sa.String(length=30), nullable=True),
    )

    # ---- Clone API 模型 vs 账单 SKU 拆分 ----
    op.add_column(
        "user_voices",
        sa.Column("clone_api_model", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("billing_sku", sa.String(length=100), nullable=True),
    )

    # ---- Audit 锚点（永久 nullable，Codex 2026-05-25 账单核对决策）----
    op.add_column(
        "user_voices",
        sa.Column("clone_provider_request_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_voices",
        sa.Column("clone_worker_request_id", sa.String(length=64), nullable=True),
    )

    # ---- 索引 ----
    op.create_index(
        "idx_user_voices_phase4_region",
        "user_voices",
        ["user_id", "region_constraint"],
    )
    op.create_index(
        "idx_user_voices_clone_provider_request_id",
        "user_voices",
        ["clone_provider_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_voices_clone_provider_request_id",
        table_name="user_voices",
    )
    op.drop_index(
        "idx_user_voices_phase4_region",
        table_name="user_voices",
    )

    op.drop_column("user_voices", "clone_worker_request_id")
    op.drop_column("user_voices", "clone_provider_request_id")
    op.drop_column("user_voices", "billing_sku")
    op.drop_column("user_voices", "clone_api_model")
    op.drop_column("user_voices", "worker_region")
    op.drop_column("user_voices", "worker_provider")
    op.drop_column("user_voices", "target_model")
    op.drop_column("user_voices", "requires_worker")
    op.drop_column("user_voices", "region_constraint")
