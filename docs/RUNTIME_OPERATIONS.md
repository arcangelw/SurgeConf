# 运行与验收指南（Surge Mac 6.6 / Surge iOS 5.14+）

面向工作 Mac、家庭网关 Mac 与 iOS 三类实例的日常运行、日志查看与最终人工验收。本文所有地址、域名、端口、ID 均为**占位示例**；引用的 Surge 官方文档为唯一权威来源。

> 本文不宣称任何物理验收已经通过：Surge 真机加载、实时视频、WebRTC、Gateway/FastPath、iOS 导入与 AI 真实命中均为**未观察项**，需按第 8 节人工执行。

## 1. 实例启动与停止

### 工作实例（本机）

```bash
cd /path/to/SurgeConf
./start.sh              # 前台（开发模式，stdout 直接进终端）
./start.sh --daemon     # nohup 守护：stdout/stderr 写 console 回退日志
./surgeconf.sh start    # launchd 守护（推荐）
./surgeconf.sh stop     # 停止（unload launchd）
./surgeconf.sh restart  # 重新生成 plist 并重启
./surgeconf.sh status   # 精确 label 查询运行状态
```

### 网关实例（独立数据目录/端口）

每个实例使用独立的数据目录与端口，互不共享数据库与调度锁：

```bash
SURGECONF_DATA_DIR="$HOME/Library/Application Support/SurgeConf-Gateway" \
SURGE_PORT=61831 ./surgeconf.sh enable
```

调度器为进程级互斥（`scheduler.lock` flock）：同一数据目录只会有一个实例运行自动更新循环，第二个实例进入 standby 只服务不循环；**不同**数据目录的两个实例各自持锁、各自调度。

## 2. 数据、备份、导出与日志

| 内容 | 默认路径 | 权限 |
|---|---|---|
| 数据库 / 备份 / 导出 | `~/Library/Application Support/SurgeConf`（`SURGECONF_DATA_DIR` 可整体重定向） | 目录 0700，文件 0600 |
| 结构化事件日志 | `~/Library/Logs/SurgeConf/surgeconf.log` | 0600，目录 0700 |
| console 回退日志 | `~/Library/Logs/SurgeConf/surgeconf-console.log` | 0600 |

- **结构化事件日志**由应用内私有轮转 handler 独占写入（5 MB × 3 备份，UTF-8）。launchd/nohup 的 stdout/stderr **只**写 console 回退日志——绝不能把任何 shell 重定向指向 `surgeconf.log`：轮转 rename 后旧文件描述符会继续写已轮换的 inode，破坏日志完整性。
- console 日志包含启动 banner 与框架级 stderr，**不**包含 HTTP access log（服务以 `--no-access-log` 启动）。它可能含库栈信息，**禁止直接分享原文**，外发前人工复核并只摘取与问题相关的行。
- 备份与导出经管理界面「备份」页或 API 进行，产物落在数据目录的 `backups/`、`exports/` 下（0600）。把数据目录整体拷贝（含 `surge_conf.db`）即完成冷备份；恢复用界面的恢复功能，不要在服务运行时直接覆盖数据库文件。

### 结构化事件行格式

每行单条、确定性 key=value，字段全部来自固定 allowlist（无 URL、节点配置、凭据、设备名、异常文本）：

```
ts=2026-01-01T00:00:00.000Z event=subscription_sync subscription_id=1 success=true node_count=12 inserted=3 updated=9 removed=0 schema_version=1
ts=2026-01-01T00:00:00.000Z event=subscription_sync subscription_id=1 success=false error_code=timeout node_count=12 inserted=0 updated=0 removed=0 schema_version=1
ts=2026-01-01T00:00:00.000Z event=database_ready applied_migrations=0 schema_version=1
```

事件名与错误码（固定集合）：

| 类别 | 取值 |
|---|---|
| 事件 | `subscription_sync` / `scheduler_active` / `scheduler_standby` / `scheduler_cycle_error` / `scheduler_item_error` / `scheduler_stop_error` / `database_ready` / `orphan_cleanup` / `startup_failed` / `service_shutdown` |
| counts 键 | `node_count` / `inserted` / `updated` / `removed` / `cleaned` / `applied_migrations` / `total` / `failed` |
| 错误码 | `timeout` / `http_status` / `invalid_content` / `network_error` / `internal_error` |

### 查看

```bash
./surgeconf.sh log                                  # tail -F 结构化事件日志
tail -f ~/Library/Logs/SurgeConf/surgeconf-console.log
```

## 3. Profile 生成后让 Surge 重载

SurgeConf 生成/更新产物文件后，Surge 不会自动感知，需手动重载：

- **Surge Mac**：主窗口菜单 `Profile → Reload`；或命令行：
  ```bash
  /Applications/Surge.app/Contents/Applications/surge-cli reload
  ```
- **远程/自动化**：HTTP API `POST /v1/profiles/reload`（需在目标实例配置 `external-controller-access`，占位形如 `example-password@127.0.0.1:9090`）。

官方文档：[HTTP API](https://manual.nssurge.com/others/http-api.html)、[Surge CLI](https://manual.nssurge.com/tools/cli.html)。

## 4. Logbook 查看 reload / DHCP / 网络事件

Surge Mac 6.6+ 的 Logbook 持久记录最近 7 天事件（profile reload、网络切换、崩溃恢复、更新与 DHCP 相关变化）：

- 入口：Surge Mac 主窗口工具区 Logbook；
- 命令行：`surge-cli logbook`（可读结构化记录）；
- Dashboard 可远程读取 Mac/iOS 实例的 Logbook 内容。

官方文档：[Logbook](https://manual.nssurge.com/tools/logbook.html)。

验收时确认：重载后在 Logbook 能看到对应 reload 记录；网关实例 DHCP 变化（客户端加入/离开）有对应事件。

## 5. Dashboard 请求查看器：AI / UDP DIRECT / FINAL

Dashboard 是 Surge Mac 附带的图形工具，可连接本机或远程实例（远程需 `external-controller-access`）：

1. 打开 Dashboard → 请求（Requests）查看器；
2. 触发对应流量后按策略列确认：
   - **AI 域名**命中 AI 专用策略组（而不是兜底或直连）；
   - **UDP 会话**（如 QUIC 例外场景）策略列显示 `DIRECT`（UDP 例外规则的预期行为）；
   - 未命中任何具体规则的请求落到 `FINAL` 兜底策略。

官方文档：[Dashboard](https://manual.nssurge.com/tools/dashboard.html)。

## 6. Fast Path 人工边界（仅 Surge Gateway VM）

UDP Fast Path 是网关 VM 场景的保护机制，人工开关前先理解边界：

- **触发**：单客户端 1 秒内 ≥10 条、或 10 秒内 ≥30 条新 UDP 连接；
- **进入后**：该客户端 UDP 降级为纯三层转发，**绕过代理规则与策略处理**（Dashboard 连接数骤减属预期）；
- **永久例外**：目标端口 **< 1024** 的 UDP、目标地址为 **FakeIP** 的 UDP 永远走常规模式，不进 Fast Path；
- **范围**：仅 Surge Gateway VM 生效，增强模式接管不适用；
- **开关**：Surge Mac 6.4.2+ Dashboard 提供按设备关闭 Fast Path 的开关，用于恢复可控性做对比排查。

官方 technote：[VM UDP Fast Path](https://kb.nssurge.com/surge-knowledge-base/technotes/udp-fast-path)。

## 7. 故障信息采集纪律

排障与反馈只采集以下内容：

- 结构化事件行的 `event` / `error_code` / counts（`node_count` 等）与时间戳；
- 订阅状态页/状态 API 的安全 DTO（错误码、上次成功时间、节点数）。

**禁止**复制或外发：

- 完整订阅 URL（含查询参数中的令牌）；
- 节点行 / server / password / 证书内容 / 设备名；
- console 回退日志原文（如必须引用，先人工脱敏）。

结构化事件日志本身已按 allowlist 过滤，可作为安全的信息来源；`error_code` 五个取值即可定位绝大多数同步问题（拉取超时、源响应异常、内容解析失败、网络错误、内部错误）。

## 8. 人工验收清单（未观察项，需真机执行）

以下项目**尚未在任何真实设备上观察通过**，升级后请逐项人工确认：

| 平台 | 项目 |
|---|---|
| 工作 Mac（mac_workstation） | 产物 `surge-cli --check` 通过；Surge 加载新 Profile；实时视频通话正常；WebRTC 行为符合预期 |
| 家庭网关（mac_gateway） | 产物 `surge-cli --check` 通过；Gateway 模式下下游设备正常；FastPath 触发/例外符合第 6 节；摄像头类设备正常 |
| iOS（ios_mobile） | Profile Syntax Check 通过后导入（iOS 只做静态检查，`--check` 为 Mac CLI 能力，不在 iOS 上冒充执行）；5.14+ 导入成功；视频正常 |
| 任意 | AI 域名真实命中 AI 策略组（Dashboard 请求查看器确认） |

Mac 产物检查命令（占位路径）：

```bash
/Applications/Surge.app/Contents/Applications/surge-cli --check /path/to/generated-mac.conf
```
