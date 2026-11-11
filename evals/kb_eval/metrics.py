"""评测指标：纯函数、零第三方依赖、可独立自测。

口径（与 README / 报告里 ``meta.metric_definitions`` 保持一致）：

Recall@k（文档级）
    先把结果按"文档"去重（同一文档的多个 chunk 只保留名次最靠前的那个），
    再取前 k 个文档。``recall@k`` 是**每查询 0/1**：ground_truth 里命中任一个
    就算 1（多真值不摊薄），最后对查询取平均。
    额外输出 ``recall_all@k`` = 前 k 个文档里命中的真值个数 / 真值总数，
    用来发现"只找到一个、漏了另一个"的情况。

MRR@k
    首个命中真值的**文档级名次**的倒数；前 k 个里没命中记 0。

要点命中率（context 级）
    把 top-k 命中 chunk 的文本拼接，逐条 key_point 做**空白归一化后子串匹配**：
    先 NFKC 归一化，再删掉全部空白字符，再小写化。中文没有空格，
    跨行/跨 chunk 的关键句只有删空白后才能匹配上；拉丁文大小写不敏感。
    输出 ``hit / total / rate / missing``。空 key_points 的用例 rate 记 null。

percentile
    排序后线性插值（等价 numpy 默认 ``method="linear"``）：
    ``rank = (p/100) * (n-1)``，在 ``floor(rank)`` 与 ``ceil(rank)`` 之间按小数部分插值。
    P50 即中位数；P95 在小样本里会偏向最大值（插值口径，已在报告 meta 里注明）。

token 估算
    确定性启发式：CJK 字符（含假名/谚文）1 个 = 1 token；连续拉丁字母词 = 1 token；
    连续数字串 = 1 token；空白与标点不计。只用于量级对比，不是真实 tokenizer
    ——报告 ``not_measured`` 里会如实标注。
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_K = 5

#: 报告里写明的百分位口径，避免以后有人换了算法却不知道历史结果怎么来的。
PERCENTILE_METHOD = "linear_interpolation"

_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # 汉字扩展 + 基本区 + 兼容区
    r"\u3040-\u30ff"  # 平假名 / 片假名
    r"\uac00-\ud7af]"  # 谚文
)
_LATIN_RE = re.compile(r"[A-Za-z]+(?:['\-][A-Za-z]+)*")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


# ---------------------------------------------------------------------------
# 文本归一化 & token 估算
# ---------------------------------------------------------------------------
def normalize_text(value: Any) -> str:
    """NFKC + 删空白 + 小写。用于要点子串匹配。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", "", text)
    return text.lower()


def normalize_key_point(value: Any) -> str:
    """要点归一化与 ``normalize_text`` 同口径（保留别名，语义更清楚）。"""
    return normalize_text(value)


def estimate_tokens(value: Any) -> int:
    """确定性 token 启发式估算：CJK 字符 + 拉丁词 + 数字串。"""
    if value is None:
        return 0
    text = str(value)
    return (
        len(_CJK_RE.findall(text))
        + len(_LATIN_RE.findall(text))
        + len(_NUM_RE.findall(text))
    )


def estimate_tokens_many(values: Iterable[Any]) -> int:
    return sum(estimate_tokens(item) for item in values)


def price_table(prices: Mapping[str, Any] | None = None) -> dict[str, float]:
    """规整 ``--prices`` 入参；非法值抛 ``ValueError``（由 CLI 转成可操作提示）。

    支持键：``input_per_m`` / ``output_per_m``（单位：每百万 token 的价格）。
    """
    table = {"input_per_m": 0.0, "output_per_m": 0.0}
    if not prices:
        return table
    if not isinstance(prices, Mapping):
        raise ValueError("prices 必须是 JSON 对象，如 {\"input_per_m\": 1.0, \"output_per_m\": 2.0}")
    unknown = set(prices) - set(table)
    if unknown:
        raise ValueError(f"prices 含未知键：{sorted(unknown)}；只支持 input_per_m / output_per_m")
    for key in table:
        if key not in prices:
            continue
        try:
            value = float(prices[key])
        except (TypeError, ValueError):
            raise ValueError(f"prices[{key}] 必须是数字，收到 {prices[key]!r}") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"prices[{key}] 必须是非负有限数，收到 {value!r}")
        table[key] = value
    return table


def parse_prices_json(raw: str | None) -> dict[str, float]:
    if raw in (None, ""):
        return price_table()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--prices 不是合法 JSON：{exc}") from None
    return price_table(loaded)


def cost_estimate(token_counts: Mapping[str, Any], prices: Mapping[str, Any] | None = None) -> dict:
    """按 token 估算成本（单位：美元，仅量级参考）。

    ``embed`` / ``rerank`` / ``llm_input`` 按输入单价，``llm_output`` 按输出单价。
    离线模式 llm 全 0，所以成本只来自 embed / rerank 估算。
    """
    table = price_table(prices)
    embed = int(token_counts.get("embed_tokens") or 0)
    rerank = int(token_counts.get("rerank_tokens") or 0)
    llm_input = int(token_counts.get("llm_input_tokens") or 0)
    llm_output = int(token_counts.get("llm_output_tokens") or 0)
    if not llm_input and token_counts.get("llm_tokens"):
        llm_input = int(token_counts["llm_tokens"])
    estimated_input = embed + rerank + llm_input
    cost_input = estimated_input * table["input_per_m"] / 1_000_000
    cost_output = llm_output * table["output_per_m"] / 1_000_000
    return {
        "input_per_m": table["input_per_m"],
        "output_per_m": table["output_per_m"],
        "estimated_input_tokens": estimated_input,
        "estimated_output_tokens": llm_output,
        "estimated_cost_usd": round(cost_input + cost_output, 10),
    }


# ---------------------------------------------------------------------------
# 文档级去重 & 排名
# ---------------------------------------------------------------------------
def result_doc_key(result: Mapping[str, Any]) -> str:
    """一条检索命中的文档键：优先 ``file_id``，退回 ``doc_id``。

    两者都是"整个文档"的标识，所以同一文档的多条 chunk 会归并到一起。
    """
    file_id = result.get("file_id")
    if file_id not in (None, ""):
        return str(file_id)
    doc_id = result.get("doc_id")
    if doc_id not in (None, ""):
        return str(doc_id)
    # 兜底：用原文名，避免整条结果被丢掉
    return str(result.get("original_name") or "")


def result_doc_aliases(result: Mapping[str, Any]) -> set[str]:
    """一条命中的全部文档标识别名（用于和数据集真值对齐）。

    harness 返回 file_id（数字），数据集写文档 key（人可读名，如 ``doc_a``）；
    在线 HTTP 只有 file_id / original_name。把两者能对上的写法都收进来：
    file_id、doc_id、original_name、original_name 去扩展名、``doc_aliases``。
    """
    aliases: set[str] = set()
    for field in ("file_id", "doc_id", "original_name", "filename"):
        value = result.get(field)
        if value not in (None, ""):
            aliases.add(str(value))
    original = result.get("original_name")
    if original:
        stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", str(original))
        if stem:
            aliases.add(stem)
    for extra in result.get("doc_aliases") or []:
        if extra not in (None, ""):
            aliases.add(str(extra))
    aliases.discard("")
    return aliases


def dedupe_by_doc(results: Sequence[Mapping[str, Any]]) -> list[dict]:
    """按文档去重、保持原顺序（名次最靠前的 chunk 代表该文档）。

    ``_rank`` 是**文档级名次**（去重后的位置），不是原始 chunk 下标：
    MRR 的口径是"第几个**文档**命中"，同一文档的重复 chunk 不占名次。
    原始下标保留在 ``_result_index`` 里方便排查。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for index, result in enumerate(results, start=1):
        key = result_doc_key(result)
        if key in seen:
            continue
        seen.add(key)
        row = dict(result)
        row["_doc_key"] = key
        row["_rank"] = len(out) + 1
        row["_result_index"] = index
        out.append(row)
    return out


def build_ground_truth_aliases(
    ground_truth_doc_ids: Sequence[Any],
    doc_id_map: Mapping[Any, Any] | None = None,
) -> dict[str, set[str]]:
    """把数据集的真值 id 展开成"可命中的别名集合"。

    ``doc_id_map`` 是 harness ``seed_corpus`` 返回的 ``{doc_key: file_id}``：
    数据集里写文档 key（人可读），检索结果给的是 file_id（数字），
    两边对不上就会全员 0 分——这里把两者都收进别名集合。
    另外如果真值本身就是 file_id，也直接保留。
    """
    aliases: dict[str, set[str]] = {}
    mapping = doc_id_map or {}
    for gt in ground_truth_doc_ids or []:
        canon = str(gt)
        values = {canon}
        mapped = mapping.get(canon)
        if mapped is None:
            # 允许 mapping 的 key 是数字、gt 是字符串等类型漂移
            for key, value in mapping.items():
                if str(key) == canon:
                    mapped = value
                    break
        if mapped not in (None, ""):
            values.add(str(mapped))
        aliases[canon] = values
    return aliases


def score_ranking(
    results: Sequence[Mapping[str, Any]],
    ground_truth_doc_ids: Sequence[Any],
    doc_id_map: Mapping[Any, Any] | None = None,
    k: int = DEFAULT_K,
) -> dict:
    """对单个查询的结果算 Recall@k / recall_all@k / MRR@k。

    返回 ``recall`` / ``recall_all`` / ``mrr`` / ``first_hit_rank`` / ``hit_docs`` /
    ``ranked_docs``（文档级排名，便于人工复核）。
    """
    if k <= 0:
        raise ValueError(f"k 必须是正整数，收到 {k}")
    aliases = build_ground_truth_aliases(ground_truth_doc_ids, doc_id_map)
    gt_total = len(aliases)
    deduped = dedupe_by_doc(results)
    top = deduped[:k]
    ranked_docs = [row["_doc_key"] for row in deduped]

    first_hit_rank: int | None = None
    hit_docs: list[str] = []
    matched_gt: list[str] = []
    for gt_id, values in aliases.items():
        for row in top:
            if result_doc_aliases(row) & values:
                if first_hit_rank is None or row["_rank"] < first_hit_rank:
                    first_hit_rank = row["_rank"]
                hit_docs.append(row["_doc_key"])
                matched_gt.append(gt_id)
                break
    hit_docs = list(dict.fromkeys(hit_docs))
    matched_gt = list(dict.fromkeys(matched_gt))

    recall = None if gt_total == 0 else (1.0 if matched_gt else 0.0)
    recall_all = None if gt_total == 0 else round(len(matched_gt) / gt_total, 6)
    mrr = 0.0 if first_hit_rank is None else round(1.0 / first_hit_rank, 6)
    return {
        "k": k,
        "recall": recall,
        "recall_all": recall_all,
        "mrr": None if gt_total == 0 else mrr,
        "first_hit_rank": first_hit_rank,
        "hit_docs": hit_docs,
        "matched_ground_truth": matched_gt,
        "ground_truth_total": gt_total,
        "ranked_docs": ranked_docs,
    }


# ---------------------------------------------------------------------------
# 要点命中率（context 级）
# ---------------------------------------------------------------------------
def key_point_hit_rate(
    context_text: str,
    key_points: Sequence[Any],
) -> dict:
    """在拼接后的检索上下文里做空白归一化子串匹配。

    ``rate`` 在 ``total == 0`` 时为 ``None``（不是 0），避免把"没有要点"
    的用例算成"要点全没命中"。
    """
    context = normalize_text(context_text)
    points = [str(point) for point in (key_points or []) if str(point).strip()]
    hit: list[str] = []
    missing: list[str] = []
    for point in points:
        needle = normalize_key_point(point)
        if needle and needle in context:
            hit.append(point)
        else:
            missing.append(point)
    total = len(points)
    return {
        "hit": len(hit),
        "total": total,
        "rate": None if total == 0 else round(len(hit) / total, 6),
        "hit_points": hit,
        "missing": missing,
    }


def join_context(results: Sequence[Mapping[str, Any]], k: int = DEFAULT_K) -> str:
    """把 top-k 命中的 chunk 文本拼成检索上下文（要点命中率的输入）。"""
    parts: list[str] = []
    for row in results[:k]:
        text = row.get("text") or ""
        if text:
            parts.append(str(text))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 延迟 / 百分位
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值百分位（numpy 默认口径，见模块 docstring）。"""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    if not 0 <= p <= 100:
        raise ValueError(f"百分位 p 必须在 [0,100]，收到 {p}")
    if len(data) == 1:
        return round(data[0], 6)
    rank = (p / 100.0) * (len(data) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(data[int(rank)], 6)
    weight = rank - low
    return round(data[low] + (data[high] - data[low]) * weight, 6)


def latency_stats(values: Sequence[float], prefix: str = "") -> dict:
    """P50 / P95 / mean / min / max / count；``prefix`` 用来区分 harness 与墙钟。"""
    data = [float(v) for v in values if v is not None]
    label = f"{prefix}_" if prefix else ""
    return {
        f"{label}p50_ms": percentile(data, 50),
        f"{label}p95_ms": percentile(data, 95),
        f"{label}mean_ms": round(sum(data) / len(data), 6) if data else None,
        f"{label}min_ms": round(min(data), 6) if data else None,
        f"{label}max_ms": round(max(data), 6) if data else None,
        f"{label}count": len(data),
    }


def mean_or_none(values: Iterable[Any]) -> float | None:
    data = [float(v) for v in values if v is not None]
    if not data:
        return None
    return round(sum(data) / len(data), 6)


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
def _run_self_test() -> int:
    failures: list[str] = []
    checks = 0

    def check(name: str, actual: Any, expected: Any) -> None:
        nonlocal checks
        checks += 1
        if actual != expected:
            failures.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
        else:
            print(f"PASS {name}")

    def check_close(name: str, actual: Any, expected: float, tol: float = 1e-6) -> None:
        nonlocal checks
        checks += 1
        if actual is None or abs(float(actual) - expected) > tol:
            failures.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
        else:
            print(f"PASS {name}")

    # --- recall / MRR：同名文档的两个 chunk 只能算一个名次 ---
    results = [
        {"file_id": 1, "text": "alpha"},
        {"file_id": 1, "text": "alpha again"},  # 同文档重复，不应挤占名次
        {"file_id": 2, "text": "beta"},
        {"file_id": 3, "text": "gamma"},
    ]
    scored = score_ranking(results, ["3"], k=5)
    check("dedupe rank of doc 3 == 3", scored["first_hit_rank"], 3)
    check_close("mrr 1/3", scored["mrr"], 1.0 / 3.0)
    check_close("recall@5 hit", scored["recall"], 1.0)
    check_close("recall_all@5 single gt", scored["recall_all"], 1.0)
    check("ranked docs deduped", scored["ranked_docs"], ["1", "2", "3"])

    # --- 多真值：命中一个 recall=1，recall_all=0.5 ---
    scored = score_ranking(results, ["2", "99"], k=5)
    check_close("recall any of multi gt", scored["recall"], 1.0)
    check_close("recall_all partial", scored["recall_all"], 0.5)
    check_close("mrr rank2", scored["mrr"], 0.5)
    check("matched gt", scored["matched_ground_truth"], ["2"])

    # --- 超过 k 的命中不算 ---
    scored = score_ranking(results, ["3"], k=2)
    check("recall@2 miss", scored["recall"], 0.0)
    check("mrr@2 miss", scored["mrr"], 0.0)
    check("first_hit_rank@2", scored["first_hit_rank"], None)

    # --- 真值走 seed 映射（doc_key -> file_id）---
    scored = score_ranking(results, ["policy_v2"], k=5, doc_id_map={"policy_v2": 2})
    check_close("gt alias via doc_id_map", scored["mrr"], 0.5)
    check_close("gt alias recall", scored["recall"], 1.0)

    # --- 真值走 original_name / 去扩展名别名（在线模式只有文件名）---
    scored = score_ranking(
        [{"file_id": 7, "original_name": "policy_v2.md", "text": ""}], ["policy_v2"], k=5
    )
    check_close("gt alias via original_name stem", scored["mrr"], 1.0)
    check("gt alias hit docs", scored["hit_docs"], ["7"])

    # --- 空真值：不参与平均，指标为 null ---
    scored = score_ranking(results, [], k=5)
    check("empty gt recall is null", scored["recall"], None)
    check("empty gt mrr is null", scored["mrr"], None)
    check("empty gt recall_all is null", scored["recall_all"], None)

    # --- 要点命中率：空白归一化 + 大小写 ---
    context = "报价单要求：合同金额 100 万，\n并于 30 日内 完成 盖章。"
    kp = key_point_hit_rate(context, ["合同金额100万", "30日内完成盖章", "完全不存在的要点"])
    check("key_point hit count", kp["hit"], 2)
    check("key_point total", kp["total"], 3)
    check_close("key_point rate", kp["rate"], 2.0 / 3.0)
    check("key_point missing", kp["missing"], ["完全不存在的要点"])
    kp_case = key_point_hit_rate("Delivery is READY", ["delivery is ready"])
    check_close("key_point latin case-insensitive", kp_case["rate"], 1.0)
    kp_empty = key_point_hit_rate("anything", [])
    check("key_point empty total", kp_empty["total"], 0)
    check("key_point empty rate is null", kp_empty["rate"], None)

    # --- 百分位：线性插值口径 ---
    check_close("p50 even", percentile([1, 2, 3, 4], 50), 2.5)
    check_close("p95 of 1..4", percentile([1, 2, 3, 4], 95), 3.85)
    check_close("p50 odd", percentile([3, 1, 2], 50), 2.0)
    check_close("p95 single", percentile([10.0], 95), 10.0)
    check("percentile empty", percentile([], 95), None)
    stats = latency_stats([10, 20, 30, 40])
    check_close("p50 stats", stats["p50_ms"], 25.0)
    check_close("mean stats", stats["mean_ms"], 25.0)
    check("stats count", stats["count"], 4)
    check("stats prefix keys", sorted(k for k in latency_stats([1], "harness") if k.startswith("harness_"))[:3],
          ["harness_count", "harness_max_ms", "harness_mean_ms"])

    # --- token 估算：2 个汉字 + 1 拉丁词 + 1 数字串 ---
    check("estimate tokens mixed", estimate_tokens("你好 world 123"), 4)
    check("estimate tokens empty", estimate_tokens(""), 0)
    check("estimate tokens punctuation only", estimate_tokens("，。！? "), 0)
    check("estimate tokens many", estimate_tokens_many(["你好", "hi there"]), 4)

    # --- 成本：2M 输入 tokens @ 1.0/M = 2.0 美元 ---
    cost = cost_estimate({"embed_tokens": 1_000_000, "rerank_tokens": 1_000_000,
                          "llm_input_tokens": 0, "llm_output_tokens": 0},
                         {"input_per_m": 1.0, "output_per_m": 2.0})
    check_close("cost input", cost["estimated_cost_usd"], 2.0)
    check_close("cost default zero", cost_estimate({"embed_tokens": 123})["estimated_cost_usd"], 0.0)

    # --- prices 解析 ---
    check("parse prices", parse_prices_json('{"input_per_m": 0.5, "output_per_m": 1}'),
          {"input_per_m": 0.5, "output_per_m": 1.0})
    try:
        parse_prices_json("{bad json")
    except ValueError as exc:
        check("parse prices invalid json raises", "不是合法 JSON" in str(exc), True)
    else:
        check("parse prices invalid json raises", "no raise", True)
    try:
        price_table({"input_per_m": -1})
    except ValueError:
        check("negative price rejected", True, True)
    else:
        check("negative price rejected", False, True)

    print()
    if failures:
        for line in failures:
            print(line)
        print(f"FAILED: {len(failures)}/{checks} checks failed")
        return 1
    print(f"ALL PASS: {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(_run_self_test())
