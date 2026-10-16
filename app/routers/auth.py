"""管理认证端点：登录、登出、会话查询与访问模式切换。

公开端点（登录/登出/会话查询）不读取任何管理业务数据；
``PUT /api/settings/access`` 受访问矩阵与 CSRF 保护（见 ``app.security``）。
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database import get_db
from app.models import SystemConfig
from app.security import (COOKIE_NAME, SESSION_MAX_AGE,
                          get_session_signer, hash_admin_password,
                          load_access_settings, login_throttle_wait,
                          record_login_failure, record_login_success,
                          require_csrf, verify_admin_password)

# 全局 KDF 并发上限：登录 scrypt 校验全部卸载到线程池执行
_KDF_SEMAPHORE = asyncio.Semaphore(4)


async def _verify_password_off_loop(password: str, encoded: str) -> bool:
    """scrypt 校验限并发并卸载到线程，避免阻塞事件循环"""
    async with _KDF_SEMAPHORE:
        return await asyncio.to_thread(verify_admin_password, password, encoded)


router = APIRouter(tags=["认证"])


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
        secure=request.url.scheme == "https",
    )


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=128)


@router.post("/api/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    # 限流按直接 TCP 对端计数，不信任任何转发头
    wait_seconds = login_throttle_wait(request)
    if wait_seconds is not None:
        return JSONResponse(status_code=429,
                            content={"detail": "尝试次数过多，请稍后再试"},
                            headers={"Retry-After": str(wait_seconds)})
    settings = await load_access_settings()
    if not settings.password_hash:
        return JSONResponse(status_code=403,
                            content={"detail": "未设置管理密码"})
    if not await _verify_password_off_loop(body.password, settings.password_hash):
        record_login_failure(request)
        return JSONResponse(status_code=401, content={"detail": "密码错误"})
    record_login_success(request)
    token, csrf = get_session_signer().issue(loopback_implicit=False)
    _set_session_cookie(response, token, request)
    return {
        "authenticated": True,
        "csrf_token": csrf,
        "admin_access": settings.admin_access,
        "installation_role": settings.installation_role,
    }


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    # 非公开端点：中间件已要求有效会话 + CSRF，此处防御性复查
    if not require_csrf(request):
        return JSONResponse(status_code=403,
                            content={"detail": "CSRF token missing or invalid"})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                           samesite="strict",
                           secure=request.url.scheme == "https")
    return {"authenticated": False}


@router.get("/api/auth/session")
async def session_info(request: Request):
    from app.security import resolve_session_for
    settings = await load_access_settings()
    session = resolve_session_for(request, settings)
    if session is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "csrf_token": session["csrf"],
        "admin_access": settings.admin_access,
        "installation_role": settings.installation_role,
        "password_configured": settings.password_configured,
    }


class AccessUpdate(BaseModel):
    installation_role: Literal["home_gateway"]
    admin_access: Literal["local_only", "lan_authenticated"]
    current_password: str | None = Field(default=None, max_length=128)
    new_password: str | None = Field(default=None, max_length=128)


@router.put("/api/settings/access")
async def update_access(body: AccessUpdate, request: Request, response: Response,
                        db=Depends(get_db)):
    """访问模式切换与管理密码设置。

    首次启用 ``lan_authenticated`` 必须同时设置密码；
    已配置密码后任何修改（含切换模式）都要求验证当前密码。
    响应只返回角色、访问模式与 ``password_configured``，绝不返回哈希。
    """
    # 中间件放行后有效会话挂在 request.state；显式复查兜底
    if not require_csrf(request):
        return JSONResponse(status_code=403,
                            content={"detail": "CSRF token missing or invalid"})

    settings = await load_access_settings()

    if settings.password_configured:
        if not body.current_password or not await _verify_password_off_loop(
                body.current_password, settings.password_hash or ""):
            return JSONResponse(status_code=403, content={"detail": "当前密码错误"})
    elif body.current_password:
        return JSONResponse(status_code=400,
                            content={"detail": "尚未设置管理密码，无需提供当前密码"})

    if body.admin_access == "lan_authenticated" and not settings.password_configured:
        if not body.new_password:
            return JSONResponse(status_code=400,
                                content={"detail": "首次启用局域网管理必须设置密码"})

    new_hash = settings.password_hash
    if body.new_password is not None:
        if not body.new_password.strip():
            return JSONResponse(status_code=400, content={"detail": "新密码不能为空"})
        new_hash = hash_admin_password(body.new_password)

    values = {
        "installation_role": body.installation_role,
        "admin_access": body.admin_access,
        "admin_password_hash": new_hash,
    }
    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key.in_(values.keys())))
    existing = {row.key: row for row in result.scalars().all()}
    for key, value in values.items():
        row = existing.get(key)
        if row:
            row.value = value
        else:
            db.add(SystemConfig(key=key, value=value))
    await db.commit()
    # 切换到 lan_authenticated 时，请求若携带回环隐式会话则一并清除
    # （隐式会话在新模式下立即失效，浏览器侧也去掉无效 Cookie）
    if body.admin_access == "lan_authenticated":
        token = request.cookies.get(COOKIE_NAME)
        if token:
            signer = get_session_signer()
            if signer.parse(token, loopback_implicit=False) is None:
                response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                                       samesite="strict",
                                       secure=request.url.scheme == "https")
    return {
        "installation_role": body.installation_role,
        "admin_access": body.admin_access,
        "password_configured": bool(new_hash),
    }
