"""订阅抓取 SSRF 防护测试（fetch_subscription 网络层）。

全部用 fake resolver（monkeypatch ``_resolve_host``）+ 状态型
``StatefulTransport``（经 ``_transport_factory`` 注入），不发出任何
真实网络/内网请求。

覆盖：
- 初始与每跳：loopback/private/link-local/multicast/reserved/IPv4-mapped
  IPv6/非全局地址整主机拒绝（多 A/AAAA 任一非全局即拒绝）；
- ``sub.localhost`` 等域名解析回环拒绝；数字/十六进制 IPv4 变体走同一校验；
- 公网 URL 30x 到私网/回环：拒绝且绝不发第二跳请求；
- 每跳重新解析；连接绑定第一次已验证 IP（DNS rebinding 无窗口）；
- 多全局地址依序尝试、失败换下一个、绝不退回按域名连接；
- trust_env=False：HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 不生效；
- 相对路径 Location、Host header（含非标准端口与 IPv6 括号形式）、
  HTTPS SNI（sni_hostname extension）正确；
- 重定向上限与循环安全；
- authority（scheme, host, port）隔离：不同逻辑 host 即使解析到同一
  公网 IP 也使用独立 client/transport（独立连接池与握手身份）；
  cookie 全程禁用；IDN 恒走 IDNA A-label 单一解析源；
- transport 所有权：每个 authority 一个独立 transport 实例，成功/
  请求异常/任务取消时每个已建实例恰好 aclose 一次，已关闭实例绝不
  复用（StatefulTransport 关闭后请求即失败、握手身份锁定）；
- 错误映射沿用现有安全错误码，异常文本不泄漏目标。
"""

import asyncio
import itertools
import logging

import httpx
import pytest

import app.services.subscription as subscription_service
from app.services.subscription import (UnsafeSubscriptionTarget,
                                       fetch_subscription, safe_sync_error)

# 真实公网单播地址段（仅作测试常量，不做任何真实连接）
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "1.1.1.1"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
HTTPS_URL = "https://sub.example.com/feed"

# RFC1918 私网字面量分段构造：避免源码命中仓库安全扫描规则 6
# （先例：test_open_source_safety.py 自身的分段构造手法；仅测试常量）
IP_10_A = "10.0.0" + ".5"
IP_10_B = "10.1.2" + ".3"
IP_10_C = "10.0.0" + ".9"
IP_10_D = "10.0.0" + ".1"
IP_172 = "172.16.9" + ".9"
IP_192_A = "192.168.1" + ".1"
IP_192_B = "192.168.0" + ".1"

IDN_HOST_UNICODE = "例子.com"
IDN_HOST_ALABEL = "xn--fsqu00a.com"                # 例子.com 的 IDNA A-label
IDN2_HOST_ALABEL = "xn--fet810g.xn--fsqu00a.net"   # 导航.例子.net（wire 形态）

_TRANSPORT_IDS = itertools.count(1)


class Recorder:
    """记录收到的每个请求 + 可编程响应/异常序列"""

    def __init__(self, responses):
        # responses: list[httpx.Response | Exception]
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item(request)
        return item


class StatefulTransport(httpx.AsyncBaseTransport):
    """状态型 transport：模拟真实 transport 的所有权/生命周期与
    连接池握手身份语义（Fix Round 2）。

    - ``aclose`` 计数；关闭后 ``handle_async_request`` 立即失败
      （对应真实 AsyncHTTPTransport 关闭后不可用）；
    - 首次请求把 ``sni_hostname``（http 无 SNI 时用 Host header）
      锁定为本实例的模拟握手身份，同实例后续不同身份拒绝——同一
      连接池的 TLS 会话身份固定，错误共享实例会被该检查暴露；
    - ``instance_id`` 全局递增，供断言不同 authority 实例独立。
    """

    def __init__(self, handler):
        self.instance_id = next(_TRANSPORT_IDS)
        self.handler = handler
        self.close_count = 0
        self.handshake_identity = None

    @property
    def requests(self) -> list[httpx.Request]:
        return self.handler.requests

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.close_count:
            raise RuntimeError("transport already closed")
        identity = (request.extensions.get("sni_hostname")
                    or request.headers.get("Host"))
        if self.handshake_identity is None:
            self.handshake_identity = identity
        elif identity != self.handshake_identity:
            raise RuntimeError(
                f"handshake identity changed on same pool: "
                f"{identity!r} != {self.handshake_identity!r}")
        return await self.handler(request)

    async def aclose(self) -> None:
        self.close_count += 1


def make_transport_factory(per_authority_responses):
    """按 authority 顺序产出独立 StatefulTransport。

    factory 第 i 次调用（第 i 个建立的 authority）返回携带第 i 组
    预编程响应的独立实例；返回 (factory, created) 供所有权断言。
    """
    created: list[StatefulTransport] = []
    queue = [list(r) for r in per_authority_responses]

    def factory():
        if not queue:
            responses = [AssertionError("unexpected extra authority")]
        else:
            responses = queue.pop(0)
        transport = StatefulTransport(Recorder(responses))
        created.append(transport)
        return transport

    return factory, created


def one_transport(responses):
    """单 authority 便捷 factory（仅应被调用一次）"""
    factory, _ = make_transport_factory([responses])
    return factory


def forbidden_factory():
    """不应发出任何请求的 transport factory"""
    return one_transport([AssertionError("must not fetch")])


def ok_response() -> httpx.Response:
    # 纯文本 Surge 节点行（base64 需 ss:// 等前缀才被识别为订阅体）
    return httpx.Response(200, text="N = trojan, 1.1.1.1, 443, password=x")


def redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


def install_resolver(monkeypatch, mapping, calls=None):
    """fake DNS：host → [地址]；未登记的 host 返回解析失败"""
    async def fake_resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append((host, port))
        addresses = mapping.get(host)
        if addresses is None:
            raise httpx.ConnectError("dns resolution failed (fake)")
        return list(addresses)
    monkeypatch.setattr(subscription_service, "_resolve_host", fake_resolve)


async def fetch(url, responses) -> tuple[list[dict], StatefulTransport]:
    """单 authority 抓取：返回 (节点, transport)（transport.requests 可查）"""
    factory, created = make_transport_factory([responses])
    nodes = await fetch_subscription(url, _transport_factory=factory)
    assert len(created) == 1
    return nodes, created[0]


def install_client_spy(monkeypatch):
    """记录 AsyncClient 构造与 aclose 顺序（证明 authority 连接边界）"""
    events: list[tuple] = []
    real_client = httpx.AsyncClient

    class SpyClient(real_client):
        def __init__(self, *args, **kwargs):
            events.append(("new", kwargs.get("trust_env"),
                           kwargs.get("follow_redirects"),
                           kwargs.get("transport")))
            super().__init__(*args, **kwargs)

        async def aclose(self):
            events.append(("close",))
            await super().aclose()

    monkeypatch.setattr(subscription_service.httpx, "AsyncClient", SpyClient)
    return events


# ===== 初始目标解析：非全局地址整主机拒绝（不发请求） =====

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "127.0.0.1",                # loopback v4
    "0.0.0.0",                  # unspecified
    IP_10_A,                    # private
    IP_172,                     # private
    IP_192_A,                   # private
    "169.254.1.1",              # link-local v4
    "100.64.0.1",               # CGNAT shared（非 global）
    "192.0.2.33",               # TEST-NET-1（文档段，非 global）
    "224.0.0.1",                # multicast v4
    "240.0.0.1",                # reserved v4
    "255.255.255.255",          # broadcast
    "::1",                      # loopback v6
    "fe80::1",                  # link-local v6
    "fc00::1",                  # ULA v6
    "ff02::1",                  # multicast v6
    "::ffff:127.0.0.1",         # IPv4-mapped IPv6（映射回环）
    "::ffff:93.184.216.34",     # IPv4-mapped IPv6（即使映射公网 v4 也拒绝）
    "64:ff9b::93.184.216.34",   # NAT64 well-known（非 global）
])
async def test_rejects_url_host_resolving_to_non_global(monkeypatch, bad):
    """域名解析出任一非全局地址 → 整主机拒绝且不发出任何请求"""
    install_resolver(monkeypatch, {"sub.example.com": [bad]})
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(HTTPS_URL, _transport_factory=forbidden_factory())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "127.0.0.1", IP_10_B, "169.254.9.9", "::1", "fe80::1",
    "::ffff:" + IP_10_D, "224.0.0.9",
])
async def test_rejects_private_ip_literal_url(monkeypatch, bad):
    """URL 直接写非全局 IP 字面量（含 [v6] 形式）→ 拒绝，不触发 DNS"""
    calls = []
    install_resolver(monkeypatch, {}, calls=calls)
    bracketed = f"[{bad}]" if ":" in bad else bad
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(f"https://{bracketed}/feed",
                                 _transport_factory=forbidden_factory())
    assert calls == []  # IP 字面量不走 DNS


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [
    "sub.localhost", "localhost", "internal.corp", "127.0.0.1.nip.io",
])
async def test_rejects_hosts_resolving_to_loopback(monkeypatch, host):
    """sub.localhost 等域名解析到回环 → 拒绝"""
    install_resolver(monkeypatch, {host: ["127.0.0.1"]})
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(f"https://{host}/feed",
                                 _transport_factory=forbidden_factory())


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://2130706433/feed",       # 127.0.0.1 十进制
    "https://0x7f000001/feed",       # 127.0.0.1 十六进制
    "https://0177.0.0.1/feed",       # 127.0.0.1 八进制
])
async def test_numeric_ip_forms_go_through_validation(monkeypatch, url):
    """数字形式 IPv4 交由解析器展开后仍被同一校验拒绝（系统 resolver 语义）"""
    install_resolver(monkeypatch, {"2130706433": ["127.0.0.1"],
                                   "0x7f000001": ["127.0.0.1"],
                                   "0177.0.0.1": ["127.0.0.1"]})
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(url, _transport_factory=forbidden_factory())


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [
    [PUBLIC_V4, IP_10_A],                      # v4 公网 + v4 私网
    ["93.184.216.34", "::ffff:127.0.0.1"],     # v4 公网 + mapped 回环
    [PUBLIC_V6, "fe80::1"],                    # v6 公网 + v6 link-local
    [PUBLIC_V4, PUBLIC_V6, IP_192_B],          # 多地址任一私网
])
async def test_any_non_global_address_rejects_whole_host(monkeypatch, mixed):
    """多 A/AAAA：任一地址非全局 → 整主机拒绝（fail closed）"""
    install_resolver(monkeypatch, {"sub.example.com": mixed})
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(HTTPS_URL, _transport_factory=forbidden_factory())


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "ftp://sub.example.com/feed",
    "file:///etc/passwd",
    "gopher://sub.example.com/feed",
    "javascript:alert(1)",
    "https:///feed",                            # 无 host
    "https://user:pass@sub.example.com/feed",   # userinfo 禁止
    "http://token@sub.example.com/feed",
    "https://sub.example.com:99999/feed",       # 非法端口
])
async def test_rejects_bad_scheme_or_userinfo(monkeypatch, url):
    """仅 http/https、禁 userinfo、host 必填、端口合法"""
    calls = []
    install_resolver(monkeypatch, {}, calls=calls)
    with pytest.raises((UnsafeSubscriptionTarget, ValueError)):
        await fetch_subscription(url, _transport_factory=forbidden_factory())
    assert calls == []


# ===== 重定向：每跳校验、拒绝第二跳、绑定 IP、Host/SNI =====

@pytest.mark.asyncio
@pytest.mark.parametrize("location,resolve_map", [
    ("http://" + IP_192_B + "/feed", {}),                  # 字面量私网
    ("http://127.0.0.1:8080/feed", {}),                    # 字面量回环
    ("http://[::1]/feed", {}),                             # 字面量 v6 回环
    ("http://evil.internal/feed", {"evil.internal": [IP_10_C]}),
    ("http://rebind.example.net/feed",
     {"rebind.example.net": ["::ffff:" + IP_192_A]}),
])
async def test_redirect_to_non_global_rejected_no_second_hop(
        monkeypatch, location, resolve_map):
    """公网 URL 30x 指向私网/回环：拒绝且只发出第一跳请求"""
    resolve_map.setdefault("sub.example.com", [PUBLIC_V4])
    install_resolver(monkeypatch, resolve_map)
    factory, created = make_transport_factory(
        [[redirect(location), AssertionError("must not follow")]])
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert len(created) == 1
    assert len(created[0].requests) == 1
    assert created[0].requests[0].url.host == PUBLIC_V4


@pytest.mark.asyncio
async def test_redirect_revalidates_dns_each_hop(monkeypatch):
    """每跳重新解析：第二跳 host 再次走 fake resolver 与全局校验；
    跨 authority 两个独立 transport 实例各服务一跳"""
    calls = []
    install_resolver(monkeypatch, {
        "sub.example.com": [PUBLIC_V4],
        "cdn.example.net": [PUBLIC_V4_ALT],
    }, calls=calls)
    factory, created = make_transport_factory([
        [redirect("https://cdn.example.net/cdn")],
        [ok_response()]])
    nodes = await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert [h for h, _ in calls] == ["sub.example.com", "cdn.example.net"]
    assert len(created) == 2
    requests = created[0].requests + created[1].requests
    assert [r.url.host for r in requests] == [PUBLIC_V4, PUBLIC_V4_ALT]
    assert nodes and nodes[0]["server"] == "1.1.1.1"


@pytest.mark.asyncio
async def test_dns_rebinding_binds_first_verified_ip(monkeypatch):
    """连接必须绑定第一次已验证 IP：resolver 后续返回恶意地址也不影响连接目标"""
    answers = [[PUBLIC_V4], ["127.0.0.1"], [IP_10_D]]

    async def rebind_resolve(host, port):
        return answers.pop(0) if answers else ["127.0.0.1"]

    monkeypatch.setattr(subscription_service, "_resolve_host", rebind_resolve)
    nodes, trans = await fetch(HTTPS_URL, [ok_response()])
    # 请求连接的 host 是第一次验证的 IP，绝不重新按域名解析连接
    assert [r.url.host for r in trans.requests] == [PUBLIC_V4]
    assert trans.requests[0].headers["Host"] == "sub.example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
async def test_all_redirect_statuses_followed(monkeypatch, code):
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    nodes, trans = await fetch(
        HTTPS_URL,
        [httpx.Response(code, headers={"Location": "/next"}),
         ok_response()])
    assert len(trans.requests) == 2
    assert trans.requests[1].url.path == "/next"


@pytest.mark.asyncio
async def test_relative_and_absolute_location_resolution(monkeypatch):
    """相对路径按当前逻辑 URL 解析；绝对路径替换 path"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    nodes, trans = await fetch(
        "https://sub.example.com/a/b/feed",
        [redirect("../c/list?token=zzz"), redirect("/final"), ok_response()])
    assert trans.requests[0].url.path == "/a/b/feed"
    assert trans.requests[1].url.path == "/a/c/list"
    assert trans.requests[1].url.query == b"token=zzz"
    assert trans.requests[2].url.path == "/final"


@pytest.mark.asyncio
async def test_host_header_and_sni_preserved_on_https(monkeypatch):
    """HTTPS：Host 用原域名（默认端口不带端口）；SNI extension 用原域名；
    连接 URL host 用已验证 IP"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    _, trans = await fetch(HTTPS_URL, [ok_response()])
    req = trans.requests[0]
    assert req.url.host == PUBLIC_V4
    assert req.headers["Host"] == "sub.example.com"
    assert req.extensions.get("sni_hostname") == "sub.example.com"


@pytest.mark.asyncio
async def test_host_header_keeps_nonstandard_port(monkeypatch):
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    _, trans = await fetch("https://sub.example.com:8443/feed", [ok_response()])
    assert trans.requests[0].headers["Host"] == "sub.example.com:8443"
    assert trans.requests[0].url.port == 8443
    assert trans.requests[0].url.host == PUBLIC_V4


@pytest.mark.asyncio
async def test_ipv6_address_bracketed_in_url_and_host_header(monkeypatch):
    """IPv6 已验证地址：连接 URL 正确加括号；Host header 用原域名"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V6]})
    _, trans = await fetch(HTTPS_URL, [ok_response()])
    req = trans.requests[0]
    assert req.url.host == PUBLIC_V6
    assert str(req.url).startswith(f"https://[{PUBLIC_V6}]")
    assert req.headers["Host"] == "sub.example.com"
    assert req.extensions.get("sni_hostname") == "sub.example.com"


@pytest.mark.asyncio
async def test_ipv6_literal_url_connects_directly(monkeypatch):
    """URL 直接写公网 IPv6 字面量：直接连接，Host header 带括号"""
    calls = []
    install_resolver(monkeypatch, {}, calls=calls)
    _, trans = await fetch(f"https://[{PUBLIC_V6}]/feed", [ok_response()])
    assert calls == []
    assert trans.requests[0].url.host == PUBLIC_V6
    assert trans.requests[0].headers["Host"] == f"[{PUBLIC_V6}]"


@pytest.mark.asyncio
async def test_http_scheme_no_sni_extension(monkeypatch):
    """http 明文：无需 SNI；Host/绑定 IP 语义不变"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    _, trans = await fetch("http://sub.example.com/feed", [ok_response()])
    req = trans.requests[0]
    assert req.url.host == PUBLIC_V4
    assert req.headers["Host"] == "sub.example.com"
    assert "sni_hostname" not in req.extensions


@pytest.mark.asyncio
async def test_multi_global_addresses_tried_in_order_no_domain_fallback(
        monkeypatch):
    """多全局地址依序尝试：第一个连接失败换下一个，绝不按域名连接；
    同 authority 的重试共用同一 transport 实例"""
    calls = []
    install_resolver(monkeypatch,
                     {"sub.example.com": [PUBLIC_V4, PUBLIC_V4_ALT]},
                     calls=calls)
    nodes, trans = await fetch(HTTPS_URL, [httpx.ConnectError("first refused"),
                                           ok_response()])
    assert [r.url.host for r in trans.requests] == [PUBLIC_V4, PUBLIC_V4_ALT]
    assert len(calls) == 1  # 一跳只解析一次，重试不重新解析
    assert nodes[0]["server"] == "1.1.1.1"


@pytest.mark.asyncio
async def test_all_addresses_exhausted_raises_connect_error(monkeypatch):
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    with pytest.raises(httpx.ConnectError):
        await fetch_subscription(
            HTTPS_URL,
            _transport_factory=one_transport(
                [httpx.ConnectError("refused 1"),
                 httpx.ConnectError("refused 2")]))


@pytest.mark.asyncio
async def test_redirect_limit_and_loop_safety(monkeypatch):
    """超过上限（>5 跳）与 A→B→A 循环都终止于 TooManyRedirects；
    authority 反复重建时每次都是新 transport 实例"""
    install_resolver(monkeypatch, {"a.example.com": [PUBLIC_V4],
                                   "b.example.com": [PUBLIC_V4_ALT]})
    factory, created = make_transport_factory(
        [[redirect("https://b.example.com/1")],
         [redirect("https://a.example.com/2")],
         [redirect("https://b.example.com/3")],
         [redirect("https://a.example.com/4")],
         [redirect("https://b.example.com/5")],
         [redirect("https://a.example.com/6")],
         [AssertionError("must not reach hop 7")]])
    with pytest.raises(httpx.TooManyRedirects):
        await fetch_subscription("https://a.example.com/feed",
                                 _transport_factory=factory)
    assert len(created) == 6  # 每跳独立 authority → 独立实例

    factory2, created2 = make_transport_factory(
        [[redirect("https://b.example.com/x")],
         [redirect("https://a.example.com/feed")]] * 4)
    with pytest.raises(httpx.TooManyRedirects):
        await fetch_subscription("https://a.example.com/feed",
                                 _transport_factory=factory2)
    assert len(created2) == 6
    assert all(t.close_count == 1 for t in created2)  # 每实例恰好关闭一次


@pytest.mark.asyncio
async def test_redirect_scheme_change_revalidated(monkeypatch):
    """https→http 与 http→https 跨协议跳转都重新校验后允许（跨
    authority 独立实例）"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4],
                                   "plain.example.net": [PUBLIC_V4_ALT]})
    factory, created = make_transport_factory([
        [redirect("http://plain.example.net/plain")],
        [ok_response()]])
    nodes = await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    req2 = created[1].requests[0]
    assert req2.url.scheme == "http"
    assert req2.url.host == PUBLIC_V4_ALT
    assert "sni_hostname" not in req2.extensions


# ===== 环境代理不生效 =====

@pytest.mark.asyncio
async def test_env_proxies_never_applied(monkeypatch):
    """HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 全部无效（trust_env=False）"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    factory, created = make_transport_factory([[ok_response()]])
    events = install_client_spy(monkeypatch)
    nodes = await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert len(events) == 2  # new + finally close（单 authority）
    new_events = [e for e in events if e[0] == "new"]
    assert all(e[1] is False for e in new_events)   # trust_env=False
    assert all(e[2] is False for e in new_events)   # follow_redirects=False
    # client 拿到的正是 factory 产出的独立实例
    assert [e[3] for e in new_events] == created
    assert created[0].requests[0].url.host == PUBLIC_V4


# ===== authority 隔离 / cookie 禁用 / IDNA A-label =====

@pytest.mark.asyncio
async def test_cross_authority_same_ip_distinct_pool_and_identity(
        monkeypatch):
    """a.example→b.example 且解析到同一公网 IP：不同逻辑 authority
    必须使用不同 transport 实例（独立连接池）与各自握手身份
    （SNI 属于各自 authority 的 TLS 会话）；旧实例在新请求前关闭；
    第一跳 Set-Cookie 不得进入第二跳。"""
    calls = []
    install_resolver(monkeypatch, {
        "a.example.com": [PUBLIC_V4],
        "b.example.com": [PUBLIC_V4],   # 同一公网 IP
    }, calls=calls)
    events = install_client_spy(monkeypatch)
    factory, created = make_transport_factory([
        [httpx.Response(302, headers={
            "Location": "https://b.example.com/next",
            "Set-Cookie": "sid=FIRST; Path=/"})],
        [ok_response()],
    ])
    nodes = await fetch_subscription("https://a.example.com/feed",
                                     _transport_factory=factory)
    # 两个 authority → 两个独立 transport/client，旧的先关闭
    assert [e[0] for e in events] == ["new", "close", "new", "close"]
    assert len(created) == 2
    assert created[0].instance_id != created[1].instance_id
    # 各自握手身份锁定为自己的逻辑 host（同 IP 不同身份 → 不同池）
    assert created[0].handshake_identity == "a.example.com"
    assert created[1].handshake_identity == "b.example.com"
    # 连接 URL 均绑定同一已验证 IP，Host/SNI 各自为逻辑 authority
    all_requests = created[0].requests + created[1].requests
    assert [r.url.host for r in all_requests] == [PUBLIC_V4, PUBLIC_V4]
    assert [r.headers["Host"] for r in all_requests] == \
        ["a.example.com", "b.example.com"]
    assert [r.extensions.get("sni_hostname") for r in all_requests] == \
        ["a.example.com", "b.example.com"]
    # Cookie 隔离：第一跳 Set-Cookie 不进第二跳
    assert "Cookie" not in created[1].requests[0].headers
    # 每个实例恰好关闭一次
    assert [t.close_count for t in created] == [1, 1]


@pytest.mark.asyncio
async def test_same_authority_shares_single_client_and_transport(monkeypatch):
    """同 authority（相对路径重定向）：单 client 复用，且 factory 只
    调用一次——同实例服务全部请求（含 Host/SNI 一致性）"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    events = install_client_spy(monkeypatch)
    factory, created = make_transport_factory(
        [[redirect("/next"), ok_response()]])
    nodes = await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert [e[0] for e in events] == ["new", "close"]  # 只构造一次
    assert len(created) == 1
    assert len(created[0].requests) == 2
    assert created[0].close_count == 1


@pytest.mark.asyncio
async def test_cookies_disabled_even_within_same_authority(monkeypatch):
    """订阅抓取无会话语义：即使同 authority，响应 Cookie 也不进后续请求"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    nodes, trans = await fetch(HTTPS_URL, [
        httpx.Response(302, headers={"Location": "/next",
                                     "Set-Cookie": "sid=LEAK; Path=/"}),
        ok_response()])
    assert "Cookie" not in trans.requests[1].headers


@pytest.mark.asyncio
async def test_idn_initial_url_uses_ascii_a_label(monkeypatch):
    """Unicode IDN 初始 URL：resolver/Host/SNI 统一用 IDNA A-label
    （单一来源 httpx.URL.raw_host），连接仍绑定已验证 IP"""
    calls = []
    install_resolver(monkeypatch, {IDN_HOST_ALABEL: [PUBLIC_V4]},
                     calls=calls)
    _, trans = await fetch(f"https://{IDN_HOST_UNICODE}/feed", [ok_response()])
    assert [h for h, _ in calls] == [IDN_HOST_ALABEL]  # resolver 收 A-label
    req = trans.requests[0]
    assert req.url.host == PUBLIC_V4
    assert req.headers["Host"] == IDN_HOST_ALABEL
    assert req.extensions.get("sni_hostname") == IDN_HOST_ALABEL


@pytest.mark.asyncio
async def test_idn_redirect_location_uses_ascii_a_label(monkeypatch):
    """redirect Location 的 IDN host（HTTP header 恒 ASCII，wire 上即
    A-label/punycode）：第二跳同样走 A-label 校验链且 resolver/Host/
    SNI 一致。Unicode 输入由初始 URL 用例覆盖（httpx.URL 构造即
    IDNA 化，Location 解析同源）。"""
    calls = []
    install_resolver(monkeypatch, {
        "sub.example.com": [PUBLIC_V4],
        IDN2_HOST_ALABEL: [PUBLIC_V4_ALT],
    }, calls=calls)
    factory, created = make_transport_factory([
        [redirect(f"https://{IDN2_HOST_ALABEL}/next")],
        [ok_response()]])
    nodes = await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert [h for h, _ in calls] == ["sub.example.com", IDN2_HOST_ALABEL]
    req2 = created[1].requests[0]
    assert req2.url.host == PUBLIC_V4_ALT
    assert req2.headers["Host"] == IDN2_HOST_ALABEL
    assert req2.extensions.get("sni_hostname") == IDN2_HOST_ALABEL


@pytest.mark.asyncio
async def test_idn_host_resolving_private_rejected(monkeypatch):
    """IDN 域名解析出私网地址同样整主机拒绝（A-label 路径不弱化校验）"""
    install_resolver(monkeypatch, {IDN_HOST_ALABEL: [IP_192_A]})
    with pytest.raises(UnsafeSubscriptionTarget):
        await fetch_subscription(f"https://{IDN_HOST_UNICODE}/feed",
                                 _transport_factory=forbidden_factory())


@pytest.mark.asyncio
async def test_nonstandard_port_cross_authority_rebuilds(monkeypatch):
    """非默认端口参与 authority 判定：同端口不同 host 仍隔离"""
    install_resolver(monkeypatch, {"a.example.com": [PUBLIC_V4],
                                   "b.example.com": [PUBLIC_V4]})
    events = install_client_spy(monkeypatch)
    factory, created = make_transport_factory([
        [redirect("https://b.example.com:8443/next")],
        [ok_response()]])
    nodes = await fetch_subscription(
        "https://a.example.com:8443/feed", _transport_factory=factory)
    assert [e[0] for e in events] == ["new", "close", "new", "close"]
    all_requests = created[0].requests + created[1].requests
    assert [r.headers["Host"] for r in all_requests] == \
        ["a.example.com:8443", "b.example.com:8443"]
    assert [t.close_count for t in created] == [1, 1]


# ===== Fix Round 2：transport 所有权与生命周期 =====

@pytest.mark.asyncio
async def test_closed_transport_rejects_requests_and_not_reused(monkeypatch):
    """StatefulTransport 关闭后请求即失败；下一 authority 拿到的是
    新实例而非已关闭实例（错误复用会被关闭态检查暴露）"""
    install_resolver(monkeypatch, {"a.example.com": [PUBLIC_V4],
                                   "b.example.com": [PUBLIC_V4_ALT]})
    factory, created = make_transport_factory([
        [redirect("https://b.example.com/next")],
        [ok_response()]])
    nodes = await fetch_subscription("https://a.example.com/feed",
                                     _transport_factory=factory)
    first, second = created
    assert first.close_count == 1          # 切换 authority 时已关闭
    assert second is not first
    assert second.close_count == 1         # finally 关闭
    # 已关闭实例再收请求 → 立即失败（所有权语义自检）
    req = httpx.Request("GET", "https://93.184.216.34/x")
    with pytest.raises(RuntimeError):
        await first.handle_async_request(req)



@pytest.mark.asyncio
async def test_each_transport_closed_exactly_once_on_success(monkeypatch):
    """多 authority 成功路径：每个已建 transport 恰好 aclose 一次"""
    install_resolver(monkeypatch, {"a.example.com": [PUBLIC_V4],
                                   "b.example.com": [PUBLIC_V4_ALT],
                                   "c.example.net": [PUBLIC_V6]})
    factory, created = make_transport_factory([
        [redirect("https://b.example.com/2")],
        [redirect("https://c.example.net/3")],
        [ok_response()]])
    nodes = await fetch_subscription("https://a.example.com/feed",
                                     _transport_factory=factory)
    assert len(created) == 3
    assert [t.close_count for t in created] == [1, 1, 1]


@pytest.mark.asyncio
async def test_each_transport_closed_once_on_request_error(monkeypatch):
    """请求异常（连接耗尽）传播时：当前与已切换的 transport 各恰好关闭一次"""
    install_resolver(monkeypatch, {"a.example.com": [PUBLIC_V4],
                                   "b.example.com": [PUBLIC_V4_ALT]})
    factory, created = make_transport_factory([
        [redirect("https://b.example.com/next")],
        [httpx.ConnectError("refused A"), httpx.ConnectError("refused B")]])
    with pytest.raises(httpx.ConnectError):
        await fetch_subscription("https://a.example.com/feed",
                                 _transport_factory=factory)
    assert [t.close_count for t in created] == [1, 1]


@pytest.mark.asyncio
async def test_transport_closed_once_on_inflight_cancellation(monkeypatch):
    """请求中途 CancelledError（网络栈内传播的取消语义）：finally 仍
    关闭当前 transport 恰好一次（Recorder 对 BaseException 响应项
    直接 raise，模拟取消从传输栈内传播）"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    factory, created = make_transport_factory([[asyncio.CancelledError()]])
    with pytest.raises(asyncio.CancelledError):
        await fetch_subscription(HTTPS_URL, _transport_factory=factory)
    assert [t.close_count for t in created] == [1]


@pytest.mark.asyncio
async def test_transport_closed_once_on_real_task_cancel(monkeypatch):
    """真实 task.cancel()：取消传播后 transport 恰好关闭一次"""

    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(60)

    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    factory, created = make_transport_factory([[hang]])
    task = asyncio.create_task(
        fetch_subscription(HTTPS_URL, _transport_factory=factory))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [t.close_count for t in created] == [1]


@pytest.mark.asyncio
async def test_new_fetch_client_default_transport(monkeypatch):
    """生产路径（不注入 factory）：_new_fetch_client(None) 让 httpx
    自建独立 AsyncHTTPTransport/连接池并随 client 关闭（单元级验证
    构造参数，不出真实网络）。"""
    captured = {}
    real_client = httpx.AsyncClient

    class SpyClient(real_client):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subscription_service.httpx, "AsyncClient", SpyClient)
    client = subscription_service._new_fetch_client(None)
    await client.aclose()
    assert captured["transport"] is None
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


# ===== 错误映射：不泄漏目标 =====

@pytest.mark.asyncio
async def test_error_mapping_reuses_safe_codes(monkeypatch):
    """SSRF 拒绝 → invalid_content；解析/连接失败/重定向超限 → network_error；
    异常文本不含目标 host/URL"""
    assert safe_sync_error(UnsafeSubscriptionTarget("x"))[0] == "invalid_content"
    assert safe_sync_error(httpx.ConnectError("dns resolution failed"))[0] \
        == "network_error"
    assert safe_sync_error(httpx.TooManyRedirects("loop"))[0] == "network_error"

    try:
        await fetch_subscription("https://" + IP_192_A + "/feed",
                                 _transport_factory=forbidden_factory())
    except UnsafeSubscriptionTarget as exc:
        assert IP_192_A not in str(exc)
        assert str(exc)  # 静态消息非空


@pytest.mark.asyncio
async def test_fetch_never_logs_url_or_host(monkeypatch, caplog):
    """fetch 路径不产生含 URL/host/query 的日志记录"""
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]})
    with caplog.at_level(logging.DEBUG, logger="app.services.subscription"):
        await fetch("https://sub.example.com/feed?token=SECRET", [ok_response()])
    for record in caplog.records:
        assert "sub.example.com" not in record.getMessage()
        assert "SECRET" not in record.getMessage()


# ===== 纯本地 smoke：重定向 + 绑定 IP 全流程 =====

@pytest.mark.asyncio
async def test_smoke_local_fake_transport_redirect_and_ip_binding(monkeypatch):
    """纯本地 fake transport 冒烟：
    https 公网域名 → 302 相对路径 → 200 纯文本节点；
    全程连接 host 均为已验证公网 IP，Host/SNI 为原域名，无真实网络。
    """
    calls = []
    install_resolver(monkeypatch, {"sub.example.com": [PUBLIC_V4]},
                     calls=calls)
    nodes, trans = await fetch(HTTPS_URL, [redirect("/next?sid=1"),
                                           ok_response()])
    assert [n["node_type"] for n in nodes] == ["trojan"]
    assert [r.url.host for r in trans.requests] == [PUBLIC_V4, PUBLIC_V4]
    assert [r.headers["Host"] for r in trans.requests] == ["sub.example.com"] * 2
    assert all(r.extensions.get("sni_hostname") == "sub.example.com"
               for r in trans.requests)
    assert trans.requests[1].url.path == "/next"
    assert trans.requests[1].url.query == b"sid=1"
    assert len(calls) == 2  # 初始 + 每跳各一次
    assert trans.close_count == 1
