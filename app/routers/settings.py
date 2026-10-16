"""系统设置端点：语言切换、数据重置、系统级配置。"""

from pathlib import Path
import os

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.database import database, get_db
from app.i18n import DEFAULT_LOCALE
from app.models import SystemConfig
from app.runtime_paths import resolve_runtime_paths
from app.services.database_migration import (PROJECT_ROOT, backup_database,
                                             prepare_database, sqlite_backup)

router = APIRouter(prefix="/api/settings", tags=["系统设置"])


@router.post("/locale")
async def set_locale(request: Request):
    body = await request.json()
    locale = body.get("locale", DEFAULT_LOCALE)
    response = Response(status_code=200)
    response.set_cookie("locale", locale, max_age=365 * 86400)
    return response


@router.post("/reset")
async def reset_data():
    """重置所有数据：先异步关闭引擎并备份，再删除文件走完整 staging 准备流程重建。

    任何失败都先从备份副本还原 live 库并重新绑定，再向上传播异常，
    避免运行时停留在"引擎存活但文件已删"或未绑定状态。
    """
    paths = resolve_runtime_paths()
    backup = None
    try:
        # 必须先 await dispose：同步 dispose 无法可靠关闭 aiosqlite 线程连接，
        # 残留连接会继续写已删除文件的 fd；备份副本供失败路径还原
        await database.dispose()
        if paths.database.exists():
            backup = backup_database(paths.database, paths, "reset")
        # WAL 引入后需连同伴生文件一并删除，否则残留 -wal/-shm 会污染重建的新库
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{paths.database}{suffix}")
            if candidate.exists():
                candidate.unlink()
        result = await prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")
    except BaseException:
        if not paths.database.exists() and backup is not None and backup.exists():
            sqlite_backup(backup, paths.database)
            os.chmod(paths.database, 0o600)
        if paths.database.exists():
            database.bind(paths.database)
        raise
    database.bind(paths.database)
    return {"reset": True, "source": result.source}


# ===== 系统级配置（排除关键词、其他分组等） =====

@router.get("/config")
async def get_sys_config(db=Depends(get_db)):
    """获取所有系统级配置"""
    result = await db.execute(select(SystemConfig))
    configs = {row.key: row.value for row in result.scalars().all()}
    return configs



class SysConfigBatchUpdate(BaseModel):
    configs: dict[str, str]

@router.put("/config/batch")
async def update_sys_config_batch(data: SysConfigBatchUpdate, db=Depends(get_db)):
    """批量 upsert 系统配置：单事务单 commit"""
    if not data.configs:
        return {"updated": 0}
    result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(data.configs.keys())))
    existing = {cfg.key: cfg for cfg in result.scalars().all()}
    for key, value in data.configs.items():
        cfg = existing.get(key)
        if cfg:
            cfg.value = value
        else:
            db.add(SystemConfig(key=key, value=value))
    await db.commit()
    return {"updated": len(data.configs)}
