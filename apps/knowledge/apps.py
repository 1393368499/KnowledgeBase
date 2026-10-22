from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_db_guards(sender, **kwargs) -> None:
    """每次 ``migrate`` 之后确保数据库触发器在位。

    迁移 ``knowledge.0002`` 已经装过一次；这里再存一次的原因有两个：
    1. 测试库重建（``pytest-django``）走的是 migrate 流程，信号是最后的兜底；
    2. 触发器被人为删掉后，重新 ``migrate`` 就能恢复。
    """
    from governance import triggers

    triggers.create_guards()


class KnowledgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.knowledge"
    label = "knowledge"
    verbose_name = "知识库"

    def ready(self) -> None:  # pragma: no cover - 装配代码
        post_migrate.connect(_install_db_guards, dispatch_uid="knowledge.install_db_guards")
