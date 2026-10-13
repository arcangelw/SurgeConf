"""系统设置端点：语言切换、数据重置、系统级配置。"""

import os

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import seed_defaults
from app.database import init_db, DB_PATH, get_db
from app.i18n import DEFAULT_LOCALE
from app.models import SystemConfig

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
    """重置所有数据：删除数据库文件并重新初始化"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    await init_db()
    await seed_defaults()
    return {"reset": True}


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
