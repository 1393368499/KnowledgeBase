"""Real-model feature-on/off evaluation; no model-quality claims from scripted tests."""
import json
import time
import unicodedata
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from apps.planner.models import Conversation
from apps.planner.services import reply_to_message


def normalize(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


class Command(BaseCommand):
    help = "用真实模型评测JSONL问题集，创建带审计的评测会话；输出不含原始问题/答案的指标报告"

    def add_arguments(self, parser):
        parser.add_argument("--dataset", required=True, help="JSONL: id, question, library_codes, key_points, mode 可选")
        parser.add_argument("--username", required=True, help="以此账号的真实权限检索")
        parser.add_argument("--output", required=True)
        parser.add_argument("--variant", choices=["agent", "control", "both"], default="agent")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"], is_active=True).first()
        if user is None:
            raise CommandError("账号不存在或已停用")
        try:
            rows = [json.loads(line) for line in Path(options["dataset"]).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("question"), str) or not row["question"].strip()
                        or not isinstance(row.get("key_points", []), list)
                        or not all(isinstance(x, str) for x in row.get("key_points", []))
                        or not isinstance(row.get("library_codes", []), list)):
                    raise ValueError("每题需要 question，key_points 与 library_codes 应为数组")
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(f"问题集无效：{exc}") from exc
        variants = [True, False] if options["variant"] == "both" else [options["variant"] == "agent"]
        results = []
        for enabled in variants:
            for index, row in enumerate(rows[:max(1, min(options["limit"], 1000))]):
                conversation = Conversation.objects.create(user=user, title=f"RAG评测 {'agent' if enabled else 'control'} #{index+1}")
                started = time.monotonic()
                item = {"case": str(row.get("id", index+1)), "variant": "agent" if enabled else "control",
                        "conversation_id": conversation.pk}
                try:
                    scope = row.get("library_codes") or None
                    with override_settings(KB_RAG_ENABLED=enabled):
                        answer = reply_to_message(user, conversation, row["question"], library_codes=scope,
                                                  strict_scope=bool(scope), research_mode=row.get("mode", "auto"))
                    research = answer.get("research", {})
                    points = row.get("key_points", [])
                    item.update(status="ok", keypoint_rate=(sum(normalize(x) in normalize(answer["content"]) for x in points)/len(points) if points else None),
                                source_count=len(research.get("sources", [])), verification=research.get("verification"),
                                budget=research.get("budget"), stop_reason=research.get("stop_reason"),
                                usage=research.get("usage") or answer.get("usage"))
                except Exception as exc:
                    item.update(status="error", error_type=type(exc).__name__)
                item["elapsed_seconds"] = round(time.monotonic()-started, 3)
                results.append(item)
                self.stdout.write(f"{item['variant']} {index+1}: {item['status']}")
        report = {"kind": "real_model_evaluation", "control_definition": "current code with KB_RAG_ENABLED=0, not a historical build",
                  "limitations": "keypoint substring rate is a proxy; factual correctness/completeness requires gold source coverage and independent review; no price estimates",
                  "results": results}
        target = Path(options["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"已保存 {len(results)} 条真实评测记录"))
