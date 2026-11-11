"""在**真实栈**上跑 live 评测集，产出可提交的脱敏结果 + 本地全量明细。

真实栈 = 真 PostgreSQL（real DB） + 真 Qdrant collection + 真 AS711 模型服务
（bge-large-zh-v1.5 embed / rerank）。语料是真的客户合同，所以这个脚本有两层
输出，绝不含糊：

提交版（``evals/results/live-<UTC>.json``，可以进仓库）
    每条 case 只有 ``{id, bucket, recall5, mrr, keypoint_rate, elapsed_ms,
    ranked_doc_aliases[:5], notes}``；文档一律用 ``doc-<sha8>`` alias，
    **没有原始文件名、没有客户名、没有金额原文**。

本地明细（``<dataset-live>/live-10-detail[-rerank].json``，不进仓库）
    带上 ``file_id``、``original_name``、``query``、命中 chunk 摘要，用来人工
    复核对错。

指标口径直接 import 离线模块 ``kb_eval.metrics``（纯函数，只读不改），保证
live 与离线数字可比：

- Recall@5：文档级去重后取前 5 个文档，命中任一真值记 1，对 10 条取平均；
- MRR@5：首个命中真值的**文档级名次**的倒数；
- 要点命中率：top-5 chunk 文本拼接 → NFKC + 删空白 + 小写 → 子串匹配；
- P50/P95：线性插值百分位（``metrics.percentile``）。

用法::

    python evals/live_check.py                     # dense + RRF，提交版结果
    python evals/live_check.py --rerank            # 再叠一层真实 rerank
    python evals/live_check.py --limit 10 --out evals/results/live-manual.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
_EVALS_DIR = EVALS_DIR
PROJECT_PARENT = EVALS_DIR.parent

if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

try:  # python -m evals.live_check
    from evals.kb_eval import metrics  # type: ignore[no-redef]
    from evals.kb_eval.live_harness import (  # type: ignore[no-redef]
        DEFAULT_LIBRARY_CODE,
        bootstrap_live,
    )
except ImportError:  # python evals/live_check.py
    from kb_eval import metrics  # type: ignore[no-redef]
    from kb_eval.live_harness import (  # type: ignore[no-redef]
        DEFAULT_LIBRARY_CODE,
        bootstrap_live,
    )

logger = logging.getLogger("kb_eval.live_check")

DEFAULT_DATASET = PROJECT_PARENT / "dataset-live" / "kb-live-10.jsonl"
DEFAULT_OUT_DIR = EVALS_DIR / "results"
DEFAULT_LOCAL_DIR = PROJECT_PARENT / "dataset-live"

METRIC_K = 5  # 指标口径固定 @5（与离线一致），不跟 --limit 走
MAX_LIMIT = 20  # 控制成本：一次检索最多返回 20 条

#: 提交版里 notes 的兜底脱敏：金额/单价这类数字不能进仓库（客户合同信息）。
_MONEY_RES: tuple[Any, ...] = (
    re.compile(r"[¥￥]\s*\d[\d,.]*"),
    re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"),
    re.compile(r"\d+\.\d{2}"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:万元|元)"),
    re.compile(r"[×xX*]\s*\d[\d,]*"),
)


def redact_notes(text: Any) -> str:
    """把 notes 里的金额/单价替换成占位符（notes 要进提交版 JSON）。"""
    out = str(text or "")
    for pattern in _MONEY_RES:
        out = pattern.sub("某金额", out)
    return out


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_dataset(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(
            f"找不到 live 评测集：{path}\n"
            "先跑：python evals/build_live_dataset.py --spec dataset-live/cases-spec.json"
        )
    cases: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"评测集第 {line_no} 行不是合法 JSON：{exc}") from None
    if not cases:
        raise SystemExit(f"评测集是空的：{path}")
    return cases


def load_alias_map(path: Path) -> dict[str, dict]:
    """本地 alias → {file_id, original_name} 映射（只在写本地明细时用）。"""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("doc-alias-map.json 解析失败，本地明细里就只有 alias")
        return {}


# ---------------------------------------------------------------------------
# 单条 case
# ---------------------------------------------------------------------------
def build_scoring_results(results: list[dict]) -> list[dict]:
    """把 harness 结果转成 ``metrics`` 认识的形状（文档级别名走 ``doc_aliases``）。

    harness 只给 alias、不给 original_name；``metrics.result_doc_aliases`` 会读
    ``doc_aliases`` 字段，所以真值（alias）和结果能对上，同时保持"文档级去重"
    由 file_id 决定。
    """
    return [
        {
            "file_id": row.get("file_id"),
            "doc_aliases": [row.get("doc_alias")] if row.get("doc_alias") else [],
            "score": row.get("score"),
            "text": row.get("text") or "",
        }
        for row in results
    ]


def ranked_doc_aliases(results: list[dict], k: int = METRIC_K) -> list[str]:
    """文档级排名（同一 file_id 只留最靠前的 chunk），返回 alias 列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for row in results:
        key = str(row.get("file_id"))
        if key in seen:
            continue
        seen.add(key)
        alias = row.get("doc_alias") or ""
        if alias:
            out.append(alias)
        if len(out) >= k:
            break
    return out


def run_case(harness, case: dict, *, limit: int, use_rerank: bool) -> dict:
    """跑一条 case，返回 ``{"case": 提交版行, "detail": 本地明细行, "report": {...}}``。"""
    case_id = str(case.get("id"))
    query = str(case.get("query") or "")
    ground_truth = list(case.get("ground_truth_doc_ids") or [])
    key_points = list(case.get("key_points") or [])

    try:
        outcome = harness.search(query, limit=limit, use_rerank=use_rerank)
    except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整轮
        logger.error("case %s live search failed: %s", case_id, type(exc).__name__)
        detail_error = f"{type(exc).__name__}: {exc}"
        return {
            "case": {
                "id": case_id,
                "bucket": case.get("bucket") or "",
                "recall5": None,
                "mrr": None,
                "keypoint_rate": None,
                "elapsed_ms": None,
                "ranked_doc_aliases": [],
                "notes": redact_notes(case.get("notes")) + f" | ERROR {type(exc).__name__}",
            },
            "detail": {
                "id": case_id,
                "query": query,
                "status": "error",
                "error": detail_error,
                "traceback": traceback.format_exc(limit=3),
            },
            "report": {},
        }

    results = outcome.get("results") or []
    scored = metrics.score_ranking(
        build_scoring_results(results), ground_truth, k=METRIC_K
    )
    keypoint = metrics.key_point_hit_rate(
        metrics.join_context(results, k=METRIC_K), key_points
    )
    elapsed_ms = outcome.get("elapsed_ms")
    report = outcome.get("report") or {}

    submitted = {
        "id": case_id,
        "bucket": case.get("bucket") or "",
        "recall5": scored.get("recall"),
        "mrr": scored.get("mrr"),
        "keypoint_rate": keypoint.get("rate"),
        "elapsed_ms": None if elapsed_ms is None else round(float(elapsed_ms), 1),
        "ranked_doc_aliases": ranked_doc_aliases(results),
        "notes": redact_notes(case.get("notes")),
    }
    detail = {
        "id": case_id,
        "bucket": case.get("bucket") or "",
        "query": query,
        "status": "ok",
        "target_file_ids": case.get("target_file_ids") or [case.get("target_file_id")],
        "ground_truth_doc_ids": ground_truth,
        "key_points": key_points,
        "recall5": scored.get("recall"),
        "recall_all5": scored.get("recall_all"),
        "mrr": scored.get("mrr"),
        "first_hit_rank": scored.get("first_hit_rank"),
        "keypoint": keypoint,
        "elapsed_ms": submitted["elapsed_ms"],
        "report": report,
        "ranked_docs": [
            {
                "rank": index,
                "file_id": row.get("file_id"),
                "doc_alias": row.get("doc_alias"),
                "score": row.get("score"),
                "text_preview": (row.get("text") or "")[:400],
            }
            for index, row in enumerate(results, start=1)
        ],
    }
    return {"case": submitted, "detail": detail, "report": report}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(cases: list[dict], details: list[dict], *, limit: int, use_rerank: bool) -> dict:
    ok_details = [row for row in details if row.get("status") == "ok"]
    latencies = [row["elapsed_ms"] for row in ok_details if row.get("elapsed_ms") is not None]
    reports = [row.get("report") or {} for row in ok_details]

    def mean_of(key: str) -> float | None:
        values = [float(row[key]) for row in cases if row.get(key) is not None]
        return metrics.mean_or_none(values)

    def report_mean(key: str) -> float | None:
        values = [
            float(report[key])
            for report in reports
            if isinstance(report.get(key), (int, float))
        ]
        return round(sum(values) / len(values), 3) if values else None

    buckets: dict[str, dict[str, Any]] = {}
    for row in cases:
        bucket = row.get("bucket") or "未分类"
        slot = buckets.setdefault(
            bucket, {"cases": 0, "recall5": 0.0, "mrr": 0.0, "keypoint_rate": 0.0, "_kp": 0}
        )
        slot["cases"] += 1
        slot["recall5"] += float(row.get("recall5") or 0.0)
        slot["mrr"] += float(row.get("mrr") or 0.0)
        if row.get("keypoint_rate") is not None:
            slot["keypoint_rate"] += float(row["keypoint_rate"])
            slot["_kp"] += 1
    for slot in buckets.values():
        slot["recall5"] = round(slot["recall5"] / slot["cases"], 4)
        slot["mrr"] = round(slot["mrr"] / slot["cases"], 4)
        slot["keypoint_rate"] = (
            round(slot["keypoint_rate"] / slot["_kp"], 4) if slot["_kp"] else None
        )
        slot.pop("_kp", None)

    latency = metrics.latency_stats(latencies) if latencies else {}
    return {
        "cases": len(cases),
        "cases_ok": len(ok_details),
        "cases_error": len(details) - len(ok_details),
        "k": METRIC_K,
        "retrieval_limit": limit,
        "rerank": bool(use_rerank),
        "recall5": mean_of("recall5"),
        "recall_all5": metrics.mean_or_none(
            [row.get("recall_all5") for row in ok_details]
        ),
        "mrr": mean_of("mrr"),
        "keypoint_rate": mean_of("keypoint_rate"),
        "latency": {
            "p50_ms": latency.get("p50_ms"),
            "p95_ms": latency.get("p95_ms"),
            "mean_ms": latency.get("mean_ms"),
            "min_ms": latency.get("min_ms"),
            "max_ms": latency.get("max_ms"),
            "count": latency.get("count", 0),
        },
        "retrieval_report": {
            "dense_hits_mean": report_mean("dense_hits"),
            "db_keyword_hits_mean": report_mean("db_keyword_hits"),
            "sparse_hits_mean": report_mean("sparse_hits"),
            "fused_hits_mean": report_mean("fused_hits"),
            "degraded_cases": sum(1 for report in reports if report.get("degraded")),
            "rerank_applied_cases": sum(
                1 for report in reports if (report.get("rerank") or {}).get("applied")
            ),
        },
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enrich_detail(detail: dict, alias_map: dict[str, dict]) -> dict:
    """本地明细补上真实文件名（只写本地文件，不打印、不返回）。"""
    def name_of(alias: str | None) -> str:
        entry = alias_map.get(alias or "") or {}
        return str(entry.get("original_name") or "")

    for row in detail.get("ranked_docs") or []:
        row["original_name"] = name_of(row.get("doc_alias"))
    detail["ground_truth_original_names"] = [
        name_of(alias) for alias in detail.get("ground_truth_doc_ids") or []
    ]
    return detail


def print_summary(meta: dict, summary: dict, out_path: Path, detail_path: Path) -> None:
    print()
    print("=" * 78)
    print(f"live eval | stack={meta['stack']} | rerank={summary['rerank']}")
    print(f"library={meta['library'].get('library_code')} "
          f"files={meta['library'].get('target_file_count')} "
          f"collection={meta['library'].get('vector_collection')}")
    print("-" * 78)
    print(f"{'id':<4} {'bucket':<20} {'R@5':>4} {'MRR':>6} {'KP':>6} {'ms':>8}  top-3 aliases")
    for row in summary["_rows"]:
        recall = "-" if row["recall5"] is None else f"{row['recall5']:.2f}"
        mrr = row["mrr"] or 0.0
        kp = "-" if row["keypoint_rate"] is None else f"{row['keypoint_rate']:.2f}"
        ms = "-" if row["elapsed_ms"] is None else f"{row['elapsed_ms']:.0f}"
        top3 = " ".join(row["ranked_doc_aliases"][:3]) or "(none)"
        print(f"{row['id']:<4} {row['bucket']:<20} {recall:>4} {mrr:>6.3f} {kp:>6} {ms:>8}  {top3}")
    print("-" * 78)
    print(f"Recall@5 = {summary['recall5']}   (recall_all@5 = {summary['recall_all5']})")
    print(f"MRR      = {summary['mrr']}")
    print(f"要点命中率 = {summary['keypoint_rate']}")
    print(f"延迟 P50/P95 = {summary['latency']['p50_ms']} / {summary['latency']['p95_ms']} ms "
          f"(mean {summary['latency']['mean_ms']}, n={summary['latency']['count']})")
    print(f"检索报告 = {json.dumps(summary['retrieval_report'], ensure_ascii=False)}")
    print(f"提交版(脱敏) -> {out_path}")
    print(f"本地明细      -> {detail_path}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实栈 live 检索评测（默认 10 条语料）")
    parser.add_argument("--limit", type=int, default=10, help="每次检索返回条数（默认 10）")
    parser.add_argument("--rerank", action="store_true", help="开启真实 rerank（AS711 :8002）")
    parser.add_argument("--out", default=None, help="提交版 JSON 路径（默认 evals/results/live-<UTC>.json）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--repo", default=os.environ.get("KB_REPO_ROOT") or str(_EVALS_DIR.parent))
    parser.add_argument("--run-root", default="artifacts/eval-runs/live-check")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--library-code", default=DEFAULT_LIBRARY_CODE)
    parser.add_argument("--quiet", action="store_true", help="只打结果，不进来回日志")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    limit = max(1, min(int(args.limit), MAX_LIMIT))
    if limit != args.limit:
        logger.warning("--limit 被夹到 [1, %d]，实际 %d（成本约束）", MAX_LIMIT, limit)

    dataset_path = Path(args.dataset)
    cases = load_dataset(dataset_path)
    if len(cases) > 10:
        logger.warning("评测集有 %d 条，只跑前 10 条（成本约束）", len(cases))
        cases = cases[:10]

    harness = bootstrap_live(
        args.repo, args.run_root, env_file=args.env_file, library_code=args.library_code
    )
    submitted: list[dict] = []
    details: list[dict] = []
    try:
        for case in cases:
            outcome = run_case(harness, case, limit=limit, use_rerank=args.rerank)
            submitted.append(outcome["case"])
            details.append(outcome["detail"])
            logger.warning(
                "case %s done: R@5=%s MRR=%s elapsed=%sms",
                case.get("id"),
                outcome["case"]["recall5"],
                outcome["case"]["mrr"],
                outcome["case"]["elapsed_ms"],
            )
        meta = {
            "stack": "live: PostgreSQL(real) + Qdrant + AS711(bge-large-zh-v1.5, rerank)",
            "library": harness.library_info(),
            "dataset_cases": len(cases),
            "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "metric_definitions": {
                "recall5": "文档级去重后 top-5 命中任一真值记 1（多真值不摊薄），10 条取平均",
                "mrr": "首个命中真值的文档级名次倒数，未命中记 0",
                "keypoint_rate": "top-5 chunk 文本拼接后 NFKC+去空白+小写 子串匹配，逐条要点命中比例",
                "p50_p95": "线性插值百分位；10 条样本下 P95 偏向最大值",
            },
            "desensitization": "文档一律 doc-<sha256(original_name)[:8]> alias；不含原始文件名/客户名/金额原文",
        }
        summary = summarize(submitted, details, limit=limit, use_rerank=args.rerank)
        rows = list(submitted)
        summary_for_print = dict(summary)
        summary_for_print["_rows"] = rows

        out_path = Path(args.out) if args.out else (
            DEFAULT_OUT_DIR / f"live-{utc_stamp()}.json"
        )
        detail_path = DEFAULT_LOCAL_DIR / (
            "live-10-detail-rerank.json" if args.rerank else "live-10-detail.json"
        )

        alias_map = load_alias_map(DEFAULT_LOCAL_DIR / "doc-alias-map.json")
        write_json(
            out_path,
            {"meta": meta, "summary": summary, "cases": submitted},
        )
        write_json(
            detail_path,
            {
                "meta": {**meta, "dataset": str(dataset_path)},
                "summary": summary,
                "cases": [enrich_detail(row, alias_map) for row in details],
            },
        )
        print_summary(meta, summary_for_print, out_path, detail_path)
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
