"""存活/就绪探针。就绪探针会真的碰一次数据库，不用假的 OK 糊过去。

**返回体只带状态词**：探针端点对未认证调用方开放（容器 HEALTHCHECK 没有凭据），
所以不能把数据库异常文本、未应用迁移名、向量库绝对路径回给调用方——这些细节
一律写服务端日志，响应里只保留 ``ok`` / ``error`` / ``pending`` / ``missing``。
"""

from __future__ import annotations

import logging

from django.db import connections
from django.db.utils import OperationalError
from django.urls import path
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from config.paths import vector_data_dir

logger = logging.getLogger(__name__)


def _pending_migrations() -> list[str]:
    """返回未应用的迁移标签；空列表表示库结构与代码一致。

    **为什么需要**：只 `SELECT 1` 的话，一个从没跑过 `migrate` 的空库也是 200 ——
    容器报 healthy，但用户一访问就 500（部署审查发现的假绿灯）。
    """
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connections["default"])
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


class LivenessView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        return Response({"status": "ok"})


class ReadinessView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        checks = {"database": "ok", "migrations": "ok", "vector_data": "ok"}
        status_code = 200
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except OperationalError as exc:
            # 异常原文可能带主机/驱动/账号信息，只写日志，不进响应体。
            logger.warning("readiness: 数据库不可用：%s", exc)
            checks["database"] = "error"
            status_code = 503
        else:
            # 能连上才算"结构对不对"有意义；连不上时上面已经 503 了。
            try:
                pending = _pending_migrations()
            except Exception as exc:  # noqa: BLE001 —— 探针自身异常也要如实报，不能变成 500
                logger.warning("readiness: 读取迁移计划失败：%s", exc)
                checks["migrations"] = "error"
                status_code = 503
            else:
                if pending:
                    logger.warning(
                        "readiness: %d 个迁移未应用（例：%s）", len(pending), pending[0]
                    )
                    checks["migrations"] = "pending"
                    status_code = 503
        target = vector_data_dir()
        if not target.is_dir():
            logger.warning("readiness: 向量数据目录不存在：%s", target)
            checks["vector_data"] = "missing"
            status_code = 503
        return Response(
            {"status": "ok" if status_code == 200 else "degraded", "checks": checks},
            status=status_code,
        )


urlpatterns = [
    path("live/", LivenessView.as_view(), name="live"),
    path("ready/", ReadinessView.as_view(), name="ready"),
]
