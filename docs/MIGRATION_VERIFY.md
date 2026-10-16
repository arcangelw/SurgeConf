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

1. **WebRTC 防泄漏规则** 4 条前置规则补齐（域名、STUN 端口×2、协议级 STUN）
2. **Claude 配套规则** `DOMAIN-SUFFIX,claudeusercontent.com,AI服务` 补齐（Artifacts CDN 归组）
3. **存量档案 `ipv6-vif` 归一**：`auto/native/off/disable → disabled`（官方规范值；auto 按既定策略强制关闭）
4. **DNS 默认值升级**（仅精确匹配旧默认值时升级，用户自定义不动）：
   - `encrypted-dns-server`：双 DoH → `h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query`（三路并发先答先用）
   - `hijack-dns`：`8.8.8.8:53, 8.8.4.4:53` → `*:53`（全劫持明文 53）
5. **`no_resolve_keywords` 系统键补齐**（IP 类规则集 no-resolve 关键词，供面板管理；生成器有代码兜底）
6. **删除死配置 `geoip-maxmind-url`**：产物无 GEOIP 规则（国内判定走 domestic.ip/ChinaMax/ASN China 三集），该 mmdb 库不会被查询——省一次下载与常驻内存

   验证：`sqlite3 "$DB" "SELECT COUNT(*) FROM config_profiles WHERE general LIKE '%geoip-maxmind-url%';"` 返回 0，且 preview 产物无 geoip 行、url-test 组均为 `tolerance=100`（官方默认，消除 50ms 容差导致的抖动切换与连接重建）。
7. **关闭 Surge 内置 Web 面板**：`http-api-web-dashboard` → `false`（常驻内存；SurgeConf 面板不依赖它，官方默认亦为 false。Surge 的 HTTP API 端口不受影响）

   验证：preview 产物含 `http-api-web-dashboard = false`。
8. **抖音优先直连**：补齐 `DouYin` 规则源并放在 `TikTok` 之前。两者都覆盖 `snssdk.com`，优先直连可避免抖音评论、视频和直播误走 TikTok 海外出口。

   验证：`SELECT name,action,sort_order FROM rule_sources WHERE name IN ('DouYin','TikTok') ORDER BY sort_order;` 中 `DouYin / 大陆直连` 位于 `TikTok` 之前。
9. **恢复媒体 QUIC 性能**：删除旧版全局 `UDP/443 → REJECT`，并补 `block-quic = per-policy`。DIRECT 保留 QUIC/HTTP3，代理按 UDP 能力自适应处理；STUN 专用防泄漏规则继续生效。

## 验证步骤

### 1. 迁移日志

```bash
grep "数据迁移" "$LOG"
```

- 老库首次启动：应有一条 `[数据迁移] 补齐内置安全规则 N 条，修正存量 ipv6-vif M 个档案，升级 General 配置 G 个档案，补系统键 K 项，补抖音直连规则源 Y 项，移除全局 QUIC 阻断 Q 条`（N≤5、M≥0、G≥0、K≤1、Y≤1、Q≤1 均正常——老库可能已有部分规则/值；出现该行即迁移已执行）
- 新库或已迁移库：无输出（幂等，无变更不打印）

### 2. WebRTC / Claude 规则落库

```bash
sqlite3 "$DB" "SELECT rule_text FROM custom_rules WHERE rule_text LIKE '%stun%' OR rule_text LIKE '%3478%' OR rule_text LIKE '%19302%' OR rule_text LIKE '%443)),REJECT%' OR rule_text LIKE '%claudeusercontent%' ORDER BY sort_order;"
```

预期包含以下 5 行（历史自建规则含这些关键词会多出其他行，集合包含即通过）：

```
DOMAIN-KEYWORD,stun,REJECT
AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT
AND,((PROTOCOL,UDP), (DEST-PORT,19302)),REJECT
PROTOCOL,STUN,REJECT
DOMAIN-SUFFIX,claudeusercontent.com,AI服务
```

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

### 5. 生成产物检查

```bash
curl -s "http://127.0.0.1:61830/api/configs/preview?profile_id=1"
```

产物应包含（判定条件为存在性行匹配，非逐字全文比对）：

```
ipv6 = false
ipv6-vif = disabled
encrypted-dns-server = h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query
hijack-dns = *:53
block-quic = per-policy
DOMAIN-KEYWORD,stun,REJECT                          ← [Rule] 段、第一条 RULE-SET 之前
PROTOCOL,STUN,REJECT
RULE-SET,<URL 含 /ip/ 的规则集>,<任意策略>,no-resolve  ← 至少一行以 ,no-resolve 结尾（DNS 泄漏防护）
```

说明：RULE-SET 行的策略随设备配置而异，判定依据是「URL 含 /ip/ 且以 ,no-resolve 结尾」而非具体策略名。

### 6. 运行时实测（Surge 已重载新配置后）

STUN 阻断（应全部超时，约 4 秒后返回 TIMEOUT）：

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

> 前提：本步与 STUN 实测均要求 Surge 开启增强模式（TUN 接管默认路由）。仅系统代理模式下裸 UDP 不经 Surge，STUN 会收到响应——需先开增强模式再测。

### 7. 幂等确认

```bash
BEFORE=$(grep -c "数据迁移" "$LOG"); ./surgeconf.sh restart && sleep 4; \
AFTER=$(grep -c "数据迁移" "$LOG"); echo "$BEFORE -> $AFTER"
```

判定：`AFTER == BEFORE`（无新增迁移日志行；历史已有行不计增量）。

## 静默升级操作（其他设备）

```bash
cd <SurgeConf 目录> && git pull && ./surgeconf.sh restart
```

- 迁移自动执行、幂等、无交互；用户自定义值（非旧默认）不会被覆盖。
- SurgeConf 侧完成后，Surge 客户端需重载 profile（若开启「自动重载外置配置」则完全静默）。
- 前端静态资源版本随 git HEAD 自动更新（`?v=<hash>`），浏览器普通刷新即拉新，无需清缓存。

## 故障排查

| 现象 | 处置 |
|---|---|
| 规则未补齐 | 先看启动日志有无异常栈；仍缺则手动插入（重启服务后生效）：`sqlite3 "$DB" "INSERT INTO custom_rules (profile_id, rule_text, position, sort_order, enabled, comment) VALUES (NULL,'PROTOCOL,STUN,REJECT','pre',94,1,'DPI 级阻断任意端口 STUN，兜底非标端口探测'),(NULL,'DOMAIN-SUFFIX,claudeusercontent.com,AI服务','pre',95,1,'Claude Artifacts 用户内容 CDN 归组 AI 服务');"` |
| ipv6-vif 仍旧值 | 迁移只归一 auto/native/off/disable 四个旧值，`always` 视为用户选择不动；确认库值后手工 UPDATE 为 disabled |
| DNS 未升级 | 迁移只精确匹配旧默认串；该设备曾自定义 DNS 则有意跳过（不算失败），需要新默认时手工改或删键重启 |
| STUN 仍通 | 确认 Surge 重载了新生成配置（SurgeConf 生成 ≠ Surge 已加载）；确认增强模式开启 |
