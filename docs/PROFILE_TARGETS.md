# 档案目标设备（Profile Targets）

SurgeConf 支持为不同的 Surge 设备形态生成不同的配置产物。每个配置档案（Profile）携带一个**目标设备**（`target`）与一个 **UDP 安全模式**（`udp_security_mode`），二者共同决定生成器的字段能力、强制安全值与默认行为。

本文是两实例部署、目标语义与运行边界的唯一指南；管理界面的目标选择器与设置页角色选择均以本文为准。

## 两实例部署与实例角色

一台 SurgeConf 服务对应一个**实例角色**（`installation_role`，设置页 → 访问控制 → 实例角色）：

| 实例角色 | 语义 | 新建档案默认目标 |
|---|---|---|
| `work_computer` 工作电脑 | 管理者日常使用的 Mac，仅本机或个人设备使用 | `mac_workstation` |
| `home_gateway` 家庭网关（默认） | 常驻 Mac，为局域网内其他设备提供共享代理 | `mac_gateway` |

要点：

- 实例角色**只影响此后新建档案在省略目标时的默认值**；显式选择目标永远优先。
- 修改角色**绝不改写已有档案**——每个档案的 target 独立保存。
- 实例角色**不是 Surge 网关（Gateway VM）的开关**。见下文「Profile 不能开启 Gateway VM」。

创建 API 契约（`POST /api/configs/profiles`）：

```bash
# 省略 target：按实例角色取默认（work_computer 实例 → mac_workstation）
curl -X POST http://127.0.0.1:61830/api/configs/profiles \
  -H "Content-Type: application/json" \
  -d '{"name": "Work-daily"}'

# 显式 target 优先（本例为虚构名称；home_gateway 实例也可显式建 iOS 档案）
curl -X POST http://127.0.0.1:61830/api/configs/profiles \
  -H "Content-Type: application/json" \
  -d '{"name": "Phone", "target": "ios_mobile"}'
```

`target` 与 `udp_security_mode` 均不允许显式 `null`（422）：省略即取默认（目标默认模式 / 实例角色默认目标）。

## 三个目标与最低版本

| 目标 | 用途 | 最低版本 | 依据 |
|---|---|---|---|
| `mac_workstation` | 单台 Mac 桌面使用 | Surge Mac 5.10.3 | 产物强制输出 `block-quic`（5.10.3 引入） |
| `mac_gateway` | Mac 作为局域网网关 | Surge Mac 5.10.3 | 同上；另强制 `gateway-restricted-to-lan`（5.8.1 引入，被 block-quic 下限覆盖） |
| `ios_mobile` | iPhone / iPad | Surge iOS 5.14.6 | 产物强制输出 `block-quic`（iOS 5.14.6 引入） |

低于上述版本的 Surge 可能无法解析产物中的强制键，导入前请先升级客户端。

## 配置作用域

General 字段能力由唯一能力矩阵（`app/profile_targets.py`）派生，UI 元数据、保存校验与生成过滤三处同源：

- **公共键**（`wifi-assist`、`ipv6`、`udp-priority` 等）三个目标都可配置。
- **Mac 专属键**（`http-api`、`external-controller-access`、`read-etc-hosts`、`socks5-listen` 等）仅 `mac_workstation` / `mac_gateway` 输出；iOS 产物不含任何 Mac-only 键。
- **网关专属键**（`gateway-restricted-to-lan`）仅 `mac_gateway` 输出，且强制为 `true`。
- **iOS 专属键**（`compatibility-mode`、`auto-suspend`）仅 `ios_mobile` 输出，且强制安全值。
- 表外未知键不属于任何目标：保存 API 拒绝（422），生成与读取一律丢弃。

目标切换后，General Tab 按当前档案重新拉取 `/api/general/fields?profile_id=<id>`，字段集合与 forced 徽标随目标即时变化；保存前会再次拉取，防止用旧目标的 forced 集合提交（fail closed）。

## UDP 安全模式与阶段边界

| 模式 | 语义 | 允许的目标 |
|---|---|---|
| `strict` | 全局拒绝 UDP 转发 | 全部 |
| `scoped` | 按策略作用域放行 UDP（Mac 目标默认） | 仅 Mac 两目标 |
| `compatible` | 兼容模式（iOS 默认） | 全部 |

- 唯一被拒绝的组合是 `ios_mobile + scoped`：创建与更新都在事务提交前校验，失败 422 且不落库。
- 只切换目标不提供模式时，模式重置为新目标默认（iOS → `compatible`，Mac → `scoped`）。
- **阶段边界**：当前 `udp_security_mode` 作为档案属性保存并参与组合校验；生成产物中的 UDP 安全底线由全目标强制值兜底（`block-quic = per-policy`、`udp-policy-not-supported-behaviour = REJECT`，保存 API 拒绝危险覆盖）。strict/scoped/compatible 的逐策略差异化生成行为属于后续 UDP 阶段，本阶段不改变产物语义。

## Profile 不能开启 Gateway VM

`mac_gateway` 目标只表示「这份配置是给作为网关运行的 Mac 生成的」，它会：

- 强制 `gateway-restricted-to-lan = true`（仅接受本网段设备，防误暴露公网）；
- 输出网关形态所需的监听类字段。

它**不会**：

- 开启或关闭 Surge 应用内的网关（Gateway VM / Enhanced Gateway）功能——那需要在 Surge 客户端侧操作；
- 改变系统路由或 DHCP——网关部署的联网拓扑由你自行规划。

把一台 Mac 变成家庭网关的运行步骤（在 Mac 上安装 Surge 并启用其网关能力、让局域网设备把网关指向该 Mac）属于 Surge 客户端操作，SurgeConf 只负责生成对应形态的配置档案。

## 数据与 Token 安全

- 档案列表 API 仅返回最小披露字段（`id` / `name` / `description` / `is_default` / `locale` / `target` / `udp_security_mode` / `token_configured`）；`token_configured` 是布尔状态，**任何接口都不回显 Token 明文或哈希**。
- 订阅 Token（`POST /api/configs/profiles/{id}/token`）只在生成/轮换的响应里显示一次；管理界面同样只在一次性对话框中展示，关闭即清空。
- 逐档案订阅端点 `GET /api/configs/profiles/{id}/subscribe?token=...` 是唯一免管理会话的只读路径，与登录会话、CSRF 完全隔离；轮换使旧链接立即失效。
- MITM 证书等秘密字段在详情里只返回 `{"configured": true/false}`。

本文中的主机名、端口、档案名（`Work-daily`、`Phone` 等）均为虚构示例；请替换为你自己的数据。
