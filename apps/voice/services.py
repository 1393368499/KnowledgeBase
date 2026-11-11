import hashlib
import io
import logging
import secrets
import uuid
import wave
from datetime import timedelta
from pathlib import Path

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.knowledge.models import KnowledgeLibrary
from apps.knowledge.permissions import readable_library_ids
from apps.planner.models import Conversation, Message
from .authentication import credential_hash
from .models import VoiceDevice, VoiceEvent, VoiceTurn, ConversationLease
from .rendering import frame

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "transcribing", "transcribed", "answering", "rendering")
MAX_PARALLEL_TASKS = 4
MAX_PENDING_TURNS = 12


def independent_tasks(device):
    return device.capabilities.get("conversation_mode", "fixed") in {"per_turn", "tasks"}


def task_route(device):
    version = int(device.capabilities.get("route_version", 1))
    routes = device.capabilities.get("task_routes", {str(version): None})
    return version, routes.get(str(version))


def select_task(device, conversation_id):
    with transaction.atomic():
        device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
        if device.capabilities.get("conversation_mode") != "tasks":
            raise ValidationError("请先启用任务会话模式")
        if conversation_id is not None:
            if not VoiceTurn.objects.filter(device=device, conversation_id=conversation_id, conversation__user=device.user).exists():
                raise ValidationError("该任务不属于这台设备")
        version, _ = task_route(device)
        routes = dict(device.capabilities.get("task_routes", {}))
        version += 1
        routes[str(version)] = conversation_id
        # Keep recent capture snapshots so changing selection during recording
        # never redirects an already-started recording to a different task.
        routes = dict(sorted(routes.items(), key=lambda item: int(item[0]))[-64:])
        device.capabilities = {**device.capabilities, "route_version": version, "task_routes": routes}
        device.save(update_fields=["capabilities"])
        return {"route_version": version, "active_conversation_id": conversation_id}


def emit(device, kind, turn=None, **payload):
    return VoiceEvent.objects.create(device=device, turn=turn, kind=kind, payload=payload)


def scope(user, mode, codes):
    if mode not in {"all_authorized", "selected"}:
        raise ValidationError("知识库范围模式无效")
    if mode == "all_authorized":
        return []
    if not isinstance(codes, list) or not 1 <= len(codes) <= 20 or any(not isinstance(c, str) for c in codes):
        raise ValidationError("请选择 1–20 个信息库")
    codes = list(dict.fromkeys(codes))
    qs = KnowledgeLibrary.objects.filter(code__in=codes, is_active=True)
    ids = readable_library_ids(user)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    if set(qs.values_list("code", flat=True)) != set(codes):
        raise ValidationError("所选信息库不存在或已无权访问，请重新选择")
    return codes


def configure(device, data):
    name = str(data.get("name", device.name)).strip()
    if not name or len(name) > 80:
        raise ValidationError("设备名称需为 1–80 个字")
    scope_mode = data.get("scope_mode", device.scope_mode)
    codes = scope(device.user, scope_mode, data.get("library_codes", device.library_codes))
    mode = data.get("conversation_mode", device.capabilities.get("conversation_mode", "fixed"))
    if mode not in {"fixed", "per_turn", "tasks"}:
        raise ValidationError("任务会话模式无效")
    device.capabilities = {**device.capabilities, "conversation_mode": mode}
    if mode == "tasks" and "route_version" not in device.capabilities:
        device.capabilities.update(route_version=1, task_routes={"1": None})
    conv_id = data.get("conversation_id", device.conversation_id)
    if conv_id:
        try:
            conversation = Conversation.objects.get(pk=conv_id, user=device.user)
        except (ValueError, TypeError, Conversation.DoesNotExist):
            raise ValidationError("会话不存在或不属于你")
    elif mode == "fixed":
        conversation = Conversation.objects.create(user=device.user, title=f"{name} 语音")
    else:
        conversation = None
    device.name = name
    device.conversation = conversation
    device.scope_mode = scope_mode
    device.library_codes = codes
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ValidationError("enabled 必须是布尔值")
        device.enabled = data["enabled"]
    device.config_version += 1
    device.save()
    return device


def add_device(user, data):
    data = {"conversation_mode": "tasks", **data}
    raw = secrets.token_urlsafe(32)
    with transaction.atomic():
        device = VoiceDevice(user=user, name="新设备", token_hash=credential_hash(raw))
        kind = data.get("device_type", "zectrix-note4")
        if not isinstance(kind, str) or not 1 <= len(kind) <= 60:
            raise ValidationError("设备类型无效")
        device.device_type = kind
        configure(device, data)
        emit(device, "device.added")
    return device, raw


def audio_root():
    return Path(settings.VOICE_AUDIO_ROOT)


def validate_wav(raw):
    if len(raw) > 2_000_044 or len(raw) < 44:
        raise ValidationError("音频文件过大或为空")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError()
            count = wav.getnframes()
            pcm = wav.readframes(count)
            if not 3200 <= count <= 960000 or len(pcm) != count * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError):
        raise ValidationError("需要 0.2–60 秒的 16kHz 单声道 PCM16 WAV 音频")
    return hashlib.sha256(raw).hexdigest()


def accept_audio(device, data, raw):
    digest = validate_wav(raw)
    try:
        client_id = uuid.UUID(str(data.get("turn_id", "")))
        version = int(data.get("config_version", ""))
    except (ValueError, TypeError):
        raise ValidationError("turn_id 或 config_version 无效")
    if not secrets.compare_digest(digest, str(data.get("audio_sha256", ""))):
        raise ValidationError("音频校验失败")
    with transaction.atomic():
        device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
        previous = VoiceTurn.objects.filter(device=device, client_turn_id=client_id).first()
        if previous:
            if previous.state == "removed":
                return None, "removed"
            if previous.state == "canceled":
                return previous, "existing"
            if previous.audio_sha256 != digest:
                return previous, "conflict"
            return previous, "existing"
        if not device.enabled or device.removed_at or not device.user.is_active:
            raise ValidationError("设备已停用")
        if version != device.config_version:
            return None, "config_changed"
        parallel = independent_tasks(device)
        grouped = device.capabilities.get("conversation_mode") == "tasks"
        if not parallel and not device.conversation_id:
            raise ValidationError("请在设备管理中选择目标会话")
        active_turns = VoiceTurn.objects.filter(device=device, state__in=ACTIVE)
        route_version, selected_conversation = None, None
        if grouped:
            try:
                route_version = str(int(data.get("route_version", "")))
            except (ValueError, TypeError):
                raise ValidationError("缺少录音开始时的任务选择，请连接 App 后重录")
            routes = dict(device.capabilities.get("task_routes", {}))
            if route_version not in routes:
                raise ValidationError("录音的任务选择已过期，请重新选择任务后重录")
            selected_conversation = routes[route_version]
            if selected_conversation is not None and not Conversation.objects.filter(pk=selected_conversation, user=device.user).exists():
                raise ValidationError("录音所属任务已不存在，请重新选择任务")
            active_tasks = active_turns.values("conversation_id").distinct().count()
            joining_active = selected_conversation is not None and active_turns.filter(conversation_id=selected_conversation).exists()
            if active_turns.count() >= MAX_PENDING_TURNS or (not joining_active and active_tasks >= MAX_PARALLEL_TASKS):
                return None, "capacity"
        elif active_turns.count() >= (MAX_PARALLEL_TASKS if parallel else 1):
            return None, "capacity" if parallel else "busy"
        if VoiceTurn.objects.filter(device=device, created_at__gte=timezone.now() - timedelta(minutes=1)).exclude(state="canceled").count() >= 10:
            return None, "rate_limited"
        codes = scope(device.user, device.scope_mode, device.library_codes)
        turn_id = uuid.uuid4()
        conversation = device.conversation
        if grouped and selected_conversation is not None:
            conversation = Conversation.objects.get(pk=selected_conversation, user=device.user)
        elif parallel:
            conversation = Conversation.objects.create(user=device.user, title="语音任务 " + str(client_id)[:8])
            if grouped:
                routes[route_version] = conversation.pk
                device.capabilities = {**device.capabilities, "task_routes": routes}
                device.save(update_fields=["capabilities"])
        if parallel:
            device.conversation = conversation
            device.save(update_fields=["conversation"])
        target = audio_root() / str(device.pk) / f"{turn_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as out:
                out.write(raw)
            turn = VoiceTurn.objects.create(
                id=turn_id, device=device, client_turn_id=client_id,
                conversation=conversation, config_version=version,
                scope_mode=device.scope_mode, library_codes=codes,
                audio_sha256=digest, audio_path=str(target),
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return turn, "accepted"


def cancel_turn(device, client_id):
    """Serialize cancellation with upload acceptance and the AI-start fence."""
    with transaction.atomic():
        device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
        turn = VoiceTurn.objects.select_for_update().filter(device=device, client_turn_id=client_id).first()
        if turn and turn.state == "canceled":
            return turn, True
        if turn and (turn.state not in {"queued", "transcribing", "transcribed", "failed"}
                     or turn.user_message_id or turn.assistant_message_id
                     or (turn.state == "failed" and turn.stage != "asr")):
            return turn, False
        if turn is None:
            # Persist the canceled UUID before a slow, already-in-flight upload
            # can arrive. That upload must never recreate a live job.
            if VoiceTurn.objects.filter(device=device, created_at__gte=timezone.now() - timedelta(minutes=1)).count() >= 60:
                raise ValidationError("取消请求过于频繁，请稍后重试")
            turn = VoiceTurn.objects.create(device=device, client_turn_id=client_id,
                conversation=None if device.capabilities.get("conversation_mode") == "tasks" else device.conversation, config_version=device.config_version,
                scope_mode=device.scope_mode, library_codes=device.library_codes,
                audio_sha256="", audio_path="", state="canceled", error="已取消发送")
        else:
            turn.state = "canceled"
            turn.error = "已取消发送"
            turn.save(update_fields=["state", "error", "updated_at"])
        emit(device, "turn.canceled", turn)
        return turn, True


def transcribe(turn):
    with open(turn.audio_path, "rb") as audio:
        response = httpx.post(settings.VOICE_ASR_URL, files={"file": ("audio.wav", audio, "audio/wav")}, timeout=120)
    response.raise_for_status()
    result = response.json()
    text = str(result.get("text", "")).strip()
    if not text or not result.get("speech_detected", False):
        raise ValueError("未听清有效语音，请重说")
    return text


def process_turn(turn_id):
    # Independent conversations run concurrently. Within one task, only the
    # earliest recording can claim work; later recordings retain their order.
    candidate = VoiceTurn.objects.filter(pk=turn_id, state="queued").first()
    if not candidate:
        return
    with transaction.atomic():
        VoiceDevice.objects.select_for_update().get(pk=candidate.device_id)
        if candidate.conversation_id:
            Conversation.objects.select_for_update().get(pk=candidate.conversation_id)
            from django.db.models import Q
            previous = VoiceTurn.objects.filter(conversation_id=candidate.conversation_id, state__in=ACTIVE).exclude(pk=candidate.pk)
            if previous.filter(Q(state__in=["transcribing", "transcribed", "answering", "rendering"]) |
                               Q(created_at__lt=candidate.created_at) |
                               Q(created_at=candidate.created_at, pk__lt=candidate.pk)).exists():
                return
            if ConversationLease.objects.filter(conversation_id=candidate.conversation_id).exists():
                return
        claimed = VoiceTurn.objects.filter(pk=turn_id, state="queued").update(state="transcribing", claimed_at=timezone.now(), updated_at=timezone.now())
    if not claimed:
        return
    turn = VoiceTurn.objects.select_related("device__user", "conversation").get(pk=turn_id)
    try:
        device = turn.device
        if not device.enabled or device.removed_at or not device.user.is_active or not turn.conversation_id:
            raise ValueError("设备不可用或会话已删除")
        scope(device.user, turn.scope_mode, turn.library_codes)
        if not turn.transcript:
            turn.transcript = transcribe(turn)
        _, _, turn.transcript_pages = frame(turn.transcript, "我说")
        with transaction.atomic():
            locked_device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
            locked_turn = VoiceTurn.objects.select_for_update().get(pk=turn.pk)
            if locked_turn.state == "canceled":
                return
            if not locked_device.enabled or locked_device.removed_at or locked_device.config_version != turn.config_version:
                raise ValueError("设备已停用或发送设置已变化")
            turn.state = "transcribed"
            turn.save()
            if independent_tasks(locked_device):
                Conversation.objects.filter(pk=turn.conversation_id, title="语音任务 " + str(turn.client_turn_id)[:8]).update(title=turn.transcript[:60])
            emit(device, "transcript.final", turn, transcript=turn.transcript)
        answer_turn(turn)
    except Exception as exc:
        # F-05（2026-11-11 复审）：对设备只回稳定、可读的失败原因；异常原文
        # （可能含上游端点/响应体/本地路径）只写日志，不进入 turn.error。
        logger.warning(
            "Voice turn %s failed at %s (%s): %s",
            turn.pk,
            turn.stage,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with transaction.atomic():
            VoiceDevice.objects.select_for_update().get(pk=turn.device_id)
            current = VoiceTurn.objects.select_for_update().get(pk=turn.pk)
            if current.state == "canceled":
                return
            turn.state = "uncertain" if turn.stage == "answer" else "failed"
            turn.error = _public_voice_error(turn.stage, exc)
            turn.save(update_fields=["state", "error", "updated_at"])
            emit(turn.device, "turn.failed", turn, detail=turn.error, state=turn.state)
            record_failure_notice(turn)


def _public_voice_error(stage: str, exc: Exception) -> str:
    """设备可见的失败原因：只给稳定类别 + 可执行下一步，不暴露异常原文。"""
    text = str(exc).lower()
    if stage == "answer":
        if "timeout" in text or "timed out" in text or "超时" in text:
            return "AI 模型服务超时，本轮未生成完整回复"
        if "429" in text or "限流" in text or "busy" in text:
            return "AI 模型服务繁忙，本轮未生成完整回复"
        return "AI 处理未完成，本轮没有生成完整回复"
    if stage == "render":
        return "回复已处理，但屏幕显示生成失败"
    return "语音识别未完成，请重新录音"


def failure_detail(turn):
    if turn.stage == "answer":
        reason = "AI 模型服务超时" if any(s in turn.error.lower() for s in ["timeout", "timed out", "超时"]) else "AI 处理未完成"
        return ("系统提示：" + reason + "，本轮没有生成完整回复。\n"
                "语音内容已记录；已完成的检索或操作不会撤销。请到网页核对操作记录后再提问，系统不会自动重复提交。")
    if turn.stage == "render":
        return "系统提示：回复已处理，但屏幕显示生成失败。请到网页查看会话，或重试显示。"
    return "系统提示：语音识别未完成，请重新录音。"


def record_failure_notice(turn):
    """Durable system feedback, never a fabricated model answer or auto replay."""
    with transaction.atomic():
        VoiceDevice.objects.select_for_update().get(pk=turn.device_id)
        current = VoiceTurn.objects.select_for_update().get(pk=turn.pk)
        if current.state not in {"failed", "uncertain"} or not current.conversation_id:
            return
        existing = Message.objects.filter(conversation_id=current.conversation_id, role=Message.Role.SYSTEM,
                                          payload__voice_turn_id=str(current.pk), payload__unavailable=True)
        if existing.exists():
            return
        Message.objects.create(conversation_id=current.conversation_id, role=Message.Role.SYSTEM,
            content=failure_detail(current), payload={"unavailable": True, "voice_turn_id": str(current.pk),
                "device_id": str(current.device_id), "state": current.state, "stage": current.stage})


def answer_turn(turn):
    from apps.planner.services import reply_to_message
    from .locking import ConversationBusy
    with transaction.atomic():
        VoiceDevice.objects.select_for_update().get(pk=turn.device_id)
        current = VoiceTurn.objects.select_for_update().get(pk=turn.pk)
        if current.state == "canceled":
            return
        turn.stage = "answer"
        turn.state = "answering"
        turn.save(update_fields=["stage", "state", "updated_at"])
    try:
        payload = reply_to_message(
            turn.device.user, turn.conversation, turn.transcript,
            library_codes=turn.library_codes if turn.scope_mode == "selected" else None,
            strict_scope=turn.scope_mode == "selected", voice_turn=turn,
        )
    except ConversationBusy:
        # No user message was created. Retry the same turn when the lease clears.
        turn.state = "queued"
        turn.save(update_fields=["state", "updated_at"])
        return
    except Exception:
        # The document tool may already have saved its result. Do not replay
        # that write merely because the final model acknowledgement timed out.
        from .documents import documents
        turn.refresh_from_db()
        saved = documents(turn)
        if not saved:
            raise
        Message.objects.create(conversation=turn.conversation, role=Message.Role.SYSTEM,
            content="报告已保存，可在开发板上下翻页阅读正文。AI 回复摘要未完成，已保存的报告不会重复生成。",
            payload={"voice_turn_id": str(turn.pk), "document_ids": [d.pk for d in saved],
                     "document_saved_reply_unavailable": True})
        turn.stage = "render"
        turn.state = "rendering"
        render_turn(turn)
        return
    turn.answer = payload["content"]
    turn.assistant_message_id = payload["assistant_message_id"]
    turn.user_message_id = payload["user_message_id"]
    turn.stage = "render"
    turn.state = "rendering"
    turn.save()
    render_turn(turn)


def render_turn(turn):
    turn.answer = screen_answer(turn)
    _, _, turn.transcript_pages = frame(turn.transcript, "我说")
    _, _, turn.answer_pages = frame(turn.answer, answer_title(turn))
    turn.state = "answer_ready"
    turn.error = ""
    with transaction.atomic():
        turn.save()
        emit(turn.device, "answer.ready", turn)


def screen_answer(turn):
    from .documents import report_text
    report = report_text(turn)
    if report is not None:
        return report
    if turn.answer.strip():
        return turn.answer
    if turn.state in {"failed", "uncertain"}:
        return failure_detail(turn)
    payload = turn.assistant_message.payload if turn.assistant_message_id else {}
    reasons = [str(p.get("reason", "")) for p in payload.get("rejected_proposals", []) if p.get("reason")]
    if reasons:
        return "本次请求未能执行：\n" + "\n".join(reasons)
    return "AI 本轮没有返回文字。请在网页中查看会话及待确认操作，或换一种说法再试。"


def answer_title(turn):
    from .documents import document_ids
    if document_ids(turn):
        return "报告正文"
    return "系统提示" if turn.state in {"failed", "uncertain"} and not turn.assistant_message_id else "AI 回复"


def answer_revision(turn):
    return hashlib.sha256((answer_title(turn) + "\n" + screen_answer(turn)).encode("utf-8")).hexdigest()


def sweep(*, process_queued=True):
    now = timezone.now()
    # Crashed model requests are not replayed. They can have persisted tool work.
    stale = VoiceTurn.objects.filter(state__in=["transcribing", "transcribed", "answering", "rendering"], claimed_at__lt=now - timedelta(minutes=15))
    for candidate in stale:
        with transaction.atomic():
            VoiceDevice.objects.select_for_update().get(pk=candidate.device_id)
            turn = VoiceTurn.objects.select_for_update().get(pk=candidate.pk)
            if turn.state not in {"transcribing", "transcribed", "answering", "rendering"} or not turn.claimed_at or turn.claimed_at >= now - timedelta(minutes=15):
                continue
            if turn.assistant_message_id and turn.stage == "render":
                try:
                    render_turn(turn)
                except Exception:
                    turn.state = "failed"
                    turn.error = "回复已保存，但屏幕排版失败，请重试显示"
                    turn.save(update_fields=["state", "error", "updated_at"])
                continue
            if turn.state in {"transcribing", "transcribed"} and not turn.user_message_id:
                turn.state = "queued"
                turn.save(update_fields=["state", "updated_at"])
                continue
            turn.state = "uncertain"
            turn.error = "处理进程中断，请核对会话记录后重新提问"
            turn.save(update_fields=["state", "error", "updated_at"])
            emit(turn.device, "turn.failed", turn, detail=turn.error, state=turn.state)
            record_failure_notice(turn)
    if process_queued:
        for turn in VoiceTurn.objects.filter(state="queued").order_by("created_at")[:20]:
            process_turn(turn.pk)
    # No raw audio is part of the immutable knowledge store.
    for turn in VoiceTurn.objects.exclude(state__in=ACTIVE).filter(created_at__lt=now - timedelta(hours=1)).exclude(audio_path="")[:100]:
        path = Path(turn.audio_path)
        if path.resolve().is_relative_to(audio_root().resolve()):
            path.unlink(missing_ok=True)
        turn.audio_path = ""
        turn.save(update_fields=["audio_path"])
