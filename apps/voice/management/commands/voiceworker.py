import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from apps.voice.services import sweep


class Command(BaseCommand):
    help = "Consume durable voice jobs independently of document ingestion"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            try:
                sweep()
            except Exception as exc:
                self.stderr.write(f"Voice worker: {type(exc).__name__}")
            if options["once"]:
                return
            time.sleep(1)
