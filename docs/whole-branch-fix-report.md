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

### 5. Fix Round 1 — authority 隔离 / cookie 禁用 / IDNA A-label 单一来源

Commit: `fix: isolate subscription redirect authorities`

复审发现的两个 Important 问题与修复（均先写失败测试复现）：

**问题 A：跨逻辑 authority 复用连接与 Cookie 泄漏。** 上一版整个
抓取共用一个 `AsyncClient`：(1) httpcore 连接池按 origin（scheme,
host, port）复用，而我们的连接 URL host 是已验证 IP——`a.example`
与 `b.example` 解析到同一公网 IP 且端口相同时 origin 完全相同，
第二跳会**复用第一跳的 TCP/TLS 连接**（第二跳的 `sni_hostname`
不再参与握手，TLS 会话属于第一跳的 host）；(2) 已实测复现：同一
client 上第一响应的 `Set-Cookie` 会自动进入后续请求（按请求 URL
的 host=IP 匹配），同 IP 不同逻辑域即发生 Cookie 跨 authority 泄漏。

修复：
- **authority（scheme, host, port）变化即 `aclose()` 旧 client 并
  新建**（`_new_fetch_client` 工厂）：连接池与 cookie jar 都不跨
  逻辑 authority；同 authority（如相对路径重定向）复用同一 client。
  生产路径各 authority 自带独立 `AsyncHTTPTransport`/httpcore 池
  （~~注入的 MockTransport 可跨 client 复用~~——**Fix Round 2 更正**：
  共享单实例是所有权缺陷，真实 transport 随第一个 client 关闭即失效，
  详见下节）。
- **Cookie 显式禁用**：每个响应后 `client.cookies.clear()`——订阅
  抓取无会话语义，即使同 authority 的相对路径跳转也不携带 Cookie。
  （注：`AsyncClient(cookies=NoCookieJar子类)` 方案无效——httpx
  构造时会用 `Cookies(cookies)` 重新包装，覆写方法丢失；清空 public
  jar 是已验证有效的最小手段。）

**问题 B：urllib/httpx 双解析漂移与 IDN。** 上一版静态校验用
`urllib.parse.urlparse`（`hostname` 对 IDN 返回 Unicode），逻辑 URL
用 `httpx.URL`（`host` 属性会把 `xn--` 解码回 Unicode，且不同输入
形态行为不一致）——Unicode host 会流入 resolver / Host header /
SNI（TLS SNI 与 HTTP/1.1 header 均要求 ASCII A-label）。

修复：**单一解析源 `httpx.URL`**，host 一律取
`u.raw_host.decode("ascii")`（实测恒为小写 ASCII：IDN→IDNA
A-label，IPv6→去括号，IPv4 原样；IDNA 编码失败在构造时抛
InvalidURL → `UnsafeSubscriptionTarget`）；resolver、Host header、
`sni_hostname` 三处统一使用该 A-label；端口范围 1–65535 显式校验
（httpx.URL 保留 99999 这类端口不报错，原 urllib 的 port 属性会抛
ValueError，单一来源后需自查）。redirect Location 在 wire 上恒为
ASCII（IDN 即 A-label/punycode 形态），与 Unicode 初始 URL 殊途同归
到同一 `httpx.URL` 归一化路径。

新增用例（8 个，全 fake resolver + MockTransport）：
cross-authority 同 IP 重建 client（构造/关闭顺序 `new→close→new→
close`）且第一跳 Set-Cookie 不进第二跳；同 authority 单 client 复用；
同 authority 内 Cookie 也禁用；IDN 初始 URL 走 A-label
（resolver/Host/SNI 断言）；IDN A-label Location 第二跳同链路；
IDN 解析私网整主机拒绝；非默认端口参与 authority 判定；注入
transport 跨重建可用。

验证：SSRF 专项 78 passed；订阅/调度/安全扫描相关 161 passed；
全量 pytest **1140 passed**。

### 6. Fix Round 2 — transport 所有权与生命周期

Commit: `test: enforce subscription transport ownership`

**问题（Important）**：上一版把单一 `_transport` 实例注入跨
authority 的所有 client：第一处 `client.aclose()` 即关闭该 transport，
后续 authority 复用的是已关闭实例——真实 `AsyncHTTPTransport` 在此
语义下请求会失败；`MockTransport.aclose` 恰好是 no-op 才掩盖了缺陷。

**修复**：私有注入改为 `_transport_factory`——每个逻辑 authority
建立时调用一次，获得**独立 transport 实例与所有权**（随所属
client `aclose()` 恰好关闭一次）；同 authority（含多地址重试、相对
路径跳转）继续复用同实例；生产不注入时每 authority 由 httpx 自建
独立 `AsyncHTTPTransport`/连接池。同时调整循环顺序为**地址校验先行**
（被拒的 authority 不消耗 factory 调用、不建 client/transport）。

**测试设施**：新增 `StatefulTransport`（状态型 transport）：关闭后
请求即失败；记录 instance id 与 close 计数；首次请求把
`sni_hostname`（http 用 Host）锁定为该实例的模拟握手身份，同实例
后续不同身份拒绝——错误共享实例会同时被关闭态与身份检查暴露。
`Recorder` 支持 Response / Exception / BaseException（取消传播）/
async callable（在途挂起）响应项。全部用例迁移到 factory 注入，
删除误导用例 `test_injected_transport_survives_client_rebuilds`
（其断言的"跨重建复用"正是缺陷本身）。

**新增/迁移用例**：a→b 同 IP 使用不同 pool 与握手身份（各自 SNI
锁定、instance id 不同、Set-Cookie 不跨跳）；关闭后请求失败且下一
authority 不复用已关闭实例；成功 / 请求异常 / 栈内 CancelledError /
真实 task.cancel() 四路径下每个已建 transport 恰好 aclose 一次；
重定向循环 6 跳 6 实例各关闭一次；同 authority 单实例复用；
`_new_fetch_client(None)` 生产构造参数单元验证。零真实网络。

**验证**：SSRF 专项 **83 passed**；订阅/调度/安全扫描相关 **160
passed**；全量 pytest **1145 passed**。

## Slice 3 — 数据库维护生命周期串行化与 WAL 状态保全

Commit: `fix: serialize database maintenance and preserve WAL state`

### 1. 问题（原 finding，三条独立复现）

**A. reset 回导旧仓库库。** `reset_database()` 先删除 live 全家文件再调
`prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")`：live
不存在且旧仓库库存在时命中其"路径 2 legacy 自动发现"，把**本应保留
原样的旧仓库库重新导入 live**——重置后 live 不是 fresh 默认数据，
而是旧库内容（且经备份/迁移链放大为用户可见的数据回滚）。

**B. 发布链先删 live WAL、后做可能失败的 staging 校验。**
`rebuild_from_source()` 顺序为 `upgrade_staging → _remove_live_sidecars
(live) → verify_and_publish(staging, live)`：integrity 检查、权限收敛、
`os.replace` 全部发生在**已删除 live -wal/-shm 之后**。WAL 引入后，
最近已提交事务可能仅存在于 -wal（尚未 checkpoint 并入 main）——
任一校验/替换失败即产生「旧 main + 已删未 checkpoint WAL」，**已提交
数据静默丢失**；同时旧实现声称的"live 字节不变"原子性在删除
sidecar 那一刻已不成立。

**C. dispose 不关 checkout 连接且维护零互斥。** `engine.dispose()` 只
关闭池内空闲连接，checkout 中的连接保持打开：恢复/重置期间在途
API/调度会话可继续写旧文件；且 restore/reset 之间、维护与请求之间
没有任何互斥。实测（见 §3）**旧连接在 `os.replace` 后提交并非写入
黑洞**：SQLite 按路径重建 -wal，提交页直接**污染替换后的新库**；
维护期间新请求还会撞上 `session_factory=None` 抛 500。

### 2. 方案

**维护屏障（`app/database.py`）**：`DatabaseRuntime` 新增同一事件循环
内的会话/维护互斥原语——
- `session()` 受控会话入口：原子登记在途计数（`_state_lock` 保护
  检查+计数），维护期阻塞在 `_sessions_allowed`；未绑定 runtime 抛
  静态 RuntimeError（而非 AttributeError）。
- `maintenance()` 维护屏障：`_maintenance_lock` 排他（restore/reset
  互斥）→ 置维护标志并阻止新会话 → 等待 `_drained`（在途会话归零，
  **不暴力中断成功同步**）→ 维护区；退出（含异常/取消）finally 可靠
  放行。维护区内只允许 dispose/bind 与 staging 私有 runtime。
- 全局 runtime 的生产会话**全部切换到受控入口**：`get_db`（FastAPI
  依赖）、`security.load_access_settings`、`bootstrap` 孤儿清理、
  `SubscriptionScheduler.run_due_once`（选择与逐项会话）；staging
  专用 runtime（`DatabaseRuntime.create`）保留工厂直用。静态守卫测试
  禁止生产模块再出现 `.session_factory` 直连（豁免 staging）。

**restore/reset 全程屏障化（`app/services/backups.py`）**：整个
「安全备份 → dispose → staging 重建 → publish → bind/失败恢复」在
`database.maintenance()` 内：在途受控会话先归零再动文件；新会话
（API/调度）阻塞到重绑完成——既看不到 `session_factory=None`，也不
写旧 inode。`reset_database` 改为**显式 fresh 入口**
`rebuild_from_source(None, paths)`：绝不走 `prepare_database` 的 legacy
自动发现（行为哨兵测试锁死），不预删 live（原子替换由发布链完成），
旧仓库库文件字节不动，reset 后 live 仅含 fresh 种子数据。

**发布链重排（`app/services/database_migration.py`）**：拆出
`verify_staging`（integrity + 伴生收敛 + 0600，**全部可能失败的
staging 侧准备**）与 `_prepare_live_for_replacement`（live 侧唯一
动作：`PRAGMA wal_checkpoint(TRUNCATE)` 把已提交 WAL 页安全并入
main 后才删 sidecar）；`verify_and_publish = verify_staging →
prepare_live → os.replace`。失败矩阵：
- integrity/权限失败：staging 侧抛错，live 一个字节不动；
- checkpoint 失败（TRUNCATE 返回 busy——并发 reader 持 WAL 快照）：
  fail closed 抛错且**不删 sidecar**，live 保持「main + 可恢复 WAL」；
- checkpoint 成功后 sidecar 删除/replace 失败：WAL 已并入 main，live
  逻辑完整（字节因合法 checkpoint 变化，**已提交数据不丢**）；
- 绝不出现「旧 main + 已删未 checkpoint WAL」。
同时修正模块内两处"live 字节不变"的过强措辞为"live 保持逻辑完整
（checkpoint 后字节可能合法变化）"。

### 3. 技术依据（实验/行为核实，非猜测）

- **TRUNCATE checkpoint 语义**：`PRAGMA wal_checkpoint(TRUNCATE)` 返回
  `(busy, log, checkpointed)`；busy=1 表示存在并发 reader 阻止完成。
  旧实现不检查返回值——busy 时照样删 -wal。新实现 busy 即拒绝清理。
- **旧连接 replace 后提交 = 污染新库（实验复现）**：连接 c1 打开
  WAL 库并提交 → `os.replace` 换掉主文件 → c1 再提交 → 新连接读取
  路径库：c1 的**全部新旧写入均出现**。机制：SQLite 主库经 fd（旧
  inode）访问，但 -wal 按路径重建，提交页写入路径侧 WAL 并被后续
  读者应用。这把 C 从"写黑洞丢数据"升级为"静默污染新库"，证明
  生产会话必须全部走受控入口（gate 保证 replace 时刻在途受控会话
  已归零且连接已 dispose）。
- **SQLAlchemy dispose 语义**：`AsyncEngine.dispose()` 归还池并关闭
  空闲连接；checkout 中的连接不阻塞 dispose，归还时被丢弃——旧
  实现的 dispose 不能作为"无并发连接"的证据，必须由屏障等待归零。

### 4. 验证证据

测试文件：`tests/test_database_maintenance.py`（15 个用例，全部 tmp
sqlite，零真实运行库）：

- **RED（实现前）**：9 failed / 5 passed——legacy 回导（prepare 哨兵
  捕获自动发现调用）、[checkpoint]/[replace] 注入下 WAL 已提交记录
  丢失（`wal_only` 计数 0）、在途会话被越过（reset 先于会话结束完成）、
  维护期新会话不阻塞、restore/reset 不互斥、get_db 撞 `NoneType`
  500；[integrity]/[permission] 因新注入点不存在而 error。
- **GREEN（实现后）**：维护专项 **15 passed**；数据库/备份/迁移/调度
  专项（backups + database_migration + subscription_scheduler +
  migrations）**157 passed**；全量 pytest **1160 passed**。
- 覆盖矩阵：legacy 存在时 reset 不回导且旧库字节不变；reset 时序
  dispose →（pre_reset 备份发布）→ live 原子替换 → 重绑；仅 WAL 已
  提交记录（前置证明 main 副本无该记录）在 integrity/权限/checkpoint/
  sidecar/replace 五类故障注入后仍可查且 `integrity_check=ok`；持有
  在途受控会话时 reset 不越过（live inode 不变）、释放后完成并读到
  fresh 库；维护期新会话/下一轮调度阻塞到重绑后读新库；在途调度式
  会话（`run_due_once` 真实同步链路）同理；restore 与 reset 互斥且
  等待方零文件副作用；维护失败/取消后屏障放行、新会话立即可用；
  replace 时刻不存在受控在途会话（inode 级断言）；静态守卫禁止生产
  代码绕过受控入口；维护期 `get_db` 阻塞而非 500、重绑后正常取得
  会话。
- 既有行为保留：备份保留策略（最近 10 份 + protected）、备份
  integrity/0600/原子发布、no-follow 打开、Alembic stamp/upgrade
  语义、启动三路径与失败回滚全部原样通过（141 → 157 项专项无回归）。

### 5. Fix Round 1 — 嵌套受控会话死锁与 bind 失败恢复缺口

Commit: `fix: close database maintenance recovery gaps`

复审发现的两个 Important 问题与修复（均先写失败测试复现）：

**问题 A：`PUT /api/settings/access` 外层会话内二次入闸死锁。** 端点由
`get_db` 持有受控会话（active=1），内部 `load_access_settings()` 又开
一个受控会话：维护屏障关闸时形成循环等待——屏障等外层归零，外层等
内层会话放行（`_sessions_allowed` 只在维护结束后 set），请求挂死。

修复：`load_access_settings(db=None)` 支持复用调用方会话；已持有
`get_db` 会话的唯一调用方 `update_access` 传入 `db`（同事务读，还消除
了读写分叉）；无会话调用方（中间件 `main.py`、登录页 `pages.py`、
`login`、`session_info`）省略参数自行受控获取。生产受控会话全部
调用点复查：`get_db`（端点入口）、`load_access_settings` 默认分支
（无外层会话）、`bootstrap` 孤儿清理（启动期）、调度器选择/逐项
会话（串行不嵌套）——`update_access` 是唯一嵌套点，已消除。

**问题 B：publish 成功后 bind 失败处于恢复盲区。** `database.bind`
在 try 块外：publish 已替换 live 后 bind 抛错 → 异常直接传播、屏障
放行，runtime 停留在未绑定（后续请求 500），且 live 已是新内容而
调用方收到失败——语义撕裂。旧恢复 helper `_rebind_and_restore_live`
只在 live 缺失时用 `sqlite_backup` **直写** live（无 integrity/权限/
sidecar 处理），其余路径仅 bind。

修复：`database.bind` 进 try；`_recover_live_and_rebind` 统一全部失败
路径（发布链失败与 bind 失败同链）：safety 存在时一律从 safety（
dispose 前一致性快照，唯一确定一致的恢复源）经**独立 reserved
staging + integrity + 权限 + 原子 replace** 恢复旧 live 一致内容后
重绑（不直接写 live、不残留旧 sidecar）；safety 缺失（首次 reset 前
live 不存在）时 live 存在即重绑、缺失即抛错。恢复/二次 bind 失败用
`raise 原异常 from 恢复异常` 链回（原异常语义保留、绝不伪装成功）。
维护闸门（maintenance 屏障）覆盖恢复+重绑全程，完成后才放行。

**新增用例（6 个，全部 tmp sqlite）**：
- 外层受控会话在途 + 屏障已关闸（轮询 `_maintenance` 确定性置位，
  非计时碰撞）时 `load_access_settings(req_db)` 直接完成（错误实现
  二次入闸则 3s 超时暴露死锁）；
- restore/reset × bind 失败一次：旧数据（marker）与仅 WAL 已提交
  记录（wal_only，前置证明仅在 WAL）均从 safety 恢复可读、
  integrity ok、runtime 重绑（`engine` 非 None）、屏障放行
  （`_maintenance` False）、原异常 `bind boom` 传播；
- restore/reset × bind 恒败：原异常保留传播、runtime 保持未绑定
  （`engine is None`，不伪装成功）、屏障放行、新会话得到静态
  `not bound` RuntimeError 而非 AttributeError；
- **真实 reader busy smoke（非 mock）**：真实 sqlite3 只读连接
  `BEGIN` + `SELECT` 持读快照 → restore 的 `wal_checkpoint(TRUNCATE)`
  等待 busy_timeout 后返回 busy → fail closed：restore 失败、
  `-wal` 未被清理、仅 WAL 已提交记录仍可读、runtime 重绑原 live。
  证明绕过屏障的外部连接（运维工具/遗留路径）持读时发布链宁可
  失败也不产生「未 checkpoint 就删 WAL」。

**验证**：RED——死锁测试 `TypeError: load_access_settings() takes 0
positional arguments`；bind 失败恢复（restore/reset）`RuntimeError:
database runtime is not bound`（数据未恢复、runtime 不可用）。GREEN——
维护专项 **21 passed**；数据库/备份/迁移/调度/auth/API 专项 **273
passed**；全量 pytest **1166 passed**。

## Slice 4 — launchd restart/enable 事务化（prepare → switch）

Commit: `fix: make launchd restart transactional`

### 1. 问题（原 finding）

`launchd_lib.sh::sc_install_launchd` 的顺序是「先 unload，后配置」：
已加载的 job 在读取/合并/校验任何配置之前就被卸载停机。复现
`SURGE_PORT=abc ./surgeconf.sh restart`（服务运行中）：

1. `launchctl unload` 先停掉正在运行的服务；
2. `sc_write_launchd_env` 把 `SURGE_PORT=abc` **持久化**进控制文件
   （写盘校验只查控制字符，不查端口语义）；
3. `sc_generate_plist` 才发现端口非法，返回失败。

结果：服务已停、控制文件已坏（下次 restart 也起不来）、无任何回滚。
load 或 `launchctl list` 确认失败同样只传播错误码，磁盘上留下与
launchd 状态不一致的新文件；首次 enable 失败会留下半发布的
control/plist/job。旧 plist 为 symlink 时还会被直接覆盖（无检查）。

### 2. 方案（两阶段事务 + 状态机回滚）

**阶段一 prepare（不停机）**：所有可能因配置/生成失败的步骤前置——

- 记录旧状态：旧 control/plist 存在性（symlink 先于存在性检查，
  plist 为 symlink 直接拒绝）、`launchctl list` 精确判定旧 loaded；
- 显式 env 捕获 → 旧架构迁移 → 读旧控制文件 → 回放合并 →
  **端口预检**（`abc` 等非法值在生成任何候选之前失败）；
- 候选 control 与候选 plist 均写入**目标目录内 mktemp 随机名 0600
  文件**（与正式路径同文件系统，rename 原子；可预测名/symlink 跟随
  均不存在：mktemp O_EXCL 独占，mv/rm 只作用于链接本身）；
- 新增 `sc_verify_plist_candidate`：venv Python plistlib 解析候选，
  顶层键白名单 + Label 精确匹配 + `EnvironmentVariables` 键集合
  （凭据键必被拒绝）+ 值控制字符（C0/C1）+ `Umask=63` + Program
  形态。argv 只传候选路径与 label，输出不含任何配置值。

**阶段二 switch（提交点）**：unload 旧 job（失败则正式文件未动，
仅清理候选）→ 同目录 rename 原子切换（旧文件先挪入随机名备份，
候选就位）→ `launchctl load` → `launchctl list` 精确确认。

**回滚（load/确认失败触发）**：尽力 unload 候选 job → 按
`SWITCHED/MOVED` 状态机精确恢复——未动过的文件不碰、挪走的
rename 回原字节、原先不存在的删除新文件（覆盖「旧不存在」情形）→
旧先前 loaded 则重新 load 并以精确 list 确认 → 清理全部候选/备份。
回滚 load 也失败：输出人工恢复指引（`launchctl load` 命令）、
保留已恢复的旧文件、明确非零，绝不伪装成功。备份恢复失败的
极端场景同样保留随机名备份文件供人工处置。

语义保持：restart 未提供键保留持久值、显式空值清除回默认、
凭据只存 0600 控制文件、plist 永远只含定位三键、Umask 63、
0600/0700 权限收敛、`set -u` 安全、无 `eval`。

### 3. 技术依据

- **rename(2) 原子性**：候选/备份与正式路径同目录，`mv` 退化为
  rename，读者要么看到旧文件要么看到新文件，无部分写状态；
- **launchctl 语义**：`unload <path>` 按路径卸载（异路径加载的
  job 卸载失败即传播失败，不产生半卸载状态）；`launchctl list
  <label>` 精确 exit status 是加载判定，第一列 PID 才是运行中；
- **plistlib**：stdlib XML plist 解析对坏结构/坏 XML 抛异常；
  C1 区（0x7F–0x9F）中 NEL(0x85) 可通过 XML 解析，须在值级显式
  拒绝（与 `sc_assert_no_control` 同区间，纵深防御）。

### 4. 验证证据

测试文件：`tests/test_launchd_scripts.py`（新增 11 个用例，全部
fake launchctl PATH 注入，零真实 launchd 操作）。fake launchctl
扩展 `FAKE_LAUNCHCTL_FAIL_ONCE`（load/unload 各支持一次失败）与
`FAKE_LAUNCHCTL_LIST_FAIL_AT`（第 N 次精确查询失败）两类注入。

- **RED（实现前）**：9 failed / 2 passed——非法端口（unload 已发生、
  控制文件已持久化坏值）、控制字符注入（unload 已发生）、data dir
  阻挡（unload 已发生）、symlink plist（旧实现竟然 returncode=0
  直接覆盖）、load 失败回滚（state 丢失、无第二次 load）、确认失败
  回滚（仅 1 次 load、无回滚消息）、回滚 load 恒败（无人工指引）、
  enable 半发布（control/plist 残留）、验证函数（不存在，127）；
  unload 失败与未 loaded 两例为行为保护（新旧实现均通过）。
- **GREEN（实现后）**：launchd 专项 + 开源安全专项 **117 passed**；
  全量 pytest **1177 passed**。
- 覆盖矩阵：非法显式值（端口/控制字符/不可创建目录/symlink plist）
  全部在 unload 前失败且正式文件字节与 loaded 状态零改动、无候选
  残留；load 失败（fail-once）→ 旧字节恢复 + 旧 job 重新加载确认 +
  两次 load + 回滚消息 + 目录无残留；确认失败（第 2 次查询注入）→
  同上完整回滚；unload 失败 → 字节不变、候选清理、job 保持 loaded；
  未 loaded → 不调 unload 直接切换成功；回滚 load 恒败 → 非零 +
  旧文件保留 + 人工指引 + 两次 load 均失败；首次 enable 失败 →
  无 control/plist/job 半发布；候选验证函数契约（未知顶层键/错
  Label/凭据键/缺环境键/C1 控制字符/坏 XML 拒绝，合法产物通过）。
- 既有行为保留：键更新语义（unset 保留/显式空清除）、凭据不进
  plist、迁移、status 精确判定、console/结构化日志分离等 29 项
  原用例全部通过（106 → 117 项专项无回归）。
- 开源安全豁免登记随测试文件变更更新（新增行为审查：仅合成占位
  凭据的 dict 索引赋值一处，无真实数据）。

### 6. Fix Round 1 — 回滚恢复条件/候选存活判定/切换窗口信号保护

Commit: `fix: guarantee launchd rollback recovery`

复审发现的四个 Important 问题与修复（均先写失败测试复现）：

**问题 A：回滚恢复条件以 SWITCHED 为准，遗漏「已挪备份、候选 mv
失败」窗口。** 旧文件 `mv` 进随机名备份（MOVED=1）后候选 mv 失败
（SWITCHED=0）时，回滚不恢复备份，且 `sc_discard_candidates` 把备份
一并删除——**旧配置字节永久丢失**。备份恢复 mv 失败时仅输出备份
路径、无具体恢复指令。

**问题 B：回滚 unload 失败被 `|| true` 吞掉。** 候选 load 成功但
list 确认失败、回滚 unload 又失败时，同 label 候选可能仍在
launchd 中，旧实现照常重新 load 旧 job 并输出「已回滚」——伪称
job 状态恢复；enable 场景（旧未加载）同样不提示候选可能仍 loaded。

**问题 C：切换窗口无信号保护。** 从 unload 到提交之间收到
TERM/INT，bash 直接死亡，可能停在「旧文件已挪走、候选已就位、
旧 job 已停」的半切换态（测试实测 TERM 后 returncode -15、
after-load 场景 INT 完全被吞 rc=0）。

**问题 D：fake launchctl load 对缺失/非普通 plist 假成功。** 回滚
「重新加载」在旧 plist 不存在时仍被记为成功，掩盖真实 launchd 会
失败的事实。

修复（`launchd_lib.sh`）：

- **回滚以 MOVED 为准**：挪进备份的旧文件无条件 rename 回正式
  路径（含候选 mv 失败、尚未 SWITCHED 的窗口）；仅未 MOVED 且候选
  已就位（原先不存在）才删除新文件；恢复成功才清空备份记录。
  恢复 mv 失败保留 0600 备份，输出逐条 `mv "<备份>" "<目标>"`
  人工指令（仅路径，不含任何配置值），明确非零。
- **候选存活判定**：回滚 unload 失败不可忽略——再用精确
  `launchctl list <label>` 区分「候选仍加载」（list 成功 → 同
  label 冲突使自动重载不可信，标记不完整，输出人工 unload+load
  指引；旧未加载场景亦明确候选可能仍 loaded）与「路径本来未加载」
  （list 失败 → 视为干净，避免真实 launchctl 对未加载路径报错的
  保守误报）。
- **子 shell + trap 事务保护**：`sc_switch_launchd` 主体整体放入
  `( )`，EXIT/HUP/INT/TERM trap 只在子 shell 生效（不破坏调用方
  trap）。信号到达 → `sc_signal_rollback` 幂等回滚一次后以
  128+signum 退出；EXIT 兜底在未提交（`SC_COMMITTED`）时自动幂等
  回滚且不改写原退出码；正常提交先标记 commit 再拆 trap。
  幂等性由 `SC_ROLLBACK_DONE` + 状态清零双重保证；无 `eval`
  （trap 仅调用固定函数名）。
- **fake launchctl 加固**：load 必须检查 plist 存在/非 symlink/
  普通文件/可读，否则 exit 1；成功时把 loaded 路径写入 state。
  新增一次性信号注入（after-unload / before-load / after-load 三
  个时点，可配 TERM/INT）与 unload 计数失败（`UNLOAD_FAIL_AT=N`）。

**新增用例（9 个，全部 fake launchctl，零真实 launchd）**：
- control/plist 候选 mv 失败（prepare 后删候选模拟）：备份按 MOVED
  恢复旧字节、旧 job 重新加载、无残留（旧实现下旧文件丢失，
  FileNotFound 复现）；
- 备份恢复 mv 失败（目录 chmod 500）：RC=1、0600 备份保留且字节
  完整、输出含 `mv "` 人工指令；
- 确认失败 + 回滚 unload 失败：磁盘恢复但输出「不完整」+ 人工
  unload/load 指引、非零（旧实现输出「已回滚」伪装完整）；
- enable（旧未加载）候选 load 成功确认失败 + 回滚 unload 失败：
  磁盘恢复 + 明确候选可能仍处于加载状态；
- 信号三时点：TERM@after-unload（无文件操作回滚+重载，rc=143）、
  TERM@before-load（MOVED 恢复+重载，rc=143）、INT@after-load（停
  候选+恢复+重载，rc=130）——旧实现实测 -15/0；
- fake load 加固契约：缺失 plist 拒绝、不建 state。

**验证**：RED——8 failed / 1 passed（fake 加固契约随测试侧先行
改造直接 GREEN）。GREEN——launchd 专项 **49 passed**；launchd +
开源安全专项 **126 passed**；全量 pytest **1186 passed**。

### 7. Fix Round 2 — 入口 PID 信号边界 / mv 窗口原子性 / 提交顺序

Commit: `fix: make launchd signal handling atomic`

复审发现的三个 Important 问题与修复（先写失败测试复现）：

**问题 A：trap 只装在 sc_switch_launchd 的子 shell，真实入口 PID
无保护。** 用户对 `surgeconf.sh`（入口进程）发 TERM/INT 时，入口
进程在等待子 shell，自身无 trap——默认死亡，退出码 -15；子 shell
的 trap 与入口进程的生命周期脱钩。

**问题 B：mv 完成与 MOVED/SWITCHED 状态赋值之间存在信号窗口。**
四个切换 mv（control/plist 旧→备份、候选→正式）任一完成但状态尚未
置位时信号到达，回滚按旧状态判断——`MOVED=0` 时不恢复备份且
`sc_discard_candidates` 把**唯一备份删除**（实测 control 场景旧文件
彻底丢失，FileNotFound）。

**问题 C：提交顺序颠倒。** 原实现 `sc_discard_candidates` 先于
`SC_COMMITTED=1` 执行，且信号 handler 不查提交标记——确认成功
瞬间的信号会把**已确认发布的新 job 卸载**并回滚旧文件（实测
after-confirm 注入下 plist 被换回旧字节）。

修复（`launchd_lib.sh`）：

- **事务在当前进程（入口 PID）执行**：去掉子 shell，`sc_switch_launchd`
  直接在调用进程（真实入口 `surgeconf.sh` 的 PID）内装 trap 执行
  事务；事务边界契约：接管 EXIT/HUP/INT/TERM 并在所有出口（成功/
  失败/信号）拆除恢复默认——本仓库入口均无自定义 trap，不受影响。
  fake 的 `$PPID` 现在命中的就是入口 PID 本身。
- **临界区原子化四个 mv + 确认**：`sc_critical_enter` 把四信号改为
  仅记录 `SC_PENDING_SIGNAL` 并摘除 EXIT 兜底；mv 返回后调用方先
  完成状态赋值，`sc_critical_leave` 再恢复回滚 handler 并处理
  pending——回滚读到的永远是已更新状态，不会删唯一备份/留半文件。
  「确认 list 成功 + 置 `SC_COMMITTED=1`」同样在临界区内完成。
- **提交顺序修正**：确认成功即置 `SC_COMMITTED=1`（临界区内）→
  拆事务 trap → `sc_discard_candidates` 清理。`sc_signal_rollback`
  尊重提交标记：已提交时不回滚、不卸载新 job，仅清理临时备份后以
  128+signum 退出；拆 trap 后清理窗口的信号最多留下 0600 临时备份，
  不破坏已发布状态。`sc_exit_rollback` 同样查提交标记，保留原退出
  码且幂等一次，正常完成不触发。

测试设施（fake 增强）：

- fake mv（PATH 注入，透传 /bin/mv）：`FAKE_MV_SIGNAL=to-backup/
  to-final` 在「目标匹配的 mv 已完成、返回前」向父进程（入口 PID）
  一次性发 TERM/INT——精确制造 mv↔赋值窗口；
- fake launchctl list 新增 after-confirm 注入点（以 load-mark 区分
  确认与探测），load 成功落 load-mark。

**新增用例（3 个）**：

- TERM@旧control→备份 mv 完成后：唯一备份不被删、旧字节恢复、
  旧 job 重载、rc=143、无残留（旧实现下 control 永久丢失复现）；
- INT@候选plist→正式 mv 完成后：同上原子回滚、rc=130；
- TERM@确认成功瞬间：**新配置保持发布**（plist/control 均为新值、
  新 job 不被卸载）、rc=143、无残留（旧实现下新 job 被卸载、
  plist 换回旧字节复现）。

**验证**：RED——2 failed / 1 passed（to-backup 丢备份、after-confirm
错误回滚；to-final 在旧实现下碰巧通过，作为行为保护）。GREEN——
launchd 专项 **52 passed**；launchd + 开源安全专项 **129 passed**；
全量 pytest **1189 passed**。既有信号三时点测试（after-unload/
before-load/after-load）在新实现下语义升级为「真实入口 PID 持有
trap」，无需改动即通过。

### 8. Fix Round 3 — 四个 rename 信号窗口的逐一测试证据

Commit: `test: cover every launchd rename signal window`

Important（测试证据缺口）：fake mv 注入只有粗粒度
`to-backup`/`to-final`（按目标名模式命中**任一**文件），无法证明
四个切换 rename 窗口（control/plist × 旧→备份/候选→正式）**各自**
被信号用例覆盖——尤其 control-final 用例中后续 plist-final 的 mv
可能误消耗唯一一次注入，使「通过」并不代表该窗口被测到。

修复（纯测试侧，生产实现无改动）：

- **fake mv 精确四标签**：`FAKE_MV_SIGNAL` 改为
  `control-backup`/`control-final`/`plist-backup`/`plist-final`，
  基于源/目标 basename 与 candidate/backup 模式双重判定（如
  control-final = 源匹配 `.launchd.env.candidate.*` 且目标为
  `launchd.env`）；回滚恢复 mv（备份→正式）不匹配任何标签，不会
  误触发。命中时把标签写入 marker 文件（一次性 marker 保留）。
- **参数化四用例**（真实入口 PID 发 TERM/INT 各半，期望 143/130）：
  各自断言 marker 内容==所选窗口（注入点确实命中且仅命中——同时
  证明 control-final 之后 plist-final 等后续窗口未消耗注入）、退出
  非零、唯一备份不被删、旧 control/plist 字节与 loaded 状态恢复、
  目录无临时残留。
- 旧 `to-backup`/`to-final` 两用例由参数化版替代；after-confirm
  提交窗口用例保留。

**验证**：launchd 专项 **54 passed**（含四窗口参数化各一）；
launchd + 开源安全专项 **131 passed**；全量 pytest **1191 passed**。
（生产实现零改动——四窗口在 Fix Round 2 的临界区实现下全部原子。）

### 9. Fix Round 4 — final 窗口回滚必须依赖 SWITCHED（首次 enable 场景）

Commit: `test: prove launchd final rename rollback state`

Important（测试证据缺口）：Round 3 的四窗口参数化用例统一预建
「旧 control/plist + loaded」状态——control-final/plist-final 窗口下
即使对应 rename 的临界区保护被移除（mutation），信号在 SWITCHED
赋值前触发回滚时 `MOVED=1`（旧文件已在备份），回滚凭备份照样恢复
旧字节，用例依旧通过。final 两例无法捕获「临界区移除」这一回归。

修复（纯测试侧，生产实现无改动）：

- **backup 两例保持原场景**（`test_mv_signal_backup_window_restores_moved`）：
  旧文件 + loaded 状态预建，验证回滚按 MOVED 恢复备份字节——这是
  backup 窗口的正确断言对象。
- **final 两例改用首次 enable 场景**（`test_mv_signal_final_window_
  first_enable_rollback`）：正式 control/plist 原先都不存在、旧 job
  未加载（无 MOVED 备份可依赖）；fake mv 完成 candidate→final 后、
  SWITCHED 赋值前向真实入口 PID 发 INT——回滚必须依赖对应 SWITCHED
  删除刚就位的文件。断言退出码 130、marker 标签精确命中、control/plist
  最终仍不存在、job 未 loaded、候选/备份目录零残留。
- 四标签精确选择（marker 断言证明后续窗口未消耗注入）与 after-confirm
  提交窗口用例保留不变。
- `tests/test_open_source_safety.py` 的 launchd 文件规则 4 豁免按
  设计随文件变更重新审查后更新 SHA-256（新增代码仅路径/退出码/marker
  断言，无凭据赋值）。

**mutation 自检（未提交，仅验证捕获能力）**：逐一临时移除
`launchd_lib.sh` 中 control-final / plist-final 对应 mv 的
`sc_critical_enter`/`sc_critical_leave`——对应用例必须红（官方
`launchd.env` / `com.surgeconf.plist` 残留在正式路径，断言
`not exists()` 失败），另一用例不受影响；还原临界区后全绿，
`git diff launchd_lib.sh` 为空。

**131 passed**；全量 pytest **1191 passed**。

### 10. Fix Round 5 — readiness 提交门（loaded ≠ 可服务）

Commit: `fix: commit launchd switch only after readiness`

Important（整分支复审 finding）：`launchctl list` exit 0 只证明 job
已 loaded，第一列 `-` 代表进程未运行——应用因数据目录网络卷/WAL/
迁移/端口绑定失败而未真正监听时，切换仍置 SC_COMMITTED 并报告成功。

**应用侧最小 readiness 端点**（`app/routers/health.py`）：
`GET /api/health/ready` → 204、无 body、无版本/配置；唯一门是
lifespan 在 yield 前置位、清理边界进入即复位的
`app.state.service_ready`（启动未完成/失败/清理期一律 503 fail
closed）。访问矩阵公开该路径（GET 安全方法，无 cookie/CSRF 要求——
`lan_authenticated` 下 launchd 本机 curl 才能探测；其他 API 路径
不因此放开）；`local_only` 非回环仍在中间件被 403。

**launchd 确认轮询**（`launchd_lib.sh::sc_wait_candidate_ready`）：
load 成功后有界 poll——精确 list 首个 tab 字段必须数字 PID（`-`
继续等待，覆盖首次迁移/冷启动的 PID 延迟；数字后回 `-` 视同进程
退出立即失败；list exit 非 0 立即失败），PID 数字时以
`curl --fail --silent --show-error --noproxy '*' --max-time <短超时>`
直连 `http://<归一host>:<port>/api/health/ready`。host 归一
（`sc_readiness_host`）：127/0.0.0.0→127.0.0.1、`::`→`[::1]`、
其他 IPv6 加括号、LAN 地址原样；参数全部引用、URL 无凭据。总时限
`SC_READINESS_TIMEOUT` 默认 60s，测试可注入缩短。

**信号安全（关键实现细节）**：launchctl 输出解析绝不用命令替换——
bash 命令替换执行期间收到信号会污染退出码（128+signum）且 trap
赋值在替换子上下文丢失（实验证实），会破坏临界区 pending 语义；
改为 prepare 阶段 mktemp 的 PID 探测文件（`SC_LIST_PROBE`，随
`sc_discard_candidates` 清理）+ `read` 解析。提交临界区保持
「最终 PID 观测 + SC_COMMITTED 置位」原子，`after-confirm` 信号
注入点迁移到「readiness 通过后的最终确认」查询（curl-ok 标记门控），
尊重提交的既有断言原样通过。

**端口严格化**：prepare 预检从「仅数字」收紧为 1..65535——0/65536
在 unload 之前拒绝，绝不因已知必失败配置停服。数据目录网络卷/WAL
等应用层故障由「进程起不来 → readiness 超时/退出」触发既有幂等
回滚，不在 shell 复制 Python 挂载判定。

**测试（RED→GREEN）**：应用侧 5 用例（local_only 回环 204 空 body/
非回环 403、lan_authenticated 无 cookie 204 且其他 API 仍 401、
端点 state 标志门缺失/False→503、未完成 lifespan 非 204）；launchd
侧 9 用例（loaded 无 PID 超时回滚且 curl 零调用、数字 PID+readiness
恒败回滚且有界多次重试、延迟 PID+延迟 ready 最终提交、PID 出现后
退出立即回滚——实测 <6s vs 时限 8s、端口 0/65536 unload 前拒绝、
enable readiness 失败零残留、host 归一五形态、curl argv 契约含
0.0.0.0→127.0.0.1 归一与无凭据）。fake 基建：launchctl load 成功
即进程运行（state=running），新增 NORUN/PID_FROM/PID_UNTIL 注入；
fake curl 默认成功、记录完整 argv、支持延迟成功/持续失败、绝不
联网。mv 四信号窗口与 after-load/after-unload 等既有用例零回归。

**验证**：launchd/auth/API/安全专项 **255 passed**；全量 pytest
**1246 passed**（Slice 4 Round 4 基线 1232 + 新增 14）。开源安全
豁免（launchd 测试文件规则 4）随文件变更复审后更新 SHA-256。

**未验证限制声明**：以上为代码层自动确认（fake launchd/curl 全覆盖，
含信号临界区与回滚路径）；真实 launchd 上的切换 smoke（KeepAlive
崩溃循环下的 readiness 超时回滚、真实 curl 对 `/api/health/ready`
的端到端探测）尚需在物理 Mac 执行 `./surgeconf.sh restart` 人工
验证。

### 11. Fix Round 6 — 绑定真实 job PID（launchctl 真实输出格式 + readiness PID 一致性）

Commit: `fix: bind launchd readiness to real job pid`

**Ruling（决策记录）**：这是 Round 5 之后的 load-bearing 阻断，而非
可延后的改进——Round 5 的 readiness 提交门在真实 macOS 上从不生效
（阻断 1 使其永远超时回滚，`restart/enable` 对真实用户恒失败），
且提交门本身存在「任意数字即放行」的绕过（阻断 2）。选择**最小兼容
修复**而非停车重做：保留两阶段事务/回滚状态机/信号临界区全部既有
语义，只替换 PID 观测来源并给提交门补一道 PID 一致性检查；所有
signal/mv 四窗口/端口/首次 enable 回滚用例零改动通过。真实 Mac
仍待物理 smoke（见文末）。

**阻断 1（PID 来源错误）**：Apple launchctl(1)（apple-oss-distributions/
launchd, man/launchctl.1）明确：`launchctl list` **无参数**时以三列
表格输出全部已加载 job（第一列 PID、第二列 last exit status、第三列
label，tab 分隔）；`launchctl list <label>` 打印的是该 job 的**详情
字典**（首行 `{`，old-style plist，运行中才有 `"PID" = <数字>;` 行）。
旧实现对带 label 输出取「首行第一个 tab 字段」——真实格式下永远解析
出 `{`（非数字），readiness 只能超时。修复：
loaded 判定继续用 `launchctl list "$label"` 的 exit status；PID 读取
改为无参数 `launchctl list` 重定向到 0600 探测文件（`SC_LIST_PROBE`，
prepare 阶段 mktemp O_EXCL，随 `sc_discard_candidates` 清理），由
`sc_parse_list_pid` 逐行 `read` 解析（无 grep/awk/命令替换，保留信号
pending 语义）：header 行字面跳过、第三列**精确等于** label 才匹配、
第一列仅接受全数字或 `-`（loaded 未运行）、匹配行数必须恰为 1——
0 行=未加载，>1 行（重复 label）/第一列畸形/空第一列一律 fail closed。
`surgeconf.sh status` 的运行中判定同根因，改为复用同一解析
（`sc_query_job_pid`，临时探测文件即用即删；解析异常 fail closed
明确报错 exit 1，不猜测状态）。

**阻断 2（提交门未绑定 readiness 时的进程）**：PID A readiness 通过
后、最终确认前 KeepAlive 换成未 ready 的 PID B，旧实现
`sc_candidate_running` 只验证「当前是数字」即置 SC_COMMITTED。修复：
`sc_wait_candidate_ready` 在 curl 成功时记录 `SC_READY_PID` 并**立即
复核**——当前 PID 必须仍是刚探测的那个进程；不一致（A→B）则清除
READY_PID 并在同一总 deadline 内对新 PID 重新执行 readiness，再次
「curl 成功 + 复核一致」才算稳定确认；复核发现退回 `-` 视同退出立即
失败。提交临界区内 `sc_candidate_running` 收紧为「当前 PID ==
SC_READY_PID」——仅数字不够，必须是同一个进程；不一致走既有幂等
回滚。进程退回 `-`/消失的继续/失败策略沿用 Round 5 既有有限语义。

**测试（RED→GREEN）**：fake launchctl 对齐真实格式——带 label 查询
输出详情字典（含 `"PID" = 12345;` 行），无参查询输出三列表（header +
`com.apple.dock`/anonymous 干扰行 + com.surgeconf 行），并新增
`PID_SWITCH_AT`（无参查询第 N 次起 A=12345→B=54321）、`ALL_PID`/
`ALL_EXTRA`（强制 PID 列/追加畸形与重复行）注入；fake curl 记录
`pid=<关联PID>` 前缀并支持 `READY_PIDS`（仅指定 PID 可服务）。RED
证据（旧实现 + 新 fake）：真实格式端到端用例 8s 超时回滚、两个换进程
用例与单元矩阵全部失败（`sc_parse_list_pid` 尚不存在 → RC=127）。
GREEN（新实现）：真实格式最终提交（无参查询 ≥3 次：轮询+复核+最终
确认）；A curl 成功后换 B 且 B 未 ready → 同一 deadline 内重试 B 至
超时、回滚旧字节并恢复旧 loaded；复核后临界区再换 B → 当前 PID ≠
READY_PID 拒绝提交并回滚（curl 仅 A 一次）；A→B 且 B 随后 ready →
仅在 B 上再次成功后才提交（curl 两次，末次关联 54321）；详情字典
伪造 PID 行 + 无参表格 `-` → 不误判、curl 零调用；解析矩阵覆盖
重复 label/畸形第一列/两列行/空文件/无 header。after-confirm 信号
注入点随实现迁移至「readiness 通过后的第 2 次无参查询」（第 1 次
=复核、第 2 次=临界区最终确认），`test_signal_after_confirm_
respects_commit`（提交被尊重、退出 143）原样通过；signal/mv 四窗口/
端口/首次 enable 回滚等既有用例零改动通过。

**验证**：launchd 专项 **69 passed**（61 + 8 新增；含 detail-dict
防御用例）；安全专项与全量见提交说明。开源安全豁免 SHA-256 随
测试文件变更同步更新。

**未验证限制声明**：以上为代码层自动确认（fake launchctl 输出对齐
Apple 手册描述的格式；未调用真实 launchctl/真实服务）。真实 Mac 上
的物理 smoke——`./surgeconf.sh restart` 后以真实 `launchctl list`
三列表核对 com.surgeconf 行 PID、KeepAlive 崩溃循环下的超时回滚、
真实 curl 对 `/api/health/ready` 的端到端探测——仍待人工执行。

## Slice 5 — 拒绝非本地数据库文件系统 + WAL 生效验证

Commit: `fix: reject unsafe database filesystems`

### 1. Finding

- `runtime_paths` 仅校验绝对路径/symlink/权限：`SURGECONF_DATA_DIR`
  可指向 SMB/NFS 挂载——SQLite WAL 依赖同卷原子 rename 与 mmap
  一致性，网络卷上会静默损坏数据库。
- `DatabaseRuntime.bind` 的 connect hook 执行
  `PRAGMA journal_mode=WAL` 但**不读取返回值**：SQLite 在网络/受限
  卷上会静默降级为 delete/memory，服务带着错误日志模式照常启动。

### 2. 修复（生产实现）

**本地挂载守卫（`app/runtime_paths.py`）**：

- 平台探针封装为可注入小函数 `_probe_mount`（monkeypatch 即可模拟
  任意平台结果）：
  - **Darwin（产品目标）**：ctypes 定义 `struct statfs64`（LP64 布局，
    挂载标志为 `f_flags` 低 32 位 + `f_flags_high` 高 32 位拼成的
    64 位），调 libc `statfs64` 后以内核权威挂载标志 `MNT_LOCAL`
    （0x1000，sys/mount.h）判定。返回码非 0、`f_fstypename` 空/
    不可解码、`f_mntonname` 不以 `/` 开头（结构布局不符特征）一律
    RuntimeError fail closed，绝不把读错的标志当权威结果。
  - **非 Darwin（测试环境）**：`/proc/mounts` 最长挂载点前缀匹配 +
    已知网络 fs 类型表（nfs/cifs/smbfs/afpfs/lustre/ceph/sshfs 等），
    黑名单未列名类型默认本地（文档声明产品目标 macOS，此路径仅
    服务测试）；读取失败或无覆盖条目同样 fail closed。
- `assert_local_filesystem(directory)`：目录不存在时对最近现存父目录
  探针（其挂载即未来目录的挂载）；错误文本只含 fs 类型名，不含
  绝对路径。
- 接入点（任何迁移/备份/数据库打开前）：
  `ensure_runtime_directories` 进入即探针 + 全部创建后对 root 复核
  （lifespan 与 `prepare_database` 的共同入口）；
  `refresh_runtime_permissions` 经 ensure 复核（备份/恢复/重置/列表
  的操作级入口，防启动后被重定向）；`assert_sqlite_database_safe`
  开头对 `path.parent` 断言（raw/staging/backup 源的最终验证，含主
  文件尚不存在的全新路径）。symlink/no-follow 原语不动、不回归。

**WAL 生效验证（`app/database.py`）**：

- PRAGMA 逻辑提取为模块级 `apply_connection_pragmas(dbapi_conn)`：
  请求 `journal_mode=WAL` 后**读取返回值**，严格归一（去空白、转
  小写）等于 `wal` 才放行；否则关闭 cursor 抛固定 RuntimeError
  （消息只含 reported 值，不含路径/参数），`synchronous`/
  `busy_timeout`/`foreign_keys` 绝不带着错误日志模式执行。
- raw 备份落盘（`backups._finalize_backup_file`）的
  `journal_mode=DELETE` 请求同样验证返回（备份文件必须独立可读）。

### 3. 测试（RED→GREEN，`tests/test_local_filesystem_guard.py` 29 例）

全用 tmp + monkeypatch 注入，不操作真实挂载：

- **A 挂载守卫**：local flag 缺失在目录/任何文件产生前拒绝且错误
  不含绝对路径；探针错误 fail closed；缺失目录探针最近现存父
  （断言探针参数==tmp_path）；创建后复核捕获翻转（断言第二次探针
  参数==root 且目录已建）；`refresh` 拒绝启动后重定向；
  `assert_sqlite_database_safe` 对全新路径同样要求本地父目录；
  `prepare_database`/`backup_database` 拒绝时零产物（无 db/backup/
  staging/tmp 残留）。
- **B Darwin 探针**：真实 `statfs64` 冒烟（tmp 在本地 apfs，
  local=True、类型名非空）；返回码非 0 与全零缓冲区（结构异常）
  各自 fail closed。
- **C /proc/mounts 策略**：最长前缀匹配（nfs 下非本地、更深 ext4
  bind mount 覆盖为本地、smbfs/afpfs 拒绝）；不可读/无覆盖条目
  fail closed。
- **D WAL 验证**：fake cursor 参数化模拟返回 delete/memory/
  truncate/persist/off/None/空行——一律关闭 cursor、RuntimeError、
  后续 PRAGMA 未执行；`WAL`/` wal ` 归一后放行且四条 PRAGMA 完整；
  真实连接端到端确认 hook 后实际模式==wal（raw 打开复核）。
- **E raw 备份**：DELETE 请求返回 wal 时拒绝；正常路径确实收敛为
  delete 模式。
- **F startup 传播**：非本地数据目录/WAL 拒绝两种场景 lifespan 均
  传播失败、`database.engine is None`（不宣告 ready）、零 live/
  staging 残留。

### 4. 验证与依据

- **API 依据**：`statfs64`/`MNT_LOCAL` 来自 Apple `sys/mount.h`；
  SQLite `PRAGMA journal_mode` 返回实际生效模式（网络 fs 上请求 WAL
  会降级返回原模式）是 SQLite 文档化行为。
- **验证**：新专项 29 passed；runtime/database/migration/backup/
  migrations 专项 148 passed；全量 pytest **1220 passed**（基线
  1191 + 新增 29）；tmp 本地目录真实 WAL smoke 通过（全新目录
  `SURGECONF_DATA_DIR` 下 prepare→bind 后 raw `PRAGMA journal_mode`
  == wal、-wal/-shm 伴生就位）。

### 5. Fix Round 1 — verified bind 与守卫收口（5 项 Important）

Commit: `fix: verify WAL before publishing database runtime`

Important 1（bind 暴露未验证引用）：`bind` 原为同步「创建 engine 即
成功」——`session_factory` 在 WAL hook 实际执行前就可被使用，
`database_ready` 也可能在未验证前记录。改为 **async verified bind**：
局部创建 engine + 注册 hook 后**实际 checkout 一次连接**（真实
SQLAlchemy/aiosqlite 路径，非 raw sqlite 旁路），hook 全部成功才原子
发布 engine/path/session_factory；失败 dispose 局部 engine 并抛错，
runtime 保持未绑定或维持旧绑定。LSP 迁移全部调用点：

- 生产：`bootstrap.lifespan`、`backups._recover_live_and_rebind`/
  `restore_backup`/`reset_database`（后两者原本就在 try 内，失败照常
  走 safety 恢复链）、`database_migration.upgrade_staging`。
- 测试：`test_runtime_paths`、`test_api_endpoints`（client fixture、
  standby/order/disposal 三脚手架）、`test_auth`、`test_udp_api`（含
  rebind 用例转 async）、`test_udp_ui`、`test_profile_tokens`、
  `test_config_validation`、`test_subscription_status_api`、
  `test_backups`、`test_database_maintenance`、
  `test_subscription_scheduler`、`test_local_filesystem_guard`。
- 附带语义收口：fixture 预建 runtime 改为「`prepare_database` 先行
  再 create」（verified bind 现在会真实建库文件，空库会干扰 lifespan
  的 schema 判定——预建完整库与生产链一致）；
  `test_lifespan_prepares_database_before_binding_live_runtime` 的
  bind 替身同步改 async。

Important 2（逐目录复核）：`ensure_runtime_directories` 创建后对
root/backups/exports **逐一** `assert_local_filesystem`（各自可能是
独立挂载点）；操作级入口经 `refresh_runtime_permissions` 自动覆盖
目标目录。新增测试：root 本地、backups/exports 模拟网络挂载时拒绝；
root 本地、backups 为网络挂载时备份在任何 tmp/target 产生前拒绝。
文档明确日志目录不在数据库挂载守卫范围（不误称数据库保障）。

Important 3（非 ASCII 挂载点）：Darwin `f_mntonname` 不再 ASCII
解码——原始字节 NUL 截断后验证首字节 `/`（`/Volumes/数据` 等 UTF-8
卷名合法）；`f_fstypename` 仍按内核约定 ASCII。新增伪造缓冲区测试
证明非 ASCII 挂载点不再被误拒。

Important 4（cursor 异常关闭）：`apply_connection_pragmas` 全部
execute/fetchone/后续 PRAGMA 纳入 try/finally——任意步骤异常都恰好
close 一次。新增参数化测试（execute1/fetchone/execute2/execute4
四故障点）。

Important 5（平台冒烟边界）：真实 Darwin statfs 冒烟加
`skipif(sys.platform != "darwin")`；新增 `_probe_mount` 调度单元测试
（两平台路径均以注入替身验证，绝不触发 Darwin ABI）。

**验证**：guard 专项 **41 passed**（29 + 12 新增/增强）；受影响专项
（runtime/maintenance/migration/backups/migrations/udp_api/auth/
profile_tokens/config_validation/status_api/scheduler/udp_ui/
api_endpoints + guard）**510+56 passed**；全量 pytest **1232 passed**
（Slice 5 基线 1220 + 新增/增强 12）；tmp 本地目录真实 WAL smoke
通过（verified bind 后 raw `PRAGMA journal_mode` == wal）。
