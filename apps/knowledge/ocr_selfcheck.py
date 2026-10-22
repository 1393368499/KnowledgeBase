"""OCR 自检样图与判定：**固定在仓库里、已被真实模型验证过**的样本。

## 为什么要固定样图（独立复现的真实事故）

旧实现现场用 PIL 渲染样图，字体候选是 Windows 的 msyh/simhei/arial 与 DejaVu：
**Docker 镜像里一个都没有** → ``ImageFont.load_default()`` 退回内置位图字体 →
1100×360 的图里整行英文的实际墨迹只有约 **138×9** 像素 → 送进 OCR 得到的是
重复的幻觉文本，看起来像"OCR 服务坏了"，其实是**自检样本坏了**
（同一台服务用 1200×420、微软雅黑 48 号渲染的清晰样图立刻 3/3 正确）。

因此这里的口径是：

* **正常路径**：读仓库里固定的 ``data/ocr_selfcheck.png``（1200×420、48px 中文；
  2026-10-16 用真实 ``OcrClient`` 对 AS711 实测：三行中英文全部识别正确，1.27s）；
* **渲染路径**（可选，``render_sample``）：只在**真的找到可用中文字体**时才渲染，
  找不到就**明确失败**（``NoUsableFontError``），绝不退回到不可读的位图字体，
  更不把不可读样本当成"模型失败"；
* **判定**：用关键词而不是整句（OCR 可能在标点/空格上有差异），并把缺了哪些词
  如实报出来。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: 固定样图（随代码一起进镜像；只读，不被运行时改写）
SAMPLE_PATH = Path(__file__).resolve().parent / "data" / "ocr_selfcheck.png"

#: 样图里的三行原文（供人工核对；判定用下面的关键词）
EXPECTED_LINES = (
    "质保期为验收合格之日起 12 个月。",
    "合同编号 HT-2026-0417，甲方：华东石化。",
    "Warranty period: 12 months after acceptance.",
)

#: 判定关键词：OCR 在空格/标点上可能有差异，用关键词更稳
EXPECTED_NEEDLES = ("质保期", "12 个月", "HT-2026-0417", "Warranty")

#: 渲染样图时可接受的字体（**必须是真正的中文字体**；DejaVu/内置位图不算）
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)

SAMPLE_SIZE = (1200, 420)
SAMPLE_FONT_SIZE = 48


class NoUsableFontError(RuntimeError):
    """镜像里没有可用的中文字体 —— 这种时候**不能**渲染自检样图。"""


@lru_cache(maxsize=1)
def sample_bytes() -> bytes:
    """固定样图的字节（只读；被测/被识别都只用它）。"""
    return SAMPLE_PATH.read_bytes()


def normalize(text: str) -> str:
    """去掉空白后比较：OCR 常在中文/数字之间插空格。"""
    return "".join((text or "").split())


def missing_needles(text: str) -> list[str]:
    """返回样图关键词里**没被识别出来**的那些（空列表 = 全部识别正确）。"""
    got = normalize(text)
    return [needle for needle in EXPECTED_NEEDLES if normalize(needle) not in got]


def render_sample() -> bytes:
    """用真实中文字体渲染样图；找不到字体就**明确失败**（不退回位图字体）。

    这条路只在需要重新生成固定样图/在镜像里做字体回归时用。
    """
    from PIL import Image, ImageDraw, ImageFont

    chosen = next((path for path in FONT_CANDIDATES if Path(path).is_file()), "")
    if not chosen:
        raise NoUsableFontError(
            "镜像里没有可用的中文字体，渲染出来的样图会不可读（旧实现正是在这里"
            "退回内置位图字体，把整行文字压成约 138×9 像素，导致 OCR 幻觉）。"
            "请安装 fonts-noto-cjk（见 deploy/app/Dockerfile）或直接使用固定样图。"
        )
    image = Image.new("RGB", SAMPLE_SIZE, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(chosen, SAMPLE_FONT_SIZE)
    y = 40
    for line in EXPECTED_LINES:
        draw.text((40, y), line, fill="black", font=font)
        y += 110
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
