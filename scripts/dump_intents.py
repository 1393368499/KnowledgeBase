import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from apps.planner.operations import all_specs
from apps.planner.services import is_mutation

for spec in all_specs():
    print(json.dumps({
        "intent": spec.intent,
        "label": spec.label,
        "params": spec.params,
        "mut": is_mutation(spec.intent),
        "confirm": spec.requires_confirmation,
        # 提案后系统直接执行、不需要人点确认（目前只有只读的 search_knowledge）
        "auto": spec.auto_execute,
        "destructive": spec.requires_destructive_flow,
        "perm": spec.permission,
        "risk": spec.risk,
    }, ensure_ascii=False))
