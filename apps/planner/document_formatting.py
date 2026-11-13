"""文档字体和颜色白名单，供编辑器及导出共同使用。"""
FONTS = {
    "xiaobiaosong": "方正小标宋简体", "fangsong": "仿宋",
    "heiti": "黑体", "kaiti": "楷体", "simsun": "宋体",
}
COLORS = {"black": "000000", "red": "FF0000"}
FIELDS = {"title_font", "body_font", "heading_font", "subheading_font", "title_color", "text_color"}


def validate_format_options(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("文档格式必须是有效的字体和颜色设置")
    clean = {}
    for key, selected in value.items():
        allowed = FONTS if key.endswith("_font") else COLORS
        if not isinstance(selected, str) or selected not in {"", *allowed}:
            raise ValueError("不支持的字体或颜色")
        if selected:
            clean[key] = selected
    return clean


def format_docx(data, options):
    if not options:
        return data
    import io
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import RGBColor
    document = Document(io.BytesIO(data))
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        paragraphs.extend(p for row in table.rows for cell in row.cells for p in cell.paragraphs)
    def heading_level(paragraph):
        name = paragraph.style.name if paragraph.style else ""
        if name.startswith("Heading ") and name.split()[-1].isdigit():
            return int(name.split()[-1])
        outline = paragraph._p.xpath("./w:pPr/w:outlineLvl/@w:val")
        return int(outline[0]) if outline else None
    levels = [heading_level(p) for p in paragraphs[1:]]
    first_level = min((level for level in levels if level is not None), default=1)
    for i, paragraph in enumerate(paragraphs):
        level = heading_level(paragraph)
        is_title = i == 0
        first_heading = level == first_level
        second_heading = level is not None
        key = "title_font" if is_title else "heading_font" if first_heading else "subheading_font" if second_heading else "body_font"
        font = FONTS.get(options.get(key))
        color = COLORS.get(options.get("title_color" if is_title else "text_color"))
        for run in paragraph.runs:
            if font:
                run.font.name = font
                run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), font)
            if color:
                run.font.color.rgb = RGBColor.from_string(color)
    stream = io.BytesIO()
    document.save(stream)
    from .official_layout import preserve_package_parts
    return preserve_package_parts(data, stream.getvalue(), {"word/document.xml"})


def format_html(html, options):
    rules = []
    for key, selector in [("body_font", "body"), ("title_font", "h1"),
                          ("heading_font", "h2"), ("subheading_font", "h3,h4")]:
        if options.get(key) in FONTS:
            rules.append(f'{selector}{{font-family:"{FONTS[options[key]]}",serif}}')
    for key, selector in [("text_color", "body,body *"), ("title_color", "h1,h1 *")]:
        if options.get(key) in COLORS:
            rules.append(f'{selector}{{color:#{COLORS[options[key]]}}}')
    return html.replace("</head>", "<style>" + "".join(rules) + "</style></head>")
