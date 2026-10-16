"""UDP 管理 UI 回归（Task 5）：页面骨架、指南路由与 i18n 完整性。

只断言用户可见行为：/profiles 页渲染 UDP 例外弹窗骨架（含覆盖式编辑
所需元素 id）、/guide/udp-compatibility 可达且渲染仓库文档、UDP 页签
与例外相关 i18n 键在 zh/en 均完整（缺失键会渲染 literal key）。
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from app.i18n import TRANSLATIONS

# UDP 页签 + 例外弹窗 + 目标感知说明的完整键面（zh/en 都必须存在）
UDP_UI_I18N_KEYS = (
    "profile.tab_udp",
    "profile.udp_mode_save",
    "profile.udp_exceptions_title",
    "profile.udp_add",
    "profile.udp_col_value",
    "profile.udp_col_display_name",
    "profile.udp_col_sort",
    "profile.udp_hint_ws",
    "profile.udp_hint_gw",
    "profile.udp_fastpath_title",
    "profile.udp_fastpath_note",
    "profile.udp_guide_link",
    "profile.udp_scoped_warning",
    "profile.udp_ios_note",
    "profile.udp_ios_webrtc_note",
    "profile.udp_exc_new",
    "profile.udp_value_label_process",
    "profile.udp_value_label_mac",
    "profile.udp_placeholder_process",
    "profile.udp_placeholder_mac",
    "profile.udp_hint_process_format",
    "profile.udp_hint_mac_format",
    "profile.udp_keep_placeholder",
    "profile.udp_keep_hint",
    "profile.udp_current_masked_label",
    "profile.udp_display_name_label",
    "profile.udp_display_name_placeholder",
    "profile.udp_enabled_label",
    "profile.udp_sort_label",
    "profile.udp_confirm_delete",
)


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """独立临时库 + 手动驱动 lifespan（与 test_api_endpoints 同一装配）"""
    import re
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
                assert meta, "回环 GET / 应签发隐式会话并输出 CSRF meta"
                c.headers["X-CSRF-Token"] = meta.group(1)
                yield c
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_profiles_page_renders_udp_dialog_skeleton(client):
    """配置方案页包含 UDP 例外弹窗骨架：覆盖式编辑与生命周期清空所依赖的元素 id"""
    page = await client.get("/profiles")
    assert page.status_code == 200
    for fragment in (
        'id="udp-exception-modal"',
        'id="udp-exc-edit-id"',
        'id="udp-exc-profile-id"',
        'id="udp-exc-value"',
        'id="udp-exc-value-label"',
        'id="udp-exc-value-hint"',
        'id="udp-exc-current-row"',
        'id="udp-exc-current-masked"',
        'id="udp-exc-display-name"',
        'id="udp-exc-enabled"',
        'id="udp-exc-sort"',
        'onclick="closeUdpExceptionDialog()"',
    ):
        assert fragment in page.text, fragment


@pytest.mark.asyncio
async def test_udp_compatibility_guide_route_renders_doc(client):
    """UDP 兼容性指南路由：渲染 docs/UDP_COMPATIBILITY.md 且携带关键小节标题"""
    resp = await client.get("/guide/udp-compatibility")
    assert resp.status_code == 200
    assert "docs/UDP_COMPATIBILITY.md" in resp.text
    for section in ("Fast Path", "WebRTC", "udp-policy-not-supported-behaviour"):
        assert section in resp.text, section
    # 未知指南 404（与 profile-targets 同语义）
    assert (await client.get("/guide/not-exist")).status_code == 404


@pytest.mark.asyncio
async def test_guide_udp_doc_open_source_placeholders_only(client):
    """指南正文只使用占位示例值（ExampleVideo / 02:00:00:00:00:01）"""
    resp = await client.get("/guide/udp-compatibility")
    assert resp.status_code == 200
    assert "ExampleVideo" in resp.text
    assert "02:00:00:00:00:01" in resp.text


def test_udp_ui_i18n_keys_complete_both_locales():
    """UDP 管理 i18n 键 zh/en 均存在且非空（缺失键会在界面渲染 literal key）"""
    for locale in ("zh", "en"):
        table = TRANSLATIONS[locale]
        for key in UDP_UI_I18N_KEYS:
            assert table.get(key), f"{locale} 缺失 {key}"


def test_i18n_locales_have_identical_key_sets():
    """zh/en 翻译表键集合一致（新增键只写一侧会静默漏翻）"""
    zh, en = set(TRANSLATIONS["zh"]), set(TRANSLATIONS["en"])
    assert zh - en == set(), f"en 缺少: {sorted(zh - en)}"
    assert en - zh == set(), f"zh 缺少: {sorted(en - zh)}"
