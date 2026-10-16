"""MITM 配置管理单元测试 + MITM API 覆盖式契约（Task 5）"""

import configparser
import json
from pathlib import Path

import pytest
from app.models import ConfigProfile
from app.default_config import DEFAULT_MITM
from app.services.generator import generate_surge_config

# admin_client / secure_app fixtures（回环隐式会话 + CSRF）来自 Task 5 测试模块
from tests.test_profile_tokens import admin_client, secure_app  # noqa: F401

_SYNTHETIC_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_profile.conf"


def _synthetic(key: str) -> str:
    """从合成夹具读取明确标记的虚构 MITM 数据（无真实证书前缀/长度）。"""
    parser = configparser.ConfigParser()
    parser.read(_SYNTHETIC_FIXTURE, encoding="utf-8")
    return parser["mitm"][key]


# ════════════════════════════════════════════
# 一、Profile MITM 字段 CRUD
# ════════════════════════════════════════════


class TestProfileMitmField:
    """ConfigProfile 模型的 mitm JSON 字段"""

    @pytest.mark.asyncio
    async def test_default_profile_has_no_mitm(self, db):
        """新建 profile 不传 mitm 时默认为空"""
        p = ConfigProfile(name="测试", locale="zh")
        db.add(p)
        await db.commit()
        assert p.mitm is None or p.mitm == {}

    @pytest.mark.asyncio
    async def test_save_full_mitm(self, db):
        """保存完整的 MITM 配置"""
        mitm = {
            "skip-server-cert-verify": False,
            "tcp-connection": True,
            "h2": False,
            "hostname": _synthetic("hostname"),
            "ca-passphrase": _synthetic("ca-passphrase"),
            "ca-p12": _synthetic("ca-p12"),
        }
        p = ConfigProfile(name="测试MITM", locale="zh", mitm=mitm)
        db.add(p)
        await db.commit()

        assert p.mitm["skip-server-cert-verify"] is False
        assert p.mitm["tcp-connection"] is True
        assert p.mitm["h2"] is False
        assert p.mitm["hostname"] == _synthetic("hostname")
        assert p.mitm["ca-passphrase"] == _synthetic("ca-passphrase")
        assert p.mitm["ca-p12"] == _synthetic("ca-p12")

    @pytest.mark.asyncio
    async def test_partial_mitm_override(self, db):
        """只覆盖部分字段，未覆盖的保留为空（由 generator 合并默认值）"""
        mitm = {"skip-server-cert-verify": False}
        p = ConfigProfile(name="部分覆盖", locale="zh", mitm=mitm)
        db.add(p)
        await db.commit()
        assert p.mitm == {"skip-server-cert-verify": False}

    @pytest.mark.asyncio
    async def test_reset_mitm_to_empty(self, db):
        """重置 mitm 为空 dict"""
        p = ConfigProfile(name="重置测试", locale="zh", mitm={"hostname": "x.com"})
        db.add(p)
        await db.commit()
        assert p.mitm == {"hostname": "x.com"}

        p.mitm = {}
        await db.commit()
        assert p.mitm == {}

    @pytest.mark.asyncio
    async def test_empty_mitm_is_serializable(self, db):
        """空 mitm 字典可序列化"""
        p = ConfigProfile(name="空MITM", locale="zh", mitm={})
        db.add(p)
        await db.commit()
        # 模拟 API 序列化
        dumped = {"id": p.id, "name": p.name, "mitm": p.mitm}
        assert dumped["mitm"] == {}


# ════════════════════════════════════════════
# 二、Generator MITM 段
# ════════════════════════════════════════════


class TestGeneratorMitm:
    """配置生成器 MITM [MITM] 段"""

    @pytest.mark.asyncio
    async def test_default_mitm_when_no_profile(self, db):
        """无 profile 时使用 DEFAULT_MITM"""
        config = await generate_surge_config(db, profile_id=None)
        mitm_lines = self._extract_mitm(config)
        assert "skip-server-cert-verify = false" in mitm_lines
        assert "tcp-connection = true" in mitm_lines
        assert "h2 = true" in mitm_lines
        assert "www.google.cn" in " ".join(mitm_lines)

    @pytest.mark.asyncio
    async def test_default_mitm_when_profile_mitm_empty(self, db):
        """profile.mitm 为空时回退 DEFAULT_MITM"""
        config = await generate_surge_config(db, profile_id=1)
        mitm_lines = self._extract_mitm(config)
        assert "skip-server-cert-verify = false" in mitm_lines
        assert "tcp-connection = true" in mitm_lines
        assert "h2 = true" in mitm_lines
        assert "www.google.cn" in " ".join(mitm_lines)

    @pytest.mark.asyncio
    async def test_profile_mitm_overrides_default(self, db):
        """profile.mitm 覆盖 DEFAULT_MITM"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="自定义MITM", locale="zh",
                          mitm={"skip-server-cert-verify": False, "hostname": "custom.example.com"})
        db.add(p)
        await db.commit()

        config = await generate_surge_config(db, profile_id=p.id)
        mitm_lines = self._extract_mitm(config)
        assert "skip-server-cert-verify = false" in mitm_lines
        assert "custom.example.com" in " ".join(mitm_lines)
        # 未覆盖的字段继承默认值
        assert "tcp-connection = true" in mitm_lines
        assert "h2 = true" in mitm_lines

    @pytest.mark.asyncio
    async def test_partial_override_preserves_other_defaults(self, db):
        """部分覆盖：只覆盖 hostname，其他保持默认"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="部分覆盖", locale="zh",
                          mitm={"hostname": "only.this.com"})
        db.add(p)
        await db.commit()

        config = await generate_surge_config(db, profile_id=p.id)
        mitm_lines = self._extract_mitm(config)
        assert "hostname = only.this.com" in mitm_lines
        assert "skip-server-cert-verify = false" in mitm_lines
        assert "tcp-connection = true" in mitm_lines
        assert "h2 = true" in mitm_lines

    @pytest.mark.asyncio
    async def test_mitm_with_ca_fields(self, db):
        """ca-passphrase 和 ca-p12 正确输出（合成 base64 夹具）"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="CA测试", locale="zh",
                          mitm={
                              "ca-passphrase": _synthetic("ca-passphrase"),
                              "ca-p12": _synthetic("ca-p12"),
                          })
        db.add(p)
        await db.commit()

        config = await generate_surge_config(db, profile_id=p.id)
        mitm_lines = self._extract_mitm(config)
        assert f"ca-passphrase = {_synthetic('ca-passphrase')}" in mitm_lines
        assert f"ca-p12 = {_synthetic('ca-p12')}" in mitm_lines

    @pytest.mark.asyncio
    async def test_mitm_bool_values_format(self, db):
        """布尔值输出为 true/false 小写"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="布尔测试", locale="zh",
                          mitm={"skip-server-cert-verify": False, "tcp-connection": False})
        db.add(p)
        await db.commit()

        config = await generate_surge_config(db, profile_id=p.id)
        mitm_lines = self._extract_mitm(config)
        assert "skip-server-cert-verify = false" in mitm_lines
        assert "tcp-connection = false" in mitm_lines
        # 确认不是大写
        assert all("True" not in l for l in mitm_lines)
        assert all("False" not in l for l in mitm_lines)

    @pytest.mark.asyncio
    async def test_mitm_hostname_with_exclude(self, db):
        """hostname 含 - 排除标记"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="排除测试", locale="zh",
                          mitm={"hostname": "*.example.com, -excluded.com"})
        db.add(p)
        await db.commit()

        config = await generate_surge_config(db, profile_id=p.id)
        mitm_lines = self._extract_mitm(config)
        assert "*.example.com" in " ".join(mitm_lines)
        assert "-excluded.com" in " ".join(mitm_lines)

    @pytest.mark.asyncio
    async def test_reset_mitm_falls_back_to_default(self, db):
        """重置 profile.mitm={} 后生成器回退 DEFAULT_MITM"""
        from app.models import ConfigProfile
        p = ConfigProfile(name="重置测试", locale="zh",
                          mitm={"skip-server-cert-verify": True})
        db.add(p)
        await db.commit()

        # 覆盖前
        config_before = await generate_surge_config(db, profile_id=p.id)
        assert "skip-server-cert-verify = true" in self._extract_mitm(config_before)

        # 重置
        p.mitm = {}
        await db.commit()

        # 覆盖后
        config_after = await generate_surge_config(db, profile_id=p.id)
        assert "skip-server-cert-verify = false" in self._extract_mitm(config_after)

    @pytest.mark.asyncio
    async def test_no_profile_still_has_mitm_section(self, db):
        """无 profile 时 [MITM] 段仍然输出默认值"""
        config = await generate_surge_config(db, profile_id=None)
        assert "[MITM]" in config

    # ── helpers ──

    @staticmethod
    def _extract_mitm(config: str) -> list[str]:
        """从完整配置中提取 [MITM] 段的行"""
        lines = config.split("\n")
        in_mitm = False
        result = []
        for line in lines:
            if line.strip() == "[MITM]":
                in_mitm = True
                continue
            if in_mitm:
                if line.startswith("[") or line.strip() == "":
                    break
                result.append(line.strip())
        return result


# ════════════════════════════════════════════
# 三、MITM API 契约：configured 状态 + 覆盖式写入（Task 5）
# ════════════════════════════════════════════


class TestMitmApiContract:
    """PUT /profiles/{id} 的 MITM 覆盖式语义与详情 configured 投影"""

    BASE = "/api/configs/profiles/1"

    @pytest.mark.asyncio
    async def test_detail_never_echoes_secrets(self, admin_client):
        """写入秘密后详情仅返回 configured；响应文本不含明文"""
        resp = await admin_client.put(self.BASE, json={
            "mitm": {"ca-passphrase": "top-secret-pass", "ca-p12": _synthetic("ca-p12"), "hostname": "x.com"},
        })
        assert resp.status_code == 200
        detail = (await admin_client.get(self.BASE)).json()
        assert detail["mitm"]["ca-passphrase"] == {"configured": True}
        assert detail["mitm"]["ca-p12"] == {"configured": True}
        assert detail["mitm"]["hostname"] == "x.com"
        dumped = json.dumps(detail)
        assert "top-secret-pass" not in dumped and _synthetic("ca-p12") not in dumped

    @pytest.mark.asyncio
    async def test_empty_secret_input_keeps_current_value(self, admin_client):
        """空字符串 / null / configured 回显对象三种空输入都保留现值"""
        await admin_client.put(self.BASE, json={
            "mitm": {"ca-passphrase": "keep-me", "hostname": "keep.example"}})
        for empty in ("", None, {"configured": True}):
            resp = await admin_client.put(self.BASE, json={"mitm": {"ca-passphrase": empty}})
            assert resp.status_code == 200
        detail = (await admin_client.get(self.BASE)).json()
        assert detail["mitm"]["ca-passphrase"] == {"configured": True}
        assert detail["mitm"]["hostname"] == "keep.example"

    @pytest.mark.asyncio
    async def test_non_secret_keys_overwrite_normally(self, admin_client):
        """非敏感键按传入值覆盖（含清除为空）"""
        await admin_client.put(self.BASE, json={"mitm": {"hostname": "a.com"}})
        await admin_client.put(self.BASE, json={"mitm": {"hostname": "", "h2": False}})
        detail = (await admin_client.get(self.BASE)).json()
        assert detail["mitm"]["hostname"] == ""
        assert detail["mitm"]["h2"] is False

    @pytest.mark.asyncio
    async def test_mitm_clear_is_explicit_and_scoped(self, admin_client):
        """mitm_clear 只清除指定键，其余保持"""
        await admin_client.put(self.BASE, json={
            "mitm": {"ca-passphrase": "p", "ca-p12": "c", "hostname": "h.com"}})
        resp = await admin_client.put(self.BASE, json={"mitm_clear": ["ca-passphrase"]})
        assert resp.status_code == 200
        detail = (await admin_client.get(self.BASE)).json()
        assert detail["mitm"]["ca-passphrase"] == {"configured": False}
        assert detail["mitm"]["ca-p12"] == {"configured": True}
        assert detail["mitm"]["hostname"] == "h.com"

    @pytest.mark.asyncio
    async def test_mitm_reset_clears_everything(self, admin_client):
        """mitm_reset 独立动作：整体回到未覆盖状态（configured 全 False）"""
        await admin_client.put(self.BASE, json={
            "mitm": {"ca-passphrase": "p", "hostname": "h.com", "h2": True}})
        resp = await admin_client.put(self.BASE, json={"mitm_reset": True})
        assert resp.status_code == 200
        detail = (await admin_client.get(self.BASE)).json()
        assert detail["mitm"] == {"ca-passphrase": {"configured": False},
                                  "ca-p12": {"configured": False}}

    @pytest.mark.asyncio
    async def test_cleared_mitm_still_generates_default_section(self, admin_client):
        """秘密清除/重置后生成器仍回退 DEFAULT_MITM，不受 configured 投影影响"""
        await admin_client.put(self.BASE, json={"mitm_reset": True})
        resp = await admin_client.get("/api/configs/preview?profile_id=1")
        assert resp.status_code == 200
        assert "[MITM]" in resp.json()["content"]
