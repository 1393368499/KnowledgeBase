"""在途检查语义的独立用例（不依赖真实部署库）。"""
from __future__ import annotations

import pytest


def _sm():
    import storage_migrate as sm
    return sm


def test_refuses_when_writers_not_stopped():
    """写入者未停 + 有在途行 ⇒ **必须拒绝**（这条安全语义不能被夹具绕过）。"""
    sm = _sm()
    rows = [{"id": 1, "parse_status": "running", "original_name": "a.pdf"}]
    with pytest.raises(sm.Abort) as exc:
        sm.assert_no_inflight_processing(writers_stopped=False, provider=lambda: rows)
    assert "未停" in str(exc.value)


def test_records_but_allows_when_writers_stopped():
    """写入者已停 ⇒ 在途行视为僵死残留：**记录但不阻塞**。"""
    sm = _sm()
    rows = [{"id": 27, "parse_status": "pending", "original_name": "p.txt"}]
    got = sm.assert_no_inflight_processing(writers_stopped=True, provider=lambda: rows)
    assert got == rows


def test_no_rows_is_clean():
    sm = _sm()
    assert sm.assert_no_inflight_processing(writers_stopped=False, provider=lambda: []) == []


def test_subprocess_forces_project_settings():
    """生产取数必须**显式**用 knowledgebase.settings，不能继承调用方的 settings 模块。"""
    import inspect
    sm = _sm()
    src = inspect.getsource(sm._inflight_rows_from_project_settings)
    assert 'DJANGO_SETTINGS_MODULE' in src and 'knowledgebase.settings' in src
    assert 'KB_PYTEST_DB' in src, "必须清掉测试库变量，否则守卫会被环境影响"
