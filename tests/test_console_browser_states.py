"""在真实浏览器中运行控制台脚本，验证故障、权限及操作结果呈现。"""
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[1] / "apps/webui/static/webui"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        instance = driver.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def render(browser):
    pages = []

    def load(overrides=None, admin=True, result=None):
        page = browser.new_page()
        pages.append(page)
        page.set_content('<body' + (' data-system-admin="1"' if admin else '') + '><main id="main"></main><div id="toast"></div></body>')
        page.add_script_tag(content=(STATIC / "kb-common.js").read_text(encoding="utf-8"))
        replies = {
            "operations": {"results": [], "summary": {"executed": 1, "settled": 4, "restart_stalled_executed": 0}},
            "stats": {"pending": 0, "processing": 0},
            "runtime-metrics": {"breakers": {"scope": "global", "shared": [], "items": []}},
            "search-feedback": {"results": []}, "index-stats": {"collections": []},
            "events": {"results": []},
        }
        replies.update(overrides or {})
        page.evaluate("""({replies, result}) => {
            KB.api = async (url) => {
                const key = Object.keys(replies).find(k => url.includes('/' + k + '/'));
                const value = replies[key];
                return value && value._failure ? {ok:false, status:value.status, body:{detail:value.detail}} :
                    {ok:true, status:200, body:value};
            };
            window.actionCalls = 0;
            KB.controlled = async () => { window.actionCalls++; return {ok:true, result:result || {detail:'完成'}}; };
        }""", {"replies": replies, "result": result})
        page.add_script_tag(content=(STATIC / "kb-pages.js").read_text(encoding="utf-8"))
        page.evaluate("KB.pages.loadAgent()")
        return page

    yield load
    for page in pages:
        page.close()


def test_read_failures_are_not_rendered_as_empty_or_healthy(render):
    failure = {"_failure": True, "status": 503, "detail": "读取失败：服务暂不可用"}
    page = render({name: failure for name in ["operations", "stats", "runtime-metrics", "search-feedback", "index-stats", "events"]})
    text = page.locator("#main").inner_text()
    assert "读取失败" in text
    for misleading in ["队列已清空", "还没有登记任何", "还没有审计事件", "还没跑过巡检", "还没有检索反馈"]:
        assert misleading not in text
    assert page.locator("#agent-refresh").count() == 1


def test_plain_account_sees_permissions_instead_of_absent_heartbeat(render):
    denied = {"_failure": True, "status": 403, "detail": "无权限"}
    page = render({name: denied for name in ["runtime-metrics", "index-stats", "events"]}, admin=False)
    assert "无权限读取运行指标" in page.locator("#main").inner_text()
    assert "还没跑过巡检" not in page.locator("#main").inner_text()
    assert page.locator("[data-agent-action]:not([disabled])").count() == 0


def test_breaker_card_includes_failures_from_worker_shared_state(render):
    page = render({"runtime-metrics": {"breakers": {"scope": "global", "items": [], "shared": [
        {"service": "worker-embedding", "state": "open", "consecutive_failures": 3, "failure_threshold": 3}
    ]}}})
    card = page.locator(".stat-card").filter(has_text="API熔断器")
    assert "worker-embedding" in card.inner_text()
    assert card.locator(".status-dot.failed").count() == 1


def test_overdue_watchdog_card_is_red(render):
    page = render({"runtime-metrics": {"watchdog": {"available": True, "overdue": True, "issues": 0,
        "checked": 1, "age_seconds": 0, "source": "manual", "verdict": "周期心跳过期"}}})
    card = page.locator(".stat-card").filter(has_text="解析看门狗")
    assert card.locator(".status-dot.failed").count() == 1
    assert "周期心跳过期" in card.inner_text()


def test_missing_or_unbound_collections_have_no_activation_button(render):
    page = render({"index-stats": {"collections": [
        {"name": "missing", "library": "lib", "exists_in_qdrant": False},
        {"name": "orphan", "library": "", "exists_in_qdrant": True},
        {"name": "active", "library": "lib", "exists_in_qdrant": True, "is_active": True},
    ]}})
    assert page.locator("[data-gen-activate]").count() == 0
    assert page.locator("[data-gen-rollback]:disabled").count() == 1


def test_learning_result_is_readable_after_refresh_and_escapes_external_text(render):
    page = render(result={"detail": "反馈规则：" + "实时加权" * 80,
        "items": [{"name": "<img src=x onerror=alert(1)>", "status": "净分 -1 · 权重 -0.150"}]})
    page.locator('[data-agent-action="learn"]').click()
    page.wait_for_selector(".modal-body")
    assert "实时加权" * 80 in page.locator(".modal-body").inner_text()
    assert "权重 -0.150" in page.locator(".modal-body").inner_text()
    assert page.locator(".modal-body img").count() == 0


def test_operation_success_rate_uses_server_total_history(render):
    page = render({"operations": {"results": [{"status": "executed"}],
        "summary": {"executed": 1, "settled": 4, "restart_stalled_executed": 0}}})
    assert "25%" in page.locator(".stat-card").filter(has_text="我的操作成功率").inner_text()


def test_concurrency_gate_failure_does_not_leave_resources_green(render):
    page = render({"runtime-metrics": {"broker_queue": {"depth": 0, "url_db": "14"},
        "model_concurrency": {"enabled": True, "available": False, "fallback": {"total": 3}}}})
    card = page.locator(".stat-card").filter(has_text="资源调控")
    assert card.locator(".status-dot.failed").count() == 1
    assert "降级放行 3 次" in card.inner_text()


def test_pending_action_cannot_be_submitted_twice_and_cancel_restores_button(render):
    page = render()
    page.evaluate("""() => {
        KB.controlled = () => new Promise(resolve => { window.actionCalls++; window.resolveAction = resolve; });
    }""")
    button = page.locator('[data-agent-action="learn"]')
    button.click()
    assert button.is_disabled()
    button.dispatch_event("click")
    assert page.evaluate("window.actionCalls") == 1
    page.evaluate("window.resolveAction({ok:false, skipped:true})")
    assert button.is_enabled()
