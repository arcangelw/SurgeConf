"""config_profiles 增加目标设备与 UDP 安全模式列

SQLite 无 ALTER ADD CONSTRAINT，三条 CHECK（目标集合 / 模式集合 /
ios_mobile 不允许 scoped）与两个非空列在同一次 batch 表重建中落盘；
旧行由列默认值确定为 mac_workstation/scoped。downgrade 逆序删除，
数据行全部保留。

Revision ID: 0003_profile_targets
Revises: 0002_profile_subscription_tokens
Create Date: 2026-10-16

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_profile_targets"
down_revision = "0002_profile_subscription_tokens"
branch_labels = None
depends_on = None

_TARGETS = "('mac_workstation', 'mac_gateway', 'ios_mobile')"
_MODES = "('strict', 'scoped', 'compatible')"


def upgrade() -> None:
    with op.batch_alter_table("config_profiles") as batch_op:
        batch_op.add_column(sa.Column("target", sa.String(length=20), nullable=False,
                                      server_default="mac_workstation"))
        batch_op.add_column(sa.Column("udp_security_mode", sa.String(length=20), nullable=False,
                                      server_default="scoped"))
        batch_op.create_check_constraint(
            "ck_config_profiles_target", f"target IN {_TARGETS}")
        batch_op.create_check_constraint(
            "ck_config_profiles_udp_security_mode", f"udp_security_mode IN {_MODES}")
        batch_op.create_check_constraint(
            "ck_config_profiles_target_udp_mode",
            "NOT (target = 'ios_mobile' AND udp_security_mode = 'scoped')")


def downgrade() -> None:
    with op.batch_alter_table("config_profiles") as batch_op:
        batch_op.drop_constraint("ck_config_profiles_target_udp_mode", type_="check")
        batch_op.drop_constraint("ck_config_profiles_udp_security_mode", type_="check")
        batch_op.drop_constraint("ck_config_profiles_target", type_="check")
        batch_op.drop_column("udp_security_mode")
        batch_op.drop_column("target")
