# 数据迁移验证指南（AI 可执行）

> 适用：多设备部署 SurgeConf。设备拉取包含迁移机制的代码并 `./surgeconf.sh restart` 后，
> 按本文逐步验证迁移是否生效。全部通过即报告「迁移验证通过」。
> 迁移逻辑：`app/bootstrap.py::apply_data_migrations`（幂等，每次启动执行，可安全重复）。

运行数据已迁出源码目录。先在 shell 中定义（与启动脚本一致的解析规则）：

```bash
DB="${SURGECONF_DATA_DIR:-$HOME/Library/Application Support/SurgeConf}/surge_conf.db"
LOG="$HOME/Library/Logs/SurgeConf/surgeconf.log"
```

## 迁移内容（随代码静默分发）

1. **Claude 配套规则** `DOMAIN-SUFFIX,claudeusercontent.com,AI服务` 补齐（Artifacts CDN 归组）
2. **存量档案 `ipv6-vif` 归一**：`auto/native/off/disable → disabled`（官方规范值；auto 按既定策略强制关闭）
3. **DNS 默认值升级**（仅精确匹配旧默认值时升级，用户自定义不动）：
   - `encrypted-dns-server`：双 DoH → `h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query`（三路并发先答先用）
   - `hijack-dns`：`8.8.8.8:53, 8.8.4.4:53` → `*:53`（全劫持明文 53）
4. **删除死配置 `geoip-maxmind-url`**：产物无 GEOIP 规则（国内判定走 domestic.ip/ChinaMax/ASN China 三集），该 mmdb 库不会被查询——省一次下载与常驻内存

   验证：`sqlite3 "$DB" "SELECT COUNT(*) FROM config_profiles WHERE general LIKE '%geoip-maxmind-url%';"` 返回 0，且 preview 产物无 geoip 行、url-test 组均为 `tolerance=100`（官方默认，消除 50ms 容差导致的抖动切换与连接重建）。
5. **关闭 Surge 内置 Web 面板**：`http-api-web-dashboard` → `false`（常驻内存；SurgeConf 面板不依赖它，官方默认亦为 false。Surge 的 HTTP API 端口不受影响）

   验证：preview 产物含 `http-api-web-dashboard = false`。
6. **抖音优先直连**：补齐 `DouYin` 规则源并放在 `TikTok` 之前。两者都覆盖 `snssdk.com`，优先直连可避免抖音评论、视频和直播误走 TikTok 海外出口。

   验证：`SELECT name,action,sort_order FROM rule_sources WHERE name IN ('DouYin','TikTok') ORDER BY sort_order;` 中 `DouYin / 大陆直连` 位于 `TikTok` 之前。
7. **恢复媒体 QUIC 性能**：删除旧全局 `UDP/443 → REJECT`（`remove_global_quic_v1`），并补 `block-quic = per-policy`。DIRECT 保留 QUIC/HTTP3，代理按 UDP 能力自适应处理；STUN 防泄漏改为按档案模式注入（见第 9 条）。

   **删除边界**（该迁移无所有权元数据，按条件而非按来源匹配）：`profile_id IS NULL` + `position='pre'` + `rule_text` **精确等于** `AND,((PROTOCOL,UDP), (DEST-PORT,443)),REJECT`（注意括号内 `, ` 逗号+空格形态）的所有记录——**即使是你自建的也会在首次升级时被一次性删除**，且不校验 `comment`/`sort_order`。文本有任何差异（多一个空格也算）或其他作用域（档案级 / 非 pre 位置）的变体一律保留。迁移记账后不再重复执行；若你确需全局阻断 UDP/443，可在升级后显式重建该规则——生成器将其作为用户自建规则保留并照常输出。
8. **`no_resolve_keywords` 系统键退役**：IP 类规则集的 no-resolve 判定已结构化（`rule_sources.ip_rules` 布尔列），关键词系统键不再被生成器读取，迁移删除该键避免面板残留失效配置。
9. **UDP 安全改为按档案动态生成**（`udp_security_modes_v1`）：
   - **v1 清理只删除冻结签名的旧全局托管行**：`profile_id IS NULL` + `position='pre'` + `sort_order` 精确相等 + `comment` 精确相等 + `rule_text` 匹配时**仅忽略逗号两侧的 ASCII 空格/Tab**（行首尾空白、token 内部空白、Unicode 空白与大小写一律保留；含 CR/LF 的行直接判不匹配）（签名见 `app/default_config.py::LEGACY_MANAGED_STUN_SIGNATURES`，即历史四条全局 STUN 阻断）。曾手改过 `comment` 或 `sort_order` 的 lookalike 一律视为用户规则保留——既不误删，也不复活。
   - **新托管 STUN 规则动态生成，绝不落库**（`custom_rules` 中不再有任何托管 STUN 行）。生成器按每个档案的 `udp_security_mode` 注入：

     | 模式 | 产物 `[Rule]` 段（注释行 `# >> 托管 UDP 安全规则` 之后、第一条 RULE-SET 之前） |
     |---|---|
     | `strict` | 恰四条 REJECT：`DOMAIN-KEYWORD,stun` → `UDP 3478` → `UDP 19302` → `PROTOCOL,STUN`（固定顺序） |
     | `scoped`（仅 Mac 两目标） | 已启用例外的 DIRECT 行在前（`mac_workstation`=进程名、`mac_gateway`=MAC 地址），随后同样四条 REJECT；无启用例外时与 strict 完全一致 |
     | `compatible` | 零托管行（用户自建规则照常保留；iOS 默认模式） |

   - 例外存 `udp_exceptions` 表；匹配值（进程名/设备 MAC）属敏感数据，验证只做计数（见验证步骤 2）。

## 验证步骤

### 1. 迁移日志

```bash
grep "数据迁移" "$LOG"
```

- 老库首次启动：每个未记账且**实际产生变更**的迁移各打印一行 `[数据迁移] <key>: <N> 项变更`（例：`[数据迁移] udp_security_modes_v1: 4 项变更`）。**零变更的迁移只记账、不打印日志**——因此首次执行 `udp_security_modes_v1` 若老库本就没有可删行（N=0），日志无此行，属正常；是否执行以 `data_migration_records` 计数确认（见步骤 2），不能以日志有无判定。
- 新库或已迁移库：无新增日志行、`data_migration_records` 无新增记录（幂等）。

### 2. UDP 安全迁移与例外（只读计数，不回显值）

```bash
sqlite3 "$DB" "SELECT COUNT(*) FROM data_migration_records WHERE key='udp_security_modes_v1';"  # 期望 1
sqlite3 "$DB" "SELECT match_type, COUNT(*) FROM udp_exceptions GROUP BY match_type;"            # 只见类型与计数
sqlite3 "$DB" "SELECT COUNT(*) FROM custom_rules WHERE rule_text LIKE '%claudeusercontent%';"   # 期望 >=1
```

说明：

- **不要再用 `SELECT rule_text ... LIKE '%stun%'` 断言「托管 STUN 必须落库」**——阶段 C 起托管 STUN 动态生成、绝不落库；strict/scoped 的 STUN 防护改在「生成产物检查」按档案模式验证。用户自建含 stun 字样的行不被迁移触碰，此类计数 >0 属正常。
- `udp_exceptions.match_value` 是敏感值（进程名/MAC），数据库验证只允许 `COUNT(*) GROUP BY match_type`；需要区分归属时 join `config_profiles` 只取 `target` 字段。
- 例外类型与档案目标的对应关系（workstation=process、gateway=mac_address、iOS 不支持例外）见 `docs/UDP_COMPATIBILITY.md`。

### 3. ipv6-vif 归一

```bash
sqlite3 "$DB" "SELECT id, name, json_extract(general,'$.ipv6-vif') FROM config_profiles;"
```

预期：所有档案均为 `disabled`（NULL 也合法 = 继承模板默认 disabled）。`always` 为用户主动选择的合法值，不动。

### 4. DNS 默认值升级

```bash
sqlite3 "$DB" "SELECT id, name, json_extract(general,'$.encrypted-dns-server'), json_extract(general,'$.hijack-dns') FROM config_profiles;"
```

预期（老默认被精确匹配升级后）：

- `encrypted-dns-server` = `h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query`
- `hijack-dns` = `*:53`

说明：若该设备 DNS 值为用户自定义（与旧默认串不同），迁移有意跳过——属正常，不算失败。

### 5. 生成产物检查（按目标 × UDP 模式）

```bash
curl -s "http://127.0.0.1:61830/api/configs/preview?profile_id=1"
```

所有档案的产物应包含（判定条件为存在性行匹配，非逐字全文比对）：

```
ipv6 = false
ipv6-vif = disabled
encrypted-dns-server = h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query
hijack-dns = *:53
block-quic = per-policy
udp-policy-not-supported-behaviour = REJECT          ← 仅约束「策略不支持 UDP 转发时」的行为，不是全局禁 UDP
RULE-SET,<URL 含 /ip/ 的规则集>,<任意策略>,no-resolve  ← 至少一行以 ,no-resolve 结尾（DNS 泄漏防护）
```

再按该档案 `target × udp_security_mode` 断言托管 UDP 段（注释行 `# >> 托管 UDP 安全规则` 之后、第一条 RULE-SET 之前）：

| 档案模式 | 预期托管行 |
|---|---|
| `strict` | 恰四条 REJECT，顺序固定：`DOMAIN-KEYWORD,stun,REJECT` → `AND,((PROTOCOL,UDP),(DEST-PORT,3478)),REJECT` → `AND,((PROTOCOL,UDP),(DEST-PORT,19302)),REJECT` → `PROTOCOL,STUN,REJECT` |
| `scoped` | 例外 DIRECT 行在前（示例：`AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT`，规范占位值），四条 REJECT 按上表顺序殿后；无启用例外时与 strict 相同 |
| `compatible` | 无 `# >> 托管 UDP 安全规则` 段、无任何托管 STUN 行；`ios_mobile` 产物恒无 `PROCESS-*` / `MAC-ADDRESS` 规则 |

产物断言口径：**项目不再动态注入全局 `UDP/443 → REJECT`**；若产物中仍出现该行，可能来自迁移范围外保留的已有用户规则（如全局 general 位置、档案级 pre/general 规则），也可能是迁移记账后新增/重建的用户规则——出现本身**不能**判迁移失败，如不需要可在管理界面删除（`remove_global_quic_v1` 的删除边界见迁移内容第 7 条）。说明：RULE-SET 行的策略随设备配置而异，判定依据是「URL 含 /ip/ 且以 ,no-resolve 结尾」而非具体策略名。

### 6. 运行时实测（Surge 已重载新配置后）

STUN 阻断（strict / 无命中例外的 scoped 档案；应全部超时，约 4 秒后返回 TIMEOUT）：

```bash
python3 -c "
import socket, struct, os
for host in ('stun.miwifi.com', 'stun.chat.bilibili.com'):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
    s.sendto(struct.pack('!HHI', 1, 0, 0x2112A442) + os.urandom(12), (host, 3478))
    try: s.recvfrom(2048); print(host, '收到响应 ← 阻断失效!')
    except socket.timeout: print(host, 'TIMEOUT ✓')
    s.close()"
```

加密 DNS 可用（任一端点返回 200）：

```bash
printf '\x00\x00\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01' > /tmp/q.bin
curl -so /dev/null -w '%{http_code}\n' -H 'content-type: application/dns-message' --data-binary @/tmp/q.bin https://dns.alidns.com/dns-query   # 期望 200
```

VIF 无全局 IPv6（排除链路本地 fe80:: 与回环 ::1，macOS lo0 恒有 ::1）：

```bash
ifconfig | grep "inet6" | grep -v "fe80::" | grep -v "::1"   # 无输出 ✓
```

> 前提：本步与 STUN 实测均要求 Surge 开启增强模式（TUN 接管默认路由）。仅系统代理模式下裸 UDP 不经 Surge，STUN 会收到响应——需先开增强模式再测。例外进程/设备自身的 UDP 属于放行目标，不受阻断预期约束。

### 7. 幂等确认

```bash
R_BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM data_migration_records;")
L_BEFORE=$(grep -c "数据迁移" "$LOG"); ./surgeconf.sh restart && sleep 4; \
L_AFTER=$(grep -c "数据迁移" "$LOG"); R_AFTER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM data_migration_records;"); \
echo "日志 $L_BEFORE -> $L_AFTER；记录 $R_BEFORE -> $R_AFTER"
```

判定：`L_AFTER == L_BEFORE` 且 `R_AFTER == R_BEFORE`（无新增迁移日志行，也无新增记账记录；历史已有行不计增量。注意零变更迁移不打印日志，故幂等判定必须包含记录数比对）。

## 三平台 UDP 验收清单

> 目的：在真实目标设备上确认 UDP 安全模式的行为边界。**物理设备上的加载与观察必须人工完成**；本仓库自动化已覆盖的部分标注「本机已观察」，其他设备升级后跑测试即可，无需重复。
>
> **记录纪律**：验收记录只写「状态（通过/失败）、规则类别、时间」，不写连接目标、域名、节点、进程名或 MAC 地址等值。

### 本机自动化已观察（CI / 开发机）

| 项 | 手段 |
|---|---|
| 三模式托管行数量与顺序（strict 四条 / scoped 例外后四条 / compatible 零托管） | `tests/test_udp_policy.py`、`tests/test_generator.py` |
| 产物无项目托管的全局 UDP/443 拒绝、General 强制键（`block-quic = per-policy`、`udp-policy-not-supported-behaviour = REJECT`） | `tests/test_generator.py` |
| iOS 产物无 `PROCESS-*` / `MAC-ADDRESS` 规则 | `tests/test_target_rule_capabilities.py` |
| Mac 两目标产物通过 Surge Mac 6.6.0 `surge-cli --check` | 开发机 CLI 校验（本计划 Task 6 执行） |
| 仓库文档无真实 MAC / 私网地址 / 用户目录 / 凭据 | `tests/test_open_source_safety.py` |

### 工作站（mac_workstation / scoped）——人工

1. 管理界面生成并下载该档案配置，Surge Mac 重载。
2. 终端查看生效规则（**仅人工观察规则名称与顺序；不要把完整输出粘贴到工单/issue——其中含你的真实规则与订阅 URL**）：

   ```bash
   '/Applications/Surge.app/Contents/Applications/surge-cli' --raw dump rule
   ```

   预期：例外进程 DIRECT 规则位于四条 STUN REJECT 之前；无项目托管的全局 UDP/443 拒绝行（若仍见该行，可能来自迁移范围外保留的已有用户规则，或迁移记账后新增/重建的用户规则——不能据出现判迁移失败；不需要可自行删除）。
3. 启动配置为例外的实时视频/通话应用，确认画面与声音正常（UDP 放行生效）。
4. 浏览器 WebRTC 检测页：不出现公开直连 STUN 候选（例外进程之外的防护仍生效）。

记录模板：`[时间] 规则顺序=通过；例外进程视频=通过；WebRTC无直连候选=通过`（只写结论，不写值）。

### 家庭网关（mac_gateway / scoped）——人工

1. 在 Surge Mac 的 Dashboard 为被测设备**关闭 Fast Path**（Fast Path 中的合格 UDP 会绕过规则/代理/MITM，不关则例外与阻断都不会生效；CLI 不支持逐设备 Fast Path，只能 Dashboard 手工操作）。
2. 加载档案后，在 Dashboard 请求查看器确认例外设备的 UDP 命中 MAC DIRECT 例外；该设备播放实时视频成功。
3. 任一**未配置例外的设备**访问 STUN（UDP 3478/19302）仍被 REJECT——防护未被例外削弱。
4. 验收完成后按设备实际需求保留或恢复 Fast Path。

记录模板：`[时间] FastPath关闭=通过；例外设备视频=通过；未配置设备STUN=阻断`。

### iOS（ios_mobile / compatible）——人工

1. Surge iOS 5.14.6 导入该档案，通过 **Profile Syntax Check**（本机 Mac 的 `surge-cli --check` 不能替代 iOS 端检查）。
2. 配置确认无 `PROCESS-*` / `MAC-ADDRESS` 规则（iOS Surge 不支持进程/MAC 匹配；compatible 亦无托管 STUN 行）。
3. 完成一次真实实时 UDP 视频通话（compatible 模式 UDP 兼容性预期最好）。
4. 知悉 compatible 的 WebRTC 直连候选风险；介意隐私时改 strict（受管 STUN 被阻断，WebRTC 直连候选随之不可用）。

记录模板：`[时间] SyntaxCheck=通过；无PROCESS/MAC规则=通过；实时视频=通过`。

## 运行时可观察性最终清单（本任务新增）

升级到含结构化事件日志的版本后，逐项确认（全程不外发日志原文）。先固定两个路径变量：

```bash
EVENT_LOG="$HOME/Library/Logs/SurgeConf/surgeconf.log"
CONSOLE_LOG="$HOME/Library/Logs/SurgeConf/surgeconf-console.log"
```

1. **双日志就位**：`"$EVENT_LOG"`（结构化事件，0600，目录 0700）与 `"$CONSOLE_LOG"`（launchd/nohup 的 stdout/stderr）同时存在且内容不同；`"$EVENT_LOG"` 只有 `ts=… event=… schema_version=1` 形式的单行记录。
2. **启动事件序列**（查 `"$EVENT_LOG"`）：重启服务后出现 `database_ready` → `orphan_cleanup` → `scheduler_active`（或 `scheduler_standby`，意外双进程时）。
3. **同步事件**（查 `"$EVENT_LOG"`）：手动或自动同步一次后出现 `subscription_sync`；失败场景 `success=false error_code=<五码之一>`。
4. **console 侧启动输出**（查 `"$CONSOLE_LOG"`）：能看到 `[启动] 数据库就绪`；再次重启（幂等）时 `[数据迁移]` 各行计数为 0 或不再出现新变更项。
5. **无秘密回归**（只输出 PASS/FAIL 与计数，绝不打印匹配行）：
   ```bash
   COUNT=$(grep -Ec '://|password|token' "$EVENT_LOG" || true)
   if [ "$COUNT" = "0" ]; then echo "PASS: 事件日志无秘密模式命中";    else echo "FAIL: 事件日志命中 $COUNT 行（请勿粘贴内容，按第 7 节纪律采集 event/error_code）"; fi
   ```
   （`grep -c` 零匹配退出码为 1，`|| true` 保证脚本继续。）
6. **plist 指向**：`plutil -p ~/Library/LaunchAgents/com.surgeconf.plist | grep -i standard` 两个路径均以 `surgeconf-console.log` 结尾（绝不指向 `surgeconf.log`）。
7. **人工验收**：按 `docs/RUNTIME_OPERATIONS.md` 第 8 节在真实设备执行（两 Mac `surge-cli --check`、iOS 静态检查后导入、实时视频、AI 命中）——本文不宣称物理验收通过。

## 静默升级操作（其他设备）

```bash
cd <SurgeConf 目录> && git pull && ./venv/bin/python -m pip install -r requirements.txt && ./surgeconf.sh restart
```

- 迁移自动执行、幂等、无交互；用户自定义值（非旧默认）不会被覆盖。升级链为 fail-fast：`git pull` 或依赖安装（`pip install -r requirements.txt`）任一失败即中止，不会重启服务——修复后重新执行整条命令。
- SurgeConf 侧完成后，Surge 客户端需重载 profile（若开启「自动重载外置配置」则完全静默）。
- 前端静态资源版本随 git HEAD 自动更新（`?v=<hash>`），浏览器普通刷新即拉新，无需清缓存。

## 故障排查

| 现象 | 处置 |
|---|---|
| claudeusercontent 规则未补齐 | 先看启动日志有无异常栈；仍缺则手动插入（重启服务后生效）：`sqlite3 "$DB" "INSERT INTO custom_rules (profile_id, rule_text, position, sort_order, enabled, comment) VALUES (NULL,'DOMAIN-SUFFIX,claudeusercontent.com,AI服务','pre',95,1,'Claude Artifacts 用户内容 CDN 归组 AI 服务');"`。**托管 STUN 规则不落库，缺失时勿手工插行**——检查该档案 `udp_security_mode`（strict/scoped 才注入）并重新生成 |
| ipv6-vif 仍旧值 | 迁移只归一 auto/native/off/disable 四个旧值，`always` 视为用户选择不动；确认库值后手工 UPDATE 为 disabled |
| DNS 未升级 | 迁移只精确匹配旧默认串；该设备曾自定义 DNS 则有意跳过（不算失败），需要新默认时手工改或删键重启 |
| STUN 仍通 | 确认该档案 UDP 模式非 compatible；确认 Surge 重载了新生成配置（SurgeConf 生成 ≠ Surge 已加载）；确认增强模式开启 |
| 例外进程/设备的 UDP 仍被拒 | 档案是 strict（strict 不输出例外）；或例外被禁用；或网关侧该设备 Fast Path 未关闭；或配置未重载——见 `docs/UDP_COMPATIBILITY.md` 排障表 |
