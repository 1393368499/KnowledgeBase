"""队列路由必须命中**真实注册的任务名**（独立审查抓到的缺陷）。

原写法 `task_routes={"apps.*": ...}` 看似合理，但本应用注册的任务是
`knowledge.process_ingested_file` / `knowledge.sweep_pending` —— 通配符
`apps.*` **一个都匹配不到**，任务会落回默认队列，专用队列形同虚设。

这里用 Celery **真实的 router** 判定，而不是只读配置字符串：
路由结果才是证据。
"""

from __future__ import annotations

import importlib

import pytest

TASKS = ["knowledge.process_ingested_file", "knowledge.sweep_pending"]


def queue_name(route) -> str:
    """route()['queue'] 是 kombu Queue 对象（不是字符串），取它的 name 再比较。

    第一版测试直接和字符串比，于是"路由明明是好的"却被判失败 ——
    断言要贴合真实返回类型。
    """
    q = (route or {}).get("queue")
    return getattr(q, "name", q or "")


def _app_with_queue(monkeypatch, queue: str | None):
    """在指定 KB_CELERY_QUEUE 下重新导入 celery 模块，拿到真实的 app。"""
    if queue is None:
        monkeypatch.delenv("KB_CELERY_QUEUE", raising=False)
    else:
        monkeypatch.setenv("KB_CELERY_QUEUE", queue)
    import knowledgebase.celery as module

    return importlib.reload(module).app


@pytest.fixture(autouse=True)
def _restore_module():
    """测试后恢复模块，避免污染其它用例。"""
    yield
    import knowledgebase.celery as module

    importlib.reload(module)


def test_registered_task_names_are_knowledge_prefixed():
    """先把"任务名长什么样"钉住——路由键依赖它。"""
    import knowledgebase.celery as module

    names = {t for t in (module.app.tasks or {}) if t.startswith("knowledge.")}
    assert "knowledge.process_ingested_file" in names
    assert "knowledge.sweep_pending" in names


def test_routes_send_real_tasks_to_the_dedicated_queue(monkeypatch):
    """只改 default_queue（设成**不同**值），路由规则读取**真实配置**。

    **不要**在测试里把 task_routes 重设成期望值 —— 那样等于把被测对象改成
    期望的样子，会掩盖配置回归（审查指出的问题）。这里只动 default_queue：
    若真实规则匹配不上，任务就会落到 some_other_default，断言即失败。
    """
    app = _app_with_queue(monkeypatch, "kb_worker_v2")
    # 唯一的改动：把默认队列换成别的名字（真实 task_routes 保持不动）
    app.conf.task_default_queue = "some_other_default"
    for task in TASKS:
        actual = queue_name(app.amqp.router.route({}, task))
        assert actual == "kb_worker_v2", (
            f"{task} 未命中**真实配置**里的专用规则，实际 {actual!r}"
            "（若为 some_other_default 说明规则没匹配上真实任务名）"
        )


def test_wrong_route_key_would_fail_this_assertion(monkeypatch):
    """反例自证：把规则键写错（apps.*）时，本测试的判定方式**会**失败。

    这条用例防止"测试恰好因为两个队列同值而恒真"。
    """
    app = _app_with_queue(monkeypatch, "kb_worker_v2")
    app.conf.task_default_queue = "some_other_default"
    # 反例里才人为放一条**错误规则**：证明本判定方式能识别出问题
    app.conf.task_routes = {"apps.*": {"queue": "kb_worker_v2"}}
    actual = queue_name(app.amqp.router.route({}, "knowledge.process_ingested_file"))
    assert actual == "some_other_default", (
        "错误规则下本应落到默认队列；若这里不是默认队列，说明判定方式不可靠"
    )
    assert actual != "kb_worker_v2"


def test_default_behaviour_unchanged_without_queue_env(monkeypatch):
    """未设置 KB_CELERY_QUEUE 时不启用专用路由（现有部署不受影响）。"""
    app = _app_with_queue(monkeypatch, None)
    assert not (app.conf.task_routes or {}), "不应无条件加路由"
    actual = queue_name(app.amqp.router.route({}, "knowledge.process_ingested_file"))
    assert actual != "kb_worker_v2"


def test_tasks_never_route_to_the_business_default_queue_when_enabled(monkeypatch):
    """启用专用队列后，两个任务都不得落到业务默认队列 celery。"""
    app = _app_with_queue(monkeypatch, "kb_worker_v2")
    for task in TASKS:
        actual = queue_name(app.amqp.router.route({}, task))
        assert actual != "celery", f"{task} 落到了业务默认队列"
