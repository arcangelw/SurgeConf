"""Task 4：final_action 生效与生成引用完整性校验。

覆盖：
- 规则文本策略段提取（含 no-resolve / 逻辑规则嵌套逗号 / FINAL 位置差异 / fail closed）
- final_action 直通 FINAL 行（zh/en 翻译、空值与未知值拒绝）
- 规则源 / 前置与通用自定义规则引用校验（disabled 服务组不可引用）
- 多错误聚合一次抛出；生成路由 422 且不产出部分配置 / 部分文件
- 订阅边界：无效 token 永远 404，配置错误只在 token 验证后 422
"""

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch

from app.default_config import locale_translate
from app.models import ConfigProfile, CustomRule, RuleSource, ServiceGroup
from app.services.config_validation import (
    BUILTIN_ACTIONS,
    GenerationContext,
    InvalidConfiguration,
    RuleActionRef,
    extract_rule_action,
    translate_policy_name,
    translate_rule_action,
    validate_generated_references,
)
from app.services.generator import generate_surge_config

# ────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────


@pytest_asyncio.fixture
async def default_profile(db):
    result = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == True))
    return result.scalar_one()


async def _set_final_action(db, profile, value):
    profile.final_action = value
    await db.commit()


# ════════════════════════════════════════════
# 一、规则策略段提取（结构化提取，fail closed）
# ════════════════════════════════════════════


class TestExtractRuleAction:
    def test_domain_rule(self):
        assert extract_rule_action("DOMAIN,example.com,AI服务") == "AI服务"

    def test_action_with_no_resolve(self):
        assert extract_rule_action("IP-CIDR,192.0.2.0/24,大陆直连,no-resolve") == "大陆直连"

    def test_logic_rule_nested_commas(self):
        rule = "AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,googlevideo.com)),REJECT-NO-DROP"
        assert extract_rule_action(rule) == "REJECT-NO-DROP"

    def test_logic_rule_with_no_resolve(self):
        rule = "OR,((DEST-PORT,3478), (PROTOCOL,STUN)),兜底,no-resolve"
        assert extract_rule_action(rule) == "兜底"

    def test_final_rule_policy_position(self):
        assert extract_rule_action("FINAL,兜底,dns-failed") == "兜底"
        assert extract_rule_action("FINAL,兜底") == "兜底"

    def test_builtin_action_passthrough(self):
        assert extract_rule_action("DOMAIN-KEYWORD,96110,REJECT") == "REJECT"
        assert extract_rule_action("PROTOCOL,STUN,REJECT") == "REJECT"

    def test_url_regex_payload_with_parentheses(self):
        assert extract_rule_action("URL-REGEX,(Subject|HELO|SMTP),DIRECT") == "DIRECT"

    def test_unparseable_short_rule_fails_closed(self):
        assert extract_rule_action("SHORT-RULE") is None
        assert extract_rule_action("") is None
        assert extract_rule_action("   ") is None

    def test_too_many_top_level_segments_fails_closed(self):
        # 顶层 5 段无法唯一定位策略段：拒绝而不是猜最后一段
        assert extract_rule_action("DOMAIN,a,b,c,DIRECT") is None

    def test_empty_policy_fails_closed(self):
        assert extract_rule_action("DOMAIN,example.com,") is None


class TestTranslateRuleAction:
    def test_translates_policy_only(self):
        line = translate_rule_action("DOMAIN-SUFFIX,example.com,AI服务", locale_translate)
        assert line == "DOMAIN-SUFFIX,example.com,AI服务"

    def test_keeps_no_resolve_suffix(self):
        # 旧行为会把 no-resolve 当策略翻译；修复后策略段正确定位且修饰符保留
        line = translate_rule_action(
            "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve",
            lambda n: locale_translate(n, "zh"))
        assert line == "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve"

    def test_english_locale_translates_policy(self):
        line = translate_rule_action("DOMAIN-SUFFIX,example.com,AI服务",
                                     lambda n: locale_translate(n, "en"))
        assert line == "DOMAIN-SUFFIX,example.com,AI"

    def test_logic_rule_translated_and_reassembled(self):
        rule = "AND,((PROTOCOL,UDP), (DEST-PORT,3478)),REJECT"
        assert translate_rule_action(rule, lambda n: locale_translate(n, "zh")) == rule

    def test_unparseable_returns_none(self):
        assert translate_rule_action("SHORT-RULE", locale_translate) is None


# ════════════════════════════════════════════
# 二、GenerationContext 与聚合校验
# ════════════════════════════════════════════


def _ctx(locale="zh", nodes=(), regions=(), services=(), combined=(),
         actions=(), final_action="手动选择", target="mac_workstation"):
    return GenerationContext(
        locale=locale,
        target=target,
        node_names=frozenset(nodes),
        region_group_names=frozenset(regions),
        service_group_names=frozenset(services),
        combined_group_names=frozenset(combined),
        rule_actions=tuple(actions),
        final_action=final_action,
    )


class TestValidateGeneratedReferences:
    def test_valid_context_passes(self):
        ctx = _ctx(
            services=("AI服务", "兜底"),
            regions=("美国",),
            combined=("手动选择", "自动选择"),
            actions=(RuleActionRef("规则源「OpenAI」", "AI服务"),
                     RuleActionRef("前置规则", "REJECT-NO-DROP"),
                     RuleActionRef("通用规则", "美国")),
            final_action="兜底",
        )
        validate_generated_references(ctx)  # 不抛即通过

    def test_builtin_actions_always_available(self):
        for action in BUILTIN_ACTIONS:
            ctx = _ctx(actions=(RuleActionRef("r", action),), final_action="手动选择")
            validate_generated_references(ctx)

    def test_english_locale_compares_translated_names(self):
        # zh 数据库逻辑名 + en 输出：校验与输出共用同一翻译
        ctx = _ctx(locale="en", services=("AI服务",), combined=("手动选择",),
                   actions=(RuleActionRef("规则源「OpenAI」", "AI服务"),),
                   final_action="手动选择")
        validate_generated_references(ctx)

    def test_no_fuzzy_fallback_for_localized_name_in_zh(self):
        # zh 档案里直接写英文名「AI」：不做反向映射回「AI服务」，判无效
        ctx = _ctx(locale="zh", services=("AI服务",), combined=("手动选择",),
                   actions=(RuleActionRef("规则源「X」", "AI"),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("AI" in e for e in ei.value.errors)

    def test_missing_group_reported_with_source(self):
        ctx = _ctx(combined=("手动选择",),
                   actions=(RuleActionRef("规则源「Stream EU」", "英国"),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("Stream EU" in e and "英国" in e for e in ei.value.errors)

    def test_unparseable_action_fails_closed(self):
        ctx = _ctx(combined=("手动选择",),
                   actions=(RuleActionRef("前置规则「SHORT」", None),))
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("SHORT" in e for e in ei.value.errors)

    def test_empty_final_action_rejected(self):
        for value in (None, "", "   "):
            ctx = _ctx(final_action=value)
            with pytest.raises(InvalidConfiguration) as ei:
                validate_generated_references(ctx)
            assert any("final_action" in e for e in ei.value.errors)

    def test_builtin_actions_always_available(self):
        for action in BUILTIN_ACTIONS:
            ctx = _ctx(actions=(RuleActionRef("r", action),),
                       combined=("手动选择",), final_action="手动选择")
            validate_generated_references(ctx)
    def test_unknown_final_action_rejected(self):
        ctx = _ctx(final_action="幽灵组")
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        assert any("幽灵组" in e and "final_action" in e for e in ei.value.errors)


    def test_disabled_group_name_not_in_available(self):
        # 被禁用的服务组：context 不含该名字 → 引用即错
        ctx = _ctx(services=(), combined=("手动选择",),
                   actions=(RuleActionRef("规则源「X」", "AI服务"),))
        with pytest.raises(InvalidConfiguration):
            validate_generated_references(ctx)

    def test_node_names_are_referenceable(self):
        ctx = _ctx(nodes=("HK-01",), combined=("手动选择",),
                   actions=(RuleActionRef("自定义规则", "HK-01"),))
        validate_generated_references(ctx)

    def test_all_errors_aggregated_in_one_raise(self):
        ctx = _ctx(
            combined=("手动选择",),
            actions=(RuleActionRef("规则源「A」", "幽灵组A"),
                     RuleActionRef("前置规则「B」", "幽灵组B"),
                     RuleActionRef("通用规则「C」", None)),
            final_action="幽灵组F",
        )
        with pytest.raises(InvalidConfiguration) as ei:
            validate_generated_references(ctx)
        errors = ei.value.errors
        assert len(errors) == 4
        assert any("幽灵组A" in e for e in errors)
        assert any("幽灵组B" in e for e in errors)
        assert any("规则「C」" in e or "C" in e for e in errors)
        assert any("final_action" in e for e in errors)


class TestTranslatePolicyName:
    def test_matches_locale_translate(self):
        assert translate_policy_name("手动选择", "en") == "NoAuto"
        assert translate_policy_name("手动选择", "zh") == "手动选择"
        assert translate_policy_name("DIRECT", "en") == "DIRECT"


# ════════════════════════════════════════════
# 三、集成：generate_surge_config 端到端
# ════════════════════════════════════════════


class TestFinalActionIntegration:
    @pytest.mark.asyncio
    async def test_final_action_controls_final_rule(self, db, default_profile):
        await _set_final_action(db, default_profile, "大陆直连")
        config = await generate_surge_config(db, default_profile.id)
        assert "FINAL,大陆直连,dns-failed" in config
        assert "FINAL,兜底,dns-failed" not in config

    @pytest.mark.asyncio
    async def test_final_action_english_locale(self, db, default_profile):
        await _set_final_action(db, default_profile, "手动选择")
        config = await generate_surge_config(db, default_profile.id, locale_override="en")
        assert "FINAL,NoAuto,dns-failed" in config

    @pytest.mark.asyncio
    async def test_empty_final_action_aborts(self, db, default_profile):
        await _set_final_action(db, default_profile, "")
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db, default_profile.id)
        assert any("final_action" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_unknown_final_action_aborts(self, db, default_profile):
        await _set_final_action(db, default_profile, "幽灵组")
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db, default_profile.id)
        assert any("幽灵组" in e for e in ei.value.errors)


class TestReferenceValidationIntegration:
    @pytest.mark.asyncio
    async def test_missing_ai_service_group_aborts_generation(self, db):
        # 引用 AI服务 的规则存在，但服务组被禁用 → 聚合报错，不生成部分配置
        db.add(CustomRule(rule_text="DOMAIN-SUFFIX,apple-relay.example.com,AI服务",
                          position="pre", sort_order=50, enabled=True))
        await db.commit()
        await db.execute(
            update(ServiceGroup).where(ServiceGroup.name == "AI服务").values(enabled=False))
        await db.commit()
        with pytest.raises(InvalidConfiguration, match="AI服务"):
            await generate_surge_config(db)

    @pytest.mark.asyncio
    async def test_rule_source_dangling_action_aborts(self, db):
        db.add(RuleSource(name="幽灵规则源", url="https://example.com/ghost.list",
                          action="幽灵组", rule_type="RULE-SET", category="global",
                          sort_order=80, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵规则源" in e and "幽灵组" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_rule_source_valid_action_passes(self, db):
        db.add(RuleSource(name="有效规则源", url="https://example.com/ok.list",
                          action="AI服务", rule_type="RULE-SET", category="ai",
                          sort_order=80, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "RULE-SET,https://example.com/ok.list,AI服务" in config

    @pytest.mark.asyncio
    async def test_custom_rule_no_resolve_action_position(self, db):
        # no-resolve 在尾部：策略段是倒数第二段，提取不得把 no-resolve 当策略
        db.add(CustomRule(rule_text="IP-CIDR,192.0.2.0/24,幽灵组,no-resolve",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵组" in e for e in ei.value.errors)
        assert not any("no-resolve" in e and "幽灵组" not in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_custom_rule_valid_no_resolve_kept(self, db):
        db.add(CustomRule(rule_text="IP-CIDR,192.0.2.0/24,大陆直连,no-resolve",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "IP-CIDR,192.0.2.0/24,大陆直连,no-resolve" in config

    @pytest.mark.asyncio
    async def test_custom_logic_rule_nested_commas_validated(self, db):
        db.add(CustomRule(
            rule_text="AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),幽灵组",
            position="pre", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("幽灵组" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_custom_logic_rule_valid_passes(self, db):
        db.add(CustomRule(
            rule_text="AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),REJECT-NO-DROP",
            position="pre", sort_order=50, enabled=True))
        await db.commit()
        config = await generate_surge_config(db)
        assert "AND,((PROTOCOL,UDP), (DOMAIN-SUFFIX,example.com)),REJECT-NO-DROP" in config

    @pytest.mark.asyncio
    async def test_unparseable_custom_rule_aborts(self, db):
        db.add(CustomRule(rule_text="SHORT-RULE", position="general",
                          sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert any("SHORT-RULE" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_multiple_errors_reported_together(self, db, default_profile):
        await _set_final_action(db, default_profile, "幽灵组F")
        db.add(RuleSource(name="坏源一", url="https://example.com/a.list",
                          action="幽灵组A", rule_type="RULE-SET", category="global",
                          sort_order=80, enabled=True))
        db.add(CustomRule(rule_text="DOMAIN,ghost.example.com,幽灵组B",
                          position="general", sort_order=50, enabled=True))
        await db.commit()
        with pytest.raises(InvalidConfiguration) as ei:
            await generate_surge_config(db)
        assert len(ei.value.errors) == 3
        assert any("幽灵组A" in e for e in ei.value.errors)
        assert any("幽灵组B" in e for e in ei.value.errors)
        assert any("final_action" in e for e in ei.value.errors)

    @pytest.mark.asyncio
    async def test_all_valid_references_three_targets(self, db, default_profile):
        """有效引用三目标回归：mac_workstation / mac_gateway / ios_mobile 全部可生成"""
        for target, udp in (("mac_workstation", "scoped"),
                            ("mac_gateway", "compatible"),
                            ("ios_mobile", "compatible")):
            profile = ConfigProfile(
                name=f"目标-{target}", is_default=False, locale="zh",
                final_action="手动选择", target=target, udp_security_mode=udp)
            db.add(profile)
        await db.commit()
        result = await db.execute(select(ConfigProfile).where(ConfigProfile.is_default == False))
        for profile in result.scalars().all():
            config = await generate_surge_config(db, profile.id)
            assert "FINAL,手动选择,dns-failed" in config

    @pytest.mark.asyncio
    async def test_every_proxy_group_line_is_referenceable(self, db_with_nodes):
        """防漂移不变量：[Proxy Group] 实际输出的每个组名都在校验可用集合内。

        若 _build_proxy_groups 新增组行但未登记到 GroupNameSinks，
        该组名将无法被规则引用，此测试守卫登记完整性。
        """
        from sqlalchemy import select as _select
        from app.models import RegionGroup, ProxyNode
        from app.services.generator import GroupNameSinks, _build_proxy_groups

        db = db_with_nodes
        regions = (await db.execute(_select(RegionGroup).where(RegionGroup.enabled == True)
                                    .order_by(RegionGroup.sort_order))).scalars().all()
        services = (await db.execute(_select(ServiceGroup).where(ServiceGroup.enabled == True)
                                     .order_by(ServiceGroup.sort_order))).scalars().all()
        nodes = (await db.execute(_select(ProxyNode).where(ProxyNode.enabled == True)
                                  .order_by(ProxyNode.name))).scalars().all()
        sinks = GroupNameSinks()
        lines = _build_proxy_groups(nodes, regions, services,
                                    lambda n: locale_translate(n, "zh"), {},
                                    name_sinks=sinks)
        emitted = set()
        for line in lines:
            if " = " in line and line.strip():
                emitted.add(line.split(" = ", 1)[0].strip().strip('"'))
        registered = sinks.region | sinks.service | sinks.combined | {"全部节点"}
        assert emitted <= registered, emitted - registered


# ════════════════════════════════════════════
# 四、路由映射：422 + 完整错误列表 + 无部分输出
# ════════════════════════════════════════════


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（与 test_api_endpoints 相同装配）"""
    from app.main import app as fastapi_app
    from app.database import DatabaseRuntime, get_db

    monkeypatch.setenv("SURGECONF_DATA_DIR", str(tmp_path / "runtime"))
    test_runtime = DatabaseRuntime.create(tmp_path / "runtime" / "surge_conf.db")

    async def override_get_db():
        async with test_runtime.session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with patch("app.database.database", test_runtime), \
         patch("app.bootstrap.database", test_runtime):
        async with fastapi_app.router.lifespan_context(fastapi_app):
            async with AsyncClient(transport=ASGITransport(app=fastapi_app),
                                   base_url="http://test") as c:
                page = await c.get("/")
                meta = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
                assert meta, "回环 GET / 应签发隐式会话并输出 CSRF"
                c.headers["X-CSRF-Token"] = meta.group(1)
                c._test_factory = test_runtime.session_factory
                c._runtime = test_runtime
                yield c

    fastapi_app.dependency_overrides.pop(get_db, None)
    await test_runtime.dispose()


async def _break_default_config(client):
    """直插数据库：把默认档案 final_action 改成无效策略"""
    async with client._test_factory() as db:
        await db.execute(update(ConfigProfile)
                         .where(ConfigProfile.is_default == True)
                         .values(final_action="幽灵组"))
        await db.commit()


class TestRouteMapping:
    @pytest.mark.asyncio
    async def test_generate_route_returns_422_with_errors(self, client):
        await _break_default_config(client)
        resp = await client.get("/api/configs/generate?profile_id=1")
        assert resp.status_code == 422
        errors = resp.json()["detail"]["errors"]
        assert any("幽灵组" in e for e in errors)

    @pytest.mark.asyncio
    async def test_preview_route_returns_422_with_errors(self, client):
        await _break_default_config(client)
        resp = await client.get("/api/configs/preview?profile_id=1")
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])

    @pytest.mark.asyncio
    async def test_export_route_422_writes_no_partial_file(self, client, tmp_path):
        from app.runtime_paths import resolve_runtime_paths

        await _break_default_config(client)
        resp = await client.post("/api/configs/1/exports", json={"confirm": True})
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])

        paths = resolve_runtime_paths()
        assert not paths.exports.exists() or list(paths.exports.iterdir()) == [], \
            "无效配置不得产出任何部分导出文件"

    @pytest.mark.asyncio
    async def test_normal_domain_errors_still_propagate(self, client, monkeypatch):
        """非 InvalidConfiguration 的领域错误（如凭据 env 非法）继续原样传播"""
        from app.routers import configs as configs_router

        async def _boom(db, profile_id=None, locale_override=None):
            raise ValueError("boom: 非领域错误")

        # patch 路由模块的导入点：configs.py 以 from-import 绑定名字
        monkeypatch.setattr(configs_router, "generate_surge_config", _boom)
        # 非领域异常不被路由吞掉或改写：直接向外传播
        # （ASGITransport 默认 raise_app_exceptions=True，原样抛给调用方）
        with pytest.raises(ValueError, match="boom"):
            await client.get("/api/configs/generate?profile_id=1")

    @pytest.mark.asyncio
    async def test_subscribe_invalid_token_is_404_not_422(self, client):
        """无效 token 永远 404：配置错误信息不泄露给未验证对端"""
        await _break_default_config(client)
        resp = await client.get("/api/configs/profiles/1/subscribe?token=bad-token")
        assert resp.status_code == 404
        assert "幽灵组" not in resp.text

    @pytest.mark.asyncio
    async def test_subscribe_valid_token_invalid_config_is_422(self, client):
        """token 验证通过后，配置本身无效可如实 422（含完整错误列表）"""
        await _break_default_config(client)
        token = (await client.post("/api/configs/profiles/1/token")).json()["token"]
        resp = await client.get(f"/api/configs/profiles/1/subscribe?token={token}")
        assert resp.status_code == 422
        assert any("幽灵组" in e for e in resp.json()["detail"]["errors"])
