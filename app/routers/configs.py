"""配置方案与生成 API"""

import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import ConfigProfile, RuleSource, CustomRule, HostMapping
from app.profile_targets import (ProfileTarget, UdpSecurityMode,
                                 default_udp_mode, validate_target_mode)
from app.services.profile_resolution import ProfileNotFound, resolve_profile
from app.runtime_paths import (harden_private_file, refresh_runtime_permissions,
                               resolve_runtime_paths)
from app.security import issue_profile_token, verify_profile_token
from app.services.backups import unique_path
from app.services.generator import generate_surge_config
from app.default_config import HOST_DNS_PRESETS, DEFAULT_RULE_SOURCES

router = APIRouter(prefix="/api/configs", tags=["配置管理"])

# MITM 秘密字段：详情仅返回 configured 状态，更新为覆盖式（空输入=保留现值）
MITM_SECRET_KEYS = ("ca-p12", "ca-passphrase")


class ConfigProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_default: bool = False
    target: ProfileTarget = ProfileTarget.MAC_WORKSTATION
    udp_security_mode: Optional[UdpSecurityMode] = None
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
    target: Optional[ProfileTarget] = None
    udp_security_mode: Optional[UdpSecurityMode] = None
    locale: Optional[str] = None
    final_action: Optional[str] = None
    general: Optional[dict] = None
    dns: Optional[dict] = None
    url_rewrites: Optional[list] = None
    header_rewrites: Optional[list] = None
    mitm: Optional[dict] = None
    mitm_reset: Optional[bool] = None
    mitm_clear: Optional[list[str]] = None


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

async def _require_profile(db: AsyncSession, profile_id: int) -> ConfigProfile:
    """显式 id 解析：不存在直接 404，绝不回退默认档案（resolve_profile 唯一语义）"""
    try:
        return await resolve_profile(db, profile_id)
    except ProfileNotFound as exc:
        raise HTTPException(404, "配置方案不存在") from exc


 # ===== 配置方案 =====

@router.get("/profiles")
async def list_profiles(db: AsyncSession = Depends(get_db)):
    """最小披露：仅列表展示所需字段；MITM/General 走已认证单档案详情"""
    result = await db.execute(select(ConfigProfile).order_by(ConfigProfile.id))
    profiles = result.scalars().all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "is_default": p.is_default,
            "locale": p.locale,
            "target": p.target,
            "token_configured": bool(p.subscription_token_hash),
        }
        for p in profiles
    ]


def _mitm_public_view(mitm: dict | None) -> dict:
    """MITM 秘密字段仅返回 configured 状态，绝不回显明文"""
    view = dict(mitm or {})
    for key in MITM_SECRET_KEYS:
        view[key] = {"configured": bool(view.get(key))}
    return view


def _profile_detail(p: ConfigProfile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "is_default": p.is_default,
        "locale": p.locale,
        "target": p.target,
        "udp_security_mode": p.udp_security_mode,
        "final_action": p.final_action,
        "general": p.general,
        "dns": p.dns,
        "url_rewrites": p.url_rewrites,
        "header_rewrites": p.header_rewrites,
        "mitm": _mitm_public_view(p.mitm),
        "token_configured": bool(p.subscription_token_hash),
    }


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    """已认证单档案详情：UI 编辑用完整数据；MITM 秘密仅 configured 状态"""
    profile = await _require_profile(db, profile_id)
    return _profile_detail(profile)


@router.post("/profiles")
async def create_profile(data: ConfigProfileCreate, db: AsyncSession = Depends(get_db)):
    # 显式 null 不等于省略：区分「未提供」（用目标默认模式）与「显式非法值」
    if data.udp_security_mode is None and "udp_security_mode" in data.model_fields_set:
        raise HTTPException(422, "udp_security_mode 不允许显式 null：省略该字段以使用目标默认模式")
    mode = data.udp_security_mode or default_udp_mode(data.target)
    try:
        validate_target_mode(data.target, mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    profile = ConfigProfile(
        name=data.name,
        description=data.description,
        is_default=data.is_default,
        target=data.target.value,
        udp_security_mode=mode.value,
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
    profile = await _require_profile(db, profile_id)

    # 目标/模式：显式提供才算（区分未提供与显式值）；组合校验先于任何
    # 修改，失败 422 且事务不提交。只换目标未给模式 → 重置新目标默认模式。
    fields_set = data.model_fields_set
    target_provided = "target" in fields_set
    mode_provided = "udp_security_mode" in fields_set
    if target_provided or mode_provided:
        new_target = data.target if target_provided else ProfileTarget(profile.target)
        new_mode = data.udp_security_mode if mode_provided else default_udp_mode(new_target)
        try:
            validate_target_mode(new_target, new_mode)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    update_data = data.model_dump(exclude_unset=True)
    update_data.pop("target", None)
    update_data.pop("udp_security_mode", None)
    mitm_reset = update_data.pop("mitm_reset", None)
    mitm_clear = update_data.pop("mitm_clear", None)
    mitm_incoming = update_data.pop("mitm", None)

    if mitm_reset:
        # 显式整体重置（独立动作）：清空全部 MITM 覆盖，回到生成器默认值
        profile.mitm = {}
    else:
        current = dict(profile.mitm or {})
        if mitm_clear:
            # 显式清除指定秘密键（独立字段）；清除不存在的键是无害幂等操作
            for key in mitm_clear:
                current.pop(key, None)
        if mitm_incoming is not None:
            for key, value in mitm_incoming.items():
                if key in MITM_SECRET_KEYS and not (isinstance(value, str) and value):
                    # 秘密键空输入（空串/null/configured 回显对象）= 保留现值；
                    # 只接受非空字符串作为新值
                    continue
                current[key] = value
        profile.mitm = current

    for key, value in update_data.items():
        setattr(profile, key, value)
    if target_provided or mode_provided:
        profile.target = new_target.value
        profile.udp_security_mode = new_mode.value
    await db.commit()
    return {"id": profile.id, "name": profile.name}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    """级联删除：单事务内删除该档案的定制规则、Host 映射与档案本体"""
    profile = await _require_profile(db, profile_id)
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(CustomRule).where(CustomRule.profile_id == profile_id))
    await db.execute(sa_delete(HostMapping).where(HostMapping.profile_id == profile_id))
    await db.delete(profile)
    await db.commit()
    return {"deleted": profile_id}


@router.put("/profiles/{profile_id}/set-default")
async def set_default_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    """设为默认：同事务内先清旧默认再置新默认，保证全局恰一条 is_default"""
    profile = await _require_profile(db, profile_id)
    from sqlalchemy import update as sa_update
    await db.execute(sa_update(ConfigProfile).where(ConfigProfile.is_default == True).values(is_default=False))
    profile.is_default = True
    await db.commit()
    return {"ok": True}


@router.post("/profiles/{profile_id}/token")
async def issue_profile_subscription_token(profile_id: int, db: AsyncSession = Depends(get_db)):
    """生成（或轮换）该档案的只读订阅 Token。

    管理会话 + CSRF 边界内；明文只在本次响应出现一次，库中仅存 SHA-256
    哈希。再次调用即轮换，旧订阅 URL 立即失效。
    """
    profile = await _require_profile(db, profile_id)
    plain, token_hash = issue_profile_token()
    profile.subscription_token_hash = token_hash
    await db.commit()
    return {"profile_id": profile.id, "token": plain}


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

@router.get("/rules/default-order")
async def get_rules_default_order():
    """内置规则源的默认排序，供前端「恢复默认排序」使用"""
    return [{"name": rs["name"], "sort_order": rs["sort_order"]} for rs in DEFAULT_RULE_SOURCES]


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

def _content_disposition(filename: str) -> str:
    """RFC 6266 下载头：剥除路径分量，ASCII 回退名 + RFC 5987 UTF-8 编码"""
    safe = Path(filename).name or "Surge.conf"
    try:
        ascii_name = safe.encode("ascii").decode("ascii")
    except UnicodeEncodeError:
        ascii_name = "Surge.conf"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe)}'


@router.get("/generate")
async def generate_config(
    profile_id: Optional[int] = None,
    filename: Optional[str] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """内存生成配置：不写任何服务器文件（服务端导出走 POST /{profile_id}/exports）"""
    content = await generate_surge_config(db, profile_id, locale)
    headers = {"Content-Disposition": _content_disposition(filename)} if filename else {}
    return PlainTextResponse(content, media_type="text/plain", headers=headers)


class ExportRequest(BaseModel):
    """显式服务端导出载荷：必须携带确认位，可自定义安全文件名"""
    confirm: bool = False
    filename: Optional[str] = None


def _export_filename(raw: Optional[str], profile_id: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw or "").name).strip("._")
    if cleaned.lower().endswith(".conf"):
        cleaned = cleaned[:-5]
    if cleaned:
        return cleaned[:80]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"surge_p{profile_id}_{timestamp}"


@router.post("/{profile_id}/exports")
async def export_config(profile_id: int, data: ExportRequest,
                        db: AsyncSession = Depends(get_db)):
    """唯一的服务端导出路径：只写 RuntimePaths.exports，0600 原子发布"""
    if not data.confirm:
        raise HTTPException(
            status_code=400,
            detail='缺少确认参数：请求体需包含 {"confirm": true}',
        )
    await _require_profile(db, profile_id)
    content = await generate_surge_config(db, profile_id, None)
    paths = resolve_runtime_paths()
    refresh_runtime_permissions(paths)
    target = unique_path(paths.exports, _export_filename(data.filename, profile_id), ".conf")
    tmp = paths.exports / f".{target.name}.tmp-{os.getpid()}-{token_hex(4)}"
    try:
        tmp.write_bytes(content.encode("utf-8"))
        harden_private_file(tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"name": target.name, "size": target.stat().st_size}


@router.get("/profiles/{profile_id}/subscribe")
async def subscribe_config(
    profile_id: int,
    token: Optional[str] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """供 Surge 通过 URL 订阅的只读端点（唯一免管理会话路径）。

    Surge 中填入：http://<ip>:<port>/api/configs/profiles/1/subscribe?token=<token>。
    令牌任何失败（缺失/格式错/不匹配/未配置）与档案不存在统一 404，
    不泄露档案是否存在。
    """
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if profile is None or not verify_profile_token(token, profile.subscription_token_hash):
        raise HTTPException(404, "Not Found")
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
    skip_proxy: bool = True


class HostMappingUpdate(BaseModel):
    domain: Optional[str] = None
    target: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None
    skip_proxy: Optional[bool] = None


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
            "skip_proxy": m.skip_proxy,
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
