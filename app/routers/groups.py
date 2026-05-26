"""策略分组管理 API"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import RegionGroup, ServiceGroup

router = APIRouter(prefix="/api/groups", tags=["策略分组"])


# ===== 地区分组 =====

class RegionGroupCreate(BaseModel):
    name: str
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    filter_regex: Optional[str] = None
    sort_order: int = 0
    auto_enabled: bool = True
    manual_enabled: bool = True


class RegionGroupUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    filter_regex: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None
    auto_enabled: Optional[bool] = None
    manual_enabled: Optional[bool] = None


@router.get("/regions")
async def list_region_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RegionGroup).order_by(RegionGroup.sort_order))
    groups = result.scalars().all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "display_name": g.display_name,
            "icon_url": g.icon_url,
            "filter_regex": g.filter_regex,
            "sort_order": g.sort_order,
            "enabled": g.enabled,
            "auto_enabled": g.auto_enabled,
            "manual_enabled": g.manual_enabled,
        }
        for g in groups
    ]


@router.post("/regions")
async def create_region_group(data: RegionGroupCreate, db: AsyncSession = Depends(get_db)):
    group = RegionGroup(
        name=data.name,
        display_name=data.display_name,
        icon_url=data.icon_url,
        filter_regex=data.filter_regex,
        sort_order=data.sort_order,
        auto_enabled=data.auto_enabled,
        manual_enabled=data.manual_enabled,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return {"id": group.id, "name": group.name}


@router.put("/regions/{group_id}")
async def update_region_group(group_id: int, data: RegionGroupUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RegionGroup).where(RegionGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(404, "地区分组不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(group, key, value)
    await db.commit()
    return {"id": group.id, "name": group.name}


@router.delete("/regions/{group_id}")
async def delete_region_group(group_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RegionGroup).where(RegionGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(404, "地区分组不存在")
    await db.delete(group)
    await db.commit()
    return {"deleted": group_id}


# ===== 服务分组 =====

class ServiceGroupCreate(BaseModel):
    name: str
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    group_type: str = "select"
    policies: list[str] = []
    sort_order: int = 0


class ServiceGroupUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    group_type: Optional[str] = None
    policies: Optional[list[str]] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None


@router.get("/services")
async def list_service_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServiceGroup).order_by(ServiceGroup.sort_order))
    groups = result.scalars().all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "display_name": g.display_name,
            "icon_url": g.icon_url,
            "group_type": g.group_type,
            "policies": g.policies or [],
            "sort_order": g.sort_order,
            "enabled": g.enabled,
        }
        for g in groups
    ]


@router.post("/services")
async def create_service_group(data: ServiceGroupCreate, db: AsyncSession = Depends(get_db)):
    group = ServiceGroup(
        name=data.name,
        display_name=data.display_name,
        icon_url=data.icon_url,
        group_type=data.group_type,
        policies=data.policies,
        sort_order=data.sort_order,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return {"id": group.id, "name": group.name}


@router.put("/services/{group_id}")
async def update_service_group(group_id: int, data: ServiceGroupUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServiceGroup).where(ServiceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(404, "服务分组不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(group, key, value)
    await db.commit()
    return {"id": group.id, "name": group.name}


@router.delete("/services/{group_id}")
async def delete_service_group(group_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServiceGroup).where(ServiceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(404, "服务分组不存在")
    await db.delete(group)
    await db.commit()
    return {"deleted": group_id}
