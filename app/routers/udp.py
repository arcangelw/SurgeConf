"""UDP 例外管理 API：父档案约束的 CRUD 与脱敏响应。

- 子项查询同时约束 ``UdpException.id`` 与 ``UdpException.profile_id``，
  跨档案访问与子项缺失统一 404（不可枚举）；
- 成功响应仅含 ``match_value_masked``，任何响应/错误消息不回显完整匹配值
  （敏感值最小披露，与模型 repr、udp_policy 错误口径一致）；
- 写入前经父档案 target 的 ``normalize_udp_match`` 规范化；更新省略
  ``match_value`` 保留原值，任一改变用最终有效二元组重新校验；
- 非空字段显式 null 是 422 而非省略；唯一约束冲突稳定 409；
- 管理边界（local_only / lan_authenticated / CSRF）由 ``app.main`` 的
  统一中间件承担，本路由不重复认证逻辑。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ConfigProfile, UdpException
from app.services.profile_resolution import ProfileNotFound, resolve_profile
from app.udp_policy import UdpMatchType, normalize_udp_match

router = APIRouter(prefix="/api/configs", tags=["UDP 例外"])

# 响应字段白名单（锁定：无 hash/secret/完整值）
EXCEPTION_FIELDS = ("id", "profile_id", "match_type", "match_value_masked",
                    "display_name", "enabled", "sort_order")

# 更新里不允许显式 null 的非空列；display_name 可空，显式 null = 清空
_REQUIRED_UPDATE_FIELDS = ("match_type", "match_value", "enabled", "sort_order")

# 唯一约束 uq_udp_exceptions_profile_match 冲突的稳定消息（不回显值）
_DUPLICATE_MESSAGE = "已存在相同的 UDP 例外（同档案同类型同匹配值）"


class UdpExceptionCreate(BaseModel):
    match_type: UdpMatchType
    match_value: str = Field(min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=100)
    enabled: bool = True
    sort_order: int = 0


class UdpExceptionUpdate(BaseModel):
    match_type: Optional[UdpMatchType] = None
    match_value: Optional[str] = Field(default=None, min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=100)
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


async def _require_profile(db: AsyncSession, profile_id: int) -> ConfigProfile:
    """父档案显式解析：不存在 404（与 configs 路由同语义、同文案）"""
    try:
        return await resolve_profile(db, profile_id)
    except ProfileNotFound as exc:
        raise HTTPException(404, "配置方案不存在") from exc


async def _require_exception(db: AsyncSession, profile_id: int,
                             exception_id: int) -> UdpException:
    """子项同时约束 id + profile_id：不存在与跨档案统一 404（不可枚举）"""
    result = await db.execute(select(UdpException).where(
        UdpException.id == exception_id,
        UdpException.profile_id == profile_id))
    exception = result.scalar_one_or_none()
    if exception is None:
        raise HTTPException(404, "UDP 例外不存在")
    return exception


def _mask_match_value(match_type: str, value: str) -> str:
    """匹配值脱敏：固定形态掩码，不泄露长度与内容。

    - mac_address：仅保留首两组与末组（``02:00:**:**:**:01`` 形态）；
      非 6 组 2 位的标准存储形态（绕过 API 直插的存量）统一 ``***``
    - process：仅保留首尾各 2 字符；长度不足 5 时首尾即拼出完整值，
      掩码形同虚设，统一 ``***``
    """
    if match_type == UdpMatchType.MAC_ADDRESS.value:
        parts = value.split(":")
        if len(parts) != 6 or any(len(part) != 2 for part in parts):
            return "***"
        return f"{parts[0]}:{parts[1]}:**:**:**:{parts[5]}"
    if len(value) < 5:
        return "***"
    return f"{value[:2]}**{value[-2:]}"


def _exception_view(exception: UdpException) -> dict:
    """唯一响应序列化出口：白名单字段 + 掩码值（绝不含 match_value）"""
    return {
        "id": exception.id,
        "profile_id": exception.profile_id,
        "match_type": exception.match_type,
        "match_value_masked": _mask_match_value(
            exception.match_type, exception.match_value),
        "display_name": exception.display_name,
        "enabled": exception.enabled,
        "sort_order": exception.sort_order,
    }


@router.get("/profiles/{profile_id}/udp-exceptions")
async def list_udp_exceptions(profile_id: int,
                              db: AsyncSession = Depends(get_db)):
    """父档案的例外列表：(sort_order, id) 排序，disabled 也显示"""
    await _require_profile(db, profile_id)
    result = await db.execute(
        select(UdpException)
        .where(UdpException.profile_id == profile_id)
        .order_by(UdpException.sort_order, UdpException.id))
    return [_exception_view(e) for e in result.scalars().all()]


@router.post("/profiles/{profile_id}/udp-exceptions", status_code=201)
async def create_udp_exception(profile_id: int, data: UdpExceptionCreate,
                               db: AsyncSession = Depends(get_db)):
    profile = await _require_profile(db, profile_id)
    try:
        # 父档案 target 决定允许的例外类型并规范化持久化形式
        normalized = normalize_udp_match(
            profile.target, data.match_type, data.match_value)
    except ValueError as exc:
        # normalizer 错误消息为静态格式说明，不含原始值
        raise HTTPException(422, str(exc)) from exc
    exception = UdpException(
        profile_id=profile_id,
        match_type=data.match_type.value,
        match_value=normalized,
        display_name=data.display_name,
        enabled=data.enabled,
        sort_order=data.sort_order,
    )
    db.add(exception)
    try:
        await db.commit()
    except IntegrityError as exc:
        # 唯一约束冲突：rollback 后稳定 409，绝不回显输入/存储值；
        # 其他 DB 错误类型不在此捕获
        await db.rollback()
        raise HTTPException(409, _DUPLICATE_MESSAGE) from exc
    await db.refresh(exception)
    return _exception_view(exception)


@router.put("/profiles/{profile_id}/udp-exceptions/{exception_id}")
async def update_udp_exception(profile_id: int, exception_id: int,
                               data: UdpExceptionUpdate,
                               db: AsyncSession = Depends(get_db)):
    profile = await _require_profile(db, profile_id)
    exception = await _require_exception(db, profile_id, exception_id)

    fields_set = data.model_fields_set
    # 显式 null ≠ 省略：非空字段直接 422（消息只含字段名，不含值）
    for field in _REQUIRED_UPDATE_FIELDS:
        if field in fields_set and getattr(data, field) is None:
            raise HTTPException(
                422, f"{field} 不允许显式 null：省略该字段以保留原值")

    # 先校验后改属性：422 时 ORM 对象保持干净，无脏事务需要回滚
    if "match_type" in fields_set or "match_value" in fields_set:
        final_type = (data.match_type if "match_type" in fields_set
                      else exception.match_type)
        final_value = (data.match_value if "match_value" in fields_set
                       else exception.match_value)
        try:
            # 最终有效二元组（请求新值或库中原值）重新校验/规范化
            normalized = normalize_udp_match(profile.target, final_type, final_value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        exception.match_type = UdpMatchType(final_type).value
        exception.match_value = normalized
    if "display_name" in fields_set:
        exception.display_name = data.display_name
    if "enabled" in fields_set:
        exception.enabled = data.enabled
    if "sort_order" in fields_set:
        exception.sort_order = data.sort_order

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, _DUPLICATE_MESSAGE) from exc
    await db.refresh(exception)
    return _exception_view(exception)


@router.delete("/profiles/{profile_id}/udp-exceptions/{exception_id}",
               status_code=204)
async def delete_udp_exception(profile_id: int, exception_id: int,
                               db: AsyncSession = Depends(get_db)):
    await _require_profile(db, profile_id)
    exception = await _require_exception(db, profile_id, exception_id)
    await db.delete(exception)
    await db.commit()
