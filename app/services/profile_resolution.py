"""档案解析唯一语义：显式 id 不存在即失败，不回退默认档案。

省略 id 才解析默认档案，默认档案缺失是独立领域错误。
需要 ConfigProfile 的调用方（档案 CRUD、生成、General）统一经由
``resolve_profile``，禁止各自再实现隐式回退。
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConfigProfile


def profile_scope(model, profile_id: int):
    """档案作用域 SQL 谓词：全局（profile_id IS NULL）或属于当前档案。

    CustomRule / HostMapping 的生成查询必须在数据库层应用该谓词，
    禁止先加载全部行再在 Python 过滤。
    """
    return or_(model.profile_id.is_(None), model.profile_id == profile_id)


class ProfileNotFound(Exception):
    """显式指定的档案 id 不存在"""

    def __init__(self, profile_id: int):
        super().__init__(f"配置方案不存在: id={profile_id}")
        self.profile_id = profile_id


class DefaultProfileMissing(Exception):
    """未指定档案且库中不存在任何默认档案"""

    def __init__(self):
        super().__init__("未指定 profile_id 且不存在默认配置方案")


async def resolve_profile(db: AsyncSession, profile_id: int | None) -> ConfigProfile:
    if profile_id is not None:
        result = await db.execute(select(ConfigProfile).where(ConfigProfile.id == profile_id))
        profile = result.scalar_one_or_none()
        if profile is None:
            raise ProfileNotFound(profile_id)
        return profile

    result = await db.execute(
        select(ConfigProfile).where(ConfigProfile.is_default == True).limit(1))
    profile = result.scalar_one_or_none()
    if profile is None:
        raise DefaultProfileMissing()
    return profile
