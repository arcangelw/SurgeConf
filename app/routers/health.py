"""最小 readiness 端点：launchd 事务切换的提交门。

唯一语义：lifespan 完整成功（迁移/绑定/维护/调度器全部就绪）后
``app.state.service_ready`` 为 True，此时 GET 返回 204、无 body、
无版本/配置信息；标志缺失或 False（启动未完成/失败/清理期）一律
503 fail closed。该路径在访问矩阵中公开（GET 安全方法、无 cookie/
CSRF 要求），local_only 下非回环仍被中间件拒绝——供 surgeconf.sh
在本机以 curl 探测候选实例是否真正可服务。
"""

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/api/health/ready")
async def health_ready(request: Request) -> Response:
    if not getattr(request.app.state, "service_ready", False):
        return Response(status_code=503)
    return Response(status_code=204)
