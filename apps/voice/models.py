import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models


class VoiceDevice(models.Model):
    if TYPE_CHECKING:
        conversation_id: int | None
        user_id: int

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    device_type = models.CharField(max_length=60, default="zectrix-note4")
    token_hash = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    conversation = models.ForeignKey("planner.Conversation", null=True, blank=True, on_delete=models.SET_NULL)
    scope_mode = models.CharField(max_length=20, default="all_authorized")
    library_codes = models.JSONField(default=list)
    config_version = models.PositiveIntegerField(default=1)
    capabilities = models.JSONField(default=dict)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class VoiceTurn(models.Model):
    if TYPE_CHECKING:
        device_id: uuid.UUID
        conversation_id: int | None
        user_message_id: int | None
        assistant_message_id: int | None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(VoiceDevice, on_delete=models.PROTECT)
    client_turn_id = models.UUIDField()
    conversation = models.ForeignKey("planner.Conversation", null=True, on_delete=models.SET_NULL)
    config_version = models.PositiveIntegerField()
    scope_mode = models.CharField(max_length=20)
    library_codes = models.JSONField(default=list)
    audio_sha256 = models.CharField(max_length=64)
    audio_path = models.TextField()
    state = models.CharField(max_length=24, default="queued", db_index=True)
    stage = models.CharField(max_length=24, default="asr")
    transcript = models.TextField(blank=True)
    error = models.TextField(blank=True)
    user_message = models.ForeignKey("planner.Message", null=True, on_delete=models.SET_NULL, related_name="voice_inputs")
    assistant_message = models.ForeignKey("planner.Message", null=True, on_delete=models.SET_NULL, related_name="voice_answers")
    answer = models.TextField(blank=True)
    answer_pages = models.PositiveIntegerField(default=0)
    transcript_pages = models.PositiveIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "client_turn_id"], name="voice_device_turn_unique")]


class VoiceEvent(models.Model):
    if TYPE_CHECKING:
        device_id: uuid.UUID
        turn_id: uuid.UUID | None

    device = models.ForeignKey(VoiceDevice, on_delete=models.PROTECT)
    turn = models.ForeignKey(VoiceTurn, null=True, on_delete=models.CASCADE)
    kind = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ConversationLease(models.Model):
    conversation = models.OneToOneField("planner.Conversation", on_delete=models.CASCADE, primary_key=True)
    token = models.UUIDField()
    expires_at = models.DateTimeField()
