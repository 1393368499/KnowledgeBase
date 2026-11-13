#!/usr/bin/env python3
"""KnowledgeBase AI 检索评测基线 —— 命令行驱动。

用法（默认 offline，无需 Docker / Postgres / 模型服务）::

    python evals/run_eval.py                     # 全量 50 条，写 evals/results/
    python evals/run_eval.py --limit 10 --dense
    python evals/run_eval.py --dataset evals/datasets/kb-50.jsonl --rerank
    python evals/run_eval.py --prices '{"input_per_m":0.5,"output_per_m":1.0}'

在线模式（``--online``）**故意不实现**：真实栈验证由 ``evals/live_check.py``
（live-verifier 维护）产出 ``evals/results/live-*.json``，本 CLI 只负责离线词法基线。

退出码：

- 0 正常
- 2 用法错误（参数/单价 JSON/online 缺少 api-base 等）
- 3 数据集非法（缺失、JSON 坏、字段不合法、为空）
- 4 依赖缺失（harness.py 未实现、Django 依赖、语料目录缺失、bootstrap 失败）
- 5 所有查询都报错（写出的报告里每一条都有 error）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence

EVAL_DIR = Path(__file__).resolve().parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from kb_eval import metrics as m  # noqa: E402
from kb_eval import report as r  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_DATASET = 3
EXIT_DEPENDENCY = 4
EXIT_QUERIES_FAILED = 5

K = 5
SEARCH_LIMIT = 10  # 多取一些给同文档重复 chunk 留余量，指标仍只看前 5 个文档
ALLOWED_BUCKETS = ("typical", "hard", "pitfall")
DEFAULT_DATASET = EVAL_DIR / "datasets" / "kb-50.jsonl"
DEFAULT_CORPUS = EVAL_DIR / "corpus"
DEFAULT_RESULTS = EVAL_DIR / "results"
FALLBACK_REPO_ROOT = EVAL_DIR.parent  # evals/ 的父目录（evals/ 放进仓库后即仓库根）


class DatasetError(RuntimeError):
    """数据集不合法；CLI 转成 EXIT_DATASET + 可操作提示。"""


def say(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", file=sys.stderr, flush=True)


def die(message: str, code: int) -> int:
    print(f"[错误] {message}", file=sys.stderr, flush=True)
    return code


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
def _as_scalar_ids(values: Any, where: str) -> list[str]:
    if not isinstance(values, list):
        raise DatasetError(f"{where} 的 ground_truth_doc_ids 必须是数组，收到 {type(values).__name__}")
    out: list[str] = []
    for item in values:
        if isinstance(item, (str, int)) and str(item).strip():
            out.append(str(item).strip())
        elif isinstance(item, float) and item.is_integer():
            out.append(str(int(item)))
        else:
            raise DatasetError(f"{where} 的 ground_truth_doc_ids 含非法元素：{item!r}")
    return out


def load_dataset(path: Path, limit: int | None = None) -> tuple[list[dict], int]:
    """读取并逐行校验 jsonl；返回 ``(cases, 原始行数)``。"""
    if not path.exists():
        raise DatasetError(f"数据集不存在：{path}（用 --dataset 指定路径）")
    if path.is_file() and path.stat().st_size == 0:
        raise DatasetError(f"数据集是空文件：{path}")

    cases: list[dict] = []
    seen_ids: set[str] = set()
    raw_lines = 0
    with open(path, "r", encoding="utf-8-sig") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw_lines += 1
            where = f"第 {lineno} 行"
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{where} 不是合法 JSON：{exc}") from None
            if not isinstance(obj, dict):
                raise DatasetError(f"{where} 必须是 JSON 对象，收到 {type(obj).__name__}")

            case_id = obj.get("id")
            if not isinstance(case_id, str) or not case_id.strip():
                raise DatasetError(f"{where} 缺少非空字符串 id")
            case_id = case_id.strip()
            if case_id in seen_ids:
                raise DatasetError(f"{where} id 重复：{case_id}")
            seen_ids.add(case_id)

            query = obj.get("query")
            if not isinstance(query, str) or not query.strip():
                raise DatasetError(f"{where}（id={case_id}）缺少非空字符串 query")

            bucket = obj.get("bucket")
            if bucket not in ALLOWED_BUCKETS:
                raise DatasetError(
                    f"{where}（id={case_id}）bucket={bucket!r} 非法；"
                    f"只允许 {list(ALLOWED_BUCKETS)}"
                )

            gt = _as_scalar_ids(obj.get("ground_truth_doc_ids"), where)
            if not gt:
                warn(f"{where}（id={case_id}）ground_truth_doc_ids 为空，该条不参与 Recall/MRR 平均")

            key_points_raw = obj.get("key_points") or []
            if not isinstance(key_points_raw, list):
                raise DatasetError(f"{where}（id={case_id}）key_points 必须是数组")
            key_points = [str(item) for item in key_points_raw if str(item).strip()]

            cases.append(
                {
                    "id": case_id,
                    "bucket": bucket,
                    "query": query.strip(),
                    "ground_truth_doc_ids": gt,
                    "key_points": key_points,
                    "notes": str(obj.get("notes") or ""),
                    "_dataset_line": lineno,
                }
            )

    if not cases:
        raise DatasetError(f"数据集没有任何有效用例：{path}")
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return cases, raw_lines


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def resolve_dataset(path_value: str | None) -> Path:
    if not path_value:
        return DEFAULT_DATASET
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    # 相对路径：先按当前工作目录（用户在仓库根敲命令最自然），再按脚本目录
    # （从其它 cwd 调脚本时仍能找到 evals/datasets/ 里的正式集）。
    cwd_candidate = candidate.resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    script_candidate = (EVAL_DIR / candidate).resolve()
    if script_candidate.exists():
        return script_candidate
    return cwd_candidate  # 交给调用方报「数据集不存在」，提示里会带上这个路径


def resolve_repo_root(cli_value: str | None, *, verbose: bool = False) -> Path | None:
    """--repo-root → env KB_REPO_ROOT → 向上找 manage.py → 离线镜像兜底。"""
    if cli_value:
        candidate = Path(cli_value).expanduser().resolve()
        if not candidate.is_dir():
            warn(f"--repo-root 指向的目录不存在：{candidate}")
            return None
        return candidate

    env_value = os.environ.get("KB_REPO_ROOT")
    if env_value:
        candidate = Path(env_value).expanduser().resolve()
        if candidate.is_dir():
            if verbose:
                say(f"[repo-root] 来自 KB_REPO_ROOT：{candidate}")
            return candidate
        warn(f"环境变量 KB_REPO_ROOT 指向的目录不存在：{candidate}，继续向下找 manage.py")

    for parent in [EVAL_DIR, *EVAL_DIR.parents]:
        if (parent / "manage.py").is_file():
            if verbose:
                say(f"[repo-root] 向上找到 manage.py：{parent}")
            return parent

    if FALLBACK_REPO_ROOT.is_dir():
        if verbose:
            say(f"[repo-root] 兜底用离线镜像：{FALLBACK_REPO_ROOT}")
        return FALLBACK_REPO_ROOT
    return None


def resolve_workdir(
    args: argparse.Namespace, run_id: str, repo_root: Path
) -> tuple[Path | None, bool, int]:
    """返回 ``(workdir, 是否由本进程负责清理)``。

    harness 契约要求 workdir **必须位于 repo_root 内**（knowledgebase/paths.py 的硬约束，
    ``bootstrap`` 会直接拒绝仓库外路径），所以默认放在
    ``<repo_root>/artifacts/evals/<run_id>``：每个 run 独立目录，天然避开 Qdrant
    embedded 的"同一目录只能有一个客户端"文件锁，跑完即删。显式 ``--workdir``
    一律保留（用户自己管），但同样必须位于 repo_root 内。

    返回 ``None`` 表示参数不合法（已打印可操作提示）。
    """
    if args.workdir:
        path = Path(args.workdir).expanduser().resolve()
    elif args.keep_workdir:
        path = (repo_root / "artifacts" / "evals" / f"keep-{run_id}").resolve()
    else:
        path = (repo_root / "artifacts" / "evals" / run_id).resolve()

    try:
        path.relative_to(repo_root)
    except ValueError:
        die(
            f"workdir 必须位于仓库根目录内：{path}\n"
            f"  repo_root={repo_root}（knowledgebase/paths.py 会拒绝项目根外的运行目录）",
            EXIT_USAGE,
        )
        return None, False, EXIT_USAGE

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die(
            f"创建 workdir 失败：{path}（{type(exc).__name__}: {exc}）\n"
            f"  提示：repo_root 需要可写；只读仓库请用 --repo-root 指向可写镜像，"
            f"或用 --workdir 指定仓库内可写目录。",
            EXIT_DEPENDENCY,
        )
        return None, False, EXIT_DEPENDENCY

    return path, (args.workdir is None and not args.keep_workdir), EXIT_OK


# ---------------------------------------------------------------------------
# harness 懒导入
# ---------------------------------------------------------------------------
def import_harness(*, verbose: bool = False):
    try:
        from kb_eval.harness import bootstrap  # noqa: WPS433
    except ModuleNotFoundError as exc:
        name = exc.name or ""
        if name == "kb_eval.harness" or name.startswith("kb_eval.harness."):
            die(
                "缺少 evals/kb_eval/harness.py（评测夹具，由 harness-engineer 交付）。\n"
                "  请先实现它，契约见 task-3：\n"
                "    from kb_eval.harness import bootstrap\n"
                "    h = bootstrap(repo_root=Path, workdir=Path, dense=bool, rerank=bool)\n"
                "    h.seed_corpus(corpus_dir) -> {doc_key: file_id}\n"
                "    h.search(query, limit=10) -> {results, report, elapsed_ms, warnings}\n"
                "    h.close()",
                EXIT_DEPENDENCY,
            )
        else:
            die(
                f"harness 的依赖模块缺失：{name}。\n"
                f"  请用项目 venv 运行：{sys.executable}\n"
                f"  并确认该依赖已安装（requirements.txt）。",
                EXIT_DEPENDENCY,
            )
        return None
    except ImportError as exc:  # 语法/循环导入等
        die(f"导入 evals/kb_eval/harness.py 失败：{exc}", EXIT_DEPENDENCY)
        return None
    return bootstrap


# ---------------------------------------------------------------------------
# 单条查询
# ---------------------------------------------------------------------------
def _elapsed_fields(raw: Mapping[str, Any], wall_ms: float) -> dict:
    harness_elapsed = raw.get("elapsed_ms")
    if isinstance(harness_elapsed, (int, float)) and not isinstance(harness_elapsed, bool):
        elapsed = round(float(harness_elapsed), 3)
        source = raw.get("elapsed_source") or "harness"
    else:
        elapsed = round(wall_ms, 3)
        source = "wall"
    delta_ratio = None
    if elapsed and elapsed > 0:
        delta_ratio = round(abs(wall_ms - elapsed) / elapsed, 4)
    return {
        "elapsed_ms": elapsed,
        "elapsed_source": source,
        "elapsed_delta_ratio": delta_ratio,
    }


def _token_counts(case: Mapping[str, Any], results: Sequence[Mapping[str, Any]],
                  *, rerank: bool, llm: bool = False) -> dict:
    """确定性 token 估算（口径见 metrics 模块 docstring）。"""
    query = case["query"]
    query_tokens = m.estimate_tokens(query)
    context_tokens = m.estimate_tokens_many(str(item.get("text") or "") for item in results[:K])
    embed_tokens = query_tokens
    rerank_tokens = (query_tokens + context_tokens) if rerank else 0
    return {
        "embed_tokens": embed_tokens,
        "rerank_tokens": rerank_tokens,
        "llm_tokens": 0 if not llm else 0,  # 离线无 LLM；在线模式同样未接入生成
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "context_tokens": context_tokens,
    }


def build_case(
    case: Mapping[str, Any],
    raw: Mapping[str, Any] | None,
    wall_ms: float,
    *,
    doc_id_map: Mapping[Any, Any] | None,
    prices: Mapping[str, Any],
    rerank: bool,
    error: str | None = None,
) -> dict:
    """把一次检索的原始返回加工成落盘 case。``raw=None`` 表示查询失败。"""
    base = {
        "id": case["id"],
        "bucket": case["bucket"],
        "query": case["query"],
        "notes": case.get("notes", ""),
        "ground_truth_doc_ids": list(case["ground_truth_doc_ids"]),
        "key_points_expected": list(case.get("key_points") or []),
        "limit": SEARCH_LIMIT,
        "k": K,
        "status": "ok",
        "error": error,
        "warnings": [],
        "result_count": 0,
        "doc_level_count": 0,
        "recall_at_5": None,
        "recall_all_at_5": None,
        "mrr_at_5": None,
        "first_hit_rank": None,
        "matched_ground_truth": [],
        "ranked_docs": [],
        "top_docs": [],
        "key_points": {"hit": 0, "total": 0, "rate": None, "hit_points": [], "missing": []},
        "answer_key_points": {
            "measured": False, "hit": None, "total": None, "rate": None,
            "reason": "离线无 LLM 生成，answer 级要点命中未实测",
        },
        "tokens": {},
        "cost": {},
    }
    if raw is None:
        base["status"] = "error"
        base.update({"elapsed_ms": None, "elapsed_source": None, "elapsed_delta_ratio": None})
        base["wall_ms"] = round(wall_ms, 3)
        base["tokens"] = {
            "embed_tokens": 0, "rerank_tokens": 0, "llm_tokens": 0,
            "llm_input_tokens": 0, "llm_output_tokens": 0, "context_tokens": 0,
        }
        base["cost"] = m.cost_estimate({}, prices)
        return base

    results = raw.get("results") or []
    if not isinstance(results, list):
        results = []
        base["warnings"].append("harness 返回的 results 不是列表，按空结果处理")

    scored = m.score_ranking(results, case["ground_truth_doc_ids"], doc_id_map=doc_id_map, k=K)
    context_text = m.join_context(results, K)
    key_points = m.key_point_hit_rate(context_text, case.get("key_points") or [])
    tokens = _token_counts(case, results, rerank=rerank)

    top_docs = []
    for row in m.dedupe_by_doc(results)[:K]:
        top_docs.append(
            {
                "rank": row["_rank"],
                "file_id": row.get("file_id"),
                "doc_id": row.get("doc_id"),
                "original_name": row.get("original_name"),
                "score": row.get("score"),
            }
        )

    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]

    base.update(
        {
            "status": "ok",
            "warnings": [str(item) for item in warnings],
            "result_count": len(results),
            "doc_level_count": len(m.dedupe_by_doc(results)),
            "recall_at_5": scored["recall"],
            "recall_all_at_5": scored["recall_all"],
            "mrr_at_5": scored["mrr"],
            "first_hit_rank": scored["first_hit_rank"],
            "matched_ground_truth": scored["matched_ground_truth"],
            "ranked_docs": scored["ranked_docs"],
            "top_docs": top_docs,
            "key_points": key_points,
            "tokens": tokens,
            "cost": m.cost_estimate(tokens, prices),
        }
    )
    base.update(_elapsed_fields(raw, wall_ms))
    base["wall_ms"] = round(wall_ms, 3)
    return base


# ---------------------------------------------------------------------------
# 主流程：offline
# ---------------------------------------------------------------------------
def run_offline(
    args: argparse.Namespace,
    cases: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Any],
    *,
    run_id: str,
    started_at: Any,
    repo_root_override: Path | None = None,
) -> tuple[dict, int]:
    bootstrap = import_harness(verbose=args.verbose)
    if bootstrap is None:
        return {}, EXIT_DEPENDENCY

    repo_root = repo_root_override or resolve_repo_root(args.repo_root, verbose=args.verbose)
    if repo_root is None:
        return {}, die(
            "找不到仓库根目录。请用 --repo-root 指定 KnowledgeBase 仓库（含 manage.py），"
            "或设置环境变量 KB_REPO_ROOT。",
            EXIT_DEPENDENCY,
        )

    corpus_dir = Path(args.corpus).expanduser().resolve() if args.corpus else DEFAULT_CORPUS
    if not corpus_dir.is_dir():
        return {}, die(
            f"评测语料目录不存在：{corpus_dir}\n"
            f"  离线评测必须先把语料放进 evals/corpus/（或用 --corpus 指定）。",
            EXIT_DEPENDENCY,
        )
    corpus_files = [path for path in corpus_dir.rglob("*") if path.is_file()]
    if not corpus_files:
        return {}, die(f"评测语料目录是空的：{corpus_dir}", EXIT_DEPENDENCY)

    # 开发期 evals/ 可能还没拷进被测仓库（例如在 evals_build/ 里构建）：
    # offline_settings.py 用 KB_EVAL_REPO_ROOT 推断"仓库根"，必须与 --repo-root 一致，
    # 否则它会拿 evals/.. 当仓库根、进而拒绝位于真实仓库内的 workdir。
    os.environ["KB_EVAL_REPO_ROOT"] = str(repo_root)
    os.environ["KB_EVAL_RUN_ID"] = run_id

    workdir, cleanup, workdir_code = resolve_workdir(args, run_id, repo_root)
    if workdir is None:
        return {}, workdir_code
    if cleanup:
        say(f"[workdir] {workdir}（跑完自动清理；--keep-workdir 可保留）")
    else:
        say(f"[workdir] {workdir}")

    h = None
    try:
        try:
            h = bootstrap(
                repo_root=repo_root,
                workdir=workdir,
                dense=bool(args.dense),
                rerank=bool(args.rerank),
            )
        except Exception as exc:  # noqa: BLE001 - 要给清晰提示而不是裸 traceback
            if args.verbose:
                traceback.print_exc()
            return {}, die(
                f"harness.bootstrap() 失败：{type(exc).__name__}: {exc}\n"
                f"  提示：离线模式要求 KB_TEST_ADAPTER=offline（sqlite + Qdrant embedded）。"
                f"检查 repo_root={repo_root}、workdir={workdir}、依赖是否装齐。",
                EXIT_DEPENDENCY,
            )

        seed_started = time.perf_counter()
        try:
            doc_id_map = h.seed_corpus(corpus_dir) or {}
        except Exception as exc:  # noqa: BLE001
            if args.verbose:
                traceback.print_exc()
            return {}, die(
                f"harness.seed_corpus() 失败：{type(exc).__name__}: {exc}\n"
                f"  语料目录：{corpus_dir}（{len(corpus_files)} 个文件）",
                EXIT_DEPENDENCY,
            )
        seed_seconds = time.perf_counter() - seed_started
        if not isinstance(doc_id_map, Mapping):
            warn(f"seed_corpus 返回的不是 dict（{type(doc_id_map).__name__}），真值映射按空处理")
            doc_id_map = {}
        say(f"[seed] {corpus_dir.name}: {len(corpus_files)} 个文件 → {len(doc_id_map)} 篇文档，"
            f"用时 {seed_seconds:.2f}s")

        started = time.perf_counter()
        executed: list[dict] = []
        for index, case in enumerate(cases, start=1):
            query_started = time.perf_counter()
            try:
                raw = h.search(case["query"], limit=SEARCH_LIMIT)
                wall_ms = (time.perf_counter() - query_started) * 1000.0
                if not isinstance(raw, Mapping):
                    raise TypeError(f"search 返回了 {type(raw).__name__}，期望 dict")
                executed.append(
                    build_case(case, raw, wall_ms, doc_id_map=doc_id_map,
                               prices=prices, rerank=bool(args.rerank))
                )
                flag = "HIT " if executed[-1]["recall_at_5"] else "MISS"
                say(
                    f"[{index:>3}/{len(cases)}] {flag} {case['id']:<24} "
                    f"{executed[-1]['elapsed_ms']:>8.1f}ms  {case['query'][:40]}"
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整轮
                wall_ms = (time.perf_counter() - query_started) * 1000.0
                error_text = f"{type(exc).__name__}: {exc}"
                executed.append(
                    build_case(case, None, wall_ms, doc_id_map=doc_id_map,
                               prices=prices, rerank=bool(args.rerank), error=error_text)
                )
                warn(f"[{index:>3}/{len(cases)}] ERROR {case['id']}: {error_text}")
        run_seconds = time.perf_counter() - started
        total_seconds = round(seed_seconds + run_seconds, 3)

        errors = sum(1 for case in executed if case["status"] != "ok")
        warnings: list[str] = []
        for case in executed:
            for item in case.get("warnings") or []:
                if item not in warnings:
                    warnings.append(item)

        # 先收尾 harness，再清 workdir，这样清理结果能如实写进 meta.degraded
        # （Windows 上 sqlite / Qdrant embedded 的文件句柄可能拖住删除）。
        close_error = None
        try:
            h.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响结论
            close_error = f"{type(exc).__name__}: {exc}"
        h = None
        cleanup_error = _cleanup_workdir(workdir) if cleanup else None

        degraded = [
            "offline 模式：sqlite + Qdrant embedded + KB_TEST_ADAPTER=offline；"
            "稠密/重排为离线桩，不代表真实模型质量",
        ]
        if errors:
            degraded.append(f"{errors}/{len(executed)} 条查询报错（见 cases[].error）")
        if args.dense:
            degraded.append("dense 已请求，但离线向量来自确定性桩")
        if args.rerank:
            degraded.append("rerank 已请求，但离线无 AS711 重排服务，走退化路径")
        if close_error:
            degraded.append(f"harness.close() 抛错（已忽略）：{close_error}")
        if cleanup_error:
            degraded.append(f"workdir 未能自动清理：{workdir}（{cleanup_error}），可手动删除")

        meta = r.build_meta(
            mode="offline",
            dataset_path=args.dataset_path,
            dataset_sha256=args.dataset_sha256,
            limit=len(executed),
            k=K,
            run_id=run_id,
            repo_root=repo_root,
            workdir=workdir,
            dense=bool(args.dense),
            rerank=bool(args.rerank),
            prices=prices,
            git_hash=r.git_short_hash(repo_root),
            generated_at=r.utc_iso(started_at),
            degraded=degraded,
            cases_total=len(executed),
            elapsed_seconds=total_seconds,
            extras={
                "corpus": str(corpus_dir),
                "corpus_files": len(corpus_files),
                "seed_documents": len(doc_id_map),
                "seed_seconds": round(seed_seconds, 3),
                "search_limit": SEARCH_LIMIT,
                "run_seconds": round(run_seconds, 3),
                "dataset_cases_loaded": args.dataset_cases_loaded,
                "dataset_lines": args.dataset_lines,
                "harness_returned_warnings": warnings[:20],
                "elapsed_total_seconds": total_seconds,
                "workdir_leftover": str(workdir) if cleanup_error else None,
            },
        )
        not_measured = r.default_not_measured(
            offline=True, dense=bool(args.dense), rerank=bool(args.rerank)
        )
        report = r.build_report(meta=meta, cases=executed, not_measured=not_measured)
        return report, EXIT_QUERIES_FAILED if errors == len(executed) else EXIT_OK
    finally:
        if h is not None:
            try:
                h.close()
            except Exception as exc:  # noqa: BLE001
                warn(f"h.close() 抛错（已忽略）：{type(exc).__name__}: {exc}")
        if cleanup:
            # 兜底：上面正常路径已清过；异常路径在这里再尽力一次。
            _cleanup_workdir(workdir)


def _cleanup_workdir(workdir: Path) -> str | None:
    """尽力删除本次 run 的 workdir；返回失败原因（``None`` = 已清理或本就不存在）。

    注意 Windows 上两个坑：
    1) ``storage.store_bytes`` 落盘的 raw 文件带 ReadOnly 属性，直接 rmtree 会
       ``PermissionError: [WinError 5]``，所以先整树清掉只读位；
    2) sqlite / Qdrant embedded 的文件句柄可能拖住删除，先关 Django 连接。
    """
    try:  # 先关掉 Django 连接，避免 Windows 上 sqlite 句柄拖住目录删除
        from django.db import connections  # noqa: WPS433 - 离线链路才 import 得到

        connections.close_all()
    except Exception:  # noqa: BLE001
        pass

    for dirpath, dirnames, filenames in os.walk(workdir):
        for name in [*filenames, *dirnames]:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
    try:
        os.chmod(workdir, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass

    shutil.rmtree(workdir, ignore_errors=True)
    if workdir.exists():
        return "目录仍有残留（文件被占用或权限不足）"
    return None


# ---------------------------------------------------------------------------
# 主流程：online —— 按 Lead 口径「故意不实现」
# ---------------------------------------------------------------------------
def run_online(
    args: argparse.Namespace,
    cases: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Any],
    *,
    run_id: str,
    started_at: Any,
) -> tuple[dict, int]:
    """在线模式不接真检索：真实栈验证由 live-verifier 的 evals/live_check.py 负责。

    原因（Lead 2026-10-29 统一口径）：真实服务（PG 55432 / Qdrant 56333 /
    AS711 embed 8000）在线可用，但那份端到端验证要有独立脚本与独立结果文件
    （evals/results/live-*.json），不与离线词法基线混在一份报告里。
    """
    die(
        "在线模式（--online）未实现，本次不会假装跑过。\n"
        "  · 离线词法基线：python evals/run_eval.py --offline（默认）\n"
        "  · 真实栈验证请用：python evals/live_check.py（由 live-verifier 维护，\n"
        "    产出 evals/results/live-*.json）\n"
        "  · 参考实现（未被本 CLI 接线）：evals/kb_eval/client_http.py",
        EXIT_USAGE,
    )
    return {}, EXIT_USAGE


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description="KnowledgeBase AI 检索评测基线（默认 offline：sqlite + Qdrant embedded）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python evals/run_eval.py --limit 10 --dense --prices '{\"input_per_m\":0.5}'",
    )
    parser.add_argument("--dataset", default=None,
                        help=f"数据集 jsonl（默认 {DEFAULT_DATASET}；相对路径先按当前目录、再按脚本目录解析）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 条（默认全部）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="离线模式（默认）")
    mode.add_argument("--online", action="store_true",
                      help="在线 HTTP 模式（需 --api-base 与真实服务）")
    parser.add_argument("--dense", action="store_true", help="启用稠密检索分支")
    parser.add_argument("--rerank", action="store_true", help="启用重排分支（离线为桩）")
    parser.add_argument("--out-dir", default=None,
                        help=f"结果输出目录（默认 {DEFAULT_RESULTS}）")
    parser.add_argument("--repo-root", default=None,
                        help="KnowledgeBase 仓库根（默认 env KB_REPO_ROOT → 向上找 manage.py）")
    parser.add_argument("--workdir", default=None,
                        help="harness 工作目录（默认 <repo-root>/artifacts/evals/<run_id>，须位于仓库内）")
    parser.add_argument("--corpus", default=None, help=f"评测语料目录（默认 {DEFAULT_CORPUS}）")
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时 workdir 便于排查")
    parser.add_argument("--prices", default=None,
                        help='单价 JSON：{"input_per_m":x,"output_per_m":y}（默认 0，仅占位）')
    parser.add_argument("--api-base", default=None, help="在线模式检索服务地址")
    parser.add_argument("--api-user", default=None, help="在线模式 Basic 用户名")
    parser.add_argument("--api-password", default=None, help="在线模式 Basic 密码")
    parser.add_argument("--api-token", default=None, help="在线模式 Bearer token（与 Basic 二选一）")
    parser.add_argument("--verbose", action="store_true", help="打印完整 traceback 与路径解析信息")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认 cp936，中文摘要表/警告会乱码或被管道按 GBK 解码；
    # 统一按 UTF-8 输出（与 harness.py::probe 的做法一致）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = r.utc_now()
    run_id = r.new_run_id(started_at)
    args.verbose = bool(getattr(args, "verbose", False))

    if args.limit is not None and args.limit <= 0:
        return die(f"--limit 必须是正整数，收到 {args.limit}", EXIT_USAGE)
    if args.online and args.workdir:
        warn("--online 模式不使用 --workdir（HTTP 客户端无本地工作目录）")

    try:
        prices = m.parse_prices_json(args.prices)
    except ValueError as exc:
        return die(f"{exc}；示例：--prices '{{\"input_per_m\":0.5,\"output_per_m\":1.0}}'", EXIT_USAGE)

    dataset_path = resolve_dataset(args.dataset)
    try:
        cases, dataset_lines = load_dataset(dataset_path, args.limit)
    except DatasetError as exc:
        return die(f"数据集非法：{exc}", EXIT_DATASET)

    args.dataset_path = str(dataset_path)
    args.dataset_sha256 = r.sha256_file(dataset_path)
    args.dataset_cases_loaded = len(cases)
    args.dataset_lines = dataset_lines

    dataset_short = dataset_path.name
    say(f"[dataset] {dataset_path}")
    say(f"[dataset] {len(cases)} 条用例（文件 {dataset_lines} 行），"
        f"sha256={(args.dataset_sha256 or 'n/a')[:16]}…")
    say("")

    try:
        if args.online:
            report, code = run_online(args, cases, prices, run_id=run_id, started_at=started_at)
        else:
            repo_root = resolve_repo_root(args.repo_root, verbose=args.verbose)
            report, code = run_offline(
                args, cases, prices, run_id=run_id, started_at=started_at,
                repo_root_override=repo_root,
            )
    except KeyboardInterrupt:
        return die("被用户中断（Ctrl+C），未写结果文件", 130)
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            traceback.print_exc()
        return die(f"评测过程意外失败：{type(exc).__name__}: {exc}（--verbose 看 traceback）",
                   EXIT_UNEXPECTED)

    if not report:
        return code or EXIT_UNEXPECTED

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else DEFAULT_RESULTS
    try:
        result_path = r.write_report(report, out_dir, filename=f"{r.utc_stamp(started_at)}.json")
    except OSError as exc:
        return die(f"写结果文件失败：{exc}（检查 --out-dir={out_dir} 是否可写）", EXIT_UNEXPECTED)

    say(r.format_summary_table(report))
    say("")
    say(f"[结果] {result_path}")
    if code == EXIT_QUERIES_FAILED:
        say("[错误] 全部查询失败：报告已写出但指标不可用，请检查上面的报错与 harness/bootstrap 输出。")
    say(f"[完成] run_id={run_id}")
    return code


if __name__ == "__main__":
    sys.exit(main())
