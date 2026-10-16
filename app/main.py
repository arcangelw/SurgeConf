"""SurgeConf - Surge 配置管理服务（装配层：app 实例、访问边界中间件、路由挂载、静态文件）"""

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap import lifespan
from app.pages import router as pages_router
from app.routers import auth, subscriptions, nodes, groups, configs, general
from app.routers import settings as settings_router
from app.security import (COOKIE_NAME, SESSION_MAX_AGE, Decision,
                          load_access_settings, require_admin_request)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

app = FastAPI(
    title="SurgeConf",
    description="Surge 配置管理服务",
    version="2.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def admin_boundary_middleware(request: Request, call_next):
    """访问矩阵唯一入口（详见 ``app.security``）：

    local_only 非回环 403；lan_authenticated 未登录 API 401 / 页面 302；
    写方法必须带与会话一致的 CSRF；静态资源与登录端点公开。
    """
    try:
        settings = await load_access_settings()
    except Exception:
        # 数据库不可用时 fail closed，不泄露内部错误细节
        return JSONResponse(status_code=503, content={"detail": "Service unavailable"})

    decision = await require_admin_request(request, settings)
    if decision == Decision.FORBIDDEN:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    if decision == Decision.UNAUTHORIZED:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    if decision == Decision.CSRF_REJECTED:
        return JSONResponse(status_code=403,
                            content={"detail": "CSRF token missing or invalid"})
    if decision == Decision.REDIRECT_LOGIN:
        return RedirectResponse(url="/login", status_code=307)

    response = await call_next(request)
    # local_only 回环安全方法签发的隐式管理会话（非回环请求不可能到达此处）
    implicit_token = getattr(request.state, "issue_implicit_session", None)
    if implicit_token is not None:
        response.set_cookie(
            COOKIE_NAME, implicit_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            path="/",
            secure=request.url.scheme == "https",
        )
    return response


app.include_router(subscriptions.router)
app.include_router(nodes.router)
app.include_router(groups.router)
app.include_router(configs.router)
app.include_router(general.router)
app.include_router(settings_router.router)
app.include_router(auth.router)
app.include_router(pages_router)

static_dir = os.path.join(BASE_DIR, "app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
