"""新增 udp_exceptions 表（目标感知 UDP 例外）

mac_workstation 按 process、mac_gateway 按 mac_address 放行 UDP（领域矩阵见
app/udp_policy.py）；本迁移只负责 schema：FK ON DELETE CASCADE、
(profile_id, match_type, match_value) 唯一、(profile_id, enabled, sort_order)
索引与 match_type CHECK。行级值校验在领域层 fail closed，CHECK 是绕写防线。

建表前经 autocommit_block 启用 PRAGMA foreign_keys=ON（PRAGMA 在事务内是
no-op，与运行时 DatabaseRuntime 每连接开 FK 同口径）。downgrade 直接删表
（新表无存量数据语义）。

Revision ID: 0005_udp_exceptions
Revises: 0004_rule_source_ip_rules
Create Date: 2026-10-16

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_udp_exceptions"
down_revision = "0004_rule_source_ip_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 的 PRAGMA foreign_keys 在事务内静默无效：先出事务再启用，
    # 建表连接随即可校验 FK 引用（表创建顺序上 config_profiles 已存在）
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")
    op.create_table(
        "udp_exceptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("match_value", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("match_type IN ('process', 'mac_address')",
                           name="ck_udp_exceptions_match_type"),
        sa.UniqueConstraint("profile_id", "match_type", "match_value",
                            name="uq_udp_exceptions_profile_match"),
        sa.ForeignKeyConstraint(["profile_id"], ["config_profiles.id"],
                                name="fk_udp_exceptions_profile_id",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_udp_exceptions_profile_enabled_sort", "udp_exceptions",
                    ["profile_id", "enabled", "sort_order"])


def downgrade() -> None:
    op.drop_index("ix_udp_exceptions_profile_enabled_sort", table_name="udp_exceptions")
    op.drop_table("udp_exceptions")
