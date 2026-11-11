"""Explicit links from one voice turn to its saved reports, with readable tables."""
import re

from apps.planner.models import DerivedDocument, Message

KEY = "voice_document_ids"


def document_ids(turn):
    if not turn.user_message_id:
        return []
    raw = turn.user_message.payload.get(KEY, [])
    return list(dict.fromkeys(v for v in raw if type(v) is int and v > 0))[:8] if isinstance(raw, list) else []


def documents(turn):
    ids = document_ids(turn)
    found = {d.pk: d for d in DerivedDocument.objects.filter(
        pk__in=ids, created_by_id=turn.device.user_id, conversation_id=turn.conversation_id)}
    return [found[i] for i in ids if i in found]


def attach_result(turn, proposal):
    """Called immediately after a successful compose tool, before final AI text."""
    if not turn or not turn.user_message_id or proposal.get("intent") != "compose_document" or proposal.get("status") != "executed":
        return
    pk = (proposal.get("result") or {}).get("document_id")
    if type(pk) is not int or not DerivedDocument.objects.filter(
            pk=pk, created_by_id=turn.device.user_id, conversation_id=turn.conversation_id).exists():
        return
    message = Message.objects.get(pk=turn.user_message_id, conversation_id=turn.conversation_id, role="user")
    ids = document_ids(turn)
    if pk not in ids and len(ids) < 8:
        message.payload[KEY] = ids + [pk]
        message.save(update_fields=["payload"])
        turn.user_message = message


def readable_markdown(text):
    """Render every table value vertically; never clip a report to a summary."""
    output, headers = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped[1:-1].split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells):
                continue
            if headers is None:
                headers = cells
                continue
            for index, cell in enumerate(cells):
                label = headers[index] if index < len(headers) else f"补充{index + 1}"
                output.append(f"{label}：{cell}")
            output.append("")
        else:
            headers = None
            output.append(re.sub(r"^\s{0,3}#{1,6}\s+", "", line))
    return "\n".join(output).replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")


def report_text(turn):
    if not document_ids(turn):
        return None
    rows = documents(turn)
    if not rows:
        return "报告已删除或暂不可读，请在手机任务中心查看。"
    pages = []
    for doc in rows:
        body = readable_markdown(doc.content_markdown)
        first, _, rest = body.partition("\n")
        if first.strip() == doc.title.strip():
            body = rest.lstrip("\n")
        pages.append(f"文档 #{doc.pk}\n{doc.title}\n\n" + body)
    return "\n\n".join(pages)
