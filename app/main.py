"""SurgeConf - Surge 配置管理服务（装配层：app 实例、访问边界中间件、路由挂载、静态文件）"""

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap import lifespan
from app.pages import router as pages_router
from app.routers import (auth, subscriptions, nodes, groups, configs, general,
                         health, udp)
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

# 校验错误的静态中文消息映射（按 Pydantic v2 error type）。
# 绝不透传动态 msg —— 自定义 validator 的 ValueError 消息可能拼入
# 原始输入值；未知 type 统一兜底文案，*_type 归为类型错误。
_VALIDATION_MESSAGES = {
    "missing": "缺少必填字段",
    "enum": "字段值不在允许的取值范围内",
    "string_too_short": "字符串长度不符合要求",
    "string_too_long": "字符串长度不符合要求",
    "string_pattern_mismatch": "字段格式不符合要求",
    "value_error": "字段值不合法",
    "json_invalid": "请求体不是合法 JSON",
    "greater_than_equal": "数值不在允许范围内",
    "less_than_equal": "数值不在允许范围内",
}


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """校验错误最小披露：只保留字段路径/类型与静态映射消息。

    Pydantic v2 默认错误结构携带原始 ``input``、``ctx`` 与动态 ``msg``
    （如超长 match_value 的完整原值、枚举合法值列表、自定义 validator
    拼入的输入值），任何端点的 422 都不得回显请求体内容。
    """
    errors = []
    for err in exc.errors():
        err_type = str(err.get("type", ""))
        message = _VALIDATION_MESSAGES.get(err_type)
        if message is None:
            message = ("字段类型不正确"
                       if err_type.endswith(("_type", "_parsing"))
                       else "输入校验失败")
        errors.append({"loc": list(err.get("loc", ())), "type": err_type,
                       "msg": message})
    return JSONResponse(status_code=422, content={"detail": errors})

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
app.include_router(udp.router)
app.include_router(general.router)
app.include_router(health.router)
app.include_router(settings_router.router)
app.include_router(auth.router)
app.include_router(pages_router)

static_dir = os.path.join(BASE_DIR, "app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
