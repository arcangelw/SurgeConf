"""SurgeConf - Surge 配置管理服务（装配层：app 实例、认证中间件、路由挂载、静态文件）"""

import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.bootstrap import lifespan
from app.pages import router as pages_router
from app.routers import subscriptions, nodes, groups, configs, general
from app.routers import settings as settings_router

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

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
        if path.startswith("/static/") or path in ("/", "/proxies", "/rules", "/profiles",
                                                     "/settings", "/api/settings/locale",
                                                     "/docs", "/openapi.json"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_API_TOKEN}":
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


app.include_router(subscriptions.router)
app.include_router(nodes.router)
app.include_router(groups.router)
app.include_router(configs.router)
app.include_router(general.router)
app.include_router(settings_router.router)
app.include_router(pages_router)

static_dir = os.path.join(BASE_DIR, "app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
