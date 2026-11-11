import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from apps.voice.services import sweep, process_turn
from apps.voice.models import VoiceTurn, ConversationLease


def run_turn(pk):
    close_old_connections()
    try:
        process_turn(pk)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = "Consume durable voice jobs independently of document ingestion"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=4)

    def handle(self, *args, **options):
        with ThreadPoolExecutor(max_workers=options["concurrency"]) as executor:
            running = {}
            while True:
                close_old_connections()
                for pk, future in list(running.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception as exc:
                            self.stderr.write(f"Voice job: {type(exc).__name__}")
                        del running[pk]
                try:
                    sweep(process_queued=False)
                    free = options["concurrency"] - len(running)
                    blocked = set(VoiceTurn.objects.filter(state__in=["transcribing", "transcribed", "answering", "rendering"]).values_list("conversation_id", flat=True))
                    blocked.update(VoiceTurn.objects.filter(pk__in=running).values_list("conversation_id", flat=True))
                    blocked.update(ConversationLease.objects.values_list("conversation_id", flat=True))
                    selected = []
                    for pk, conversation_id in VoiceTurn.objects.filter(state="queued").exclude(pk__in=running).order_by("created_at").values_list("pk", "conversation_id")[:100]:
                        if len(selected) >= free:
                            break
                        if conversation_id in blocked:
                            continue
                        selected.append(pk)
                        blocked.add(conversation_id)
                    for pk in selected:
                        running[pk] = executor.submit(run_turn, pk)
                except Exception as exc:
                    self.stderr.write(f"Voice worker: {type(exc).__name__}")
                if options["once"]:
                    for future in running.values():
                        future.result()
                    return
                time.sleep(1)
