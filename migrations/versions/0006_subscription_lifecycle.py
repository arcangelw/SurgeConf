"""订阅生命周期观测与节点稳定身份

subscriptions：``last_update`` 拆分为 ``last_attempt_at`` /
``last_success_at``，新增 ``next_update_at`` / ``last_error_code`` /
``last_node_count``；旧 ``last_update``（成功语义）复制到 attempt 与
success 两个字段。范围外 ``update_interval``（NULL 或 <300 / >604800）
归一为 86400。enabled+auto_update 订阅的 ``next_update_at`` 取上次成功
加归一间隔；从未同步过取 ``created_at``；其余为 NULL。

proxy_nodes：新增 ``stable_key``（连接规范化 SHA-256 身份）。手动节点
（subscription_id 为 NULL）保持 NULL。升级在建唯一索引前为现有订阅节点
计算 key 并按"存在手动地区 → 钉选非空 → 被人工禁用 → id 较小"的
字典序选 survivor，合并首个非空 region_manual、钉选并集（确定性排序）
与最严格的 enabled=false，随后删除其余重复行——被去重删除的行在
downgrade 中不可恢复。订阅节点 config 非法时升级失败（staging 上原子
回滚，源库不变）。

结构：``ck_proxy_nodes_subscription_stable_key``（subscription_id 非
NULL 则 stable_key 必须 非 NULL）与部分唯一索引
``uq_proxy_nodes_subscription_stable_key``（仅对非 NULL stable_key）。

本文件内嵌规范化算法的冻结副本与 golden vector，绝不 import
app.services.node_identity——未来运行时算法演进不改变历史迁移结果。
日志只输出冲突计数，不含节点/订阅/URL/config 内容。

Revision ID: 0006_subscription_lifecycle
Revises: 0005_udp_exceptions
Create Date: 2026-10-17

"""

import hashlib
import json
import logging
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_subscription_lifecycle"
down_revision = "0005_udp_exceptions"
branch_labels = None
depends_on = None

logger = logging.getLogger("surgeconf.migration.subscription_lifecycle")

_DEFAULT_INTERVAL = 86400
_MIN_INTERVAL = 300
_MAX_INTERVAL = 604800
_MAX_PORT = 65535

# ===== 冻结的连接规范化算法（与 app/services/node_identity.py 的 0006 版本
# 字节级一致；golden vector 自校验防漂移）。静态异常文本，绝不回显 config。


class _FrozenIdentityError(RuntimeError):
    """config 无法规范化（静态消息，不含 config 内容）"""


def _frozen_split_tokens(text):
    tokens, buf, in_quotes = [], [], False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            buf.append(char)
        elif char == "," and not in_quotes:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if in_quotes:
        raise _FrozenIdentityError("节点连接参数包含未闭合引号")
    tokens.append("".join(buf))
    return tokens


def _frozen_unquote(token):
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token


def _frozen_token(raw):
    token = _frozen_unquote(raw.strip())
    if not token:
        raise _FrozenIdentityError("节点连接包含空参数")
    return token


def _frozen_port(raw):
    if not raw:
        raise _FrozenIdentityError("节点连接缺少端口")
    try:
        port = int(raw, 10)
    except ValueError:
        raise _FrozenIdentityError("节点连接端口不是合法整数") from None
    if not 1 <= port <= _MAX_PORT:
        raise _FrozenIdentityError("节点连接端口超出有效范围")
    return str(port)


def _frozen_key(config):
    if not isinstance(config, str) or not config.strip():
        raise _FrozenIdentityError("节点缺少连接配置")
    if any(char in config for char in ("\r", "\n", "\0")):
        raise _FrozenIdentityError("节点连接配置包含非法控制字符")
    if "=" not in config:
        raise _FrozenIdentityError("节点连接配置缺少连接表达")
    _, _, expression = config.partition("=")
    if not expression.strip():
        raise _FrozenIdentityError("节点连接配置缺少连接表达")
    tokens = [_frozen_token(t) for t in _frozen_split_tokens(expression)]
    if len(tokens) < 3:
        raise _FrozenIdentityError("节点连接参数不完整")
    protocol = tokens[0]
    if any(ch.isspace() for ch in protocol):
        raise _FrozenIdentityError("节点协议名格式非法")
    host = tokens[1]
    if any(ch.isspace() for ch in host):
        raise _FrozenIdentityError("节点主机名格式非法")
    port = _frozen_port(tokens[2])
    positional, key_values = [], []
    for token in tokens[3:]:
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if not key or any(ch.isspace() for ch in key):
                raise _FrozenIdentityError("节点参数名格式非法")
            key_values.append((key, _frozen_token(value)))
        else:
            positional.append(token)
    parts = [protocol.lower(), host.lower(), port, *positional,
             *(f"{k}={v}" for k, v in sorted(key_values))]
    canonical = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# golden vectors：算法漂移（与 0006 冻结版不一致）时升级直接失败
_GOLDEN_VECTORS = (
    ("US A = trojan, edge.example.com, 443, password=synthetic",
     "US Renamed = TROJAN , EDGE.Example.COM , 0443 , password=synthetic",
     "905a00f8e51b5ad424b686a4dcfc4b693ff2cfd9ca59b2e9ca6f95cea4e63f13"),
    ("JP = ss, relay.example.jp, 8388, aes-256-gcm, password=alpha",
     "JP 2 = ss, relay.example.jp, 8388, password=alpha, aes-256-gcm",
     "2a9e16a3d2d8cc906cdcb2b92b94beded9a962953e3c773b5ddaeef4b0a1f865"),
)


def _verify_frozen_algorithm() -> None:
    for first, second, expected in _GOLDEN_VECTORS:
        if _frozen_key(first) != expected or _frozen_key(second) != expected:
            raise RuntimeError("0006 canonicalizer drifted from golden vectors")


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _normalized_interval(raw) -> int:
    if raw is None or not (_MIN_INTERVAL <= raw <= _MAX_INTERVAL):
        return _DEFAULT_INTERVAL
    return raw


def _survivor_rank(row) -> tuple:
    """survivor 优先级（字典序）：手动地区非空 → 钉选非空 → 被禁用 → id 小"""
    region = (row["region_manual"] or "").strip()
    pins_raw = row["pinned_services"]
    pins = json.loads(pins_raw) if isinstance(pins_raw, str) and pins_raw else (pins_raw or [])
    if not isinstance(pins, list):
        raise RuntimeError("订阅节点钉选数据损坏")
    return (0 if region else 1,
            0 if pins else 1,
            0 if not row["enabled"] else 1,
            row["id"])


def _merged_pins(rows) -> str | None:
    """钉选并集（确定性排序）；全部为空时返回 None"""
    union: set = set()
    for row in rows:
        pins_raw = row["pinned_services"]
        pins = json.loads(pins_raw) if isinstance(pins_raw, str) and pins_raw else (pins_raw or [])
        if not isinstance(pins, list):
            raise RuntimeError("订阅节点钉选数据损坏")
        union.update(pins)
    return json.dumps(sorted(union)) if union else None


def upgrade() -> None:
    _verify_frozen_algorithm()
    bind = op.get_bind()

    # ---- subscriptions：读取旧状态（last_update 删除前） ----
    subs = bind.execute(sa.text(
        "SELECT id, enabled, auto_update, update_interval, last_update, created_at "
        "FROM subscriptions")).mappings().all()

    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_success_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("next_update_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("last_node_count", sa.Integer(), nullable=True))

    # ---- 状态回填 + interval 归一（时间保持 UTC naive，可比较） ----
    for sub in subs:
        interval = _normalized_interval(sub["update_interval"])
        last_update = _parse_ts(sub["last_update"])
        created_at = _parse_ts(sub["created_at"])
        enabled = bool(sub["enabled"])
        auto_update = bool(sub["auto_update"])
        if enabled and auto_update:
            if last_update is not None:
                next_update_at = last_update + timedelta(seconds=interval)
            else:
                next_update_at = created_at  # 从未同步：从创建时间起调度
        else:
            next_update_at = None
        bind.execute(sa.text(
            "UPDATE subscriptions SET last_attempt_at = :attempt, last_success_at = :success, "
            "next_update_at = :next_update, update_interval = :interval WHERE id = :id"),
            {"attempt": last_update, "success": last_update,
             "next_update": next_update_at, "interval": interval, "id": sub["id"]})

    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("last_update")

    # ---- proxy_nodes：先加列，回填 key 并去重，再施加约束与索引 ----
    with op.batch_alter_table("proxy_nodes") as batch:
        batch.add_column(sa.Column("stable_key", sa.String(length=64), nullable=True))

    nodes = bind.execute(sa.text(
        "SELECT id, subscription_id, config, region_manual, pinned_services, enabled "
        "FROM proxy_nodes WHERE subscription_id IS NOT NULL")).mappings().all()

    groups: dict[tuple[int, str], list] = {}
    for node in nodes:
        key = _frozen_key(node["config"])  # 非法 config → 迁移失败，源库不变
        bind.execute(sa.text(
            "UPDATE proxy_nodes SET stable_key = :key WHERE id = :id"),
            {"key": key, "id": node["id"]})
        groups.setdefault((node["subscription_id"], key), []).append(node)

    duplicate_groups = 0
    removed_rows = 0
    for (_, _), rows in groups.items():
        if len(rows) < 2:
            continue
        duplicate_groups += 1
        ordered = sorted(rows, key=_survivor_rank)
        survivor = ordered[0]
        region_manual = next((r["region_manual"] for r in ordered
                              if (r["region_manual"] or "").strip()), None)
        enabled = 0 if any(not bool(r["enabled"]) for r in ordered) else 1
        bind.execute(sa.text(
            "UPDATE proxy_nodes SET region_manual = :region, pinned_services = :pins, "
            "enabled = :enabled WHERE id = :id"),
            {"region": region_manual, "pins": _merged_pins(ordered),
             "enabled": enabled, "id": survivor["id"]})
        for loser in ordered[1:]:
            bind.execute(sa.text("DELETE FROM proxy_nodes WHERE id = :id"),
                         {"id": loser["id"]})
            removed_rows += 1
    if duplicate_groups:
        logger.info("subscription node identity merge: %d duplicate groups, %d rows removed",
                    duplicate_groups, removed_rows)

    with op.batch_alter_table("proxy_nodes") as batch:
        batch.create_check_constraint(
            "ck_proxy_nodes_subscription_stable_key",
            "subscription_id IS NULL OR stable_key IS NOT NULL")

    op.create_index("uq_proxy_nodes_subscription_stable_key", "proxy_nodes",
                    ["subscription_id", "stable_key"], unique=True,
                    sqlite_where=sa.text("stable_key IS NOT NULL"))

    # last_node_count = 去重后的每订阅节点数
    bind.execute(sa.text(
        "UPDATE subscriptions SET last_node_count = ("
        " SELECT COUNT(*) FROM proxy_nodes p "
        " WHERE p.subscription_id = subscriptions.id)"))


def downgrade() -> None:
    bind = op.get_bind()

    # last_update 从 last_success_at 恢复（NULL 保持 NULL）
    success_map = bind.execute(sa.text(
        "SELECT id, last_success_at FROM subscriptions")).mappings().all()

    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("last_update", sa.DateTime(), nullable=True))
    for row in success_map:
        bind.execute(sa.text(
            "UPDATE subscriptions SET last_update = :success WHERE id = :id"),
            {"success": row["last_success_at"], "id": row["id"]})
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("last_attempt_at")
        batch.drop_column("last_success_at")
        batch.drop_column("next_update_at")
        batch.drop_column("last_error_code")
        batch.drop_column("last_node_count")

    op.drop_index("uq_proxy_nodes_subscription_stable_key", table_name="proxy_nodes")
    with op.batch_alter_table("proxy_nodes") as batch:
        batch.drop_constraint("ck_proxy_nodes_subscription_stable_key", type_="check")
        batch.drop_column("stable_key")
