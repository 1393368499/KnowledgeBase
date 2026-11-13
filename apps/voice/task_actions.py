"""Device-scoped task naming and confirmed conversation deletion."""
from pathlib import Path
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from apps.audit.services import append_event
from apps.planner import services as planner
from apps.planner.models import Conversation, OperationRecord
from .models import VoiceDevice, VoiceTurn, ConversationLease
from . import services


def owned_conversation(device, conversation_id):
    if not VoiceTurn.objects.filter(device=device, conversation_id=conversation_id).exclude(state="removed").exists():
        raise Http404()
    conversation = Conversation.objects.select_for_update().filter(pk=conversation_id, user=device.user).first()
    if conversation is None:
        raise Http404()
    return conversation


def rename(device, conversation_id, title):
    title = str(title or "").strip()
    if not title or len(title) > 200:
        raise ValidationError("任务名称需为 1–200 个字")
    with transaction.atomic():
        device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
        conversation = owned_conversation(device, conversation_id)
        previous = conversation.title
        conversation.title = title
        conversation.save(update_fields=["title", "updated_at"])
        append_event(action="conversation.renamed", actor_id=device.user_id, resource_kind="conversation",
            resource_key=f"conversation:{conversation_id}", payload={"previous_title": previous, "title": title, "device_id": str(device.pk)})
        selection = services.select_task(device, conversation_id) if services.task_route(device)[1] == conversation_id else {}
        services.emit(device, "task.renamed", title=title, conversation_id=conversation_id)
    return {"conversation_id": conversation_id, "title": title, **selection}


def delete(device, conversation_id, data):
    action = data.get("action", "prepare")
    with transaction.atomic():
        device = VoiceDevice.objects.select_for_update().get(pk=device.pk)
        operation = None
        if action in {"confirm", "abandon"}:
            operation_id = data.get("operation_id")
            if type(operation_id) is not int or not 0 < operation_id < 2**63:
                raise ValidationError("删除确认与当前任务不匹配")
            operation = OperationRecord.objects.select_for_update().filter(
                pk=operation_id, user=device.user, intent="delete_conversation",
                params__conversation=conversation_id, result__voice_device_id=str(device.pk),  # pyright: ignore[reportCallIssue]
            ).first()
            if not operation:
                raise ValidationError("删除确认与当前任务不匹配")
            if action == "confirm" and operation.status == "executed":
                return operation.result
        conversation = owned_conversation(device, conversation_id)
        if action == "abandon":
            assert operation is not None
            return planner.abandon_operation(device.user, operation)
        if VoiceTurn.objects.filter(conversation=conversation, state__in=services.ACTIVE).exists() or ConversationLease.objects.filter(conversation=conversation).exists():
            raise ValidationError("任务仍在处理中，请完成后再删除")
        if action == "prepare":
            proposal = planner.propose_operation(device.user, intent="delete_conversation", params={"conversation": conversation_id}, conversation=conversation)
            OperationRecord.objects.filter(pk=proposal["operation_id"]).update(result={"voice_device_id": str(device.pk)})
            return {**proposal, "title": conversation.title, "conversation_id": conversation_id,
                "detail": "将同步删除手机任务和知识库会话及聊天记录。已生成文档、已入库资料和操作审计保留。"}
        if action != "confirm" or operation is None:
            raise ValidationError("删除操作无效")
        planner.confirm_operation(device.user, operation, token=str(data.get("confirm_token", "")))
        turns = list(VoiceTurn.objects.filter(conversation=conversation).values_list("pk", "audio_path"))
        result = planner.execute_operation(device.user, operation)
        if result.get("status") == "failed":
            raise ValidationError("删除未完成，请核对任务状态")
        ids = [pk for pk, _ in turns]
        VoiceTurn.objects.filter(pk__in=ids).update(state="removed", transcript="", answer="", error="", answer_pages=0, transcript_pages=0, updated_at=timezone.now())
        if services.task_route(device)[1] == conversation_id:
            services.select_task(device, None)
        operation.refresh_from_db()
        operation.result = {**operation.result, "voice_device_id": str(device.pk)}
        operation.save(update_fields=["result"])
        services.emit(device, "task.deleted", conversation_id=conversation_id)
        def clean_audio():
            for pk, filename in turns:
                if not filename:
                    continue
                path = Path(filename)
                if not path.resolve().is_relative_to(services.audio_root().resolve()):
                    continue
                try:
                    path.unlink(missing_ok=True)
                    VoiceTurn.objects.filter(pk=pk, state="removed").update(audio_path="")
                except OSError:
                    services.logger.warning("Deleted task audio cleanup deferred")
        transaction.on_commit(clean_audio)
        return operation.result
