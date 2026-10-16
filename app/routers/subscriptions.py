"""订阅管理 API"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import Subscription, ProxyNode
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
            "tag": s.tag,
            "color": s.color or SUB_COLORS[(s.id - 1) % len(SUB_COLORS)],
            "enabled": s.enabled,
            "auto_update": s.auto_update,
            "update_interval": s.update_interval,
            "last_attempt_at": s.last_attempt_at.isoformat() if s.last_attempt_at else None,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "next_update_at": s.next_update_at.isoformat() if s.next_update_at else None,
            "last_error_code": s.last_error_code,
            "last_node_count": s.last_node_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in subs
    ]


@router.get("/{sub_id}")
async def get_subscription(sub_id: int, db: AsyncSession = Depends(get_db)):
    """已认证订阅详情：返回 URL 供编辑（列表不携带）"""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")
    return {
        "id": sub.id,
        "name": sub.name,
        "url": sub.url,
        "tag": sub.tag,
        "color": sub.color or SUB_COLORS[(sub.id - 1) % len(SUB_COLORS)],
        "enabled": sub.enabled,
        "auto_update": sub.auto_update,
        "update_interval": sub.update_interval,
        "last_attempt_at": sub.last_attempt_at.isoformat() if sub.last_attempt_at else None,
        "last_success_at": sub.last_success_at.isoformat() if sub.last_success_at else None,
        "next_update_at": sub.next_update_at.isoformat() if sub.next_update_at else None,
        "last_error_code": sub.last_error_code,
        "last_node_count": sub.last_node_count,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


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
    await db.commit()
    return {"id": sub.id, "name": sub.name}


@router.delete("/{sub_id}")
async def delete_subscription(sub_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")

    # 级联删除该订阅下的所有节点（与同步时清旧节点的逻辑保持一致）
    await db.execute(delete(ProxyNode).where(ProxyNode.subscription_id == sub_id))
    await db.execute(delete(Subscription).where(Subscription.id == sub_id))
    await db.commit()
    return {"deleted": sub_id}


@router.post("/{sub_id}/sync")
async def sync_one(sub_id: int, db: AsyncSession = Depends(get_db)):
    """手动同步单项（不受 enabled/auto 限制）：成功 200 安全 DTO；
    同步失败 502 返回同一安全 DTO（不嵌套 detail、不携带异常原文）；
    订阅不存在返回静态 404。"""
    result = await sync_subscription(db, sub_id)
    if result is None:
        raise HTTPException(404, "订阅不存在")
    if not result.success:
        return JSONResponse(status_code=502, content=result.to_dict())
    return result.to_dict()


@router.post("/sync-all")
async def sync_all(db: AsyncSession = Depends(get_db)):
    """同步所有启用订阅：逐项安全 DTO，单项失败不中断（各含错误码）"""
    results = await sync_all_subscriptions(db)
    return {"results": [r.to_dict() for r in results]}
