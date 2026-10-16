"""系统设置端点：语言切换、备份生命周期、数据重置、系统级配置。"""

import os
import stat as stat_mod

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.database import get_db
from app.i18n import DEFAULT_LOCALE
from app.models import SystemConfig
from app.services.backups import (create_backup, list_backups,
                                  reset_database, restore_backup)

router = APIRouter(prefix="/api/settings", tags=["系统设置"])


@router.post("/locale")
async def set_locale(request: Request):
    body = await request.json()
    locale = body.get("locale", DEFAULT_LOCALE)
    response = Response(status_code=200)
    response.set_cookie("locale", locale, max_age=365 * 86400)
    return response


class ConfirmRequest(BaseModel):
    """破坏性操作（恢复/重置/导出）的显式确认载荷"""
    confirm: bool = False


def _require_confirm(confirm: bool) -> None:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail='缺少确认参数：请求体需包含 {"confirm": true}',
        )


@router.post("/reset")
async def reset_data(confirm: ConfirmRequest):
    """重置所有数据（要求 ``{"confirm": true}``）。

    完整流程（dispose → 强制备份 → staging 重建 → 原子替换 → 重绑）
    见 ``app.services.backups.reset_database``。
    """
    _require_confirm(confirm.confirm)
    await reset_database()
    return {"reset": True}


# ===== 备份生命周期 =====


@router.post("/backups")
async def create_backup_now():
    """手动备份 live 库（SQLite Backup API 原子发布，最近 10 份保留）"""
    info = await create_backup("manual")
    return info.as_dict()


@router.get("/backups")
async def get_backups():
    """备份列表（时间降序）；只含文件名与元数据，不含绝对路径"""
    return [item.as_dict() for item in reversed(list_backups())]


@router.get("/backups/{name}")
async def download_backup(name: str):
    """下载备份：name 必须命中现有备份白名单（list_backups 已 refresh
    并逐项 lstat），命中后以 ``O_NOFOLLOW`` 打开同一 fd 交付——
    ``fstat`` 验证普通文件后由该 fd 流式读出，响应头（长度/文件名）
    全部来自 fstat 与白名单名，绝不按路径二次打开（无 FileResponse
    TOCTOU 窗口）。fd 在流结束与后台任务双路径幂等关闭。
    """
    for item in list_backups():
        if item.name != name:
            continue
        try:
            fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="备份不存在") from exc
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            os.close(fd)
            raise HTTPException(status_code=404, detail="备份不存在")

        closed = False

        def _close_fd() -> None:
            nonlocal closed
            if not closed:
                closed = True
                os.close(fd)

        def _stream():
            try:
                while True:
                    chunk = os.read(fd, 256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                _close_fd()

        return StreamingResponse(
            _stream(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(st.st_size),
                "Content-Disposition": f'attachment; filename="{item.name}"',
            },
            background=BackgroundTask(_close_fd),
        )
    raise HTTPException(status_code=404, detail="备份不存在")


@router.post("/backups/{name}/restore")
async def restore_backup_now(name: str, confirm: ConfirmRequest):
    """恢复备份（要求 ``{"confirm": true}``）：先强制备份当前 live 再替换

    失败时原 live 库保持可用并向上传播异常，不返回半成功。
    """
    _require_confirm(confirm.confirm)
    try:
        await restore_backup(name)
    except LookupError:
        raise HTTPException(status_code=404, detail="备份不存在")
    return {"restored": name}


# ===== 系统级配置（排除关键词、其他分组等） =====

# 访问控制键由 PUT /api/settings/access 专用端点管理，
# 防止批量端点读出密码哈希或绕过密码验证改写访问模式
PROTECTED_CONFIG_KEYS = {"installation_role", "admin_access", "admin_password_hash"}


@router.get("/config")
async def get_sys_config(db=Depends(get_db)):
    """获取所有系统级配置（访问控制键除外）"""
    result = await db.execute(select(SystemConfig))
    configs = {row.key: row.value for row in result.scalars().all()}
    return {k: v for k, v in configs.items() if k not in PROTECTED_CONFIG_KEYS}



class SysConfigBatchUpdate(BaseModel):
    configs: dict[str, str]

@router.put("/config/batch")
async def update_sys_config_batch(data: SysConfigBatchUpdate, db=Depends(get_db)):
    """批量 upsert 系统配置：单事务单 commit（访问控制键走专用端点）"""
    if not data.configs:
        return {"updated": 0}
    if PROTECTED_CONFIG_KEYS & data.configs.keys():
        return JSONResponse(status_code=400,
                            content={"detail": "访问控制配置请使用 PUT /api/settings/access"})
    result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(data.configs.keys())))
    existing = {cfg.key: cfg for cfg in result.scalars().all()}
    for key, value in data.configs.items():
        cfg = existing.get(key)
        if cfg:
            cfg.value = value
        else:
            db.add(SystemConfig(key=key, value=value))
    await db.commit()
    return {"updated": len(data.configs)}
