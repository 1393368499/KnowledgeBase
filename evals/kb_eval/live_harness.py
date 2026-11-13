"""真实栈（live）检索夹具：真 PostgreSQL + 真 Qdrant + 真 AS711 模型服务。

与离线 ``harness.py`` 的区别：这里**不造语料**，直接对正在运行的真实服务发起
检索。库里的资料是真实客户合同，因此本模块有一条不可妥协的硬约束——**脱敏**：

- 对外只暴露 ``doc_alias = "doc-" + sha256(original_name)[:8]``，不返回、不记录原始文件名；
- 所有 logger 只输出 alias，失败也只是 alias + 机制，不是文件名；
- 凭据在运行时从 ``.env`` 读进 ``os.environ``，不落盘、不打印、不进异常消息。

运行姿势（为什么必须这样，踩过的坑都在这里）：

1. 在**源码镜像**里以 ``DJANGO_SETTINGS_MODULE=knowledgebase.settings`` + ``django.setup()``
   跑；不经过 HTTP API、不建测试库。
2. 运行时目录必须落在 ``PROJECT_ROOT`` 内：``knowledgebase/paths.py::ensure_runtime_dirs``
   会调 ``assert_inside_project_root`` 硬校验，跑在项目外的 run 目录会直接抛
   ``StoragePathError``。所以 ``run_root`` 不在项目内时，会自动挪到
   ``<repo_root>/artifacts/eval-runs/<name>``。
3. 必须显式 ``DB_ENGINE=postgres``：``knowledgebase.settings`` 在非 DEBUG + sqlite 的组合下
   直接拒绝启动。
4. 超管口径检索（``permission_codes=None``）绕开成员权限，这是"验证检索链路"而不是
   "验证授权"的正确姿势；授权另有人测。

用法::

    from evals.kb_eval.live_harness import bootstrap_live

    harness = bootstrap_live(Path(r"<repo-root>"), Path("run-live"))
    try:
        files = harness.list_target_files()
        out = harness.search("服务合同的付款方式是什么", limit=10)
    finally:
        harness.close()

命令行冒烟（只打印 alias 与命中数）::

    python -m evals.kb_eval.live_harness --repo <repo-root> --query "..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("kb_eval.live")

#: evals/ 目录（本文件在 evals/kb_eval/ 下）
_EVALS_DIR = Path(__file__).resolve().parent.parent

#: 默认目标信息库（真实语料库：code=contract-archive）
DEFAULT_LIBRARY_CODE = "contract-archive"

#: 会从 ``.env`` 搬进 ``os.environ`` 的键前缀（只搬配置，不猜、不补、不打印值）
ENV_IMPORT_PREFIXES: tuple[str, ...] = (
    "KB_PG_",
    "KB_QDRANT",
    "QDRANT",
    "KB_REDIS",
    "REDIS",
    "AS711_",
    "KB_EMBED",
    "KB_RERANK",
    "KB_LLM",
    "KB_OCR",
    "KB_SEARCH",
    "KB_MODEL_CALLS_DISABLED",
    "DJANGO_",
    "SECRET_KEY",
    "KB_TEST_ADAPTER",
    "KB_SERVICE_",
    "KB_UPLOAD",
    "KB_DISPATCH",
    "KB_LOAD_TEST",
    "DB_ENGINE",
    "POSTGRES_",
)

#: 检索报告里允许对外暴露的字段（白名单；报告其它字段可能带内部路径/文件名）
REPORT_FIELDS: tuple[str, ...] = (
    "collections",
    "dense_hits",
    "sparse_hits",
    "db_keyword_hits",
    "fused_hits",
    "rerank",
    "degraded",
    "degraded_reason",
    "vector_store_available",
    "dense_checked_by_breaker",
    "local_mode_sparse_unsupported",
    "eligible_files",
    "reason",
)

_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,8})$")

#: 进程内只做一次 django.setup()；第二次 bootstrap 只重建 harness 对象。
_DJANGO_READY = False


# ---------------------------------------------------------------------------
# 脱敏工具
# ---------------------------------------------------------------------------
def doc_alias(original_name: str | None) -> str:
    """文件名 → 稳定别名 ``doc-<sha256 前 8 位 hex>``。

    用 sha256 而不是随机 id：同一份资料在多次运行、多个进程里别名一致，
    报告可以跨轮次对比；同时不可逆（不是 md5 前缀那种"看着像哈希"的东西）。
    """
    raw = (original_name or "").strip()
    if not raw:
        return "doc-00000000"
    return "doc-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def extension_of(original_name: str | None) -> str:
    """扩展名（小写、不带点）；没有就返回空串。"""
    match = _EXT_RE.search((original_name or "").strip())
    return match.group(1).lower() if match else ""


# ---------------------------------------------------------------------------
# 环境装载
# ---------------------------------------------------------------------------
def load_env_file(path: Path) -> dict[str, str]:
    """读 ``.env`` 成 dict（只解析 KEY=VALUE，忽略注释/空行）。

    返回值只在本模块内部使用，绝不写文件、绝不进日志、绝不进报告。
    """
    env: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _candidate_env_files(repo_root: Path) -> list[Path]:
    out: list[Path] = [repo_root / ".env"]
    for var in ("KB_LIVE_ENV_FILE", "KB_SOURCE_ROOT"):
        raw = os.environ.get(var, "").strip()
        if raw:
            candidate = Path(raw)
            out.append(candidate if candidate.suffix else candidate / ".env")
    return out


def _resolve_env_file(repo_root: Path, env_file: Path | str | None) -> Path:
    if env_file is not None:
        path = Path(env_file)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            raise FileNotFoundError(f"指定的 .env 不存在：{path}")
        return path
    tried: list[str] = []
    for candidate in _candidate_env_files(repo_root):
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到真实栈 .env。已尝试：" + "；".join(tried)
        + "。请用 env_file= 参数或 KB_LIVE_ENV_FILE / KB_SOURCE_ROOT 环境变量指定。"
    )


def _resolve_run_root(repo_root: Path, run_root: Path | str) -> Path:
    """run_root 必须落在项目根内；落在外面就自动挪进去（硬约束，见模块说明）。"""
    root = repo_root.resolve()
    candidate = Path(run_root)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    fallback = root / "artifacts" / "eval-runs" / (candidate.name or "live")
    logger.warning(
        "run_root 不在项目根内，已改到 %s（knowledgebase.paths 强制运行时目录必须在项目内）",
        fallback,
    )
    return fallback


def _inject_env(env: Mapping[str, str]) -> dict[str, Any]:
    """把 .env 里允许的键注入 os.environ，并覆盖成"真栈检索"姿势。

    返回一份**只含非敏感事实**的摘要（键名、数据库引擎、是否命中 dense 开关），
    供报告 meta 使用；值本身永不外泄。
    """
    imported: list[str] = []
    for key, value in env.items():
        if key.startswith(ENV_IMPORT_PREFIXES):
            os.environ[key] = value
            imported.append(key)

    # 落盘目录：全部重定向到 run_root 下（稍后由 bootstrap 写入具体路径）
    os.environ["DB_ENGINE"] = os.environ.get("DB_ENGINE") or "postgres"
    # 真栈必须真调模型：关掉测试适配器与"禁调模型"开关，打开 dense。
    os.environ["KB_TEST_ADAPTER"] = ""
    os.environ["KB_MODEL_CALLS_DISABLED"] = "0"
    os.environ["KB_SEARCH_DENSE_ENABLED"] = "1"
    os.environ["DJANGO_SETTINGS_MODULE"] = "knowledgebase.settings"
    os.environ.setdefault("DJANGO_DEBUG", "1")
    # settings.py 会用 python-dotenv override=True 再读一次 .env；我们已经把需要的键
    # 注入好了，关掉二次加载避免它把运行时目录改回项目默认值（确定性优先）。
    os.environ["KB_SKIP_DOTENV"] = "1"

    return {
        "env_imported_keys": sorted(imported),
        "db_engine": os.environ.get("DB_ENGINE"),
        "dense_enabled": os.environ.get("KB_SEARCH_DENSE_ENABLED"),
        "model_calls_disabled": os.environ.get("KB_MODEL_CALLS_DISABLED"),
    }


def _prepare_runtime_dirs(run_root: Path) -> dict[str, Path]:
    dirs = {
        "vector_data": run_root / "vector_data",
        "media": run_root / "media",
        "raw_store": run_root / "media" / "raw",
        "db": run_root / "db",
        "artifacts": run_root / "artifacts",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    os.environ["VECTOR_DATA_DIR"] = str(dirs["vector_data"])
    os.environ["RAW_STORE_DIR"] = str(dirs["raw_store"])
    os.environ["MEDIA_ROOT_DIR"] = str(dirs["media"])
    os.environ["DB_DIR"] = str(dirs["db"])
    return dirs


#: 真栈噪音源：settings.LOGGING 会把 root 调到 INFO，这些 logger 必须单独压下去
NOISY_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "qdrant_client",
    "asyncssh",
    "apps.knowledge.pipeline",
    "apps.knowledge.vectorindex",
    "apps.knowledge.embedding",
    "apps.knowledge.rerank",
)


def _config_logging(quiet: bool) -> None:
    """压日志。

    注意：``logging.basicConfig(level=...)`` 在 root 已有 handler 时是**空操作**，
    而 Django ``settings.LOGGING`` 一定会先给 root 装 handler，所以这里必须
    显式 ``root.setLevel``，否则 INFO 级 httpx 请求日志会把 10 条评测刷成几百行。
    """
    root = logging.getLogger()
    root.setLevel(logging.WARNING if quiet else logging.INFO)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if not quiet:  # pragma: no cover - 交互排查用
        logging.getLogger("kb_eval.live").setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# LiveHarness
# ---------------------------------------------------------------------------
class LiveHarness:
    """真实栈检索夹具。

    只读语义：本类**不写业务数据**（唯一的写是检索链自己的熔断/反馈计数，
    那是真实服务本来就会写的）。``close()`` 关掉数据库连接，可以安全重入。
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        run_root: Path,
        library_code: str,
        env_summary: dict[str, Any],
        runtime_dirs: Mapping[str, Path],
    ) -> None:
        self.repo_root = Path(repo_root)
        self.run_root = Path(run_root)
        self.library_code = library_code
        self.env_summary = dict(env_summary)
        self.runtime_dirs = dict(runtime_dirs)
        self._alias_by_file_id: dict[int, str] = {}
        self._django_models = _import_django_models()
        self.library = self._resolve_library(library_code)

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _resolve_library(code: str):
        KnowledgeLibrary = _lm().KnowledgeLibrary
        library = KnowledgeLibrary.objects.filter(code=code, is_active=True).first()
        if library is None:
            active = list(
                KnowledgeLibrary.objects.filter(is_active=True).values_list("id", "code")
            )
            raise LookupError(
                f"信息库 code={code!r} 不存在或未激活；当前激活的库（id, code）={active}"
            )
        return library

    def _target_queryset(self):
        IngestedFile = _lm().IngestedFile
        return (
            IngestedFile.objects.filter(
                library=self.library,
                parse_status="done",
                vector_status="indexed",
            )
            .exclude(chunk_count=0)
            .order_by("id")
        )

    def _alias_for_row(self, row) -> str:
        file_id = int(row.pk)
        cached = self._alias_by_file_id.get(file_id)
        if cached is None:
            cached = doc_alias(getattr(row, "original_name", ""))
            self._alias_by_file_id[file_id] = cached
        return cached

    # -- 公共 API ---------------------------------------------------------
    def library_info(self) -> dict[str, Any]:
        """目标库的脱敏摘要（给报告 meta 用：不出现文件名）。"""
        return {
            "library_code": self.library.code,
            "library_id": int(self.library.pk),
            "vector_collection": getattr(self.library, "vector_collection", "") or "",
            "active_generation": getattr(self.library, "active_generation", None),
            "target_file_count": self._target_queryset().count(),
        }

    def list_target_files(self, library_code: str | None = None) -> list[dict[str, Any]]:
        """列出可供评测的目标文件（只给 alias，不给原始名）。

        返回 ``[{file_id, doc_alias, extension, chunk_count, library_id}]``，
        按 file_id 升序，便于"挑 10 个不同文件"这类确定性抽样。
        """
        if library_code and library_code != self.library_code:
            self.library = self._resolve_library(library_code)
            self.library_code = library_code
        rows = list(self._target_queryset().only("id", "original_name", "chunk_count"))
        return [
            {
                "file_id": int(row.pk),
                "doc_alias": self._alias_for_row(row),
                "extension": extension_of(row.original_name),
                "chunk_count": int(row.chunk_count or 0),
                "library_id": int(self.library.pk),
            }
            for row in rows
        ]

    def chunks_of(
        self, file_id: int, *, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        """取某份资料的真实分段（造题/核对真值用；**只允许本地使用**）。"""
        DocumentChunk = _lm().DocumentChunk
        qs = (
            DocumentChunk.objects.filter(file_id=int(file_id))
            .order_by("ordinal")
            .values_list("ordinal", "text", "page")
        )
        if offset:
            qs = qs[offset:]
        if limit:
            qs = qs[: int(limit)]
        return [
            {"ordinal": int(ordinal), "text": text or "", "page": page}
            for ordinal, text, page in qs
        ]

    def export_alias_map(self, dest: Path | str, *, library_code: str | None = None) -> int:
        """把 alias → file_id / 真实文件名 写到**本地**文件，返回条数。

        为什么用"导出文件"而不是"返回名字"：harness 的返回值和日志只允许出现
        alias；但本地排查需要知道 alias 对应哪份真实文件。所以名字只落进调用方
        指定的本地文件（约定为 ``<dataset-live>/doc-alias-map.json``，不进仓库），
        **不经过 API 返回值、不经过日志**。
        """
        rows = self.list_target_files(library_code=library_code)
        wanted = {row["file_id"] for row in rows}
        IngestedFile = _lm().IngestedFile
        mapping: dict[str, dict[str, Any]] = {}
        for file_id, original_name in IngestedFile.objects.filter(
            pk__in=list(wanted)
        ).values_list("id", "original_name"):
            mapping[doc_alias(original_name)] = {
                "file_id": int(file_id),
                "original_name": original_name or "",
            }
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("alias map exported: %d entries -> local file only", len(mapping))
        return len(mapping)

    def search(
        self,
        query: str,
        limit: int = 10,
        use_rerank: bool = False,
        *,
        libraries: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        pipeline = _pipeline()
        libs = list(libraries) if libraries else [self.library]
        t0 = time.perf_counter()
        outcome = pipeline.search(
            query=query,
            libraries=libs,
            permission_codes=None,  # 超管口径：验证检索链路，不验证授权
            limit=int(limit),
            use_rerank=bool(use_rerank),
            keyword_library_ids=[int(lib.pk) for lib in libs],
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        raw_results = outcome.get("results") or []
        results: list[dict[str, Any]] = []
        for item in raw_results:
            name = item.get("original_name") or ""
            alias = doc_alias(name)
            file_id = item.get("file_id")
            if file_id not in (None, ""):
                self._alias_by_file_id.setdefault(int(file_id), alias)
            results.append(
                {
                    "file_id": None if file_id in (None, "") else int(file_id),
                    "doc_alias": alias,
                    "extension": extension_of(name),
                    "score": _as_float(item.get("score")),
                    "text": item.get("text") or "",
                }
            )

        report = sanitize_report(outcome.get("report"))
        logger.debug(
            "live search done: %d results, dense=%s dbkw=%s fused=%s degraded=%s elapsed=%.0fms",
            len(results),
            report.get("dense_hits"),
            report.get("db_keyword_hits"),
            report.get("fused_hits"),
            report.get("degraded"),
            elapsed_ms,
        )
        return {
            "results": results,
            "report": report,
            "elapsed_ms": round(elapsed_ms, 3),
        }

    def close(self) -> None:
        """关掉 Django 数据库连接（不 destroy 任何东西）。"""
        try:
            from django.db import connections

            connections.close_all()
        except Exception:  # pragma: no cover - 关闭失败不该影响结果落盘
            logger.warning("close(): 关闭数据库连接失败，忽略")

    def __enter__(self) -> "LiveHarness":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _as_float(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def sanitize_report(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """报告白名单：只留可公开的链路计数，避免内部路径/异常文本外泄。"""
    out: dict[str, Any] = {}
    for key in REPORT_FIELDS:
        if report and key in report:
            out[key] = report[key]
    rerank = out.get("rerank")
    if isinstance(rerank, Mapping):
        # rerank 子结构只留三个布尔/数字位，丢掉 error 文本（可能带端点或片段）
        out["rerank"] = {
            "enabled": bool(rerank.get("enabled")),
            "applied": bool(rerank.get("applied")),
            "error": "1" if rerank.get("error") else "",
        }
    return out


# ---------------------------------------------------------------------------
# Django 引导
# ---------------------------------------------------------------------------
def _import_django_models():
    from apps.knowledge import models

    return models


def _lm():
    """models 模块句柄（延迟导入，避免在 django.setup() 前触发 app registry）。"""
    import sys as _sys

    return _sys.modules["apps.knowledge.models"]


def _pipeline():
    from apps.knowledge import pipeline

    return pipeline


def bootstrap_live(
    repo_root: Path | str,
    run_root: Path | str,
    *,
    env_file: Path | str | None = None,
    library_code: str = DEFAULT_LIBRARY_CODE,
    quiet: bool = True,
) -> LiveHarness:
    """初始化真实栈夹具。

    参数
    ----
    repo_root
        含 ``knowledgebase/settings.py`` 的源码根（默认取 ``evals/`` 的父目录）。
        必须是可写目录：运行时目录要落在它里面。
    run_root
        运行期目录；落在项目外会被自动挪进 ``<repo_root>/artifacts/eval-runs/<name>``。
    env_file
        ``.env`` 路径；默认 ``repo_root/.env`` → ``$KB_LIVE_ENV_FILE`` → ``$KB_SOURCE_ROOT/.env``。
    library_code
        目标信息库的 code（默认 ``contract-archive``）。
    quiet
        True 时把日志压到 WARNING（真栈输出很吵，评测只看结果）。

    每个进程只真正 ``django.setup()`` 一次；重复调用只重建 harness 对象。
    """
    global _DJANGO_READY

    root = Path(repo_root).expanduser().resolve()
    if not (root / "config" / "settings.py").is_file():
        raise FileNotFoundError(f"repo_root 不像 KW 源码根（缺 knowledgebase/settings.py）：{root}")

    resolved_env = _resolve_env_file(root, env_file)
    run_dir = _resolve_run_root(root, run_root)
    env_summary = _inject_env(load_env_file(resolved_env))
    runtime_dirs = _prepare_runtime_dirs(run_dir)

    if not _DJANGO_READY:
        sys.path.insert(0, str(root))
        os.chdir(root)
        import django

        django.setup()  # settings.LOGGING 在这之后才生效，所以压日志放在它后面
        _DJANGO_READY = True
        logger.info("live harness bootstrapped (run_root=%s)", run_dir)
    _config_logging(quiet)

    harness = LiveHarness(
        repo_root=root,
        run_root=run_dir,
        library_code=library_code,
        env_summary=env_summary,
        runtime_dirs=runtime_dirs,
    )
    harness.env_summary["run_root"] = str(run_dir)
    harness.env_summary["env_file_keys"] = len(env_summary.get("env_imported_keys") or [])
    return harness


# ---------------------------------------------------------------------------
# 冒烟 CLI（只打印 alias）
# ---------------------------------------------------------------------------
def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真栈检索冒烟（只打印 alias）")
    parser.add_argument("--repo", default=os.environ.get("KB_REPO_ROOT") or str(_EVALS_DIR.parent))
    parser.add_argument("--run-root", default="artifacts/eval-runs/live-smoke")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--library-code", default=DEFAULT_LIBRARY_CODE)
    parser.add_argument("--query", default="服务合同的付款方式是什么")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--list-files", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # 控制台乱码很吵
        except Exception:  # pragma: no cover
            pass

    harness = bootstrap_live(
        args.repo, args.run_root, env_file=args.env_file, library_code=args.library_code
    )
    try:
        print("library:", harness.library_info())
        if args.list_files:
            files = harness.list_target_files()
            print(f"target files: {len(files)}")
            for row in files[:60]:
                print(
                    f"  {row['file_id']:>5}  {row['doc_alias']}  "
                    f"ext={row['extension'] or '-':<4} chunks={row['chunk_count']}"
                )
        out = harness.search(args.query, limit=args.limit, use_rerank=args.rerank)
        print("elapsed_ms:", out["elapsed_ms"])
        print("report:", out["report"])
        for rank, row in enumerate(out["results"], start=1):
            print(
                f"  #{rank:<2} {row['doc_alias']}  score={row['score']:.4f}  "
                f"chars={len(row['text'])}"
            )
    finally:
        harness.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
