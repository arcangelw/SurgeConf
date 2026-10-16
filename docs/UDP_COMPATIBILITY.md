# UDP 兼容性与 Fast Path 指南

本文说明 SurgeConf 的 **UDP 安全模式**、**UDP 例外**与 Surge 客户端侧 **Gateway VM / Fast Path** 的边界：哪些由本工具管理、哪些只能在 Surge 客户端 UI 操作，以及常见问题的排查。管理界面的 UDP 页签（配置方案 → UDP 安全）以本文为准。

> 示例说明：本文所有进程名使用 `ExampleVideo`、所有 MAC 地址使用 `02:00:00:00:00:01`，均为占位值，请替换为你自己的数据。本仓库不含任何真实企业、网络或节点数据。

## 三种 UDP 安全模式

每个配置档案携带一个 `udp_security_mode`（配置方案 → 编辑，或档案内「UDP 安全」页签直接切换）。三种模式的差异**只体现在项目管理的四条 STUN 防护规则与例外放行规则**上；任何模式都不会全局禁用 UDP，其余 UDP 流量始终由你的普通规则与各策略的 UDP 转发能力决定：

| 模式 | 生成产物 | 适用目标 |
|---|---|---|
| `strict` | 注入四条受管 STUN 防护规则以阻断受管 STUN 流量：`DOMAIN-KEYWORD,stun,REJECT`、UDP 3478、UDP 19302 与 `PROTOCOL,STUN`；例外不输出（保留在库中，切回 scoped 后恢复） | 全部目标 |
| `scoped` | 先输出已启用例外的放行规则，随后注入**与 strict 完全相同的**四条 STUN 防护（Mac 默认）。无已启用例外时，产物中的 STUN 防护与 strict 完全一致 | 仅 Mac 两目标 |
| `compatible` | 不注入任何受管 STUN 防护（iOS 默认）；用户自建规则照常保留 | 全部目标 |

唯一被拒绝的组合是 `ios_mobile + scoped`（Surge iOS 不支持 scoped 语义，创建/更新都会被 API 以 422 拒绝）。

管理界面在 scoped 模式且无已启用例外时显示「STUN 防护与 strict 相同」的警告——这只描述上述受管防护的等价性，**不是**「拒绝全部 UDP」。

## 目标矩阵：谁支持 UDP 例外

| 目标 | 例外匹配类型 | 管理界面 |
|---|---|---|
| `mac_workstation` | 进程名（`process`，例：`ExampleVideo`） | 档案 → UDP 安全 → 例外表 |
| `mac_gateway` | 局域网设备 MAC 地址（`mac_address`，例：`02:00:00:00:00:01`） | 同上 |
| `ios_mobile` | **不支持任何 UDP 例外**（Surge iOS 无进程名 / MAC 匹配） | UDP 页签保留：仅模式（strict / compatible）与风险说明，无例外控件 |

- scoped 模式下没有任何已启用的例外时，产物中的 STUN 防护与 strict 相同（界面会显示明确警告）；添加或启用例外后警告消失，禁用/删除最后一个已启用例外后警告恢复。
- 例外列表与界面只显示**脱敏匹配值**（如 `EX****EO`）。完整值仅保存在本实例数据库中用于生成配置，创建/更新/列表 API 的响应均不回显完整值。
- 编辑例外为**覆盖式**：完整值输入框永远空白，留空表示保持原值，仅当输入新值时请求才携带匹配值。

## Gateway VM 与 Fast Path：由 Surge 客户端管理

**Gateway VM 只能在 Surge Mac 的 UI 中启用**（Surge Mac → Dashboard → 网关设置）。SurgeConf 的 `mac_gateway` 目标只负责生成网关形态的配置（强制 `gateway-restricted-to-lan = true` 等），它**不是** Gateway VM 的开关，也无法替你启用或关闭 Gateway VM。

**Fast Path** 是 Surge 网关对局域网设备的加速机制，同样**只能在 Surge Mac 客户端的 Dashboard 中按设备管理**：

- **符合条件并进入 UDP Fast Path 的 UDP 数据包**会绕过规则、代理与 MITM（不经过策略判定）。
- 并非所有流量都进入 Fast Path：目标端口低于 1024、Fake IP 等不符合条件的流量不进入；**TCP 不受此开关影响**。
- 因此**需要精确 UDP 分流的设备必须在 Surge Dashboard 中关闭其 Fast Path**，否则规则（包括 UDP 例外生成的放行/拒绝规则）不会对该设备的合格 UDP 流量生效。
- **Surge Mac 6.6 的 `surge-cli` 不用于自动控制逐设备 Fast Path**：请勿尝试用脚本经 CLI 批量开关 Fast Path；逐设备操作只应通过 Dashboard 进行。

## iOS compatible 的 WebRTC 风险

`ios_mobile` 默认 `compatible`：UDP 兼容性最好（游戏、通话类应用正常），但**浏览器 WebRTC 可能获得直连候选地址**——即对端可见你的真实出口 IP。介意该隐私暴露时，请将档案切换为 `strict`（受管 STUN 流量被阻断后，WebRTC 直连候选随之不可用）。

## 关于 `udp-policy-not-supported-behaviour = REJECT`

所有目标的产物都强制输出 `udp-policy-not-supported-behaviour = REJECT`。注意它**不是「全局禁用 UDP」**：

- 它只决定「当命中的策略**不支持 UDP 转发**时」的行为：`REJECT` 表示拒绝该 UDP 会话，而不是回落直连（DIRECT）造成绕过代理的泄漏。
- 其余 UDP 流量仍按普通规则与 UDP 安全模式处理：strict/scoped 输出上述受管 STUN 防护，scoped 下例外命中的进程/设备（`ExampleVideo`、`02:00:00:00:00:01`）被放行，compatible 不加防护。

## 已有配置需要重载/检查

模式与例外的变更**只影响之后生成的配置文件**：

1. 在管理界面修改模式或例外后，重新下载/预览配置，确认 UDP 相关规则符合预期。
2. 设备上**仍在使用的旧配置不会自动更新**——请在 Surge 中重新下载配置或重新导入（订阅用户等待/触发更新），然后重载。
3. 升级 SurgeConf 后，建议对每个档案执行一次「预览」检查 UDP 规则段（尤其旧档案此前的默认值可能与新默认不同）。

## 排障

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 添加了例外，设备/进程的 UDP 仍被拒 | 档案是 strict（strict 不输出例外）；或例外被禁用；或设备配置未重载 | 确认模式为 scoped、例外已启用；重新生成并重载配置 |
| 例外设备的流量完全不走路由规则 | 该设备在 Surge Dashboard 开了 Fast Path，合格 UDP 数据包绕过规则/代理/MITM（属预期） | 在 Surge Dashboard 关闭该设备的 Fast Path |
| Gateway VM 没有生效 | 本工具不控制 Gateway VM | 在 Surge Mac 客户端 UI 中启用 Gateway VM |
| 想用 CLI 批量管理 Fast Path | Surge Mac 6.6 CLI 不支持逐设备 Fast Path | 仅通过 Dashboard 手动管理 |
| iOS 上 WebRTC 暴露真实 IP | compatible 模式允许直连候选 | 改用 strict，或接受该风险 |
| iOS 档案 UDP 页签没有例外表 | iOS 目标不支持例外（无进程/MAC 匹配） | 属预期；页签仅模式与说明，模式可选 strict / compatible |
| 保存 scoped 时警告「STUN 防护与 strict 相同」 | 尚无已启用的例外 | 添加或启用至少一个例外 |
| 例外列表显示 `***` 之类占位 | 脱敏碰撞时的静态占位（不包含原值片段） | 属预期；完整值仍保存在实例中 |

相关文档：[PROFILE_TARGETS.md](PROFILE_TARGETS.md)（目标设备、实例角色与 General 能力矩阵）。
