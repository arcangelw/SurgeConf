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

# 掩码碰撞 fallback 的静态占位序列（都不含任何原值片段）
_FALLBACK_MASKS = ("***", "[redacted]")

# MAC 掩码前必须 fullmatch 的规范存储形态：6 组 2 位十六进制、冒号分隔
#（接受存量小写；掩码输出以 normalize 契约的大写为准）
_MAC_CANONICAL = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")

# 仅用于脱敏碰撞比较的 MAC-like 规范器（非领域合法性判定）：
# 6 组、每组 2 位十六进制或 ``**``、分隔符 ``:``/``-``（允许混合，
# 保守纳入），统一 uppercase+冒号。掩码形存量（aa:bb:**:**:**:ff 等）
# 也能得到规范 key；不可规范返回 None。绝不影响 normalize_udp_match
# 的写入校验（非法值仍不可经 API 写入）。
_MAC_LIKE_GROUP = r"(?:[0-9A-Fa-f]{2}|\*\*)"
_MAC_LIKE_PATTERN = re.compile(
    rf"{_MAC_LIKE_GROUP}(?:[:-]{_MAC_LIKE_GROUP}){{5}}")


def _mask_collision_key(value: str) -> str | None:
    if not _MAC_LIKE_PATTERN.fullmatch(value):
        return None
    return value.upper().replace("-", ":")


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


def _safe_fallback_mask(forbidden: frozenset[str]) -> str:
    """取第一个不在敏感值集合中的静态占位（永不包含原值片段）。

    先试固定占位序列；极端情况下（占位本身被存为 raw）以
    ``<redacted:N>`` 递增，集合有限故必然选出安全值。
    """
    for mask in _FALLBACK_MASKS:
        if mask not in forbidden:
            return mask
    n = 1
    while True:
        mask = f"<redacted:{n}>"
        if mask not in forbidden:
            return mask
        n += 1


async def _forbidden_match_values(db: AsyncSession) -> frozenset[str]:
    """全库 UDP 例外完整匹配值（跨档案）+ MAC-like 规范等价形式。

    掩码候选不得等于其中任何值（防止一条记录的掩码回显另一条记录的
    完整敏感值）。规范器接受合法与掩码形 MAC 存量（含大小写、横线、
    混合分隔变体）；非 MAC 形值只按原文纳入。集合本身绝不进入
    响应或日志。
    """
    result = await db.execute(select(UdpException.match_value))
    values = {row[0] for row in result.all()}
    for value in tuple(values):
        key = _mask_collision_key(value)
        if key is not None:
            values.add(key)
    return frozenset(values)


def _mask_match_value(match_type: str, value: str,
                      forbidden: frozenset[str]) -> str:
    """匹配值脱敏：固定形态掩码，并对全库敏感值集合 fail closed。

    - mac_address：先 fullmatch 规范 6 组 2 位十六进制（接受存量小写），
      输出以 normalize 契约的大写为据；非 hex、横线/点分等错误分隔、
      含 ``**`` 的六组一律 fallback
    - process：仅保留首尾各 2 字符；长度不足 5 统一 fallback
    - 候选等于自身、或等于库中任何完整匹配值（含 MAC 规范等价）时
      fallback：从静态占位序列取第一个未命中集合的值，绝不回退到
      任何原值片段
    """
    candidate: str | None = None
    if match_type == UdpMatchType.MAC_ADDRESS.value:
        if _MAC_CANONICAL.fullmatch(value):
            head0, head1, _, _, _, tail = value.split(":")
            candidate = (f"{head0.upper()}:{head1.upper()}"
                         f":**:**:**:{tail.upper()}")
    elif len(value) >= 5:
        candidate = f"{value[:2]}**{value[-2:]}"
    if (candidate is not None and candidate != value
            and candidate not in forbidden):
        return candidate
    return _safe_fallback_mask(forbidden)


def _exception_view(exception: UdpException,
                    forbidden: frozenset[str]) -> dict:
    """唯一响应序列化出口：白名单字段 + 掩码值（绝不含 match_value）"""
    return {
        "id": exception.id,
        "profile_id": exception.profile_id,
        "match_type": exception.match_type,
        "match_value_masked": _mask_match_value(
            exception.match_type, exception.match_value, forbidden),
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
    forbidden = await _forbidden_match_values(db)
    result = await db.execute(
        select(UdpException)
        .where(UdpException.profile_id == profile_id)
        .order_by(UdpException.sort_order, UdpException.id))
    return [_exception_view(e, forbidden) for e in result.scalars().all()]


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
    forbidden = await _forbidden_match_values(db)
    return _exception_view(exception, forbidden)


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
    forbidden = await _forbidden_match_values(db)
    return _exception_view(exception, forbidden)


@router.delete("/profiles/{profile_id}/udp-exceptions/{exception_id}",
               status_code=204)
async def delete_udp_exception(profile_id: int, exception_id: int,
                               db: AsyncSession = Depends(get_db)):
    await _require_profile(db, profile_id)
    exception = await _require_exception(db, profile_id, exception_id)
    await db.delete(exception)
    await _commit_or_http_error(db)
