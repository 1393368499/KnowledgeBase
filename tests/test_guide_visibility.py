"""使用说明里的环境自述必须与**声明的可见范围**一致（Codex 审计 OP-12/OP-20）。

原状：系统配置页声明"远程模型服务/模型与基础设施配置只有系统管理员能看到"，但
``/api/v1/planner/guide/`` 对所有登录用户都回传了真实 LLM ``base_url``（内网端点）
与模型名 —— 声明与实际可见范围自相矛盾。

修法：端点与模型名只给系统管理员；普通账号只拿 ``configured`` 布尔 + 一句说明。
本文件把这四条边界钉住（不是"改提示"，而是改可见范围）。
"""

from __future__ import annotations

import pytest
from django.test import override_settings

pytestmark = pytest.mark.django_db

GUIDE = "/api/v1/planner/guide/"

FAKE_SERVICES = {
    "llm": {"base_url": "http://10.99.0.7:8000/v1", "model": "internal-llm-x"},
    "embedding": {"base_url": "http://10.99.0.7:8001/v1", "model": "bge"},
}


@override_settings(MODEL_SERVICES=FAKE_SERVICES)
def test_plain_user_does_not_see_the_llm_endpoint(api, user):
    api.force_authenticate(user=user)
    response = api.get(GUIDE)
    assert response.status_code == 200, response.content[:200]
    llm = response.data["environment"]["llm"]
    assert llm["base_url"] == "", "普通账号仍能看到内网端点"
    assert llm["model"] == "", "普通账号仍能看到模型名"
    assert llm["configured"] is True, "「是否已配置」这个事实应当保留"
    assert llm["restricted"] is True
    assert "系统管理员" in llm.get("note", "")
    # 整个响应体里都不该出现内网地址与模型名（防止别处漏出去）
    body = response.content.decode("utf-8")
    assert "10.99.0.7" not in body and "internal-llm-x" not in body


@override_settings(MODEL_SERVICES=FAKE_SERVICES)
def test_system_admin_still_sees_the_llm_configuration(api, admin_user):
    api.force_authenticate(user=admin_user)
    llm = api.get(GUIDE).data["environment"]["llm"]
    assert llm["base_url"] == "http://10.99.0.7:8000/v1"
    assert llm["model"] == "internal-llm-x"
    assert llm["restricted"] is False


def test_guide_requires_login(api):
    response = api.get(GUIDE)
    assert response.status_code in (401, 403)


@override_settings(MODEL_SERVICES={})
def test_unconfigured_llm_is_reported_as_not_configured(api, user):
    api.force_authenticate(user=user)
    llm = api.get(GUIDE).data["environment"]["llm"]
    assert llm["configured"] is False
    assert llm["base_url"] == "" and llm["model"] == ""
