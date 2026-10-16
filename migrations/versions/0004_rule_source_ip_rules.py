"""rule_sources 增加结构化 ip_rules 列

IP 类规则集的 no-resolve 判定从 URL 关键词猜测改为结构化字段声明：
新增非空布尔列 ip_rules（默认 0）。存量行的内置 IP 源回填由业务数据迁移
rule_source_ip_flags_v1 按稳定内置签名 (name + url + rule_type) 完成，
本迁移只负责 schema 推进；downgrade 删除该列，数据行其余部分保留。

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


def upgrade() -> None:
    with op.batch_alter_table("rule_sources") as batch_op:
        batch_op.add_column(sa.Column("ip_rules", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("rule_sources") as batch_op:
        batch_op.drop_column("ip_rules")
