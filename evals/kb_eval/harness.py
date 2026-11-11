"""离线评测 harness：在 sqlite + Qdrant embedded + 确定性假向量下跑**真实检索链路**。

契约（与 run_eval.py / metrics.py 对接，别改签名）::

    h = bootstrap(repo_root, workdir, dense=False, rerank=False) -> Harness
    mapping = h.seed_corpus(corpus_dir)   # {doc_key: file_id}
    out = h.search("查询", limit=10)      # {"results","report","elapsed_ms","warnings"}
    h.close()

要点
----
* **零外呼**：``offline_settings`` 把 Qdrant 设为 embedded、模型总开关保持关闭语义
  之外的那一路全靠 ``KB_TEST_ADAPTER=offline`` 假向量；分类/重排等模型入口一律
  注入假客户端或直接关闭。全流程不产生任何网络请求。
* **不自己拼 collection 名**：目标来自 ``pipeline.ensure_library_index`` 的真实登记行；
  检索由 ``pipeline.search`` 自己按登记行解析（见 pipeline.py 1888-1953 注释）。
* **一个进程一个 Qdrant 客户端**：embedded 模式由文件锁串行化，同一目录不能开第二个
  客户端，所以客户端由 harness 持有并显式传给 ensure / process_file / search。
* 造数据走真实链路：``storage.store_bytes`` → ``services.ingest_file`` →
  ``pipeline.process_file``（解析 / 分段 / 假向量 / upsert 都由流水线完成）。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["Harness", "bootstrap", "probe", "HarnessError"]

# evals/kb_eval/harness.py -> evals/kb_eval -> evals -> 仓库根
_EVALS_DIR: Path = Path(__file__).resolve().parent.parent
#: 开发期 evals/ 可能还没拷进仓库（例如在 evals_build/ 里构建），
#: 允许用 KB_EVAL_REPO_ROOT 显式指定被测仓库根；正式交付时留空即可。
_DEFAULT_REPO_ROOT: Path = Path(
    os.environ.get("KB_EVAL_REPO_ROOT", "").strip() or str(_EVALS_DIR.parent)
).expanduser()

#: 种子语料允许的扩展名（与 settings.KB_ALLOWED_UPLOAD_EXT 的核心子集一致）
_SEED_EXTS = {".md", ".txt", ".csv"}


class HarnessError(RuntimeError):
    """harness 初始化/造数据/检索失败时抛出（信息面向人，便于排查）。"""


class _OfflineClassifier:
    """假分类器：``classify_content`` 的注入点。

    为什么必须注入：``process_file`` 会自动做性质判定，默认走真实 LLM 客户端
    （``_ModelClassifier`` → ``apps.planner.llm.LlmClient``）。离线评测绝不能外呼，
    而 ``KB_MODEL_CALLS_DISABLED=1`` 又会连带关掉稠密检索，所以只能在这里注入。
    返回 ``("", 原因)`` → 判定为"待识别"，不写 classification，也不改任何内容。
    """

    def classify(self, *, text: str = "", original_name: str = "") -> tuple[str, str]:
        return "", "offline-eval：不调用模型，保持待识别"


@dataclass
class Harness:
    """离线 harness 的持有对象（由 :func:`bootstrap` 创建）。"""

    repo_root: Path
    workdir: Path
    dense: bool
    rerank: bool
    client: Any
    library: Any
    profile: Any = None
    collection: str = ""
    generation: int = 0
    dimension: int = 0
    warnings: list[str] = field(default_factory=list)
    #: doc_key -> file_id（seed 时建立，search 用它反推 doc_id）
    doc_to_file: dict[str, int] = field(default_factory=dict)
    file_to_doc: dict[int, str] = field(default_factory=dict)
    #: 最近一次检索的原始返回（report / pipeline 结果），便于 run_eval 取细节
    last_report: dict = field(default_factory=dict)
    last_raw: dict = field(default_factory=dict)
    closed: bool = False

    # ------------------------------------------------------------ 只读属性 ----
    @property
    def db_path(self) -> Path:
        return self.workdir / "db" / "kb.sqlite3"

    @property
    def vector_dir(self) -> Path:
        return self.workdir / "vector_data" / "embedded"

    # ------------------------------------------------------------ 造数据 ----
    def seed_corpus(self, corpus_dir: str | os.PathLike[str] | Iterable[str]) -> dict[str, int]:
        """把语料落进库并跑完流水线，返回 ``{doc_key: file_id}``。

        ``corpus_dir`` 可以是目录（递归收集 ``.md``/``.txt``/``.csv``，按相对路径排序，
        保证确定性），也可以是文件路径的可迭代对象。``doc_key`` 取文件名 stem；
        stem 冲突时依次退化为 ``<父目录>/<stem>``、``<stem>__2``、``<stem>__3``…。

        幂等：同内容重复 seed 会命中 ``ingest_file`` 的内容去重，已 DONE 的文件
        ``process_file`` 直接跳过（不重复嵌入）。
        """
        self._ensure_open()
        files = self._collect_corpus_files(corpus_dir)
        if not files:
            raise HarnessError(f"语料目录里没有可入库文件（支持 {sorted(_SEED_EXTS)}）：{corpus_dir}")

        from apps.knowledge import pipeline, services, storage

        mapping: dict[str, int] = {}
        used_keys: set[str] = set()
        for path in files:
            raw = path.read_bytes()
            blob = storage.store_bytes(raw)  # 内容寻址落盘（已存在则复用）
            rel_parent = ""
            try:
                rel_parent = str(path.parent.relative_to(Path(corpus_dir))).replace("\\", "/")
            except (TypeError, ValueError):
                rel_parent = ""
            if rel_parent == ".":
                rel_parent = ""
            result = services.ingest_file(
                library=self.library,
                original_name=path.name,
                content_sha256=blob.sha256,
                size_bytes=blob.size_bytes,
                blob=blob,
                source="eval",
                directory_path=rel_parent,
            )
            record = result.file
            doc_key = self._doc_key_for(path, used_keys)
            used_keys.add(doc_key)
            mapping[doc_key] = record.pk
            self.doc_to_file[doc_key] = record.pk
            self.file_to_doc[record.pk] = doc_key

            if not result.created:
                self.warnings.append(
                    f"语料 {path.name} 命中内容去重（file_id={record.pk}），复用已有记录"
                )
            try:
                step = pipeline.process_file(
                    record,
                    client=self.client,
                    classify_client=_OfflineClassifier(),
                )
            except Exception as exc:  # noqa: BLE001 - 失败要给可读诊断
                raise HarnessError(
                    f"流水线处理 {path.name}（file_id={record.pk}）失败："
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            record.refresh_from_db()
            if not getattr(step, "ok", False):
                raise HarnessError(
                    f"流水线处理 {path.name} 未成功：detail={getattr(step, 'detail', '')!r}"
                )
            if str(getattr(record, "parse_status", "")) != "done":
                raise HarnessError(
                    f"资料 {path.name} 处理完但状态是 {record.parse_status!r}（期望 done）"
                )

        # 库指针/统计刷新，保证 search 拿到最新登记
        self.library.refresh_from_db()
        return mapping

    @staticmethod
    def _doc_key_for(path: Path, used: set[str]) -> str:
        stem = path.stem
        if stem not in used:
            return stem
        parent = path.parent.name
        candidate = f"{parent}/{stem}" if parent else stem
        if candidate not in used:
            return candidate
        n = 2
        while f"{stem}__{n}" in used:
            n += 1
        return f"{stem}__{n}"

    @staticmethod
    def _collect_corpus_files(
        corpus_dir: str | os.PathLike[str] | Iterable[str],
    ) -> list[Path]:
        if isinstance(corpus_dir, (str, os.PathLike)):
            root = Path(corpus_dir).expanduser().resolve()
            if root.is_file():
                return [root]
            if not root.is_dir():
                raise HarnessError(f"语料目录不存在：{root}")
            return sorted(
                (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _SEED_EXTS),
                key=lambda p: str(p.relative_to(root)).replace("\\", "/"),
            )
        out = [Path(p).expanduser().resolve() for p in corpus_dir]
        return [p for p in out if p.is_file()]

    # -------------------------------------------------------------- 检索 ----
    def search(self, query: str, limit: int = 10) -> dict:
        """跑一次真实混合检索，返回契约结构。``elapsed_ms`` 只计 ``pipeline.search`` 的墙钟。"""
        self._ensure_open()
        from apps.knowledge import pipeline

        started = time.perf_counter()
        # ``dense=False`` 的真关闭方式：pipeline.py:2038 故意在**测试适配器开启**时
        # 忽略全局 dense 开关（让测试能覆盖稠密路），所以单设
        # KB_SEARCH_DENSE_ENABLED=0 关不掉它。检索这一刻临时把适配器摘掉，
        # dense 就会走"已配置关闭"的正常降级分支（不发嵌入、不碰网络）；
        # seed 阶段的假向量早就写进 collection 了，不受影响。
        adapter_snapshot = None
        if not self.dense:
            from django.conf import settings as dj_settings

            adapter_snapshot = getattr(dj_settings, "KB_TEST_ADAPTER", "")
            dj_settings.KB_TEST_ADAPTER = ""
        try:
            raw = pipeline.search(
                query=query,
                libraries=[self.library],
                permission_codes=[f"library:{self.library.pk}"],
                limit=limit,
                use_rerank=self.rerank,
                client=self.client,
            )
        except Exception as exc:  # noqa: BLE001 - 检索失败也要给结构化诊断
            raise HarnessError(f"检索失败（query={query!r}）：{type(exc).__name__}: {exc}") from exc
        finally:
            if adapter_snapshot is not None:
                from django.conf import settings as dj_settings

                dj_settings.KB_TEST_ADAPTER = adapter_snapshot
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)

        self.last_raw = raw
        report = dict(raw.get("report") or {})
        self.last_report = report
        results = [self._shape_hit(item) for item in raw.get("results") or []]
        return {
            "results": results,
            "report": report,
            "elapsed_ms": elapsed_ms,
            "warnings": self._warnings_from_report(report),
        }

    def _shape_hit(self, item: dict) -> dict:
        file_id = item.get("file_id")
        original_name = str(item.get("original_name", "") or "")
        doc_id = self.file_to_doc.get(file_id) if file_id is not None else None
        if not doc_id:
            # 兜底：doc_id 由 original_name 的 stem 反推（seed 外的历史行也能对上）
            doc_id = Path(original_name).stem if original_name else ""
            if doc_id and file_id is not None:
                self.file_to_doc.setdefault(file_id, doc_id)
        return {
            "file_id": file_id,
            "doc_id": doc_id,
            "text": str(item.get("text", "") or ""),
            "score": item.get("score", 0.0),
            "original_name": original_name,
            "library_code": str(item.get("library_code", "") or ""),
        }

    def _warnings_from_report(self, report: dict) -> list[str]:
        out = list(self.warnings)
        if report.get("degraded"):
            out.append(f"检索降级：{report.get('degraded_reason') or '原因未知'}")
        for failed in report.get("failed_collections") or []:
            out.append(
                f"库 {failed.get('library_id')} 的 collection {failed.get('collection')} 检索失败："
                f"{failed.get('reason')}"
            )
        if report.get("local_mode_sparse_unsupported"):
            out.append(
                "Qdrant 非 server 模式（embedded）：sparse(MatchText) 路的可靠性见 BASELINE 说明"
            )
        if report.get("tombstoned_dropped"):
            out.append(f"已丢弃 {report['tombstoned_dropped']} 条无法核验/已删除的残留向量")
        return out

    # -------------------------------------------------------------- 收尾 ----
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.client is not None:
                closer = getattr(self.client, "close", None)
                if callable(closer):
                    closer()
        except Exception:  # noqa: BLE001 - 关闭失败不影响评测结论
            pass
        self.client = None

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self.closed:
            raise HarnessError("harness 已关闭（client 已释放），请重新 bootstrap")


# ---------------------------------------------------------------- bootstrap ----


def bootstrap(
    repo_root: str | os.PathLike[str] | None = None,
    workdir: str | os.PathLike[str] | None = None,
    dense: bool = False,
    rerank: bool = False,
    *,
    library_code: str = "eval-offline",
    library_name: str = "离线评测信息库",
    fresh: bool = False,
    migrate: bool = True,
) -> Harness:
    """准备离线环境并返回 :class:`Harness`。

    参数
    ----
    repo_root : 仓库根（含 ``manage.py``/``config``）。默认 = 本文件向上两级。
    workdir   : 运行目录（sqlite / vector_data / raw / media 全在里面）。
                必须位于 ``repo_root`` 内（``config/paths.py`` 的硬约束），
                默认 ``<repo_root>/artifacts/evals/offline``。
    dense     : 是否启用稠密检索（默认 False，只走 sparse+DB 关键词）。
                注意实现方式：``pipeline.py:2038`` 在**测试适配器开启**时会故意
                忽略 ``KB_SEARCH_DENSE_ENABLED``（让测试能覆盖稠密路），所以
                ``dense=False`` 是"检索那一刻临时摘掉 KB_TEST_ADAPTER"来实现的；
                入库阶段的假向量照旧由适配器写入。
    rerank    : 是否启用重排。离线环境没有重排服务，传 True 只会在 warnings 里
                记一条已知限制，实际保持关闭（绝不外呼）。
    fresh     : True 时先清掉 workdir 下的运行期数据（db/vector_data/raw/media）。

    注意
    ----
    同一个 workdir 同一时刻只能有一个进程持有 Qdrant embedded 客户端。
    """
    repo = Path(repo_root).expanduser().resolve() if repo_root else _DEFAULT_REPO_ROOT
    if not (repo / "manage.py").is_file() and not (repo / "config" / "settings.py").is_file():
        raise HarnessError(
            f"repo_root 看起来不是本项目根目录（找不到 manage.py / config/settings.py）：{repo}"
        )
    work = Path(workdir).expanduser().resolve() if workdir else repo / "artifacts" / "evals" / "offline"
    try:
        work.relative_to(repo)
    except ValueError:
        raise HarnessError(
            f"workdir 必须位于 repo_root 内（config/paths.py 会拒绝项目根外的路径）：{work}"
        ) from None

    if fresh:
        for sub in ("db", "vector_data", "media", "raw"):
            target = work / sub
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)

    work.mkdir(parents=True, exist_ok=True)
    os.environ["KB_EVAL_WORKDIR"] = str(work)
    os.environ["KB_SEARCH_DENSE_ENABLED"] = "1" if dense else "0"
    os.environ["KB_SEARCH_USE_RERANK"] = "0"  # 离线重排必关

    for entry in (str(repo), str(_EVALS_DIR)):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    os.environ["DJANGO_SETTINGS_MODULE"] = "offline_settings"
    try:
        import django
        from django.conf import settings as dj_settings

        if not dj_settings.configured:
            django.setup()
        else:
            # 已被别的入口 setup 过：只接受同一个离线 settings，否则直接报错而不是静默跑错。
            if os.environ.get("DJANGO_SETTINGS_MODULE") != "offline_settings":
                raise HarnessError(
                    "Django 已用其它 settings 初始化（"
                    f"{dj_settings.SETTINGS_MODULE!r}），无法在同一进程切到 offline_settings"
                )
    except HarnessError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HarnessError(f"django.setup() 失败（offline_settings）：{type(exc).__name__}: {exc}") from exc

    from django.core.management import call_command

    if migrate:
        try:
            call_command("migrate", verbosity=0, interactive=False)
        except Exception as exc:  # noqa: BLE001
            raise HarnessError(f"migrate 失败（sqlite 离线库 {work}）：{type(exc).__name__}: {exc}") from exc

    from apps.knowledge import pipeline, vectorindex
    from apps.knowledge.embedding import active_profile
    from apps.knowledge.models import KnowledgeLibrary

    warnings: list[str] = []
    if rerank:
        warnings.append(
            "离线环境没有重排服务：rerank 请求已按已知限制关闭（KB_SEARCH_USE_RERANK=0），"
            "评测口径为 RRF 融合后的结果"
        )
    if not dense:
        warnings.append(
            "dense=False：检索阶段临时摘掉 KB_TEST_ADAPTER 以真正跳过稠密路"
            "（pipeline.py:2038 在适配器开启时会忽略全局 dense 开关）；"
            "入库阶段写入的确定性假向量不受影响，本口径结果来自 sparse + DB 关键词两路"
        )

    try:
        client = vectorindex.build_client()
    except Exception as exc:  # noqa: BLE001
        raise HarnessError(
            "打开 Qdrant embedded 客户端失败（同一 workdir 同时只允许一个进程持有）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    library, _created = KnowledgeLibrary.objects.get_or_create(
        code=library_code,
        defaults={
            "name": library_name,
            "level": "internal",
            "is_active": True,
            "description": "evals/offline harness 自动创建",
        },
    )
    if not library.is_active:
        KnowledgeLibrary.objects.filter(pk=library.pk).update(is_active=True)
        library.refresh_from_db()

    profile = active_profile()
    try:
        index_info = pipeline.ensure_library_index(library, profile=profile, client=client)
    except Exception as exc:  # noqa: BLE001
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        raise HarnessError(
            f"ensure_library_index 失败（库 {library.code}）：{type(exc).__name__}: {exc}"
        ) from exc
    library.refresh_from_db()

    harness = Harness(
        repo_root=repo,
        workdir=work,
        dense=bool(dense),
        rerank=False,
        client=client,
        library=library,
        profile=profile,
        collection=str(index_info.get("collection", "")),
        generation=int(index_info.get("generation", 0) or 0),
        dimension=int(index_info.get("dimension", getattr(profile, "dimension", 0)) or 0),
        warnings=warnings,
    )
    harness.file_to_doc.update(_load_existing_map(library))
    return harness


def _load_existing_map(library) -> dict[int, str]:
    """已入库文件 → doc_key（stem），让"先 seed、后重启进程再 search"也能拿到 doc_id。"""
    from apps.knowledge.models import IngestedFile

    out: dict[int, str] = {}
    for row in IngestedFile._base_manager.filter(library=library).only("id", "original_name"):
        stem = Path(str(row.original_name or "")).stem
        if stem:
            out[int(row.id)] = stem
    return out


# ------------------------------------------------------------------- probe ----


_PROBE_DOCS = {
    "客户信息共享管理办法.md": (
        "# 客户信息共享管理办法\n\n"
        "跨部门共享客户信息，须由需求部门提交申请，经客户归属部门负责人审批；"
        "涉及涉密级别的客户信息，还须信息安全合规岗复核后方可共享。\n"
        "共享范围以最小必要为原则，禁止将客户名单整批导出到个人设备。\n"
    ),
    "数据分级分类规范.md": (
        "# 数据分级分类规范\n\n"
        "数据分为公开、内部、涉密三级。涉密数据必须加密存储，访问需二次审批；"
        "内部数据跨部门使用应登记用途与有效期。\n"
    ),
    "检索服务运维手册.md": (
        "# 检索服务运维手册\n\n"
        "检索服务由稠密向量、正文关键词与数据库关键词三条路融合；"
        "Qdrant collection 按信息库与代次划分，换嵌入模型时必须新建代次并全量重建索引。\n"
    ),
}


def probe(workdir: str | os.PathLike[str] | None = None, repo_root: str | os.PathLike[str] | None = None) -> int:
    """端到端冒烟：migrate → 造库 → seed 3 份 md → 中文查询 → 打印前 3 命中。"""
    try:  # Windows 控制台默认 cp936，中文输出会乱码/抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    repo = Path(repo_root).expanduser().resolve() if repo_root else _DEFAULT_REPO_ROOT
    work = (
        Path(workdir).expanduser().resolve()
        if workdir
        else repo / "artifacts" / "evals" / "probe-harness"
    )
    corpus = work / "corpus-probe"
    corpus.mkdir(parents=True, exist_ok=True)
    for name, body in _PROBE_DOCS.items():
        (corpus / name).write_text(body, encoding="utf-8")

    print(f"[probe] repo_root = {repo}")
    print(f"[probe] workdir   = {work}")
    started = time.perf_counter()
    harness = bootstrap(repo, work, dense=True, fresh=False)
    boot_ms = round((time.perf_counter() - started) * 1000.0, 1)
    print(
        f"[probe] bootstrap {boot_ms} ms · library={harness.library.code} "
        f"collection={harness.collection} generation={harness.generation} "
        f"dim={harness.dimension} dense={harness.dense} rerank={harness.rerank}"
    )
    for w in harness.warnings:
        print(f"[probe] warning: {w}")

    mapping = harness.seed_corpus(corpus)
    print(f"[probe] seeded {len(mapping)} docs: {mapping}")

    query = "跨部门共享客户信息需要哪级审批"
    out = harness.search(query, limit=10)
    report = out["report"]
    print(f"[probe] query = {query!r} · elapsed_ms = {out['elapsed_ms']}")
    print(
        "[probe] hits: dense={} sparse={} db_keyword={} fused={} collections={}".format(
            report.get("dense_hits"),
            report.get("sparse_hits"),
            report.get("db_keyword_hits"),
            report.get("fused_hits"),
            report.get("collections"),
        )
    )
    print(
        "[probe] flags: degraded={!r} dense_skipped={!r} local_mode_sparse_unsupported={!r} "
        "test_adapter={!r}".format(
            report.get("degraded"),
            report.get("dense_skipped_by_breaker"),
            report.get("local_mode_sparse_unsupported"),
            report.get("test_adapter"),
        )
    )
    for w in out["warnings"]:
        print(f"[probe] warning: {w}")
    for i, hit in enumerate(out["results"][:3], 1):
        text = (hit["text"] or "").replace("\n", " ")[:60]
        print(
            f"[probe] #{i} doc_id={hit['doc_id']} score={hit['score']:.6f} "
            f"file_id={hit['file_id']} name={hit['original_name']} text={text}"
        )
    if not out["results"]:
        print("[probe] 失败：没有命中任何结果")
        harness.close()
        return 2

    # 同进程二次检索：验证确定性（同 query 同参数应完全一致）
    again = harness.search(query, limit=10)
    same = [
        (h["doc_id"], h["file_id"], round(float(h["score"]), 6)) for h in out["results"]
    ] == [(h["doc_id"], h["file_id"], round(float(h["score"]), 6)) for h in again["results"]]
    print(f"[probe] determinism(same-process) = {same}")
    harness.close()
    if not same:
        print("[probe] 失败：两次检索结果不一致")
        return 3
    print("[probe] OK")
    return 0


def _main(argv: list[str]) -> int:
    if "--probe" in argv:
        workdir = None
        repo_root = None
        for i, arg in enumerate(argv):
            if arg == "--workdir" and i + 1 < len(argv):
                workdir = argv[i + 1]
            elif arg == "--repo" and i + 1 < len(argv):
                repo_root = argv[i + 1]
        return probe(workdir=workdir, repo_root=repo_root)
    print(
        "用法: python evals/kb_eval/harness.py --probe "
        "[--repo <被测仓库根>] [--workdir <仓库内运行目录>]"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
