# 数据迁移验证指南（AI 可执行）

> 适用：多设备部署 SurgeConf。设备拉取包含迁移机制的代码并 `./surgeconf.sh restart` 后，
> 按本文逐步验证迁移是否生效。全部通过即报告「迁移验证通过」。
> 迁移逻辑：`app/main.py::apply_data_migrations`（幂等，每次启动执行）。

## 迁移内容

1. **STUN 防护规则**（防 WebRTC 泄露真实出口）三条前置规则补齐
2. **存量档案 `ipv6-vif: auto → disable`**（与 `ipv6 = false` 配合彻底禁用 IPv6）
3. **`no_resolve_keywords` 系统键补齐**（IP 类规则集 no-resolve 关键词，供面板管理；生成器有代码兜底）

## 验证步骤

### 1. 迁移日志

```bash
grep "数据迁移" logs/surgeconf.log
```

- 老库首次启动：应有一条 `[数据迁移] 补齐内置安全规则 N 条，修正存量 ipv6-vif M 个档案，补系统键 K 项`（N≤3、M≥0、K≤1 均正常——老库可能已有部分同文本规则、vif 已非 auto、或键已存在；出现该行即迁移已执行）
- 新库或已迁移库：无输出（幂等，无变更不打印）

### 2. STUN 规则落库

```bash
sqlite3 data/surge_conf.db "SELECT rule_text FROM custom_rules WHERE rule_text LIKE '%stun%' OR rule_text LIKE '%3478%' OR rule_text LIKE '%19302%' ORDER BY sort_order;"
```

预期包含以下 3 行（若历史自建规则文本含这些关键词会多出其他行，集合包含即通过）：

```
DOMAIN-KEYWORD,stun,REJECT
AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT
AND,((PROTOCOL,UDP), (DEST-PORT,19302)),REJECT
```

### 3. ipv6-vif 存量修正

```bash
sqlite3 data/surge_conf.db "SELECT id, name, json_extract(general,'$.ipv6-vif') FROM config_profiles;"
```

预期：所有档案均为 `disable`（NULL 也合法 = 继承模板默认 disable）。

### 3b. no_resolve_keywords 系统键落库

```bash
sqlite3 data/surge_conf.db "SELECT value FROM system_config WHERE key='no_resolve_keywords';"
```

预期恰好一行：`(ip),ASN,/ip/,Lan`（表名为单数 system_config）。

### 4. 生成产物检查

```bash
curl -s "http://127.0.0.1:61830/api/configs/generate"   # 返回完整配置文本
```

产物应包含（判定条件为存在性行匹配，非逐字全文比对）：

```
ipv6 = false
ipv6-vif = disable
DOMAIN-KEYWORD,stun,REJECT                          ← [Rule] 段、第一条 RULE-SET 之前
AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT
AND,((PROTOCOL,UDP), (DEST-PORT,19302)),REJECT
RULE-SET,<URL 含 /ip/ 的规则集>,<任意策略>,no-resolve  ← 至少一行以 ,no-resolve 结尾（DNS 泄漏防护）
```

说明：RULE-SET 行的策略随设备配置而异（大陆直连/美国/REJECT 等），判定依据是「URL 含 /ip/ 且以 ,no-resolve 结尾」而非具体策略名。

### 5. 运行时实测（Surge 已重载新配置后）

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

VIF 无全局 IPv6（排除链路本地 fe80:: 与回环 ::1，macOS lo0 恒有 ::1）：

```bash
ifconfig | grep "inet6" | grep -v "fe80::" | grep -v "::1"   # 无输出 ✓
```

> 前提：本步与 STUN 实测均要求 Surge 开启增强模式（TUN 接管默认路由）。仅系统代理模式下裸 UDP 不经 Surge，STUN 会收到响应——需先开增强模式再测。

### 6. 幂等确认

```bash
BEFORE=$(grep -c "数据迁移" logs/surgeconf.log); ./surgeconf.sh restart && sleep 4; \
AFTER=$(grep -c "数据迁移" logs/surgeconf.log); echo "$BEFORE -> $AFTER"
```

判定：`AFTER == BEFORE`（无新增迁移日志行；历史已有行不计增量）。

## 故障排查

| 现象 | 处置 |
|---|---|
| 规则未补齐 | 先看启动日志有无异常栈；仍缺则手动插入（重启服务后生效）：`sqlite3 data/surge_conf.db "INSERT INTO custom_rules (profile_id, rule_text, position, sort_order, enabled, comment) VALUES (NULL,'DOMAIN-KEYWORD,stun,REJECT','pre',90,1,'阻断 STUN 防止 WebRTC 泄露真实出口'),(NULL,'AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT','pre',91,1,'阻断 IP 直连 STUN (UDP 3478)'),(NULL,'AND,((PROTOCOL,UDP), (DEST-PORT,19302)),REJECT','pre',92,1,'阻断 IP 直连 STUN (UDP 19302)');"` |
| ipv6-vif 仍 auto | 迁移只改 `auto`，其他值视为用户自定义不动；确认库值后手工 UPDATE |
| STUN 仍通 | 确认 Surge 重载了新生成配置（SurgeConf 生成 ≠ Surge 已加载） |
