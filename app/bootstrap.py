"""应用启动引导：种子数据、启动维护与 lifespan。

schema 与业务数据迁移交给 ``app/services/database_migration.py`` 的
``prepare_database``（staging 原子准备），本模块不再执行任何建表或
schema 变更。
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.database import database
from app.runtime_paths import (configure_private_umask,
                               ensure_runtime_directories,
                               resolve_runtime_paths)
from app.models import RegionGroup, ServiceGroup, RuleSource, ConfigProfile, CustomRule, HostMapping, SystemConfig
from app.default_config import (
    DEFAULT_REGION_GROUPS, DEFAULT_SERVICE_GROUPS,
    DEFAULT_RULE_SOURCES, DEFAULT_GENERAL,
    DEFAULT_URL_REWRITES, DEFAULT_HEADER_REWRITES,
    DEFAULT_MITM, DEFAULT_PRE_RULES, DEFAULT_GENERAL_RULES,
    DEFAULT_HOST_MAPPINGS,
)
from app.profile_targets import default_target_for_role, default_udp_mode
from app.security import DEFAULT_INSTALLATION_ROLE

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def seed_defaults(session_factory) -> None:
    """首次启动时填充默认数据（在 staging 上执行，幂等）"""
    from sqlalchemy import select

    async with session_factory() as db:
        result = await db.execute(select(RegionGroup).limit(1))
        if result.scalar_one_or_none():
            # 已有数据，仅确保系统配置存在
            pass
        else:
            for rg in DEFAULT_REGION_GROUPS:
                db.add(RegionGroup(**rg))

            for sg in DEFAULT_SERVICE_GROUPS:
                db.add(ServiceGroup(**sg))

            for rs in DEFAULT_RULE_SOURCES:
                db.add(RuleSource(**rs))

            # 新建默认档案按 DEFAULT_INSTALLATION_ROLE + 中央 role→target 映射
            # 显式写 target/mode（默认 work_computer → mac_workstation/scoped，
            # 与 0003 迁移旧档 server_default 同值但代码路径独立）
            seed_target = default_target_for_role(DEFAULT_INSTALLATION_ROLE)
            db.add(ConfigProfile(
                name="默认配置",
                description="基于原始 Surge.conf 的默认配置",
                is_default=True,
                locale="zh",
                final_action="兜底",
                target=seed_target.value,
                udp_security_mode=default_udp_mode(seed_target).value,
                general=dict(DEFAULT_GENERAL),
                url_rewrites=list(DEFAULT_URL_REWRITES),
                header_rewrites=list(DEFAULT_HEADER_REWRITES),
                mitm=dict(DEFAULT_MITM),
            ))

            for r in DEFAULT_PRE_RULES:
                db.add(CustomRule(profile_id=None, **r))
            for r in DEFAULT_GENERAL_RULES:
                db.add(CustomRule(profile_id=None, **r))
            for h in DEFAULT_HOST_MAPPINGS:
                db.add(HostMapping(profile_id=None, **h))

            await db.commit()

    # 确保系统级配置存在（首次启动或数据库已有旧数据时）
    # 注：no_resolve_keywords 键已由结构化 rule_sources.ip_rules 取代
    # （历史补键后随 retire_no_resolve_keywords_v1 退役），此处不种该键
    sys_config_keys = {"exclusion_keywords", "other_auto_enabled", "other_manual_enabled", "skip_proxy_custom"}
    existing_configs = set()
    async with session_factory() as db:
        result = await db.execute(select(SystemConfig))
        for row in result.scalars().all():
            existing_configs.add(row.key)
        missing = sys_config_keys - existing_configs
        if "exclusion_keywords" in missing:
            db.add(SystemConfig(key="exclusion_keywords",
                                value="Remain|Expired|Expire|应急|官网|如需|套餐|去除|剩余|距离|Reset|重置|流量|Traffic|Used|已用|Today|今日|GB|Download|Upload|Speed"))
        if "other_auto_enabled" in missing:
            db.add(SystemConfig(key="other_auto_enabled", value="true"))
        if "other_manual_enabled" in missing:
            db.add(SystemConfig(key="other_manual_enabled", value="true"))
        if "skip_proxy_custom" in missing:
            db.add(SystemConfig(key="skip_proxy_custom", value="[]"))
        if missing:
            await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = """
╔══════════════════════════════════════════════════════════╗
║                     SurgeConf                           ║
║                                                         ║
║  免责声明:                                               ║
║  本工具仅用于学习和研究 Surge 配置管理技术。               ║
║  使用前请阅读完整的免责声明:                              ║
║  github.com/arcangelw/SurgeConf#免责声明                ║
║                                                         ║
║  继续使用即表示您接受免责声明的一切条款。                  ║
╚═════════════════════════════════════════════════════════╝
"""
    print(banner, file=sys.stderr)
    configure_private_umask()
    paths = resolve_runtime_paths()
    ensure_runtime_directories(paths)
    # 安全目录创建后、任何迁移/绑定之前配置结构化事件日志：
    # 启动全程的安全事件（database_ready/orphan_cleanup/scheduler_*/
    # subscription_sync/startup_failed）都由此落盘；本进程是
    # RuntimePaths.log_file 的唯一 writer（launchd/nohup 的 stdout/
    # stderr 一律指向独立的 surgeconf-console.log）
    from app.logging_config import (configure_event_logging, log_event,
                                    shutdown_event_logging)
    configure_event_logging(paths.log_file)

    # configure 成功后的一切（首次 harden、迁移、绑定、维护、调度器、
    # yield、清理）都在同一外层异常/清理边界内。清理顺序固定：
    # scheduler stop → database dispose → logging shutdown，每步独立
    # try——前一步抛错也继续执行后续步骤；清理异常绝不遮蔽正在传播
    # 的原始启动/请求异常（只打到 stderr），无原始异常时传播首个
    # 清理异常。
    from app.runtime_paths import harden_runtime_permissions
    scheduler = None
    cleanup_errors: list[BaseException] = []
    try:
        try:
            # 首次 harden（既有产物收敛）也在边界内：失败同样走
            # startup_failed + 完整清理
            harden_runtime_permissions(paths)
            # 延迟导入：database_migration 顶层依赖本模块的 seed_defaults
            from app.services.database_migration import prepare_database
            result = await prepare_database(
                paths, PROJECT_ROOT / "data" / "surge_conf.db")
            harden_runtime_permissions(paths)
            database.bind(paths.database)
            log_event("database_ready",
                      counts={"applied_migrations": len(result.applied_migrations)})
            print(f"[启动] 数据库就绪（来源 {result.source}，"
                  f"已应用业务迁移 {len(result.applied_migrations)} 项）",
                  file=sys.stderr)

            # 数据维护：清理孤儿节点（修复旧版本删除订阅时未级联删除节点导致的残留）
            from app.services.subscription import cleanup_orphan_nodes
            async with database.session_factory() as db:
                removed = await cleanup_orphan_nodes(db)
            log_event("orphan_cleanup", counts={"removed": removed})
            if removed:
                print(f"[数据维护] 已清理 {removed} 个孤儿节点（归属订阅已删除的残留节点）",
                      file=sys.stderr)

            # 单实例自动更新调度器：数据库准备/维护完成后启动；锁竞争进入
            # standby（本实例正常服务，不运行更新循环）
            from app.services.subscription_scheduler import SubscriptionScheduler
            scheduler = SubscriptionScheduler(database, paths.root / "scheduler.lock")
            app.state.subscription_scheduler = scheduler
            app.state.subscription_scheduler_active = await scheduler.start()
            if app.state.subscription_scheduler_active:
                log_event("scheduler_active")
                print("[启动] 订阅自动更新调度器已启动", file=sys.stderr)
            else:
                log_event("scheduler_standby")
                print("[启动] 检测到另一实例正在运行，本实例调度器进入 standby", file=sys.stderr)
        except BaseException:
            # 启动阶段（harden/迁移/绑定/维护/调度器）失败：只记固定
            # 错误码，绝不携带异常消息/类型名，随后交由清理边界收敛
            log_event("startup_failed", error_code="internal_error")
            raise
        yield
        log_event("service_shutdown")
    except BaseException:
        # 原始启动/请求期异常正在传播：清理步骤的异常（若有，稍后由
        # finally 收集）不遮蔽它
        raise
    finally:
        # 嵌套清理：stop 抛错也继续 dispose，dispose 抛错也继续
        # logging shutdown（handler/FD 最终必然关闭）
        try:
            if scheduler is not None:
                await scheduler.stop()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            await database.dispose()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            shutdown_event_logging()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors and sys.exc_info()[0] is not None:
            # 原始异常正在传播：清理异常只提示，绝不遮蔽
            print(f"[关闭] {len(cleanup_errors)} 个清理步骤异常（已忽略，"
                  f"不遮蔽原始错误）", file=sys.stderr)
    if cleanup_errors:
        # 无原始异常（正常 yield 返回）：传播首个清理异常
        raise cleanup_errors[0]
