# SurgeConf 升级交接文档（供各设备 AI 执行）

> 本文档由主设备的维护会话生成。目的：`docs/MIGRATION_VERIFY.md` 覆盖**能静默迁移的部分**
> （git pull + restart 自动生效）；本文档覆盖**无法静默处理、必须人工/AI 介入的工作**。
> 各设备 AI 按顺序逐项执行并回报。


## 一、背景与提交链

以下 commit 均未推送（以 `git log origin/main..HEAD` 实际输出为准）：

| commit | 内容 |
|---|---|
| `e8250ff`…`69f5739` | 前端 9 页收拢为 5 页 |
| `b4ad7d8` | 静态资源版本参数（`?v=<git hash>`）根治浏览器缓存 |
| `63fe6e3` | 修复配置预览弹窗不显示 |
| `f059767` | General 表单紧凑化 |
| `0536b04` | ipv6-vif 统一官方规范值 disabled |
| `c14eed5` | DNS 优化：阿里 DoH3 三路并发 + hijack-dns `*:53` 全劫持 |
| `7fb101b` | WebRTC 防泄漏补口（UDP 443/QUIC + PROTOCOL,STUN）+ 静默分发闭环 |

## 二、各设备标准流程（先跑这个）

```bash
cd <SurgeConf 目录>
git pull && ./surgeconf.sh restart && sleep 3
./venv/bin/python -m pytest tests/ -q        # 应 110 passed
```

然后按 `docs/MIGRATION_VERIFY.md` 全文自检（AI 可执行，全部通过即报告「迁移验证通过」）。

## 三、无法静默处理的工作清单

以下各项**迁移不会自动完成**，逐项人工/AI 处理：

### 1. Surge 客户端重载配置（每台设备必须）

SurgeConf 只生成配置，Surge 不会自动加载。

- 操作：Surge → 重新载入对应 profile（或在 SurgeConf 面板点「应用配置」）
- 若 Surge 开启了「自动重载外置配置」则此步可跳过（检查 Settings → Profile → Auto Reload）
- 验收：Surge 事件日志出现 reload 记录

### 2. Surge 版本兼容校验（老版本客户端必须）

新配置含近年特性：`h3://` 加密 DNS、`PROTOCOL,STUN` 规则、`AND` 逻辑规则。

- 操作：Surge 菜单 → Profile → Profile Syntax Check（语法校验）
- 过旧版本对未知参数会警告或忽略；若报「未知规则」，升级 Surge 到最新版后重新校验
- 验收：校验无 error 级问题

### 3. 增强模式确认（防泄漏生效前提）

WebRTC/DNS 防线依赖增强模式（TUN 接管默认路由 + fake-ip）。

- 操作：确认 Surge 增强模式开启（系统出现 utun 接口、默认路由指向 198.18.0.1）
- 验收：`dig +short api.z.ai` 返回 `198.18.x.x`（fake-ip 段）

### 4. DNS 自定义档案人工定夺（仅当验证步骤 4 未通过）

迁移只升级「精确匹配旧默认值」的档案；若该设备曾自定义 DNS（改过端点或 hijack-dns），
迁移**有意跳过**（不算失败）。是否采用新默认由设备主人决定：

- 采用：在 SurgeConf → 方案 → General → DNS 分组，把加密 DNS 改为
  `h3://dns.alidns.com/dns-query, https://dns.alidns.com/dns-query, https://doh.pub/dns-query`，
  劫持 DNS 改为 `*:53`，保存
- 保留自定义：不动，无需任何操作

### 5. WebRTC 泄露复测（验收项）

配置重载后，在浏览器访问 WebRTC 检测页（如 browserleaks.com/webrtc）：

- 预期：无公开 IP 候选（srflx 候选为空或被阻断），不显示真实出口 IP
- 若仍显示中国 UDP 出口：按 MIGRATION_VERIFY.md 故障排查表逐项排查

### 6. 浏览器侧（无需处理，说明性）

前端缓存已由 `?v=<git hash>` 版本参数根治，普通刷新即拉新，**无需清缓存**。

## 四、回滚

任一步骤出问题，回到上一可用状态：

```bash
git log --oneline -10          # 找到问题前的 commit
git checkout <commit> -- . && git checkout <commit>   # 或直接 reset
./surgeconf.sh restart
```

数据库迁移均为幂等且只做「归一/补齐/旧默认升级」，回滚代码后旧数据无破坏，无需回滚 DB。

## 五、回报格式

每台设备完成后向主人回报：

```
设备：<hostname>
迁移验证：通过/未通过（未通过附 MIGRATION_VERIFY 失败步骤）
手工项：1 Surge 重载 ✓/✗；2 语法校验 ✓/✗；3 增强模式 ✓/✗；4 DNS 定夺 <采用/保留/不适用>；5 WebRTC 复测 ✓/✗
```
