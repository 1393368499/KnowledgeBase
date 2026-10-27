import hashlib
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=1)
def font():
    choices = [getattr(settings, "VOICE_FONT_PATH", ""),
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "C:/Windows/Fonts/msyh.ttc"]
    choices += [str(p) for p in Path(settings.MEDIA_ROOT, "fonts").glob("**/*") if p.suffix.lower() in {".ttf", ".ttc", ".otf"}]
    for choice in choices:
        if choice and Path(choice).is_file():
            return ImageFont.truetype(choice, 20)
    raise RuntimeError("缺少中文显示字体，请配置 VOICE_FONT_PATH")


@lru_cache(maxsize=16)
def page_lines(text):
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[图片：\1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = text.replace("**", "").replace("```", "")
    f = font()
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for char in paragraph:
            if line and f.getlength(line + char) > 376:
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    return [lines[i:i + 9] for i in range(0, len(lines), 9)] or [[""]]


@lru_cache(maxsize=128)
def frame(text, title, index=0):
    pages = page_lines(text)
    if index < 0 or index >= len(pages):
        raise IndexError("页面不存在")
    im = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(im)
    f = font()
    draw.text((12, 4), title[:18], font=f, fill=0)
    draw.line((10, 32, 390, 32), fill=0)
    for number, line in enumerate(pages[index]):
        draw.text((12, 40 + 25 * number), line, font=f, fill=0)
    draw.text((12, 273), f"{index + 1}/{len(pages)}  上下键翻页", font=f, fill=0)
    raw = im.tobytes()
    return raw, hashlib.sha256(raw).hexdigest(), len(pages)
