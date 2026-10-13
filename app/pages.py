"""页面路由：渲染 5 个 Jinja2 模板页（按 Surge 功能模块组织）。"""

import os
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.i18n import get_translations, DEFAULT_LOCALE
from app.default_config import LOCALE_NAMES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _asset_version() -> str:
    """git HEAD 短哈希作静态资源版本戳，无 git 环境回落 mtime。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(BASE_DIR), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return str(int(os.path.getmtime(os.path.join(BASE_DIR, "static", "js", "app.js"))))


ASSET_VER = _asset_version()

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(BASE_DIR), "app", "templates"))

router = APIRouter()


def _get_locale(request: Request) -> str:
    return request.cookies.get("locale", DEFAULT_LOCALE)


def _render(request: Request, template: str, extra: dict | None = None):
    locale = _get_locale(request)
    ctx = {
        "request": request,
        "t": get_translations(locale),
        "locale": locale,
        "locale_names": LOCALE_NAMES,
        "asset_ver": ASSET_VER,
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(template, ctx)



@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _render(request, "index.html")


@router.get("/proxies", response_class=HTMLResponse)
async def page_proxies(request: Request):
    return _render(request, "proxies.html")


@router.get("/rules", response_class=HTMLResponse)
async def page_rules(request: Request):
    return _render(request, "rules.html")


@router.get("/profiles", response_class=HTMLResponse)
async def page_profiles(request: Request):
    return _render(request, "profiles.html")


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return _render(request, "settings.html")
