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

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ConfigProfile, UdpException
from app.services.profile_resolution import ProfileNotFound, resolve_profile
from app.udp_policy import (UdpMatchType, _contains_control_character,
                            normalize_udp_match)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/configs", tags=["UDP 例外"])

# 响应字段白名单（锁定：无 hash/secret/完整值）
EXCEPTION_FIELDS = ("id", "profile_id", "match_type", "match_value_masked",
                    "display_name", "enabled", "sort_order")

# 更新里不允许显式 null 的非空列；display_name 可空，显式 null = 清空
_REQUIRED_UPDATE_FIELDS = ("match_type", "match_value", "enabled", "sort_order")

# 唯一约束 uq_udp_exceptions_profile_match 冲突的稳定消息（不回显值）
_DUPLICATE_MESSAGE = "已存在相同的 UDP 例外（同档案同类型同匹配值）"

# 其他数据库错误的静态文案（绝不拼接异常文本/语句/参数）
_DB_UNAVAILABLE = "服务暂时不可用，请稍后重试"

# MAC 掩码前必须 fullmatch 的规范存储形态：6 组 2 位十六进制、冒号分隔
#（接受存量小写；掩码输出以 normalize 契约的大写为准）
_MAC_CANONICAL = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


class _DisplayNameChecked(BaseModel):
    """display_name 公共字段与校验（Create/Update 共用）。

    拒绝换行/控制字符（列表注入防护），消息为静态文案；即使未来有人
    把值拼进 ValueError，全局 RequestValidationError 处理器也只回
    静态映射消息。
    """

    display_name: Optional[str] = Field(default=None, max_length=100)

    @field_validator("display_name")
    @classmethod
    def _no_control_characters(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and _contains_control_character(value):
            raise ValueError("display_name 不得包含换行或控制字符")
        return value


class UdpExceptionCreate(_DisplayNameChecked):
    match_type: UdpMatchType
    match_value: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    sort_order: int = 0


class UdpExceptionUpdate(_DisplayNameChecked):
    match_type: Optional[UdpMatchType] = None
    match_value: Optional[str] = Field(default=None, min_length=1, max_length=255)
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

    - mac_address：先 fullmatch 规范 6 组 2 位十六进制（接受存量小写），
      输出以 normalize 契约的大写为据（``02:00:**:**:**:01`` 形态）；
      非 hex、横线/点分等错误分隔、含 ``**`` 的六组一律 ``***``
    - process：仅保留首尾各 2 字符；长度不足 5 统一 ``***``
    - 任何分支的掩码候选若逐字等于原值（值本身形如 ``ab**cd``），
      掩码形同虚设，回退 ``***``
    """
    if match_type == UdpMatchType.MAC_ADDRESS.value:
        if not _MAC_CANONICAL.fullmatch(value):
            return "***"
        head0, head1, _, _, _, tail = value.split(":")
        candidate = f"{head0.upper()}:{head1.upper()}:**:**:**:{tail.upper()}"
        return candidate if candidate != value else "***"
    if len(value) < 5:
        return "***"
    candidate = f"{value[:2]}**{value[-2:]}"
    return candidate if candidate != value else "***"


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


async def _commit_or_http_error(db: AsyncSession) -> None:
    """写路径统一提交：唯一冲突 409；其他 SQLAlchemy 错误 rollback 后
    静态 503。日志仅记异常类型名（语句/参数/异常文本一律不落盘，
    引擎层另以 hide_parameters 兜底隐藏绑定参数）。"""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, _DUPLICATE_MESSAGE) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error("UDP 例外数据库写入失败: %s", type(exc).__name__)
        raise HTTPException(503, _DB_UNAVAILABLE) from exc


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
    await _commit_or_http_error(db)
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

    await _commit_or_http_error(db)
    await db.refresh(exception)
    return _exception_view(exception)


@router.delete("/profiles/{profile_id}/udp-exceptions/{exception_id}",
               status_code=204)
async def delete_udp_exception(profile_id: int, exception_id: int,
                               db: AsyncSession = Depends(get_db)):
    await _require_profile(db, profile_id)
    exception = await _require_exception(db, profile_id, exception_id)
    await db.delete(exception)
    await _commit_or_http_error(db)
