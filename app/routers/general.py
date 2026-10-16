"""General 设置 API — 字段元数据与值均按档案目标（ProfileTarget）过滤"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from pydantic import BaseModel
from app.database import get_db
from app.models import ConfigProfile, SystemConfig
from app.general_fields import general_groups_for_target
from app.default_config import DEFAULT_GENERAL, DEFAULT_SKIP_PROXY
from app.profile_targets import (ProfileTarget, filter_general_for_target,
                                 forced_general_values, general_targets_for)
from app.services.profile_resolution import (DefaultProfileMissing, ProfileNotFound,
                                             resolve_profile)

router = APIRouter(prefix="/api/general", tags=["General 设置"])


async def _resolve_or_404(db: AsyncSession, profile_id: int | None) -> ConfigProfile:
    """档案解析唯一入口：显式 id 不存在或无默认档案均 404，绝不复制回退逻辑"""
    try:
        return await resolve_profile(db, profile_id)
    except ProfileNotFound:
        raise HTTPException(404, "配置方案不存在")
    except DefaultProfileMissing:
        raise HTTPException(404, "未找到默认配置方案")


def _drop_empty(values: dict) -> dict:
    """跳过 None 与空字符串（清空的表单项不应写入配置）"""
    return {k: v for k, v in values.items() if v is not None and v != ""}


@router.get("/fields")
async def get_fields(profile_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """返回当前目标可配置的字段元数据（驱动前端表单渲染）"""
    profile = await _resolve_or_404(db, profile_id)
    return general_groups_for_target(ProfileTarget(profile.target))


@router.get("/values")
async def get_values(profile_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """返回当前生效的 General 值（按目标过滤 + 强制安全值）"""
    profile = await _resolve_or_404(db, profile_id)
    values = dict(DEFAULT_GENERAL)
    if profile.general:
        values.update(profile.general)
    values = filter_general_for_target(ProfileTarget(profile.target), _drop_empty(values))

    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "values": values,
    }


@router.put("/values")
async def save_values(profile_id: int, values: dict, db: AsyncSession = Depends(get_db)):
    """保存 General 值到指定 profile（增量合并）

    - 当前目标不支持的请求键：422 并指出 key（不静默吞新输入）
    - 强制安全键（block-quic 等）不允许危险覆盖值：422
    - 旧目标持久残留的不兼容键：静默清理
    - 强制安全值随保存固化到持久层
    """
    profile = await _resolve_or_404(db, profile_id)
    target = ProfileTarget(profile.target)

    unsupported = sorted(k for k in values if target not in general_targets_for(k))
    if unsupported:
        raise HTTPException(
            422, f"当前目标 {target.value} 不支持字段: {', '.join(unsupported)}")

    forced = forced_general_values(target)
    for key, forced_value in forced.items():
        if key in values and values[key] != forced_value:
            raise HTTPException(
                422, f"字段 {key} 已强制为安全值 {forced_value!r}，不允许覆盖")

    current = dict(profile.general or {})
    current.update(values)
    # 清理旧目标残留（持久值，非本次输入）后固化强制安全值
    cleaned = {k: v for k, v in current.items() if target in general_targets_for(k)}
    cleaned.update(forced)
    profile.general = cleaned
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
