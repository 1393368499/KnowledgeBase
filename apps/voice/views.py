import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import DeviceAuthentication, credential_hash
from .models import VoiceDevice, VoiceEvent, VoiceTurn
from . import services
from .rendering import frame


class EndpointView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        import json
        import re
        from pathlib import Path
        from django.conf import settings
        from knowledgebase.paths import PROJECT_ROOT
        path = Path(getattr(settings, "KB_VOICE_ENDPOINT_FILE", PROJECT_ROOT / "data/media/voice-connection/current.json"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("server"), str) or not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", data.get("server", "")):
                raise ValueError()
            response = Response({"server": data["server"], "updated_at": data.get("updated_at", "")})
        except (OSError, ValueError, TypeError):
            response = Response({"server": "", "detail": "本次启动的远程地址尚未就绪，请稍后刷新"})
        response["Cache-Control"] = "no-store"
        return response


def device_json(d):
    route, selected = services.task_route(d)
    return {"id": str(d.pk), "name": d.name, "device_type": d.device_type,
            "enabled": d.enabled, "conversation_id": d.conversation_id,
            "scope_mode": d.scope_mode, "library_codes": d.library_codes,
            "config_version": d.config_version,
            "conversation_mode": d.capabilities.get("conversation_mode", "fixed"),
            "max_parallel_tasks": services.MAX_PARALLEL_TASKS if services.independent_tasks(d) else 1,
            "route_version": route, "active_conversation_id": selected,
            "online": bool(d.last_seen_at and d.last_seen_at > timezone.now() - timedelta(seconds=100)),
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None}


def turn_json(t):
    answer_pages = t.answer_pages
    from .documents import document_ids
    if t.state in {"failed", "uncertain"} or document_ids(t):
        from .rendering import page_lines
        answer_pages = len(page_lines(services.screen_answer(t)))
    return {"turn_id": str(t.client_turn_id), "task_id": str(t.pk), "device_id": str(t.device_id),
            "config_version": t.config_version, "conversation_id": t.conversation_id,
            "state": t.state, "stage": t.stage, "transcript": t.transcript, "detail": t.error,
            "user_message_id": t.user_message_id, "assistant_message_id": t.assistant_message_id,
            "answer_page_count": answer_pages, "transcript_page_count": t.transcript_pages,
            "answer_text": services.screen_answer(t)[:16000] if t.state in {"answer_ready", "failed", "uncertain"} else "",
            "content_version": 1}


class DevicesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"results": [device_json(d) for d in VoiceDevice.objects.filter(user=request.user, removed_at__isnull=True)]})

    def post(self, request):
        d, raw = services.add_device(request.user, request.data)
        response = Response({**device_json(d), "device_token": raw}, status=201)
        response["Cache-Control"] = "no-store"
        return response


class DeviceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, device_id):
        with transaction.atomic():
            d = get_object_or_404(VoiceDevice.objects.select_for_update(), pk=device_id, user=request.user, removed_at__isnull=True)
            active = VoiceTurn.objects.filter(device=d, state__in=services.ACTIVE).exists()
            # Disabling is always possible, even while a turn is running.
            if active and any(k != "enabled" for k in request.data):
                return Response({"detail": "设备有正在处理的录音，完成后再修改设置"}, status=409)
            services.configure(d, request.data)
            services.emit(d, "device.config_changed")
        return Response(device_json(d))

    def delete(self, request, device_id):
        with transaction.atomic():
            d = get_object_or_404(VoiceDevice.objects.select_for_update(), pk=device_id, user=request.user, removed_at__isnull=True)
            d.enabled = False
            d.removed_at = timezone.now()
            d.token_hash = credential_hash(secrets.token_urlsafe(32))
            d.save(update_fields=["enabled", "removed_at", "token_hash"])
            services.emit(d, "device.removed")
        return Response(status=204)


class RotateTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, device_id):
        with transaction.atomic():
            d = get_object_or_404(VoiceDevice.objects.select_for_update(), pk=device_id, user=request.user, removed_at__isnull=True)
            raw = secrets.token_urlsafe(32)
            d.token_hash = credential_hash(raw)
            d.save(update_fields=["token_hash"])
        response = Response({"device_token": raw})
        response["Cache-Control"] = "no-store"
        return response


class EventsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            cursor = max(0, int(request.query_params.get("after", 0)))
        except ValueError:
            raise ValidationError("事件游标无效")
        qs = VoiceEvent.objects.filter(device__user=request.user)
        if request.query_params.get("latest") == "1":
            newest = qs.order_by("-id").values_list("id", flat=True).first() or 0
            return Response({"results": [], "next_cursor": newest})
        rows = list(qs.filter(id__gt=cursor).select_related("device", "turn").order_by("id")[:100])
        return Response({"results": [{"id": e.pk, "kind": e.kind, "device_id": str(e.device_id),
                           "device_name": e.device.name, "turn_id": str(e.turn_id) if e.turn_id else None,
                           "conversation_id": e.turn.conversation_id if e.turn_id else None, **e.payload}
                          for e in rows], "next_cursor": rows[-1].pk if rows else cursor})


class DeviceView(APIView):
    authentication_classes = [DeviceAuthentication]
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class ConfigView(DeviceView):
    def get(self, request):
        data = device_json(request.auth)
        query = VoiceTurn.objects.filter(device=request.auth, state="answer_ready")
        if request.auth.capabilities.get("conversation_mode") == "tasks":
            selected = data.get("active_conversation_id")
            query = query.filter(conversation_id=selected) if selected else query.none()
        latest = query.order_by("-created_at").first()
        return Response({**data, "protocol_version": 1, "max_record_seconds": 60,
                         "active_turn_ids": list(VoiceTurn.objects.filter(device=request.auth, state__in=services.ACTIVE).order_by("created_at").values_list("client_turn_id", flat=True)[:services.MAX_PARALLEL_TASKS]),
                         "latest_turn_id": str(latest.client_turn_id) if latest else None,
                         "latest_conversation_id": latest.conversation_id if latest else None,
                         "latest_answer_revision": services.answer_revision(latest) if latest else ""})

    def post(self, request):
        return self.get(request)


class StandbyView(DeviceView):
    def get(self, request):
        from .standby import standby_frame
        raw, checksum = standby_frame(request.auth)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = checksum
        response["X-Frame-Format"] = "400x300-mono-msb-0black"
        return response


def task_rows(device):
    from django.db.models import Case, When, Value, IntegerField
    query = VoiceTurn.objects.filter(device=device, conversation__user=device.user).exclude(state="removed").select_related("conversation").annotate(
        active_first=Case(When(state__in=services.ACTIVE, then=Value(0)), default=Value(1), output_field=IntegerField()))
    grouped = {}
    for t in query.order_by("active_first", "-created_at")[:200]:
        grouped.setdefault(t.conversation_id, []).append(t)
    rows = []
    for conversation_id, turns in list(grouped.items())[:20]:
        active = sorted((t for t in turns if t.state in services.ACTIVE), key=lambda t: (t.created_at, str(t.pk)))
        latest = max(turns, key=lambda t: t.created_at)
        shown = active[0] if active else latest
        completed = next((t for t in sorted(turns, key=lambda t: t.created_at, reverse=True) if t.state in {"answer_ready", "failed", "uncertain"}), None)
        rows.append({"turn_id": str(shown.client_turn_id), "conversation_id": conversation_id,
            "title": latest.conversation.title or "语音任务", "state": shown.state, "stage": shown.stage,
            "updated_at": latest.conversation.updated_at.isoformat(),
            "pending_count": len(active), "recording_count": VoiceTurn.objects.filter(device=device, conversation_id=conversation_id).count(),
            "transcript": shown.transcript[:160], "answer_summary": services.screen_answer(completed)[:100] if completed else "",
            "can_cancel": shown.state in {"queued", "transcribing", "transcribed"} and not shown.user_message_id,
            "created_at": latest.created_at.isoformat()})
    return rows


class TasksView(DeviceView):
    def get(self, request):
        device = request.auth
        version, selected = services.task_route(device)
        active = VoiceTurn.objects.filter(device=device, state__in=services.ACTIVE)
        title = "新任务（下一条语音开始）"
        if selected:
            title = services.Conversation.objects.filter(pk=selected, user=device.user).values_list("title", flat=True).first() or "当前任务"
        return Response({"device_id": str(device.pk), "server_time": timezone.now().isoformat(),
            "max_parallel_tasks": services.MAX_PARALLEL_TASKS, "route_version": version,
            "active_conversation_id": selected, "active_task_title": title,
            "active_count": active.values("conversation_id").distinct().count(),
            "pending_recordings": active.count(), "results": task_rows(device)})


class TaskSelectionView(DeviceView):
    def post(self, request):
        value = request.data.get("conversation_id")
        try:
            value = int(value) if value is not None else None
        except (ValueError, TypeError):
            raise ValidationError("任务编号无效")
        return Response(services.select_task(request.auth, value))


class TaskMenuView(DeviceView):
    def get(self, request, number):
        from .standby import menu_frame
        _, selected = services.task_route(request.auth)
        try:
            raw, checksum, count, conversation = menu_frame(task_rows(request.auth), number, selected)
        except IndexError:
            return Response(status=404)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = checksum
        response["X-Menu-Count"] = str(count)
        response["X-Menu-Conversation"] = str(conversation)
        return response


class TaskHomeView(DeviceView):
    def get(self, request):
        from .standby import home_frame
        raw, checksum = home_frame(request.auth)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = checksum
        return response


class TaskDetailView(DeviceView):
    def get(self, request, conversation_id):
        turns = list(VoiceTurn.objects.filter(device=request.auth, conversation_id=conversation_id,
            conversation__user=request.user).order_by("-created_at")[:6])
        if not turns:
            return Response(status=404)
        data = turn_json(turns[0])
        data["transcript"] = data["transcript"][:1500]
        history = []
        for t in reversed(turns):
            row = turn_json(t)
            row["transcript"] = row["transcript"][:1000]
            row["answer_text"] = row["answer_text"][:1000]
            history.append(row)
        data["turns"] = history
        data["answer_text"] = data["answer_text"][:3000]
        # The web conversation is authoritative, including replies/edits made
        # after a voice turn completes. Bound payloads fit the BLE/phone budget.
        from apps.planner.models import Message, DerivedDocument
        messages = Message.objects.filter(conversation_id=conversation_id, conversation__user=request.user).order_by("-id")[:6]
        data["messages"] = [{"id": m.pk, "role": m.role, "content": m.content[:2000],
                             "truncated": len(m.content) > 2000, "created_at": m.created_at.isoformat()}
                            for m in reversed(list(messages))]
        data["documents"] = list(DerivedDocument.objects.filter(conversation_id=conversation_id,
            created_by=request.user).order_by("-updated_at").values("id", "title", "updated_at")[:4])
        data["server_time"] = timezone.now().isoformat()
        return Response(data)


class TaskRenameView(DeviceView):
    def post(self, request, conversation_id):
        from .task_actions import rename
        return Response(rename(request.auth, conversation_id, request.data.get("title")))


class TaskDeleteView(DeviceView):
    def post(self, request, conversation_id):
        from .task_actions import delete
        from apps.planner.services import PlannerError
        try:
            return Response(delete(request.auth, conversation_id, request.data))
        except PlannerError as exc:
            raise ValidationError(str(exc))


class UploadView(DeviceView):
    parser_classes = [MultiPartParser, FormParser]

    #: 服务端支持的设备协议版本。固件在录音上传里带 ``protocol``（当前 1）；
    #: 显式给出不支持的版本时**明确拒绝**，而不是静默按 1 处理（F-07，2026-11-11 复审）。
    SUPPORTED_PROTOCOLS = frozenset({1})

    def post(self, request):
        raw_protocol = request.data.get("protocol")
        if raw_protocol not in (None, ""):
            try:
                protocol = int(raw_protocol)
            except (TypeError, ValueError):
                raise ValidationError({"protocol": ["协议版本必须是整数"]})
            if protocol not in self.SUPPORTED_PROTOCOLS:
                return Response(
                    {
                        "code": "protocol_unsupported",
                        "detail": f"设备协议版本 {protocol} 不受支持，请升级固件（服务端支持：1）",
                        "supported_protocols": sorted(self.SUPPORTED_PROTOCOLS),
                    },
                    status=426,
                )
        audio = request.FILES.get("file")
        if audio is None:
            raise ValidationError("缺少音频 file")
        raw = audio.read(2_000_045)
        turn, result = services.accept_audio(request.auth, request.data, raw)
        if result in {"conflict", "config_changed", "busy"}:
            return Response({"code": result, "detail": {"conflict": "同一轮次不能更换录音", "config_changed": "设备设置已变化，请重新录音", "busy": "设备正在回答上一条录音"}[result]}, status=409)
        if result == "rate_limited":
            return Response({"detail": "录音提交过于频繁，请稍后重试"}, status=429)
        if result == "capacity":
            return Response({"code": "capacity", "detail": "已有四个任务正在处理，请稍候继续发送"}, status=429)
        if result == "removed":
            return Response({"code": "removed", "detail": "该录音所属任务已删除"}, status=410)
        return Response(turn_json(turn), status=202 if result == "accepted" else 200)


def device_turn(request, turn_id):
    return get_object_or_404(VoiceTurn.objects.exclude(state="removed"), device=request.auth, client_turn_id=turn_id)


class TurnView(DeviceView):
    def get(self, request, turn_id):
        return Response(turn_json(device_turn(request, turn_id)))


class CancelTurnView(DeviceView):
    def post(self, request, turn_id):
        turn, canceled = services.cancel_turn(request.auth, turn_id)
        payload = turn_json(turn)
        if not canceled:
            payload["detail"] = "已进入 AI 处理，无法取消这条发送"
        return Response(payload, status=200 if canceled else 409)


class RetryView(DeviceView):
    def post(self, request, turn_id):
        with transaction.atomic():
            t = get_object_or_404(VoiceTurn.objects.select_for_update(), device=request.auth, client_turn_id=turn_id)
            if VoiceTurn.objects.filter(device=request.auth, state__in=services.ACTIVE).exclude(pk=t.pk).exists():
                return Response({"detail": "设备有其他任务正在处理"}, status=409)
            if t.state != "failed" or t.config_version != request.auth.config_version:
                return Response({"detail": "这条任务不能自动重试，请核对会话记录"}, status=409)
            if t.stage == "render" and t.assistant_message_id:
                services.render_turn(t)
            elif t.stage == "asr" and t.audio_path:
                t.state = "queued"
                t.error = ""
                t.save()
            else:
                return Response({"detail": "无法安全恢复，请重新录音"}, status=409)
        return Response(turn_json(t))


class PageView(DeviceView):
    def get(self, request, turn_id, view, number):
        t = device_turn(request, turn_id)
        if view not in {"answer", "transcript"}:
            return Response(status=404)
        text = (services.screen_answer(t) if t.state in {"answer_ready", "failed", "uncertain"} else t.answer) if view == "answer" else t.transcript
        if not text:
            return Response({"detail": "内容尚未生成"}, status=409)
        try:
            title = services.answer_title(t) if view == "answer" else "我说"
            raw, sha, count = frame(text, title, number)
        except IndexError:
            return Response(status=404)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = sha
        response["X-Page-Count"] = str(count)
        response["X-Frame-Format"] = "400x300-mono-msb-0black"
        return response


class PageBatchView(DeviceView):
    """A bounded batch lets the terminal page locally without round trips."""

    def get(self, request, turn_id, view, number):
        t = device_turn(request, turn_id)
        if view not in {"answer", "transcript"}:
            return Response(status=404)
        text = (services.screen_answer(t) if t.state in {"answer_ready", "failed", "uncertain"} else t.answer) if view == "answer" else t.transcript
        if not text:
            return Response({"detail": "内容尚未生成"}, status=409)
        title = services.answer_title(t) if view == "answer" else "我说"
        try:
            first, _, count = frame(text, title, number)
        except IndexError:
            return Response(status=404)
        from .documents import document_ids
        # Show a report immediately; fetch subsequent pages in background batches.
        limit = 1 if view == "answer" and number == 0 and document_ids(t) else 8
        frames = [first] + [frame(text, title, i)[0] for i in range(number + 1, min(number + limit, count))]
        raw = b"".join(frames)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = hashlib.sha256(raw).hexdigest()
        response["X-Page-Count"] = str(count)
        response["X-Frame-Count"] = str(len(frames))
        response["X-Frame-Format"] = "400x300-mono-msb-0black"
        return response
