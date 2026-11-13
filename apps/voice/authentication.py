import hashlib

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import VoiceDevice


def credential_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeviceAuthentication(BaseAuthentication):
    """Device credentials are accepted only by device endpoints, never general APIs."""
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if len(parts) != 2 or parts[0].lower() != b"bearer":
            raise AuthenticationFailed("需要设备凭据")
        try:
            raw = parts[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("设备凭据无效")
        device = VoiceDevice.objects.select_related("user").filter(
            token_hash=credential_hash(raw), enabled=True, removed_at__isnull=True,
            user__is_active=True,  # pyright: ignore[reportCallIssue]
        ).first()
        if device is None:
            raise AuthenticationFailed("设备凭据无效或设备已停用")
        VoiceDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
        return device.user, device

    def authenticate_header(self, request):
        return "Bearer"
