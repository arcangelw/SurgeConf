"""Skip-Proxy 合并逻辑 & Host skip_proxy 字段测试"""

import pytest
from app.models import SystemConfig, HostMapping
from app.services.generator import generate_surge_config
from app.routers.configs import (
    create_host_mapping, update_host_mapping, list_host_mappings,
    HostMappingCreate, HostMappingUpdate,
)


def _extract_skip_proxy_items(config: str) -> list[str]:
    line = next(l for l in config.splitlines() if l.startswith("skip-proxy ="))
    return [x.strip() for x in line.split("=", 1)[1].split(",") if x.strip()]


@pytest.mark.asyncio
async def test_skip_proxy_merge_default_custom_host(db):
    """skip-proxy = 默认 + 自定义 + host 勾选域名，去重；host 未勾选的不含"""
    db.add(SystemConfig(key="skip_proxy_custom",
                        value='["198.51.100.0/24", " *.example.com "]'))
    from app.models import ConfigProfile
    from sqlalchemy import select
    default_pid = (await db.execute(
        select(ConfigProfile).where(ConfigProfile.is_default == True)
    )).scalar_one().id

    # 当前方案中勾选/未勾选的 host
    db.add(HostMapping(domain="keep.example.com", target="server:1.1.1.1",
                       skip_proxy=True, enabled=True, sort_order=0, profile_id=default_pid))
    db.add(HostMapping(domain="drop.example.com", target="server:1.1.1.1",
                       skip_proxy=False, enabled=True, sort_order=1, profile_id=default_pid))
    # 其他方案的 host（即使勾选）不并入当前配置
    db.add(HostMapping(domain="other.example.com", target="server:1.1.1.1",
                       skip_proxy=True, enabled=True, sort_order=2, profile_id=default_pid + 1000))
    # 全局 host（profile_id=None，规则管理的默认映射）勾选后同样并入
    db.add(HostMapping(domain="default.example.com", target="server:1.1.1.1",
                       skip_proxy=True, enabled=True, sort_order=3, profile_id=None))
    await db.commit()

    items = _extract_skip_proxy_items(await generate_surge_config(db))

    # 内置默认条目
    assert "localhost" in items
    assert "10.0.0.0/8" in items
    # 系统级自定义条目（两侧空格被 strip）
    assert "198.51.100.0/24" in items
    assert "*.example.com" in items
    # 当前方案中勾选的域名并入
    assert "keep.example.com" in items
    # 当前方案中未勾选的域名不出现
    assert "drop.example.com" not in items
    # 其他方案的 host 不并入
    assert "other.example.com" not in items
    # 全局勾选的默认 host 也并入（作用域 = 全局 + 当前档案）
    assert "default.example.com" in items
    # 去重
    assert len(items) == len(set(items))


@pytest.mark.asyncio
async def test_skip_proxy_legacy_profile_value_merged(db):
    """旧版 profile.general 残留的 skip-proxy 仍被合并（升级兼容）"""
    from app.models import ConfigProfile
    from sqlalchemy import select
    res = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == True))
    profile = res.scalar_one()
    profile.general = {"skip-proxy": "legacy.example.com, 10.0.0.0/8"}
    await db.commit()

    items = _extract_skip_proxy_items(await generate_surge_config(db))
    assert "legacy.example.com" in items   # 旧残留并入
    assert items.count("10.0.0.0/8") == 1  # 与默认重复时去重


@pytest.mark.asyncio
async def test_host_mapping_skip_proxy_field(db):
    """HostMapping CRUD 透传 skip_proxy，默认 True"""
    await create_host_mapping(
        HostMappingCreate(domain="a.test", target="server:1.1.1.1"), db)
    await create_host_mapping(
        HostMappingCreate(domain="b.test", target="server:1.1.1.1", skip_proxy=False), db)

    mappings = await list_host_mappings(db=db)
    by_domain = {m["domain"]: m for m in mappings}
    assert by_domain["a.test"]["skip_proxy"] is True
    assert by_domain["b.test"]["skip_proxy"] is False

    # 更新
    aid = by_domain["a.test"]["id"]
    await update_host_mapping(aid, HostMappingUpdate(skip_proxy=False), db)
    mappings = await list_host_mappings(db=db)
    a = next(m for m in mappings if m["domain"] == "a.test")
    assert a["skip_proxy"] is False


@pytest.mark.asyncio
async def test_config_has_script_section(db):
    """生成的配置应包含 [Script] 段"""
    config = await generate_surge_config(db)
    assert "[Script]" in config


@pytest.mark.asyncio
async def test_general_skip_proxy_endpoints(db):
    """/api/general/skip-proxy GET/PUT：自定义条目持久化，返回默认列表"""
    from app.routers.general import get_skip_proxy, save_skip_proxy, SkipProxyUpdate

    data = await get_skip_proxy(db=db)
    assert data["custom"] == []
    assert isinstance(data["defaults"], list) and "localhost" in data["defaults"]

    r = await save_skip_proxy(SkipProxyUpdate(items=["  203.0.113.0/24 ", "x.com"]), db=db)
    assert r["items"] == ["203.0.113.0/24", "x.com"]

    data = await get_skip_proxy(db=db)
    assert data["custom"] == ["203.0.113.0/24", "x.com"]

