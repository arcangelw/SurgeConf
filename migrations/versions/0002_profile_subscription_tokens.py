"""config_profiles 增加逐档案订阅令牌哈希列

只为愿意启用 URL 订阅的档案保存 ``subscription_token_hash``（SHA-256 hex，
nullable）。迁移不为任何既有档案自动生成令牌——旧档案保持 NULL（未配置），
由管理员在 UI 中首次生成；明文令牌只在签发/轮换响应中出现一次。

Revision ID: 0002_profile_subscription_t
Revises: 0001_current_schema
Create Date: 2026-10-16

"""

import sqlalchemy as sa
from alembic import op

revision = "0002_profile_subscription_tokens"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "config_profiles",
        sa.Column("subscription_token_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("config_profiles", "subscription_token_hash")
