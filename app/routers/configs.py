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
from sqlalchemy import func, select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import ConfigProfile, RuleSource, CustomRule, HostMapping, UdpException
from app.profile_targets import (ProfileTarget, UdpSecurityMode,
                                 default_target_for_role, default_udp_mode,
                                 validate_target_mode)
from app.udp_policy import udp_match_types_allowed_for
from app.services.profile_resolution import (DefaultProfileMissing, ProfileNotFound,
                                             resolve_profile)
from app.runtime_paths import (harden_private_file, refresh_runtime_permissions,
                               resolve_runtime_paths)
from app.security import (issue_profile_token, resolve_installation_role,
                          verify_profile_token)
from app.services.backups import unique_path
from app.services.config_validation import (InvalidConfiguration, is_final_rule,
                                            validate_rule_order)
from app.services.generator import generate_surge_config
from app.default_config import HOST_DNS_PRESETS, DEFAULT_RULE_SOURCES

router = APIRouter(prefix="/api/configs", tags=["配置管理"])

# MITM 秘密字段：详情仅返回 configured 状态，更新为覆盖式（空输入=保留现值）
MITM_SECRET_KEYS = ("ca-p12", "ca-passphrase")


class ConfigProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_default: bool = False
    target: Optional[ProfileTarget] = None
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
    ip_rules: bool = False
    rule_type: str = "RULE-SET"
    category: Optional[str] = None
    sort_order: int = 0
    enabled: bool = True


class RuleSourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    action: Optional[str] = None
    ip_rules: Optional[bool] = None
    rule_type: Optional[str] = None
    category: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None

def _reject_multiline_rule_text(rule_text: str) -> None:
    """CustomRule.rule_text 语义是一行一条规则：含任何 CR/LF 一律 422。
    第二物理行可能携带 FINAL / PROCESS-NAME 等借道绕过结构化检测，
    保存端与生成端（绕库存量）同口径 fail closed。错误不回显规则内容。"""
    if "\r" in rule_text or "\n" in rule_text:
        raise HTTPException(
            422, "rule_text 必须是单行规则：不允许换行，请拆分为多条规则")


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
            "udp_security_mode": p.udp_security_mode,
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
    # 显式 null 不等于省略：区分「未提供」（用实例角色默认目标）与「显式非法值」
    if data.target is None and "target" in data.model_fields_set:
        raise HTTPException(422, "target 不允许显式 null：省略该字段以使用实例角色默认目标")
    if data.target is None:
        # 未显式指定 target：按实例角色单向映射取默认（显式 target 优先；
        # 角色变化只影响此后创建，已有档案不受影响）。角色经请求同一
        # session 读取（resolve_installation_role），不另开连接
        role = await resolve_installation_role(db)
        data.target = default_target_for_role(role)
    # 显式 null 不等于省略：区分「未提供」（用目标默认模式）与「显式非法值」
    if data.udp_security_mode is None and "udp_security_mode" in data.model_fields_set:
        raise HTTPException(422, "udp_security_mode 不允许显式 null：省略该字段以使用目标默认模式")
    # final_action 显式 null 不等于省略：未提供用模型默认（手动选择）
    if data.final_action is None and "final_action" in data.model_fields_set:
        raise HTTPException(422, "final_action 不允许显式 null：省略该字段以使用默认兜底策略")
    final_action = data.final_action or "手动选择"
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
        final_action=final_action,
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

    # 改变 target 前查询 UDP 例外兼容性：存在新目标不允许的例外类型时 422，
    # detail 只给数量（不回显 type/value/name），事务不提交、不自动删除；
    # target 不变（或未提供）不额外阻止
    if target_provided and new_target is not ProfileTarget(profile.target):
        allowed_values = {t.value for t in udp_match_types_allowed_for(new_target)}
        incompatible = select(func.count()).select_from(UdpException).where(
            UdpException.profile_id == profile_id)
        if allowed_values:
            incompatible = incompatible.where(UdpException.match_type.not_in(allowed_values))
        count = (await db.execute(incompatible)).scalar_one()
        if count:
            raise HTTPException(
                422, f"目标切换与现有 UDP 例外不兼容：请先删除或调整 {count} 条例外")

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
    """级联删除：custom_rules / host_mappings 无 DB 外键（0001 基线），
    单事务内手工删除；UDP 例外由 FK ON DELETE CASCADE 数据库级清除"""
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
            "ip_rules": bool(r.ip_rules),
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
        ip_rules=data.ip_rules,
        sort_order=data.sort_order,
        enabled=data.enabled,
    )
    db.add(rule)
    # 新增后 flush（新行进入本事务可见集），在 commit 前构造完整 enabled
    # 候选并校验顺序不变量；失败 rollback（数据库不变）并返回与
    # reorder/update 端点同一 422 结构
    try:
        await db.flush()
        await _validate_rule_order_or_422(db)
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    await db.refresh(rule)
    return {"id": rule.id, "name": rule.name}


async def _validate_rule_order_or_422(db: AsyncSession) -> None:
    """提交前按数据库当前完整 enabled 候选序（含请求未触及项）校验顺序不变量；
    违例时抛 422（调用方负责 rollback 整个事务）。"""
    candidates = (await db.execute(
        select(RuleSource).where(RuleSource.enabled == True))).scalars().all()
    try:
        validate_rule_order(candidates)
    except InvalidConfiguration as exc:
        raise HTTPException(422, _invalid_configuration_detail(exc)) from exc


class RuleSourceReorderItem(BaseModel):
    """reorder 条目强类型：id 与 sort_order 均为必填 int，显式 null 直接 422"""
    id: int
    sort_order: int


@router.put("/rules/reorder")
async def reorder_rules(data: list[RuleSourceReorderItem], db: AsyncSession = Depends(get_db)):
    """批量更新规则排序 [{id: 1, sort_order: 0}, ...]

    既有语义保留：未知 id 静默忽略（按请求数量计数）、重复 id 后写覆盖、
    遗漏项不动；sort_order 显式 null / 非整数由 Pydantic 422 拒绝。
    """
    for item in data:
        result = await db.execute(select(RuleSource).where(RuleSource.id == item.id))
        rule = result.scalar_one_or_none()
        if rule:
            rule.sort_order = item.sort_order
    # 破坏任一顺序不变量：422 并回滚整个事务（含本次请求未触及的记录）
    try:
        await _validate_rule_order_or_422(db)
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    return {"reordered": len(data)}


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: int, data: RuleSourceUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleSource).where(RuleSource.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "规则不存在")

    update_data = data.model_dump(exclude_unset=True)
    # 区分「未提供」与「显式 null」：非空列显式 null 直接 422（category 为
    # 可选列，显式 null 保留其清空语义）
    _NON_NULLABLE_FIELDS = {"name", "url", "action", "rule_type",
                            "sort_order", "enabled", "ip_rules"}
    explicit_null = sorted(key for key, value in update_data.items()
                           if value is None and key in _NON_NULLABLE_FIELDS)
    if explicit_null:
        raise HTTPException(422, f"字段不允许显式 null: {', '.join(explicit_null)}")
    for key, value in update_data.items():
        setattr(rule, key, value)
    # sort_order / enabled / 身份字段变化都可能破坏顺序不变量：提交前校验
    try:
        await _validate_rule_order_or_422(db)
    except HTTPException:
        await db.rollback()
        raise
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


def _invalid_configuration_detail(exc: InvalidConfiguration) -> dict:
    """422 响应体：完整错误列表一次性返回，便于前端逐条呈现"""
    return {"code": "invalid_configuration", "errors": exc.errors}


async def _render_profile_config(db: AsyncSession, profile_id: Optional[int], locale: Optional[str]) -> str:
    """生成入口统一错误语义：显式 id 不存在 404；省略且无默认档案 422；
    引用无效聚合 422（不返回部分配置）"""
    try:
        return await generate_surge_config(db, profile_id, locale)
    except ProfileNotFound as exc:
        raise HTTPException(404, "配置方案不存在") from exc
    except DefaultProfileMissing as exc:
        raise HTTPException(422, "未指定 profile_id 且不存在默认配置方案") from exc
    except InvalidConfiguration as exc:
        raise HTTPException(422, _invalid_configuration_detail(exc)) from exc

@router.get("/generate")
async def generate_config(
    profile_id: Optional[int] = None,
    filename: Optional[str] = None,
    locale: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """内存生成配置：不写任何服务器文件（服务端导出走 POST /{profile_id}/exports）"""
    content = await _render_profile_config(db, profile_id, locale)
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
    content = await _render_profile_config(db, profile_id, None)
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
    try:
        content = await generate_surge_config(db, profile_id, locale)
    except (ProfileNotFound, DefaultProfileMissing):
        # token 验证通过后、生成阶段档案恰好被删（确定性竞态）：
        # 与一切 token 失败同口径 404，不泄露档案存在性差异
        raise HTTPException(404, "Not Found")
    except InvalidConfiguration as exc:
        # token 已验证：配置本身无效可如实 422（完整错误列表），
        # 该分支位于 token 校验之后，无效 token 永远到不了这里
        raise HTTPException(422, _invalid_configuration_detail(exc)) from exc
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
    content = await _render_profile_config(db, profile_id, locale)
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
    # FINAL 为生成器专属终结规则（按档案 final_action 追加且恰一条），
    # pre/general 任何自定义 FINAL（内置 action 与 policy action 均同）
    # 在保存端拒绝；生成校验端对绕过写入的存量同样 fail closed
    _reject_multiline_rule_text(data.rule_text)
    if is_final_rule(data.rule_text):
        raise HTTPException(
            422, "FINAL 是生成器专属终结规则，不允许保存为自定义规则；兜底策略请在档案设置中修改 final_action")
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
    # 更新端同口径拦截：把 rule_text 改成 FINAL 拒绝（不改 rule_text 的
    # 更新如排序/启停不受影响）
    if update_data.get("rule_text") is not None:
        _reject_multiline_rule_text(update_data["rule_text"])
    if update_data.get("rule_text") is not None and is_final_rule(update_data["rule_text"]):
        raise HTTPException(
            422, "FINAL 是生成器专属终结规则，不允许保存为自定义规则；兜底策略请在档案设置中修改 final_action")
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
