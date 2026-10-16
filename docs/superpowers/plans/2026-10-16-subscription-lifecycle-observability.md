# 订阅生命周期与可观测性实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让订阅自动更新真正执行，以稳定身份 upsert 节点并保留人工状态，在失败时保留最后可用数据，同时向管理页面提供不泄露秘密的调度与同步状态。

**Architecture:** 节点稳定键由规范化连接配置计算，单次同步先在内存完成拉取、解析和去重，再在一个事务内 upsert 与删除缺失节点。lifespan 启动一个持有 `fcntl.flock` 的单实例 asyncio 调度器；调度器只查询到期任务，逐个调用与手动同步相同的服务函数。

**Tech Stack:** Python 3.13、asyncio、fcntl（macOS）、FastAPI、SQLAlchemy 2、Alembic、httpx、Jinja2、原生 JavaScript、pytest。

**Spec:** `docs/superpowers/specs/2026-10-16-target-aware-surge-config-design.md`

## Global Constraints

- 本计划依赖阶段 A–C 全部完成。
- 同步成功前不得删除或改写上一批可用节点。
- 同一订阅中稳定键相同的节点必须保留数据库 id、`region_manual`、`enabled` 和 `pinned_services`。
- 拉取失败、HTTP 失败、解析异常或结果为空都保留上一批节点，并记录脱敏失败摘要。
- 调度器只处理 `enabled=true AND auto_update=true` 且已到期的订阅；手动同步不受 `auto_update` 限制。
- 多 worker/重复进程中只能有一个调度循环；未获取锁的进程正常提供 Web 服务。
- 日志不得包含订阅 URL、节点地址、节点配置、Token、证书或真实设备名称。
- 时间统一以 UTC 存储，API 使用带 `Z` 的 ISO 8601；UI 按浏览器本地时区显示。
- Surge Mac 6.6 Logbook、Dashboard 和 Profile reload 只做运行验收指引，不通过 6.6 CLI 自动化不存在的能力。

## Test Fixture Contracts

- `parsed_node`/`same_connection` create synthetic parsed nodes using `example.com` and literal `synthetic-*` credentials.
- `runtime_db` is a temporary `DatabaseRuntime`; `subscription` and `add_subscription` create rows with no real provider data.
- `subscription_node_snapshot` returns ids plus provider/manual fields for one subscription, sorted by id.
- `failed_result(code)` builds a `SyncResult` with a fixed safe message；`emit_sync_log` must discard its URL/config arguments before logging.
- `UTC_NOW` is a fixed timezone-aware instant shared by sync and scheduler tests.

---

### Task 1: 订阅状态和节点稳定键 schema

**Files:**
- Create: `migrations/versions/0005_subscription_lifecycle.py`
- Modify: `app/models.py`
- Create: `app/services/node_identity.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_node_identity.py`
- Modify: `tests/test_subscriptions.py`

**Interfaces:**
- Replaces: `Subscription.last_update` with `last_attempt_at`, `last_success_at`, `next_update_at`, `last_error_code`, `last_node_count`。
- Adds: `ProxyNode.stable_key: str | None`；订阅节点非空，手动节点允许 NULL。
- Produces: `stable_node_key(node_data: Mapping[str, object]) -> str`。
- Adds unique index: `(subscription_id, stable_key)`，仅对非 NULL stable_key 生效。

- [ ] **Step 1: 写稳定键行为测试**

```python
def test_stable_key_ignores_display_name():
    before = parsed_node(name="US A", config="US A = trojan, edge.example.com, 443, password=synthetic")
    after = parsed_node(name="US Renamed", config="US Renamed = trojan, edge.example.com, 443, password=synthetic")
    assert stable_node_key(before) == stable_node_key(after)


def test_stable_key_distinguishes_credentials():
    first = parsed_node(name="A", config="A = trojan, edge.example.com, 443, password=synthetic-a")
    second = parsed_node(name="B", config="B = trojan, edge.example.com, 443, password=synthetic-b")
    assert stable_node_key(first) != stable_node_key(second)
```

- [ ] **Step 2: 运行测试确认字段和函数不存在**

Run: `venv/bin/pytest tests/test_node_identity.py tests/test_subscriptions.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现不落明文的稳定键**

从 `config` 第一个 `=` 后提取连接参数，规范化协议名、主机大小写和端口，把 `key=value` 参数解析后按 key/value 排序，再对规范化字节做 SHA-256；认证参数参与摘要以区分同端点节点，但中间明文不持久化。返回十六进制摘要，拒绝没有连接参数的订阅节点。

```python
def stable_node_key(node_data):
    canonical = canonical_connection_bytes(node_data)
    return hashlib.sha256(canonical).hexdigest()
```

- [ ] **Step 4: 编写 Alembic 数据迁移**

迁移新增订阅状态字段，将旧 `last_update` 复制到 `last_success_at`，把范围外的 `update_interval` 归一为 86400，再删除旧列。为现有订阅节点计算 stable_key；同一订阅冲突时按“存在手动地区、钉选非空、被人工禁用、id 较小”的优先级选 survivor，合并首个非空 `region_manual`、钉选并集和最严格的 `enabled=false`，再删除其余记录并创建唯一索引。迁移日志只输出冲突数量。

Alembic revision 内嵌该版本的规范化算法和两个 golden vector，不从 `app.services.node_identity` 导入运行时代码，保证未来算法演进不会改变历史迁移结果。

- [ ] **Step 5: 运行迁移与模型测试并提交**

Run: `venv/bin/pytest tests/test_node_identity.py tests/test_database_migration.py tests/test_subscriptions.py -v`
Expected: PASS。

```bash
git add migrations/versions/0005_subscription_lifecycle.py app/models.py app/services/node_identity.py tests/conftest.py tests/test_node_identity.py tests/test_subscriptions.py
git commit -m "feat: add stable subscription node identity"
```

---

### Task 2: 事务化节点 upsert 和失败保留

**Files:**
- Modify: `app/services/subscription.py`
- Modify: `app/routers/subscriptions.py`
- Modify: `tests/test_subscriptions.py`
- Create: `tests/test_subscription_sync.py`

**Interfaces:**
- Produces: `SyncResult(subscription_id, success, node_count, inserted, updated, removed, error_code, public_message)`。
- Produces: `sync_subscription(db, sub_id, now=None) -> SyncResult`。
- Produces: `safe_sync_error(exc) -> tuple[error_code, public_message]`，只返回固定错误码和本地化所需的安全消息，不含 URL 或目标地址。
- Changes: 手动同步 API 成功返回 200；同步失败返回 502 和脱敏摘要，数据库保留最后可用节点。

- [ ] **Step 1: 写 upsert 保留和失败原子性测试**

```python
async def test_sync_upsert_preserves_manual_state(db, subscription, monkeypatch):
    original = await add_subscription_node(
        db, subscription, name="Old Name", region_manual="US",
        enabled=False, pinned_services=["AI服务"],
    )
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(return_value=[same_connection(name="New Name")]))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    refreshed = await db.get(ProxyNode, original.id)
    assert result.success is True
    assert refreshed.name == "New Name"
    assert refreshed.region_manual == "US"
    assert refreshed.enabled is False
    assert refreshed.pinned_services == ["AI服务"]


async def test_failed_or_empty_sync_keeps_last_good_nodes(db, subscription, monkeypatch):
    before = await subscription_node_snapshot(db, subscription.id)
    monkeypatch.setattr(subscription_service, "fetch_subscription",
                        AsyncMock(side_effect=httpx.TimeoutException("synthetic timeout")))
    result = await sync_subscription(db, subscription.id, now=UTC_NOW)
    assert result.success is False
    assert await subscription_node_snapshot(db, subscription.id) == before
    assert "http" not in result.public_message.lower()
```

- [ ] **Step 2: 运行测试确认当前删除后重建会丢人工状态**

Run: `venv/bin/pytest tests/test_subscription_sync.py -v`
Expected: FAIL。

- [ ] **Step 3: 将同步拆成拉取阶段和单事务写入阶段**

拉取、解析、稳定键计算和去重全部在事务外完成。结果为空视为 `empty_subscription` 错误。成功后在一个事务中：

1. 查询当前订阅节点并按 stable_key 建表；
2. 匹配项只更新 provider 字段：name、node_type、server、port、config、region_auto；
3. 新节点写 `enabled=true`、空人工地区和空钉选；
4. 成功结果中不存在的旧订阅节点才删除；
5. 更新 success/attempt/count/next_update 并清空 `last_error_code`；
6. 单次 commit。

禁止覆盖 `region_manual`、`enabled`、`pinned_services`。

- [ ] **Step 4: 记录脱敏失败状态**

失败使用独立短事务更新 `last_attempt_at`、`last_error_code` 和 `next_update_at`，不改 `last_success_at`/`last_node_count`。`safe_sync_error` 只允许固定 error_code：`timeout`、`http_status`、`invalid_content`、`network_error`、`internal_error`；公开消息不拼接异常原文。

- [ ] **Step 5: 运行同步测试并提交**

Run: `venv/bin/pytest tests/test_subscription_sync.py tests/test_subscriptions.py -v`
Expected: PASS。

```bash
git add app/services/subscription.py app/routers/subscriptions.py tests/test_subscription_sync.py tests/test_subscriptions.py
git commit -m "feat: upsert subscription nodes transactionally"
```

---

### Task 3: 单实例自动更新调度器

**Files:**
- Create: `app/services/subscription_scheduler.py`
- Modify: `app/bootstrap.py`
- Modify: `app/routers/subscriptions.py`
- Modify: `app/models.py`
- Create: `tests/test_subscription_scheduler.py`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- Produces: `SubscriptionScheduler(database, lock_path, clock=utc_now)`。
- Async methods: `start() -> bool`、`stop() -> None`、`run_due_once(now=None) -> list[SyncResult]`。
- Lock file: `RuntimePaths.root / "scheduler.lock"`，权限 `0600`。
- Validation: `update_interval` 必须在 300–604800 秒之间。

- [ ] **Step 1: 写到期选择、互斥锁和关闭测试**

```python
async def test_scheduler_runs_only_due_auto_enabled(runtime_db, tmp_path):
    due = await add_subscription(runtime_db, enabled=True, auto_update=True,
                                 next_update_at=UTC_NOW - timedelta(seconds=1))
    await add_subscription(runtime_db, enabled=True, auto_update=False,
                           next_update_at=UTC_NOW - timedelta(seconds=1))
    scheduler = SubscriptionScheduler(runtime_db, tmp_path / "scheduler.lock", clock=lambda: UTC_NOW)
    results = await scheduler.run_due_once()
    assert [item.subscription_id for item in results] == [due.id]


async def test_only_one_scheduler_holds_process_lock(runtime_db, tmp_path):
    first = SubscriptionScheduler(runtime_db, tmp_path / "scheduler.lock")
    second = SubscriptionScheduler(runtime_db, tmp_path / "scheduler.lock")
    assert await first.start() is True
    assert await second.start() is False
    await first.stop()
    assert await second.start() is True
    await second.stop()
```

- [ ] **Step 2: 运行测试确认调度器不存在**

Run: `venv/bin/pytest tests/test_subscription_scheduler.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现 macOS 文件锁和可取消循环**

`start` 以 `os.open(..., 0o600)` 打开文件并执行 `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；成功后保存 fd 并创建 asyncio task，失败关闭 fd 并返回 false。循环每 30 秒调用 `run_due_once`，用 `asyncio.Event.wait()` 加 timeout，`stop` 设置事件、await task、释放锁并关闭 fd。

- [ ] **Step 4: 定义调度时间行为**

新订阅创建时 `next_update_at=now`（若 auto_update）或 NULL。成功后为 `now + update_interval`；失败后使用 `min(update_interval, 900)` 重试，避免紧密循环。修改 interval/auto_update 时重算 next_update；禁用订阅时设为 NULL。

- [ ] **Step 5: 接入 lifespan**

数据库迁移和 seed 完成后启动 scheduler；lifespan `finally` 中先 `await scheduler.stop()`，再 dispose 数据库。未拿到锁只记录“scheduler standby”，不视为启动错误。

- [ ] **Step 6: 运行调度器和生命周期测试并提交**

Run: `venv/bin/pytest tests/test_subscription_scheduler.py tests/test_api_endpoints.py -v`
Expected: PASS，无 pending asyncio task 警告。

```bash
git add app/services/subscription_scheduler.py app/bootstrap.py app/routers/subscriptions.py app/models.py tests/test_subscription_scheduler.py tests/test_api_endpoints.py
git commit -m "feat: schedule subscription updates once per instance"
```

---

### Task 4: 同步状态 API 和管理页面

**Files:**
- Modify: `app/routers/subscriptions.py`
- Modify: `app/templates/proxies.html`
- Modify: `app/static/js/proxies.js`
- Modify: `app/templates/index.html`
- Modify: `app/static/js/index.js`
- Modify: `app/i18n.py`
- Modify: `app/static/css/style.css`
- Create: `tests/test_subscription_status_api.py`

**Interfaces:**
- Subscription list returns: id、name、tag、color、enabled、auto_update、update_interval、last_attempt_at、last_success_at、next_update_at、last_error_code、last_node_count。
- Subscription URL remains absent from list；已认证单项详情端点供编辑。
- Dashboard adds aggregate: due、healthy、failed、never_synced。

- [ ] **Step 1: 写状态响应和 URL 脱敏测试**

```python
async def test_subscription_status_is_safe_and_complete(admin_client):
    item = (await admin_client.get("/api/subscriptions")).json()[0]
    assert "url" not in item
    assert set(item) >= {
        "last_attempt_at", "last_success_at", "next_update_at",
        "last_error_code", "last_node_count",
    }
    assert item["last_success_at"].endswith("Z")
```

- [ ] **Step 2: 运行测试确认当前响应仍使用 `last_update`**

Run: `venv/bin/pytest tests/test_subscription_status_api.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现安全状态 DTO**

建立单一 `_subscription_summary` 序列化函数，将 UTC 时间转为 `YYYY-MM-DDTHH:MM:SSZ`。数据库和 API 的 `last_error_code` 都只使用固定码，不保存或返回异常原文；UI 根据固定码显示本地化文案。

- [ ] **Step 4: 更新订阅卡片和首页**

订阅卡片显示“上次成功、下次计划、节点数、当前状态”，失败时保留上次成功时间。首页显示四个聚合数量，并提供手动同步；所有时间用 `Intl.DateTimeFormat` 本地化。状态色同时有文本和图标，不仅依赖颜色。

- [ ] **Step 5: 浏览器 smoke**

用临时数据库创建成功、失败、从未同步和已禁用四类订阅；打开 `/` 和 `/proxies`，验证状态、时间和响应式布局，控制台无 error，Network 响应不含 URL 或节点配置。

- [ ] **Step 6: 运行 API 测试并提交**

Run: `venv/bin/pytest tests/test_subscription_status_api.py tests/test_api_endpoints.py -v`
Expected: PASS。

```bash
git add app/routers/subscriptions.py app/templates/proxies.html app/static/js/proxies.js app/templates/index.html app/static/js/index.js app/i18n.py app/static/css/style.css tests/test_subscription_status_api.py
git commit -m "feat: expose safe subscription sync status"
```

---

### Task 5: 运行日志、Logbook 指引和最终验收

**Files:**
- Create: `app/logging_config.py`
- Modify: `app/bootstrap.py`
- Modify: `app/services/subscription.py`
- Modify: `app/services/subscription_scheduler.py`
- Modify: `README.md`
- Modify: `docs/MIGRATION_VERIFY.md`
- Create: `docs/RUNTIME_OPERATIONS.md`
- Modify: `tests/test_open_source_safety.py`
- Create: `tests/test_logging_safety.py`

**Interfaces:**
- Produces: 结构化、可轮转的本地日志，路径来自 `RuntimePaths.log_file`。
- Log fields limited to: event、subscription_id、success、counts、error_code、schema_version、profile_id、target。
- Documentation covers: Profile reload、Logbook、Dashboard 请求查看器、Gateway DHCP/网络事件。

- [ ] **Step 1: 写日志秘密过滤测试**

```python
def test_sync_log_does_not_contain_subscription_or_node_secrets(caplog):
    emit_sync_log(
        subscription_id=7,
        url="https://synthetic-user:synthetic-secret@example.com/sub?token=synthetic",
        node_config="Example = trojan, edge.example.com, 443, password=synthetic",
        result=failed_result("timeout"),
    )
    text = caplog.text
    assert "subscription_id=7" in text and "error_code=timeout" in text
    assert "secret" not in text and "password" not in text and "token=" not in text
```

- [ ] **Step 2: 运行测试确认统一日志过滤尚不存在**

Run: `venv/bin/pytest tests/test_logging_safety.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现日志配置和允许字段适配器**

使用 `logging.handlers.RotatingFileHandler(maxBytes=5_000_000, backupCount=3)`，文件和轮转文件权限 `0600`。业务代码只调用 `log_event(event, **allowed_fields)`；传入非允许键立即抛 `ValueError`，避免开发者误记 URL/配置。

- [ ] **Step 4: 编写运行操作指南**

`docs/RUNTIME_OPERATIONS.md` 包含：

1. 工作实例与网关实例启动/停止；
2. 数据、备份、导出和日志目录；
3. Profile 生成后通过 Surge UI reload；
4. Surge Mac 6.6 Logbook 查看 reload、DHCP 和网络事件；
5. Dashboard 请求查看器确认 AI、UDP DIRECT 和 FINAL 命中；
6. Fast Path 的人工开关边界；
7. 故障时只采集事件码和数量，禁止复制完整订阅 URL、节点或证书。

- [ ] **Step 5: 运行完整自动化验证**

Run: `venv/bin/pytest -q`
Expected: 0 failed。

Run: `venv/bin/pytest tests/test_open_source_safety.py tests/test_logging_safety.py -v`
Expected: PASS。

- [ ] **Step 6: 运行端到端 smoke**

使用临时运行目录启动服务，创建虚构订阅并用本地 HTTP fixture 完成两轮同步；第二轮修改节点显示名并制造一次失败。观察结果必须为：节点 id 和人工字段保持、失败后节点不变、状态页显示上次成功和失败码、只有一个 scheduler 获得锁、日志无 fixture URL 和节点配置。

随后按 `docs/RUNTIME_OPERATIONS.md` 在实际三平台执行最终验收：Mac 两个产物 `surge-cli --check` 通过，iOS Profile Syntax Check 通过，工作 Mac/家庭网关/iOS 各完成一次实时视频，AI 域名命中 AI 组。

- [ ] **Step 7: 提交**

```bash
git add app/logging_config.py app/bootstrap.py app/services/subscription.py app/services/subscription_scheduler.py README.md docs/MIGRATION_VERIFY.md docs/RUNTIME_OPERATIONS.md tests/test_open_source_safety.py tests/test_logging_safety.py
git commit -m "docs: add runtime operations and safe observability"
```
