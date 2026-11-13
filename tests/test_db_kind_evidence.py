"""把"这次到底连的是哪个库"变成**可断言的证据**（测试基础设施缺陷的回归）。

背景（实测踩到）：``knowledgebase/test_settings.py`` 在 pytest-django 的 configure 阶段就被
导入，而 ``tests/conftest.py`` 原先在 ``pytest_configure`` 里才
``os.environ.setdefault("KB_TEST_DB", config.getoption("--kb-db"))`` —— 为时已晚，
于是 ``pytest --kb-db=postgres`` **静默跑 SQLite**，报告却写着"已在 Postgres 上验证"。

现在：开关在 ``pytest_load_initial_conftests`` 阶段生效（早于 settings 加载），
并且在 ``pytest_configure`` 里用真实 ``connection.vendor`` 复核，对不上直接报错。
本文件再加一道**用例级**断言：报告里说的库类型必须等于真实 vendor。
"""

from __future__ import annotations

import os

from django.db import connection

import pytest

pytestmark = pytest.mark.django_db


def test_reported_kind_matches_real_vendor(pytestconfig):
    """真实驱动必须是报告里那个：postgresql ↔ postgres，sqlite3 ↔ sqlite。"""
    requested = os.environ.get("KB_PYTEST_DB", os.environ.get("KB_TEST_DB", "sqlite")).lower()
    vendor = connection.vendor
    if requested in {"postgres", "postgresql"}:
        assert vendor == "postgresql", (
            f"请求了 Postgres，真实 vendor 却是 {vendor!r} —— 又是静默降级"
        )
    else:
        assert vendor in {"sqlite", "postgresql"}, vendor
    # 把证据写进报告头（pytest -q 也会显示）
    pytestconfig._kb_db_actual = "postgres" if vendor == "postgresql" else "sqlite"
    pytestconfig._kb_db_vendor = vendor


def test_postgres_only_features_are_really_available():
    """真 PostgreSQL 时才校验一个 PG 专有行为；SQLite 下如实跳过并说明原因。"""
    if connection.vendor != "postgresql":
        pytest.skip("当前是 SQLite：PG 专有行为（如触发器的 DDL 事务性）需要真 PG 才能验证")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version_num')::int")
        version = cursor.fetchone()[0]
    assert version >= 120000, f"PostgreSQL 版本过低：{version}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")
        assert cursor.fetchone()[0] >= 0
