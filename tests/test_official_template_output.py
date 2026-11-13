"""公文模板真正生效：提示词结构契约 + 占位符守卫 + Word 导出排印 + 预览口径。

背景（实测 2026-10-19）：用户套着「公文基本规范模板」写《出差出行报告》，产出的是
"要点 + 表格"的 PPT 体；落款里还照抄了模板的示例电话 `13xxxxxxx`，成文日期写成
2026年10月15日（与资料/当天都不符）。四层都要有可回归的护栏。
"""

from __future__ import annotations

import io
import json
from datetime import date

import httpx
import pytest
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from apps.planner import document_export, document_templates as dt, services
from apps.planner.llm import LlmClient, LlmProfile
from apps.planner.models import DerivedDocument, DocumentTemplate

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------- 工具 ----


def _text_reply(text: str) -> httpx.Response:
    payload = {
        "id": "chatcmpl-style",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    return httpx.Response(200, json=payload)


def _fake_llm(handler) -> LlmClient:
    return LlmClient(
        LlmProfile(base_url="http://llm.test/v1", model="test-model"),
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def _docx_bytes(paragraphs: list[str]) -> bytes:
    document = DocxDocument()
    for line in paragraphs:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _chunked(library, make_file, *, name="出差申请.md", count=2):
    """撰写要用"已处理且有分段"的资料，否则会以"没有可用的分段"被拒。"""
    from apps.knowledge.models import DocumentChunk, ParseStatus

    record = make_file(library, name=name, status=ParseStatus.DONE)
    for ordinal in range(0, count):
        DocumentChunk.objects.create(
            file=record, ordinal=ordinal, text=f"第{ordinal}段：出差事由与行程", token_estimate=8
        )
    type(record)._base_manager.filter(pk=record.pk).update(chunk_count=count)
    record.refresh_from_db()
    return record


def _official_template(user):
    """公文模板（.docx → category=official），正文就是模板里的排版说明。"""
    return dt.create_template(
        raw=_docx_bytes(
            [
                "标题文字方正小标宋体二号",
                "一、一级标题：黑体字三号",
                "（一）二级标题：楷体字三号 加粗",
                "正文：仿宋字三号 每页22行每行28字",
                "落款单位名称",
                "2023年8月30日",
                "（联系人：xxx，13xxxxxxx）",
            ]
        ),
        original_name="01.公文基本规范模板.docx",
        user=user,
    )


def _compose(user, template, *, sources: list[int], handler):
    return services.compose_document(
        user,
        title="出差出行报告",
        topic="陶鑫宇 2026 年 10 月 14 日赴陵水城建集团出差",
        source_file_ids=sources,
        template_id=template.pk,
        client=_fake_llm(handler),
    )


# ------------------------------------------------- 1) 提示词：公文结构契约 ----


def test_official_template_prompt_states_structure_contract(user):
    """单给一段排版说明不够：提示词里必须有可执行的公文结构契约。"""
    block = dt.template_prompt_block(_official_template(user))

    assert "按党政机关公文的行文结构写" in block
    assert "第一行是标题" in block                 # 标题不编号、不写「一、」
    assert "一、" in block and "（一）" in block    # 中文序号层级
    assert "不要用 - / * 项目符号、不要用表格" in block
    assert "落款单位" in block and "成文日期" in block
    assert "13xxxxxxx" in block                    # 占位符被点名禁止
    assert "绝对不能出现在正文里" in block


def test_non_official_template_prompt_has_no_official_contract(user):
    """PPT/其他模板不该被塞进"党政机关公文"那套要求（否则会被写歪）。"""
    template = DocumentTemplate.objects.create(
        name="汇报模板",
        original_name="汇报.pptx",
        suffix=".pptx",
        size_bytes=10,
        content_sha256="a" * 64,
        stored_path="missing.pptx",
        content_text="第 1 页 封面；第 2 页 进展；第 3 页 计划",
        category=DocumentTemplate.Category.PRESENTATION,
        uploaded_by=user,
    )
    block = dt.template_prompt_block(template)

    assert "按党政机关公文的行文结构写" not in block
    assert "不要用 - / * 项目符号、不要用表格" not in block
    # 演示模板自己的口径仍在
    assert "演示模板" in block or "每一页" in block


# ------------------------------------------------- 2) 占位符守卫：扫描口径 ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("（联系人：陶鑫宇，13xxxxxxx）", ["13xxxxxxx"]),
        ("附件：1.XXXX说明", ["XXXX"]),
        ("落款单位：某某单位", ["某某单位"]),
        ("日期：XXXX年XX月XX日", ["XXXX"]),
        ("正文里写了「占位符」三个字", ["占位符"]),
        ("正常的文档正文，电话 13800138000", []),
        ("（联系人：陶鑫宇，13800138000）", []),
    ],
)
def test_placeholder_leak_detection(text, expected):
    """只抓"模板示例占位"这一形态；真实号码、正常正文不能被误伤。"""
    assert services._template_placeholder_leaks(text) == expected


# ------------------------------------------ 3) 撰写：占位符纠正一次后落盘 ----


def test_compose_document_rewrites_once_when_placeholder_copied(
    user, library, make_file, grant_member, monkeypatch
):
    """模型照抄模板占位电话 → 纠正一次；第二次合格就正常保存。"""
    grant_member(library, user)
    # 冻结"今天"：否则成文日期守卫会按运行当天的日期再纠正一轮（第三调用），
    # 这条用例就变成"只在 2026-10-19 当天通过"的定时炸弹。
    monkeypatch.setattr(services.timezone, "localdate", lambda: date(2026, 10, 19))
    record = _chunked(library, make_file)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        if len(calls) == 1:
            return _text_reply(
                "# 出差出行报告\n\n一、基本情况\n\n陶鑫宇于 2026 年 10 月 14 日赴陵水。\n\n"
                "交付中心\n\n2026年10月19日\n\n（联系人：陶鑫宇，13xxxxxxx）"
            )
        return _text_reply(
            "# 出差出行报告\n\n一、基本情况\n\n陶鑫宇于 2026 年 10 月 14 日赴陵水。\n\n"
            "交付中心\n\n2026年10月19日"
        )

    result = _compose(user, _official_template(user), sources=[record.pk], handler=handler)

    assert len(calls) == 2, "第一次照抄了占位符，必须再要一次正文"
    assert "13xxxxxxx" not in result["content_markdown"]
    assert "占位" in calls[1]["messages"][-1]["content"]
    document = DerivedDocument.objects.get(pk=result["document_id"])
    assert "13xxxxxxx" not in document.content_markdown


def test_compose_document_fails_closed_when_placeholder_persists(
    user, library, make_file, grant_member
):
    """纠正后仍带着占位符 → 明确失败、不存草稿（绝不把示例电话存成正式文档）。"""
    grant_member(library, user)
    record = _chunked(library, make_file)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return _text_reply("# 出差出行报告\n\n正文。\n\n（联系人：陶鑫宇，13xxxxxxx）")

    with pytest.raises(services.PlannerError) as excinfo:
        _compose(user, _official_template(user), sources=[record.pk], handler=handler)

    assert len(calls) == 2
    assert "占位符" in str(excinfo.value)
    assert DerivedDocument.objects.filter(created_by=user).count() == 0


def test_compose_document_without_template_does_not_apply_placeholder_guard(
    user, library, make_file, grant_member
):
    """没套模板时不该多问一轮：占位符守卫只在"套了模板"的路径上生效。"""
    grant_member(library, user)
    record = _chunked(library, make_file)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return _text_reply("# 出行报告\n\n正文。\n\n（联系人：xxx，13xxxxxxx）")

    result = services.compose_document(
        user, title="出行报告", topic="出差", source_file_ids=[record.pk],
        client=_fake_llm(handler),
    )
    assert len(calls) == 1
    assert result["document_id"]


# ------------------------------------- 4) Word 导出：按公文口径排印 ----


def _today_cn() -> str:
    from django.utils import timezone

    today = timezone.localdate()
    return f"{today.year}年{today.month}月{today.day}日"


def test_official_template_date_is_corrected_by_model_retry(
    user, library, make_file, grant_member
):
    """成文日期凭空写成 10月15日（生成当天不是那天、资料里也没有）→ 纠正一次。"""
    grant_member(library, user)
    record = _chunked(library, make_file)
    calls: list[dict] = []
    today = _today_cn()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        if len(calls) == 1:
            return _text_reply("# 出差出行报告\n\n一、基本情况\n\n正文。\n\n交付中心\n\n2026年10月15日")
        return _text_reply(f"# 出差出行报告\n\n一、基本情况\n\n正文。\n\n交付中心\n\n{today}")

    result = _compose(user, _official_template(user), sources=[record.pk], handler=handler)

    assert len(calls) == 2
    assert today in result["content_markdown"]
    assert "2026年10月15日" not in result["content_markdown"]
    assert today in calls[1]["messages"][-1]["content"]


def test_official_template_date_is_forced_to_today_when_model_insists(
    user, library, make_file, grant_member
):
    """模型坚持写错日期时，落款日期这一行改成今天（不留假日期，也不丢整份文档）。"""
    grant_member(library, user)
    record = _chunked(library, make_file)
    calls: list[dict] = []
    today = _today_cn()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return _text_reply("# 出差出行报告\n\n一、基本情况\n\n正文。\n\n交付中心\n\n2026年10月15日")

    result = _compose(user, _official_template(user), sources=[record.pk], handler=handler)

    assert len(calls) == 2
    assert today in result["content_markdown"]
    assert "2026年10月15日" not in result["content_markdown"]
    assert result["document_id"], "不该因为日期不对就丢掉整份文档"


def test_official_template_date_from_sources_is_kept(
    user, library, make_file, grant_member
):
    """资料里真实出现过的成文日期要保留（不能一律改成今天）。"""
    from apps.knowledge.models import DocumentChunk

    grant_member(library, user)
    record = _chunked(library, make_file)
    DocumentChunk.objects.create(
        file=record, ordinal=9, text="本文成文日期：2026年10月1日", token_estimate=8
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return _text_reply("# 报告\n\n一、基本情况\n\n正文。\n\n交付中心\n\n2026年10月1日")

    result = _compose(user, _official_template(user), sources=[record.pk], handler=handler)

    assert len(calls) == 1, "资料里有这个日期，不该再纠正"
    assert "2026年10月1日" in result["content_markdown"]


def test_docx_export_maps_chinese_numbered_headings(user):
    """公文分条没有 Markdown `##`：导出时要识别「一、」「（一）」并套标题样式。"""
    template = _official_template(user)
    document = DerivedDocument.objects.create(
        created_by=user,
        title="关于出差情况的报告",
        topic="出差",
        content_markdown=(
            "# 关于出差情况的报告\n\n"
            "一、基本情况\n\n"
            "（一）出差人员\n\n"
            "陶鑫宇随孙海涛赴陵水城建集团出差。\n\n"
            "一、去程票据为海口东至陵水，票价为人民币92.00元。\n\n"
            "交付中心\n\n"
            "2026年10月19日\n"
        ),
        status=DerivedDocument.Status.DRAFT,
        template=template,  # pyright: ignore[reportCallIssue]
        template_name=template.name,  # pyright: ignore[reportCallIssue]
    )

    output = DocxDocument(io.BytesIO(document_export.document_to_docx(document)))
    paragraphs = [p for p in output.paragraphs if (p.text or "").strip()]
    by_text = {p.text.strip(): p for p in paragraphs}

    assert by_text["一、基本情况"].style is not None
    assert by_text["（一）出差人员"].style is not None
    assert by_text["一、去程票据为海口东至陵水，票价为人民币92.00元。"].style is not None
    assert by_text["一、基本情况"].style.name == "Heading 1"
    assert by_text["（一）出差人员"].style.name == "Heading 2"
    # 分条正文（长句、以句号结尾）不能被刷成标题
    assert by_text["一、去程票据为海口东至陵水，票价为人民币92.00元。"].style.name == "Normal"

    # 落款单位与成文日期各占一段、都居右
    assert by_text["交付中心"].alignment == WD_ALIGN_PARAGRAPH.RIGHT
    assert by_text["2026年10月19日"].alignment == WD_ALIGN_PARAGRAPH.RIGHT


def test_docx_export_attachment_file_names_are_not_headings(user):
    """附件清单「2.陵水-海口东.pdf」像编号标题，但它是文件名，不能刷成标题。"""
    template = _official_template(user)
    document = DerivedDocument.objects.create(
        created_by=user,
        title="报告",
        topic="出差",
        content_markdown=(
            "# 报告\n\n一、基本情况\n\n正文。\n\n附件：1.海口东-陵水.pdf\n2.陵水-海口东.pdf\n"
        ),
        status=DerivedDocument.Status.DRAFT,
        template=template,  # pyright: ignore[reportCallIssue]
        template_name=template.name,  # pyright: ignore[reportCallIssue]
    )

    output = DocxDocument(io.BytesIO(document_export.document_to_docx(document)))
    by_text = {p.text.strip(): p for p in output.paragraphs if (p.text or "").strip()}

    style = by_text["2.陵水-海口东.pdf"].style
    assert style is not None and style.name == "Normal"


def test_docx_export_uses_official_typography(user):
    """标题居中二号、一级标题用 Heading 1、落款与日期居右 —— 不再"退化成长正文"。"""
    template = _official_template(user)
    document = DerivedDocument.objects.create(
        created_by=user,
        title="关于陶鑫宇赴陵水出差的报告",
        topic="出差",
        content_markdown=(
            "# 关于陶鑫宇赴陵水出差的报告\n\n"
            "## 一、基本情况\n\n"
            "陶鑫宇于 2026 年 10 月 14 日赴陵水城建集团出差。\n\n"
            "交付中心\n\n"
            "2026年10月19日\n"
        ),
        status=DerivedDocument.Status.DRAFT,
        template=template,  # pyright: ignore[reportCallIssue]
        template_name=template.name,  # pyright: ignore[reportCallIssue]
    )

    data = document_export.document_to_docx(document)
    output = DocxDocument(io.BytesIO(data))
    paragraphs = [p for p in output.paragraphs if (p.text or "").strip()]

    title = paragraphs[0]
    assert title.text == "关于陶鑫宇赴陵水出差的报告"
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert title.runs and title.runs[0].font.size == Pt(22)

    # Markdown 的 `##` 是公文"一级标题"：样式必须落到 Heading 1，不再是 Heading 2
    heading = next(p for p in paragraphs if p.text.startswith("一、基本情况"))
    assert heading.style is not None and heading.style.name == "Heading 1"

    # 成文日期居右
    date_para = next(p for p in paragraphs if p.text.strip() == "2026年10月19日")
    assert date_para.alignment == WD_ALIGN_PARAGRAPH.RIGHT

    # 模板里那段"示例公文"正文不能残留在成品里
    assert not any("标题文字方正小标宋体二号" in p.text for p in paragraphs)


def test_docx_export_without_template_still_works(user):
    """没套模板的普通导出不能被公文逻辑带坏（走 markdown_to_docx）。"""
    document = DerivedDocument.objects.create(
        created_by=user,
        title="普通草稿",
        topic="测试",
        content_markdown="# 普通草稿\n\n一段正文。",
        status=DerivedDocument.Status.DRAFT,
    )
    data = document_export.document_to_docx(document)
    output = DocxDocument(io.BytesIO(data))
    assert any(p.text == "普通草稿" for p in output.paragraphs)


# ------------------------------------------- 5) 接口：预览要拿到模板类别 ----


def test_document_detail_exposes_template_category(user, auth_api):
    """预览按模板口径排版，界面必须先拿到 category（只有名字不够）。"""
    template = _official_template(user)
    document = DerivedDocument.objects.create(
        created_by=user,
        title="通知",
        topic="事项",
        content_markdown="# 通知\n\n正文",
        status=DerivedDocument.Status.DRAFT,
        template=template,  # pyright: ignore[reportCallIssue]
        template_name=template.name,  # pyright: ignore[reportCallIssue]
    )
    client = auth_api(user)

    response = client.get(f"/api/v1/planner/documents/{document.pk}/")
    assert response.status_code == 200
    body = response.json()
    body = body.get("body", body) if isinstance(body, dict) else body
    assert body["template_category"] == "official"
    assert body["template_name"] == template.name


def test_document_detail_template_category_empty_without_template(user, auth_api):
    document = DerivedDocument.objects.create(
        created_by=user,
        title="无模板",
        topic="事项",
        content_markdown="# 无模板\n\n正文",
        status=DerivedDocument.Status.DRAFT,
    )
    client = auth_api(user)

    body = client.get(f"/api/v1/planner/documents/{document.pk}/").json()
    body = body.get("body", body) if isinstance(body, dict) else body
    assert body["template_category"] == ""
