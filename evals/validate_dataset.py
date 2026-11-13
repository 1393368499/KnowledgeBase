#!/usr/bin/env python
"""kb-50 评测集程序化校验。

校验项：
  1. 数据集恰好 50 条，bucket 计数严格 20 / 20 / 10；
  2. id 非空且唯一；
  3. query 非空且互不重复；
  4. ground_truth_doc_ids 非空、均为 corpus 下真实存在的 doc-key；
  5. key_points 非空，且每个要点在「正确文档」正文中逐字出现（忽略空白与换行差异）；
  6. 语料侧：corpus 下 20 篇 .md，文件名（doc-key）合法、且每篇至少被一条用例当作真值引用。

退出码：全部通过 0；存在问题 1（逐条打印到 stderr）。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVALS_DIR / "corpus"
DATASET_PATH = EVALS_DIR / "datasets" / "kb-50.jsonl"

EXPECTED_TOTAL = 50
EXPECTED_BUCKETS = {"typical": 20, "hard": 20, "pitfall": 10}
REQUIRED_FIELDS = ("id", "bucket", "query", "ground_truth_doc_ids", "key_points", "notes")
DOC_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# 空白归一化：所有空白（含全角空格、不换行空格、零宽字符）直接删除。
WS_RE = re.compile(r"[\s\u00a0\u3000\u200b\u200c\u200d\u2060\ufeff]+")

JUDGEMENT_MARKERS = ("说明了", "说明", "为什么", "是因为", "体现了", "反映出", "需要注意")


def strip_ws(text: str) -> str:
    return WS_RE.sub("", text)


def short(text: str, limit: int = 48) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_corpus() -> dict[str, str]:
    """doc-key -> 正文（未归一化）。"""
    docs: dict[str, str] = {}
    if not CORPUS_DIR.is_dir():
        return docs
    for path in sorted(CORPUS_DIR.glob("*.md")):
        docs[path.stem] = path.read_text(encoding="utf-8")
    return docs


def load_records(problems: list[str]) -> list[dict]:
    if not DATASET_PATH.is_file():
        problems.append(f"[数据集] 找不到 {DATASET_PATH}")
        return []
    raw = DATASET_PATH.read_text(encoding="utf-8")
    lines = raw.splitlines()
    records: list[dict] = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            problems.append(f"[数据集] 第 {lineno} 行为空行，jsonl 每行必须是完整记录")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"[数据集] 第 {lineno} 行不是合法 JSON：{exc}")
            continue
        if not isinstance(obj, dict):
            problems.append(f"[数据集] 第 {lineno} 行不是 JSON 对象")
            continue
        obj["__line__"] = lineno
        records.append(obj)
    return records


def validate_record(rec: dict, docs: dict[str, str], problems: list[str], warnings: list[str]) -> None:
    line = rec.get("__line__", "?")
    rid = rec.get("id")
    label = f"第 {line} 行 id={rid}"

    missing = [field for field in REQUIRED_FIELDS if field not in rec]
    if missing:
        problems.append(f"[字段] {label} 缺少字段：{', '.join(missing)}")
        return

    if not isinstance(rid, str) or not rid.strip():
        problems.append(f"[id] {label} id 必须是非空字符串")
    bucket = rec["bucket"]
    if bucket not in EXPECTED_BUCKETS:
        problems.append(f"[bucket] {label} bucket={bucket!r} 不在 {sorted(EXPECTED_BUCKETS)} 内")

    query = rec["query"]
    if not isinstance(query, str) or not query.strip():
        problems.append(f"[query] {label} query 必须是非空字符串")

    notes = rec["notes"]
    if not isinstance(notes, str) or not notes.strip():
        problems.append(f"[notes] {label} notes 必须是非空字符串")

    gt = rec["ground_truth_doc_ids"]
    if not isinstance(gt, list) or not gt:
        problems.append(f"[gt] {label} ground_truth_doc_ids 必须是非空列表")
        gt = []
    else:
        seen: set[str] = set()
        for doc_id in gt:
            if not isinstance(doc_id, str) or not doc_id.strip():
                problems.append(f"[gt] {label} ground_truth_doc_ids 含非法元素 {doc_id!r}")
                continue
            if doc_id in seen:
                problems.append(f"[gt] {label} ground_truth_doc_ids 重复引用 {doc_id}")
                continue
            seen.add(doc_id)
            if doc_id not in docs:
                problems.append(f"[gt] {label} 真值文档 {doc_id!r} 在 corpus/ 下不存在")

    key_points = rec["key_points"]
    if not isinstance(key_points, list) or not key_points:
        problems.append(f"[key_points] {label} key_points 必须是非空列表")
        return

    gt_norm = [strip_ws(docs[doc_id]) for doc_id in gt if doc_id in docs]
    for kp in key_points:
        if not isinstance(kp, str) or not kp.strip():
            problems.append(f"[key_points] {label} 含空要点")
            continue
        kp_norm = strip_ws(kp)
        if not kp_norm:
            problems.append(f"[key_points] {label} 要点 {kp!r} 归一化后为空")
            continue
        if len(kp_norm) > 40:
            warnings.append(f"[要点偏长] {label} {kp!r} 共 {len(kp_norm)} 字，建议 4~20 字")
        if any(marker in kp for marker in JUDGEMENT_MARKERS):
            warnings.append(f"[判断句] {label} {kp!r} 像判断句，建议改成可逐字命中的事实串")
        if not gt_norm:
            continue
        if not any(kp_norm in body for body in gt_norm):
            problems.append(
                f"[key_point 未命中] {label} 要点 {kp!r} 在真值文档 {gt} 正文中找不到（已忽略空白差异）"
            )


def validate_corpus(docs: dict[str, str], referenced: set[str], problems: list[str], warnings: list[str]) -> None:
    if len(docs) != 20:
        problems.append(f"[corpus] corpus/ 下应有 20 篇 .md，实际 {len(docs)} 篇")
    for doc_key, text in docs.items():
        if not DOC_KEY_RE.match(doc_key):
            problems.append(f"[corpus] 文档名 {doc_key!r} 不符合小写英文+连字符规范")
        if not strip_ws(text):
            problems.append(f"[corpus] 文档 {doc_key} 正文为空")
    for doc_key in sorted(referenced - set(docs)):
        problems.append(f"[corpus] 用例引用了不存在的文档 {doc_key}")
    unused = sorted(set(docs) - referenced)
    if unused:
        warnings.append(f"[未引用] 以下文档没有作为任何用例的真值：{', '.join(unused)}")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    problems: list[str] = []
    warnings: list[str] = []

    docs = load_corpus()
    records = load_records(problems)

    if len(records) != EXPECTED_TOTAL:
        problems.append(f"[总数] 数据集应有 {EXPECTED_TOTAL} 条，实际 {len(records)} 条")

    bucket_count = Counter(rec.get("bucket") for rec in records)
    for bucket, expected in EXPECTED_BUCKETS.items():
        actual = bucket_count.get(bucket, 0)
        if actual != expected:
            problems.append(f"[bucket 计数] {bucket} 应有 {expected} 条，实际 {actual} 条")
    for bucket in sorted((k for k in bucket_count if k not in EXPECTED_BUCKETS), key=str):
        problems.append(f"[bucket 计数] 出现未知 bucket {bucket!r}")

    ids = [rec.get("id") for rec in records if isinstance(rec.get("id"), str)]
    for rid, times in Counter(ids).items():
        if times > 1:
            problems.append(f"[id 重复] id={rid} 出现 {times} 次")
    idset = set(ids)
    expected_prefix = {"typical": "T", "hard": "H", "pitfall": "P"}
    for bucket, prefix in expected_prefix.items():
        for index in range(1, EXPECTED_BUCKETS[bucket] + 1):
            candidate = f"{prefix}{index:02d}"
            if candidate not in idset:
                warnings.append(f"[编号缺口] {bucket} 期望的 id {candidate} 未出现")

    queries = [(rec.get("id"), rec["query"]) for rec in records if isinstance(rec.get("query"), str)]
    for query, times in Counter(q.strip() for _, q in queries).items():
        if times > 1:
            dup_ids = [rid for rid, q in queries if q.strip() == query]
            problems.append(f"[query 重复] {query!r} 出现 {times} 次（{', '.join(map(str, dup_ids))}）")

    # typical 不应直接把文档标题当 query
    titles: set[str] = set()
    for text in docs.values():
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                titles.add(strip_ws(line.lstrip("#").strip()))
    for rec in records:
        if rec.get("bucket") != "typical":
            continue
        if strip_ws(rec.get("query", "")) in titles:
            warnings.append(f"[标题当 query] id={rec.get('id')} query 与某文档标题完全一致")

    for rec in records:
        validate_record(rec, docs, problems, warnings)

    referenced = {doc_id for rec in records for doc_id in rec.get("ground_truth_doc_ids", []) if isinstance(doc_id, str)}
    validate_corpus(docs, referenced, problems, warnings)

    if problems:
        print(f"校验失败：共 {len(problems)} 个问题", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        if warnings:
            print(f"\n另有 {len(warnings)} 条提示：", file=sys.stderr)
            for warning in warnings:
                print(f"  - {warning}", file=sys.stderr)
        return 1

    print(f"校验通过：{DATASET_PATH.name} 共 {len(records)} 条用例")
    for bucket in ("typical", "hard", "pitfall"):
        print(f"  - {bucket:<8} {bucket_count.get(bucket, 0):>2} 条")
    print(f"语料：corpus/ 下 {len(docs)} 篇 .md，全部被至少一条用例引用为真值")
    total_points = sum(len(rec.get("key_points", [])) for rec in records)
    multi_gt = sum(1 for rec in records if len(rec.get("ground_truth_doc_ids", [])) > 1)
    print(f"要点：共 {total_points} 个 key_point，全部在真值文档正文中逐字命中（忽略空白差异）")
    print(f"多真值用例：{multi_gt} 条；唯一 id：{len(idset)} 个；唯一 query：{len({q.strip() for _, q in queries})} 个")
    if warnings:
        print(f"\n提示 {len(warnings)} 条（不影响退出码）：")
        for warning in warnings:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
