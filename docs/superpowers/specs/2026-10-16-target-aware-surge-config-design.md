# SurgeConf 目标感知配置与运行数据设计

**状态：** 已批准，待实施计划
**日期：** 2026-10-16
**适用版本：** Surge Mac 6.6.0（Build 11270）及以上；Surge iOS 5.14.6 及以上

## 1. 背景

SurgeConf 当前以单一生成路径输出 Surge 配置，无法严格区分工作电脑、家庭网关和手机三个运行目标。配置方案虽然保存了 `profile_id`、`final_action` 等作用域字段，但生成器没有完整执行这些语义：方案专属规则和 Host 可能进入其他方案，`final_action` 未决定最终规则，Mac 与 iOS 专属 General 参数也混在同一产物中。

UDP 策略同时存在安全与兼容冲突。全局 UDP/443 或 STUN 阻断可降低 QUIC、WebRTC 泄漏风险，但也会阻断依赖 ICE/STUN 的实时视频、摄像头和 P2P 应用。不同目标平台具备不同的精细匹配能力，必须按平台和档案生成规则，而不是继续使用一组全局规则。

运行数据库虽然被 Git 忽略，但仍位于源码仓库内，导致服务状态与代码目录、worktree 和升级流程耦合。数据库还缺少显式 schema 版本、外键启用、并发读写策略、迁移前备份和严格文件权限。

## 2. 目标

1. 一套代码支持三种配置目标：
   - 工作电脑上的 Surge Mac；
   - 家庭网关上的 Surge Mac Gateway VM；
   - Surge iOS 5.14.6 及以上。
2. 工作电脑与家庭网关分别运行独立 SurgeConf 实例和独立数据库。
3. 在设置中标记当前实例角色，并在每个配置方案上声明目标平台。
4. 严格执行全局与方案专属数据的作用域，企业配置不得进入个人或家庭产物。
5. 保持 AI、国内应用、流媒体等公共分流能力，并用顺序不变量保证 AI 服务优先命中正确策略。
6. 使用目标感知 UDP 模式，在保持必要 WebRTC 防护的同时恢复合法实时视频。
7. 将数据库迁至 macOS Application Support，提供可回滚的原子迁移、备份和 schema 管理。
8. 管理 API 与只读 Profile 订阅使用不同访问边界，禁止未认证读取敏感配置。
9. 所有代码、文档、测试和提交信息满足开源仓库脱敏要求。

## 3. 非目标

1. SurgeConf 不通过 Profile 参数开启或关闭 Surge Gateway VM；该功能由 Surge Mac 界面管理。
2. 第一阶段不自动控制 Surge 6.6 的逐设备 UDP Fast Path 开关。当前版本的 CLI 不提供对应设备管理命令。
3. 不在设备之间同步或合并 SQLite 数据库。
4. 不使用 Git、worktree、iCloud 源码目录或网络文件系统承载运行数据库。
5. 不引入 SQLCipher，也不把每个代理节点凭据拆分到 Keychain。先通过目录权限、认证边界和 API 脱敏保护整个数据库。
6. 不使用 Surge `#!include` 作为首期配置分层机制。分层由 SurgeConf 生成器完成，避免远程文件依赖和 UI 编辑限制。

## 4. 开源脱敏基线

仓库内永久禁止出现以下真实数据：

- 用户名、绝对用户目录、主机名和设备序列信息；
- 真实局域网地址、MAC 地址和设备名称；
- 企业域名、内部网段、企业 DNS 和组织名称；
- 订阅商名称、订阅 URL、节点地址和节点凭据；
- 控制器密码、HTTP API Token、Profile Token；
- MITM 证书、私钥、口令及可识别长度特征；
- 日志中的请求目标、证书内容和敏感配置值。

示例必须使用 `example.com`、RFC 5737 测试地址和虚构设备。迁移日志只记录数量、版本和成功/失败状态。测试夹具不得从运行数据库复制。

提交前执行敏感信息扫描；命中疑似密钥、真实私网信息或本机路径时必须人工复核。

## 5. 官方能力边界

### 5.1 Surge Mac

- 6.0 起支持 Gateway VM。
- 6.1 起支持 `MAC-ADDRESS` 规则。
- 6.4 起支持 Gateway VM UDP Fast Path；6.4.2 起可按设备关闭。
- 6.6.0 新增 Logbook、远程 Logbook 查看、策略组图标编辑和 Agent Skill 引导。
- Gateway VM 必须在 Surge Mac UI 中启用，Profile 不提供启用开关。
- UDP Fast Path 会绕过代理、规则和策略处理；目的端口低于 1024 或目标为 Fake IP 时不进入 Fast Path。

### 5.2 Surge iOS

- 本项目最低支持 iOS 5.14.6。
- `PROTOCOL,STUN` 在 iOS 5.11.2 起可用。
- `block-quic` 在 iOS 5.14.6 起可用。
- iOS 不支持 `PROCESS-NAME`；Mac 专属规则不得输出到 iOS 产物。
- `compatibility-mode` 和 `auto-suspend` 属于 iOS 专属 General 参数。

### 5.3 参考资料

- [Surge Mac Release Notes](https://nssurge.com/support/mac/release-notes)
- [Gateway Mode](https://manual.nssurge.com/features/gateway.html)
- [Source and Port Rules](https://manual.nssurge.com/rules/source-and-port.html)
- [Process Rules](https://manual.nssurge.com/rules/process.html)
- [Protocol and Network Rules](https://manual.nssurge.com/rules/protocol-and-network.html)
- [Logical Rules](https://manual.nssurge.com/rules/logical.html)
- [General Section Options](https://manual.nssurge.com/profile/general.html)
- [VM UDP Fast Path](https://kb.nssurge.com/surge-knowledge-base/technotes/udp-fast-path.md)
- [Logbook](https://manual.nssurge.com/tools/logbook.html)
- [Detached Profile](https://kb.nssurge.com/surge-knowledge-base/zh/guidelines/detached-profile.md)

## 6. 部署架构

每台 Mac 使用独立实例：

```text
工作电脑
  SurgeConf instance role = work_computer
  独立数据库
  可生成 mac_workstation 与 ios_mobile 档案

家庭网关
  SurgeConf instance role = home_gateway
  独立数据库
  默认生成 mac_gateway 档案
```

实例角色决定新档案默认值、设置页说明、管理访问默认值和运行验收提示。实例角色不限制可创建的档案目标，也不自动改写已有档案。

## 7. 数据模型

### 7.1 SystemConfig

新增键：

```text
installation_role = work_computer | home_gateway
admin_access = local_only | lan_authenticated
```

默认策略：

| 实例角色 | 管理后台 | Profile 订阅 |
|---|---|---|
| `work_computer` | 仅本机 | 可对局域网开放，但必须携带档案 Token |
| `home_gateway` | 局域网密码登录 | 可对局域网开放，但必须携带档案 Token |

### 7.2 ConfigProfile

新增字段：

```text
target = mac_workstation | mac_gateway | ios_mobile
udp_security_mode = strict | scoped | compatible
subscription_token_hash
```

约束：

- 现有档案迁移为 `mac_workstation`。
- 不自动复制现有档案，不自动创建含企业信息的 iOS 档案。
- Token 只在创建或轮换时显示明文，数据库只保存哈希。
- 未知 `target` 或 `udp_security_mode` 必须拒绝保存和生成。
- `ios_mobile` 仅允许 `strict` 或 `compatible`；设置为 `scoped` 时返回 422。

### 7.3 UdpException

新增结构化表：

```text
id
profile_id
match_type = process | mac_address
match_value
display_name
enabled
sort_order
```

约束：

- `process` 仅适用于 `mac_workstation`。
- `mac_address` 仅适用于 `mac_gateway`。
- `ios_mobile` 不允许创建无法生效的 UDP 例外。
- MAC 地址只存在于家庭网关实例的运行数据库。
- API 列表默认返回脱敏后的匹配值；完整值只用于覆盖式更新和生成。

## 8. 配置作用域

### 8.1 全局与档案专属

规则和 Host 的生效集合必须为：

```text
profile_id IS NULL
OR profile_id = current_profile.id
```

其他档案数据不得进入当前产物。

远程规则源和服务组继续作为公共规则库。需要覆盖公共行为时，使用当前档案的前置规则，不复制整套公共规则源。

### 8.2 最终策略

`ConfigProfile.final_action` 必须直接生成：

```ini
FINAL,<translated final_action>,dns-failed
```

引用不存在或已禁用的策略组时生成失败，不静默回落到默认档案或其他策略。

### 8.3 无效档案

显式传入不存在的 `profile_id` 时返回 404。只有调用方完全不传 `profile_id` 时才使用默认档案。

## 9. 目标感知生成管线

生成器按固定阶段执行：

```text
读取公共数据
→ 解析当前档案
→ 加载目标平台能力
→ 合并并过滤 General
→ 生成节点与策略组
→ 合并全局规则与当前档案规则
→ 注入目标感知 UDP 规则
→ 合并全局 Host 与当前档案 Host
→ 输出 Rewrite 与 MITM
→ 验证策略和段间引用
→ 返回内存产物
```

预览、订阅和下载不得产生服务器端文件。只有显式“导出到服务器”操作才写入 Application Support 下的 `exports/`。

## 10. General 平台能力

### 10.1 公共参数

DNS、IPv6、QUIC、测试地址和通用流量参数由公共 allowlist 输出。保留：

```ini
block-quic = per-policy
udp-policy-not-supported-behaviour = REJECT
```

禁止恢复全局 UDP/443 拒绝规则。

### 10.2 mac_workstation

- 不输出 iOS-only 参数，如 `compatibility-mode`、`auto-suspend`。
- 可输出 Mac-only 参数，如监听器和 `/etc/hosts` 行为。
- 支持 `PROCESS-NAME` UDP 例外。

### 10.3 mac_gateway

- 输出 `gateway-restricted-to-lan = true`。
- 不输出 iOS-only 参数。
- 支持 `MAC-ADDRESS` UDP 例外和设备级规则。
- 页面说明 Gateway VM、DNS、DHCP、IPv6 RA 和 UDP Fast Path 的运行时前提。

### 10.4 ios_mobile

- 输出 `compatibility-mode = 1`。
- 输出 `auto-suspend = true`，在检测到 Surge Mac 网关接管时自动暂停手机自身接管。
- 不输出 Mac-only 监听器、`read-etc-hosts` 或进程规则。
- 最低版本固定为 5.14.6，因此可输出 `block-quic = per-policy`。

## 11. UDP 安全策略

### 11.1 根因

旧配置可能仍含全局 UDP/443 拒绝，即使生成器已删除；必须同时验证生成产物和 Surge 当前有效规则。

全局 STUN 域名、端口和协议阻断无法区分浏览器 WebRTC 与合法 ICE/STUN 视频建链，可能导致实时视频失败。

### 11.2 模式

```text
strict
scoped
compatible
```

| 模式 | 行为 |
|---|---|
| `strict` | 生成 STUN 域名、3478/19302 端口和 `PROTOCOL,STUN` 阻断 |
| `scoped` | 先生成明确的 DIRECT 例外，再生成 strict 阻断 |
| `compatible` | 不生成全局 STUN 阻断，保留代理不支持 UDP 时 REJECT |

默认值：

| 目标 | 默认模式 |
|---|---|
| `mac_workstation` | `scoped` |
| `mac_gateway` | `scoped` |
| `ios_mobile` | `compatible` |

### 11.3 规则示例

工作 Mac：

```ini
AND,((PROCESS-NAME,ExampleVideo),(PROTOCOL,UDP)),DIRECT
PROTOCOL,STUN,REJECT
```

家庭网关：

```ini
AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT
PROTOCOL,STUN,REJECT
```

示例 MAC 为本地管理地址，仅用于文档和测试。

### 11.4 Fast Path

- Surge Gateway VM 自动判断是否触发 UDP Fast Path。
- Fast Path 流量绕过规则和代理，因此不能与“精确 UDP 分流”同时成立。
- 对可信且需要高并发 UDP 的设备可保持 Fast Path。
- 对必须精确分流的设备，用户在 Surge Dashboard 中关闭该设备的 Fast Path。
- Surge Mac 6.6 的当前 CLI 不作为自动化 Fast Path 管理依赖。

## 12. AI 与服务分流不变量

1. 显式 AI 前置规则优先于 Apple、Google 和 Global 规则。
2. AI 服务规则源统一指向 AI 服务策略组。
3. 服务组必须存在且启用；否则配置生成失败。
4. DouYin 规则源必须位于 TikTok 前。
5. 国内直连规则必须位于 Global 兜底前。
6. IP 类规则源继续使用 `no-resolve`，避免代理域名为匹配 IP 规则触发本地 DNS。
7. 企业前置规则仅存在于企业档案。
8. 每种目标产物均执行规则顺序断言和策略引用完整性检查。

## 13. 数据库运行目录

默认目录：

```text
~/Library/Application Support/SurgeConf/
├── surge_conf.db
├── surge_conf.db-wal
├── surge_conf.db-shm
├── backups/
└── exports/

~/Library/Logs/SurgeConf/
└── surgeconf.log
```

允许通过 `SURGECONF_DATA_DIR` 覆盖数据目录，用于测试或特殊部署。数据库不得放在网络文件系统。

权限：

- Application Support 目录：`0700`
- 数据库、WAL、备份和敏感导出：`0600`
- 日志：`0600`

SQLite 参数：

```text
journal_mode = WAL
synchronous = NORMAL
busy_timeout = 5000
foreign_keys = ON
```

## 14. Schema 与数据迁移

### 14.1 Alembic

引入 Alembic 管理表、列、索引、外键和约束。删除捕获所有异常的 `ALTER TABLE` 模式。

业务默认值补齐继续使用幂等数据迁移，但每项必须具备明确版本或迁移记录，不得依赖异常判断是否执行。

### 14.2 旧库搬迁

新路径无数据库而旧仓库路径存在时：

1. 创建权限为 `0700` 的目标目录；
2. 使用 SQLite Backup API 写入目标目录临时文件；
3. 执行 `PRAGMA integrity_check`；
4. 设置文件权限；
5. 原子重命名为正式数据库；
6. 创建 schema 升级前备份；
7. 执行 Alembic 升级；
8. 保留旧数据库作为回滚副本，不自动删除。

如果新旧路径都存在，使用新路径并记录旧库被忽略。迁移失败时服务拒绝启动，不修改旧库，不发布半迁移数据库。

### 14.3 备份

- schema 升级前自动备份；
- 重置数据前强制备份；
- 设置页提供立即备份和下载；
- 自动备份保留最近 10 份；
- 恢复前关闭数据库会话，校验备份，原子替换并重启；
- 不自动合并不同设备数据库。

## 15. 服务访问与认证

### 15.1 管理后台

- `local_only`：只接受本机管理请求。
- `lan_authenticated`：允许局域网访问，但必须完成密码登录并使用 HttpOnly、SameSite 会话 Cookie。

- 当 Profile 订阅需要局域网访问时，服务进程可以监听局域网地址，但中间件必须在路由处理前执行访问判定。
- `local_only` 模式下，除带有效档案 Token 的只读订阅请求外，所有非回环页面和管理 API 请求均拒绝。
- `lan_authenticated` 模式必须先设置管理密码；数据库只保存带随机盐的 `scrypt` 哈希，不保存明文密码。
- 会话签名密钥首次启动时随机生成，单独保存在 Application Support 中，权限为 `0600`。
- 管理会话使用 `HttpOnly`、`SameSite=Strict` Cookie；所有状态变更请求还必须校验 CSRF Token。
- 写操作、节点列表、订阅列表、档案详情和备份接口全部属于管理边界。

### 15.2 Profile 订阅

- 每个档案拥有独立可轮换 Token。
- URL Token 只授予该档案生成配置的只读权限。
- Token 不得访问管理 API、其他档案或备份。
- Token 轮换后旧 URL 立即失效。
- 日志不得记录完整订阅 URL 或 Token。

### 15.3 敏感字段

- 档案列表 API 不返回 MITM 私钥、证书和密码。
- 敏感值采用“是否已配置”状态和覆盖式更新。
- 节点凭据不通过面板的普通列表接口返回。
- 重置、恢复、Token 轮换和导出属于高风险操作，必须二次确认。

## 16. 订阅生命周期

该部分在目标感知生成完成后实施：

1. 真正执行 `auto_update` 和 `update_interval`。
2. 同步从全删重建改为稳定身份 upsert。
3. 保留人工地区、启停状态和服务钉选。
4. 同步失败保留上一批可用节点。
5. 首页显示上次成功、下次计划和失败摘要。
6. 定时任务不在多个 worker 中重复执行；启动时使用单实例调度锁。

## 17. 错误处理

- 无效目标、模式、策略引用或不兼容 UDP 例外返回 422。
- 不存在的显式档案返回 404。
- 远程规则源失败不破坏数据库中的上一个版本。
- 数据库迁移、完整性检查或备份失败时服务拒绝启动。
- 配置生成失败不写文件、不返回部分配置。
- Profile Token 验证失败统一返回 404，避免枚举档案。
- 运行时无法自动确认 Surge reload 时，页面必须明确显示“服务端产物已更新，客户端状态未验证”。

## 18. 分阶段交付

### 阶段 A：运行数据与安全边界

- Application Support、WAL、Alembic、旧库迁移和备份；
- 管理访问模式和 Profile Token；
- 敏感字段脱敏；
- 删除生成端点的源码目录写入。

### 阶段 B：目标感知档案

- 实例角色、档案目标和平台 General allowlist；
- 修复规则与 Host 作用域；
- `final_action` 生效；
- Mac、Gateway、iOS 三种产物。

### 阶段 C：UDP 兼容与网关设备

- UDP 三模式；
- 工作 Mac 进程例外；
- 网关 MAC 例外；
- iOS 兼容默认值；
- Fast Path 操作提示和运行时验收。

### 阶段 D：订阅生命周期与可观测性

- 自动更新调度；
- 节点 upsert；
- 同步状态展示；
- Logbook reload/DHCP 验收指引。

每个阶段必须独立通过测试、运行 smoke 和代码审查后才能进入下一阶段。

## 19. 验收标准

### 19.1 配置隔离

- 企业规则、Host 和 MITM 仅进入企业档案。
- 个人及家庭档案不包含任何企业标识。
- 默认档案和显式档案选择行为可预测。

### 19.2 平台兼容

- `mac_workstation` 不含 iOS-only 参数。
- `mac_gateway` 含网关安全参数，不含 iOS-only 参数。
- `ios_mobile` 含 iOS 参数，不含 Mac-only 参数、进程规则或 MAC 规则。
- 两种 Mac 产物通过 Surge Mac 6.6 `surge-cli --check`。
- iOS 产物通过 Surge iOS 5.14.6 Profile Syntax Check。

### 19.3 UDP

- 生成产物和 Surge 有效规则均无全局 UDP/443 拒绝。
- 工作 Mac 配置的进程 UDP 可直连，浏览器 STUN 仍被阻断。
- 网关配置的可信 MAC UDP 可直连，未配置设备保持严格策略。
- iOS 兼容档案可播放合法实时 UDP 视频。
- 所有目标保持 `udp-policy-not-supported-behaviour = REJECT` 和 `block-quic = per-policy`。

### 19.4 AI 分流

- OpenAI、Claude、Gemini 和综合 AI 规则先于重叠规则。
- AI 域名命中 AI 服务组，不落入 Google、Global 或 FINAL。
- IP 类规则源保持 `no-resolve`。

### 19.5 数据与安全

- 新数据库位于 Application Support，权限正确。
- 旧库保留，目标库 `integrity_check=ok`。
- 重复启动不重复迁移。
- 未认证局域网请求不能读取节点、订阅、企业配置、MITM 或备份。
- Profile Token 只能读取其所属档案。

### 19.6 运行时

- 工作 Mac、iOS、家庭网关下游设备分别完成一次真实实时视频播放。
- 工作 Mac 浏览器 WebRTC 检测不出现公开直连 STUN 候选。
- Surge 6.6 Logbook 出现对应 Profile reload 和必要的 DHCP/网络事件。
- Dashboard 请求查看器确认视频流命中预期 DIRECT 规则。

## 20. 测试策略

- 生成器兼容矩阵：覆盖 3 个目标 × 3 种 UDP 模式；有效组合验证产物，无效组合验证返回 422。
- 档案隔离：全局、当前档案、其他档案三类数据。
- General allowlist：公共、Mac-only、iOS-only 键集合。
- UDP 规则顺序、平台约束和无全局 UDP/443 回归。
- AI、DouYin/TikTok、国内/Global 顺序不变量。
- Alembic 从当前 schema 副本升级。
- 旧库迁移成功、失败、中断和重复启动。
- SQLite 权限、WAL、外键与并发读写。
- 管理会话、Profile Token、跨档案越权和敏感字段脱敏。
- 订阅 upsert 保留人工字段和失败回滚。
- 三个平台的真实运行 smoke；测试通过不能代替运行时视频验证。
