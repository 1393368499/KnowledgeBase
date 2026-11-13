"""Read-only, device-scoped pocket dashboard and actual in-progress work."""
import calendar
import hashlib
from functools import lru_cache

from django.utils import timezone
from PIL import Image, ImageDraw

from apps.knowledge.models import IngestedFile, KnowledgeLibrary
from apps.knowledge.permissions import readable_library_ids
from apps.planner.models import OperationRecord, LibraryRebuildRun
from apps.planner.operations import OPERATIONS
from .models import VoiceTurn
from .rendering import font
from .services import ACTIVE


def tasks(device):
    # Scope is checked again on every refresh, including after permissions change.
    libraries = KnowledgeLibrary.objects.filter(is_active=True)
    allowed = readable_library_ids(device.user)
    if allowed is not None:
        libraries = libraries.filter(pk__in=allowed)
    if device.scope_mode == "selected":
        libraries = libraries.filter(code__in=device.library_codes)
    rows = []
    labels = {"queued": "等待识别", "transcribing": "识别语音", "transcribed": "准备回答",
              "answering": "AI 正在回答", "rendering": "生成屏幕内容"}
    seen = set()
    for turn in VoiceTurn.objects.filter(device=device, state__in=ACTIVE).order_by("created_at")[:12]:
        if turn.conversation_id in seen:
            continue
        seen.add(turn.conversation_id)
        rows.append(labels[turn.state] + ("：" + turn.transcript[:14] if turn.transcript else ""))
        if len(seen) == 4:
            break
    for op in OperationRecord.objects.filter(user=device.user, status="executing").order_by("created_at")[:3]:
        spec = OPERATIONS.get(op.intent)
        # Target names may refer to permissions the account no longer has.
        rows.append("执行：" + (spec.label if spec else "知识库操作"))
    for run in LibraryRebuildRun.objects.filter(library__in=libraries, status="running").select_related("library")[:2]:  # pyright: ignore[reportCallIssue]
        rows.append("重建：" + run.library.name[:28])
    for item in IngestedFile.objects.filter(library__in=libraries, parse_status__in=["pending", "running"]).order_by("id")[:4]:
        rows.append(("解析：" if item.parse_status == "running" else "排队：") + item.original_name[:28])
    return tuple(rows[:4])


def _lines(text, face, width, count):
    """Fit measured text, including unbroken filenames, into a bounded area."""
    text = " ".join(str(text).split())
    lines, line = [], ""
    for char in text:
        if line and face.getlength(line + char) > width:
            lines.append(line)
            line = ""
        line += char
    lines.append(line)
    if len(lines) > count:
        lines = lines[:count]
        while lines[-1] and face.getlength(lines[-1] + "…") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


@lru_cache(maxsize=32)
def render(minute, rows, selection="", task_title="", task_state=""):
    now = timezone.datetime.fromisoformat(minute)
    image = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(image)
    small = font().font_variant(size=13)
    date = font().font_variant(size=17)
    title = font().font_variant(size=19)
    clock = font().font_variant(size=29)
    weekday = "一二三四五六日"[now.weekday()]
    draw.text((12, 9), now.strftime("%Y/%m/%d") + "  周" + weekday, font=date, fill=0)
    time = now.strftime("%H:%M")
    draw.text((388 - clock.getlength(time), -1), time, font=clock, fill=0)
    draw.line((10, 39, 390, 39), fill=0)

    # The destination of the next recording is the primary information.
    draw.rounded_rectangle((10, 48, 390, 128), radius=7, outline=0)
    draw.text((21, 53), selection or "当前：新任务", font=small, fill=0)
    state = task_state or ("准备就绪" if not rows else "后台处理中")
    state = _lines(state, small, 108, 1)[0]
    draw.text((378 - small.getlength(state), 53), state, font=small, fill=0)
    if task_title:
        for i, line in enumerate(_lines(task_title, title, 356, 2)):
            draw.text((21, 76 + i * 23), line, font=title, fill=0)
    else:
        draw.text((21, 76), "准备好，开始下一件事", font=title, fill=0)
        draw.text((21, 102), "按住正面说话，松手发送", font=small, fill=0)

    draw.text((12, 136), f"{now.month:02d} 月", font=small, fill=0)
    draw.line((201, 138, 201, 265), fill=0)
    calendar_font = font().font_variant(size=13)
    for col, label in enumerate("一二三四五六日"):
        draw.text((17 + col * 26, 156), label, font=calendar_font, fill=0)
    for row, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(now.year, now.month)):
        for col, day in enumerate(week):
            if not day:
                continue
            x, y = 12 + col * 26, 176 + row * 15
            today = day == now.day
            if today:
                draw.rounded_rectangle((x + 1, y, x + 23, y + 14), radius=3, fill=0)
            label = str(day)
            draw.text((x + (24 - calendar_font.getlength(label)) / 2, y - 2), label,
                      font=calendar_font, fill=1 if today else 0)
    draw.text((214, 136), "正在处理" + (f" · {len(rows)}" if rows else ""), font=small, fill=0)
    if not rows:
        draw.text((214, 179), "暂无进行中的工作", font=small, fill=0)
        draw.text((214, 203), "下一件事，随时开口", font=small, fill=0)
    for i, text in enumerate(rows[:4]):
        y = 165 + i * 24
        draw.rectangle((215, y + 6, 218, y + 9), fill=0)
        # Four simultaneous tasks stay readable; the focused task gets its
        # full two-line title above, while background rows use an ellipsis.
        draw.text((224, y), _lines(text, small, 163, 1)[0], font=small, fill=0)
    draw.line((10, 273, 390, 273), fill=0)
    hint = "按住说话 · 松手发送    长按下键：选任务"
    draw.text(((400 - small.getlength(hint)) / 2, 278), hint, font=small, fill=0)
    raw = image.tobytes()
    return raw, hashlib.sha256(raw).hexdigest()


def standby_frame(device):
    now = timezone.localtime().replace(second=0, microsecond=0)
    from .services import task_route, Conversation
    _, selected = task_route(device)
    selection = f"当前任务 #{selected}" if selected else "当前：新任务"
    title = (Conversation.objects.filter(pk=selected, user=device.user)
             .values_list("title", flat=True).first()) if selected else ""
    latest = None
    if selected:
        turns = VoiceTurn.objects.filter(device=device, conversation_id=selected)
        latest = (turns.filter(state__in=ACTIVE).order_by("created_at")
                  .values_list("state", flat=True).first())
        if latest is None:
            latest = turns.order_by("-created_at").values_list("state", flat=True).first()
    state = {"queued": "等待识别", "transcribing": "识别中", "transcribed": "准备回答",
             "answering": "AI 回答中", "rendering": "整理回复", "answer_ready": "已完成 · 可继续",
             "failed": "处理未完成", "uncertain": "请查看任务", "canceled": "已取消"}.get(latest or "", "准备就绪")
    return render(now.isoformat(), tasks(device), selection, title or "", state)


def menu_frame(rows, index, selected):
    items = [(0, "＋ 新建任务", "")] + [(r["conversation_id"], r["title"], "处理中" if r["pending_count"] else "已完成") for r in rows]
    if not 0 <= index < len(items):
        raise IndexError()
    image = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(image)
    f = font().font_variant(size=17)
    draw.text((10, 5), "选择任务", font=font(), fill=0)
    draw.line((10, 34, 390, 34), fill=0)
    start = max(0, min(index - 2, len(items) - 6))
    for n, (conversation, title, state) in enumerate(items[start:start + 6], start):
        y = 43 + (n - start) * 34
        focus = n == index
        if focus:
            draw.rectangle((8, y, 392, y + 31), fill=0)
        label = title if not conversation else f"#{conversation} {title[:14]}"
        if conversation and conversation == selected:
            label += " ✓"
        draw.text((13, y + 3), label, font=f, fill=1 if focus else 0)
    draw.text((10, 266), "上下选择 · 正面确认 · 长按下键返回", font=f.font_variant(size=14), fill=0)
    raw = image.tobytes()
    return raw, hashlib.sha256(raw).hexdigest(), len(items), items[index][0]


def home_frame(device):
    from .services import task_route, Conversation
    _, selected = task_route(device)
    title = Conversation.objects.filter(pk=selected, user=device.user).values_list("title", flat=True).first() if selected else "新建任务"
    text = ((f"当前任务 #{selected}\n" if selected else "当前：新任务\n") + (title or "任务")[:36] +
            "\n\n按住说话，松手发送。\n后续语音补充到当前任务。\n长按下键选择其他任务。\n不同任务并行，同一任务顺序处理。")
    from .rendering import frame
    raw, sha, _ = frame(text, "当前任务")
    return raw, sha
