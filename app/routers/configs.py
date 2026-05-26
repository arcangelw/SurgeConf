"""配置方案与生成 API"""

import os
import socket
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import ConfigProfile, RuleSource, CustomRule, HostMapping
from app.services.generator import generate_surge_config
from app.default_config import HOST_DNS_PRESETS

router = APIRouter(prefix="/api/configs", tags=["配置管理"])


class ConfigProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_default: bool = False
    locale: Optional[str] = "zh"
    final_action: Optional[str] = "手动选择"
    general: Optional[dict] = None
    dns: Optional[dict] = None
    url_rewrites: Optional[list] = None
    header_rewrites: Optional[list] = None
    mitm: Optional[dict] = None


class ConfigProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_default: Optional[bool] = None
    locale: Optional[str] = None
    final_action: Optional[str] = None
    general: Optional[dict] = None
    dns: Optional[dict] = None
    url_rewrites: Optional[list] = None
    header_rewrites: Optional[list] = None
    mitm: Optional[dict] = None


class RuleSourceCreate(BaseModel):
    name: str
    url: str
    action: str
    rule_type: str = "RULE-SET"
    category: Optional[str] = None
    sort_order: int = 0
    enabled: bool = True


class RuleSourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    action: Optional[str] = None
    rule_type: Optional[str] = None
    category: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None


# ===== 配置方案 =====

@router.get("/profiles")
async def list_profiles(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigProfile).order_by(ConfigProfile.id))
    profiles = result.scalars().all()
    resp = []
    for p in profiles:
        resp.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "is_default": p.is_default,
            "locale": p.locale,
            "final_action": p.final_action,
            "general": p.general,
            "dns": p.dns,
            "url_rewrites": p.url_rewrites,
            "header_rewrites": p.header_rewrites,
            "mitm": p.mitm,
        })
    return resp


@router.post("/profiles")
async def create_profile(data: ConfigProfileCreate, db: AsyncSession = Depends(get_db)):
    profile = ConfigProfile(
        name=data.name,
        description=data.description,
        is_default=data.is_default,
        locale=data.locale or "zh",
        general=data.general or {},
        dns=data.dns or {},
        url_rewrites=data.url_rewrites or [],
        header_rewrites=data.header_rewrites or [],
        mitm=data.mitm or {},
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return {"id": profile.id, "name": profile.name}


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, data: ConfigProfileUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "配置方案不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(profile, key, value)
    await db.commit()
    return {"id": profile.id, "name": profile.name}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "配置方案不存在")
    await db.delete(profile)
    await db.commit()
    return {"deleted": profile_id}


# ===== 规则源 =====

@router.get("/rules")
async def list_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleSource).order_by(RuleSource.sort_order))
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "url": r.url,
            "action": r.action,
            "rule_type": r.rule_type,
            "category": r.category,
            "sort_order": r.sort_order,
            "enabled": r.enabled,
        }
        for r in rules
    ]


@router.post("/rules")
async def create_rule(data: RuleSourceCreate, db: AsyncSession = Depends(get_db)):
    rule = RuleSource(
        name=data.name,
        url=data.url,
        action=data.action,
        rule_type=data.rule_type,
        category=data.category,
        sort_order=data.sort_order,
        enabled=data.enabled,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": rule.id, "name": rule.name}


@router.put("/rules/reorder")
async def reorder_rules(data: list[dict], db: AsyncSession = Depends(get_db)):
    """批量更新规则排序 [{id: 1, sort_order: 0}, ...]"""
    for item in data:
        result = await db.execute(select(RuleSource).where(RuleSource.id == item["id"]))
        rule = result.scalar_one_or_none()
        if rule:
            rule.sort_order = item["sort_order"]
    await db.commit()
    return {"reordered": len(data)}


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: int, data: RuleSourceUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleSource).where(RuleSource.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "规则不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(rule, key, value)
    await db.commit()
    return {"id": rule.id, "name": rule.name}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleSource).where(RuleSource.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "规则不存在")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id}


# ===== 配置生成 =====

@router.get("/generate")
async def generate_config(
    profile_id: Optional[int] = None,
    filename: Optional[str] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    content = await generate_surge_config(db, profile_id, locale)

    # 保存到 data 目录（过滤路径遍历）
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(output_dir, exist_ok=True)
    save_name = (filename or "Surge.conf").replace("/", "").replace("\\", "").replace("..", "")
    output_path = os.path.join(output_dir, save_name)
    with open(output_path, "w") as f:
        f.write(content)

    # 如果指定了 filename，返回带 Content-Disposition 的下载响应
    if filename:
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return PlainTextResponse(content, media_type="text/plain")


@router.get("/subscribe")
async def subscribe_config(
    profile_id: Optional[int] = None,
    token: Optional[str] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """供 Surge 通过 URL 订阅的端点。Surge 中填入：
    http://<ip>:61830/api/configs/subscribe?profile_id=1
    """
    content = await generate_surge_config(db, profile_id, locale)
    # Surge 订阅需要设置合适的 Content-Type 和更新间隔提示
    return PlainTextResponse(
        content,
        media_type="text/plain",
        headers={
            "Content-Disposition": 'attachment; filename="Surge.conf"',
            "Profile-Update-Interval": "24",  # 小时
            "Subscription-Userinfo": "managed-by=SurgeConf",
        },
    )


@router.get("/preview")
async def preview_config(
    profile_id: Optional[int] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    content = await generate_surge_config(db, profile_id, locale)
    return {"content": content, "size": len(content)}


@router.get("/server-ips")
async def server_ips():
    """返回本机所有可用 IP 地址，供订阅链接使用"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for ip in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(ip[4][0])
    except Exception:
        pass
    ips.discard("127.0.0.1")
    result = sorted(ips)
    return {"ips": result}


# ===== 自定义规则 =====

class CustomRuleCreate(BaseModel):
    profile_id: Optional[int] = None
    rule_text: str
    position: str = "general"
    sort_order: int = 0
    enabled: bool = True
    comment: Optional[str] = None


class CustomRuleUpdate(BaseModel):
    rule_text: Optional[str] = None
    position: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None
    comment: Optional[str] = None


@router.get("/custom-rules")
async def list_custom_rules(
    profile_id: Optional[int] = None,
    position: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(CustomRule).order_by(CustomRule.position, CustomRule.sort_order)
    if profile_id is not None:
        query = query.where(CustomRule.profile_id == profile_id)
    else:
        query = query.where(CustomRule.profile_id.is_(None))
    if position:
        query = query.where(CustomRule.position == position)
    result = await db.execute(query)
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "profile_id": r.profile_id,
            "rule_text": r.rule_text,
            "position": r.position,
            "sort_order": r.sort_order,
            "enabled": r.enabled,
            "comment": r.comment,
        }
        for r in rules
    ]


@router.post("/custom-rules")
async def create_custom_rule(data: CustomRuleCreate, db: AsyncSession = Depends(get_db)):
    rule = CustomRule(**data.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": rule.id}


@router.put("/custom-rules/reorder")
async def reorder_custom_rules(data: list[dict], db: AsyncSession = Depends(get_db)):
    for item in data:
        result = await db.execute(select(CustomRule).where(CustomRule.id == item["id"]))
        rule = result.scalar_one_or_none()
        if rule:
            rule.sort_order = item["sort_order"]
    await db.commit()
    return {"reordered": len(data)}


@router.put("/custom-rules/{rule_id}")
async def update_custom_rule(rule_id: int, data: CustomRuleUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CustomRule).where(CustomRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "规则不存在")
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(rule, key, value)
    await db.commit()
    return {"id": rule.id}


@router.delete("/custom-rules/{rule_id}")
async def delete_custom_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CustomRule).where(CustomRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "规则不存在")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id}


# ===== Host 映射 =====

class HostMappingCreate(BaseModel):
    profile_id: Optional[int] = None
    domain: str
    target: str
    sort_order: int = 0
    enabled: bool = True


class HostMappingUpdate(BaseModel):
    domain: Optional[str] = None
    target: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None


@router.get("/host-mappings")
async def list_host_mappings(
    profile_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(HostMapping).order_by(HostMapping.sort_order)
    if profile_id is not None:
        query = query.where(HostMapping.profile_id == profile_id)
    else:
        query = query.where(HostMapping.profile_id.is_(None))
    result = await db.execute(query)
    mappings = result.scalars().all()
    return [
        {
            "id": m.id,
            "profile_id": m.profile_id,
            "domain": m.domain,
            "target": m.target,
            "sort_order": m.sort_order,
            "enabled": m.enabled,
        }
        for m in mappings
    ]


@router.get("/host-presets")
async def host_dns_presets():
    return HOST_DNS_PRESETS


@router.post("/host-mappings")
async def create_host_mapping(data: HostMappingCreate, db: AsyncSession = Depends(get_db)):
    mapping = HostMapping(**data.model_dump())
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)
    return {"id": mapping.id}


@router.put("/host-mappings/reorder")
async def reorder_host_mappings(data: list[dict], db: AsyncSession = Depends(get_db)):
    for item in data:
        result = await db.execute(select(HostMapping).where(HostMapping.id == item["id"]))
        mapping = result.scalar_one_or_none()
        if mapping:
            mapping.sort_order = item["sort_order"]
    await db.commit()
    return {"reordered": len(data)}


@router.put("/host-mappings/{mapping_id}")
async def update_host_mapping(mapping_id: int, data: HostMappingUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(HostMapping).where(HostMapping.id == mapping_id))
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise HTTPException(404, "映射不存在")
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(mapping, key, value)
    await db.commit()
    return {"id": mapping.id}


@router.delete("/host-mappings/{mapping_id}")
async def delete_host_mapping(mapping_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(HostMapping).where(HostMapping.id == mapping_id))
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise HTTPException(404, "映射不存在")
    await db.delete(mapping)
    await db.commit()
    return {"deleted": mapping_id}
