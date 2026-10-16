# Whole-Branch Fix Report

Target-aware Surge 配置分支的整体修复报告。每个 Slice 记录：问题、
方案、技术依据（官方资料核实）、验证证据。

---

## Slice 2 — 订阅抓取 SSRF 防护（fetch 阶段地址绑定）

Commit: `fix: bind subscription fetches to validated public addresses`

### 1. 问题（原 finding）

`app/services/subscription.py` 原 `fetch_subscription`：

- `_validate_url` 仅对 URL **文本**里的 host 做字面量检查（IP 字面量
  检查 private/loopback/link-local/multicast，域名直接放行）；
- `httpx.AsyncClient(follow_redirects=True)`：每跳重定向目标不做任何
  校验，公网订阅 30x → 内网/回环任意地址直达；
- 域名不解析：`internal.corp`、`sub.localhost`、解析回环/私网的域名
  全部放行；多 A/AAAA 混合公网+私网也放行；
- `trust_env` 默认开启：环境 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` 会把
  抓取流量送往任意指定代理（调度器周期触发 → 稳定 SSRF 探测面）；
- DNS rebinding：即便校验域名，客户端连接时仍按域名二次解析，
  校验与连接之间存在 TOCTOU 窗口。

### 2. 方案（fail-closed，逐层）

抓取路径 `fetch_subscription` 重写为手动循环，**初始与每一跳**执行：

1. **静态校验** `_parse_fetch_target`：仅 `http`/`https`、禁
   userinfo、host 必填、端口合法；`httpx.InvalidURL`（含前导零
   IPv4 等变体）统一转 `UnsafeSubscriptionTarget`。
2. **地址验证** `_validated_target_addresses`：
   - IP 字面量直接校验，不触发 DNS；
   - 域名经 `_resolve_host`（`asyncio` `getaddrinfo`，全部 A/AAAA）
     后**整体验证**：任一地址非全局即**整主机拒绝**；
   - `_is_global_public_ip` = `is_global` 且非 multicast/reserved/
     unspecified；**IPv4-mapped IPv6（`::ffff:0:0/96`）一律拒绝**
     （含映射公网 v4 的形式——映射语义即内嵌 v4，等价绕过 v4 校验）。
3. **连接绑定已验证 IP** `_request_bound_to_addresses`：
   - 连接 URL 用 `logical.copy_with(host=address)`（已验证 IP，
     IPv6 由 httpx 规范加括号）；
   - 显式 `Host` header 保留原 host（非默认端口带端口，IPv6 括号）；
   - HTTPS 传 `extensions={"sni_hostname": host}`（仅 https 且非 IP
     字面量）；
   - 多全局地址依序尝试，连接级失败（ConnectError/ConnectTimeout）
     换下一个，**绝不回退按域名连接**；
   - 客户端全程无机会按域名二次解析 → **DNS rebinding 窗口消除**。
4. **重定向手动处理**：`follow_redirects=False`；301/302/303/307/308
   的 `Location` 相对当前逻辑 URL（`httpx.URL.join`）解析后，
   新目标**重新走全部校验**；上限 5 跳，超限/循环抛
   `httpx.TooManyRedirects`。
5. **环境隔离**：`trust_env=False`，忽略 `HTTP(S)_PROXY` 等。
6. **不泄漏目标**：`UnsafeSubscriptionTarget` 消息全部为静态文本；
   fetch 路径不记录 URL/host/query；错误沿用 `safe_sync_error`
   allowlist（SSRF 拒绝 → `invalid_content`，解析/连接失败/重定向
   超限 → `network_error`），订阅事务语义零改动。

### 3. 技术依据（已用当前 httpx/httpcore 资料核实，非猜测）

- **httpcore 1.0.9**（本仓库安装版本）`_async/connection.py::_connect`：
  `sni_hostname = request.extensions.get("sni_hostname", None)`，
  TLS `start_tls(server_hostname=sni_hostname or origin.host)` ——
  即：连接始终用 origin（连接 URL）的 host，而 SNI 与证书主机名
  校验用 `sni_hostname` extension。官方 httpcore docs/extensions.md
  给出等价示例（`https://185.199.108.153` + `Host: www.encode.io` +
  `sni_hostname: www.encode.io`）。
- **httpx 0.28.1**（实测核实）：`AsyncClient.get(..., extensions=...)`
  支持 request 级 extensions；显式 `headers={"Host": ...}` 可覆盖
  URL 推导的 Host；`URL.copy_with(host=<v6>)` 自动加括号；
  `URL.join` 按 RFC 3986 解析相对/绝对 Location。
- 因此「连接 URL = 已验证 IP + Host header = 原 host + SNI =
  原 host」同时满足 IP 绑定与 HTTPS Host/SNI/证书正确性。

### 4. 验证证据

测试文件：`tests/test_subscription_fetch_ssrf.py`（70 个用例，
全部 fake resolver + `httpx.MockTransport`，零真实网络）：

- RED：实现前运行 → `ImportError: cannot import name
  'UnsafeSubscriptionTarget'`（先失败复现）。
- GREEN：专项 70 passed；订阅/调度相关
  （sync/subscriptions/scheduler/status_api）84 passed；
  全量 pytest **1132 passed**（含仓库开源安全基线扫描）。
- 覆盖矩阵：loopback/private/link-local/CGNAT/TEST-NET/multicast/
  reserved/unspecified/IPv4-mapped IPv6/NAT64 拒绝；多 A/AAAA 任一
  非全局整主机拒绝；`sub.localhost` 等解析回环拒绝；十/十六/八进制
  IPv4 变体拒绝；IP 字面量 URL 拒绝；非 http(s) 协议与 userinfo
  拒绝；30x → 私网/回环/mapped 拒绝且不发第二跳；每跳重新解析；
  DNS rebinding 绑定首验 IP；多全局地址依序尝试不回退域名；301/
  302/303/307/308 全跟随；相对/绝对 Location 解析；Host header
  （默认端口/非标准端口/IPv6 括号）；SNI 仅 https 且用原域名；
  重定向 5 跳上限与 A→B→A 循环安全；跨协议跳转重校验；
  `HTTP_PROXY` 等环境代理不生效（`trust_env=False` 断言）；
  错误映射沿用安全码且异常文本无目标；日志无 URL/host/query；
  纯本地 smoke（`test_smoke_local_fake_transport_redirect_and_ip_binding`）
  证明 redirect 全流程 + 连接 IP 绑定 + Host/SNI 正确。
