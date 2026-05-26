"""Phase 4.2 A.1: CosyVoice clone voice temporary-expiry lifecycle.

Revision ID: 031_user_voice_temp_expiry
Revises: 030_cosyvoice_clone_metadata
Create Date: 2026-05-26

Phase 4.2 plan v4-followup §12 用户决策落实。给 ``user_voices`` 表新增
2 个字段 + 1 个 partial index，支撑 CosyVoice 克隆音色的"任务级临时音色"
保存策略：

1. ``is_temporary`` BOOLEAN NOT NULL DEFAULT FALSE
   - TRUE = 任务级临时音色（用户克隆时未勾"保存到我的音色库"，默认）
   - FALSE = 长期保留音色（用户主动保存，跨任务可见）
   - 旧 row（Phase 4.1 已落 MiniMax / CosyVoice / VolcEngine voice）默认
     ``FALSE`` —— 行为字节级不变

2. ``temporary_expires_at`` TIMESTAMP WITH TIME ZONE NULL
   - 临时音色的计划过期时间（默认 ``now() + 7 days``，应用层填）
   - 仅 ``is_temporary=TRUE`` 时使用
   - **严禁**用于 active 判断 —— active 仍以现有 ``expired_at IS NULL`` 为准
   - **绝不**简写成 ``expires_at`` —— 现有 ``user_voices.expired_at``（软删
     时间戳）字面太近，简写会让后续 query 大概率写错（v4-followup §12.3 决策）

3. Partial index ``idx_user_voices_temp_expires_pending``
   - 索引 ``temporary_expires_at`` 列
   - 仅覆盖 ``is_temporary=TRUE AND expired_at IS NULL`` 的行
   - 给 Phase 4.2 的清理 sweeper 用（每日扫到期临时音色调 DashScope
     delete_voice）；命中行 cardinality 远小于全表，partial index 比全
     索引省 ~95% 空间

清理任务的 SELECT 与 partial index 完全对齐：

    SELECT id, voice_id FROM user_voices
    WHERE provider = 'cosyvoice_voice_clone'
      AND is_temporary = TRUE
      AND temporary_expires_at < now()
      AND expired_at IS NULL
    ORDER BY temporary_expires_at ASC
    LIMIT 200;

字段命名守卫：``tests/test_phase42_a1_user_voice_naming_guards.py``
锁死本表禁止新增裸 ``expires_at`` 列、ORM 禁止声明裸 ``expires_at``
属性、active query 必须用 ``expired_at IS NULL``（不得用
``temporary_expires_at``）。任何违反立刻 CI red。

ORM 一致性守卫：``tests/test_alembic_031_user_voice_temp_expiry.py``
AST 扫 migration + ORM 反射两端字段，类型 / nullable / server_default /
partial index where 子句全部对齐。

A.1 只做 schema + ORM + 守卫；endpoint / 前端 / Smart 路径不动。
本 migration 暂不部署 US prod —— 待 Phase 4.2 Phase F 统一部署。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "031_user_voice_temp_expiry"
down_revision: Union[str, None] = "030_cosyvoice_clone_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 临时音色标记 ----
    # 旧 row（Phase 4.1 之前所有 MiniMax / CosyVoice / VolcEngine voice）默认
    # FALSE，行为不变。新 CosyVoice 克隆若不勾"保存到我的音色库"则应用层
    # 写 TRUE。``server_default=sa.false()`` 让 ALTER 添加列时旧 row 自动
    # 兜底，避免 NOT NULL 约束在历史数据上失败。
    op.add_column(
        "user_voices",
        sa.Column(
            "is_temporary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ---- 临时音色计划过期时间 ----
    # ⚠️ 字段命名必须保持 ``temporary_expires_at`` —— 不要简写成
    # ``expires_at``。现有 ``user_voices.expired_at``（migration 010 落，
    # 软删时间戳）字面太近，简写会让后续 query 大概率写错（Codex 2026-05-26
    # v4-followup review 重点）。
    #
    # nullable=True：长期音色（is_temporary=FALSE）这一列必须是 NULL。
    # 旧 row 默认 NULL（is_temporary=FALSE → 没有计划过期）。
    op.add_column(
        "user_voices",
        sa.Column(
            "temporary_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ---- Partial index for 清理 sweeper ----
    # 仅索引"是临时音色 + 尚未软删"的行。覆盖 Phase 4.2 清理任务的 SELECT
    # 条件，让每日扫描 cost 与全表大小解耦。
    #
    # partial WHERE 必须包含 ``expired_at IS NULL`` —— 这是 v4-followup
    # 软删策略的核心：sweeper 成功 delete DashScope voice 后写
    # ``expired_at = now()``，让该行不再被本 index 命中，自动从下一轮 sweep
    # 候选集移除（幂等保证）。
    op.create_index(
        "idx_user_voices_temp_expires_pending",
        "user_voices",
        ["temporary_expires_at"],
        postgresql_where=sa.text("is_temporary = TRUE AND expired_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_user_voices_temp_expires_pending",
        table_name="user_voices",
    )
    op.drop_column("user_voices", "temporary_expires_at")
    op.drop_column("user_voices", "is_temporary")
