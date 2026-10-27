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


def device_json(d):
    return {"id": str(d.pk), "name": d.name, "device_type": d.device_type,
            "enabled": d.enabled, "conversation_id": d.conversation_id,
            "scope_mode": d.scope_mode, "library_codes": d.library_codes,
            "config_version": d.config_version,
            "online": bool(d.last_seen_at and d.last_seen_at > timezone.now() - timedelta(seconds=100)),
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None}


def turn_json(t):
    return {"turn_id": str(t.client_turn_id), "task_id": str(t.pk), "device_id": str(t.device_id),
            "config_version": t.config_version, "conversation_id": t.conversation_id,
            "state": t.state, "stage": t.stage, "transcript": t.transcript, "detail": t.error,
            "user_message_id": t.user_message_id, "assistant_message_id": t.assistant_message_id,
            "answer_page_count": t.answer_pages, "transcript_page_count": t.transcript_pages,
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
        latest = VoiceTurn.objects.filter(device=request.auth, state="answer_ready").order_by("-created_at").first()
        return Response({**device_json(request.auth), "protocol_version": 1, "max_record_seconds": 60,
                         "latest_turn_id": str(latest.client_turn_id) if latest else None})

    def post(self, request):
        return self.get(request)


class UploadView(DeviceView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        audio = request.FILES.get("file")
        if audio is None:
            raise ValidationError("缺少音频 file")
        raw = audio.read(2_000_045)
        turn, result = services.accept_audio(request.auth, request.data, raw)
        if result in {"conflict", "config_changed", "busy"}:
            return Response({"code": result, "detail": {"conflict": "同一轮次不能更换录音", "config_changed": "设备设置已变化，请重新录音", "busy": "设备正在回答上一条录音"}[result]}, status=409)
        if result == "rate_limited":
            return Response({"detail": "录音提交过于频繁，请稍后重试"}, status=429)
        return Response(turn_json(turn), status=202 if result == "accepted" else 200)


def device_turn(request, turn_id):
    return get_object_or_404(VoiceTurn, device=request.auth, client_turn_id=turn_id)


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
        text = (services.screen_answer(t) if t.state == "answer_ready" else t.answer) if view == "answer" else t.transcript
        if not text:
            return Response({"detail": "内容尚未生成"}, status=409)
        try:
            raw, sha, count = frame(text, "AI 回复" if view == "answer" else "我说", number)
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
        text = (services.screen_answer(t) if t.state == "answer_ready" else t.answer) if view == "answer" else t.transcript
        if not text:
            return Response({"detail": "内容尚未生成"}, status=409)
        title = "AI 回复" if view == "answer" else "我说"
        try:
            first, _, count = frame(text, title, number)
        except IndexError:
            return Response(status=404)
        frames = [first] + [frame(text, title, i)[0] for i in range(number + 1, min(number + 8, count))]
        raw = b"".join(frames)
        response = HttpResponse(raw, content_type="application/octet-stream")
        response["X-Content-SHA256"] = hashlib.sha256(raw).hexdigest()
        response["X-Page-Count"] = str(count)
        response["X-Frame-Count"] = str(len(frames))
        response["X-Frame-Format"] = "400x300-mono-msb-0black"
        return response
