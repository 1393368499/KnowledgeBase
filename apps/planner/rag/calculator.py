"""Exact decimal arithmetic on explicitly selected source numbers. No eval()."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext
from .reader import readable_file
from .contracts import Evidence
from apps.knowledge.models import DocumentChunk

NUMBER = re.compile(r"(?<![\d.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])")
UNITS = {"元", "万元", "亿元", "人", "个", "份", "天", "月", "年", "%", "次", "台"}


class EvidenceCalculator:
    def calculate(self, user, *, operation, items, unit, scope=None):
        from apps.planner.services import PlannerError
        try:
            operands = json.loads(items)
        except (TypeError, ValueError):
            raise PlannerError("计算 items 必须是 JSON 数组") from None
        if operation not in {"sum", "difference", "average", "min", "max", "count"}:
            raise PlannerError("计算只支持 sum/difference/average/min/max/count")
        if not isinstance(operands, list) or not 1 <= len(operands) <= 100:
            raise PlannerError("计算需要 1～100 个有来源的数值")
        if operation == "difference" and len(operands) != 2:
            raise PlannerError("difference 必须恰好提供两个数值，按前项减后项")
        if unit not in UNITS:
            raise PlannerError("请提供统一且明确的单位：" + "、".join(sorted(UNITS)))
        values, evidence, seen = [], [], set()
        for item in operands:
            if not isinstance(item, dict) or not {"file_id", "ordinal", "value", "quote", "version"} <= set(item):
                raise PlannerError("每项需要 file_id、ordinal、value、quote、version")
            if type(item["file_id"]) is not int or type(item["ordinal"]) is not int:
                raise PlannerError("file_id 与 ordinal 必须为整数")
            record = readable_file(user, item["file_id"], scope)
            if item["version"] != record.content_sha256:
                raise PlannerError("计算来源版本已变化，请重新读取")
            chunk = DocumentChunk.objects.select_related("file__library").filter(
                file=record, ordinal=item["ordinal"]).first()
            quote = item["quote"]
            if chunk is None or not isinstance(quote, str) or not quote.strip() or quote not in chunk.text:
                raise PlannerError("计算引用必须逐字出现在对应原文段落")
            raw = str(item["value"])
            if len(raw) > 60 or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", raw):
                raise PlannerError("计算数值必须是有限十进制数，不能用表达式")
            try:
                value = Decimal(raw)
            except InvalidOperation:
                raise PlannerError("无效的十进制数") from None
            normalized = unicodedata.normalize("NFKC", quote)
            matches = [m for m in NUMBER.finditer(normalized)
                       if Decimal(m.group().replace(",", "")) == value]
            # Bind the selected value to its own unit, not another number in the sentence.
            if not any(normalized[m.end():].lstrip().startswith(unit) for m in matches):
                raise PlannerError("数值及其单位未在引用中相邻出现；请核对单位，不自动换算")
            key = (record.pk, chunk.ordinal, quote, raw)
            if key in seen:
                raise PlannerError("重复计算了同一来源数值，请先去重")
            seen.add(key)
            values.append(value)
            evidence.append(Evidence.from_chunk(chunk, source="calculation").to_dict())
        with localcontext() as ctx:
            ctx.prec = 100
            result = {"sum": lambda: sum(values, Decimal(0)),
                      "difference": lambda: values[0] - values[1],
                      "average": lambda: sum(values, Decimal(0)) / Decimal(len(values)),
                      "min": lambda: min(values), "max": lambda: max(values),
                      "count": lambda: Decimal(len(values))}[operation]()
        return {"operation": operation, "value": format(result, "f"),
                "unit": "项" if operation == "count" else unit, "operand_count": len(values),
                "evidence": evidence, "coverage": "provided_operands_only",
                "note": "仅计算已提供的数值；不代表全库统计，未自动判断业务去重或合同变更关系"}
