"""搜索页三个已证实缺陷的回归（Codex 独立浏览器审查）。

本仓库没有 JS 测试运行器，约定是"**源码级断言**锁关键语义 + 后端**真实 API** 断言
可验证的行为"（见 `tests/test_preview_frontend.py` / `tests/test_review_ui_fixes.py`）。

缺陷与对应证据：

1. 客户筛选不进"已生效条件" —— 只选客户时界面说"输入关键词或选一个筛选条件"，
   看起来筛选没生效（后端其实生效）。→ 源码断言 `active` / `activeTags` 含 customer，
   并用真实 `/files/` 请求证明 `customer=` 确实在服务端生效（否则源码断言会锁错方向）。
2. 请求无票据，乱序响应覆盖新结果；「清空」后旧结果回来。→ 源码断言票据自增、
   响应处比较票据、清空递增票据并立刻清空结果区。
3. 连续点「下一页」把 page 推出总页数 → 后端 404 → 分页条被隐藏，无法恢复。
   → 源码断言页码夹紧 + 404 分支保留分页条；API 断言越界页**确实** 404（证明夹紧不是多余的）。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember

pytestmark = pytest.mark.django_db

SEARCH_JS = Path("apps/webui/static/webui/kb-search.js")


def _script() -> str:
    return SEARCH_JS.read_text(encoding="utf-8")


# =========================================================================
# ① 客户筛选必须参与"已生效条件"
# =========================================================================


def test_customer_counts_as_active_filter():
    """`active` 判定必须含 customer，否则"只选客户"看起来筛选没生效。"""
    script = _script()

    # renderBlank 之外的判定在 structuredSearch 里：找到那个 active 表达式
    match = re.search(r"const active = Boolean\((.*?)\);", script, re.S)
    assert match, "structuredSearch 里的 active 判定不见了（源码结构变了，请同步本用例）"
    active_expr = match.group(1)
    assert "state.customer" in active_expr, (
        "active 判定漏了 state.customer：只选客户时会显示"
        "'输入关键词或选一个筛选条件'，用户以为筛选没生效"
    )


def test_customer_is_rendered_as_active_tag():
    """已生效条件区必须把客户显示出来（文案 `客户：xxx`）。"""
    script = _script()
    assert 'tags.push("客户：" + state.customer)' in script, (
        "activeTags() 没有输出客户标签，用户看不到客户筛选已生效"
    )
    # 另外三个维度仍在（不能被这次改动挤掉）
    for marker in ('"部门："', '"性质："', '"金额 ≥ "'):
        assert marker in script, f"activeTags 丢了原有维度：{marker}"


def test_customer_filter_really_applies_server_side(client, admin, library):
    """真实 API：`customer=` 必须在**服务端**生效（否则源码断言锁的是个假前提）。

    同时证明"只选客户、没有关键词"是一条**有效**检索：返回非空且被过滤。
    """
    _make_file(library, "cust-a.txt", customer="华东电力")
    _make_file(library, "cust-b.txt", customer="西北矿业")
    client.force_login(admin)

    response = client.get("/api/v1/knowledge/files/?customer=" + "华东电力")
    assert response.status_code == 200, response.content[:300]
    rows = response.json()["results"]
    assert rows, "只按客户筛选应当命中（这正是'筛选其实生效了'的后端事实）"
    assert {row["customer"] for row in rows} == {"华东电力"}
    assert response.json()["count"] == 1


# =========================================================================
# ② 请求票据：乱序响应不得覆盖新结果；清空作废在途请求
# =========================================================================


def test_search_has_monotonic_ticket():
    """每次检索/列表请求都要发一个新票据（自增计数器）。"""
    script = _script()
    assert re.search(r"let searchTicket = 0;", script), "票据计数器不存在"
    # 两个通路各自发号（分页与筛选共用同一个计数器）
    assert script.count("++searchTicket") >= 2, (
        "结构化检索与混合检索没有各自发号：分页/筛选请求无法互相作废"
    )


def test_stale_response_is_discarded_compare_at_response():
    """响应用之前必须**比较票据**，不是同一个号就直接丢弃。"""
    script = _script()
    guards = re.findall(r"if \(ticket !== searchTicket\) return;", script)
    assert len(guards) >= 2, (
        f"响应处缺少票据比较（找到 {len(guards)} 处）：慢响应会覆盖新结果"
    )
    # 比较必须发生在请求**之后**（即 await 之后），否则拦不住乱序
    for function_name in ("hybridSearch", "structuredSearch"):
        body = _function_body(script, function_name)
        assert "const ticket = ++searchTicket;" in body, f"{function_name} 没发票据"
        assert "await" in body and "if (ticket !== searchTicket) return;" in body, (
            f"{function_name} 没有在响应处丢弃过期票据"
        )
        assert body.index("await") < body.index("if (ticket !== searchTicket) return;"), (
            f"{function_name} 的票据比较发生在请求之前，等于没拦"
        )


def test_reset_clears_results_and_invalidates_inflight_requests():
    """「清空」必须递增票据（作废旧请求）**并立刻**清空结果区。"""
    script = _script()
    body = _function_body(script, '"search-reset").addEventListener("click"')
    assert "searchTicket += 1" in body, (
        "清空没有作废在途请求：清空前发出的慢响应回来会把旧结果画回结果区"
    )
    assert '$("search-results").innerHTML = ""' in body, "清空没有立刻清空结果区"
    # 顺序上：先作废票据，再清界面（否则清完仍可能被旧响应覆盖）
    assert body.index("searchTicket += 1") < body.index('$("search-results").innerHTML = ""')


# =========================================================================
# ③ 翻页越界：先夹紧页码；后端仍 404 时必须保留分页条
# =========================================================================


def test_next_page_is_clamped_to_known_total():
    """「下一页」必须用已知总页数夹紧，而不是无条件 +1。"""
    script = _script()
    body = _function_body(script, '"search-next").addEventListener("click"')
    assert re.search(
        r"const next = Math\.min\(state\.page \+ 1, searchTotalPages\);", body
    ), "「下一页」没有夹紧页码：连续点会越界 → 404 → 分页条消失"
    assert "if (next === state.page) return;" in body, "越界时没有直接返回（仍会发越界请求）"
    # 成功响应里必须记住真实总页数（夹紧的唯一依据）
    assert re.search(r"searchTotalPages = pages;", script), "没有记录真实总页数"
    assert re.search(r"searchTotalPages = Math\.max\(1, Math\.ceil\(total / PAGE_SIZE\)\)", script) or (
        'const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));' in script
    ), "总页数计算不见了"


def test_pager_is_not_hidden_when_page_request_fails():
    """后端仍失败（越界 404）时**不许隐藏分页条**，并要写明原因。"""
    script = _script()
    body = _function_body(script, "async function structuredSearch")
    error_branch = body[body.index("if (!result.ok)") : body.index("const body = result.body")]
    assert '$("search-pager").style.display = "flex"' in error_branch, (
        "失败分支把分页条藏起来了：页码一旦越界，用户再也点不到「上一页」"
    )
    assert 'style.display = "none"' not in error_branch, "失败分支仍在隐藏分页条"
    assert "本页请求失败" in error_branch, "失败时没有如实说明原因"
    assert "$(\"search-prev\").disabled = state.page <= 1;" in error_branch, (
        "失败时没有保证「上一页」可用（用户无法返回有效页）"
    )


def test_out_of_range_page_really_404s_from_backend(client, admin, library):
    """证明"夹紧"不是多余的：越界页在后端**确实** 404。

    这正是缺陷 3 的根因链：`page` 越界 → 404 → 旧代码隐藏分页条 → 无法恢复。
    """
    _make_file(library, "page-1.txt")
    client.force_login(admin)

    ok = client.get("/api/v1/knowledge/files/?page_size=10&page=1")
    assert ok.status_code == 200
    assert ok.json()["count"] == 1

    beyond = client.get("/api/v1/knowledge/files/?page_size=10&page=7")
    assert beyond.status_code == 404, (
        f"越界页竟然不是 404（实际 {beyond.status_code}）：请同步缺陷 3 的措辞与测试"
    )


# =========================================================================
# 顺手修：无分片序号时文案不谎称"分片 #0"；无关键词不渲染反馈按钮
# =========================================================================


def test_no_ordinal_means_no_chunk_number_in_copy():
    """后端没给 `ordinal` 时，结果卡不写 `data-chunk`，toast 也不准说"分片 #0"。"""
    script = _script()
    # 结果卡：只在真有 ordinal 时才写 data-chunk
    assert "const hasOrdinal = item.ordinal !== undefined && item.ordinal !== null" in script
    assert "(hasOrdinal ? ' data-chunk=\"' + rawOrdinal + '\"' : \"\")" in script, (
        "结果卡仍无条件写 data-chunk（没序号时恒为 0 = 编造序号）"
    )
    assert "(Number.isFinite(ordinal) && ordinal >= 0 ? ordinal : 0)" not in script, (
        "旧的恒 0 写法还在"
    )
    # 反馈提交：读真实属性，缺属性时不当成 0
    assert 'card.getAttribute("data-chunk")' in script
    assert "const hasChunkOrdinal = chunkAttr !== null" in script
    # toast：有号才写序号，无号时明确说是"资料级"（不能伪造"分片 #0"）
    assert 'KB.toast("反馈已记录（分片 #" + payload.chunk_ordinal' not in script, (
        "toast 仍无条件宣称分片序号"
    )
    assert '(hasChunkOrdinal' in script and '（分片 #" + payload.chunk_ordinal + "）' in script, (
        "toast 缺少『有真实序号才写序号』的分支"
    )
    assert "资料级" in script, "无序号时没有向用户说明反馈是资料级的"
    # 提交时显式声明粒度：有真实序号 = chunk，没有 = file（不再把缺号当第 0 片）
    assert 'scope: hasChunkOrdinal ? "chunk" : "file"' in script, (
        "反馈 payload 没有显式反馈粒度"
    )
    assert "if (hasChunkOrdinal) payload.chunk_ordinal = Number(chunkAttr);" in script, (
        "无真实序号时仍会提交 chunk_ordinal=0"
    )


def test_feedback_buttons_only_when_a_query_exists():
    """结构化列表（无关键词）时后端会因空 query 400 → 不渲染反馈按钮。"""
    script = _script()
    assert "const canFeedback = Boolean(state.q);" in script, (
        "反馈按钮没有按'是否有具体检索词'门控"
    )
    card = _function_body(script, "function resultCard")
    assert "(canFeedback" in card, "resultCard 没有用门控值决定是否渲染反馈按钮"
    assert 'data-feedback="accurate"' in card and 'data-feedback="inaccurate"' in card
    # 有检索词时必须渲染（不能把反馈功能整体删掉）
    assert card.count("canFeedback") >= 2


def test_empty_query_feedback_is_really_rejected_with_400(client, admin, library):
    """真实 API：空 query 反馈确实 400 —— 所以"不渲染按钮"不是偷懒，是必需。

    同时证明**有**关键词时同一条通路 200（门控没有把正常路径一起关掉）。
    """
    record = _make_file(library, "feedback.txt")
    client.force_login(admin)

    rejected = client.post(
        "/api/v1/knowledge/search-feedback/",
        {"verdict": "accurate", "query": "", "file_id": record.pk, "chunk_ordinal": 0},
        content_type="application/json",
    )
    assert rejected.status_code == 400, (
        f"空 query 竟然被接受（{rejected.status_code}）：那门控就与后端约束不一致了"
    )

    accepted = client.post(
        "/api/v1/knowledge/search-feedback/",
        {"verdict": "accurate", "query": "合同", "file_id": record.pk, "chunk_ordinal": 0},
        content_type="application/json",
    )
    assert accepted.status_code in {200, 201}, accepted.content[:300]


# =========================================================================
# 结构性守卫：本次改动不得碰红线文件
# =========================================================================


def test_prototype_files_untouched():
    """只允许改 kb-search.js；无引用的原型 CSS 已删除，不得复活。"""
    proto = Path("apps/webui/static/webui/kb-proto.css")
    assert not proto.exists(), "kb-proto.css 是无引用的死文件，已删除，不能复活"


# =========================================================================
# 夹具 / 辅助
# =========================================================================


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(
        username=f"race-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def library(admin):
    item = KnowledgeLibrary.objects.create(
        code=f"lib-{uuid.uuid4().hex[:6]}", name="竞态库"
    )
    LibraryMember.objects.create(library=item, user=admin, role="admin")
    return item


def _make_file(library: KnowledgeLibrary, name: str, **extra) -> IngestedFile:
    return IngestedFile._base_manager.create(
        library=library,
        original_name=name,
        content_sha256=uuid.uuid4().hex,
        size_bytes=10,
        file_type="txt",
        parse_status="done",
        **extra,
    )


def _function_body(script: str, needle: str) -> str:
    """取 `needle` 之后到下一个顶层 `function`/监听器结尾的粗糙切片。

    这里不追求 JS 解析精度（仓库没有 JS 运行器），只要能把断言限定在目标函数的
    文本范围内，避免"别处也有同样字样"造成的假绿。
    """
    start = script.index(needle)
    tail = script[start:]
    # 以行首两个空格的 `}` 作为当前函数的收尾（本文件缩进稳定为 2 空格）
    end = tail.find("\n  }")
    return tail if end == -1 else tail[: end + 4]


def test_function_body_helper_is_not_vacuous():
    """辅助切片必须真的截断（否则上面的断言可能落进整份文件，变成假绿）。"""
    script = _script()
    body = _function_body(script, "function resultCard")
    assert "function activeTags" not in body, "切片没有截断，断言范围不可信"
    assert "const canFeedback" in body
