"""用户实测反馈的两条回归（2026-10-16）。

**第一处**：用户在会话里说"写一个出行报告"，助手只回了一张"搜索知识库 · 已完成"
的卡片，**报告一个字都没有**。根因（生产库实证）：一次用户消息只调一次模型
（``services.reply_to_message``），工具执行结果不回灌，模型这一轮选了只读的
search_knowledge 就结束了 —— 空正文 + 合法 tool_call 是合法返回，所以也不会报错。
处置：系统提示词里把"要写文档"的默认动作钉死为 **提案 compose_document**。

**第二处**：搜索页结果卡里的"命中分片小字摘要"（``.chunk``）需要撤掉
（用户原话："不要这些红框中的小字"）。撤掉的是**渲染**，不是数据：
后端返回的 ``text`` / ``ordinal`` 一个字段没动，"准确 / 不准确"反馈仍然
按真实分片序号落库。原型文件 ``docs/prototype/knowledge-base-prototype.html`` 保持只读。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.planner import operations, services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db


def _read(relative: str) -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / relative).read_text(encoding="utf-8")


# ------------------------------------------- 1) "写文档"必须走 compose_document ----


def test_system_prompt_tells_model_to_propose_compose_document():
    prompt = services.SYSTEM_PROMPT
    assert "写文档规则" in prompt, "提示词里没有写文档的规则"
    assert "compose_document" in prompt, "提示词没有点明写文档用哪个工具"
    # 必须明确否定"只检索"这条歧路 —— 实测模型就是这么走偏的
    rule = prompt[prompt.index("写文档规则") :]
    assert "search_knowledge" in rule, "没说明'只调检索'不算写文档"
    assert "不要" in rule


def test_compose_document_is_registered_as_a_low_risk_writable_action():
    spec = operations.get("compose_document")
    assert spec is not None, "compose_document 没有登记"
    assert spec.permission == "library:read"
    assert spec.risk == operations.RISK_LOW
    assert spec.requires_destructive_flow is False
    # 现行策略：只有删除保留人工确认；生成文档参数完整后直接执行
    assert spec.requires_confirmation is False
    assert spec.auto_execute is True
    # `template_id` 是这次新增的**可选**参数：不选就不用模板（用户明确要求的默认行为）
    assert set(spec.params) == {"title", "topic", "sources", "template_id"}


def test_compose_document_is_offered_to_the_model_as_a_tool():
    """工具模式就是模型的"菜单"：菜单里没有，模型永远提不出来。"""
    tools = {tool["function"]["name"] for tool in services.tool_schema()}
    assert "compose_document" in tools
    entry = next(
        tool for tool in services.tool_schema() if tool["function"]["name"] == "compose_document"
    )
    props = entry["function"]["parameters"]["properties"]
    assert "title" in props and "topic" in props


def test_compose_document_requires_sources_and_leaves_no_confirmation_card(admin_user):
    """现行协议：`sources` 为空 → 明确要求补齐、不落待确认卡；选好参考文件后直接生成。

    ``docs/compose-document-reference-files-2026-10-19.md`` +
    ``docs/delete-only-confirmation-2026-10-27.md``：指定参考文件后直接生成，
    不再有"一次确认"这一步。
    """
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user,
            intent="compose_document",
            params={"title": "出行报告", "topic": "陶鑫宇 出差 陵水 海口东 车次"},
        )
    assert "参考文件" in str(exc.value)
    assert not OperationRecord.objects.exists(), "缺参考文件不应留下待确认卡"
    spec = operations.get("compose_document")
    assert spec.requires_confirmation is False
    assert spec.auto_execute is True


# ------------------------------------------------- 2) 搜索页不再渲染小字摘要 ----


def test_search_result_card_no_longer_renders_the_excerpt_block():
    script = _read("apps/webui/static/webui/kb-search.js")
    assert 'class="chunk"' not in script, "结果卡又在渲染小字摘要了"
    assert "highlight(" not in script, "小字摘要的关键词高亮渲染还在"


def test_search_result_card_keeps_the_useful_parts():
    """撤掉摘要不能顺手撤掉有用的东西：文件名、匹配度、预览与反馈入口。"""
    script = _read("apps/webui/static/webui/kb-search.js")
    assert "匹配度 " in script
    assert 'class="name"' in script
    assert "data-preview=" in script
    assert 'data-feedback="accurate"' in script
    assert 'data-feedback="inaccurate"' in script
    # 反馈仍按**真实分片序号**提交（绝不是写死 0）：
    # 后端没有给序号时不许伪造"第 0 片"，而是显式声明资料级反馈（scope=file）。
    assert "const ordinal = Number(item.ordinal || 0);" not in script
    assert 'card.getAttribute("data-chunk")' in script
    assert "if (hasChunkOrdinal) payload.chunk_ordinal = Number(chunkAttr);" in script
    assert 'scope: hasChunkOrdinal ? "chunk" : "file"' in script
    assert 'data-chunk="' in script


def test_prototype_file_itself_is_untouched():
    """去掉的是**实现**里的渲染；原型文件一个字节都不许改（只读基线）。"""
    proto = _read("docs/prototype/knowledge-base-prototype.html")
    assert '<div class="chunk">' in proto, "原型被改了（它应当保持只读）"
    # 死代码清理（2026-11-11 全栈复审 P1）：`kb-proto.css`（29 KB）没有任何页面
    # 或脚本引用，属于死文件，已删除。反向守卫：它不能复活，也不能再被模板/JS 引入
    # —— 引入一个不存在的样式表会让页面裸奔，而且这种回归不会有人报错。
    root = Path(__file__).resolve().parent.parent
    proto_css = root / "apps/webui/static/webui/kb-proto.css"
    assert not proto_css.exists(), "kb-proto.css 是无引用的死文件，已删除，不能复活"
    webui_root = root / "apps/webui"
    for pattern in ("static/webui/*.js", "templates/**/*.html"):
        for path in webui_root.glob(pattern):
            text = path.read_text(encoding="utf-8")
            assert "kb-proto.css" not in text, f"{path} 仍在引用已删除的 kb-proto.css"


# --------------------------------- 3) 空正文的助手消息不再只剩一个空白气泡 ----


def test_empty_assistant_reply_is_explained_instead_of_blank():
    """正文为空 + 只有工具调用时（写文档前先检索就是这样），必须如实说明。

    不能让用户对着空白气泡猜"模型是不是什么都没干"——这正是"报告这怎么没写"的观感来源。
    """
    script = _read("apps/webui/static/webui/kb-assistant.js")
    assert "function emptyReplyHtml(" in script, "没有空正文的说明分支"
    assert "payload.tool_calls" in script, "说明没有依据真实 payload（会变成编造）"
    assert "这一轮没有正文回答" in script
    # 渲染路径必须把 payload 传进来，否则说明永远拿不到工具调用
    assert "payload: payload," in script


def test_empty_reply_helper_never_invents_an_answer():
    """空正文时**不能**拼出一段像回答的话：只能讲真实发生过的调用。"""
    script = _read("apps/webui/static/webui/kb-assistant.js")
    start = script.index("function emptyReplyHtml(")
    body = script[start : start + 1200]
    assert "TOOL_NAMES[call.name] || call.name" in body, (
        "工具名不认识时应当原样显示真实名称，而不是编一个中文名"
    )
    # 只允许输出"没有正文回答 + 发起了哪些操作"，不给任何结论性内容
    assert "根据" not in body and "结论" not in body


def test_assistant_message_call_site_passes_payload():
    """会话渲染处（renderHome → renderMessage）必须带上 payload。"""
    script = _read("apps/webui/static/webui/kb-assistant.js")
    call = script[script.index("return renderMessage({") :][:520]
    assert "payload: payload," in call


# ------------------- 4) 模型把"资料文件名"当来源传进来时，待办卡不许报废 ----
#
# 生产实测（2026-10-16 11:xx）：点"写一个出行报告"后模型正确提案了
# compose_document，但 `sources` 里写的是**文件全名**（"陶鑫宇提交的出差申请
# 202610132300000331266.pdf"）。旧实现把 sources 的每一项都当成信息库 code，
# 于是整张卡直接变成 "compose_document · 信息库不存在：<文件名>"，报告照样出不来。


def _compose_via_controlled(user, params: dict):
    """提案即生成（现行策略：非删除动作零人工确认），返回执行结果体。"""
    proposal = services.propose_operation(user, intent="compose_document", params=params)
    assert proposal["auto_executed"] is True, proposal
    assert proposal["confirm_token"] == ""
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED, operation.status
    return proposal["result"]


def test_propose_accepts_a_file_name_in_sources(admin_user, library, make_file, monkeypatch):
    record = make_file(library, "陶鑫宇提交的出差申请202610132300000331266.pdf")
    captured: dict = {}

    def fake_compose(user, *, title, topic, conversation=None, library_codes=None,
                     source_file_ids=None, template_id=None, client=None):
        captured.update(source_file_ids=source_file_ids, library_codes=library_codes)
        return {"document_id": 4241, "title": title}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    proposal = services.propose_operation(
        admin_user,
        intent="compose_document",
        params={
            "title": "出行报告",
            "topic": "陶鑫宇 陵水 海口东 车次",
            "sources": [record.original_name],
        },
    )
    assert proposal["status"] == OperationRecord.Status.EXECUTED, "文件名来源又把提案弄废了"
    assert proposal["auto_executed"] is True
    assert captured["source_file_ids"] == [record.pk]


def test_target_library_ids_resolves_file_name_to_its_library(admin_user, library, make_file):
    record = make_file(library, "海口东-陵水.pdf")
    ids = services._target_library_ids({"sources": [record.original_name]})
    assert ids == [library.pk]


def test_target_library_ids_still_fails_closed_for_unknown_libraries():
    """`libraries` 是**目标**参数，认不出来必须拒绝（不能因为放宽 sources 而放宽它）。"""
    with pytest.raises(services.PlannerError) as exc:
        services._target_library_ids({"libraries": ["no-such-library"]})
    assert "信息库不存在" in str(exc.value)


def test_split_compose_sources_separates_libraries_files_and_junk(
    admin_user, library, make_file
):
    record = make_file(library, "陵水-海口东.pdf")
    codes, file_ids, ignored = services._split_compose_sources(
        admin_user, [library.code, record.original_name, "并不存在的东西.pdf"]
    )
    assert codes == [library.code]
    assert file_ids == [record.pk]
    assert ignored == ["并不存在的东西.pdf"], "拆不开的来源必须如实回传，不能悄悄丢掉"


def test_execute_routes_file_backed_sources_into_source_file_ids(
    admin_user, library, make_file, monkeypatch
):
    """文件名来源 → `source_file_ids`（最精确），而不是被当成信息库 code。"""
    record = make_file(library, "陶鑫宇提交的出差申请.pdf")
    captured: dict = {}

    def fake_compose(user, *, title, topic, conversation=None, library_codes=None,
                     source_file_ids=None, template_id=None, client=None):
        captured.update(
            title=title, topic=topic,
            library_codes=library_codes, source_file_ids=source_file_ids,
        )
        return {"document_id": 4242, "title": title}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    result = _compose_via_controlled(
        admin_user,
        {"title": "出行报告", "topic": "陵水", "sources": [record.original_name]},
    )
    assert captured["source_file_ids"] == [record.pk]
    assert captured["library_codes"] is None, "没有信息库来源时不该硬塞一个库进去"
    assert "ignored_sources" not in result


def test_execute_reports_sources_it_could_not_resolve(admin_user, monkeypatch, library, make_file):
    """有有效参考文件时，额外无法解析的来源仍须在结果中说明。"""
    record = make_file(library, "有效参考.pdf")
    captured: dict = {}

    def fake_compose(user, *, title, topic, conversation=None, library_codes=None,
                     source_file_ids=None, template_id=None, client=None):
        captured.update(library_codes=library_codes, source_file_ids=source_file_ids)
        return {"document_id": 4243, "title": title}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    result = _compose_via_controlled(
        admin_user,
        {"title": "出行报告", "topic": "陵水", "sources": [record.pk, "并不存在的东西.pdf"]},
    )
    assert captured["source_file_ids"] == [record.pk]
    assert result["ignored_sources"] == ["并不存在的东西.pdf"]


# ---------- 5) 模型把"资料 id（数字）"当来源传进来时，待办卡同样不许报废 ----
#
# 生产实测（2026-10-16 14:11，用户第二次反馈"还是没有写"）：模型提案
# compose_document，`sources` 写成 **[40, 41, 42]**（资料 id 数字）。
# 参数校验只认"非空字符串"，于是整张卡被拒，界面只留一句
# "AI 建议了 1 个操作，但都没有创建待办：参数 sources 的元素必须是非空字符串"。
# 数字 id 是**合法写法**（下游 _library_by_ref/_files_by_ref 本来就按数字字符串
# 认资料 id），拒绝它属于把合法输入枪毙。


def test_propose_accepts_integer_file_ids_in_sources(admin_user, library, make_file, monkeypatch):
    first = make_file(library, "海口东-陵水.pdf")
    second = make_file(library, "陵水-海口东.pdf")

    def fake_compose(user, *, title, topic, conversation=None, library_codes=None,
                     source_file_ids=None, template_id=None, client=None):
        return {"document_id": 4242, "title": title}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    proposal = services.propose_operation(
        admin_user,
        intent="compose_document",
        params={
            "title": "出行报告",
            "topic": "陶鑫宇 陵水 行程",
            "sources": [first.pk, second.pk],
        },
    )
    assert proposal["status"] == OperationRecord.Status.EXECUTED, "数字 id 来源又把提案弄废了"
    assert proposal["auto_executed"] is True
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.params["sources"] == [str(first.pk), str(second.pk)], (
        "数字 id 应当归一成字符串存库，避免同一份资料有两种写法"
    )


def test_execute_routes_integer_file_ids_into_source_file_ids(
    admin_user, library, make_file, monkeypatch
):
    record = make_file(library, "陶鑫宇提交的出差申请.pdf")
    captured: dict = {}

    def fake_compose(user, *, title, topic, conversation=None, library_codes=None,
                     source_file_ids=None, template_id=None, client=None):
        captured.update(library_codes=library_codes, source_file_ids=source_file_ids)
        return {"document_id": 4244, "title": title}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    _compose_via_controlled(
        admin_user,
        {"title": "出行报告", "topic": "陵水", "sources": [record.pk]},
    )
    assert captured["source_file_ids"] == [record.pk]
    assert captured["library_codes"] is None


def test_target_library_ids_resolves_integer_id(admin_user, library, make_file):
    record = make_file(library, "海口东-陵水.pdf")
    assert services._target_library_ids({"sources": [str(record.pk)]}) == [library.pk]


@pytest.mark.parametrize(
    "bad",
    [None, True, "", "   ", {"id": 40}, ["40"], 40.5],
    ids=["null", "true", "空串", "空白串", "对象", "嵌套数组", "浮点"],
)
def test_sources_still_rejects_junk_elements(admin_user, bad):
    """放宽的是"数字 id 这种合法写法"，不是"随便什么都能过"。"""
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user,
            intent="compose_document",
            params={"title": "出行报告", "topic": "陵水", "sources": [bad]},
        )
    assert "sources" in str(exc.value)


def test_sources_must_be_an_array(admin_user):
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user,
            intent="compose_document",
            params={"title": "出行报告", "topic": "陵水", "sources": "40"},
        )
    assert "必须是数组" in str(exc.value)


def test_tool_schema_tells_the_model_what_sources_accepts():
    """工具声明必须写清 sources 能写什么，并从源头声明 items 类型。"""
    tool = next(item for item in services.tool_schema() if item["function"]["name"] == "compose_document")
    spec = tool["function"]["parameters"]["properties"]["sources"]
    assert spec["type"] == "array"
    assert spec["items"] == {"type": "string"}, "数组不声明 items 时模型会自由发挥"
    assert "资料 id" in spec["description"]
    assert "信息库 code" in spec["description"]
    assert services.OPERATIONS["compose_document"].params["sources"] == "refs?"
    assert "sources" in services.SYSTEM_PROMPT

