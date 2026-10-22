"""governance 作为 Django app 注册。

为什么需要它：``manage.py guards`` 这条管理命令住在 ``governance/management/commands/``。
Django 只会在 ``INSTALLED_APPS`` 里的 app 下发现管理命令，所以 governance 必须注册。

它**不定义任何模型**（``models.py`` 故意不存在），因此不会产生迁移，
也不会凭空多出一张表——注册它只是为了"命令能被发现"和"将来放 app 级配置"。
"""

from __future__ import annotations

from django.apps import AppConfig


class GovernanceConfig(AppConfig):
    name = "governance"
    label = "governance"
    verbose_name = "不可篡改治理层"
    default_auto_field = "django.db.models.BigAutoField"
