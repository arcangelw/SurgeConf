"""平台 General 能力矩阵：能力表唯一真源、保存 API 过滤与生成器接入

能力表（COMMON/MAC_ONLY/IOS_ONLY/GATEWAY_ONLY + GENERATOR_ONLY +
强制安全值）是唯一真源，字段元数据与值过滤两个出口必须由同一张表
派生，不允许漂移。表外未知键不属于任何目标：PUT 拒绝、生成/读取丢弃。
所有凭据/地址均为合成值（回环地址 + 占位用户名），不依赖真实运行库。
"""

import pytest

from app.default_config import DEFAULT_GENERAL
from app.general_fields import (GENERAL_GROUPS, general_fields_for_target,
                                general_groups_for_target)
from app.models import ConfigProfile
from app.profile_targets import (COMMON_GENERAL_KEYS, CONFIGURABLE_GENERAL_KEYS,
                                 GATEWAY_ONLY, GENERATOR_ONLY_KEYS,
                                 IOS_ONLY, KNOWN_GENERAL_KEYS, MAC_ONLY,
                                 ProfileTarget, configurable_general_keys,
                                 filter_general_for_target,
                                 forced_general_values, general_targets_for)
from app.routers.general import get_fields, get_values, save_values

# 合成值全集：覆盖元数据全部键 + 平台专属键 + 危险强制键输入
ALL_SYNTHETIC_GENERAL_VALUES = {
    # 通用键
    "wifi-assist": True,
    "ipv6": False,
    "ipv6-vif": "auto",
    "udp-priority": False,
    "all-hybrid": False,
    "internet-test-url": "http://synthetic.test/generate_204",
    "proxy-test-url": "http://synthetic.test/generate_204",
    "test-timeout": 5,
    "dns-server": "192.0.2.53",
    "encrypted-dns-server": "https://synthetic.test/dns-query",
    "encrypted-dns-follow-outbound-mode": False,
    "always-real-ip": "*.synthetic.test",
    "hijack-dns": "*:53",
    "loglevel": "notify",
    "show-error-page-for-reject": True,
    "exclude-simple-hostnames": True,
    "include-all-networks": False,
    "include-local-networks": False,
    "block-quic": "proxy",  # 危险值：必须被强制为 per-policy
    "udp-policy-not-supported-behaviour": "DIRECT",  # 危险值：必须被强制为 REJECT
    # Mac 专属（workstation + gateway）
    "external-controller-access": "synthetic-admin@127.0.0.1:6171",
    "http-api": "synthetic-admin@127.0.0.1:6172",
    "http-api-tls": True,
    "http-api-web-dashboard": False,
    "http-listen": "127.0.0.1",
    "socks5-listen": "127.0.0.1",
    "allow-wifi-access": False,
    "wifi-access-http-port": 6152,
    "wifi-access-socks5-port": 6153,
    "allow-hotspot-access": False,
    "read-etc-hosts": True,
    "use-local-host-item-for-proxy": True,
    "proxy-restricted-to-lan": False,
    # iOS 专属
    "compatibility-mode": 3,
    "auto-suspend": False,
    # Gateway 专属
    "gateway-restricted-to-lan": False,
}

ALL_TARGETS = list(ProfileTarget)


# ===== 能力矩阵（brief Step 1）=====

@pytest.mark.parametrize(("target", "present", "absent"), [
    (ProfileTarget.MAC_WORKSTATION,
     {"read-etc-hosts", "http-api"},
     {"compatibility-mode", "auto-suspend", "gateway-restricted-to-lan"}),
    (ProfileTarget.MAC_GATEWAY,
     {"read-etc-hosts", "http-api", "gateway-restricted-to-lan"},
     {"compatibility-mode", "auto-suspend"}),
    (ProfileTarget.IOS_MOBILE,
     {"compatibility-mode", "auto-suspend", "block-quic"},
     {"http-api", "http-listen", "read-etc-hosts", "gateway-restricted-to-lan"}),
])
def test_general_capability_matrix(target, present, absent):
    values = filter_general_for_target(target, ALL_SYNTHETIC_GENERAL_VALUES)
    assert present <= values.keys()
    assert not (absent & values.keys())


# ===== 强制安全值 =====

class TestForcedSafetyValues:
    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_common_forced_all_targets(self, target):
        """block-quic / udp-policy-not-supported-behaviour 在所有目标强制安全值"""
        out = filter_general_for_target(target, {
            "block-quic": "proxy",
            "udp-policy-not-supported-behaviour": "DIRECT",
        })
        assert out["block-quic"] == "per-policy"
        assert out["udp-policy-not-supported-behaviour"] == "REJECT"

    def test_common_forced_injected_when_missing(self):
        out = filter_general_for_target(ProfileTarget.MAC_WORKSTATION, {})
        assert out["block-quic"] == "per-policy"
        assert out["udp-policy-not-supported-behaviour"] == "REJECT"

    def test_gateway_forces_lan_restriction(self):
        out = filter_general_for_target(ProfileTarget.MAC_GATEWAY,
                                        {"gateway-restricted-to-lan": False})
        assert out["gateway-restricted-to-lan"] is True

    def test_ios_forces_compat_and_suspend(self):
        out = filter_general_for_target(ProfileTarget.IOS_MOBILE,
                                        {"compatibility-mode": 3, "auto-suspend": False})
        assert out["compatibility-mode"] == 1
        assert out["auto-suspend"] is True

    def test_workstation_keeps_unforced_input(self):
        out = filter_general_for_target(ProfileTarget.MAC_WORKSTATION,
                                        {"read-etc-hosts": False})
        assert out["read-etc-hosts"] is False
        assert "compatibility-mode" not in out

    def test_forced_general_values_lookup(self):
        forced = forced_general_values(ProfileTarget.IOS_MOBILE)
        assert forced["compatibility-mode"] == 1
        assert forced["auto-suspend"] is True
        assert forced["block-quic"] == "per-policy"
        assert "gateway-restricted-to-lan" not in forced


# ===== 唯一真源：能力表 <-> 字段元数据 =====

class TestSingleSourceOfTruth:
    def test_metadata_covers_all_capability_keys(self):
        """能力表中的键全部出现在字段元数据中（不允许无元数据的专属键）"""
        meta_keys = {f.key for g in GENERAL_GROUPS for f in g["fields"]}
        assert IOS_ONLY | MAC_ONLY | GATEWAY_ONLY <= meta_keys

    def test_metadata_includes_new_fields(self):
        meta_keys = {f.key for g in GENERAL_GROUPS for f in g["fields"]}
        assert {"auto-suspend", "gateway-restricted-to-lan",
                "block-quic", "http-api-tls"} <= meta_keys

    def test_fielddef_targets_derive_from_capability_matrix(self):
        """FieldDef.targets 必须与能力表逐键对齐（派生而非手写第二份）"""
        for g in GENERAL_GROUPS:
            for f in g["fields"]:
                assert set(f.targets) == set(general_targets_for(f.key)), f.key

    def test_filter_and_field_metadata_agree(self):
        """值过滤出口与字段元数据出口对每个目标给出同一 key 集合"""
        assert ALL_SYNTHETIC_GENERAL_VALUES.keys() >= {
            f.key for g in GENERAL_GROUPS for f in g["fields"]}
        for target in ALL_TARGETS:
            filtered = filter_general_for_target(target, ALL_SYNTHETIC_GENERAL_VALUES)
            meta_keys = {f["key"] for f in general_fields_for_target(target)}
            assert set(filtered) == meta_keys, target

    def test_default_general_keys_all_in_metadata(self):
        meta_keys = {f.key for g in GENERAL_GROUPS for f in g["fields"]}
        assert set(DEFAULT_GENERAL) <= meta_keys

    def test_unknown_key_belongs_to_no_target(self):
        """表外未知键不属于任何目标：PUT 拒绝、生成/读取丢弃"""
        assert general_targets_for("some-future-key") == frozenset()

    def test_generator_only_keys_allowed_in_output_not_configurable(self):
        """skip-proxy 属生成期内部键：输出放行但不可配置"""
        assert general_targets_for("skip-proxy") == frozenset(ProfileTarget)
        assert "skip-proxy" in GENERATOR_ONLY_KEYS
        for target in ProfileTarget:
            assert "skip-proxy" not in configurable_general_keys(target)

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_filter_drops_unknown_keeps_generator_only(self, target):
        """filter：未知键丢弃；生成期内部键（skip-proxy）输出保留"""
        out = filter_general_for_target(
            target, {"some-unknown-key": "x", "skip-proxy": "localhost, 127.0.0.1"})
        assert "some-unknown-key" not in out
        assert out["skip-proxy"] == "localhost, 127.0.0.1"

    def test_capability_covers_default_general_and_metadata(self):
        """能力表完备性：DEFAULT_GENERAL 与字段元数据全部登记，无表外键"""
        meta_keys = {f.key for g in GENERAL_GROUPS for f in g["fields"]}
        # 元数据键 == 可配置键全集（双向对齐，防两侧漂移）
        assert meta_keys == set(CONFIGURABLE_GENERAL_KEYS)
        # 默认值与元数据都在已知键域内
        assert set(DEFAULT_GENERAL) <= KNOWN_GENERAL_KEYS
        # 已知键 = 可配置 + 生成期内部，无重叠
        assert KNOWN_GENERAL_KEYS == set(CONFIGURABLE_GENERAL_KEYS) | set(GENERATOR_ONLY_KEYS)
        assert COMMON_GENERAL_KEYS.isdisjoint(GENERATOR_ONLY_KEYS)

# ===== GET /api/general/fields =====

async def _add_profile(db, name, target, general=None, udp_mode=None):
    profile = ConfigProfile(
        name=name, is_default=False, locale="zh", final_action="手动选择",
        general=general if general is not None else dict(DEFAULT_GENERAL),
        target=target.value,
        udp_security_mode=udp_mode or ("compatible" if target is ProfileTarget.IOS_MOBILE else "scoped"),
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


class TestFieldsEndpoint:
    @pytest.mark.asyncio
    async def test_fields_filtered_by_profile_target(self, db):
        profile = await _add_profile(db, "iPhone 档", ProfileTarget.IOS_MOBILE)
        groups = await get_fields(profile_id=profile.id, db=db)
        keys = {f["key"] for g in groups for f in g["fields"]}
        assert "compatibility-mode" in keys
        assert "http-api" not in keys
        assert "gateway-restricted-to-lan" not in keys

    @pytest.mark.asyncio
    async def test_fields_gateway_profile(self, db):
        profile = await _add_profile(db, "网关档", ProfileTarget.MAC_GATEWAY)
        groups = await get_fields(profile_id=profile.id, db=db)
        keys = {f["key"] for g in groups for f in g["fields"]}
        assert {"gateway-restricted-to-lan", "read-etc-hosts"} <= keys
        assert "compatibility-mode" not in keys

    @pytest.mark.asyncio
    async def test_fields_default_workstation(self, db):
        """无 profile_id：解析默认档案（workstation）"""
        groups = await get_fields(profile_id=None, db=db)
        keys = {f["key"] for g in groups for f in g["fields"]}
        assert "read-etc-hosts" in keys
        assert "compatibility-mode" not in keys
        assert "gateway-restricted-to-lan" not in keys

    @pytest.mark.asyncio
    async def test_fields_unknown_profile_404(self, db):
        with pytest.raises(Exception) as exc_info:
            await get_fields(profile_id=99999, db=db)
        assert getattr(exc_info.value, "status_code", None) == 404

    @pytest.mark.asyncio
    async def test_fields_skip_empty_group(self, db):
        """iOS 档案的 controller 组全为 Mac 专属：整组不返回"""
        profile = await _add_profile(db, "iPhone 档", ProfileTarget.IOS_MOBILE)
        groups = await get_fields(profile_id=profile.id, db=db)
        assert all(g["fields"] for g in groups)
        assert "controller" not in {g["key"] for g in groups}

    def test_general_groups_for_target_structure(self):
        groups = general_groups_for_target(ProfileTarget.IOS_MOBILE)
        assert groups and all("label_zh" in g and "fields" in g for g in groups)


class TestForcedFieldMetadata:
    """强制安全字段的元数据逐目标回归：只暴露唯一允许选项"""

    def _field(self, target, key):
        return next(f for f in general_fields_for_target(target) if f["key"] == key)

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_block_quic_single_option_forced(self, target):
        f = self._field(target, "block-quic")
        assert f["forced"] is True
        assert [o["value"] for o in f["options"]] == ["per-policy"]

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_udp_behaviour_single_option_forced(self, target):
        f = self._field(target, "udp-policy-not-supported-behaviour")
        assert f["forced"] is True
        assert [o["value"] for o in f["options"]] == ["REJECT"]

    def test_gateway_lan_restriction_forced(self):
        f = self._field(ProfileTarget.MAC_GATEWAY, "gateway-restricted-to-lan")
        assert f["forced"] is True

    def test_ios_compat_and_suspend_forced(self):
        for key in ("compatibility-mode", "auto-suspend"):
            f = self._field(ProfileTarget.IOS_MOBILE, key)
            assert f["forced"] is True, key

    @pytest.mark.parametrize("target", ALL_TARGETS)
    def test_non_forced_fields_not_marked(self, target):
        forced_keys = set(forced_general_values(target))
        for f in general_fields_for_target(target):
            if f["key"] not in forced_keys:
                assert not f.get("forced"), f["key"]

    def test_workstation_has_no_ios_only_forced_fields(self):
        keys = {f["key"] for f in general_fields_for_target(ProfileTarget.MAC_WORKSTATION)}
        assert not ({"compatibility-mode", "auto-suspend", "gateway-restricted-to-lan"} & keys)


# ===== GET /api/general/values =====

class TestGetValuesEndpoint:
    @pytest.mark.asyncio
    async def test_get_values_unknown_profile_404(self, db):
        with pytest.raises(Exception) as exc_info:
            await get_values(profile_id=99999, db=db)
        assert getattr(exc_info.value, "status_code", None) == 404

    @pytest.mark.asyncio
    async def test_get_values_filters_and_forces(self, db):
        """iOS 档案：旧残留 Mac 键被过滤，强制键生效，空值跳过"""
        profile = await _add_profile(
            db, "iPhone 档", ProfileTarget.IOS_MOBILE,
            general={"http-api": "synthetic-admin@127.0.0.1:6172",
                     "wifi-assist": True, "internet-test-url": "",
                     "test-timeout": None},
        )
        data = await get_values(profile_id=profile.id, db=db)
        assert data["profile_id"] == profile.id
        assert "http-api" not in data["values"]
        assert data["values"]["compatibility-mode"] == 1
        assert data["values"]["auto-suspend"] is True
        assert data["values"]["block-quic"] == "per-policy"
        assert "internet-test-url" not in data["values"]

    @pytest.mark.asyncio
    async def test_get_values_drops_generator_only_and_unknown(self, db):
        """存量 skip-proxy 与表外未知键不进入用户可配置值"""
        profile = await _add_profile(
            db, "脏值档", ProfileTarget.MAC_WORKSTATION,
            general={"skip-proxy": "stale", "some-future-key": 1,
                     "loglevel": "info"},
        )
        data = await get_values(profile_id=profile.id, db=db)
        assert "skip-proxy" not in data["values"]
        assert "some-future-key" not in data["values"]
        assert data["values"]["loglevel"] == "info"

    @pytest.mark.asyncio
    async def test_get_values_none_uses_default(self, db):
        data = await get_values(profile_id=None, db=db)
        assert data["profile_id"] == 1
        assert data["values"]["read-etc-hosts"] is True
        assert "compatibility-mode" not in data["values"]


# ===== PUT /api/general/values =====

class TestSaveValuesEndpoint:
    @pytest.mark.asyncio
    async def test_save_rejects_unsupported_key_with_name(self, db):
        """workstation 档案请求 iOS 专属键：422 且错误信息含 key 名"""
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=1, values={"compatibility-mode": 1}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422
        assert "compatibility-mode" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_save_rejects_ios_key_on_gateway(self, db):
        profile = await _add_profile(db, "网关档", ProfileTarget.MAC_GATEWAY)
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=profile.id, values={"auto-suspend": True}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422

    @pytest.mark.asyncio
    async def test_save_rejects_gateway_key_on_workstation(self, db):
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=1, values={"gateway-restricted-to-lan": True}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422

    @pytest.mark.asyncio
    async def test_save_rejects_dangerous_forced_value(self, db):
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=1, values={"block-quic": "proxy"}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422
        assert "block-quic" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_save_rejects_dangerous_udp_behaviour(self, db):
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=1,
                              values={"udp-policy-not-supported-behaviour": "DROP"}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422

    @pytest.mark.asyncio
    async def test_save_accepts_forced_value(self, db):
        """与强制值一致的请求可保存（幂等）"""
        await save_values(profile_id=1, values={"block-quic": "per-policy"}, db=db)

    @pytest.mark.asyncio
    async def test_save_unknown_profile_404(self, db):
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=99999, values={"wifi-assist": True}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 404

    @pytest.mark.asyncio
    async def test_save_cleans_legacy_incompatible_keys(self, db):
        """旧目标残留（持久层，不在本次请求中）：静默清理；新输入非法：422"""
        profile = await _add_profile(
            db, "迁移档", ProfileTarget.MAC_WORKSTATION,
            general={"compatibility-mode": 3, "read-etc-hosts": True},
        )
        await save_values(profile_id=profile.id, values={"loglevel": "info"}, db=db)
        assert profile.general == {"read-etc-hosts": True, "loglevel": "info",
                                   "block-quic": "per-policy",
                                   "udp-policy-not-supported-behaviour": "REJECT"}

    @pytest.mark.asyncio
    async def test_save_persists_forced_values(self, db):
        profile = await _add_profile(db, "iPhone 档", ProfileTarget.IOS_MOBILE)
        await save_values(profile_id=profile.id, values={"wifi-assist": True}, db=db)
        assert profile.general["compatibility-mode"] == 1
        assert profile.general["auto-suspend"] is True
        assert profile.general["wifi-assist"] is True

    @pytest.mark.asyncio
    async def test_save_rejects_unknown_key_no_commit(self, db):
        """表外未知键：422 指出 key 且持久层不提交（原值不动）"""
        profile = await db.get(ConfigProfile, 1)
        before = dict(profile.general or {})
        with pytest.raises(Exception) as exc_info:
            await save_values(profile_id=1, values={"some-future-key": "on"}, db=db)
        assert getattr(exc_info.value, "status_code", None) == 422
        assert "some-future-key" in str(exc_info.value.detail)
        assert dict(profile.general or {}) == before

    @pytest.mark.asyncio
    async def test_save_cleans_legacy_unknown_and_generator_keys(self, db):
        """持久层旧残留的未知键与生成期内部键随下次保存清理"""
        profile = await _add_profile(
            db, "脏档", ProfileTarget.MAC_WORKSTATION,
            general={"some-future-key": "x", "skip-proxy": "stale",
                     "compatibility-mode": 3, "loglevel": "notify"},
        )
        await save_values(profile_id=profile.id, values={"test-timeout": 8}, db=db)
        assert set(profile.general) == {"loglevel", "test-timeout",
                                        "block-quic",
                                        "udp-policy-not-supported-behaviour"}
        assert "skip-proxy" not in profile.general
