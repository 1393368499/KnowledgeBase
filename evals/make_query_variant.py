#!/usr/bin/env python
"""评测集输入变体生成器：造"同一批题、不同输入形态"的对照集。

为什么需要它：中文检索的词法路（DB `icontains` + Qdrant `MatchText`）表现高度依赖
query 里有没有空格/标点。人工写的评测集为了"可控"往往带空格，而真实用户输入是
连续中文。两个对照能直接量出这个差异（BASELINE.md 表 A vs 表 C）。

用法（结果写到任意位置，不会覆盖 evals/datasets/ 里的正式集）::

    # 去空格 = 模拟真实用户连续输入
    python evals/make_query_variant.py --in evals/datasets/kb-50.jsonl \
        --out artifacts/kb-50-natural.jsonl

    # 显式加空格 = 给词法路"最友好"的输入（每个中文片段一个空格）
    python evals/make_query_variant.py --in evals/datasets/kb-50.jsonl \
        --out artifacts/kb-50-spaced.jsonl --mode split

    # 只改前 10 条，便于小样本排查
    python evals/make_query_variant.py --in evals/datasets/kb-50.jsonl \
        --out artifacts/kb-50-natural-10.jsonl --limit 10

只依赖标准库；不读写数据集以外的文件。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 可以切开的字符块：连续的中文片段 / 连续 ASCII 片段（编号、数字、英文词）
_CHUNK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9][A-Za-z0-9._-]*")

#: 需要从 query 里压掉的空白（全角空格一并处理，避免"看着没空格其实是全角"）
_SPACE = re.compile(r"[\s\u3000]+")


def strip_spaces(query: str) -> str:
    """去掉 query 内所有空白（首尾也去掉）。"""
    return _SPACE.sub("", query).strip()


def split_chunks(query: str) -> str:
    """把 query 切成中文片段/ASCII 片段并用单空格连接。"""
    chunks = _CHUNK.findall(_SPACE.sub(" ", query))
    return " ".join(chunks).strip()


MODES = {
    "strip": ("去空格（真实用户连续输入）", strip_spaces),
    "split": ("切成中文/ASCII 片段（对词法路最友好）", split_chunks),
}


def build_rows(rows: list[dict], mode: str) -> tuple[list[dict], int]:
    transform = MODES[mode][1]
    out: list[dict] = []
    changed = 0
    for row in rows:
        query = str(row.get("query") or "")
        variant = transform(query)
        if variant != query:
            changed += 1
        item = dict(row)
        item["query"] = variant
        item["query_original"] = query
        notes = str(item.get("notes") or "").strip()
        item["notes"] = f"{notes} [variant:{mode}]".strip()
        out.append(item)
    return out, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成评测集输入变体（去空格 / 加空格），用于词法敏感性对照",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--in", dest="src", required=True, help="源数据集 jsonl")
    parser.add_argument("--out", dest="dst", required=True, help="输出 jsonl（建议放在 artifacts/）")
    parser.add_argument("--mode", choices=sorted(MODES), default="strip",
                        help="变体模式：" + "；".join(f"{k}={v[0]}" for k, v in MODES.items()))
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = parser.parse_args(argv)

    src = Path(args.src).expanduser()
    if not src.is_file():
        print(f"[错误] 数据集不存在：{src}", file=sys.stderr)
        return 2
    try:
        rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        print(f"[错误] 数据集不是合法 jsonl：{exc}", file=sys.stderr)
        return 3
    if not rows:
        print(f"[错误] 数据集是空的：{src}", file=sys.stderr)
        return 3

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    variants, changed = build_rows(rows, args.mode)
    dst = Path(args.dst).expanduser()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="\n") as handle:
        for item in variants:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"[变体] {MODES[args.mode][0]}：{len(variants)} 条，其中 {changed} 条 query 被改写")
    print(f"[输出] {dst}")
    print(f"[提示] 用 python evals/run_eval.py --dataset {dst} 跑对照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
