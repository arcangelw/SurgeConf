"""系统设置端点：语言切换、数据重置、系统级配置。"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.database import database, get_db
from app.i18n import DEFAULT_LOCALE
from app.models import SystemConfig
from app.runtime_paths import resolve_runtime_paths
from app.services.database_migration import PROJECT_ROOT, prepare_database

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
    """重置所有数据：删除数据库文件后走完整 staging 准备流程重建并重绑运行时"""
    # WAL 引入后需连同伴生文件一并删除，否则残留 -wal/-shm 会污染重建的新库
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database.path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    paths = resolve_runtime_paths()
    result = await prepare_database(paths, PROJECT_ROOT / "data" / "surge_conf.db")
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
