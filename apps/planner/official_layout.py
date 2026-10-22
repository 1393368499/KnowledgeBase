"""复用公文范本的段落直接格式，而不仅是它的样式表。"""

from copy import deepcopy
import io
import re
import zipfile

from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

DATE = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$")
NUMBER = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|[（(][一二三四五六七八九十百]+[）)]|\d+[.．])"
)


def preserve_package_parts(original, generated, changed_parts):
    """保留未编辑的 OOXML 部件原始字节，避免对象模型保存时无关重写。"""
    stream = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as source,
        zipfile.ZipFile(io.BytesIO(generated)) as updated,
    ):
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
            original_names = set(source.namelist())
            for item in updated.infolist():
                data = updated.read(item.filename)
                if (
                    item.filename in original_names
                    and item.filename not in changed_parts
                ):
                    data = source.read(item.filename)
                output.writestr(item, data)
    return stream.getvalue()


class OfficialLayout:
    def __init__(self, document):
        self.document = document
        paragraphs = list(document.paragraphs)
        self.samples = {}
        self.title_gap = 0
        self.signature_gap = 0
        self.line_spacing = Pt(28)
        self.modified_parts = {"word/document.xml", "word/settings.xml"}
        for index, paragraph in enumerate(paragraphs):
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name.lower()
            if style.startswith("heading ") and style[-1:].isdigit():
                self.samples.setdefault("Heading " + style[-1], paragraph)
                if not self.title_gap:
                    before = index - 1
                    while before >= 0 and not paragraphs[before].text.strip():
                        self.title_gap += 1
                        before -= 1
            if index < 3 and paragraph.alignment == WD_ALIGN_PARAGRAPH.CENTER:
                self.samples.setdefault("title", paragraph)
            if DATE.match(text):
                if (
                    paragraph.alignment is None
                    and not paragraph.paragraph_format.left_indent
                ):
                    continue
                self.samples.setdefault("date", paragraph)
                previous = index - 1
                while previous >= 0 and not paragraphs[previous].text.strip():
                    previous -= 1
                if previous >= 0:
                    self.samples.setdefault("signature", paragraphs[previous])
                    before = previous - 1
                    while before >= 0 and not paragraphs[before].text.strip():
                        self.signature_gap += 1
                        before -= 1
        candidates = [
            p
            for p in paragraphs
            if p.text.strip()
            and p.style.name == "Normal"
            and p.paragraph_format.first_line_indent
            and p.alignment not in (WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.RIGHT)
        ]
        if candidates:
            sample = max(
                candidates,
                key=lambda p: p.paragraph_format.line_spacing_rule
                == WD_LINE_SPACING.EXACTLY,
            )
            self.samples["Normal"] = sample
            if sample.paragraph_format.line_spacing_rule == WD_LINE_SPACING.EXACTLY:
                self.line_spacing = sample.paragraph_format.line_spacing

    def apply(self, paragraph, role):
        sample = self.samples.get(role)
        if sample is not None:
            if paragraph._p.pPr is not None:
                paragraph._p.remove(paragraph._p.pPr)
            if sample._p.pPr is not None:
                paragraph._p.insert(0, deepcopy(sample._p.pPr))
            paragraph.style = sample.style
            source_run = next((r for r in sample.runs if r.text.strip()), None)
            source_rpr = source_run._r.rPr if source_run is not None else None
            for run in paragraph.runs:
                if source_rpr is not None:
                    if run._r.rPr is not None:
                        run._r.remove(run._r.rPr)
                    run._r.insert(0, deepcopy(source_rpr))
        else:
            paragraph.paragraph_format.first_line_indent = Pt(
                0 if role == "title" else 32
            )
            if role == "title":
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.font.size = Pt(22)
                    run.font.name = "方正小标宋简体"
                    run._r.get_or_add_rPr().rFonts.set(
                        qn("w:eastAsia"), "方正小标宋简体"
                    )
        paragraph.paragraph_format.line_spacing = self.line_spacing
        paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        if role.startswith("Heading") and NUMBER.match(paragraph.text.strip()):
            # 文本已有序号时覆盖样式继承的自动编号，防止“一、一、”。
            num = paragraph._p.get_or_add_pPr().get_or_add_numPr()
            num.get_or_add_numId().val = 0
        if role == "title":
            paragraph.paragraph_format.keep_with_next = True
        # 已安装的中文字体采用 Windows 新名称；旧 GB2312 名称保持同字体族。
        font_aliases = {"楷体_GB2312": "楷体", "仿宋_GB2312": "仿宋"}
        for run in paragraph.runs:
            fonts = run._r.rPr.rFonts if run._r.rPr is not None else None
            inherited = paragraph.style.element.xpath("./w:rPr/w:rFonts/@w:eastAsia")
            name = fonts.get(qn("w:eastAsia")) if fonts is not None else None
            name = name or (inherited[0] if inherited else "")
            if name in font_aliases:
                run.font.name = font_aliases[name]
                run._r.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font_aliases[name])

    def signatures(self):
        paragraphs = list(self.document.paragraphs)
        for index, paragraph in enumerate(paragraphs):
            if not DATE.match(paragraph.text.strip()):
                continue
            previous = next(
                (p for p in reversed(paragraphs[:index]) if p.text.strip()), None
            )
            self.apply(paragraph, "date")
            if "date" not in self.samples:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                paragraph.paragraph_format.first_line_indent = Pt(0)
            if (
                previous is not None
                and len(previous.text.strip()) <= 60
                and not previous.text.strip().startswith("（")
            ):
                self.apply(previous, "signature")
                if "signature" not in self.samples:
                    previous.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    previous.paragraph_format.first_line_indent = Pt(0)
                previous.paragraph_format.keep_with_next = True
                if self.signature_gap:
                    previous.paragraph_format.space_before = (
                        self.line_spacing * self.signature_gap
                    )

    def clean_footer_instructions(self):
        # 页码位于内容控件中，不能仅检查 footer.paragraphs，也不能删除整个控件。
        for relation in self.document.part.rels.values():
            if relation.reltype.endswith("/footer") and not relation.is_external:
                root = relation.target_part._element
                for paragraph in root.xpath(".//w:p"):
                    text = "".join(paragraph.xpath(".//w:t/text()"))
                    if "页码" not in text or "格式" not in text:
                        continue
                    instructions = " ".join(paragraph.xpath(".//w:instrText/text()"))
                    if not re.search(r"\bPAGE\b", instructions):
                        continue
                    self.modified_parts.add(
                        str(relation.target_part.partname).lstrip("/")
                    )
                    in_field = False
                    started = False
                    for run in paragraph.findall(qn("w:r")):
                        for child in run:
                            if child.tag == qn("w:fldChar"):
                                kind = child.get(qn("w:fldCharType"))
                                in_field = kind != "end"
                            if child.tag == qn("w:t") and not in_field:
                                child.text = "- " if not started else ""
                                child.set(qn("xml:space"), "preserve")
                                started = True
                    run = OxmlElement("w:r")
                    rpr = OxmlElement("w:rPr")
                    fonts = OxmlElement("w:rFonts")
                    for attr in ("ascii", "hAnsi", "eastAsia"):
                        fonts.set(qn("w:" + attr), "仿宋")
                    rpr.append(fonts)
                    size = OxmlElement("w:sz")
                    size.set(qn("w:val"), "28")
                    rpr.append(size)
                    run.append(rpr)
                    ending = OxmlElement("w:t")
                    ending.set(qn("xml:space"), "preserve")
                    ending.text = " -"
                    run.append(ending)
                    paragraph.append(run)
        settings = self.document.settings.element
        update = settings.find(qn("w:updateFields"))
        if update is None:
            update = OxmlElement("w:updateFields")
            settings.append(update)
        update.set(qn("w:val"), "true")
