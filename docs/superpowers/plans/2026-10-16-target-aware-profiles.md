# 目标感知档案实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个配置档案明确面向工作 Mac、家庭网关 Mac 或 Surge iOS，并严格隔离档案数据、过滤平台参数、兑现最终策略和 AI 分流顺序。

**Architecture:** `ConfigProfile` 保存目标和 UDP 模式，平台能力集中在纯 Python 模块中；生成器先解析唯一档案，再按作用域取数并通过目标能力过滤 General。生成后的规则和策略引用在返回前统一验证，路由层只负责把领域错误映射为 404/422。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、Jinja2、原生 JavaScript、pytest。

**Spec:** `docs/superpowers/specs/2026-10-16-target-aware-surge-config-design.md`

## Global Constraints

- 本计划依赖 `2026-10-16-runtime-data-security-boundary.md` 全部完成。
- 支持目标固定为 `mac_workstation | mac_gateway | ios_mobile`。
- Surge Mac 最低版本为 6.6.0（Build 11270）；Surge iOS 最低版本为 5.14.6。
- 现有档案迁移为 `mac_workstation`，不得自动复制档案或企业配置。
- `ios_mobile` 只允许 `udp_security_mode=strict|compatible`；`scoped` 返回 422。
- 显式档案不存在必须 404；只有完全省略 `profile_id` 才可使用默认档案。
- 生效规则和 Host 集合固定为 `profile_id IS NULL OR profile_id=current_profile.id`。
- `ConfigProfile.final_action` 必须生成最终 `FINAL`；无效或禁用策略引用必须中止生成。
- iOS 产物不得含 Mac 监听器、控制器、`read-etc-hosts`、进程规则或 MAC 规则。
- 本阶段只建立 UDP 模式字段和平台验证，不改变 STUN 规则；UDP 规则由后续 UDP 兼容计划实现。
- 测试和文档只使用 `example.com`、RFC 5737 地址和虚构档案。

## Test Fixture Contracts

- `upgraded_legacy_db` is a temporary copy of the pre-target schema upgraded through Alembic; `sqlite_rows` uses a short-lived sqlite3 connection.
- `two_profiles`/`workstation_profiles` create two persisted synthetic profiles and expose `.first`/`.second`; `add_rule` and `add_host` always commit before generation.
- `ALL_SYNTHETIC_GENERAL_VALUES` contains every declared General key with a type-correct non-secret value.
- `default_profile(db)` returns the single persisted default profile; `index_of_rule_source` and `rule_lines` parse only the generated `[Rule]` section.
- `set_instance_role` and `get_profile` call the authenticated HTTP API rather than mutating ORM objects directly.

---

### Task 1: 档案目标 schema、领域类型和迁移

**Files:**
- Create: `migrations/versions/0003_profile_targets.py`
- Modify: `app/models.py`
- Create: `app/profile_targets.py`
- Create: `app/services/profile_resolution.py`
- Modify: `app/routers/configs.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_api_endpoints.py`
- Create: `tests/test_profile_targets.py`

**Interfaces:**
- Adds: `ConfigProfile.target: str`，默认 `mac_workstation`。
- Adds: `ConfigProfile.udp_security_mode: str`，默认由目标决定。
- Produces: `ProfileTarget(StrEnum)`、`UdpSecurityMode(StrEnum)`、`default_udp_mode(target)`、`validate_target_mode(target, mode)`。
- Produces: `resolve_profile(db, profile_id: int | None) -> ConfigProfile`；显式 id 不存在抛 `ProfileNotFound`，默认档案不存在抛 `DefaultProfileMissing`。
- Produces: Pydantic `ConfigProfileCreate/Update` 对目标与模式的 422 校验。

- [ ] **Step 1: 写 schema 迁移和兼容矩阵失败测试**

```python
@pytest.mark.parametrize(("target", "mode", "valid"), [
    ("mac_workstation", "strict", True),
    ("mac_workstation", "scoped", True),
    ("mac_workstation", "compatible", True),
    ("mac_gateway", "strict", True),
    ("mac_gateway", "scoped", True),
    ("mac_gateway", "compatible", True),
    ("ios_mobile", "strict", True),
    ("ios_mobile", "scoped", False),
    ("ios_mobile", "compatible", True),
])
def test_target_udp_mode_matrix(target, mode, valid):
    if valid:
        validate_target_mode(ProfileTarget(target), UdpSecurityMode(mode))
    else:
        with pytest.raises(ValueError, match="ios_mobile.*scoped"):
            validate_target_mode(ProfileTarget(target), UdpSecurityMode(mode))


async def test_existing_profiles_migrate_to_workstation(upgraded_legacy_db):
    rows = sqlite_rows(upgraded_legacy_db, "SELECT target, udp_security_mode FROM config_profiles")
    assert rows and set(rows) == {("mac_workstation", "scoped")}
```

- [ ] **Step 2: 运行测试确认字段和类型尚不存在**

Run: `venv/bin/pytest tests/test_profile_targets.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现枚举和默认矩阵**

```python
class ProfileTarget(StrEnum):
    MAC_WORKSTATION = "mac_workstation"
    MAC_GATEWAY = "mac_gateway"
    IOS_MOBILE = "ios_mobile"


class UdpSecurityMode(StrEnum):
    STRICT = "strict"
    SCOPED = "scoped"
    COMPATIBLE = "compatible"


def default_udp_mode(target: ProfileTarget) -> UdpSecurityMode:
    return UdpSecurityMode.COMPATIBLE if target is ProfileTarget.IOS_MOBILE else UdpSecurityMode.SCOPED
```

`validate_target_mode` 只拒绝 `ios_mobile + scoped`；未知枚举由 Pydantic 在进入服务前返回 422。

- [ ] **Step 4: 增加 Alembic 列和约束**

迁移先添加非空列并使用 `mac_workstation/scoped` 填充现有行，再创建枚举值 `CHECK` 约束。SQLite 使用 Alembic batch table rebuild，downgrade 删除两个约束和字段。

- [ ] **Step 5: 更新档案 CRUD**

创建档案时若未传 `udp_security_mode`，调用 `default_udp_mode(target)`；更新 target 且未同时传 mode 时，按新 target 重置默认模式。若同时传入不兼容组合，返回 422 且不提交事务。档案 CRUD 和后续 General API 从本任务起统一调用 `resolve_profile`，禁止再次实现隐式回退。

- [ ] **Step 6: 运行定向测试并提交**

Run: `venv/bin/pytest tests/test_profile_targets.py tests/test_api_endpoints.py -v`
Expected: PASS。

```bash
git add migrations/versions/0003_profile_targets.py app/models.py app/profile_targets.py app/services/profile_resolution.py app/routers/configs.py tests/conftest.py tests/test_profile_targets.py tests/test_api_endpoints.py
git commit -m "feat: add target-aware configuration profiles"
```

---

### Task 2: 平台 General 能力矩阵

**Files:**
- Modify: `app/profile_targets.py`
- Modify: `app/general_fields.py`
- Modify: `app/routers/general.py`
- Modify: `app/services/generator.py`
- Create: `tests/test_general_capabilities.py`
- Modify: `tests/test_generator.py`

**Interfaces:**
- Consumes: `resolve_profile`，General 字段和值端点不得自行回退默认档案。
- Produces: `filter_general_for_target(target, values) -> dict[str, object]`。
- Produces: `general_fields_for_target(target) -> list[dict]`。
- Changes: `GET /api/general/fields?profile_id=<id>` 只返回当前目标可配置字段；未知档案 404。

- [ ] **Step 1: 写三平台字段集合测试**

```python
@pytest.mark.parametrize(("target", "present", "absent"), [
    (ProfileTarget.MAC_WORKSTATION,
     {"read-etc-hosts", "http-api"},
     {"compatibility-mode", "auto-suspend", "gateway-restricted-to-lan"}),
    (ProfileTarget.MAC_GATEWAY,
     {"read-etc-hosts", "http-api", "gateway-restricted-to-lan"},
     {"compatibility-mode", "auto-suspend"}),
    (ProfileTarget.IOS_MOBILE,
     {"compatibility-mode", "auto-suspend", "block-quic"},
     {"http-api", "http-listen", "read-etc-hosts", "gateway-restricted-to-lan"}),
])
def test_general_capability_matrix(target, present, absent):
    values = filter_general_for_target(target, ALL_SYNTHETIC_GENERAL_VALUES)
    assert present <= values.keys()
    assert not (absent & values.keys())
```

- [ ] **Step 2: 运行测试确认当前生成器输出未过滤字段**

Run: `venv/bin/pytest tests/test_general_capabilities.py -v`
Expected: FAIL。

- [ ] **Step 3: 定义唯一能力表**

在 `profile_targets.py` 定义：

```python
IOS_ONLY = frozenset({"compatibility-mode", "auto-suspend"})
MAC_ONLY = frozenset({
    "external-controller-access", "http-api", "http-api-tls",
    "http-api-web-dashboard", "http-listen", "socks5-listen",
    "allow-wifi-access", "wifi-access-http-port", "wifi-access-socks5-port",
    "allow-hotspot-access", "read-etc-hosts",
    "use-local-host-item-for-proxy", "proxy-restricted-to-lan",
})
GATEWAY_ONLY = frozenset({"gateway-restricted-to-lan"})
```

`filter_general_for_target` 先丢弃目标不支持的键，再强制：

```python
if target is ProfileTarget.MAC_GATEWAY:
    output["gateway-restricted-to-lan"] = True
elif target is ProfileTarget.IOS_MOBILE:
    output["compatibility-mode"] = 1
    output["auto-suspend"] = True
```

所有目标强制保留 `block-quic=per-policy` 和 `udp-policy-not-supported-behaviour=REJECT`；保存 API 拒绝这两个键的危险覆盖值。

- [ ] **Step 4: 让 General 字段元数据使用同一能力表**

给 `FieldDef` 增加 `targets: frozenset[ProfileTarget]`，并补齐当前元数据缺少的 `auto-suspend`、`gateway-restricted-to-lan`、`block-quic`、`http-api-tls`。`get_fields(profile_id)` 必须先解析档案，按 `targets` 过滤；`save_values` 丢弃旧目标遗留的不兼容键并拒绝当前目标不支持的新键，返回 422 和具体 key。

- [ ] **Step 5: 接入生成器并运行测试**

生成器合并 `DEFAULT_GENERAL` 和档案值后调用 `filter_general_for_target`，并跳过 `None` 和空字符串。运行：

Run: `venv/bin/pytest tests/test_general_capabilities.py tests/test_generator.py -v`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/profile_targets.py app/general_fields.py app/routers/general.py app/services/generator.py tests/test_general_capabilities.py tests/test_generator.py
git commit -m "feat: filter General options by Surge target"
```

---

### Task 3: 严格档案解析和数据作用域

**Files:**
- Modify: `app/services/profile_resolution.py`
- Modify: `app/services/generator.py`
- Modify: `app/routers/configs.py`
- Modify: `app/routers/general.py`
- Modify: `tests/test_generator.py`
- Modify: `tests/test_skip_proxy.py`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- Consumes: `resolve_profile`、`ProfileNotFound`、`DefaultProfileMissing`。
- Produces: `profile_scope(model, profile_id)` SQLAlchemy predicate，等价于 `model.profile_id IS NULL OR model.profile_id=profile_id`。

- [ ] **Step 1: 写显式 404 和跨档案隔离测试**

```python
async def test_explicit_missing_profile_never_falls_back(db):
    with pytest.raises(ProfileNotFound):
        await generate_surge_config(db, profile_id=99999)


async def test_rules_and_hosts_are_global_plus_current_only(db, two_profiles):
    add_rule(db, None, "DOMAIN,global.example,DIRECT")
    add_rule(db, two_profiles.first.id, "DOMAIN,work.example,DIRECT")
    add_rule(db, two_profiles.second.id, "DOMAIN,private.example,DIRECT")
    add_host(db, None, "global-host.example", "203.0.113.10")
    add_host(db, two_profiles.first.id, "work-host.example", "203.0.113.11")
    add_host(db, two_profiles.second.id, "private-host.example", "203.0.113.12")
    config = await generate_surge_config(db, two_profiles.first.id)
    assert "global.example" in config and "work.example" in config
    assert "private.example" not in config and "private-host.example" not in config
```

- [ ] **Step 2: 运行测试确认当前生成器会回退默认档案并加载全部作用域**

Run: `venv/bin/pytest tests/test_generator.py tests/test_skip_proxy.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现唯一档案解析入口**

```python
async def resolve_profile(db, profile_id):
    if profile_id is not None:
        profile = await db.scalar(select(ConfigProfile).where(ConfigProfile.id == profile_id))
        if profile is None:
            raise ProfileNotFound(profile_id)
        return profile
    profile = await db.scalar(select(ConfigProfile).where(ConfigProfile.is_default.is_(True)).limit(1))
    if profile is None:
        raise DefaultProfileMissing()
    return profile
```

所有 `/generate`、`/preview`、订阅和 General 端点共用该函数。路由层将 `ProfileNotFound` 映射 404，将默认档案缺失映射 422。

- [ ] **Step 4: 在 SQL 查询阶段实施作用域**

规则和 Host 查询使用 `or_(Model.profile_id.is_(None), Model.profile_id == profile.id)`，不得先加载所有行再在 Python 过滤。`_build_skip_proxy` 使用已经过滤后的 Host，因此全局和当前档案中 `skip_proxy=true` 的 Host 都进入列表。

- [ ] **Step 5: 运行隔离测试和 API 回归**

Run: `venv/bin/pytest tests/test_generator.py tests/test_skip_proxy.py tests/test_api_endpoints.py -v`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/profile_resolution.py app/services/generator.py app/routers/configs.py app/routers/general.py tests/test_generator.py tests/test_skip_proxy.py tests/test_api_endpoints.py
git commit -m "fix: enforce profile data isolation"
```

---

### Task 4: 最终策略和生成完整性验证

**Files:**
- Create: `app/services/config_validation.py`
- Modify: `app/services/generator.py`
- Modify: `app/routers/configs.py`
- Modify: `tests/test_generator.py`
- Create: `tests/test_config_validation.py`

**Interfaces:**
- Produces: `translate_policy_name(name, locale) -> str`。
- Produces: `validate_generated_references(context: GenerationContext) -> None`。
- Raises: `InvalidConfiguration(errors: list[str])`，路由返回 422，不返回部分配置。
- Changes: `_build_rules(..., final_action: str)` 使用当前档案的最终策略。

- [ ] **Step 1: 写最终策略和缺失服务组测试**

```python
async def test_final_action_controls_final_rule(db):
    profile = await default_profile(db)
    profile.final_action = "大陆直连"
    await db.commit()
    config = await generate_surge_config(db, profile.id)
    assert "FINAL,大陆直连,dns-failed" in config
    assert "FINAL,兜底,dns-failed" not in config


async def test_missing_ai_service_group_aborts_generation(db):
    await db.execute(update(ServiceGroup).where(ServiceGroup.name == "AI服务").values(enabled=False))
    await db.commit()
    with pytest.raises(InvalidConfiguration, match="AI服务"):
        await generate_surge_config(db)
```

- [ ] **Step 2: 运行测试确认 `FINAL` 当前硬编码且引用不校验**

Run: `venv/bin/pytest tests/test_config_validation.py tests/test_generator.py -v`
Expected: FAIL。

- [ ] **Step 3: 引入生成上下文和引用校验**

`GenerationContext` 保存目标、locale、已生成节点名、地区组名、服务组名、组合组名和规则 action。允许的终结动作固定为 Surge 内置动作加已生成策略组：

```python
BUILTIN_ACTIONS = frozenset({"DIRECT", "REJECT", "REJECT-NO-DROP", "REJECT-DROP", "DNS"})
```

校验每个规则源、CustomRule 的末尾 action 以及 `final_action`。被禁用或没有生成的服务组不能被引用；错误按行聚合后一次性抛出。

- [ ] **Step 4: 让 `final_action` 进入 `_build_rules`**

```python
def _build_rules(..., final_action: str) -> list[str]:
    ...
    lines.append(f"FINAL,{_quote(translate_policy_name(final_action, locale))},dns-failed")
```

不要使用 `final_action or 兜底` 静默回退；空值必须由校验器拒绝。

- [ ] **Step 5: 运行验证测试**

Run: `venv/bin/pytest tests/test_config_validation.py tests/test_generator.py -v`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/config_validation.py app/services/generator.py app/routers/configs.py tests/test_config_validation.py tests/test_generator.py
git commit -m "feat: validate generated policy references"
```

---

### Task 5: AI 与公共规则顺序不变量

**Files:**
- Modify: `app/services/config_validation.py`
- Modify: `app/services/generator.py`
- Modify: `app/default_config.py`
- Modify: `tests/test_config_validation.py`
- Modify: `tests/test_generator.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces: `validate_rule_order(rule_sources) -> None`。
- Invariants: AI 在 Apple/Google/Global 重叠规则前；DouYin 在 TikTok 前；国内规则在 Global 前；IP 规则源生成 `no-resolve`。

- [ ] **Step 1: 写四个行为级顺序测试**

```python
@pytest.mark.parametrize(("earlier", "later"), [
    ("AI", "Google"),
    ("DouYin", "TikTok"),
    ("Domestic", "Global"),
])
async def test_required_rule_order_in_generated_output(db, earlier, later):
    config = await generate_surge_config(db)
    assert index_of_rule_source(config, earlier) < index_of_rule_source(config, later)


async def test_ip_rule_sources_always_use_no_resolve(db):
    config = await generate_surge_config(db)
    ip_lines = [line for line in rule_lines(config) if "/ip/" in line or "ASN" in line]
    assert ip_lines and all(line.endswith(",no-resolve") for line in ip_lines)
```

- [ ] **Step 2: 运行测试并记录真实失败顺序**

Run: `venv/bin/pytest tests/test_config_validation.py tests/test_generator.py -v`
Expected: 至少 AI 与重叠组顺序测试在当前规则排序下失败；不得通过修改测试期望绕过。

- [ ] **Step 3: 保留显式排序并拒绝破坏不变量的更新**

生成器继续使用数据库可见的稳定顺序：

```python
ordered = sorted(rule_sources, key=lambda item: (item.sort_order, item.id))
validate_rule_order(ordered)
```

更新 `DEFAULT_RULE_SOURCES` 的内置 `sort_order`，使 AI 位于 Apple、Google 和 Global 前，同时保持 DouYin/TikTok、Mainland/Global 顺序。版本化数据迁移只在现有内置顺序违反不变量时重排受影响的内置记录，不覆盖未知自定义规则源。后台 reorder 端点在提交前构造完整候选顺序并调用 `validate_rule_order`；破坏任一不变量时返回 422 并回滚整个事务。

- [ ] **Step 4: 运行顺序与迁移回归**

Run: `venv/bin/pytest tests/test_config_validation.py tests/test_generator.py tests/test_migrations.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/services/config_validation.py app/services/generator.py app/default_config.py tests/test_config_validation.py tests/test_generator.py tests/test_migrations.py
git commit -m "fix: enforce routing order invariants"
```

---

### Task 6: 档案目标管理 UI 与阶段 B 验收

**Files:**
- Modify: `app/templates/profiles.html`
- Modify: `app/static/js/profiles.js`
- Modify: `app/templates/settings.html`
- Modify: `app/static/js/settings.js`
- Modify: `app/i18n.py`
- Modify: `README.md`
- Create: `docs/PROFILE_TARGETS.md`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- UI consumes: 档案列表中的 `target`、`udp_security_mode`、`token_configured`。
- UI writes: 创建/编辑档案时提交 target 和兼容 mode；iOS 不展示 `scoped`。
- Settings consumes: `installation_role`，只影响新档案默认 target，不改写已有档案。

- [ ] **Step 1: 写实例角色默认值和 UI API 契约测试**

```python
async def test_new_profile_defaults_follow_instance_role(admin_client):
    await set_instance_role(admin_client, "home_gateway")
    created = await admin_client.post("/api/configs/profiles", json={"name": "Gateway"})
    profile = await get_profile(admin_client, created.json()["id"])
    assert profile["target"] == "mac_gateway"
    assert profile["udp_security_mode"] == "scoped"
```

- [ ] **Step 2: 运行测试确认当前创建端点没有目标默认逻辑**

Run: `venv/bin/pytest tests/test_api_endpoints.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现实例角色到新档案默认值的单向映射**

`work_computer -> mac_workstation`，`home_gateway -> mac_gateway`。角色修改只影响此后不显式传 target 的创建请求；已有档案逐条保持。

- [ ] **Step 4: 更新档案和设置页面**

档案弹窗显示三种目标的明确说明和最低版本；iOS 选择器只给 `strict/compatible`。General Tab 请求 `/api/general/fields?profile_id=<id>`，切换 target 后重新加载字段。设置页显示“实例角色不是 Surge Gateway VM 开关”，并链接运行指南。

- [ ] **Step 5: 更新公开文档**

`docs/PROFILE_TARGETS.md` 说明两实例部署、三个目标、配置作用域、版本下限和 Profile 不会开启 Gateway VM。所有示例使用虚构数据。

- [ ] **Step 6: 自动化与真实产物 smoke**

Run: `venv/bin/pytest -q`
Expected: 0 failed。

为三个目标各创建一个临时档案并调用 preview；保存响应正文到 `/tmp`，运行：

```bash
'/Applications/Surge.app/Contents/Applications/surge-cli' --check /tmp/mac-workstation.conf
'/Applications/Surge.app/Contents/Applications/surge-cli' --check /tmp/mac-gateway.conf
```

Expected: 两个 Mac 产物校验 exit 0；iOS 产物人工导入 Surge iOS 5.14.6 Profile Syntax Check，无 Mac-only 键。临时文件验收后删除。

- [ ] **Step 7: 提交**

```bash
git add app/templates/profiles.html app/static/js/profiles.js app/templates/settings.html app/static/js/settings.js app/i18n.py README.md docs/PROFILE_TARGETS.md tests/test_api_endpoints.py
git commit -m "feat: expose profile targets in management UI"
```
