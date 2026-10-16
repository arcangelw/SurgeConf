"""订阅拉取与节点解析服务"""

import re
import base64
import logging
import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models import Subscription, ProxyNode
from app.services.node_identity import stable_node_key, NodeIdentityError

logger = logging.getLogger(__name__)


# 节点名称中需要过滤的关键词
FILTER_KEYWORDS = re.compile(r"Remain|Expired|官网|如需|套餐|去除|剩余|距离|Reset|重置|流量")


def _decode_subscription(content: str) -> list[str]:
    """解码订阅内容，支持 base64 和纯文本格式"""
    content = content.strip()
    try:
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        if any(line.startswith(("ss://", "vmess://", "trojan://", "hysteria2://", "tuic://", "snell://")) for line in decoded.split("\n")[:5]):
            return [line.strip() for line in decoded.split("\n") if line.strip()]
    except Exception:
        pass
    return [line.strip() for line in content.split("\n") if line.strip()]


def _parse_surge_line(line: str) -> dict | None:
    """解析 Surge 格式节点行: Name = type, server, port, ..."""
    if "=" not in line:
        return None
    name, rest = line.split("=", 1)
    name = name.strip()
    rest = rest.strip()

    parts = [p.strip() for p in rest.split(",")]
    if len(parts) < 2:
        return None

    node_type = parts[0].lower()
    server = parts[1] if len(parts) > 1 else ""
    port = 0
    try:
        port = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        pass

    if node_type not in ("ss", "vmess", "trojan", "hysteria2", "tuic", "snell",
                          "http", "socks5", "socks5-tls", "ssh", "wireguard", "anytls"):
        return None

    return {
        "name": name,
        "node_type": node_type,
        "server": server,
        "port": port,
        "config": line.strip(),
    }


def _parse_uri(uri: str) -> dict | None:
    """解析 URI 格式节点 (ss://, vmess://, trojan:// 等)，转换为 Surge 格式"""
    if not uri or "://" not in uri:
        return None

    scheme, rest = uri.split("://", 1)
    scheme = scheme.lower()

    # 提取 fragment 作为名称
    name = ""
    if "#" in rest:
        rest, name_part = rest.rsplit("#", 1)
        name = _urldecode(name_part)
    if not name:
        name = f"{scheme}-node"

    try:
        if scheme == "ss":
            return _parse_ss(rest, name)
        elif scheme == "vmess":
            return _parse_vmess(rest, name)
        elif scheme == "trojan":
            return _parse_trojan(rest, name)
        elif scheme == "hysteria2" or scheme == "hy2":
            return _parse_hysteria2(rest, name)
        elif scheme == "tuic":
            return _parse_tuic(rest, name)
    except Exception as e:
        logger.debug("解析节点失败 [%s]: %s", name, e)

    return None


def _urldecode(s: str) -> str:
    import urllib.parse
    return urllib.parse.unquote(s)


def _parse_host_port(rest: str) -> tuple[str, int]:
    """从 host:port 或 host:port?params 格式中提取 host 和 port"""
    host_port = rest.split("?")[0].split("/")[0]
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        return host, int(port_str)
    return host_port, 443


def _parse_params(rest: str) -> dict:
    """从 URI 中提取查询参数"""
    if "?" not in rest:
        return {}
    params_str = rest.split("?", 1)[1].split("#")[0]
    params = {}
    for pair in params_str.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = _urldecode(v)
    return params


def _parse_ss(rest: str, name: str) -> dict | None:
    """解析 Shadowsocks URI"""
    try:
        if "@" in rest:
            encoded, host_port = rest.split("@", 1)
            padding = 4 - len(encoded) % 4
            if padding != 4:
                encoded += "=" * padding
            decoded = base64.b64decode(encoded).decode()
            method, password = decoded.split(":", 1)
        else:
            return None

        host, port = _parse_host_port(host_port)
        params = _parse_params(rest)

        config = f"{name} = ss, {host}, {port}, encrypt-method={method}, password={password}"
        if params.get("obfs"):
            config += f", obfs={params['obfs']}"
        if params.get("obfs-host"):
            config += f", obfs-host={params['obfs-host']}"

        return {"name": name, "node_type": "ss", "server": host, "port": port, "config": config}
    except Exception:
        return None


def _parse_vmess(rest: str, name: str) -> dict | None:
    """解析 VMess URI"""
    try:
        padding = 4 - len(rest) % 4
        if padding != 4:
            rest_padded = rest + "=" * padding
        else:
            rest_padded = rest
        decoded = base64.b64decode(rest_padded).decode()
        import json
        info = json.loads(decoded)

        host = info.get("add", "")
        port = int(info.get("port", 443))
        uuid = info.get("id", "")
        net = info.get("net", "tcp")

        config = f"{name} = vmess, {host}, {port}, username={uuid}"
        if net == "ws":
            config += ", ws=true"
            path = info.get("path", "")
            if path:
                config += f", ws-path={path}"
            host_header = info.get("host", "")
            if host_header:
                config += f", ws-headers=Host:{host_header}"
        elif net == "grpc":
            config += f", grpc-service-name={info.get('path', '')}"

        tls = info.get("tls", "")
        if tls == "tls":
            sni = info.get("sni", host)
            config += f", tls=true, sni={sni}"

        return {"name": name, "node_type": "vmess", "server": host, "port": port, "config": config}
    except Exception:
        return None


def _parse_trojan(rest: str, name: str) -> dict | None:
    """解析 Trojan URI"""
    try:
        password, host_port_part = rest.split("@", 1)
        host, port = _parse_host_port(host_port_part)
        params = _parse_params(rest)

        sni = params.get("sni", host)
        config = f"{name} = trojan, {host}, {port}, password={password}, sni={sni}"

        return {"name": name, "node_type": "trojan", "server": host, "port": port, "config": config}
    except Exception:
        return None


def _parse_hysteria2(rest: str, name: str) -> dict | None:
    """解析 Hysteria2 URI"""
    try:
        password, host_port_part = rest.split("@", 1)
        host, port = _parse_host_port(host_port_part)
        params = _parse_params(rest)
        sni = params.get("sni", host)
        insecure = params.get("insecure", "0")

        config = f"{name} = hysteria2, {host}, {port}, password={password}, sni={sni}"
        if insecure == "1":
            config += ", skip-cert-verify=true"

        return {"name": name, "node_type": "hysteria2", "server": host, "port": port, "config": config}
    except Exception:
        return None


def _parse_tuic(rest: str, name: str) -> dict | None:
    """解析 TUIC URI"""
    try:
        uuid_password, host_port_part = rest.split("@", 1)
        host, port = _parse_host_port(host_port_part)
        params = _parse_params(rest)

        if ":" in uuid_password:
            uuid, password = uuid_password.split(":", 1)
        else:
            uuid = uuid_password
            password = ""

        sni = params.get("sni", host)
        config = f"{name} = tuic, {host}, {port}, uuid={uuid}, sni={sni}"
        if password:
            config += f", password={password}"

        return {"name": name, "node_type": "tuic", "server": host, "port": port, "config": config}
    except Exception:
        return None


def parse_subscription_content(content: str) -> list[dict]:
    """解析订阅内容，返回节点列表"""
    lines = _decode_subscription(content)
    nodes = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        if FILTER_KEYWORDS.search(line):
            continue

        node = None
        if "://" in line:
            node = _parse_uri(line)
        elif "=" in line:
            node = _parse_surge_line(line)

        if node and node["name"]:
            nodes.append(node)

    return nodes


def _validate_url(url: str) -> str:
    """验证 URL 防止 SSRF，只允许 http/https 协议和常见代理订阅域名"""
    import urllib.parse
    import ipaddress

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"不支持的协议: {parsed.scheme}")
    host = parsed.hostname or ""

    # 禁止内网域名
    if host in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        raise ValueError("不允许的地址: 内网地址")

    # 尝试 IP 检测
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url  # 域名而非 IP，放行

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise ValueError("不允许的地址: 内网/保留地址")

    return url


async def fetch_subscription(url: str) -> list[dict]:
    """拉取订阅链接并解析节点（纯网络阶段，不持有任何数据库事务）"""
    _validate_url(url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return parse_subscription_content(resp.text)


# ===== 同步结果安全 DTO 与错误分类 =====

class _EmptySubscriptionError(Exception):
    """拉取成功但解析不出任何节点（fail closed：不写入任何变更）"""


_ERROR_PUBLIC_MESSAGES = {
    "timeout": "订阅拉取超时，已保留上次可用节点",
    "http_status": "订阅源响应异常，已保留上次可用节点",
    "invalid_content": "订阅内容无法解析，已保留上次可用节点",
    "network_error": "订阅源网络错误，已保留上次可用节点",
    "internal_error": "同步发生内部错误，已保留上次可用节点",
}


def safe_sync_error(exc: BaseException) -> tuple[str, str]:
    """把同步异常映射为固定错误码与公开消息。

    错误码 allowlist：timeout / http_status / invalid_content /
    network_error / internal_error。消息为静态文本，绝不拼接异常原文、
    URL、主机、配置或响应体。分类顺序敏感：Timeout 是 RequestError
    子类，必须先于 RequestError 判断。
    """
    if isinstance(exc, httpx.TimeoutException):
        code = "timeout"
    elif isinstance(exc, httpx.HTTPStatusError):
        code = "http_status"
    elif isinstance(exc, httpx.RequestError):
        code = "network_error"
    elif isinstance(exc, (_EmptySubscriptionError, NodeIdentityError, ValueError)):
        # ValueError 覆盖 URL 校验拒绝、解码与 JSON 解析错误
        code = "invalid_content"
    else:
        code = "internal_error"
    return code, _ERROR_PUBLIC_MESSAGES[code]


@dataclass(frozen=True)
class SyncResult:
    """同步结果安全 DTO：对外序列化只有这些字段，无 URL/配置/异常细节。

    计数语义（报告与测试保持一致）：
    - 成功：node_count 为去重后节点数；inserted = 新 stable_key 建立的
      节点数；updated = 匹配到既有 stable_key 的处理数（即使字段值未变
      也计数）；removed = 本次结果中缺失而被删除的旧节点数。
    - 失败：零节点写入，inserted/updated/removed 全 0；node_count 保持
      上次成功值 last_node_count（从未成功过则为 None）。
    """

    subscription_id: int
    success: bool
    node_count: int | None
    inserted: int
    updated: int
    removed: int
    error_code: str | None
    public_message: str

    def to_dict(self) -> dict:
        """对外序列化（仅安全字段）"""
        return {
            "subscription_id": self.subscription_id,
            "success": self.success,
            "node_count": self.node_count,
            "inserted": self.inserted,
            "updated": self.updated,
            "removed": self.removed,
            "error_code": self.error_code,
            "public_message": self.public_message,
        }


_SYNC_INTERVAL_MIN = 300
_SYNC_INTERVAL_MAX = 604800
_SYNC_INTERVAL_DEFAULT = 86400
_FAILURE_RETRY_CAP = 900


def _normalized_interval(interval: int | None) -> int:
    """与 0006 迁移同口径：范围外/NULL 归一为默认 86400"""
    if interval is None or not (_SYNC_INTERVAL_MIN <= interval <= _SYNC_INTERVAL_MAX):
        return _SYNC_INTERVAL_DEFAULT
    return interval


def _utc_naive(now: datetime | None = None) -> datetime:
    """统一为 UTC naive（库内时间口径）。

    None → 当前 UTC；aware 只接受零 UTC 偏移（非 UTC 时区属调用方
    编程错误，直接抛 ValueError，不进入同步流程）；naive 按库内口径
    视为 UTC。
    """
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    offset = now.utcoffset()
    if offset is not None and offset != timedelta(0):
        raise ValueError("now 必须为 UTC（aware 零偏移）或 naive UTC")
    return now.replace(tzinfo=None) if offset is not None else now


def _prepare_incoming(nodes: list[dict]) -> list[tuple[str, dict]]:
    """解析结果 → (stable_key, node_data)；同 key 保留首个（确定性去重）"""
    prepared: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for node_data in nodes:
        key = stable_node_key(node_data)
        if key in seen:
            continue
        seen.add(key)
        prepared.append((key, node_data))
    return prepared


async def _record_failed_attempt(db: AsyncSession, sub_id: int,
                                  now: datetime, code: str) -> None:
    """失败状态短事务：只更新 attempt/error_code/next。

    不触碰 last_success_at/last_node_count 与任何节点；enabled+auto 时
    next = now + min(interval, 900)，否则 NULL。订阅并发删除时静默跳过；
    本事务自身失败仅记录错误码。
    """
    try:
        sub = (await db.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )).scalar_one_or_none()
        if sub is None:
            await db.rollback()
            return
        sub.last_attempt_at = now
        sub.last_error_code = code
        if sub.enabled and sub.auto_update:
            delay = min(_normalized_interval(sub.update_interval), _FAILURE_RETRY_CAP)
            sub.next_update_at = now + timedelta(seconds=delay)
        else:
            sub.next_update_at = None
        await db.commit()
    except Exception:
        await db.rollback()
        logger.error("subscription %s failure state write failed: code=%s", sub_id, code)


async def sync_subscription(db: AsyncSession, sub_id: int,
                            now: datetime | None = None) -> SyncResult | None:
    """同步单个订阅：拉取/解析/去重在事务外，成功后单事务 upsert。

    订阅不存在（含同步期间被并发删除）返回 None，由调用方决定 404。

    成功写事务：重查订阅与现有节点，按 stable_key 匹配——匹配项只更新
    provider 字段（name/node_type/server/port/config/region_auto），保留
    id、region_manual、enabled、pinned_services、created_at 等人工状态；
    新节点 enabled=True、人工字段为空；缺失旧节点删除；更新
    attempt/success/node_count、清空 error_code；一次 commit。

    任何失败（拉取/解析/空结果/写事务异常）：节点零写入，独立短事务只
    记录 attempt/error_code/next，保留最后可用节点与既有成功状态。
    """
    now = _utc_naive(now)

    # 阶段 1：短只读事务取订阅快照（普通值，不携带 ORM 对象跨事务）
    row = (await db.execute(
        select(Subscription.url, Subscription.last_node_count)
        .where(Subscription.id == sub_id)
    )).first()
    await db.commit()
    if row is None:
        return None
    url, last_node_count = row

    # 阶段 2：网络拉取、解析、稳定键计算、同批去重（全部在写事务外）
    try:
        prepared = _prepare_incoming(await fetch_subscription(url))
        if not prepared:
            raise _EmptySubscriptionError()
    except Exception as exc:
        code, message = safe_sync_error(exc)
        logger.error("subscription %s sync failed: code=%s", sub_id, code)
        await _record_failed_attempt(db, sub_id, now, code)
        return SyncResult(sub_id, False, last_node_count, 0, 0, 0, code, message)

    # 阶段 3：单一写事务 upsert
    try:
        sub = (await db.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )).scalar_one_or_none()
        if sub is None:
            await db.rollback()
            return None
        existing = {
            node.stable_key: node
            for node in (await db.execute(
                select(ProxyNode).where(ProxyNode.subscription_id == sub_id)
            )).scalars()
        }
        incoming_keys = {key for key, _ in prepared}
        inserted = updated = removed = 0
        for key, node_data in prepared:
            node = existing.get(key)
            if node is not None:
                # 只覆盖 provider 字段；人工字段原样保留
                node.name = node_data["name"]
                node.node_type = node_data["node_type"]
                node.server = node_data["server"]
                node.port = node_data["port"]
                node.config = node_data["config"]
                node.region_auto = node_data.get("region_auto")
                updated += 1
            else:
                db.add(ProxyNode(
                    name=node_data["name"],
                    subscription_id=sub_id,
                    node_type=node_data["node_type"],
                    server=node_data["server"],
                    port=node_data["port"],
                    config=node_data["config"],
                    stable_key=key,
                    region_auto=node_data.get("region_auto"),
                    enabled=True,
                ))
                inserted += 1
        stale_ids = [node.id for key, node in existing.items()
                     if key not in incoming_keys]
        if stale_ids:
            await db.execute(delete(ProxyNode).where(ProxyNode.id.in_(stale_ids)))
            removed = len(stale_ids)
        sub.last_attempt_at = now
        sub.last_success_at = now
        sub.last_error_code = None
        sub.last_node_count = len(prepared)
        if sub.enabled and sub.auto_update:
            sub.next_update_at = now + timedelta(
                seconds=_normalized_interval(sub.update_interval))
        else:
            sub.next_update_at = None
        await db.commit()
        return SyncResult(sub_id, True, len(prepared), inserted, updated,
                          removed, None, "同步成功")
    except Exception as exc:
        await db.rollback()
        code, message = safe_sync_error(exc)
        logger.error("subscription %s sync write failed: code=%s", sub_id, code)
        await _record_failed_attempt(db, sub_id, now, code)
        return SyncResult(sub_id, False, last_node_count, 0, 0, 0, code, message)


async def sync_all_subscriptions(db: AsyncSession) -> list[SyncResult]:
    """同步所有启用订阅：逐项安全 DTO，单项失败不中断；并发删除项跳过"""
    ids = (await db.execute(
        select(Subscription.id).where(Subscription.enabled == True)  # noqa: E712
    )).scalars().all()
    await db.commit()
    results = []
    for sub_id in ids:
        result = await sync_subscription(db, sub_id)
        if result is not None:
            results.append(result)
    return results


async def cleanup_orphan_nodes(db: AsyncSession) -> int:
    """清理孤儿节点：删除 subscription_id 指向已不存在订阅的节点。

    用于修复旧版本删除订阅时未级联删除节点而残留在库中的数据，
    升级后在启动流程（seed_defaults）中调用一次即可完成存量修复。
    subscription_id 为 NULL 的手动节点不受影响。
    返回被删除的节点数量。
    """
    result = await db.execute(select(Subscription.id))
    valid_ids = [row[0] for row in result.all()]

    if valid_ids:
        stmt = delete(ProxyNode).where(
            ProxyNode.subscription_id.isnot(None),
            ProxyNode.subscription_id.notin_(valid_ids),
        )
    else:
        # 没有任何订阅时，所有带 subscription_id 的节点都是孤儿
        stmt = delete(ProxyNode).where(ProxyNode.subscription_id.isnot(None))

    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


def auto_detect_region(name: str, region_groups: list[dict]) -> str | None:
    """根据节点名称自动检测地区"""
    for group in region_groups:
        regex = group.get("filter_regex")
        if regex and re.search(regex, name):
            return group["name"]
    return None
