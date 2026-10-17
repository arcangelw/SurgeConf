# 运行与验收指南（Surge Mac 6.6 / Surge iOS 5.14+）

面向工作 Mac、家庭网关 Mac 与 iOS 三类实例的日常运行、日志查看与最终人工验收。本文所有地址、域名、端口、ID 均为**占位示例**；引用的 Surge 官方文档为唯一权威来源。

> 本文不宣称任何物理验收已经通过：Surge 真机加载、实时视频、WebRTC、Gateway/FastPath、iOS 导入与 AI 真实命中均为**未观察项**，需按第 8 节人工执行。

## 1. 实例启动与停止

### 工作电脑 / 家庭网关（分别在各自物理 Mac 执行）

标准脚本在每台 Mac 上只管理**一个**固定 launchd 实例（默认数据目录
与端口）。工作电脑与家庭网关是两台不同的物理 Mac，各自克隆仓库并
执行同样的安装命令：

```bash
cd /path/to/SurgeConf          # 每台 Mac 各自的仓库副本
./surgeconf.sh start           # launchd 守护（首次安装并启动）
./surgeconf.sh stop            # 停止（unload launchd）
./surgeconf.sh restart         # 重新生成 plist 并重启
./surgeconf.sh status          # 精确 label 查询运行状态
```

**同一台 Mac 上同时安装两个 launchd 实例不受支持**（标准脚本固定
Label/数据目录/端口，第二个 `enable` 会覆盖第一个的 plist）。

调度器为进程级互斥（`scheduler.lock` flock）：这是对意外双进程的
保护——同一数据目录上第二个进程进入 standby 只服务不循环，绝不
产生两条自动更新循环。

开发调试可临时前台/守护运行（不注册 launchd，与 launchd 实例互斥于
调度锁）：

```bash
./start.sh              # 前台（stdout 直接进终端）
./start.sh --daemon     # nohup 守护：stdout/stderr 写 console 回退日志
./surgeconf.sh run      # 前台运行（读取控制文件配置）
```

## 2. 数据、备份、导出与日志

| 内容 | 默认路径 | 权限 |
|---|---|---|
| 数据库 / 备份 / 导出 | `~/Library/Application Support/SurgeConf`（`SURGECONF_DATA_DIR` 可整体重定向） | 目录 0700，文件 0600 |
| 结构化事件日志 | `~/Library/Logs/SurgeConf/surgeconf.log` | 0600，目录 0700 |
| console 回退日志 | `~/Library/Logs/SurgeConf/surgeconf-console.log` | 0600 |

- **结构化事件日志**由应用内私有轮转 handler 独占写入（5 MB × 3 备份，UTF-8）。launchd/nohup 的 stdout/stderr **只**写 console 回退日志——绝不能把任何 shell 重定向指向 `surgeconf.log`：轮转 rename 后旧文件描述符会继续写已轮换的 inode，破坏日志完整性。
- console 日志包含启动 banner 与框架级 stderr，**不**包含 HTTP access log（服务以 `--no-access-log` 启动）。它可能含库栈信息，**禁止直接分享原文**，外发前人工复核并只摘取与问题相关的行。
- **数据目录必须位于本地卷**：`SURGECONF_DATA_DIR` 指向网络文件系统（NFS/SMB/AFP 等未置 `MNT_LOCAL` 的挂载）时服务**拒绝启动**（fail closed），root/`backups`/`exports` 三个目录逐一复核（各自可能是独立挂载点），备份/恢复/重置同样在操作前拒绝——SQLite WAL 依赖同卷原子 rename 与 mmap 一致性，网络卷会静默损坏数据库；连接建立时还会实测 `journal_mode=WAL` 是否生效（未生效即拒绝服务）。判定依据是 macOS `statfs64` 返回的内核权威挂载标志 `MNT_LOCAL`（非路径前缀或类型名猜测）：目录不存在时检查最近现存父目录、创建后复核。启动失败/操作被拒且 console 日志出现 `runtime directory is on a non-local filesystem (type '…')` 或 `journal_mode=WAL was not honored` 时，把数据目录改回本地磁盘后重启即可。**不要尝试关闭 WAL 来绕过**：WAL 是备份一致性（页级快照）、崩溃恢复与迁移原子发布的前提，绕过等于放弃数据库完整性保证。本守卫只覆盖数据目录（root 及其子目录）；`~/Library/Logs` 下的日志目录由日志子系统自行创建，**不**在数据库完整性保障范围内。产品目标平台为 macOS；Linux 等非 Darwin 环境仅用于测试，以 `/proc/mounts` 已知网络文件系统类型表做同类拒绝（黑名单未列名类型默认本地，仅测试环境语义）。

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
- **远程/自动化**：HTTP API `POST /v1/profiles/reload`。该端点由
  `[General]` 的 `http-api` 启用（占位形如 `http-api = <key>@127.0.0.1:6171`），
  请求带 `X-Key: <key>` 头认证。注意 `external-controller-access` **不会**
  启用 HTTP API 端点（两者是独立配置）：
  ```bash
  (                                # 子 shell：块内 exit 不退出当前交互 shell
    unset KEY                      # 先清防继承已 export 的旧值
    read -sr KEY || { unset KEY; exit 1; }  # read 失败（EOF/中断）同样清理后失败
    [ -n "$KEY" ] || { unset KEY; exit 1; } # 空 key 直接失败，不发起请求
    printf 'X-Key: %s\n' "$KEY" | curl --fail --silent --show-error \
      -X POST --header @- https://127.0.0.1:6171/v1/profiles/reload
    rc=$?                          # 捕获 curl 退出码后再清理
    unset KEY                      # 用完即清
    exit "$rc"                     # curl 结果带回 $?（--fail 下 HTTP 4xx/5xx 亦非零）
  )
  ```
  - 仓库默认 `http-api-tls = true`：示例必须走 `https://`，默认前提是
    **已将 Surge MITM CA 加入系统钥匙串并信任**（Surge 导出 CA 后双击
    安装、在钥匙串访问中信任）。**严禁 `-k` / `--insecure` 跳过证书
    校验**。仅当你在 General 中显式设置 `http-api-tls = false` 时才用
    `http://`，协议必须与配置匹配。
  - key 经 stdin 以 `--header @-` 注入：不出现在 curl argv、进程列表
    与命令历史。stdin 管道与 shell 变量仍是**本机敏感数据**（不是
    加密通道），执行后必须 `unset` 清理变量；禁止把真实 key 写进
    命令行参数、临时文件、URL query、环境导出或粘贴到任何工单。
  - 若 Surge MITM CA **尚未**装入系统信任，用下面的可执行分支显式
    指定 CA 文件（同样自包含，独立读取并验证路径；不得用占位路径
    充当实际参数）：
  ```bash
  (                                # 自包含：不依赖上一块，可单独复制执行
    unset KEY CA_FILE              # 先清防继承已 export 的旧值
    read -sr KEY || { unset KEY CA_FILE; exit 1; }  # read 失败同样清理后失败
    [ -n "$KEY" ] || { unset KEY CA_FILE; exit 1; } # 空 key 直接失败，不发请求
    read -r CA_FILE || { unset KEY CA_FILE; exit 1; }  # CA 路径读取失败同上
    [ -n "$CA_FILE" ] || { unset KEY CA_FILE; exit 1; } # 空 CA 路径直接失败
    [ -f "$CA_FILE" ] || { echo "CA 文件不存在或不是普通文件" >&2; \
      unset KEY CA_FILE; exit 1; } # 验证失败同样清理 KEY/CA_FILE 再退出
    printf 'X-Key: %s\n' "$KEY" | curl --fail --silent --show-error \
      -X POST --header @- \
      --cacert "$CA_FILE" \
      https://127.0.0.1:6171/v1/profiles/reload
    rc=$?                          # 捕获 curl 退出码后再清理
    unset KEY CA_FILE              # 用完即清
    exit "$rc"                     # curl 结果带回 $?（含 HTTP 4xx/5xx）
  )
  ```
  （两个代码块各自自包含：复制任一块单独执行，都会在子 shell 内
  完成「清理继承值 → 隐藏读取（read 失败或空值即清理变量并以
  非零退出，不发起请求）→ 发送 → 捕获退出码 → 清理变量」闭环，
  KEY 不留在当前 shell 的变量里；curl 以 `--fail --silent
  --show-error` 调用，HTTP 4xx/5xx 与连接错误都表现为非零退出码
  并经 `$?` 带回；块内 `exit` 只退出子 shell，不会退出你正在
  使用的交互 shell。）

官方文档：[HTTP API](https://manual.nssurge.com/tools/http-api.html)、[Surge CLI](https://manual.nssurge.com/tools/cli.html)。

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
