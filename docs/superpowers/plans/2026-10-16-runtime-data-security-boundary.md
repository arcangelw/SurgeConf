# 运行数据与安全边界实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 SurgeConf 的数据库、日志、备份和导出迁出源码仓库，并建立本机管理、局域网认证管理和逐档案只读订阅的安全边界。

**Architecture:** 运行目录解析、SQLite 生命周期、迁移/备份和认证分别放入独立模块。应用启动时先在未连接数据库的状态下完成原子搬迁和 Alembic 升级，再创建异步引擎；HTTP 中间件只信任直接 TCP 对端地址，管理会话与 Profile Token 使用不同的验证路径。

**Tech Stack:** Python 3.13、FastAPI 0.115、SQLAlchemy 2.0、aiosqlite 0.20、Alembic、SQLite Backup API、Jinja2、原生 JavaScript、pytest。

**Spec:** `docs/superpowers/specs/2026-10-16-target-aware-surge-config-design.md`

## Global Constraints

- 工作实例默认 `installation_role=work_computer`、`admin_access=local_only`。
- 家庭网关切换到 `lan_authenticated` 前必须先设置管理密码。
- 默认数据库目录固定为 `~/Library/Application Support/SurgeConf/`；测试可用 `SURGECONF_DATA_DIR` 覆盖。
- 数据目录权限为 `0700`；数据库、WAL、备份、敏感导出、日志和会话密钥权限为 `0600`。
- SQLite 必须启用 WAL、`synchronous=NORMAL`、`busy_timeout=5000` 和外键。
- 旧仓库数据库永不自动删除；任何 schema 升级必须先生成可恢复备份，并在 staging 副本成功后原子替换。
- 管理密码只保存带随机盐的 `scrypt` 哈希；Profile Token 只保存 SHA-256 哈希，明文仅在创建或轮换时返回一次。
- 未认证请求不得读取节点凭据、订阅 URL、企业配置、MITM 或备份。
- Uvicorn 和应用日志不得记录包含 Profile Token 的完整请求目标；启动脚本关闭默认 access log，业务日志只记录路由模板、状态码和档案 id。
- 仓库、测试夹具、文档、日志和提交信息不得包含真实用户目录、局域网地址、企业域名、节点凭据、证书或 Token。
- 本计划完成后再执行目标感知档案计划；不得在本阶段引入 UDP 行为变化。

## Test Fixture Contracts

- `runtime_paths(root)` returns a `RuntimePaths` whose root, backups, exports, key and log paths all live below `root`; it never touches the real home directory.
- `create_current_schema_database(path)` creates the pre-Alembic schema represented by commit `e919e16`, inserts one synthetic profile, and deliberately omits `alembic_version`.
- `sqlite_scalar(path, sql)` opens a short-lived sqlite3 connection, returns the first column, and always closes it.
- `admin_client` uses a loopback ASGI client; `client_from_lan` uses `203.0.113.10` as the direct peer and never relies on forwarding headers.
- `repo_text_files` enumerates tracked and untracked, non-ignored UTF-8 files; `scan_files` returns only `(path, rule_id)` pairs.

---

### Task 1: 运行目录与 SQLite 连接策略

**Files:**
- Create: `app/runtime_paths.py`
- Modify: `app/database.py`
- Create: `tests/test_runtime_paths.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- Produces: `RuntimePaths`, `resolve_runtime_paths(home=None)`, `ensure_runtime_directories(paths)`、`configure_private_umask()`。
- Produces: `DatabaseRuntime`, `database`, `get_db()`；后续迁移、备份和调度只通过 `database` 获取引擎与会话。
- Removes: 模块级 `DB_DIR`、`DB_PATH`、`engine`、`async_session` 导出；所有调用方在本阶段内完成迁移，不保留兼容别名。

- [ ] **Step 1: 写运行目录和 PRAGMA 的失败测试**

```python
# tests/test_runtime_paths.py
from pathlib import Path
from sqlalchemy import text

from app.runtime_paths import resolve_runtime_paths, ensure_runtime_directories
from app.database import DatabaseRuntime


def test_data_dir_override_is_private(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setenv("SURGECONF_DATA_DIR", str(root))
    paths = resolve_runtime_paths(home=tmp_path / "home")
    ensure_runtime_directories(paths)
    assert paths.database == root / "surge_conf.db"
    assert paths.backups == root / "backups"
    assert paths.exports == root / "exports"
    assert paths.root.stat().st_mode & 0o777 == 0o700


async def test_sqlite_connection_pragmas(tmp_path):
    runtime = DatabaseRuntime.create(tmp_path / "runtime.db")
    async with runtime.engine.connect() as conn:
        assert (await conn.scalar(text("PRAGMA journal_mode"))).lower() == "wal"
        assert await conn.scalar(text("PRAGMA synchronous")) == 1
        assert await conn.scalar(text("PRAGMA busy_timeout")) == 5000
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
    await runtime.dispose()
```

- [ ] **Step 2: 运行测试确认因接口缺失失败**

Run: `venv/bin/pytest tests/test_runtime_paths.py -v`
Expected: FAIL，提示 `app.runtime_paths` 或 `DatabaseRuntime` 不存在。

- [ ] **Step 3: 实现路径值对象和显式数据库运行时**

```python
# app/runtime_paths.py
@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    database: Path
    backups: Path
    exports: Path
    session_secret: Path
    log_dir: Path
    log_file: Path


def resolve_runtime_paths(home: Path | None = None) -> RuntimePaths:
    home = home or Path.home()
    root = Path(os.environ.get(
        "SURGECONF_DATA_DIR",
        home / "Library" / "Application Support" / "SurgeConf",
    )).expanduser()
    log_dir = home / "Library" / "Logs" / "SurgeConf"
    return RuntimePaths(root, root / "surge_conf.db", root / "backups",
                        root / "exports", root / "session.key",
                        log_dir, log_dir / "surgeconf.log")
```

`configure_private_umask()` 在任何文件或数据库连接创建前执行 `os.umask(0o077)`，保证 SQLite 后续创建的 WAL/SHM 继承私有权限。`ensure_runtime_directories` 使用 `mkdir(mode=0o700, parents=True, exist_ok=True)` 后再次 `chmod(0o700)`。`DatabaseRuntime.create(path)` 创建 `sqlite+aiosqlite` 引擎，并通过 SQLAlchemy `connect` 事件对每条 DB-API 连接执行四条 PRAGMA；`get_db()` 从全局 `database.session_factory` 产生会话。

- [ ] **Step 4: 更新测试夹具，不再依赖被删除的模块级引擎**

`tests/conftest.py` 继续创建每测试独立的内存引擎，但共享 `Base`；API 测试后续通过 `app.dependency_overrides[get_db]` 注入测试会话，不再 patch `app.database.engine`。

- [ ] **Step 5: 运行定向测试**

Run: `venv/bin/pytest tests/test_runtime_paths.py tests/test_api_endpoints.py -v`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/runtime_paths.py app/database.py tests/test_runtime_paths.py tests/conftest.py tests/test_api_endpoints.py
git commit -m "refactor: isolate runtime database state"
```

---

### Task 2: Alembic 基线和原子数据库搬迁

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_current_schema.py`
- Create: `app/services/database_migration.py`
- Create: `app/services/data_migrations.py`
- Modify: `app/database.py`
- Modify: `app/bootstrap.py`
- Modify: `app/models.py`
- Modify: `requirements.txt`
- Create: `tests/test_database_migration.py`
- Replace: `tests/test_migrations.py` 中只验证异常式 `ALTER TABLE` 的结构迁移测试；保留业务数据迁移测试。

**Interfaces:**
- Consumes: `RuntimePaths`、`DatabaseRuntime`。
- Produces: `async prepare_database(paths: RuntimePaths, legacy_path: Path) -> MigrationResult`；schema、种子和业务数据迁移都在 staging 数据库完成。
- Produces: `run_alembic_upgrade(database_path: Path) -> None`，只操作传入的 staging 文件。
- Adds model `DataMigrationRecord(key, applied_at)`；`app/services/data_migrations.py` 提供 `has_data_migration(db, key)` 和 `record_data_migration(db, key)`，供后续业务数据迁移显式记账。
- Invariant: `lifespan` 在创建任何数据库连接前调用 `prepare_database`。

- [ ] **Step 1: 写新库、旧库、失败回滚和重复启动测试**

```python
# tests/test_database_migration.py

@pytest.mark.asyncio
async def test_legacy_database_moves_through_staging(tmp_path):
    legacy = create_current_schema_database(tmp_path / "repo" / "data" / "surge_conf.db")
    paths = runtime_paths(tmp_path / "app-support")
    result = await prepare_database(paths, legacy)
    assert result.source == "legacy"
    assert paths.database.exists()
    assert legacy.exists()
    assert sqlite_scalar(paths.database, "PRAGMA integrity_check") == "ok"
    assert paths.database.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_failed_upgrade_keeps_live_database_unchanged(tmp_path, monkeypatch):
    paths = runtime_paths(tmp_path / "app-support")
    create_current_schema_database(paths.database)
    before = paths.database.read_bytes()
    monkeypatch.setattr(database_migration, "run_alembic_upgrade",
                        lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await prepare_database(paths, tmp_path / "missing.db")
    assert paths.database.read_bytes() == before


@pytest.mark.asyncio
async def test_prepare_database_is_idempotent(tmp_path):
    paths = runtime_paths(tmp_path / "app-support")
    await prepare_database(paths, tmp_path / "missing.db")
    first = sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version")
    backups_before = {item.name for item in paths.backups.iterdir()}
    await prepare_database(paths, tmp_path / "missing.db")
    assert sqlite_scalar(paths.database, "SELECT version_num FROM alembic_version") == first
    assert {item.name for item in paths.backups.iterdir()} == backups_before
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv/bin/pytest tests/test_database_migration.py -v`
Expected: FAIL，迁移服务和 Alembic 配置尚不存在。

- [ ] **Step 3: 增加 Alembic 当前 schema 基线**

`0001_current_schema.py` 必须显式创建当前 `models.py` 的全部表、索引和唯一约束，并新增 `data_migration_records(key PRIMARY KEY, applied_at)`，不调用 `Base.metadata.create_all()`。`env.py` 从 `-x database_path=<path>` 读取同步 SQLite URL，禁止硬编码仓库路径。

`run_alembic_upgrade(path)` 使用程序化配置：

```python
def run_alembic_upgrade(database_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.cmd_opts = Namespace(x=[f"database_path={database_path}"])
    command.upgrade(cfg, "head")
```

现有数据库若包含当前基线表且没有 `alembic_version`，先验证必需表和列，再 `stamp 0001_current_schema`；未知或不完整 schema 必须中止并给出表/列名，不执行猜测性修补。

- [ ] **Step 4: 实现 staging 升级与原子替换**

`prepare_database` 分三条路径：

1. 新旧库均不存在：在同目录 staging 文件上执行 Alembic、种子和业务数据迁移，再 `integrity_check`、`chmod(0600)`、`os.replace`。
2. 仅旧仓库库存在：SQLite Backup API 复制到 staging，同时把未升级副本备份到 `backups/`；在 staging 上完成 stamp/upgrade、种子、业务数据迁移和校验后原子发布；旧库保持原样。
3. 新库已存在：先只读检查 Alembic head 和业务迁移 key；全部已完成时直接复用 live，不创建备份或 staging。存在待执行项时才用 SQLite Backup API 生成时间戳备份并复制 live 到 staging；只在 staging 执行全部迁移，成功后原子替换 live。失败时删除 staging，live 字节不变。

`prepare_database` 为 staging 创建临时 `DatabaseRuntime`，依次调用 `seed_defaults(session_factory)` 和 `apply_data_migrations(session_factory)`，在检查 WAL 后关闭引擎并清理 staging 的 `-wal/-shm`，再做最终完整性检查和替换。禁止 `shutil.copyfile` 复制打开的 SQLite；备份文件名只含 UTC 时间和 schema 版本，不含主机名或用户名。

- [ ] **Step 5: 删除 `init_db()` 中捕获所有异常的 `ALTER TABLE`**

`app/database.py` 不再执行建表或 schema 变更。`app/bootstrap.py::lifespan` 顺序固定为：

```python
configure_private_umask()
paths = resolve_runtime_paths()
ensure_runtime_directories(paths)
await prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")
database.bind(paths.database)
```

`apply_data_migrations` 改为有序注册表，每项与写入 `DataMigrationRecord` 使用同一事务。现有逻辑拆成固定 key：`security_rules_v1`、`douyin_rule_v1`、`ipv6_vif_v1`、`dns_defaults_v1`、`remove_global_quic_v1`、`no_resolve_keywords_v1`；已记录项直接跳过，失败项回滚且不写记录。这样用户之后删除默认规则不会在每次启动时被重新补回。

- [ ] **Step 6: 运行迁移测试和业务数据迁移回归**

Run: `venv/bin/pytest tests/test_database_migration.py tests/test_migrations.py -v`
Expected: PASS；失败升级用例确认 live 数据库未变化。

- [ ] **Step 7: 提交**

```bash
git add alembic.ini migrations app/services/database_migration.py app/services/data_migrations.py app/database.py app/models.py app/bootstrap.py requirements.txt tests/test_database_migration.py tests/test_migrations.py
git commit -m "feat: migrate runtime database atomically"
```

---


### Task 3: 管理认证、会话和 CSRF

**Files:**
- Create: `app/security.py`
- Create: `app/routers/auth.py`
- Modify: `app/main.py`
- Modify: `app/pages.py`
- Modify: `app/templates/base.html`
- Create: `app/templates/login.html`
- Modify: `app/static/js/app.js`
- Modify: `app/templates/settings.html`
- Modify: `app/static/js/settings.js`
- Modify: `app/i18n.py`
- Create: `tests/test_auth.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `hash_admin_password(password) -> str`、`verify_admin_password(password, encoded) -> bool`，编码格式固定为 `scrypt$n$r$p$salt_b64$digest_b64`。
- Produces: `SessionSigner`，使用 Application Support 下 32 字节随机密钥签发 `surgeconf_session`。
- Produces: `require_admin_request(request, settings)` 中间件判定和 `require_csrf(request)`。
- Produces: `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/session`、`PUT /api/settings/access`。

- [ ] **Step 1: 写访问矩阵和 CSRF 失败测试**

```python
@pytest.mark.parametrize("path", ["/", "/api/subscriptions", "/api/configs/profiles"])
async def test_local_only_rejects_non_loopback(client_from_lan, path):
    assert (await client_from_lan.get(path)).status_code == 403


async def test_lan_admin_requires_login_and_csrf(client_from_lan):
    assert (await client_from_lan.get("/api/configs/profiles")).status_code == 401
    login = await client_from_lan.post("/api/auth/login", json={"password": "correct horse battery staple"})
    assert login.status_code == 200
    assert (await client_from_lan.put("/api/settings/config/batch", json={"configs": {}})).status_code == 403
    csrf = login.json()["csrf_token"]
    assert (await client_from_lan.put("/api/settings/config/batch", headers={"X-CSRF-Token": csrf}, json={"configs": {}})).status_code == 200
```

- [ ] **Step 2: 运行测试确认当前中间件放行页面且仅支持静态 Bearer Token**

Run: `venv/bin/pytest tests/test_auth.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现密码、签名会话和直接对端判定**

管理会话签名使用 `itsdangerous.URLSafeTimedSerializer` 和 Application Support 下 32 字节随机密钥；`requirements.txt` 固定 `itsdangerous==2.2.0`。密码使用 `hashlib.scrypt(n=2**14, r=8, p=1, salt=os.urandom(16))` 和 `hmac.compare_digest`。会话负载只含随机 session id、签发时间、CSRF 随机值；Cookie 设置 `HttpOnly`、`SameSite=Strict`、`Path=/`，HTTPS 请求额外设置 `Secure`。退出登录清除 Cookie。

`local_only` 使用 `ipaddress.ip_address(request.client.host).is_loopback`；不信任 `X-Forwarded-For`。回环页面请求自动获得签名管理会话和 CSRF 值，避免本机模式要求密码；非回环请求永远不能利用该隐式会话。`lan_authenticated` 未登录页面重定向到 `/login`，API 返回 401。静态资源和登录端点公开，但不暴露数据。

删除 `_API_TOKEN`/`SURGE_API_TOKEN` 静态 Bearer 中间件和其公开页面例外列表；新的访问矩阵是唯一认证入口，不保留双轨认证。

- [ ] **Step 4: 将所有状态变更请求接入 CSRF**

`base.html` 输出当前会话的 CSRF meta；`app.js` 的 `post`、`put`、`del` 自动附加 `X-CSRF-Token`。直接使用 `fetch` 的 locale、重置、同步和节点操作改用统一 API 封装或显式添加该头。

- [ ] **Step 5: 实现设置页访问模式切换**

`PUT /api/settings/access` 接受：

```json
{
  "installation_role": "home_gateway",
  "admin_access": "lan_authenticated",
  "current_password": null,
  "new_password": "correct horse battery staple"
}
```

首次启用 LAN 管理必须同时设置密码；后续修改密码要求验证当前密码。响应只返回角色、访问模式和 `password_configured`，绝不返回哈希。

- [ ] **Step 6: 运行认证测试和 API 回归**

Run: `venv/bin/pytest tests/test_auth.py tests/test_api_endpoints.py -v`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add app/security.py app/routers/auth.py app/main.py app/pages.py app/templates/base.html app/templates/login.html app/static/js/app.js app/templates/settings.html app/static/js/settings.js app/i18n.py tests/test_auth.py requirements.txt
git commit -m "feat: enforce instance administration boundary"
```

---

### Task 4: 备份、恢复、重置和显式导出

**Files:**
- Create: `app/services/backups.py`
- Modify: `app/routers/settings.py`
- Modify: `app/routers/configs.py`
- Modify: `app/templates/settings.html`
- Modify: `app/static/js/settings.js`
- Modify: `app/i18n.py`
- Create: `tests/test_backups.py`
- Modify: `tests/test_api_endpoints.py`

**Interfaces:**
- Consumes: `database`, `RuntimePaths`。
- Produces: `create_backup(reason: str) -> BackupInfo`、`list_backups() -> list[BackupInfo]`、`restore_backup(name: str) -> None`、`reset_database() -> None`。
- Produces admin endpoints: `POST /api/settings/backups`、`GET /api/settings/backups`、`GET /api/settings/backups/{name}`、`POST /api/settings/backups/{name}/restore`、`POST /api/settings/reset`。
- Changes generation: `GET /api/configs/generate` and `/preview` are memory-only; `POST /api/configs/{profile_id}/exports` is the only server-side export path.

- [ ] **Step 1: 写备份一致性和生成端点无副作用测试**

```python
async def test_backup_is_integrity_checked_and_pruned(runtime_db):
    for _ in range(12):
        await create_backup("manual")
    backups = list_backups()
    assert len(backups) == 10
    assert all(sqlite_scalar(item.path, "PRAGMA integrity_check") == "ok" for item in backups)


async def test_generate_does_not_write_server_file(client, runtime_paths):
    response = await client.get("/api/configs/generate?profile_id=1&filename=Example.conf")
    assert response.status_code == 200
    assert list(runtime_paths.exports.iterdir()) == []
```

- [ ] **Step 2: 运行测试确认现有 `/generate` 写文件导致失败**

Run: `venv/bin/pytest tests/test_backups.py tests/test_api_endpoints.py -v`
Expected: FAIL；`exports` 服务尚不存在，生成端点仍写源码目录。

- [ ] **Step 3: 实现 SQLite Backup API 服务**

每次备份使用只读源连接和临时目标文件，完成后执行 `integrity_check`、`chmod(0600)`、`os.replace`。恢复流程必须：创建强制备份 → `await database.dispose()` → 把选中备份复制到 staging → 在 staging 执行 Alembic、数据迁移和 `integrity_check` → 原子替换 → `database.bind()`；重置流程以 Alembic 在 staging 创建全新库后原子替换。任一步失败都重新绑定原 live 库并抛错。

下载响应只接受 `BackupInfo.name` 白名单值，不接受任意路径。恢复、重置和显式导出接口要求请求体包含 `{"confirm": true}`。

- [ ] **Step 4: 删除生成端点源码目录写入**

`generate_config` 直接返回内存内容及安全的 `Content-Disposition`；文件名通过 `Path(name).name` 和 ASCII/RFC 5987 编码生成响应头。新导出端点只写 `RuntimePaths.exports`，文件权限为 `0600`。

- [ ] **Step 5: 在设置页增加备份/恢复/重置状态**

页面展示备份时间、schema 版本、文件大小和原因，不展示绝对路径。恢复和重置沿用 `confirmDialog` 二次确认；下载使用浏览器导航，错误通过同源 API 先校验后再下载。

- [ ] **Step 6: 运行定向测试**

Run: `venv/bin/pytest tests/test_backups.py tests/test_api_endpoints.py -v`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add app/services/backups.py app/routers/settings.py app/routers/configs.py app/templates/settings.html app/static/js/settings.js app/i18n.py tests/test_backups.py tests/test_api_endpoints.py
git commit -m "feat: add safe database backup lifecycle"
```


---

### Task 5: 逐档案只读订阅 Token 与响应脱敏

**Files:**
- Create: `migrations/versions/0002_profile_subscription_tokens.py`
- Modify: `app/models.py`
- Modify: `app/security.py`
- Modify: `app/routers/configs.py`
- Modify: `app/routers/nodes.py`
- Modify: `app/routers/subscriptions.py`
- Modify: `app/static/js/profiles.js`
- Modify: `app/i18n.py`
- Create: `tests/test_profile_tokens.py`
- Modify: `tests/test_api_endpoints.py`
- Modify: `tests/test_mitm.py`

**Interfaces:**
- Adds: `ConfigProfile.subscription_token_hash: str | None`。
- Produces: `issue_profile_token() -> tuple[plain_token, token_hash]`、`verify_profile_token(plain, token_hash) -> bool`。
- Produces: `POST /api/configs/profiles/{profile_id}/token`，仅管理会话可调用，明文只在本次响应出现。
- Replaces: 未认证 `/api/configs/subscribe?profile_id=...`，新端点为 `GET /api/configs/profiles/{profile_id}/subscribe?token=...`；失败统一 404。

- [ ] **Step 1: 写 Token 隔离和敏感响应测试**

```python
async def test_profile_token_reads_only_its_profile(admin_client, anonymous_client):
    token = (await admin_client.post("/api/configs/profiles/1/token")).json()["token"]
    assert (await anonymous_client.get(f"/api/configs/profiles/1/subscribe?token={token}")).status_code == 200
    assert (await anonymous_client.get(f"/api/configs/profiles/2/subscribe?token={token}")).status_code == 404
    assert (await anonymous_client.get("/api/configs/profiles/1/subscribe?token=bad")).status_code == 404


async def test_list_responses_exclude_credentials(admin_client):
    profile = (await admin_client.get("/api/configs/profiles")).json()[0]
    assert "mitm" not in profile and "general" not in profile
    node = (await admin_client.get("/api/nodes")).json()[0]
    assert "config" not in node and "server" not in node
    subscription = (await admin_client.get("/api/subscriptions")).json()[0]
    assert "url" not in subscription
```

- [ ] **Step 2: 运行测试确认未认证订阅和敏感列表当前可读**

Run: `venv/bin/pytest tests/test_profile_tokens.py tests/test_api_endpoints.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现高熵 Profile Token**

明文使用 `secrets.token_urlsafe(32)`；数据库保存 `sha256(token.encode()).hexdigest()`。验证先检查格式和长度，再使用 `hmac.compare_digest`。迁移不给旧档案自动生成不可见 Token；UI 显示“未配置”，由管理员首次生成。

- [ ] **Step 4: 收紧普通列表响应**

档案列表仅返回 id、name、description、is_default、locale、未来的 target/mode 和 `token_configured`。MITM/General 使用已认证的单档案详情端点；MITM 密钥字段使用 `configured: true|false` 和覆盖式写入。节点列表不返回 `server`、`port`、`config`；节点详情仍位于管理边界。订阅列表不返回 URL，新增已认证详情端点供编辑。

- [ ] **Step 5: 更新订阅 URL UI**

Token 生成/轮换后只在一次性对话框中显示完整 URL；关闭后前端状态不缓存明文。复制按钮在没有 Token 时提示先生成，不从数据库或列表响应恢复明文。

- [ ] **Step 6: 运行安全测试**

Run: `venv/bin/pytest tests/test_profile_tokens.py tests/test_api_endpoints.py tests/test_mitm.py -v`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add migrations/versions/0002_profile_subscription_tokens.py app/models.py app/security.py app/routers/configs.py app/routers/nodes.py app/routers/subscriptions.py app/static/js/profiles.js app/i18n.py tests/test_profile_tokens.py tests/test_api_endpoints.py tests/test_mitm.py
git commit -m "feat: secure profile subscriptions and responses"
```

---

### Task 6: 开源脱敏、启动脚本和阶段 A 运行验收

**Files:**
- Modify: `app/default_config.py`
- Modify: `run_surgeconf.sh`
- Modify: `start.sh`
- Modify: `surgeconf.sh`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `docs/MIGRATION_VERIFY.md`
- Create: `tests/test_open_source_safety.py`
- Modify: `tests/test_mitm.py`
- Create: `tests/fixtures/synthetic_profile.conf`

**Interfaces:**
- Removes: 默认控制器用户名和已知访问密钥；`SURGE_CONTROLLER_ACCESS`、`SURGE_HTTP_API` 未设置时不输出对应 General 键。
- Changes: launchd 日志写入 `~/Library/Logs/SurgeConf/surgeconf.log`；`SURGE_HOST` 默认仍为 `127.0.0.1`，只有明确启用 LAN 管理后才由用户设置为局域网监听。

- [ ] **Step 1: 写仓库安全基线测试**

```python
# tests/test_open_source_safety.py
FORBIDDEN = [
    re.compile("/" + "Users/" + r"[^/\s]+/"),
    re.compile("BEGIN " + r"(?:RSA |EC )?PRIVATE KEY"),
    re.compile(r"(?:ca-p12|SURGE_API_TOKEN)\s*[=:]\s*[^\s$<{]", re.I),
]


def test_default_general_has_no_static_controller_credentials():
    assert not {"external-controller-access", "http-api"} & DEFAULT_GENERAL.keys()


def test_repository_text_has_no_machine_or_secret_material(repo_text_files):
    violations = scan_files(
        repo_text_files,
        FORBIDDEN,
        excluded={"tests/test_open_source_safety.py"},
        allowlist={"tests/fixtures/synthetic_profile.conf"},
    )
    assert violations == []
```

扫描输入必须来自 `git ls-files --cached --others --exclude-standard -z`，覆盖已跟踪和待提交文件，并忽略二进制文件与依赖目录；测试只输出文件和规则编号，不回显疑似秘密内容。

- [ ] **Step 2: 运行测试确认静态控制器默认值被检出**

Run: `venv/bin/pytest tests/test_open_source_safety.py -v`
Expected: FAIL，`test_default_general_has_no_static_controller_credentials` 指向 `app/default_config.py` 的静态控制器键；扫描器不打印匹配值。

- [ ] **Step 3: 删除个人化和不安全默认值**

`DEFAULT_GENERAL` 不再含静态 `external-controller-access` 或 `http-api`。生成器只在对应环境变量非空时注入；文档示例使用 `example-access-key@127.0.0.1:6160`。`tests/test_mitm.py` 从 `tests/fixtures/synthetic_profile.conf` 读取明确标记的短虚构证书数据，不再内联类似真实证书长度和前缀的字符串。

- [ ] **Step 4: 更新服务脚本和文档**

三个脚本在启动 Python 前执行 `umask 077`，创建 Application Support/Logs 目录并设置权限，不再创建仓库 `logs/`；Uvicorn 一律传 `--no-access-log`，避免查询参数中的 Profile Token 进入默认访问日志。README 项目结构删除 `data/` 作为运行目录的描述，新增工作实例和家庭网关部署命令。`MIGRATION_VERIFY.md` 的 SQLite 和日志命令改为从 `SURGECONF_DATA_DIR` 或默认 Application Support 路径读取，不包含真实设备信息。

- [ ] **Step 5: 运行完整自动化验证**

Run: `venv/bin/pytest -q`
Expected: 全部通过，0 failed。

Run: `venv/bin/python -m alembic -x database_path=/tmp/surgeconf-plan-a-smoke.db upgrade head`
Expected: exit 0；临时数据库 `PRAGMA integrity_check` 为 `ok`。完成后删除该临时文件。

- [ ] **Step 6: 运行服务 smoke**

Run: `SURGECONF_DATA_DIR="$(mktemp -d)" SURGE_HOST=127.0.0.1 SURGE_PORT=61831 venv/bin/python -m uvicorn app.main:app --no-access-log`

另一个终端验证：

```bash
curl -i http://127.0.0.1:61831/
curl -i http://127.0.0.1:61831/api/configs/profiles
```

Expected: 本机请求成功；数据库位于临时运行目录；源码树未产生数据库、配置或日志文件。停止服务并删除临时目录。

再以 `SURGE_HOST=0.0.0.0` 启动同一临时实例，从另一台局域网设备请求首页和管理 API：`local_only` 下均为 403；切换 `lan_authenticated` 后，未登录 API 为 401，登录且携带 CSRF 的写请求成功。最后用一个新生成的档案 Token 请求只读订阅并确认 200，用同一 Token 请求其他档案确认 404。验收记录只写状态码，不记录主机地址、Cookie 或 Token。

- [ ] **Step 7: 提交**

```bash
git add app/default_config.py run_surgeconf.sh start.sh surgeconf.sh .gitignore README.md docs/MIGRATION_VERIFY.md tests/test_open_source_safety.py tests/test_mitm.py tests/fixtures/synthetic_profile.conf
git commit -m "chore: harden open-source runtime defaults"
```
