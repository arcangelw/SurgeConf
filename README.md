# SurgeConf

**⚠️ 重要提示：使用前请阅读完整的[免责声明](#免责声明)。继续使用本软件即表示您接受其全部条款。**

Surge 配置管理工具 — 提供 Web 界面管理 Surge 配置文件，支持多订阅管理、节点分组、自定义规则、配置方案切换、中英双语输出。

> **本工具不提供、不分发、不内置任何代理服务器、VPN 节点或代理订阅源。所有数据仅在本地处理。**

## 快速开始

### 前置要求

- Python 3.10+

#### 方式 A：使用 mise（推荐）

[mise](https://mise.jdx.dev) 自动管理 Python 版本，进入项目目录即生效：

```bash
mise install python@3.13
cd SurgeConf  # 自动读取 .mise.toml 切换版本
```

#### 方式 B：不使用 mise

确保系统已安装 Python 3.10+：

```bash
python3 --version  # 确认 >= 3.10
```

### 克隆并启动

```bash
# 克隆项目
git clone https://github.com/arcangelw/SurgeConf.git
cd SurgeConf

# 方式一：一步到位（自动创建 venv + 安装依赖 + 启动）
./start.sh

# 方式二：使用管理脚本（自动创建 venv + 前台运行）
./surgeconf.sh run

# 方式三：守护模式（后台运行，需先启动一次以创建 venv）
./start.sh                # 首次运行自动创建 venv
./surgeconf.sh start      # launchd 后台运行
```

访问 http://127.0.0.1:61830

## 功能概览

| 功能 | 说明 |
|------|------|
| 多订阅管理 | 添加多个订阅源，标签 + 颜色区分，一键同步 |
| 节点地区分组 | 自动正则匹配 + 手动分配，按地区归类节点 |
| 节点绑定服务 | 将特定节点绑定到 AI、Netflix 等服务分组 |
| 策略分组 | 管理地区组和服务组的策略链，支持拖拽排序 |
| 规则管理 | 可视化管理 RULE-SET 规则源，启用/禁用/排序/自定义 |
| 自定义规则 | 前置规则 + 通用规则，独立管理 |
| Host 映射 | 结构化列表管理 Host 映射，预置 DNS 选项 |
| 配置方案 | 创建多套配置（日常/工作/英文），按场景切换 |
| 中英双语 | 同一份数据可生成中文或英文分组名的配置文件 |
| 管理 API | 配置方案的 CRUD 及远程生成调用 |
| 自动测速分组 | 配置节点的自动测速策略组参数 |
| 服务管理 | surgeconf.sh 一站式管理脚本 |
| 开机自启 | macOS launchd 登录时自动启动 |

---

## 服务管理

```bash
./surgeconf.sh {start|stop|restart|run|enable|disable|status|log}
```

| 命令 | 功能 |
|------|------|
| `start` | 启动服务（launchd 守护模式） |
| `stop` | 停止服务 |
| `restart` | 重启服务 |
| `run` | 独立启动（前台模式，不使用 launchd） |
| `enable` | 注册开机自启 |
| `disable` | 取消开机自启 |
| `status` | 查看运行状态 |
| `log` | 实时查看日志 |

### 开机自启

注册后 macOS 登录时自动启动 SurgeConf，崩溃后自动恢复：

```bash
./surgeconf.sh enable   # 注册开机自启
./surgeconf.sh disable  # 取消开机自启
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SURGE_HOST` | `127.0.0.1` | 监听地址（前台运行）；开机自启（plist）固定为 `0.0.0.0` 以支持局域网访问 |
| `SURGE_PORT` | `61830` | 监听端口 |
| `SURGE_API_TOKEN` | — | API 认证 Token（留空不启用认证） |
| `SURGE_CONTROLLER_ACCESS` | `arcangelw@0.0.0.0:6160` | Surge 外部控制器访问凭据 |
| `SURGE_HTTP_API` | `arcangelw@0.0.0.0:6166` | Surge HTTP API 访问凭据 |

> 监听地址说明：前台运行（`run`）默认 `127.0.0.1` 仅本机访问；注册开机自启（`enable`）生成的 plist 中固定使用 `0.0.0.0` 以支持局域网访问。如需在前台运行时也暴露到局域网：`SURGE_HOST=0.0.0.0 ./surgeconf.sh run`。

## API 文档

http://127.0.0.1:61830/docs

## API 认证

默认情况下所有 API 无需认证即可访问。如需保护 API 端点，设置环境变量即可启用 Bearer Token 认证：

```bash
export SURGE_API_TOKEN=your_token_here
./surgeconf.sh run
```

启用后，API 请求需要携带 `Authorization: Bearer your_token_here` 头，页面和静态资源不受影响。

---

## 使用场景与自定义规则指南

### 场景一：企业内网直连

企业环境中有大量内网域名和 IP 段需要绕过代理直连，避免影响办公系统访问。

**需要做的：**

1. **添加内网域名规则** — 在「规则管理」的「通用自定义规则」中添加：
   ```
   DOMAIN-SUFFIX,internal.company.com,DIRECT
   DOMAIN-SUFFIX,corp.local,DIRECT
   DOMAIN,intranet,DIRECT
   ```

2. **添加内网 IP 段** — 企业内网通常使用私有 IP 段：
   ```
   IP-CIDR,10.0.0.0/8,DIRECT
   IP-CIDR,172.16.0.0/12,DIRECT
   IP-CIDR,192.168.0.0/16,DIRECT
   ```
   > 注意：默认配置中 `skip-proxy` 已包含这些段，但如果你需要更精细的控制（比如某些内网段走代理），可以通过规则覆盖。

3. **添加企业 Host 映射** — 在配置方案的 Host 映射中添加：
   ```
   *.internal.company.com → server:10.0.1.100
   gitlab.corp → server:10.0.2.50
   ```

4. **指定内网 DNS** — 某些企业域名只能通过内部 DNS 解析，在 General 中配置：
   ```
   dns-server = 223.5.5.5, 10.0.1.1, 10.0.1.2
   ```
   或在 Host 映射中直接指定。

---

### 场景二：企业 VPN 共存

使用 Surge 的同时需要连接企业 VPN，两者可能冲突。

**需要做的：**

1. **VPN 网段直连** — 将 VPN 分配的网段加入通用自定义规则：
   ```
   IP-CIDR,100.64.0.0/10,DIRECT
   ```

2. **VPN 相关域名直连** — VPN 拨号、认证相关的域名不走代理：
   ```
   DOMAIN-SUFFIX,vpn.company.com,DIRECT
   DOMAIN-SUFFIX,otp.company.com,DIRECT
   ```

3. **排除 VPN 接口** — 在 General 中设置（Surge 默认已处理大部分场景）：
   ```
   include-all-networks = false
   include-local-networks = false
   ```

4. **创建「工作模式」配置方案** — 专门为办公场景创建一个配置方案，开启 VPN 相关规则，日常使用时切回默认方案。

---

### 场景三：自定义 DNS 策略

某些场景需要特定域名使用特定 DNS 服务器解析。

**需要做的：**

在配置方案的 Host 映射中添加，支持预置 DNS 选项：

| 目标值 | 说明 |
|--------|------|
| `server:system` | 系统默认 DNS |
| `server:223.5.5.5` | 阿里 DNS |
| `server:119.29.29.29` | 腾讯 DNSPod |
| `server:8.8.4.4` | Google DNS |
| `server:1.1.1.1` | Cloudflare DNS |
| 自定义 IP | 直接映射到指定 IP 地址 |

---

### 场景四：开发者环境

开发工作中需要访问 GitHub、Docker Hub、NPM 等服务，同时部分内网开发环境需要直连。

**需要做的：**

1. **开发服务走代理** — 默认规则已包含 GitHub、Docker 等，如需自定义可在通用自定义规则中添加。

2. **内网开发环境直连** — 通用自定义规则添加：
   ```
   DOMAIN-SUFFIX,dev.local,DIRECT
   DOMAIN-SUFFIX,staging.internal,DIRECT
   IP-CIDR,10.10.0.0/16,DIRECT
   ```

3. **API 测试环境** — 某些测试 API 需要指定 IP 或 DNS，在 Host 映射中添加。

4. **修复 GitHub 429** — 默认配置已包含 Header Rewrite，将 GitHub 请求的语言设为英文避免限速。

---

### 场景五：家庭/共享网络

多人共享同一网络，或需要 Surge 作为网关为其他设备提供代理。

**需要做的：**

1. **开启 Wi-Fi 分享** — 在 General 中：
   ```json
   {
     "allow-wifi-access": true,
     "wifi-access-http-port": 6152,
     "wifi-access-socks5-port": 6153
   }
   ```

2. **自定义远程控制器** — 在「系统设置」中修改控制器用户名和端口，其他设备可通过 API 管理 Surge。

3. **屏蔽特定设备广告** — 通过规则管理中的广告拦截规则，已默认启用 SKK 规则集。如需额外屏蔽，在通用自定义规则中添加：
   ```
   DOMAIN-SUFFIX,ads.example.com,REJECT
   DOMAIN-KEYWORD,tracker,REJECT
   ```

---

### 场景六：多配置方案切换

创建多个配置方案，在不同场景下快速切换。

**推荐方案：**

| 方案名 | 语言 | 用途 |
|--------|------|------|
| 默认配置 | 中文 | 日常使用 |
| 工作模式 | 中文 | 企业内网规则、VPN 共存 |
| 流媒体 | 中文 | 绑定流媒体节点 |
| English | English | 英文分组名（给非中文用户） |

每个方案可以独立设置：
- **输出语言**（中/英文分组名）
- **General 设置**（控制器、DNS、Wi-Fi 分享等）
- **前置自定义规则**（覆盖规则集的匹配）
- **通用自定义规则**（补充规则集未覆盖的域名）
- **Host 映射**（企业 DNS、测试环境等，支持预置 DNS 选项）
- **URL/Header Rewrite**
- **MITM 配置**

---

### 场景七：MITM 证书配置

MITM（中间人解密）是 URL Rewrite、Header Rewrite、Script 等功能的前提。Surge 需要拦截 HTTPS 流量才能改写响应。

#### 配置步骤

**1. 生成证书** — Surge → 设置 → MITM → 配置证书 → 生成新的 CA 证书

**2. 安装证书到系统** — Surge → 设置 → MITM → 安装证书

**3. 信任证书** — 系统设置 → 通用 → 关于本机 → 证书信任设置 → 开启 Surge CA 的完全信任

**4. 在本工具中配置 MITM** — 在配置方案中编辑 MITM 配置：

```json
{
  "skip-server-cert-verify": true,
  "tcp-connection": true,
  "h2": true,
  "hostname": "www.google.cn, api.abema.io, *.zhihu.com, -CUSTOMMitM, sub.store"
}
```

**hostname 字段说明：**

| 写法 | 含义 |
|------|------|
| `example.com` | 仅匹配该域名 |
| `*.example.com` | 匹配所有子域名 |
| `-example.com` | 排除该域名（前缀减号） |

#### 安全提示

- `skip-server-cert-verify = true` 会跳过验证 Surge 到目标服务器之间的证书，在公共网络上有安全风险
- 生产环境建议设为 `false`，仅在内网或可信网络下使用 `true`
- MITM 证书是本地生成的，不会上传到任何服务器

---

## 规则优先级

生成的 Surge.conf 规则按以下顺序排列：

```
 1. 前置自定义规则  → 覆盖规则集的匹配（如 apple-relay → AI、googleapis → AI）
 2. 规则修正        → DIRECT（修复连接问题）
 3. 广告拦截        → REJECT（SKK 规则集）
 4. 国内应用        → 微信、网易云、B站、微博
 5. Apple 服务      → App Store、Apple News、Apple TV
 6. AI 服务         → OpenAI、Claude、Gemini、Bing
 7. 流媒体          → Disney+、Netflix、TikTok、YouTube
 8. 地区流媒体解锁  → US、EU、JP、KR、HK、TW
 9. 社交媒体        → Twitter、Telegram、Facebook、Instagram
10. 其他国外服务    → OneDrive、Microsoft、GitHub
11. 国内规则        → SKK + ChinaMax 规则集
12. 国外规则        → CDN、Global 规则集
13. 通用自定义规则  → 补充规则集未覆盖的域名
14. 本地网络        → LAN DIRECT
15. 兜底规则        → FINAL 兜底
```

**前置规则 vs 通用规则 vs Host 映射：**

| 类型 | 位置 | 适用场景 |
|------|------|----------|
| 前置自定义规则 | 规则集之前 | 覆盖规则集的匹配结果，如将 apple-relay 重定向到 AI 服务 |
| 通用自定义规则 | 规则集之后 | 补充规则集未覆盖的域名，如特定网站走代理 |
| 默认 Host 映射 | [Host] 段 | 全局默认 DNS/IP 映射，配置方案可单独覆盖 |

## 许可证

本项目采用 **MIT License + Additional Terms**，详见 [LICENSE](LICENSE) 文件。使用本软件即表示您已阅读并同意该许可证的全部条款。

## 免责声明

**简要声明（完整法律条款见 [LICENSE](LICENSE) 文件中的 Additional Terms）：**

| 事项 | 说明 |
|------|------|
| 软件性质 | 本工具是一个本地运行的配置文件管理界面，不提供/不分发/不内置任何代理服务器、VPN 节点或订阅源 |
| 数据隐私 | 所有数据仅在本地 SQLite 数据库中存储和处理，不收集、不上传任何用户信息 |
| 第三方内容 | 默认引用的规则集来自第三方开源项目，作者对其内容不承担责任；用户自行添加的内容由用户负责 |
| 使用责任 | 请确保您的使用行为符合所在国家或地区的法律法规 |
| 争议管辖 | 本声明受中华人民共和国法律管辖，争议提交作者所在地有管辖权的人民法院 |

---

## 致谢

默认规则配置参考了 [ClashConnectRules/Surge](https://github.com/ClashConnectRules/Surge.git) 项目，感谢其整理的高质量规则集。

---

## 项目结构

```
SurgeConf/
├── app/
│   ├── main.py              # FastAPI 应用入口 + 种子数据
│   ├── models.py            # 数据库模型（含 CustomRule、HostMapping）
│   ├── database.py          # 数据库配置 + 迁移
│   ├── i18n.py              # 多语言支持
│   ├── default_config.py    # 默认配置 + 翻译映射 + DNS 预置选项
│   ├── general_fields.py    # General 字段定义
│   ├── routers/
│   │   ├── subscriptions.py # 订阅管理 API
│   │   ├── nodes.py         # 节点管理 + 绑定服务 API
│   │   ├── groups.py        # 策略分组 API
│   │   ├── configs.py       # 配置方案 + 自定义规则 + Host 映射 + 生成 API
│   │   └── general.py       # General 设置 API
│   ├── services/
│   │   ├── subscription.py  # 订阅拉取与节点解析
│   │   └── generator.py     # Surge 配置生成器
│   ├── templates/           # HTML 页面模板
│   └── static/              # CSS + JS
├── data/                    # SQLite 数据库 + 生成的配置
├── tests/                   # 单元测试
├── start.sh                 # 启动脚本（开发模式）
├── surgeconf.sh             # 服务管理脚本（启停/自启/状态）
├── run_surgeconf.sh         # launchd 包装脚本（动态路径解析）
└── requirements.txt
```
