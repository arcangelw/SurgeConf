# UDP 兼容与网关设备实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用目标感知的 strict、scoped、compatible 三种模式替代全局 STUN 阻断，在工作 Mac 和家庭网关上提供精确 DIRECT 例外，并让 iOS 实时 UDP 视频恢复可用。

**Architecture:** UDP 例外作为档案子资源持久化，目标校验和规则构建分别放在领域模块与纯函数模块中。生成器在普通前置规则之前注入“例外 → 阻断”序列；旧数据库中由项目自动种下的全局 STUN 规则通过有版本记录的数据迁移删除，用户自建规则不做模糊删除。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、Surge Mac 6.6 CLI、Surge iOS 5.14.6、pytest。

**Spec:** `docs/superpowers/specs/2026-10-16-target-aware-surge-config-design.md`

## Global Constraints

- 本计划依赖运行数据与安全边界、目标感知档案两个计划全部完成。
- `strict` 生成四条项目管理的 STUN 阻断；`scoped` 先生成 DIRECT 例外再生成相同阻断；`compatible` 不生成项目管理的全局 STUN 阻断。
- `mac_workstation` 的例外类型只能是 `process`；`mac_gateway` 只能是 `mac_address`；`ios_mobile` 不允许 UDP 例外。
- `ios_mobile` 只允许 `strict|compatible`，`scoped` 返回 422。
- 所有目标始终保留 `udp-policy-not-supported-behaviour=REJECT` 和 `block-quic=per-policy`。
- 任何目标都不得生成全局 UDP/443 拒绝规则。
- Surge Gateway VM 和逐设备 UDP Fast Path 由 Surge Mac UI 管理；SurgeConf 6.6 集成不得声称自动控制它们。
- API 列表返回脱敏匹配值；完整 MAC/进程值仅在覆盖式写入和配置生成时使用。
- 真实摄像头、设备名称、MAC 地址和局域网地址只存在运行数据库及本地人工验收记录，不进入仓库、日志或提交。

## Test Fixture Contracts

- `gateway_profile` is a persisted `mac_gateway/scoped` profile；`workstation_profiles` contains two persisted `mac_workstation/scoped` profiles.
- `create_process_exception` calls the authenticated API with `ExampleVideo` and returns the created id.
- `add_exact_legacy_security_rules` inserts only the four historical rule/comment/sort signatures; `migration_record_exists` queries `DataMigrationRecord` by key.
- `synthetic_exceptions_for(target)` returns one valid process exception for work Mac, one valid locally administered MAC for gateway, and an empty list for iOS.
- `valid_target_mode_pairs` returns the eight valid combinations from the approved 3×3 matrix; `assert_udp_shape` checks exact rule presence and order.

---

### Task 1: UDP 例外 schema 和领域校验

**Files:**
- Create: `migrations/versions/0004_udp_exceptions.py`
- Modify: `app/models.py`
- Create: `app/udp_policy.py`
- Modify: `app/routers/configs.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_udp_policy.py`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- Adds model: `UdpException(id, profile_id, match_type, match_value, display_name, enabled, sort_order)`。
- Produces: `UdpMatchType(StrEnum)`、`normalize_udp_match(target, match_type, value) -> str`。
- Invariant: `profile_id` 外键 `ON DELETE CASCADE`；`(profile_id, match_type, match_value)` 唯一。
- Changes: 修改档案 target 时，若现有例外与新 target 不兼容则返回 422，不自动删除。

- [ ] **Step 1: 写目标/类型和值校验测试**

```python
@pytest.mark.parametrize(("target", "kind", "value", "normalized"), [
    ("mac_workstation", "process", "ExampleVideo", "ExampleVideo"),
    ("mac_gateway", "mac_address", "02-00-00-00-00-01", "02:00:00:00:00:01"),
])
def test_normalize_valid_udp_match(target, kind, value, normalized):
    assert normalize_udp_match(ProfileTarget(target), UdpMatchType(kind), value) == normalized


@pytest.mark.parametrize(("target", "kind", "value"), [
    ("ios_mobile", "process", "ExampleVideo"),
    ("mac_workstation", "mac_address", "02:00:00:00:00:01"),
    ("mac_gateway", "process", "ExampleVideo"),
    ("mac_workstation", "process", "bad,name"),
    ("mac_gateway", "mac_address", "not-a-mac"),
])
def test_reject_incompatible_udp_match(target, kind, value):
    with pytest.raises(ValueError):
        normalize_udp_match(ProfileTarget(target), UdpMatchType(kind), value)
```

- [ ] **Step 2: 运行测试确认接口不存在**

Run: `venv/bin/pytest tests/test_udp_policy.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现结构化模型和规范化函数**

进程值要求 1–255 字符，拒绝逗号、双引号、换行和控制字符；MAC 接受 `:` 或 `-` 分隔的六组十六进制，持久化为大写冒号形式。错误信息只说明格式，不回显完整敏感值。

Alembic 迁移创建外键、唯一约束和 `(profile_id, enabled, sort_order)` 索引；升级前启用 `PRAGMA foreign_keys=ON`。

- [ ] **Step 4: 将例外纳入档案 target 更新校验和级联删除**

`update_profile` 在改变 target 前查询例外类型；存在不兼容数据时返回 422，响应只给出例外数量。`delete_profile` 依赖数据库级级联并增加断言测试，不再为新表手工逐条删除。

- [ ] **Step 5: 运行测试并提交**

Run: `venv/bin/pytest tests/test_udp_policy.py tests/test_api_endpoints.py -v`
Expected: PASS。

```bash
git add migrations/versions/0004_udp_exceptions.py app/models.py app/udp_policy.py app/routers/configs.py tests/conftest.py tests/test_udp_policy.py tests/test_api_endpoints.py
git commit -m "feat: model target-aware UDP exceptions"
```

---

### Task 2: UDP 例外管理 API 和脱敏响应

**Files:**
- Create: `app/routers/udp.py`
- Modify: `app/main.py`
- Create: `tests/test_udp_api.py`

**Interfaces:**
- Produces: `GET /api/configs/profiles/{profile_id}/udp-exceptions`。
- Produces: `POST /api/configs/profiles/{profile_id}/udp-exceptions`。
- Produces: `PUT /api/configs/profiles/{profile_id}/udp-exceptions/{exception_id}`。
- Produces: `DELETE /api/configs/profiles/{profile_id}/udp-exceptions/{exception_id}`。
- List response: `match_value_masked`，不含 `match_value`；更新省略 `match_value` 时保留原值。

- [ ] **Step 1: 写 CRUD、跨档案和脱敏测试**

```python
async def test_udp_exception_list_masks_match_value(admin_client, gateway_profile):
    base = f"/api/configs/profiles/{gateway_profile.id}/udp-exceptions"
    created = await admin_client.post(base, json={
        "match_type": "mac_address",
        "match_value": "02:00:00:00:00:01",
        "display_name": "Example display",
        "enabled": True,
        "sort_order": 10,
    })
    assert created.status_code == 201
    item = (await admin_client.get(base)).json()[0]
    assert item["match_value_masked"] == "02:00:**:**:**:01"
    assert "match_value" not in item


async def test_udp_exception_cannot_cross_profile(
    admin_client, workstation_profiles,
):
    first, second = workstation_profiles
    item_id = await create_process_exception(admin_client, profile_id=first.id)
    assert (await admin_client.put(
        f"/api/configs/profiles/{second.id}/udp-exceptions/{item_id}",
        json={"enabled": False},
    )).status_code == 404
```

- [ ] **Step 2: 运行测试确认路由不存在**

Run: `venv/bin/pytest tests/test_udp_api.py -v`
Expected: FAIL，返回 404。

- [ ] **Step 3: 实现按父档案约束的 CRUD**

每个查询同时使用 `UdpException.id` 和 `UdpException.profile_id`；创建前加载档案 target 并调用 `normalize_udp_match`。列表脱敏函数：MAC 只保留首两组和末组，进程名只保留首尾各两个字符；短值统一返回 `***`。

- [ ] **Step 4: 验证管理边界和 CSRF**

所有四个端点沿用阶段 A 管理中间件；POST/PUT/DELETE 没有 CSRF 时返回 403。错误响应不得包含数据库中的完整匹配值。

- [ ] **Step 5: 运行 API 测试并提交**

Run: `venv/bin/pytest tests/test_udp_api.py tests/test_auth.py -v`
Expected: PASS。

```bash
git add app/routers/udp.py app/main.py tests/test_udp_api.py
git commit -m "feat: add secured UDP exception API"
```

---

### Task 3: 版本化清理旧全局 STUN 规则

**Files:**
- Modify: `app/default_config.py`
- Modify: `app/bootstrap.py`
- Modify: `app/services/generator.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_generator.py`

**Interfaces:**
- Consumes: 阶段 A 的 `DataMigrationRecord`/数据迁移版本记录接口。
- Produces migration key: `udp_security_modes_v1`。
- Removes from defaults: 项目自动种下的四条全局 STUN 规则。
- Preserves: 不匹配完整内置签名的用户自建 STUN/UDP 规则。

- [ ] **Step 1: 写精确删除和用户规则保留测试**

```python
async def test_udp_mode_migration_removes_only_managed_stun_rules(db):
    managed = add_exact_legacy_security_rules(db)
    user = CustomRule(profile_id=None, position="pre", sort_order=12,
                      rule_text="PROTOCOL,STUN,REJECT", comment="user-owned")
    db.add(user)
    await db.commit()
    await apply_data_migrations(db)
    remaining = await all_custom_rules(db)
    assert user.id in {row.id for row in remaining}
    assert not ({row.id for row in managed} & {row.id for row in remaining})
    assert await migration_record_exists(db, "udp_security_modes_v1")
```

- [ ] **Step 2: 运行测试确认旧逻辑仍强制补回 STUN**

Run: `venv/bin/pytest tests/test_migrations.py -v`
Expected: FAIL。

- [ ] **Step 3: 将内置 UDP 阻断移出默认 CustomRule**

在 `default_config.py` 定义不可持久化的生成常量：

```python
MANAGED_STUN_RULES = (
    "DOMAIN-KEYWORD,stun,REJECT",
    "AND,((PROTOCOL,UDP),(DEST-PORT,3478)),REJECT",
    "AND,((PROTOCOL,UDP),(DEST-PORT,19302)),REJECT",
    "PROTOCOL,STUN,REJECT",
)
```

`DEFAULT_PRE_RULES` 和 `SECURITY_PRE_RULES` 不再含这些规则。迁移只删除同时匹配 `profile_id IS NULL`、旧 sort_order、旧 comment 和规范化 rule_text 的记录。

- [ ] **Step 4: 确保迁移只执行一次且日志不泄露规则内容**

迁移成功后写入 `DataMigrationRecord(key="udp_security_modes_v1")`。日志只输出删除数量；重复启动不再查询或修改。事务失败不得写记录。

- [ ] **Step 5: 运行迁移与生成器回归并提交**

Run: `venv/bin/pytest tests/test_migrations.py tests/test_generator.py -v`
Expected: PASS；此时生成器暂未注入新规则的测试将在 Task 4 添加。

```bash
git add app/default_config.py app/bootstrap.py app/services/generator.py tests/test_migrations.py tests/test_generator.py
git commit -m "refactor: move managed STUN rules out of stored defaults"
```

---

### Task 4: 三模式 UDP 规则生成

**Files:**
- Create: `app/services/udp_rules.py`
- Modify: `app/services/generator.py`
- Modify: `app/services/config_validation.py`
- Modify: `tests/test_udp_policy.py`
- Modify: `tests/test_generator.py`
- Modify: `tests/test_config_validation.py`

**Interfaces:**
- Produces: `build_udp_rules(target, mode, exceptions) -> list[str]`。
- Changes: 生成顺序为档案 pre rules 中的明确企业/用户覆盖规则 → 项目管理的 UDP DIRECT 例外 → 项目管理的 STUN 阻断 → RuleSource；所有顺序均在 `FINAL` 前。
- Raises: 不兼容 target/mode/exception 时 `InvalidConfiguration`。

- [ ] **Step 1: 写 3×3 兼容矩阵和规则顺序测试**

```python
@pytest.mark.parametrize(("target", "mode", "expected"), [
    ("mac_workstation", "strict", "strict"),
    ("mac_workstation", "scoped", "process_then_strict"),
    ("mac_workstation", "compatible", "none"),
    ("mac_gateway", "strict", "strict"),
    ("mac_gateway", "scoped", "mac_then_strict"),
    ("mac_gateway", "compatible", "none"),
    ("ios_mobile", "strict", "strict"),
    ("ios_mobile", "scoped", "invalid"),
    ("ios_mobile", "compatible", "none"),
])
def test_udp_generation_matrix(target, mode, expected):
    if expected == "invalid":
        with pytest.raises(InvalidConfiguration):
            build_udp_rules(target, mode, [])
    else:
        lines = build_udp_rules(target, mode, synthetic_exceptions_for(target))
        assert_udp_shape(lines, expected)


def test_no_mode_emits_global_udp_443_reject():
    for target, mode in valid_target_mode_pairs():
        assert "DEST-PORT,443" not in "\n".join(build_udp_rules(target, mode, synthetic_exceptions_for(target)))
```

- [ ] **Step 2: 运行测试确认构建器不存在**

Run: `venv/bin/pytest tests/test_udp_policy.py tests/test_generator.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现纯函数规则构建器**

```python
def build_udp_rules(target, mode, exceptions):
    validate_target_mode(target, mode)
    if mode is UdpSecurityMode.COMPATIBLE:
        return []
    lines = []
    if mode is UdpSecurityMode.SCOPED:
        lines.extend(_direct_exception_line(target, item)
                     for item in sorted(enabled(exceptions), key=lambda x: (x.sort_order, x.id)))
    lines.extend(MANAGED_STUN_RULES)
    return lines
```

`process` 输出 `AND,((PROCESS-NAME,ExampleVideo),(PROTOCOL,UDP)),DIRECT`；包含空格的合法进程名由 `udp_rules.py::quote_rule_atom` 加双引号。`mac_address` 输出 `AND,((MAC-ADDRESS,02:00:00:00:00:01),(PROTOCOL,UDP)),DIRECT`。值已经在写入时规范化，生成时仍拒绝逗号、双引号和换行，防止配置注入。

- [ ] **Step 4: 接入生成器查询和顺序**

只查询当前 profile 的 enabled 例外。`strict` 忽略已有例外但不删除；切回 `scoped` 后恢复。`compatible` 不注入 STUN，但用户自建 CustomRule 仍按其明确配置保留。

- [ ] **Step 5: 运行矩阵、完整生成和 Surge 语法测试**

Run: `venv/bin/pytest tests/test_udp_policy.py tests/test_generator.py tests/test_config_validation.py -v`
Expected: PASS。

将两个 Mac scoped 产物写入 `/tmp` 并执行：

```bash
'/Applications/Surge.app/Contents/Applications/surge-cli' --check /tmp/mac-workstation-scoped.conf
'/Applications/Surge.app/Contents/Applications/surge-cli' --check /tmp/mac-gateway-scoped.conf
```

Expected: exit 0；临时产物验收后删除。

- [ ] **Step 6: 提交**

```bash
git add app/services/udp_rules.py app/services/generator.py app/services/config_validation.py tests/test_udp_policy.py tests/test_generator.py tests/test_config_validation.py
git commit -m "feat: generate target-aware UDP security rules"
```

---

### Task 5: UDP 管理界面和 Fast Path 指引

**Files:**
- Modify: `app/templates/profiles.html`
- Modify: `app/static/js/profiles.js`
- Modify: `app/i18n.py`
- Modify: `app/static/css/style.css`
- Create: `docs/UDP_COMPATIBILITY.md`

**Interfaces:**
- UI consumes: 档案 target/mode 和脱敏例外列表。
- UI writes: mode 更新、例外新增、覆盖式编辑、启停和删除。
- Documentation: 明确 Gateway VM/Fast Path 均由 Surge UI 管理。

- [ ] **Step 1: 为每种目标定义可见控制**

工作 Mac 显示进程名输入；家庭网关显示 MAC 输入并提示值只保存在该实例；iOS 隐藏例外表且只显示 strict/compatible。scoped 无启用例外时显示“行为等同 strict”的警告，但允许保存，供稍后添加例外。

- [ ] **Step 2: 实现不回填秘密值的覆盖式编辑**

列表只渲染 `match_value_masked`。编辑时完整值输入框为空，旁边显示“留空保持原值”；请求体仅在用户输入新值时包含 `match_value`。任何 DOM 属性和日志都不得缓存完整 MAC。

- [ ] **Step 3: 增加 Fast Path 和运行前提说明**

`docs/UDP_COMPATIBILITY.md` 必须包含：

- Gateway VM 在 Surge Mac UI 启用，不是 Profile 开关；
- Fast Path 绕过规则和代理；需要精确分流的设备必须在 Dashboard 关闭 Fast Path；
- Surge Mac 6.6 CLI 不用于自动控制逐设备 Fast Path；
- iOS compatible 恢复兼容性，但接受浏览器 WebRTC 直连候选风险；
- 所有示例使用 `ExampleVideo` 和 `02:00:00:00:00:01`。

- [ ] **Step 4: 浏览器 smoke**

启动临时实例，使用真实浏览器依次创建三种档案；验证目标切换后的模式选项、例外表、脱敏显示、错误提示和移动端布局。检查浏览器控制台无 error，网络响应无完整秘密值。

- [ ] **Step 5: 提交**

```bash
git add app/templates/profiles.html app/static/js/profiles.js app/i18n.py app/static/css/style.css docs/UDP_COMPATIBILITY.md
git commit -m "feat: add UDP compatibility controls"
```

---

### Task 6: 三平台运行验收与迁移文档更新

**Files:**
- Modify: `docs/MIGRATION_VERIFY.md`
- Modify: `README.md`
- Modify: `tests/test_open_source_safety.py`

**Interfaces:**
- Verification only；不新增运行时代码。
- Produces: 可在工作 Mac、家庭网关和 iOS 分别执行的无敏感信息验收清单。

- [ ] **Step 1: 更新迁移验证文档**

删除“全局 STUN 必须落库”的旧期望，改为按档案生成行为：strict 有四条、scoped 有例外后四条、compatible 无项目管理 STUN。数据库命令不得打印 `match_value`，只允许 `COUNT(*) GROUP BY match_type`。

- [ ] **Step 2: 运行完整自动化测试和敏感扫描**

Run: `venv/bin/pytest -q`
Expected: 0 failed。

Run: `venv/bin/pytest tests/test_open_source_safety.py -v`
Expected: PASS，且输出不回显命中内容。

- [ ] **Step 3: 验证工作 Mac**

生成并加载 `mac_workstation/scoped` 档案。运行：

```bash
'/Applications/Surge.app/Contents/Applications/surge-cli' --raw dump rule
```

Expected: 有进程 DIRECT 例外，随后有 STUN 阻断，无全局 UDP/443 拒绝。启动配置的实时视频应用并观察视频成功；浏览器 WebRTC 检测不出现公开直连 STUN 候选。验收记录只写“通过/失败”和规则名称，不写连接目标。

- [ ] **Step 4: 验证家庭网关**

在 Gateway Dashboard 对测试设备关闭 Fast Path，加载 `mac_gateway/scoped`，确认请求查看器命中 MAC DIRECT 例外并可播放实时视频；未配置设备仍命中 STUN 阻断。完成后按设备实际需求保留或恢复 Fast Path。

- [ ] **Step 5: 验证 iOS**

在 Surge iOS 5.14.6 导入 `ios_mobile/compatible`，通过 Profile Syntax Check，确认配置无 PROCESS/MAC 规则并完成一次真实实时 UDP 视频播放。

- [ ] **Step 6: 提交文档**

```bash
git add docs/MIGRATION_VERIFY.md README.md tests/test_open_source_safety.py
git commit -m "docs: verify UDP modes across Surge targets"
```
