from base64 import b32encode

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Enroll a Django admin in TOTP from a trusted interactive terminal."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        try:
            user = get_user_model().objects.get(
                username=options["username"], is_active=True, is_staff=True,
            )
        except get_user_model().DoesNotExist as exc:
            raise CommandError("No active Django admin with that username") from exc

        device = TOTPDevice(user=user, name="Authenticator", confirmed=False)
        self.stdout.write("Authenticator setup key (time-based, 6 digits, 30 seconds):")
        self.stdout.write(b32encode(device.bin_key).decode("ascii"))
        token = input("Enter the current code from the authenticator: ").strip()
        with transaction.atomic():
            if not device.verify_token(token):
                raise CommandError("Code verification failed; no device was enrolled")
            device.confirmed = True
            device.save(update_fields=["confirmed"])
        self.stdout.write(self.style.SUCCESS("TOTP enrollment verified"))
