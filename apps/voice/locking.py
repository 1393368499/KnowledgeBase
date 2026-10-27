import uuid
from datetime import timedelta
from functools import wraps

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ConversationLease


class ConversationBusy(RuntimeError):
    pass


def serialize_conversation(func):
    @wraps(func)
    def wrapped(user, conversation, *args, **kwargs):
        token = uuid.uuid4()
        now = timezone.now()
        # A late external response must never race a replacement worker. Expired
        # leases therefore need inspection rather than automatic takeover.
        try:
            with transaction.atomic():
                ConversationLease.objects.create(
                    conversation=conversation, token=token, expires_at=now + timedelta(minutes=15),
                )
        except IntegrityError:
            raise ConversationBusy("这个会话正在处理另一条消息；若处理进程意外退出，请先核对处理状态")
        try:
            return func(user, conversation, *args, **kwargs)
        finally:
            ConversationLease.objects.filter(conversation=conversation, token=token).delete()
    return wrapped
