"""节点管理 API - 含 IP 归属地、手动钉选服务分组"""

import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import ProxyNode, RegionGroup, Subscription
from app.services.subscription import auto_detect_region

router = APIRouter(prefix="/api/nodes", tags=["节点管理"])


class NodeRegionUpdate(BaseModel):
    region_manual: Optional[str] = None
    enabled: Optional[bool] = None
    pinned_services: Optional[list[str]] = None


class BatchRegionUpdate(BaseModel):
    node_ids: list[int]
    region_manual: str


class BatchPinUpdate(BaseModel):
    node_ids: list[int]
    service_name: str
    pinned: bool = True


async def _get_sub_map(db: AsyncSession) -> dict:
    result = await db.execute(select(Subscription))
    subs = result.scalars().all()
    return {
        s.id: {"name": s.name, "tag": s.tag, "color": s.color or "#6c5ce7"}
        for s in subs
    }


def _node_to_dict(n: ProxyNode, sub_map: dict) -> dict:
    sub_info = sub_map.get(n.subscription_id, {})
    return {
        "id": n.id,
        "name": n.name,
        "subscription_id": n.subscription_id,
        "subscription_name": sub_info.get("name"),
        "subscription_tag": sub_info.get("tag"),
        "subscription_color": sub_info.get("color"),
        "node_type": n.node_type,
        "server": n.server,
        "port": n.port,
        "config": n.config,
        "region_auto": n.region_auto,
        "region_manual": n.region_manual,
        "pinned_services": n.pinned_services or [],
        "enabled": n.enabled,
    }


# ===== 固定路径路由（必须在 /{node_id} 前注册） =====

@router.get("")
async def list_nodes(
    subscription_id: Optional[int] = None,
    region: Optional[str] = None,
    unassigned: Optional[bool] = None,
    pinned_to: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    sub_map = await _get_sub_map(db)
    query = select(ProxyNode).where(ProxyNode.enabled == True)
    if subscription_id:
        query = query.where(ProxyNode.subscription_id == subscription_id)
    if region:
        query = query.where(
            (ProxyNode.region_manual == region) | (ProxyNode.region_auto == region)
        )
    if unassigned:
        query = query.where(ProxyNode.region_manual == None, ProxyNode.region_auto == None)
    if pinned_to:
        query = query.where(ProxyNode.pinned_services.contains(f'["{pinned_to}"]'))

    query = query.order_by(ProxyNode.name)
    result = await db.execute(query)
    nodes = result.scalars().all()
    return [_node_to_dict(n, sub_map) for n in nodes]


@router.get("/by-region")
async def nodes_by_region(db: AsyncSession = Depends(get_db)):
    sub_map = await _get_sub_map(db)

    regions_result = await db.execute(
        select(RegionGroup).where(RegionGroup.enabled == True).order_by(RegionGroup.sort_order)
    )
    region_groups = regions_result.scalars().all()

    nodes_result = await db.execute(select(ProxyNode).where(ProxyNode.enabled == True))
    nodes = nodes_result.scalars().all()

    groups = {}
    for rg in region_groups:
        groups[rg.name] = []

    unassigned = []
    for node in nodes:
        region = node.region_manual or node.region_auto
        nd = _node_to_dict(node, sub_map)
        if region and region in groups:
            groups[region].append(nd)
        else:
            unassigned.append(nd)

    return {"groups": groups, "unassigned": unassigned}


@router.put("/batch/region")
async def batch_update_region(data: BatchRegionUpdate, db: AsyncSession = Depends(get_db)):
    for node_id in data.node_ids:
        result = await db.execute(select(ProxyNode).where(ProxyNode.id == node_id))
        node = result.scalar_one_or_none()
        if node:
            node.region_manual = data.region_manual
    await db.commit()
    return {"updated": len(data.node_ids)}


@router.put("/batch/pin")
async def batch_pin_to_service(data: BatchPinUpdate, db: AsyncSession = Depends(get_db)):
    for node_id in data.node_ids:
        result = await db.execute(select(ProxyNode).where(ProxyNode.id == node_id))
        node = result.scalar_one_or_none()
        if node:
            services = list(node.pinned_services or [])
            if data.pinned and data.service_name not in services:
                services.append(data.service_name)
            elif not data.pinned and data.service_name in services:
                services.remove(data.service_name)
            node.pinned_services = services
    await db.commit()
    return {"updated": len(data.node_ids), "pinned": data.pinned, "service": data.service_name}


@router.post("/auto-assign")
async def auto_assign_regions(db: AsyncSession = Depends(get_db)):
    regions_result = await db.execute(
        select(RegionGroup).where(RegionGroup.enabled == True).order_by(RegionGroup.sort_order)
    )
    region_groups = [{"name": rg.name, "filter_regex": rg.filter_regex} for rg in regions_result.scalars().all()]

    nodes_result = await db.execute(select(ProxyNode).where(ProxyNode.enabled == True))
    nodes = nodes_result.scalars().all()

    assigned = 0
    for node in nodes:
        if node.region_manual:
            continue
        detected = auto_detect_region(node.name, region_groups)
        if detected:
            node.region_auto = detected
            assigned += 1

    await db.commit()
    return {"total": len(nodes), "assigned": assigned, "regions": [rg["name"] for rg in region_groups]}


# ===== 动态路径路由（放在最后） =====

@router.get("/{node_id}")
async def get_node_detail(node_id: int, db: AsyncSession = Depends(get_db)):
    sub_map = await _get_sub_map(db)
    result = await db.execute(select(ProxyNode).where(ProxyNode.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "节点不存在")
    return _node_to_dict(node, sub_map)


@router.put("/{node_id}")
async def update_node(node_id: int, data: NodeRegionUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ProxyNode).where(ProxyNode.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "节点不存在")

    if data.region_manual is not None:
        node.region_manual = data.region_manual
    if data.enabled is not None:
        node.enabled = data.enabled
    if data.pinned_services is not None:
        node.pinned_services = data.pinned_services
    await db.commit()
    return {
        "id": node.id,
        "region_manual": node.region_manual,
        "pinned_services": node.pinned_services,
        "enabled": node.enabled,
    }
