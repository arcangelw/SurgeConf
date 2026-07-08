"""General 设置 API"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from pydantic import BaseModel
from app.database import get_db
from app.models import ConfigProfile, SystemConfig
from app.general_fields import GENERAL_GROUPS
from app.default_config import DEFAULT_GENERAL, DEFAULT_SKIP_PROXY

router = APIRouter(prefix="/api/general", tags=["General 设置"])


@router.get("/fields")
async def get_fields():
    """返回字段元数据（驱动前端表单渲染）"""
    groups = []
    for g in GENERAL_GROUPS:
        fields = []
        for f in g["fields"]:
            fd = {
                "key": f.key,
                "label_zh": f.label_zh,
                "label_en": f.label_en,
                "type": f.type,
                "hint_zh": f.hint_zh,
                "hint_en": f.hint_en,
                "options": [{"value": o[0], "label_zh": o[1], "label_en": o[2]} for o in f.options],
            }
            fields.append(fd)
        groups.append({
            "key": g["key"],
            "label_zh": g["label_zh"],
            "label_en": g["label_en"],
            "fields": fields,
        })
    return groups


@router.get("/values")
async def get_values(profile_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """返回当前生效的 General 值"""
    profile = None
    if profile_id:
        result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
        profile = result.scalar_one_or_none()
    if not profile:
        result = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == True))
        profile = result.scalar_one_or_none()

    values = dict(DEFAULT_GENERAL)
    if profile and profile.general:
        values.update(profile.general)

    return {
        "profile_id": profile.id if profile else None,
        "profile_name": profile.name if profile else "默认",
        "values": values,
    }


@router.put("/values")
async def save_values(profile_id: int, values: dict, db: AsyncSession = Depends(get_db)):
    """保存 General 值到指定 profile（增量合并）"""
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "配置方案不存在")

    current = dict(profile.general or {})
    current.update(values)
    profile.general = dict(current)
    await db.commit()
    return {"saved": True}


# ===== Skip-Proxy 自定义条目（全局，存于 SystemConfig） =====

@router.get("/skip-proxy")
async def get_skip_proxy(db: AsyncSession = Depends(get_db)):
    """返回自定义 skip-proxy 条目 + 内置默认条目"""
    import json
    result = await db.execute(select(SystemConfig).where(SystemConfig.key == "skip_proxy_custom"))
    cfg = result.scalar_one_or_none()
    try:
        custom = json.loads(cfg.value) if (cfg and cfg.value) else []
    except (ValueError, TypeError):
        custom = []
    return {"custom": custom, "defaults": list(DEFAULT_SKIP_PROXY)}


class SkipProxyUpdate(BaseModel):
    items: list[str]


@router.put("/skip-proxy")
async def save_skip_proxy(data: SkipProxyUpdate, db: AsyncSession = Depends(get_db)):
    """保存自定义 skip-proxy 条目（覆盖式）"""
    import json
    items = [str(x).strip() for x in data.items if str(x).strip()]
    value = json.dumps(items, ensure_ascii=False)
    result = await db.execute(select(SystemConfig).where(SystemConfig.key == "skip_proxy_custom"))
    cfg = result.scalar_one_or_none()
    if cfg:
        cfg.value = value
    else:
        db.add(SystemConfig(key="skip_proxy_custom", value=value))
    await db.commit()
    return {"saved": True, "items": items}
