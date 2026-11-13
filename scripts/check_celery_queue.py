import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from knowledgebase.celery import app

print("KB_CELERY_QUEUE =", repr(os.environ.get("KB_CELERY_QUEUE", "")))
print("task_default_queue =", app.conf.task_default_queue)
print("task_queues keys =", sorted((app.conf.task_queues or {}).keys()))
print("task_routes =", app.conf.task_routes)
