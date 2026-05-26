"""SurgeConf - Surge 配置管理服务"""

import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

from app.database import init_db, DB_PATH
from app.models import RegionGroup, ServiceGroup, RuleSource, ConfigProfile, CustomRule, HostMapping
from app.default_config import (
    DEFAULT_REGION_GROUPS, DEFAULT_SERVICE_GROUPS,
    DEFAULT_RULE_SOURCES, DEFAULT_GENERAL,
    DEFAULT_URL_REWRITES, DEFAULT_HEADER_REWRITES,
    DEFAULT_MITM, DEFAULT_PRE_RULES, DEFAULT_GENERAL_RULES,
    DEFAULT_HOST_MAPPINGS,
)
from app.i18n import get_translations, DEFAULT_LOCALE
from app.default_config import LOCALE_NAMES
from app.routers import subscriptions, nodes, groups, configs, general

from sqlalchemy.ext.asyncio import async_sessionmaker
from app.database import engine

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


async def seed_defaults():
    """首次启动时填充默认数据"""
    from sqlalchemy import select

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as db:
        result = await db.execute(select(RegionGroup).limit(1))
        if result.scalar_one_or_none():
            return

        for rg in DEFAULT_REGION_GROUPS:
            db.add(RegionGroup(**rg))

        for sg in DEFAULT_SERVICE_GROUPS:
            db.add(ServiceGroup(**sg))

        for rs in DEFAULT_RULE_SOURCES:
            db.add(RuleSource(**rs))

        db.add(ConfigProfile(
            name="默认配置",
            description="基于原始 Surge.conf 的默认配置",
            is_default=True,
            locale="zh",
            final_action="兜底",
            general=dict(DEFAULT_GENERAL),
            url_rewrites=list(DEFAULT_URL_REWRITES),
            header_rewrites=list(DEFAULT_HEADER_REWRITES),
            mitm=dict(DEFAULT_MITM),
        ))

        for r in DEFAULT_PRE_RULES:
            db.add(CustomRule(profile_id=None, **r))
        for r in DEFAULT_GENERAL_RULES:
            db.add(CustomRule(profile_id=None, **r))
        for h in DEFAULT_HOST_MAPPINGS:
            db.add(HostMapping(profile_id=None, **h))

        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = """
╔══════════════════════════════════════════════════════════╗
║                     SurgeConf                           ║
║                                                         ║
║  免责声明:                                               ║
║  本工具仅用于学习和研究 Surge 配置管理技术。               ║
║  使用前请阅读完整的免责声明:                              ║
║  github.com/arcangelw/SurgeConf#免责声明                ║
║                                                         ║
║  继续使用即表示您接受免责声明的全部条款。                  ║
╚══════════════════════════════════════════════════════════╝
"""
    print(banner, file=sys.stderr)
    await init_db()
    await seed_defaults()
    yield


app = FastAPI(
    title="SurgeConf",
    description="Surge 配置管理服务",
    version="2.0.0",
    lifespan=lifespan,
)

# 可选的静态 API Token 认证（通过环境变量 SURGE_API_TOKEN 启用）
_API_TOKEN = os.environ.get("SURGE_API_TOKEN") or ""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _API_TOKEN:
        # 跳过静态文件和页面路由的认证
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/subscriptions", "/nodes", "/groups",
                                                     "/rules", "/profiles", "/generator",
                                                     "/general", "/settings", "/api/settings/locale",
                                                     "/docs", "/openapi.json"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_API_TOKEN}":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


app.include_router(subscriptions.router)
app.include_router(nodes.router)
app.include_router(groups.router)
app.include_router(configs.router)
app.include_router(general.router)

static_dir = os.path.join(BASE_DIR, "app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))


def _get_locale(request: Request) -> str:
    return request.cookies.get("locale", DEFAULT_LOCALE)


def _render(request: Request, template: str, extra: dict | None = None):
    locale = _get_locale(request)
    ctx = {
        "request": request,
        "t": get_translations(locale),
        "locale": locale,
        "locale_names": LOCALE_NAMES if locale == "en" else {},
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(template, ctx)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _render(request, "index.html")


@app.get("/subscriptions", response_class=HTMLResponse)
async def page_subscriptions(request: Request):
    return _render(request, "subscriptions.html")


@app.get("/nodes", response_class=HTMLResponse)
async def page_nodes(request: Request):
    return _render(request, "nodes.html")


@app.get("/groups", response_class=HTMLResponse)
async def page_groups(request: Request):
    return _render(request, "groups.html")


@app.get("/rules", response_class=HTMLResponse)
async def page_rules(request: Request):
    return _render(request, "rules.html")


@app.get("/profiles", response_class=HTMLResponse)
async def page_profiles(request: Request):
    return _render(request, "profiles.html")


@app.get("/generator", response_class=HTMLResponse)
async def page_generator(request: Request):
    return _render(request, "generator.html")


@app.get("/general", response_class=HTMLResponse)
async def page_general(request: Request):
    return _render(request, "general.html")


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return _render(request, "settings.html")


@app.post("/api/settings/locale")
async def set_locale(request: Request):
    body = await request.json()
    locale = body.get("locale", DEFAULT_LOCALE)
    response = Response(status_code=200)
    response.set_cookie("locale", locale, max_age=365 * 86400)
    return response


@app.post("/api/settings/reset")
async def reset_data():
    """重置所有数据：删除数据库文件并重新初始化"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    await init_db()
    await seed_defaults()
    return {"reset": True}
