"""模板分类、真实PPT解析/导出，以及文档归组存储详情的权限与分页。"""
import io
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from pptx import Presentation
from pptx.shapes.autoshape import Shape
from pptx.util import Inches

from apps.knowledge.models import DocumentChunk
from apps.planner.document_export import build_export
from apps.planner.document_templates import create_template, delete_template, template_path
from apps.planner.models import DocumentTemplate, OperationRecord
from apps.planner import services
from apps.planner import document_templates
from tests.planner_fixtures import legacy_proposal

pytestmark = pytest.mark.django_db


def test_upload_handles_unwritable_template_directory(auth_api, user, monkeypatch):
    root = document_templates.templates_root()
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == root:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    response = auth_api(user).post("/api/v1/planner/templates/", {
        "file": SimpleUploadedFile("模板.md", "# 公文模板\n标题\n正文".encode()),
    }, format="multipart")
    assert response.status_code == 503
    assert response.data["code"] == "template_storage_unavailable"
    assert "写入权限" in response.data["detail"]
    assert str(root) not in response.data["detail"]
    assert not DocumentTemplate.objects.exists()


def test_failed_template_publish_cleans_partial_file(auth_api, user, monkeypatch):
    root = document_templates.templates_root()
    def fail_publish(*args):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(document_templates.os, "replace", fail_publish)
    response = auth_api(user).post("/api/v1/planner/templates/", {
        "file": SimpleUploadedFile("模板.md", "# 公文模板\n标题\n正文".encode()),
    }, format="multipart")
    assert response.status_code == 503
    assert not list(root.glob("*.part"))
    assert not DocumentTemplate.objects.exists()


def pptx_bytes():
    presentation = Presentation()
    presentation.slide_width = Inches(12)
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    title, body = slide.shapes.title, slide.placeholders[1]
    assert title is not None and isinstance(body, Shape)
    title.text_frame.paragraphs[0].add_run().text = "项目汇报范本"
    body.text_frame.paragraphs[0].add_run().text = "项目概况\n进度安排\n风险分析"
    stream = io.BytesIO()
    presentation.save(stream)
    return stream.getvalue()


def test_ppt_upload_classification_and_export(auth_api, user):
    api = auth_api(user)
    result = api.post("/api/v1/planner/templates/", {
        "file": SimpleUploadedFile("汇报.pptx", pptx_bytes()),
        "category": "presentation", "name": "项目汇报", "description": "项目进度汇报用",
    }, format="multipart")
    assert result.status_code == 201, result.data
    row = result.data
    assert row["category_label"] == "PPT模板"
    template = DocumentTemplate.objects.get(pk=row["id"])
    assert "第1页" in template.content_text and "项目概况" in template.content_text
    # compose_document 现在要求先选参考文件（指定后直接生成、无需确认）；
    # 本用例验证的是"给待执行的操作挂模板"这条旧协议路径，因此用 legacy 夹具
    # 模拟一条策略变更前已存在的待确认记录（不是公共 API 的新行为）。
    proposal = legacy_proposal(user, intent="compose_document", params={"title": "汇报", "topic": "进展"})
    selected = api.post(f"/api/v1/planner/operations/{proposal['operation_id']}/template/",
                        {"template_id": template.pk}, format="json")
    assert selected.status_code == 200
    assert OperationRecord.objects.get(pk=proposal["operation_id"]).params["template_id"] == template.pk
    export = build_export(SimpleNamespace(title="新汇报", content_markdown="## 项目概况\n- 本期完成任务\n\n## 风险\n- 资源不足",
                                         template=template), "pptx")
    presentation = Presentation(io.BytesIO(export.content))
    assert presentation.slide_width == Inches(12)
    assert len(presentation.slides) == 2
    text = "\n".join(s.text for slide in presentation.slides for s in slide.shapes if isinstance(s, Shape))
    assert "本期完成任务" in text and "资源不足" in text
    assert "项目汇报范本" not in text


@pytest.mark.parametrize("name,category", [("memo.md", "official"), ("data.csv", "other")])
def test_template_category_filter(auth_api, user, name, category):
    api = auth_api(user)
    raw = "# 会议纪要\n时间\n议程".encode() if name.endswith("md") else "项目,金额\n预算,100".encode()
    response = api.post("/api/v1/planner/templates/", {
        "file": SimpleUploadedFile(name, raw), "category": category, "description": "测试用途",
    }, format="multipart")
    assert response.status_code == 201, response.data
    rows = api.get(f"/api/v1/planner/templates/?category={category}&q=测试用途").data["results"]
    assert len(rows) == 1 and rows[0]["category"] == category
    assert not api.get("/api/v1/planner/templates/?q=不存在的模板").data["results"]


def test_bad_category_and_corrupt_ppt_rejected(auth_api, user):
    api = auth_api(user)
    for name, raw, category in [("x.md", b"# hi", "invalid"), ("x.pptx", b"broken", "presentation")]:
        response = api.post("/api/v1/planner/templates/", {
            "file": SimpleUploadedFile(name, raw), "category": category,
        }, format="multipart")
        assert response.status_code == 400
    assert DocumentTemplate.objects.count() == 0


def test_delete_shared_blob_keeps_other_template(user):
    first = create_template(raw=b"# format", original_name="a.md", user=user)
    second = create_template(raw=b"# format", original_name="b.md", user=user)
    path = template_path(second)
    delete_template(first)
    assert path.is_file()
    delete_template(second)
    assert not path.exists()


def test_grouped_documents_and_chunk_pagination(auth_api, user, library, grant_member, make_file, collection):
    grant_member(library, user)
    first = make_file(library, "甲.md", chunk_count=3, embedded_chunk_count=3,
                      vector_status="indexed", vector_collection_name=collection.name, vector_generation=1)
    second = make_file(library, "乙.md", chunk_count=1, embedded_chunk_count=0)
    for file, size in [(first, 3), (second, 1)]:
        for i in range(size):
            DocumentChunk.objects.create(file=file, ordinal=i, text=f"{file.original_name}分段{i}", page=i + 1)
    api = auth_api(user)
    response = api.get(f"/api/v1/knowledge/libraries/{library.pk}/documents/?page_size=1")
    assert response.status_code == 200
    assert response.data["count"] == 2 and response.data["next"]
    assert response.data["totals"] == {"documents": 2, "parsed_chunks": 4, "embedded_chunks": 3}
    filtered = api.get(f"/api/v1/knowledge/libraries/{library.pk}/documents/?q=甲").data
    assert [row["id"] for row in filtered["results"]] == [first.pk]
    details = api.get(f"/api/v1/knowledge/files/{first.pk}/stored-info/?page_size=2").data
    assert details["count"] == 3 and len(details["results"]) == 2
    assert all(row["file"] == first.pk and row["point_id"] for row in details["results"])
    assert details["vector"]["dimension"] == 1024
    assert details["vector"]["embedded_chunks"] == 3
    page2 = api.get(f"/api/v1/knowledge/files/{first.pk}/stored-info/?page_size=2&page=2").data
    assert page2["results"][0]["ordinal"] == 2


def test_storage_details_do_not_leak_private_libraries(auth_api, other_user, library, make_file):
    file = make_file(library)
    api = auth_api(other_user)
    assert api.get(f"/api/v1/knowledge/libraries/{library.pk}/documents/").status_code == 404
    assert api.get(f"/api/v1/knowledge/files/{file.pk}/stored-info/").status_code == 404


def test_templates_and_details_require_login(api, library, make_file):
    file = make_file(library)
    for url in ["/api/v1/planner/templates/", f"/api/v1/knowledge/libraries/{library.pk}/documents/",
                f"/api/v1/knowledge/files/{file.pk}/stored-info/"]:
        assert api.get(url).status_code in {401, 403}


def test_ppt_template_prompt_reaches_writer(user, library, grant_member, make_file):
    grant_member(library, user)
    file = make_file(library)
    DocumentChunk.objects.create(file=file, ordinal=0, text="项目已完成验收")
    template = create_template(raw=pptx_bytes(), original_name="汇报.pptx", user=user)
    calls = []
    class Writer:
        def chat(self, messages):
            calls.append(messages)
            return SimpleNamespace(content="## 进展\n- 已完成验收", model="test", usage={})
    result = services.compose_document(user, title="汇报", topic="验收", source_file_ids=[file.pk],
                                       template_id=template.pk, client=Writer())  # pyright: ignore[reportArgumentType]
    assert result["template_id"] == template.pk
    assert "演示模板" in calls[0][0]["content"]
    assert "项目概况" in calls[0][0]["content"]


def test_word_template_keeps_page_setup_header_and_style(user):
    from docx import Document
    from docx.shared import Inches, Pt
    original = Document()
    original.sections[0].left_margin = Inches(1.6)
    original.sections[0].header.paragraphs[0].text = "单位公文抬头"
    from docx.styles.style import ParagraphStyle

    original_style = original.styles["Normal"]
    assert isinstance(original_style, ParagraphStyle)
    original_style.font.size = Pt(16)
    original.add_paragraph("模板旧正文")
    source = io.BytesIO()
    original.save(source)
    template = create_template(raw=source.getvalue(), original_name="公文.docx", user=user)
    export = build_export(SimpleNamespace(title="新通知", content_markdown="# 新通知\n\n这是真实正文。", template=template), "docx")
    output = Document(io.BytesIO(export.content))
    assert output.sections[0].left_margin == Inches(1.6)
    assert "单位公文抬头" in output.sections[0].header.paragraphs[0].text
    output_style = output.styles["Normal"]
    assert isinstance(output_style, ParagraphStyle)
    assert output_style.font.size == Pt(16)
    assert "这是真实正文。" in "\n".join(p.text for p in output.paragraphs)
    assert "模板旧正文" not in "\n".join(p.text for p in output.paragraphs)

