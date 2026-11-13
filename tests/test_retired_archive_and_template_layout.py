import io
from types import SimpleNamespace

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.styles.style import ParagraphStyle


@pytest.mark.django_db
def test_retired_archiving_is_absent_from_catalog_and_rejected_by_api(
    auth_api, admin_user
):
    from apps.planner.models import OperationRecord

    api = auth_api(admin_user)
    catalog = api.get("/api/v1/planner/catalog/").data["results"]
    assert not {"archive_file", "unarchive_file"} & {row["intent"] for row in catalog}
    for intent in ["archive_file", "unarchive_file"]:
        assert (
            api.post(
                "/api/v1/planner/operations/propose/",
                {"intent": intent, "params": {"file_id": 1}},
                format="json",
            ).status_code
            == 410
        )
        operation = OperationRecord.objects.create(
            user=admin_user, intent=intent, params={"file_id": 1}
        )
        for action in ["confirm", "execute", "resume"]:
            assert (
                api.post(
                    f"/api/v1/planner/operations/{operation.pk}/{action}/",
                    {},
                    format="json",
                ).status_code
                == 410
            )
        operation.refresh_from_db()
        assert operation.status == OperationRecord.Status.PROPOSED


def test_real_template_direct_layout_and_footer_controls_survive_export(
    tmp_path, monkeypatch
):
    from apps.planner.document_export import document_to_docx
    from apps.planner import document_templates

    template = Document()
    normal_style = template.styles["Normal"]
    assert isinstance(normal_style, ParagraphStyle)
    normal_style.font.size = Pt(16)
    title = template.add_paragraph("标题示例")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.runs[0].font.size = Pt(22)
    title.runs[0].bold = False
    heading = template.add_paragraph("一级标题示例", style="Heading 1")
    heading.paragraph_format.first_line_indent = Pt(32)
    heading._p.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = 8
    body = template.add_paragraph("正文示例")
    body.paragraph_format.first_line_indent = Pt(32)
    body.paragraph_format.line_spacing = Pt(28)
    body.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    sdt = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    paragraph = OxmlElement("w:p")
    for tag, value in [
        ("t", "页码在页面底部，格式带横线"),
        ("instrText", " PAGE "),
        ("t", "1"),
    ]:
        run = OxmlElement("w:r")
        if tag == "instrText":
            start = OxmlElement("w:fldChar")
            start.set(qn("w:fldCharType"), "begin")
            run.append(start)
        element = OxmlElement("w:" + tag)
        element.text = value
        run.append(element)
        paragraph.append(run)
    end = OxmlElement("w:r")
    char = OxmlElement("w:fldChar")
    char.set(qn("w:fldCharType"), "end")
    end.append(char)
    paragraph.append(end)
    content.append(paragraph)
    sdt.append(content)
    template.sections[0].footer._element.append(sdt)
    path = tmp_path / "template.docx"
    template.save(path)
    monkeypatch.setattr(document_templates, "template_path", lambda value: path)
    result = document_to_docx(
        SimpleNamespace(
            title="报告",
            content_markdown="报告\n\n## 一、情况\n\n正文。",
            template=SimpleNamespace(suffix=".docx", category="official"),
        )
    )
    output = Document(io.BytesIO(result))
    assert [p.text for p in output.paragraphs].count("报告") == 1
    assert output.paragraphs[0].runs[0].bold is False
    section_heading = next(p for p in output.paragraphs if p.text == "一、情况")
    properties = section_heading._p.pPr
    assert properties is not None
    assert properties.numPr.numId.val == 0
    paragraph = next(p for p in output.paragraphs if p.text == "正文。")
    assert paragraph.paragraph_format.first_line_indent == Pt(32)
    assert paragraph.paragraph_format.line_spacing_rule == WD_LINE_SPACING.EXACTLY
    assert paragraph.paragraph_format.line_spacing == Pt(28)
    footer = output.sections[0].footer._element
    assert "页码在页面底部" not in footer.xml
    assert footer.xpath(".//w:sdt") and footer.xpath(".//w:instrText")
    assert output.sections[0].page_width == template.sections[0].page_width
