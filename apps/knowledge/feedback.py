"""基于反馈的检索重排加权（目标 ③："基于反馈的学习规则"）。

**它是什么**：把"准确/不准确"的人工反馈聚合成一个**有界**的排序微调量，
让多次被判不准确的命中次序下降、被判准确的上升。

**它不是什么**（明确边界，避免误导）：
  * **不是训练** —— 不更新任何模型、不重写向量、不改源文档；
  * **不是硬过滤** —— 被判不准确**不会**让资料消失（那等于用反馈做删除），
    只是排序微调；权重有上下界，永远盖不过相关性本身；
  * **不是黑盒** —— 每个命中都会带上 `feedback_weight` 与 `feedback_counts`，
    调用方与界面都能看到"这一条为什么被上/下调"。

权重口径（可复盘）：
    每条 `accurate` 记 +1，每条 `inaccurate` 记 -1，
    归一化后**夹到 ±MAX_ADJUST**（默认 0.15），再乘到原始分数上做相对调整。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 单条命中的最大相对调整量。有界是硬要求：
#: 反馈只是"排序偏好"，绝不能把弱相关结果顶到强相关结果前面。
MAX_ADJUST = 0.15


def feedback_weights(file_ids: Iterable[int]) -> dict[int, float]:
    """按资料聚合反馈，返回 `{file_id: 权重}`（已夹到 ±MAX_ADJUST）。

    只统计**仍然存在**的资料（`SearchFeedback.file` 用了 `SET_NULL`，
    资料被删后反馈会挂在 NULL 上，不会被算进来）。
    """
    from django.db.models import Count, Q

    from .models import SearchFeedback

    wanted = [int(item) for item in file_ids]
    if not wanted:
        return {}

    rows = (
        SearchFeedback.objects.filter(file_id__in=wanted)
        .values("file_id")
        .annotate(
            accurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.ACCURATE)),
            inaccurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.INACCURATE)),
        )
    )

    weights: dict[int, float] = {}
    for row in rows:
        total = row["accurate"] + row["inaccurate"]
        if total <= 0:
            continue
        # 归一化到 [-1, 1]，再夹到 ±MAX_ADJUST
        raw = (row["accurate"] - row["inaccurate"]) / total
        weight = max(-MAX_ADJUST, min(MAX_ADJUST, raw * MAX_ADJUST))
        if weight:
            weights[row["file_id"]] = round(weight, 6)
    return weights


def feedback_counts(file_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """返回 `{file_id: {"accurate": n, "inaccurate": m}}`，用于**可审计**展示。"""
    from django.db.models import Count, Q

    from .models import SearchFeedback

    wanted = [int(item) for item in file_ids]
    if not wanted:
        return {}
    rows = (
        SearchFeedback.objects.filter(file_id__in=wanted)
        .values("file_id")
        .annotate(
            accurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.ACCURATE)),
            inaccurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.INACCURATE)),
        )
    )
    return {
        row["file_id"]: {"accurate": row["accurate"], "inaccurate": row["inaccurate"]}
        for row in rows
    }


def apply_feedback_weight(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按反馈权重**微调排序**，并把权重/计数写回每条命中（可审计）。

    调整方式：`adjusted = score * (1 + weight)`（权重有界 ⇒ 相对幅度很小）。
    没有 `score` 的命中（例如 DB 关键词兜底）按权重做**次序**微调而不编造分数。
    """
    ids = [int(item["file_id"]) for item in items if item.get("file_id") is not None]
    weights = feedback_weights(ids)
    counts = feedback_counts(ids)
    if not weights:
        return items

    for index, item in enumerate(items):
        raw_file_id = item.get("file_id")
        file_id = int(raw_file_id) if raw_file_id is not None else None
        weight = weights.get(file_id, 0.0) if file_id is not None else 0.0
        item["feedback_weight"] = weight
        item["feedback_counts"] = (
            counts.get(file_id, {"accurate": 0, "inaccurate": 0})
            if file_id is not None
            else {"accurate": 0, "inaccurate": 0}
        )
        if weight and isinstance(item.get("score"), (int, float)):
            item["score"] = round(float(item["score"]) * (1 + weight), 6)
        item["_feedback_order"] = -weight        # 次序微调（稳定排序用）

    # 文件名命中优先级不受反馈影响；同级才按调整分数/权重做稳定排序。
    def sort_key(pair: tuple[int, dict[str, Any]]):
        index, item = pair
        score = item.get("score")
        return (
            -int(item.get("filename_match_rank") or 0),
            -(score if isinstance(score, (int, float)) else 0.0),
            item.get("_feedback_order", 0.0),
            index,
        )

    ordered = [item for _, item in sorted(enumerate(items), key=sort_key)]
    for item in ordered:
        item.pop("_feedback_order", None)
    return ordered

def feedback_max_adjust() -> float:
    """当前生效的最大调整幅度（供报告/界面展示，便于复盘）。"""
    return MAX_ADJUST
