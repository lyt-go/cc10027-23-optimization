"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

交付路由必须排在通用 ``/api/{module}`` 之前，否则 ``/api/delivery/faults``
这类固定路径会被动态路径抢先匹配。
"""
from __future__ import annotations

from app.routers import delivery, resource

ROUTERS = [delivery, resource]
