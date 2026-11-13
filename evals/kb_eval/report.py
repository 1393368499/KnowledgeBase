"""把逐条评测 case 聚合成可分发的报告 JSON。

输出结构（``run_eval.py`` 写盘的就是这个）：:

    {
      "meta":       {run_id, mode, dataset, dataset_sha256, limit, ...},
      "summary":    {"overall": {...}, "by_bucket": {"typical": {...}, ...}},
      "cases":      [逐条查询明细],
      "not_measured": [{"metric","measured","reason"}, ...]
    }

设计约束：

- 只做聚合与格式化，不跑检索、不写盘之外的动作（写盘函数只负责 JSON）。
- 所有平均值在无数值时为 ``null``，不用 0 假装"测了但结果差"。
- ``not_measured`` 由调用方传入；本模块提供离线/在线默认清单，
  每一项必须能回答"为什么没测"。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # 作为包导入（run_eval 的正常路径）
    from . import metrics as m
except ImportError:  # 直接 `python report.py` 自测时
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import metrics as m  # type: ignore[no-redef]

REPORT_VERSION = 1

#: 报告里固定出现的三个 bucket；数据集出现新 bucket 时追加，不丢弃。
STANDARD_BUCKETS = ("typical", "hard", "pitfall")

#: 指标口径说明随报告一起落盘，避免以后口径变了历史 JSON 无法解释。
METRIC_DEFINITIONS = {
    "recall_at_5": "文档级去重后取前 5 个文档；命中任一 ground_truth 记为 1，按查询平均。空真值不参与平均。",
    "recall_all_at_5": "前 5 个文档里命中真值数 / 真值总数，按查询平均；用于发现多真值只命中一部分。",
    "mrr_at_5": "首个命中真值的文档级名次倒数；前 5 无命中记 0。",
    "key_point_hit_rate": (
        "top-5 chunk 文本拼接后，逐 key_point 做 NFKC + 删空白 + 小写后的子串匹配；"
        "微平均 = 总命中要点 / 总要点数。"
    ),
    "latency": "harness 返回的 elapsed_ms 为主；另记 time.perf_counter 墙钟用于交叉校验。",
    "percentile_method": m.PERCENTILE_METHOD,
    "token_estimation": (
        "确定性启发式：CJK 字符 1 个 = 1 token；连续拉丁词 = 1；连续数字串 = 1；"
        "空白标点不计。不是真实 tokenizer。"
    ),
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp(moment: datetime | None = None) -> str:
    """``20261029T013600Z`` 形式的时间戳（结果文件名用）。"""
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def git_short_hash(repo_root: str | Path | None) -> str | None:
    """取 ``git rev-parse --short HEAD``；不是 git 仓库 / 没装 git 就返回 None。"""
    if not repo_root:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def new_run_id(moment: datetime | None = None) -> str:
    return f"{utc_stamp(moment)}-{uuid.uuid4().hex[:8]}"


def build_meta(
    *,
    mode: str,
    dataset_path: str | Path,
    dataset_sha256: str | None,
    limit: int,
    k: int,
    run_id: str,
    repo_root: str | Path | None,
    workdir: str | Path | None,
    dense: bool,
    rerank: bool,
    prices: Mapping[str, Any],
    git_hash: str | None = None,
    generated_at: str | None = None,
    degraded: Sequence[str] | None = None,
    cases_total: int | None = None,
    elapsed_seconds: float | None = None,
    extras: Mapping[str, Any] | None = None,
) -> dict:
    """组装 ``meta``：可复现一次运行所需的全部上下文。"""
    price_table = m.price_table(prices)
    nonzero_price = price_table["input_per_m"] > 0 or price_table["output_per_m"] > 0
    meta: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "mode": mode,
        "dataset": str(Path(dataset_path).resolve()),
        "dataset_sha256": dataset_sha256,
        "limit": int(limit),
        "k": int(k),
        "cases_total": cases_total,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "git_short_hash": git_hash,
        "repo_root": str(repo_root) if repo_root else None,
        "workdir": str(workdir) if workdir else None,
        "dense": bool(dense),
        "rerank": bool(rerank),
        "prices": price_table,
        "cost_basis": "configured_price" if nonzero_price else "estimated_placeholder_price",
        "generated_at": generated_at or utc_iso(),
        "elapsed_seconds": None if elapsed_seconds is None else round(float(elapsed_seconds), 3),
        "degraded": list(degraded or []),
        "metric_definitions": METRIC_DEFINITIONS,
    }
    if extras:
        for key, value in extras.items():
            meta[key] = value
    return meta


# ---------------------------------------------------------------------------
# 默认 not_measured 清单
# ---------------------------------------------------------------------------
def default_not_measured(
    *,
    offline: bool = True,
    dense: bool = False,
    rerank: bool = False,
    online: bool = False,
    custom: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict]:
    """如实列出"这次没测"的指标。``measured`` 一律 false。

    名称与口径由 Lead 统一（2026-10-29）：离线报告只承诺词法基线，
    真实栈数字由 ``evals/live_check.py`` 另出 ``evals/results/live-*.json``。
    """
    items: list[dict] = []

    def add(metric: str, reason: str) -> None:
        items.append({"metric": metric, "measured": False, "reason": reason})

    add(
        "answer_level_keypoint_hit",
        "answer 级要点命中需要 LLM 生成答案；离线链路不生成答案，该指标为 null，"
        "本报告只输出 context 级要点命中率。",
    )
    add(
        "dense_semantic_quality",
        "离线 Qdrant embedded 下的向量由确定性桩生成（假向量），不代表真实 embedding 的语义召回质量。",
    )
    add(
        "rerank_quality",
        "离线适配器没有 AS711 重排服务，rerank 为退化/桩路径，不能用于评价重排质量。",
    )
    add(
        "real_model_tokens",
        "token 数为启发式估算（CJK 字符 / 拉丁词 / 数字串），本次没有真实模型调用，"
        "不是真实 tokenizer 计数。",
    )
    add(
        "live_stack",
        "真实栈（PG / Qdrant / AS711 / 真实检索链路）的数字见 evals/results/live-*.json"
        "（由 evals/live_check.py 产出），不混进本份离线报告。",
    )
    if custom:
        for extra in custom:
            item = {"metric": str(extra.get("metric", "custom")),
                    "measured": bool(extra.get("measured", False)),
                    "reason": str(extra.get("reason", ""))}
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _mean(values: Iterable[Any]) -> float | None:
    return m.mean_or_none(values)


def _sum_token_field(cases: Sequence[Mapping[str, Any]], name: str) -> int:
    total = 0
    for case in cases:
        tokens = case.get("tokens") or {}
        total += int(tokens.get(name) or 0)
    return total


def summarize_cases(
    cases: Sequence[Mapping[str, Any]],
    k: int = m.DEFAULT_K,
    prices: Mapping[str, Any] | None = None,
) -> dict:
    """把一个 case 列表聚合成一组指标（overall 和每个 bucket 共用）。"""
    total = len(cases)
    scored = [case for case in cases if case.get("status", "ok") == "ok"]
    errored = [case for case in cases if case.get("status", "ok") != "ok"]
    with_gt = [case for case in scored if case.get("recall_at_5") is not None]

    key_point_hit = 0
    key_point_total = 0
    key_point_rates: list[float] = []
    for case in scored:
        points = case.get("key_points") or {}
        total_points = int(points.get("total") or 0)
        if total_points <= 0:
            continue
        key_point_hit += int(points.get("hit") or 0)
        key_point_total += total_points
        rate = points.get("rate")
        if rate is not None:
            key_point_rates.append(float(rate))

    latency = {}
    latency.update(m.latency_stats(
        [elapsed for case in scored if (elapsed := case.get("elapsed_ms")) is not None],
        prefix="harness",
    ))
    latency.update(m.latency_stats(
        [wall for case in scored if (wall := case.get("wall_ms")) is not None],
        prefix="wall",
    ))
    total_wall_ms = sum(
        float(case.get("wall_ms") or 0) for case in scored
    )

    token_counts = {
        "embed_tokens": _sum_token_field(cases, "embed_tokens"),
        "rerank_tokens": _sum_token_field(cases, "rerank_tokens"),
        "llm_tokens": _sum_token_field(cases, "llm_tokens"),
        "llm_input_tokens": _sum_token_field(cases, "llm_input_tokens"),
        "llm_output_tokens": _sum_token_field(cases, "llm_output_tokens"),
    }
    token_counts["total_tokens"] = (
        token_counts["embed_tokens"] + token_counts["rerank_tokens"] + token_counts["llm_tokens"]
    )
    cost = m.cost_estimate(token_counts, prices)

    return {
        "k": k,
        "cases_total": total,
        "cases_scored": len(scored),
        "cases_error": len(errored),
        "cases_with_ground_truth": len(with_gt),
        "cases_missing_ground_truth": len(scored) - len(with_gt),
        "recall_at_5": _mean(case.get("recall_at_5") for case in with_gt),
        "recall_all_at_5": _mean(case.get("recall_all_at_5") for case in with_gt),
        "mrr_at_5": _mean(case.get("mrr_at_5") for case in with_gt),
        "answer_key_point_hit_rate": None,
        "key_point_hit": key_point_hit,
        "key_point_total": key_point_total,
        "key_point_hit_rate": (
            round(key_point_hit / key_point_total, 6) if key_point_total else None
        ),
        "key_point_hit_rate_macro": _mean(key_point_rates),
        "latency": latency,
        "total_wall_ms": round(total_wall_ms, 3),
        "tokens": token_counts,
        "cost": cost,
        "error_rate": round(len(errored) / total, 6) if total else None,
    }


def bucket_of(case: Mapping[str, Any]) -> str:
    bucket = str(case.get("bucket") or "unknown")
    return bucket or "unknown"


def build_report(
    *,
    meta: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    not_measured: Sequence[Mapping[str, Any]],
    k: int = m.DEFAULT_K,
) -> dict:
    """组装最终报告：overall + by_bucket（固定三桶先出现，新桶追加）。"""
    prices = meta.get("prices") if isinstance(meta, Mapping) else None
    overall = summarize_cases(cases, k, prices)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(bucket_of(case), []).append(case)
    bucket_order = list(STANDARD_BUCKETS) + sorted(set(grouped) - set(STANDARD_BUCKETS))
    by_bucket: dict[str, dict] = {}
    for bucket in bucket_order:
        rows = grouped.get(bucket, [])
        stats = summarize_cases(rows, k, prices)
        stats["bucket"] = bucket
        by_bucket[bucket] = stats
    return {
        "meta": dict(meta),
        "summary": {"overall": overall, "by_bucket": by_bucket},
        "cases": list(cases),
        "not_measured": [dict(item) for item in not_measured],
    }


# ---------------------------------------------------------------------------
# 写盘 & 终端摘要
# ---------------------------------------------------------------------------
def write_report(report: Mapping[str, Any], out_dir: str | Path, filename: str | None = None) -> Path:
    """写 ``<out_dir>/<UTC 时间戳>.json``；重名时加短随机后缀，绝不覆盖历史结果。"""
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"{utc_stamp()}.json"
    if not name.endswith(".json"):
        name += ".json"
    target = target_dir / name
    if target.exists():  # 同一秒内跑两次
        target = target_dir / f"{target.stem}-{uuid.uuid4().hex[:4]}{target.suffix}"
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=_json_default)
        handle.write("\n")
    return target


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:5.1f}%"


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "  n/a"
    return f"{float(value):7.1f}"


def format_summary_table(report: Mapping[str, Any]) -> str:
    """终端人类可读摘要：总体 + 分 bucket。"""
    summary = report.get("summary") or {}
    overall = summary.get("overall") or {}
    by_bucket = summary.get("by_bucket") or {}
    meta = report.get("meta") or {}

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("KnowledgeBase AI 检索评测基线")
    lines.append("=" * 78)
    lines.append(
        f"run_id={meta.get('run_id')}  mode={meta.get('mode')}  "
        f"k=5  limit={meta.get('limit')}  cases={overall.get('cases_total')}"
    )
    lines.append(f"dataset={meta.get('dataset')}")
    lines.append(f"dataset_sha256={(meta.get('dataset_sha256') or '')[:16]}…  python={meta.get('python')}")
    dense = "on" if meta.get("dense") else "off"
    rerank = "on" if meta.get("rerank") else "off"
    lines.append(f"dense={dense}  rerank={rerank}  cost_basis={meta.get('cost_basis')}")
    for note in meta.get("degraded") or []:
        lines.append(f"  [degraded] {note}")
    lines.append("")

    header = (
        f"{'bucket':<10}{'cases':>6}{'recall@5':>10}{'recall_all':>11}"
        f"{'MRR@5':>9}{'要点命中':>10}{'P50ms':>9}{'P95ms':>9}{'errors':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    def row(label: str, stats: Mapping[str, Any]) -> str:
        latency = stats.get("latency") or {}
        return (
            f"{label:<10}{stats.get('cases_total', 0):>6}"
            f"{_fmt_rate(stats.get('recall_at_5')):>10}"
            f"{_fmt_rate(stats.get('recall_all_at_5')):>11}"
            f"{_fmt_rate(stats.get('mrr_at_5')):>9}"
            f"{_fmt_rate(stats.get('key_point_hit_rate')):>10}"
            f"{_fmt_ms(latency.get('harness_p50_ms')):>9}"
            f"{_fmt_ms(latency.get('harness_p95_ms')):>9}"
            f"{stats.get('cases_error', 0):>8}"
        )

    lines.append(row("OVERALL", overall))
    for bucket, stats in by_bucket.items():
        if stats.get("cases_total", 0) == 0:
            continue
        lines.append(row(bucket, stats))
    lines.append("")

    tokens = overall.get("tokens") or {}
    cost = overall.get("cost") or {}
    lines.append(
        f"tokens(est): embed={tokens.get('embed_tokens', 0)} "
        f"rerank={tokens.get('rerank_tokens', 0)} llm={tokens.get('llm_tokens', 0)} "
        f"total={tokens.get('total_tokens', 0)}"
    )
    lines.append(
        f"cost(est): ${cost.get('estimated_cost_usd', 0.0)} "
        f"(input_per_m={cost.get('input_per_m', 0)} output_per_m={cost.get('output_per_m', 0)}, "
        f"basis={meta.get('cost_basis')})"
    )
    lines.append(f"total_wall={overall.get('total_wall_ms', 0) / 1000:.2f}s")
    lines.append("")

    not_measured = report.get("not_measured") or []
    lines.append(f"未实测项（{len(not_measured)} 项，报告 JSON 里逐条有 reason）：")
    for item in not_measured:
        lines.append(f"  - {item.get('metric')}: {item.get('reason')}")
    lines.append("=" * 78)
    return "\n".join(lines)


def print_summary(report: Mapping[str, Any], stream: Callable[[str], Any] | None = None) -> None:
    text = format_summary_table(report)
    (stream or print)(text)


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
def _sample_cases() -> list[dict]:
    return [
        {
            "id": "q1", "bucket": "typical", "query": "报销流程", "status": "ok",
            "elapsed_ms": 100.0, "wall_ms": 105.0,
            "recall_at_5": 1.0, "recall_all_at_5": 1.0, "mrr_at_5": 1.0,
            "key_points": {"hit": 2, "total": 2, "rate": 1.0, "hit_points": [], "missing": []},
            "tokens": {"embed_tokens": 10, "rerank_tokens": 0, "llm_tokens": 0,
                       "llm_input_tokens": 0, "llm_output_tokens": 0},
            "cost": {"estimated_cost_usd": 0.0},
        },
        {
            "id": "q2", "bucket": "hard", "query": "违约金怎么算", "status": "ok",
            "elapsed_ms": 300.0, "wall_ms": 290.0,
            "recall_at_5": 0.0, "recall_all_at_5": 0.0, "mrr_at_5": 0.0,
            "key_points": {"hit": 1, "total": 4, "rate": 0.25, "hit_points": [], "missing": []},
            "tokens": {"embed_tokens": 20, "rerank_tokens": 30, "llm_tokens": 0,
                       "llm_input_tokens": 0, "llm_output_tokens": 0},
            "cost": {"estimated_cost_usd": 0.0},
        },
        {
            "id": "q3", "bucket": "pitfall", "query": "哪个库有印章", "status": "ok",
            "elapsed_ms": 200.0, "wall_ms": 210.0,
            "recall_at_5": 1.0, "recall_all_at_5": 0.5, "mrr_at_5": 0.5,
            "key_points": {"hit": 0, "total": 0, "rate": None, "hit_points": [], "missing": []},
            "tokens": {"embed_tokens": 30, "rerank_tokens": 0, "llm_tokens": 0,
                       "llm_input_tokens": 0, "llm_output_tokens": 0},
            "cost": {"estimated_cost_usd": 0.0},
        },
        {
            "id": "q4", "bucket": "typical", "query": "坏查询", "status": "error",
            "error": "search failed: boom", "elapsed_ms": None, "wall_ms": 5.0,
            "recall_at_5": None, "recall_all_at_5": None, "mrr_at_5": None,
            "key_points": {"hit": 0, "total": 0, "rate": None, "hit_points": [], "missing": []},
            "tokens": {"embed_tokens": 0, "rerank_tokens": 0, "llm_tokens": 0,
                       "llm_input_tokens": 0, "llm_output_tokens": 0},
            "cost": {"estimated_cost_usd": 0.0},
        },
    ]


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

    cases = _sample_cases()
    meta = build_meta(
        mode="offline", dataset_path="evals/datasets/kb-50.jsonl",
        dataset_sha256="deadbeef", limit=50, k=5, run_id="run-selftest",
        repo_root="<repo-root>", workdir="D:/tmp/wd",
        dense=False, rerank=False, prices={},
        git_hash="abc1234", generated_at="2026-10-29T01:36:00Z",
        degraded=["offline test harness"],
    )
    not_measured = default_not_measured(offline=True)
    report = build_report(meta=meta, cases=cases, not_measured=not_measured)

    overall = report["summary"]["overall"]
    check("overall cases_total", overall["cases_total"], 4)
    check("overall cases_scored", overall["cases_scored"], 3)
    check("overall cases_error", overall["cases_error"], 1)
    check("overall recall mean", overall["recall_at_5"], round(2.0 / 3.0, 6))
    check("overall recall_all mean", overall["recall_all_at_5"], round(1.5 / 3.0, 6))
    check("overall mrr mean", overall["mrr_at_5"], round(1.5 / 3.0, 6))
    check("overall key_point micro", overall["key_point_hit_rate"], round(3.0 / 6.0, 6))
    check("overall key_point total", overall["key_point_total"], 6)
    check("overall p50 (2 samples insuff? no, 3)", overall["latency"]["harness_p50_ms"], 200.0)
    check("overall p95", overall["latency"]["harness_p95_ms"], round(290.0, 6))
    check("overall tokens embed", overall["tokens"]["embed_tokens"], 60)
    check("overall tokens total", overall["tokens"]["total_tokens"], 90)

    buckets = report["summary"]["by_bucket"]
    check("bucket typical cases", buckets["typical"]["cases_total"], 2)
    check("bucket typical scored", buckets["typical"]["cases_scored"], 1)
    check("bucket typical recall", buckets["typical"]["recall_at_5"], 1.0)
    check("bucket hard recall", buckets["hard"]["recall_at_5"], 0.0)
    check("bucket hard keypoint", buckets["hard"]["key_point_hit_rate"], 0.25)
    check("empty bucket present", buckets["pitfall"]["cases_total"], 1)

    check("not_measured non-empty", len(not_measured) >= 5, True)
    check("not_measured all measured=false",
          all(item["measured"] is False for item in not_measured), True)
    check("not_measured metric names",
          "answer_level_keypoint_hit" in [i["metric"] for i in not_measured], True)
    check("not_measured live_stack",
          any(i["metric"] == "live_stack" and "live_check.py" in i["reason"]
              for i in not_measured), True)
    check("meta git hash kept", report["meta"]["git_short_hash"], "abc1234")
    check("meta cost basis placeholder", report["meta"]["cost_basis"], "estimated_placeholder_price")

    payload = json.dumps(report, ensure_ascii=False, default=_json_default)
    reloaded = json.loads(payload)
    check("json roundtrip keys", sorted(reloaded.keys()),
          ["cases", "meta", "not_measured", "summary"])

    table = format_summary_table(report)
    check("table has OVERALL", "OVERALL" in table, True)
    check("table has bucket hard", "hard" in table, True)
    check("table lists not_measured", "未实测项" in table, True)

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
