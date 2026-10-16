"""rule_sources 增加结构化 ip_rules 列并收敛 sort_order 非空

IP 类规则集的 no-resolve 判定从 URL 关键词猜测改为结构化字段声明：
新增非空布尔列 ip_rules（默认 0）。同时把 sort_order 收敛为非空列：
存量 NULL 先规范为 0（SQLite ASC 中 NULL 本就排在最前，语义不变），
再 batch alter 为 NOT NULL。存量行的内置 IP 源回填由业务数据迁移
rule_source_ip_flags_v1 按稳定内置签名 (name + url + rule_type) 完成，
本迁移只负责 schema 推进。

downgrade 可逆：恢复 sort_order 可空、删除 ip_rules 列，并删除
rule_source_ip_flags_v1 业务迁移记账（data_migration_records 表存在时），
使 downgrade → upgrade 后回填迁移可以重新执行。

Revision ID: 0004_rule_source_ip_rules
Revises: 0003_profile_targets
Create Date: 2026-10-16

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_rule_source_ip_rules"
down_revision = "0003_profile_targets"
branch_labels = None
depends_on = None

# data_migration_records 不在 Alembic 管辖（由运行时 _sync_schema 补建），
# 表不存在时 DELETE 通过 EXISTS 子查询静默 no-op
_DELETE_IP_FLAGS_RECORD = (
    "DELETE FROM data_migration_records WHERE key = 'rule_source_ip_flags_v1' "
    "AND EXISTS (SELECT 1 FROM sqlite_master "
    "WHERE type = 'table' AND name = 'data_migration_records')")


def upgrade() -> None:
    # 存量 NULL sort_order 收敛为 0（NULL 在 SQLite ASC 排序中最小、本就排最前，
    # 收敛不改变生成顺序语义）
    op.execute("UPDATE rule_sources SET sort_order = 0 WHERE sort_order IS NULL")
    with op.batch_alter_table("rule_sources") as batch_op:
        batch_op.add_column(sa.Column("ip_rules", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.alter_column("sort_order", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    # 删除 ip_flags 回填记账：downgrade → upgrade 后迁移可重新回填；
    # order / retire 迁移记账不随本列删除，保持其语义独立
    op.execute(_DELETE_IP_FLAGS_RECORD)
    with op.batch_alter_table("rule_sources") as batch_op:
        batch_op.drop_column("ip_rules")
        batch_op.alter_column("sort_order", existing_type=sa.Integer(), nullable=True)
