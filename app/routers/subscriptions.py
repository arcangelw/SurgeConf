"""订阅管理 API"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from app.database import get_db
from app.models import Subscription
from app.services.subscription import sync_subscription, sync_all_subscriptions

router = APIRouter(prefix="/api/subscriptions", tags=["订阅管理"])

SUB_COLORS = ["#6c5ce7", "#00b894", "#e17055", "#0984e3", "#fdcb6e", "#e84393", "#00cec9", "#d63031"]


class SubscriptionCreate(BaseModel):
    name: str
    url: str
    tag: Optional[str] = None
    color: Optional[str] = None
    enabled: bool = True
    auto_update: bool = True
    update_interval: int = 86400


class SubscriptionUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    tag: Optional[str] = None
    color: Optional[str] = None
    enabled: Optional[bool] = None
    auto_update: Optional[bool] = None
    update_interval: Optional[int] = None


@router.get("")
async def list_subscriptions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).order_by(Subscription.id))
    subs = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "tag": s.tag,
            "color": s.color or SUB_COLORS[(s.id - 1) % len(SUB_COLORS)],
            "enabled": s.enabled,
            "auto_update": s.auto_update,
            "update_interval": s.update_interval,
            "last_update": s.last_update.isoformat() if s.last_update else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in subs
    ]


@router.post("")
async def create_subscription(data: SubscriptionCreate, db: AsyncSession = Depends(get_db)):
    sub = Subscription(
        name=data.name,
        url=data.url,
        tag=data.tag,
        color=data.color,
        enabled=data.enabled,
        auto_update=data.auto_update,
        update_interval=data.update_interval,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"id": sub.id, "name": sub.name}


@router.put("/{sub_id}")
async def update_subscription(sub_id: int, data: SubscriptionUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(sub, key, value)
    sub.updated_at = datetime.now()
    await db.commit()
    return {"id": sub.id, "name": sub.name}


@router.delete("/{sub_id}")
async def delete_subscription(sub_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")

    await db.execute(delete(Subscription).where(Subscription.id == sub_id))
    await db.commit()
    return {"deleted": sub_id}


@router.post("/{sub_id}/sync")
async def sync_one(sub_id: int, db: AsyncSession = Depends(get_db)):
    result = await sync_subscription(db, sub_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/sync-all")
async def sync_all(db: AsyncSession = Depends(get_db)):
    results = await sync_all_subscriptions(db)
    return {"results": results}
