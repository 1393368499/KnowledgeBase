"""从**真实语料**采样，生成 live 评测集的造题脚本（10 条）。

两种模式
========

``--spec PATH``（推荐）
    读一份**本地**用例定义（JSON），逐条：
    1. 用 ``live_harness`` 打开真实库，取出目标文件的真实 chunk；
    2. 校验每个 ``key_point`` 确实**逐字**（空白归一化后）出现在目标文件里；
    3. 统计该要点在**整个语料**里被多少份文件包含（重复度，用来判断
       "这题是不是近重复兄弟文件"）；
    4. 找到包含首个要点的证据 chunk（记录 ordinal），生成评测集。

``--auto``（无 spec 时的缺省）
    纯规则抽样：按"有合同编号 / 有金额 / 有日期"的事实丰富度给文件排序，
    贪心挑 10 份事实互不相同的文件，套模板问句。质量一般，用于没有本地
    spec 时的可复现兜底。

为什么要有本地 spec
===================

真实语料里是客户合同：客户全名、合同金额都不能进仓库。所以：

- 本脚本**只含抽取规则与问句模板**，不含任何真实语料文本；
- 用例定义（query / key_points）放在**本地不提交目录**，与产出的 JSONL 同处；
- 产出的 JSONL 默认落到 ``<evals 的父目录>/dataset-live/kb-live-10.jsonl``，
  **绝不写进 evals/**（``--out`` 想指到 evals/ 下会被拒绝）。

顺带产出 ``doc-alias-map.json``（alias → file_id / 真实文件名）：这是**本地明细**
才需要的东西，仅用于把提交版报告和本地排查对上号，不进仓库。

用法::

    python evals/build_live_dataset.py --spec dataset-live/cases-spec.json
    python evals/build_live_dataset.py --auto
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
_EVALS_DIR = EVALS_DIR
PROJECT_PARENT = EVALS_DIR.parent  # evals/ 的父目录：允许写 dataset-live/ 的地方

if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

try:  # 以包方式导入（python -m evals.build_live_dataset）
    from evals.kb_eval.live_harness import (  # type: ignore[no-redef]
        DEFAULT_LIBRARY_CODE,
        bootstrap_live,
        doc_alias,
        extension_of,
    )
except ImportError:  # 直接跑脚本（python evals/build_live_dataset.py）
    from kb_eval.live_harness import (  # type: ignore[no-redef]
        DEFAULT_LIBRARY_CODE,
        bootstrap_live,
    )

logger = logging.getLogger("kb_eval.build_live_dataset")

DEFAULT_OUT = PROJECT_PARENT / "dataset-live" / "kb-live-10.jsonl"
DEFAULT_SPEC = PROJECT_PARENT / "dataset-live" / "cases-spec.json"
TARGET_CASE_COUNT = 10

#: 事实抽取规则（只抽"能被用户问出来的点"）
DOC_NO_RE = re.compile(
    r"(?:合同编号|合同编码|协议编号|项目编号|订单号)\s*[:：]?\s*"
    r"([A-Za-z0-9\u4e00-\u9fa5\[\]（）()\-－_/]{4,32})"
)
AMOUNT_RE = re.compile(
    r"(?:合同金额|合同总价|总价款|合同总价款|总计)\s*[:：]?\s*"
    r"(?:RMB|人民币)?\s*[¥￥]?\s*[:：]?\s*([0-9][0-9,\.]{2,})"
)
DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def normalize(value: str | None) -> str:
    """与 metrics.normalize_text 同口径：NFKC + 去空白 + 小写。

    这里自己实现一份，避免造题脚本和指标模块的导入顺序耦合（口径必须一致）。
    """
    import unicodedata

    if value is None:
        return ""
    return WS_RE.sub("", unicodedata.normalize("NFKC", str(value))).lower()


def _squash(text: str) -> str:
    return WS_RE.sub(" ", (text or "")).strip()


def extract_facts(text: str) -> dict[str, str]:
    """从一段真实正文里抽"可问事实"（编号 / 金额 / 日期）。"""
    facts: dict[str, str] = {}
    match = DOC_NO_RE.search(text)
    if match:
        facts["doc_no"] = match.group(1).strip()
    match = AMOUNT_RE.search(text)
    if match:
        facts["amount"] = match.group(1).strip()
    match = DATE_RE.search(text)
    if match:
        facts["date"] = _squash(match.group(0))
    return facts


def _fact_uniqueness(files: list[dict], texts: dict[int, list[str]]) -> dict[str, int]:
    """每个抽取出来事实值 → 全语料里出现过它的文件数（越小越"独"）。"""
    counts: dict[str, int] = {}
    for row in files:
        joined = normalize("\n".join(texts.get(row["file_id"], [])))
        for value in extract_facts("\n".join(texts.get(row["file_id"], []))).values():
            needle = normalize(value)
            if needle and needle in joined:
                counts[needle] = counts.get(needle, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 载入真实语料
# ---------------------------------------------------------------------------
def load_corpus(harness, library_code: str) -> tuple[list[dict], dict[int, list[dict]]]:
    """读出目标文件的 alias 与**全部** chunk（内存里用，不落盘）。"""
    files = harness.list_target_files(library_code=library_code)
    chunks: dict[int, list[dict]] = {}
    for row in files:
        chunks[row["file_id"]] = harness.chunks_of(row["file_id"])
    logger.info("corpus loaded: %d files, %d chunks", len(files), sum(len(v) for v in chunks.values()))
    return files, chunks


# ---------------------------------------------------------------------------
# 模式一：本地 spec（curated）
# ---------------------------------------------------------------------------
def build_from_spec(harness, spec: dict, files: list[dict], chunks: dict[int, list[dict]]) -> list[dict]:
    by_id = {row["file_id"]: row for row in files}
    # 全语料归一化文本：算要点重复度
    corpus_norm = {
        row["file_id"]: normalize("\n".join(ch["text"] for ch in chunks.get(row["file_id"], [])))
        for row in files
    }

    cases: list[dict] = []
    seen_ids: set[str] = set()
    for raw in spec.get("cases") or []:
        case_id = str(raw["id"]).strip()
        if case_id in seen_ids:
            raise ValueError(f"spec 里 id 重复：{case_id}")
        seen_ids.add(case_id)

        target_ids = [int(x) for x in (raw.get("target_file_ids") or [])]
        if not target_ids:
            raise ValueError(f"{case_id}: 缺 target_file_ids")
        for fid in target_ids:
            if fid not in by_id:
                raise ValueError(f"{case_id}: target file_id={fid} 不在目标文件清单里")

        key_points = [str(x).strip() for x in (raw.get("key_points") or []) if str(x).strip()]
        if not key_points:
            raise ValueError(f"{case_id}: 缺 key_points")

        # 校验：要点必须逐字出现在目标文件里（否则这题的要点命中率永远 0）
        missing: list[str] = []
        for point in key_points:
            needle = normalize(point)
            if not any(needle in corpus_norm[fid] for fid in target_ids):
                missing.append(point)
        if missing:
            raise ValueError(f"{case_id}: key_points 在目标文件里找不到：{missing}")

        # 重复度：有多少份文件包含该要点（1 = 只有目标文件，说明这题靠内容就能定位）
        dup_counts = {
            point: sum(1 for text in corpus_norm.values() if normalize(point) in text)
            for point in key_points
        }

        # 证据 chunk：目标文件里第一个包含首个要点的分段（本地明细用来复核）
        evidence = None
        for fid in target_ids:
            for ch in chunks[fid]:
                if normalize(key_points[0]) in normalize(ch["text"]):
                    evidence = {"file_id": fid, "ordinal": ch["ordinal"], "page": ch["page"]}
                    break
            if evidence:
                break

        cases.append(
            {
                "id": case_id,
                "bucket": str(raw.get("bucket") or "未分类"),
                "query": str(raw["query"]).strip(),
                "ground_truth_doc_ids": [by_id[fid]["doc_alias"] for fid in target_ids],
                "target_file_id": target_ids[0],
                "target_file_ids": target_ids,
                "key_points": key_points,
                "notes": str(raw.get("notes") or "").strip(),
                "keypoint_doc_counts": dup_counts,
                "evidence": evidence,
            }
        )
    return cases


# ---------------------------------------------------------------------------
# 模式二：自动抽样（兜底，无本地 spec 时用）
# ---------------------------------------------------------------------------
def _auto_pick(files: list[dict], chunks: dict[int, list[dict]]) -> list[dict]:
    """按事实丰富度 + 事实互不相同，贪心挑 10 份文件。"""
    candidates: list[dict] = []
    for row in files:
        fid = row["file_id"]
        text = "\n".join(ch["text"] for ch in chunks.get(fid, []))
        if len(text.strip()) < 200:
            continue
        facts = extract_facts(text)
        if not facts:
            continue
        score = len(facts) + (1 if row["chunk_count"] >= 10 else 0)
        candidates.append({"file_id": fid, "facts": facts, "score": score, "row": row})
    candidates.sort(key=lambda item: (-item["score"], item["file_id"]))

    picked: list[dict] = []
    used_values: set[str] = set()
    used_aliases: set[str] = set()
    for cand in candidates:
        alias = cand["row"]["doc_alias"]
        values = {normalize(v) for v in cand["facts"].values()}
        if alias in used_aliases:
            continue
        if values & used_values:  # 事实与已选文件重复 → 换个"独"一点的
            continue
        picked.append(cand)
        used_values |= values
        used_aliases.add(alias)
        if len(picked) >= TARGET_CASE_COUNT:
            break
    if len(picked) < TARGET_CASE_COUNT:  # 放宽：允许事实重复，凑够 10 条
        for cand in candidates:
            if len(picked) >= TARGET_CASE_COUNT:
                break
            if cand["row"]["doc_alias"] in used_aliases:
                continue
            picked.append(cand)
            used_aliases.add(cand["row"]["doc_alias"])
    return picked


def build_auto(harness, files: list[dict], chunks: dict[int, list[dict]]) -> list[dict]:
    cases: list[dict] = []
    for index, cand in enumerate(_auto_pick(files, chunks), start=1):
        fid = cand["file_id"]
        facts = cand["facts"]
        alias = cand["row"]["doc_alias"]
        if "doc_no" in facts and "amount" in facts:
            query = f"{facts['doc_no']} 那份合同的合同金额是多少？"
            key_points = [facts["doc_no"], facts["amount"]]
            bucket = "编号+金额"
        elif "doc_no" in facts:
            query = f"{facts['doc_no']} 那份合同的签订日期是哪天？"
            key_points = [facts["doc_no"]] + ([facts["date"]] if "date" in facts else [])
            bucket = "编号"
        elif "amount" in facts:
            query = f"合同金额是 {facts['amount']} 元的那份攻防演练服务合同，服务内容是什么？"
            key_points = [facts["amount"]]
            bucket = "金额"
        else:
            query = f"签订日期是 {facts.get('date', '')} 的那份合同的合同编号是什么？"
            key_points = [facts.get("date", "")]
            bucket = "日期"
        cases.append(
            {
                "id": f"L{index:02d}",
                "bucket": bucket,
                "query": query,
                "ground_truth_doc_ids": [alias],
                "target_file_id": fid,
                "target_file_ids": [fid],
                "key_points": [p for p in key_points if p],
                "notes": f"自动抽样：{bucket}（证据 chunk 见本地明细）",
                "keypoint_doc_counts": {},
                "evidence": None,
            }
        )
    # 自动模式也做一次要点存在性校验（不达标就报错，不静默出脏题）
    for case in cases:
        text = normalize("\n".join(ch["text"] for ch in chunks.get(case["target_file_id"], [])))
        for point in case["key_points"]:
            if normalize(point) not in text:
                raise ValueError(f"{case['id']}: 自动抽取的要点 {point!r} 不在目标文件里，请改用 --spec")
    return cases


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def _assert_outside_evals(out_path: Path) -> None:
    resolved = out_path.resolve()
    evals_root = EVALS_DIR.resolve()
    if resolved == evals_root or evals_root in resolved.parents:
        raise SystemExit(
            f"拒绝写入 evals/：{resolved}\n"
            "真实语料的评测集（含要点原文）必须落在本地不提交目录，"
            f"默认 {DEFAULT_OUT}。"
        )


def write_outputs(harness, out_path: Path, cases: list[dict], files: list[dict], meta: dict) -> None:
    _assert_outside_evals(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) 评测集（本地；字段按约定，另加 bucket 便于分桶统计）
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    # 2) alias → file_id / 真实文件名（**本地明细**用；绝不能被提交）
    harness.export_alias_map(out_path.parent / "doc-alias-map.json")

    # 3) 造题自检报告（本地）：要点重复度 + 证据 chunk
    report = {
        "meta": meta,
        "case_count": len(cases),
        "buckets": _count_by(cases, "bucket"),
        "cases": [
            {
                "id": case["id"],
                "bucket": case["bucket"],
                "target_file_id": case["target_file_id"],
                "target_doc_aliases": case["ground_truth_doc_ids"],
                "keypoint_doc_counts": case.get("keypoint_doc_counts") or {},
                "evidence": case.get("evidence"),
            }
            for case in cases
        ],
    }
    (out_path.parent / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("wrote %s (%d cases)", out_path, len(cases))


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "")
        out[value] = out.get(value, 0) + 1
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从真实语料造 10 条 live 评测集（本地落盘）")
    parser.add_argument("--repo", default=os.environ.get("KB_REPO_ROOT") or str(_EVALS_DIR.parent),
                        help="源码根（含 config/settings.py）")
    parser.add_argument("--run-root", default="artifacts/eval-runs/dataset-build")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--library-code", default=DEFAULT_LIBRARY_CODE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--spec", default=None, help="本地用例定义 JSON（推荐）")
    parser.add_argument("--auto", action="store_true", help="无 spec 时的规则抽样模式")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    out_path = Path(args.out)
    if not args.auto and not args.spec:
        args.spec = str(DEFAULT_SPEC) if DEFAULT_SPEC.is_file() else None
        if args.spec is None:
            args.auto = True
            logger.warning("没有 --spec 也没有本地 %s，退回 --auto 规则抽样", DEFAULT_SPEC)

    harness = bootstrap_live(
        args.repo, args.run_root, env_file=args.env_file, library_code=args.library_code
    )
    try:
        files, chunks = load_corpus(harness, args.library_code)
        if args.auto:
            cases = build_auto(harness, files, chunks)
        else:
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8-sig"))
            cases = build_from_spec(harness, spec, files, chunks)
        if len(cases) != TARGET_CASE_COUNT:
            raise SystemExit(f"用例数必须是 {TARGET_CASE_COUNT}，实际 {len(cases)}")
        meta = {
            "library": harness.library_info(),
            "mode": "auto" if args.auto else "spec",
            "spec_name": Path(args.spec).name if args.spec else "",
            "generated_from": "real live stack (PostgreSQL + Qdrant + AS711)",
            "case_count": len(cases),
        }
        write_outputs(harness, out_path, cases, files, meta)
        print(f"OK: {len(cases)} cases -> {out_path}")
        print("buckets:", json.dumps(_count_by(cases, "bucket"), ensure_ascii=False))
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
