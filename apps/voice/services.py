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
from .models import VoiceDevice, VoiceEvent, VoiceTurn
from .rendering import frame

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "transcribing", "transcribed", "answering", "rendering")


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
    mode = data.get("scope_mode", device.scope_mode)
    codes = scope(device.user, mode, data.get("library_codes", device.library_codes))
    conv_id = data.get("conversation_id", device.conversation_id)
    if conv_id:
        try:
            conversation = Conversation.objects.get(pk=conv_id, user=device.user)
        except (ValueError, TypeError, Conversation.DoesNotExist):
            raise ValidationError("会话不存在或不属于你")
    else:
        conversation = Conversation.objects.create(user=device.user, title=f"{name} 语音")
    device.name = name
    device.conversation = conversation
    device.scope_mode = mode
    device.library_codes = codes
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ValidationError("enabled 必须是布尔值")
        device.enabled = data["enabled"]
    device.config_version += 1
    device.save()
    return device


def add_device(user, data):
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
            if previous.state == "canceled":
                return previous, "existing"
            if previous.audio_sha256 != digest:
                return previous, "conflict"
            return previous, "existing"
        if not device.enabled or device.removed_at or not device.user.is_active:
            raise ValidationError("设备已停用")
        if version != device.config_version:
            return None, "config_changed"
        if not device.conversation_id:
            raise ValidationError("请在设备管理中选择目标会话")
        if VoiceTurn.objects.filter(device=device, state__in=ACTIVE).exists():
            return None, "busy"
        if VoiceTurn.objects.filter(device=device, created_at__gte=timezone.now() - timedelta(minutes=1)).exclude(state="canceled").count() >= 10:
            return None, "rate_limited"
        codes = scope(device.user, device.scope_mode, device.library_codes)
        turn_id = uuid.uuid4()
        target = audio_root() / str(device.pk) / f"{turn_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as out:
                out.write(raw)
            turn = VoiceTurn.objects.create(
                id=turn_id, device=device, client_turn_id=client_id,
                conversation=device.conversation, config_version=version,
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
                conversation=device.conversation, config_version=device.config_version,
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
    # CAS claim: only one worker can process a durable queued row.
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
            emit(device, "transcript.final", turn, transcript=turn.transcript)
        answer_turn(turn)
    except Exception as exc:
        logger.warning("Voice turn %s failed at %s (%s)", turn.pk, turn.stage, type(exc).__name__)
        with transaction.atomic():
            VoiceDevice.objects.select_for_update().get(pk=turn.device_id)
            current = VoiceTurn.objects.select_for_update().get(pk=turn.pk)
            if current.state == "canceled":
                return
            turn.state = "uncertain" if turn.stage == "answer" else "failed"
            turn.error = str(exc)[:500]
            turn.save(update_fields=["state", "error", "updated_at"])
            emit(turn.device, "turn.failed", turn, detail=turn.error, state=turn.state)


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
    _, _, turn.answer_pages = frame(turn.answer, "AI 回复")
    turn.state = "answer_ready"
    turn.error = ""
    with transaction.atomic():
        turn.save()
        emit(turn.device, "answer.ready", turn)


def screen_answer(turn):
    if turn.answer.strip():
        return turn.answer
    payload = turn.assistant_message.payload if turn.assistant_message_id else {}
    reasons = [str(p.get("reason", "")) for p in payload.get("rejected_proposals", []) if p.get("reason")]
    if reasons:
        return "本次请求未能执行：\n" + "\n".join(reasons)
    return "AI 本轮没有返回文字。请在网页中查看会话及待确认操作，或换一种说法再试。"


def sweep():
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
    for turn in VoiceTurn.objects.filter(state="queued").order_by("created_at")[:20]:
        process_turn(turn.pk)
    # No raw audio is part of the immutable knowledge store.
    for turn in VoiceTurn.objects.exclude(state__in=ACTIVE).filter(created_at__lt=now - timedelta(hours=1)).exclude(audio_path="")[:100]:
        path = Path(turn.audio_path)
        if path.resolve().is_relative_to(audio_root().resolve()):
            path.unlink(missing_ok=True)
        turn.audio_path = ""
        turn.save(update_fields=["audio_path"])
