"""General 设置 API"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import ConfigProfile
from app.general_fields import GENERAL_GROUPS

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
    from app.default_config import DEFAULT_GENERAL

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
