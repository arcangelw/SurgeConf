"""页面路由：渲染 9 个 Jinja2 模板页（路径与原 main.py 完全一致）。"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.i18n import get_translations, DEFAULT_LOCALE
from app.default_config import LOCALE_NAMES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    }
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(template, ctx)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _render(request, "index.html")


@router.get("/subscriptions", response_class=HTMLResponse)
async def page_subscriptions(request: Request):
    return _render(request, "subscriptions.html")


@router.get("/nodes", response_class=HTMLResponse)
async def page_nodes(request: Request):
    return _render(request, "nodes.html")


@router.get("/groups", response_class=HTMLResponse)
async def page_groups(request: Request):
    return _render(request, "groups.html")

@router.get("/proxies", response_class=HTMLResponse)
async def page_proxies(request: Request):
    return _render(request, "proxies.html")


@router.get("/rules", response_class=HTMLResponse)
async def page_rules(request: Request):
    return _render(request, "rules.html")


@router.get("/profiles", response_class=HTMLResponse)
async def page_profiles(request: Request):
    return _render(request, "profiles.html")


@router.get("/generator", response_class=HTMLResponse)
async def page_generator(request: Request):
    return _render(request, "generator.html")


@router.get("/general", response_class=HTMLResponse)
async def page_general(request: Request):
    return _render(request, "general.html")


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return _render(request, "settings.html")
