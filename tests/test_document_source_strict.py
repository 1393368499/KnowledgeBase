"""严格来源校验（审查第 1 条）：显式来源**不得悄悄扩大或缩小**范围。

旧行为（已修）：`source_file_ids` 用 `str(v).isdigit()` 逐项丢弃非法值 ——
  * `["bad"]` 被丢成空列表 → "指定来源"静默退化成**全库检索**（范围被扩大）；
  * 换算/负数/标量（字符串、对象）被吞掉；
  * 混合了无权/不存在的 id 时，其余项照样送进模型（范围被无声缩小）。

现在的口径：只要显式给了 `source_file_ids`，就逐项校验，任何一项不合法即 **400**，
且必须**在调用模型之前**拒绝（不产生任何模型外呼、不落文档）。
"""

from __future__ import annotations

import pytest

URL = "/api/v1/planner/documents/"


@pytest.fixture
def no_model(monkeypatch):
    """任何模型外呼都算失败：用来证明"非法来源在调用模型之前就被拒绝"。"""
    from apps.planner import services

    calls = {"n": 0}

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        raise AssertionError("非法来源不该触发模型调用")

    monkeypatch.setattr(services.LlmClient, "chat", boom, raising=False)
    return calls


def _post(client, admin_user, **payload):
    client.force_login(admin_user)
    body = {"title": "来源校验", "topic": "质保期"}
    body.update(payload)
    return client.post(URL, body, content_type="application/json")


@pytest.mark.parametrize(
    "bad_value",
    [
        ["bad"],          # 非数字字符串（旧实现被丢成空列表 → 退回全库）
        [-1],             # 负数
        [0],              # 非正整数
        [1.5],            # 浮点
        [True],           # 布尔（int 的子类，必须排除）
        [{"id": 1}],      # 对象
        [],               # 显式空数组
    ],
)
def test_invalid_source_file_ids_are_rejected_before_model(
    db, client, admin_user, no_model, bad_value
):
    response = _post(client, admin_user, source_file_ids=bad_value)
    assert response.status_code == 400, response.content[:200]
    assert no_model["n"] == 0, "非法来源竟然触发了模型调用"


def test_scalar_instead_of_array_is_rejected(db, client, admin_user, no_model):
    """标量（而不是数组）必须明确 400，不能当成"没传"退回全库。"""
    response = _post(client, admin_user, source_file_ids="1,2")
    assert response.status_code == 400, response.content[:200]
    assert "数组" in response.json()["detail"]
    assert no_model["n"] == 0


def test_missing_source_is_rejected_not_silently_dropped(
    db, client, admin_user, library, make_file, grant_member, no_model
):
    """不存在的 id 必须 400（不能悄悄丢掉它、只用剩下能用的那几份）。"""
    grant_member(library, admin_user)
    good = make_file(library=library, name="good.txt")
    response = _post(client, admin_user, source_file_ids=[good.pk, 999999])
    assert response.status_code == 400, response.content[:200]
    assert "999999" in response.json()["detail"]
    assert no_model["n"] == 0


def test_unreadable_source_is_rejected(
    db, client, user, make_library, make_file, grant_member, no_model
):
    """**普通无权用户**引用他人资料 → 必须严格 400，且模型零调用。

    这里刻意不用系统管理员：管理员对所有库都可读，用它写"201 或 400 都算通过"
    等于没有断言（假绿灯）。普通用户才是真实越权场景。
    """
    # 先给该用户一个**自己的可读库**：否则会先被"没有可用信息库"的守卫拦下，
    # 走不到来源校验这条路径 —— 那样测的就不是本用例要测的东西。
    mine = make_library(code="strict-mine", name="我的库")
    grant_member(mine, user, role="admin")
    other = make_library(code="secret-lib", name="别人的库")
    secret = make_file(library=other, name="secret.txt")
    client.force_login(user)
    response = client.post(
        URL,
        {"title": "越权来源", "topic": "x", "source_file_ids": [secret.pk]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content[:200]
    assert str(secret.pk) in response.json()["detail"]
    assert no_model["n"] == 0, "越权来源竟然触发了模型调用"


def test_mixed_missing_and_unauthorized_all_rejected(
    db, client, user, make_library, make_file, grant_member, no_model
):
    """**混合**（合法 + 不存在 + 越权）必须整体拒绝：不能只用合法那几份继续生成。"""
    mine = make_library(code="mine-lib", name="我的库")
    grant_member(mine, user, role="admin")
    good = make_file(library=mine, name="mine.txt")
    other = make_library(code="other-lib", name="他人库")
    secret = make_file(library=other, name="other.txt")

    client.force_login(user)
    response = client.post(
        URL,
        {
            "title": "混合来源",
            "topic": "x",
            "source_file_ids": [good.pk, 987654, secret.pk],
        },
        content_type="application/json",
    )
    assert response.status_code == 400, response.content[:200]
    assert no_model["n"] == 0


def test_explicit_null_is_distinguished_from_omitted(db, client, user, no_model):
    """显式 `null` 与"省略字段"必须**区分**：省略=全库，null=400。"""
    client.force_login(user)
    response = client.post(
        URL,
        {"title": "null 来源", "topic": "x", "source_file_ids": None},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content[:200]
    assert "省略" in response.json()["detail"]
    assert no_model["n"] == 0
