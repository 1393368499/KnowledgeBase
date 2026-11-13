"""worker 崩溃恢复相关的配置不变量（对应 `docs/delivery-status-2026-10-15.md` §5.2 的实测）。

实测背景（`artifacts/acceptance/crash-case-b-*.json`）：
  * 杀掉 worker 的**池子进程** → 父进程仍在 → `task_reject_on_worker_lost` 立刻重投（40/40、3.5s）；
  * **硬杀整个容器** → 父进程一起没了 → 只能等 kombu 的 `visibility_timeout`
    （kombu `transport/redis.py` L369/L415/L419，默认 3600s）→ 实测 4 条在飞消息
    还要等 3554s 才回来，重启的 worker 只能处理队列里的其余 196 条。

这两个测试锁住修复后的**关系**（而不是某个魔法数），以后谁把超时调坏都会红：
  1. `visibility_timeout > 任务硬超时` —— 否则还活着的任务会被重复投递（同一份资料被处理两遍）；
  2. `软超时 < 硬超时`、且硬超时**有界** —— 否则恢复 SLA 无从谈起（任务可能永远不返回）。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from django.conf import settings


def _env_defaults() -> dict[str, str]:
    return {
        "DJANGO_SETTINGS_MODULE": "knowledgebase.settings",
        "KB_SKIP_DOTENV": "1",
        "KB_PYTEST_DB": "sqlite",
    }


def test_visibility_timeout_exceeds_hard_time_limit() -> None:
    """重新投递必须晚于任务硬超时，否则"还活着的任务"会被重复投递。"""
    limit = settings.CELERY_TASK_TIME_LIMIT
    transport = settings.CELERY_BROKER_TRANSPORT_OPTIONS
    visibility = transport["visibility_timeout"]
    assert isinstance(limit, int) and limit > 0
    assert visibility > limit, (
        f"visibility_timeout({visibility}) 必须大于 CELERY_TASK_TIME_LIMIT({limit})"
    )


def test_soft_time_limit_below_hard_limit() -> None:
    assert 0 < settings.CELERY_TASK_SOFT_TIME_LIMIT < settings.CELERY_TASK_TIME_LIMIT


def test_recovery_sla_is_bounded() -> None:
    """硬超时有界：崩溃后最长等待时间必须是个"可以讲给运维听"的值（< 1 小时）。"""
    assert settings.CELERY_TASK_TIME_LIMIT <= 3600
    assert settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"] <= 3600


def test_default_visibility_is_hard_limit_plus_slack() -> None:
    """未用环境变量覆盖时，默认值是"硬超时 + 300s 余量"（与实现一致）。"""
    if os.environ.get("KB_CELERY_VISIBILITY_TIMEOUT"):
        pytest.skip("环境变量已覆盖 visibility_timeout")
    assert (
        settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"]
        == settings.CELERY_TASK_TIME_LIMIT + 300
    )


def test_celery_app_conf_receives_both_settings() -> None:
    """`app.config_from_object(...namespace="CELERY")` 必须真的把它们交给 Celery。"""
    from knowledgebase.celery import app

    assert app.conf.broker_transport_options["visibility_timeout"] == (
        settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"]
    )
    assert app.conf.task_time_limit == settings.CELERY_TASK_TIME_LIMIT
    assert app.conf.task_soft_time_limit == settings.CELERY_TASK_SOFT_TIME_LIMIT


def test_env_overrides_are_honoured() -> None:
    """三个超时都要能用环境变量覆盖（运维在服务器上必须能调，不必改代码）。"""
    code = (
        "import django;django.setup();"
        "from django.conf import settings;"
        "print(settings.CELERY_TASK_TIME_LIMIT,"
        "settings.CELERY_TASK_SOFT_TIME_LIMIT,"
        "settings.CELERY_BROKER_TRANSPORT_OPTIONS['visibility_timeout'])"
    )
    env = {
        **os.environ,
        **_env_defaults(),
        "KB_CELERY_TASK_TIME_LIMIT": "120",
        "KB_CELERY_VISIBILITY_TIMEOUT": "180",
    }
    env.pop("KB_CELERY_TASK_SOFT_TIME_LIMIT", None)  # 未设时必须跟着硬超时派生
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    # 软超时未设时按硬超时的 4/5 派生（120 → 96），且必须是正数
    expected_soft = max(1, 120 * 4 // 5)
    assert completed.stdout.split() == ["120", str(expected_soft), "180"]
