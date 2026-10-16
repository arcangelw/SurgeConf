"""订阅管理 API"""

import re
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel, Field, field_validator
from typing import Optional

from app.database import get_db
from app.models import (Subscription, ProxyNode, UPDATE_INTERVAL_MIN,
                        UPDATE_INTERVAL_MAX)
from app.services.subscription import (sync_subscription,
                                       sync_all_subscriptions, _utc_naive,
                                       SYNC_ERROR_CODES)

router = APIRouter(prefix="/api/subscriptions", tags=["订阅管理"])

SUB_COLORS = ["#6c5ce7", "#00b894", "#e17055", "#0984e3", "#fdcb6e", "#e84393", "#00cec9", "#d63031"]

# 规范颜色格式：仅接受 #RRGGBB（序列化时统一收敛为小写）
SUB_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _palette_color(sub_id: int) -> str:
    return SUB_COLORS[(sub_id - 1) % len(SUB_COLORS)]


def _safe_color(value: Optional[str], sub_id: int) -> str:
    """存量脏颜色收敛到调色板 fallback，绝不透传进前端样式"""
    if isinstance(value, str) and SUB_COLOR_RE.match(value):
        return value.lower()
    return _palette_color(sub_id)


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    """时间序列化：DB naive 当 UTC、aware 转 UTC，去微秒输出 YYYY-MM-DDTHH:MM:SSZ"""
    if value is None:
        return None
    dt = value.astimezone(timezone.utc) if value.tzinfo is not None else value
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_error_code(code: Optional[str]) -> Optional[str]:
    """错误码对外只允许固定五码：脏/未知码收敛为 internal_error，绝不原样返回"""
    if code is None:
        return None
    return code if code in SYNC_ERROR_CODES else "internal_error"


def _subscription_summary(sub: Subscription) -> dict:
    """列表/详情共用安全摘要：固定 12 字段，绝不包含 url/created_at/server/config"""
    return {
        "id": sub.id,
        "name": sub.name,
        "tag": sub.tag,
        "color": _safe_color(sub.color, sub.id),
        "enabled": sub.enabled,
        "auto_update": sub.auto_update,
        "update_interval": sub.update_interval,
        "last_attempt_at": _iso_utc(sub.last_attempt_at),
        "last_success_at": _iso_utc(sub.last_success_at),
        "next_update_at": _iso_utc(sub.next_update_at),
        "last_error_code": _safe_error_code(sub.last_error_code),
        "last_node_count": sub.last_node_count,
    }


class SubscriptionCreate(BaseModel):
    name: str
    url: str
    tag: Optional[str] = None
    color: Optional[str] = None
    enabled: bool = True
    auto_update: bool = True
    update_interval: int = Field(default=86400, ge=UPDATE_INTERVAL_MIN,
                                 le=UPDATE_INTERVAL_MAX)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        """null 表示清除（回退调色板）；非空必须是规范 #RRGGBB"""
        if value is None:
            return None
        if not SUB_COLOR_RE.match(value):
            raise ValueError("color 必须为 #RRGGBB 格式")
        return value.lower()


class SubscriptionUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    tag: Optional[str] = None
    color: Optional[str] = None
    enabled: Optional[bool] = None
    auto_update: Optional[bool] = None
    update_interval: Optional[int] = Field(default=None, ge=UPDATE_INTERVAL_MIN,
                                           le=UPDATE_INTERVAL_MAX)

    @field_validator("update_interval")
    @classmethod
    def _reject_explicit_none(cls, value: Optional[int]) -> Optional[int]:
        """显式 null 的间隔是非法输入（省略才表示不修改）"""
        if value is None:
            raise ValueError("update_interval 不可为空")
        return value

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        """null 表示清除（回退调色板）；非空必须是规范 #RRGGBB"""
        if value is None:
            return None
        if not SUB_COLOR_RE.match(value):
            raise ValueError("color 必须为 #RRGGBB 格式")
        return value.lower()


@router.get("")
async def list_subscriptions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).order_by(Subscription.id))
    return [_subscription_summary(s) for s in result.scalars().all()]


@router.get("/status-summary")
async def subscription_status_summary(db: AsyncSession = Depends(get_db)):
    """订阅同步状态聚合（exact keys：due/healthy/failed/never_synced）。

    - 只查询调度与状态列，不读/不返回 URL 或节点配置；
    - 禁用订阅不进入任何一项；
    - due = enabled+auto_update+next<=now（到期维度，可与其它状态重叠）；
    - healthy = enabled+last_success 非空+error 空；
      failed = enabled+error 非空；
      never_synced = enabled+last_success 空（与 failed 可同时成立）。
    """
    rows = await db.execute(
        select(Subscription.enabled, Subscription.auto_update,
               Subscription.next_update_at, Subscription.last_success_at,
               Subscription.last_error_code))
    now = _utc_naive()
    summary = {"due": 0, "healthy": 0, "failed": 0, "never_synced": 0}
    for enabled, auto_update, next_at, success_at, error_code in rows.all():
        if not enabled:
            continue
        if auto_update and next_at is not None and next_at <= now:
            summary["due"] += 1
        if error_code:
            summary["failed"] += 1
        if success_at is None:
            summary["never_synced"] += 1
        elif not error_code:
            summary["healthy"] += 1
    return summary


@router.get("/{sub_id}")
async def get_subscription(sub_id: int, db: AsyncSession = Depends(get_db)):
    """已认证订阅详情：summary + url（url 仅供管理编辑回填；列表不携带）"""
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")
    data = _subscription_summary(sub)
    data["url"] = sub.url
    return data


@router.post("")
async def create_subscription(data: SubscriptionCreate, db: AsyncSession = Depends(get_db)):
    """创建订阅：enabled+auto 立即排期（next=now），否则不参与调度（NULL）"""
    now = _utc_naive()
    sub = Subscription(
        name=data.name,
        url=data.url,
        tag=data.tag,
        color=data.color,
        enabled=data.enabled,
        auto_update=data.auto_update,
        update_interval=data.update_interval,
        next_update_at=now if (data.enabled and data.auto_update) else None,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"id": sub.id, "name": sub.name}


@router.put("/{sub_id}")
async def update_subscription(sub_id: int, data: SubscriptionUpdate, db: AsyncSession = Depends(get_db)):
    """更新订阅：仅在调度相关值（enabled/auto/interval）实际变化时重算 next。

    - 最终 disabled 或 auto off → NULL；
    - 从 disabled/auto-off 开启 → now（立即到期）；
    - 保持 enabled+auto 且 interval 改变 → now + 新 interval；
    - 仅改 name/url/tag/color 等不动 next（全字段提交但值未变亦然）。
    """
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "订阅不存在")

    was_scheduled = bool(sub.enabled and sub.auto_update)
    old_interval = sub.update_interval
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(sub, key, value)

    if not (sub.enabled and sub.auto_update):
        sub.next_update_at = None
    elif not was_scheduled:
        sub.next_update_at = _utc_naive()
    elif sub.update_interval != old_interval:
        sub.next_update_at = _utc_naive() + timedelta(seconds=sub.update_interval)
    elif sub.next_update_at is None:
        # 防御：本应排期的订阅缺 next（历史数据）→ 补 now，绝不静默永不调度
        sub.next_update_at = _utc_naive()
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
