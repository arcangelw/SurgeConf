"""当前 schema 基线

显式创建 app/models.py 的全部表、索引与唯一约束（不调用
Base.metadata.create_all()），并新增 data_migration_records 记账表。
后续 schema 变更必须新增迁移版本，禁止修改本文件。

Revision ID: 0001_current_schema
Revises:
Create Date: 2026-10-16

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("tag", sa.String(length=50), nullable=True),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("auto_update", sa.Boolean(), nullable=True),
        sa.Column("update_interval", sa.Integer(), nullable=True),
        sa.Column("last_update", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "proxy_nodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("node_type", sa.String(length=50), nullable=False),
        sa.Column("server", sa.String(length=500), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("region_auto", sa.String(length=100), nullable=True),
        sa.Column("region_manual", sa.String(length=100), nullable=True),
        sa.Column("pinned_services", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "region_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column("filter_regex", sa.String(length=500), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("auto_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=True),
        sa.Column("manual_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "service_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column("group_type", sa.String(length=20), nullable=True),
        sa.Column("policies", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "config_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=True),
        sa.Column("locale", sa.String(length=10), nullable=True),
        sa.Column("final_action", sa.String(length=100), nullable=True),
        sa.Column("general", sa.JSON(), nullable=True),
        sa.Column("dns", sa.JSON(), nullable=True),
        sa.Column("url_rewrites", sa.JSON(), nullable=True),
        sa.Column("header_rewrites", sa.JSON(), nullable=True),
        sa.Column("mitm", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "custom_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("rule_text", sa.String(length=500), nullable=False),
        sa.Column("position", sa.String(length=20), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("comment", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "host_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("domain", sa.String(length=500), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("skip_proxy", sa.Boolean(), server_default=sa.text("1"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rule_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("rule_type", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        # 业务数据迁移记账（app/services/data_migrations.py）。
        # 与 _ensure_data_migration_records() 中的手写 DDL 保持一致。
        "data_migration_records",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("applied_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("data_migration_records")
    op.drop_table("system_config")
    op.drop_table("rule_sources")
    op.drop_table("host_mappings")
    op.drop_table("custom_rules")
    op.drop_table("config_profiles")
    op.drop_table("service_groups")
    op.drop_table("region_groups")
    op.drop_table("proxy_nodes")
    op.drop_table("subscriptions")
