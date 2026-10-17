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
