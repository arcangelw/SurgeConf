"""订阅拉取与节点解析服务"""

import re
import base64
import httpx
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models import Subscription, ProxyNode


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
    except Exception:
        pass

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
            decoded = base64.b64decode(encoded + "==").decode()
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
        decoded = base64.b64decode(rest + "==").decode()
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
        config = f"{name} = tuic, {host}, {port}, uuid={uuid}, password={password}, sni={sni}"

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


async def fetch_subscription(sub: Subscription) -> list[dict]:
    """拉取订阅链接并解析节点"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(sub.url)
        resp.raise_for_status()
        return parse_subscription_content(resp.text)


async def sync_subscription(db: AsyncSession, sub_id: int) -> dict:
    """同步单个订阅：拉取、解析、存入数据库"""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        return {"error": "订阅不存在"}

    try:
        nodes = await fetch_subscription(sub)

        # 删除该订阅的旧节点
        await db.execute(delete(ProxyNode).where(ProxyNode.subscription_id == sub_id))

        # 插入新节点
        for node_data in nodes:
            node = ProxyNode(
                name=node_data["name"],
                subscription_id=sub_id,
                node_type=node_data["node_type"],
                server=node_data["server"],
                port=node_data["port"],
                config=node_data["config"],
                enabled=True,
            )
            db.add(node)

        sub.last_update = datetime.now()
        await db.commit()

        return {"subscription": sub.name, "node_count": len(nodes)}
    except Exception as e:
        await db.rollback()
        return {"error": str(e)}


async def sync_all_subscriptions(db: AsyncSession) -> list[dict]:
    """同步所有启用的订阅"""
    result = await db.execute(
        select(Subscription).where(Subscription.enabled == True)
    )
    subs = result.scalars().all()
    results = []
    for sub in subs:
        r = await sync_subscription(db, sub.id)
        results.append(r)
    return results


def auto_detect_region(name: str, region_groups: list[dict]) -> str | None:
    """根据节点名称自动检测地区"""
    for group in region_groups:
        regex = group.get("filter_regex")
        if regex and re.search(regex, name):
            return group["name"]
    return None
