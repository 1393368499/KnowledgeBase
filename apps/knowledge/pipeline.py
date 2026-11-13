"""入库流水线：原始字节 → 解析分段 → 真实嵌入 → Qdrant 写入。

一次 :func:`process_file` 的完整步骤（每一步都真实执行，可单独观测）：

1. 读取 ``data/media/raw/<sha256>`` 的原始字节，**重算哈希**与库里的 ``content_sha256``
   比对；不一致直接失败（说明磁盘上的事实来源被改过）。
2. 调 :func:`apps.knowledge.parsers.parse_bytes` 得到分段（PDF 扫描页走 AS711 OCR）。
3. 把分段写入 ``DocumentChunk``（append-only；重复解析不会产生第二份，见下）。
4. 调 AS711 嵌入服务，**逐条校验维度**；得到 1024 维向量。
5. 写入该库当前 generation 的 Qdrant collection，payload 带 ``permission_code``。

"失败重试不重复、不改来源"
--------------------------
- 原始字节按内容寻址，重试时 ``store_stream`` 命中同一个路径并校验复用（reused=True），
  **不覆盖、不新增副本**；
- 分段写入前先看该资料已有多少段：解析成功的段数一致就跳过（幂等），
  不一致则拒绝（说明解析器版本变了，必须走 generation 重建，不能原地改已入库内容）；
- 向量点 id 用 ``uuid5(f"kb:{file.pk}:{ordinal}")``：重试是 upsert 同一个点，
  不会产生第二份向量；
- 嵌入失败时 ``vector_status`` 记为 ``partial`` 并记录已写入数量，下次续写。
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.audit.services import append_event

from . import parsers, storage, vectorindex, progress
from .embedding import EmbeddingError, embed_for_indexing, embed_texts
from .models import DocumentChunk, IngestedFile, ParseStatus, VectorCollection
from governance.immutable import ImmutableRecordError
from .ocr import OcrClient
from .parsers import ParsedBlock
from .rerank import RerankClient
from .resilience import ServiceUnavailable
from .vectorindex import VectorPoint
from .writegate import WriteFrozen

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """流水线失败（可重试）。"""


class SourceChanged(PipelineError):
    """磁盘上的原始字节与库里的指纹不一致——事实来源被改动。"""


@dataclass
class StepResult:
    """每一步的实测结果，直接写进审计事件与 API 响应，便于验收核对。"""

    ok: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)


#: 向量库客户端工厂。测试可以替换它来复用同一个客户端实例
#: （本地嵌入模式的 Qdrant 由文件锁串行化，同一目录不能开第二个客户端）。
QDRANT_CLIENT_FACTORY = vectorindex.build_client


def build_qdrant_client():
    """统一从这里造客户端：测试通过替换 :data:`QDRANT_CLIENT_FACTORY` 注入。"""
    return QDRANT_CLIENT_FACTORY()


class LeaseNotAcquired(PipelineError):
    """这份资料正在被别的 worker 处理（或租约还没过期）——本次不去抢。"""


def _lease_seconds() -> int:
    return int(getattr(settings, "KB_PROCESS_LEASE_SECONDS", 900))


class AlreadyProcessed(PipelineError):
    """这份资料**已经处理完成**，本次调用直接跳过（不改状态、不耗尝试号）。"""


class BudgetExhausted(PipelineError):
    """尝试次数已用尽：需要人工介入（**claim 未生效，不会留下 RUNNING**）。"""


class StaleGeneration(PipelineError):
    """本 job 手里的代次已经过期：库指针在它跑模型 IO 期间被切换/冻结。

    这是**拒绝写入**而不是"照旧写回"的语义：本次向量已经写进了旧代次的
    collection，把档案指针写回旧代会让刚切换上线的新索引立刻漏掉这份资料。
    正确处置是让调用方在新代次上重跑一次增量索引。
    """


def acquire_lease(
    file: IngestedFile,
    *,
    token: str = "",
    force: bool = False,
    consume_budget: bool = True,
    allow_archived: bool = False,
) -> str:
    """用**数据库 CAS** 抢一份处理租约；条件不满足时抛对应异常。

    ``WHERE`` 条件一次涵盖全部"能不能处理"的判定（审查意见）：

    - 租约空闲或已过期（始终要求）；
    - ``parse_status <> DONE``——**除非** ``force``（重试/重建要能重跑已完成资料）；
    - 尝试次数未超上限——**除非** ``consume_budget=False``；
    - ``parse_status <> ARCHIVED``——**除非** ``allow_archived``。

    **为什么不能"先无条件抢下来再检查"**：那样一个"读到 PENDING 的旧实例"
    会在另一个 worker 已经把它跑成 DONE 之后，仍然把状态改回 RUNNING 并把
    尝试号 +1，然后重新解析一遍——DONE 检查永远失效。把条件放进 CAS 之后，
    这种情况**命中 0 行**，我们再按当前真实状态给出"已完成/预算用尽/被占用"
    的准确结论，且**不改动任何字段**。

    三个开关的分工（很重要）：

    - ``force``：允许处理已完成资料（重试、换模型重建）；
    - ``consume_budget``：本次是否算"一次处理尝试"。
      普通重试 = ``force=True, consume_budget=True``（失败要计入上限，
      否则坏文件会无限重试）；**重建** = ``force=True, consume_budget=False``
      （重建只是换一套向量，不该吃解析失败预算，否则换几次模型就永远处理不了）；
    - ``allow_archived``：**唯一**允许把已归档资料变回 RUNNING 的通道。
      它**不是** HTTP 参数能打开的东西：只有受控服务 ``planner.unarchive_file``
      （library:admin + 确认 + 审计）在内部传 ``True``。普通 ``process`` 接口
      （writer 权限即可）、``parse_file``（library:write）与后台周期任务都不传，
      因此**不能绕过恢复入口**——否则 writer 只要 ``?force=true`` 就能反归档。
      放进 CAS 条件里还能挡住"归档与处理同时发生"的竞态。
    """
    token = token or uuid.uuid4().hex
    now = timezone.now()
    expires = now + timedelta(seconds=_lease_seconds())
    model = type(file)

    queryset = (
        model._base_manager.filter(pk=file.pk)
        .filter(
            Q(parse_lease_token="")
            | Q(parse_lease_expires_at__lt=now)
            | Q(parse_lease_expires_at__isnull=True)
        )
    )
    if not force:
        queryset = queryset.exclude(parse_status=ParseStatus.DONE)
    if not allow_archived:
        # 归档件只能通过受控恢复重新处理（见 docstring）
        queryset = queryset.exclude(parse_status=ParseStatus.ARCHIVED)
    if consume_budget:
        queryset = queryset.filter(parse_attempts__lt=IngestedFile.MAX_PARSE_ATTEMPTS)

    fields: dict = {
        "parse_lease_token": token,
        "parse_lease_started_at": now,
        "parse_lease_expires_at": expires,
        "parse_status": ParseStatus.RUNNING,
        "parse_error": "",
    }
    if consume_budget:
        fields["parse_attempts"] = F("parse_attempts") + 1

    # **gate 锁与租约 CAS 必须在同一个外层事务里**（增量审查要求）：冻结方在同一
    # 行锁上判定，因此"冻结生效后仍有 worker 领到租约"在数据库层面不可能发生。
    # 冻结期间**显式拒绝**，并且**不触碰任何字段**（现有 lease 原样保留，不抢占）。
    from django.db import transaction as _tx

    from .writegate import assert_writable_locked

    with _tx.atomic():
        assert_writable_locked(file.library_id, action=f"资料 #{file.pk} 领取解析租约")
        updated = queryset.update(**fields)
    if updated:
        file.refresh_from_db()
        return token

    # 没抢到：读当前真实状态，给出**准确**原因（一律不改任何字段）
    current = (
        model._base_manager.filter(pk=file.pk)
        .values("parse_status", "parse_attempts", "parse_lease_token", "parse_lease_expires_at")
        .first()
        or {}
    )
    if not current:
        raise PipelineError(f"资料 #{file.pk} 不存在")
    if not force and current["parse_status"] == ParseStatus.DONE:
        raise AlreadyProcessed(f"资料 #{file.pk} 已处理完成，本次跳过")
    if not allow_archived and current["parse_status"] == ParseStatus.ARCHIVED:
        # **明确拒绝并指路**：归档件只能通过受控恢复入口重新处理，
        # 普通解析/重试（writer 权限即可）与后台任务都不得绕过它。
        raise PipelineError(
            f"资料 #{file.pk} 已归档：归档资料只能通过受控「恢复归档」重新处理"
            "（需要该库管理员权限 + 一次独立确认 + 审计），"
            "普通的解析/重试入口不会恢复它。"
        )
    if consume_budget and int(current["parse_attempts"] or 0) >= IngestedFile.MAX_PARSE_ATTEMPTS:
        raise BudgetExhausted(
            f"资料 #{file.pk} 已尝试 {current['parse_attempts']} 次，达到上限 "
            f"{IngestedFile.MAX_PARSE_ATTEMPTS}；需人工介入"
            "（本次未改动状态，资料不会卡在 RUNNING）"
        )
    if current["parse_lease_token"]:
        raise LeaseNotAcquired(
            f"资料 #{file.pk} 正在被处理（租约持有者 "
            f"{str(current['parse_lease_token'])[:8]}…，"
            f"到期 {current['parse_lease_expires_at']}），本次跳过"
        )
    raise LeaseNotAcquired(
        f"资料 #{file.pk} 当前状态 {current['parse_status']} 不可处理，本次跳过"
    )


def release_lease(file: IngestedFile, token: str) -> bool:
    """按 token 释放租约（条件写：只有持有者能释放）。"""
    model = type(file)
    updated = model._base_manager.filter(pk=file.pk, parse_lease_token=token).update(
        parse_lease_token="", parse_lease_started_at=None, parse_lease_expires_at=None
    )
    return bool(updated)


def lease_still_held(file: IngestedFile, token: str) -> bool:
    """确认"这份租约仍然属于我且没过期"。

    **所有**状态写入前都要过这道检查：否则一个过期被接管的旧 worker 醒来之后，
    会把新 owner 的 RUNNING/DONE 覆盖成自己的结果（审查方指出的竞态）。
    """
    if not token:
        return False
    row = (
        type(file)
        ._base_manager.filter(pk=file.pk, parse_lease_token=token)
        .values("parse_lease_expires_at")
        .first()
    )
    if row is None:
        return False
    expires = row["parse_lease_expires_at"]
    return expires is None or expires > timezone.now()


def renew_lease(file: IngestedFile, token: str) -> bool:
    """续租：长任务（大文件解析、批量嵌入）期间保持持有权。

    **不得复活已过期的 token**：WHERE 里带 ``expires_at > now``，
    所以"已经过期、可能已被别人接管"的租约不能靠续租抢回来——
    那种情况下返回 False，调用方必须立刻停手（抛 :class:`LeaseLost`）。
    """
    if not token:
        return False
    model = type(file)
    now = timezone.now()
    updated = model._base_manager.filter(
        pk=file.pk, parse_lease_token=token, parse_lease_expires_at__gt=now
    ).update(parse_lease_expires_at=now + timedelta(seconds=_lease_seconds()))
    return bool(updated)


def assert_lease(file: IngestedFile, token: str) -> None:
    """确认租约仍然属于我且未过期；否则抛 :class:`LeaseLost`。"""
    if not renew_lease(file, token):
        raise LeaseLost(
            f"资料 #{file.pk} 的租约已过期或被接管，停止处理以免覆盖新持有者"
        )


class LeaseLost(PipelineError):
    """租约已被别人接管：必须立刻停止写入，避免覆盖新 owner 的状态。"""


def _guarded_status(file: IngestedFile, token: str, *args, **kwargs):
    """带租约校验的状态写入：**一条 UPDATE 完成核对 + 写入**（无 TOCTOU）。

    早先是"先 SELECT 看租约 → 再无条件 UPDATE"，两步之间租约可能被接管，
    旧 worker 仍然会把状态写进去（典型 TOCTOU）。现在改成一条 CAS：

        UPDATE ... SET <状态字段> WHERE pk = ? AND lease_token = ? AND expires_at > now

    用**命中行数 == 1** 判定成功。跑 0 行就说明租约已经不是我的了，
    抛 :class:`LeaseLost` 让对方（新 owner）的结果不被覆盖。

    状态机的合法性检查（``update_parse_status`` 里的流转校验）仍在，
    但它在 UPDATE 之前用**已经刷新过的实例**判断，并且把旧 status 也作为
    UPDATE 的 WHERE 条件，确保"我检查的那一行"就是"我写的那一行"。
    """
    if not token:
        # 没有租约 token = 这条路径**不在租约保护下**（单元测试直接调 embed_chunks、
        # 或引导阶段的一次性写入）。回落到模型自己的受控单行更新：仍是
        # append-only 白名单 + 触发器守卫，只是不做跨 worker 互斥——
        # 而互斥本来就是租约的职责。
        file.refresh_from_db()
        file.update_parse_status(args[0] if args else "", **kwargs)
        file.refresh_from_db()
        return file
    file.refresh_from_db()  # 拿到最新的 parse_status 做流转判断
    previous_status = file.parse_status
    error = kwargs.get("error", "")
    # 用模型里的纯逻辑算字段与流转校验（会写内存实例，不落库）
    fields = file._prepare_controlled_fields(args[0] if args else "", **kwargs)
    now = timezone.now()
    updated = (
        type(file)
        ._base_manager.filter(
            pk=file.pk,
            parse_lease_token=token,
            parse_lease_expires_at__gt=now,
            parse_status=previous_status,
        )
        .update(**fields)
    )
    if updated != 1:
        raise LeaseLost(
            f"资料 #{file.pk} 的租约已失效或被接管（CAS 命中 {updated} 行），"
            f"停止写入以免覆盖新持有者的状态（原错误：{error}）"
        )
    file.refresh_from_db()
    return file


@contextmanager
def hold_lease(
    file: IngestedFile,
    *,
    token: str = "",
    force: bool = False,
    consume_budget: bool = True,
    allow_archived: bool = False,
):
    """处理期间的租约上下文：条件不满足时抛对应异常（见 :func:`acquire_lease`）。

    **没有"抢不到也继续跑"的开关**（审查方要求删除这个后门）：抢不到就意味着
    有别的 worker 在写同一份资料，继续跑只会产生两份互相覆盖的结果。
    """
    held = acquire_lease(
        file,
        token=token,
        force=force,
        consume_budget=consume_budget,
        allow_archived=allow_archived,
    )
    try:
        yield held
    finally:
        release_lease(file, held)


# ------------------------------------------------------------------ 解析 ----


def parse_stored_file(
    file: IngestedFile,
    *,
    ocr=None,
    max_ocr_pages: int | None = None,
) -> tuple[list[ParsedBlock], StepResult]:
    """读原始字节 → 校验哈希 → 解析。不写数据库。"""
    raw = storage.read_blob(file.content_sha256)  # 内部会重算哈希，损坏即抛
    progress.report('extracting', '正在解析正文')
    actual_size = len(raw)
    if actual_size != (file.raw_size_bytes or file.size_bytes):
        # raw_size_bytes 为 0 表示还没落盘记录，此时以 DB 里的 size_bytes 为口径
        if file.raw_size_bytes:
            raise SourceChanged(
                f"原始字节大小 {actual_size} 与入库记录 {file.raw_size_bytes} 不一致"
            )
    if max_ocr_pages is None:
        max_ocr_pages = int(getattr(settings, "KB_OCR_MAX_PAGES", 200))
    result = parsers.parse_bytes(
        raw, file.original_name, ocr=ocr, ocr_pages=max_ocr_pages
    )
    step = StepResult(
        ok=True,
        detail=f"解析完成（{result.parser}）",
        metrics={
            "parser": result.parser,
            "blocks": len(result.blocks),
            "chars": result.text_chars,
            "used_ocr": result.used_ocr,
        },
    )
    return result.blocks, step


# ------------------------------------------------------------------ 分段落库 ----


def write_chunks(file: IngestedFile, blocks: list[ParsedBlock]) -> StepResult:
    """把解析分段写进 ``DocumentChunk``（原子 + 幂等）。

    两条硬要求（审查意见 4）：

    1. **原子**：整批段落在**一个事务**里写完。旧实现逐条 ``save()`` 且不带事务，
       中途出错会留下"半份分段"——而分段是 append-only，补不回来，重试还会因为
       数量不等而永远失败。
    2. **按内容判等，不只看数量**：重试时必须逐段核对 ``text`` / ``page`` / ``ordinal``。
       只比数量的话，"解析器改了行为、段数恰好相同"会被当成"已写过"而跳过，
       索引里留下的就是过期内容。
    """
    existing = list(file.chunks.order_by("ordinal"))
    if existing and not blocks:
        # 重建/换模型重嵌入：分段已经解析好且不可变，**直接复用**，
        # 不重新解析、也不消耗解析失败重试预算（审查意见 21:15 第 4 条）。
        return StepResult(
            ok=True,
            detail=f"复用已入库的 {len(existing)} 段（重建不重新解析）",
            metrics={"chunks": len(existing), "reused": True},
        )
    if existing:
        mismatch = _first_chunk_mismatch(existing, blocks)
        if mismatch is None:
            return StepResult(
                ok=True,
                detail="分段已存在且内容一致，跳过（幂等）",
                metrics={"chunks": len(existing), "idempotent_skip": True},
            )
        # 不原地改已入库内容：要么走 generation 重建，要么人工处理
        raise PipelineError(
            f"该资料已有 {len(existing)} 段，与本次解析结果不一致（{mismatch}）。"
            "已入库分段是 append-only，不能原地替换；请走索引重建流程。"
        )

    with transaction.atomic():
        DocumentChunk.objects.bulk_create(
            [
                DocumentChunk(
                    file=file,
                    ordinal=index,
                    text=block.text,
                    page=block.page,
                    token_estimate=max(1, len(block.text) // 2),
                    permission_code=file.permission_code,
                )
                for index, block in enumerate(blocks)
            ]
        )
    return StepResult(ok=True, detail=f"写入 {len(blocks)} 段", metrics={"chunks": len(blocks)})


def _first_chunk_mismatch(existing: list, blocks: list[ParsedBlock]) -> str | None:
    """逐段核对；一致返回 None，不一致返回人类可读的第一处差异。"""
    if len(existing) != len(blocks):
        return f"段数 {len(existing)} ≠ {len(blocks)}"
    for chunk, block in zip(existing, blocks, strict=True):
        if chunk.ordinal != block.order:
            return f"第 {chunk.pk} 段序号 {chunk.ordinal} ≠ {block.order}"
        if chunk.text != block.text:
            return f"第 {chunk.ordinal} 段文本不一致"
        if chunk.page != block.page:
            return f"第 {chunk.ordinal} 段页码 {chunk.page} ≠ {block.page}"
    return None


# ------------------------------------------------------------------ 嵌入+索引 ----


def embed_chunks(
    file: IngestedFile,
    *,
    profile=None,
    on_progress=None,
    client=None,
    http_client=None,
    target_collection: str = "",
    target_generation: int = 0,
    lease_token: str = "",
    write_permission: dict | None = None,
) -> StepResult:
    """调 AS711 真实嵌入并写入 Qdrant。维度不符由 embedding 层显式拒绝。

    ``client`` / ``http_client`` 只用于测试注入：生产路径两者都为 ``None``，
    由本函数自己构造并在结束时关闭。

    ``target_collection`` / ``target_generation``：**重建到新 generation 时使用**。
    给了它们就只往目标 collection 写点，**不改 file 的当前指针，也不动
    ``embedded_chunk_count``**——因为那一代还没被校验、也没被切换。
    只有锁库事务里校验通过、指针切换之后，"当前指向/已完成计数"才一起更新
    （见 :func:`apps.knowledge.vectorindex.activate_generation`）。

    ``write_permission``：**仅供内部调用方（``_process_file_locked``）使用的出参字典**。
    非重建路径会把"本次写入所依据的库级许可"放进去（``fence_token`` /
    ``collection`` / ``generation``），让**紧随其后的完成写入**能在**同一把许可**下
    再校验一次（见 :func:`_process_file_locked`）。
    刻意不做成返回值字段：StepResult 的 metrics 会进审计 payload 与 HTTP 报告，
    闸门令牌不该出现在那些地方。
    """
    from .embedding import profile_for_registration

    chunks = list(file.chunks.order_by("ordinal"))
    if not chunks:
        raise PipelineError("没有分段可嵌入（请先解析）")

    #: 非重建路径的**库级写许可**（闸门令牌）。重建路径为 ""：
    #: 目标代次写入由 ``rebuild_library`` 自己的 freeze owner 负责（见其 ``assert_fence``）。
    #: 空串在 ``assert_writable_locked`` 里是"**提供了一个令牌**"，永远不可能等于
    #: 当前令牌（闸门初建即非空），所以一旦被误用到重建路径也是 **fail-closed**。
    write_token: str = ""
    rebuilding = bool(target_collection)
    if rebuilding:
        collection = target_collection
        generation = target_generation
        if profile is None:
            # 重建必须显式给出目标 profile：否则"重建到哪一代、用哪个模型"没有依据
            raise PipelineError(
                "重建（给了 target_collection）必须显式传入目标嵌入 profile，"
                "拒绝用全局 active profile 猜目标代次的模型。"
            )
        prof = profile
        # ---- 目标代次的身份校验（审查要求②）----
        #
        # 只拿到一个 collection 名字**不能**证明它就是"本库这一代"的索引：
        # 名字可能是别的库的、可能是未登记的孤儿集合、也可能登记的是另一套
        # 模型/维度/端点（同维不同模型）。任何一种都会把向量写进错误的索引——
        # 而且是**静默**写进去，检索时才表现为"莫名其妙搜不到"。
        _target_row = (
            VectorCollection.objects.filter(
                library_id=file.library_id, name=target_collection
            )
            .values("profile", "embedding_model", "model_version", "dimension", "base_url")
            .first()
        )
        if _target_row is None:
            raise PipelineError(
                f"目标 collection {target_collection!r} 不是信息库 #{file.library_id} "
                "已登记的代次：拒绝写入（不接受跨库或未登记的目标索引）"
            )
        _mismatch: list[str] = []
        if (prof.name or "") != (_target_row.get("profile") or ""):
            _mismatch.append(f"profile {prof.name!r} vs {_target_row.get('profile')!r}")
        if int(prof.dimension) != int(_target_row.get("dimension") or 0):
            _mismatch.append(f"dimension {prof.dimension} vs {_target_row.get('dimension')}")
        if (prof.model or "") != (_target_row.get("embedding_model") or ""):
            _mismatch.append(f"model {prof.model!r} vs {_target_row.get('embedding_model')!r}")
        if (prof.model_version or "") != (_target_row.get("model_version") or ""):
            _mismatch.append(
                f"model_version {prof.model_version!r} vs {_target_row.get('model_version')!r}"
            )
        if (getattr(prof, "base_url", "") or "").rstrip("/") != (
            _target_row.get("base_url") or ""
        ).rstrip("/"):
            _mismatch.append("base_url 与目标代次登记端点不一致")
        if _mismatch:
            raise PipelineError(
                f"传入的目标 profile 与目标代次登记快照不一致"
                f"（collection={target_collection!r}）：" + "；".join(_mismatch)
                + "。拒绝写入：重建不得把另一套模型写进这一代。"
            )
    else:
        # **不信任调用方缓存实例上的指针**：切换后一个旧 file 实例的
        # ``vector_collection_name`` 仍指向旧代次，若信它，增量写入（甚至空 pending
        # 时的 `_guarded_status`）会把 DB 指针改回旧代。所以一律**从 DB 重新解析**
        # 当前库指针与代次，再由登记行决定模型。
        #
        # 这两件事放在**同一个短事务**里做：先过写闸门（冻结/被接管即拒绝），
        # 再在持有闸门行锁的情况下读**权威**库指针。这样"检查过的指针"与
        # "决定写哪个 collection"出自同一次持锁读取，中间没有 TOCTOU 窗口。
        # 模型 IO（秒级~分钟级）绝不放进来——它在下面的事务之外执行，
        # 写回之前会**再复核一次**（活指针 + 闸门，见本函数末尾）。
        from .models import KnowledgeLibrary
        from .writegate import assert_writable_locked

        with transaction.atomic():
            # **许可必须被带过模型 IO**（审查要求③）：这里拿到的不只是"当前可写"，
            # 更是"本次写入所依据的那一把令牌"。同一事务里读到的库指针
            # （collection/generation）就是这份许可的内容。
            write_token = assert_writable_locked(
                file.library_id, action=f"资料 #{file.pk} 增量嵌入"
            )
            library_row = (
                KnowledgeLibrary.objects.filter(pk=file.library_id)
                .values("vector_collection", "active_generation")
                .first()
                or {}
            )
        collection = library_row.get("vector_collection") or ""
        generation = int(library_row.get("active_generation") or 0)
        if not collection:
            # 全新库还没有任何已激活的代次：**明确报错**，不要按 generation 猜名字。
            # 猜名字会指向一个不存在/未登记的 collection，然后被安全校验挡住，
            # 报出来的错和真实原因（"这个库还没建索引"）差很远。
            # 正确做法是先跑 `manage.py kbrebuild --library <code>` 建出第一代。
            raise PipelineError(
                f"信息库 #{file.library_id} 还没有可用的向量 collection（未初始化）。"
                "请先执行索引初建：manage.py kbrebuild --library <库code>，"
                "或 POST /api/v1/knowledge/generations/ + 重建流程。"
            )
        if profile is None:
            # **按该 file 所属 collection 的登记快照取模型**，绝不回落全局 active profile：
            # 换模型后旧代次仍会被增量写入，回落会把"新模型的维度/端点"用到旧集合上（串模型）。
            row = (
                VectorCollection.objects.filter(library_id=file.library_id, name=collection)
                .values("profile", "embedding_model", "model_version", "dimension", "base_url")
                .first()
            )
            if row is None:
                raise PipelineError(
                    f"collection {collection!r} 没有登记行，无法确定这一代使用的嵌入模型："
                    "拒绝回落到全局 active profile（否则会把新模型/旧维度写进该代索引）。"
                    "请先执行：manage.py kbrebuild --library <库code>。"
                )
            prof = profile_for_registration(
                profile=row.get("profile") or "",
                model=row.get("embedding_model") or "",
                model_version=row.get("model_version") or "",
                dimension=int(row.get("dimension") or 0),
                base_url=row.get("base_url") or "",
                where=f"信息库 #{file.library_id} 的 collection {collection!r}",
            )
        else:
            # **显式传入 profile 也必须与登记快照严格一致**（同维不同模型/版本会污染
            # 同一 collection）。快照是权威，传入值只作为一致性断言。
            row = (
                VectorCollection.objects.filter(library_id=file.library_id, name=collection)
                .values("profile", "embedding_model", "model_version", "dimension", "base_url")
                .first()
            )
            if row is None:
                raise PipelineError(
                    f"collection {collection!r} 没有登记行：拒绝按调用方传入的 "
                    f"profile {profile.name!r} 写入（无法证明与该代模型一致）"
                )
            mismatches: list[str] = []
            if profile.name != (row.get("profile") or ""):
                mismatches.append(f"profile {profile.name!r} vs {row.get('profile')!r}")
            if int(profile.dimension) != int(row.get("dimension") or 0):
                mismatches.append(
                    f"dimension {profile.dimension} vs {row.get('dimension')}"
                )
            if (profile.model or "") != (row.get("embedding_model") or ""):
                mismatches.append(f"model {profile.model!r} vs {row.get('embedding_model')!r}")
            if (profile.model_version or "") != (row.get("model_version") or ""):
                mismatches.append(
                    f"model_version {profile.model_version!r} vs {row.get('model_version')!r}"
                )
            if (profile.base_url or "").rstrip("/") != (row.get("base_url") or "").rstrip("/"):
                mismatches.append("base_url 与登记快照不一致")
            if mismatches:
                raise PipelineError(
                    f"传入的 profile 与该代登记快照不一致（collection={collection!r}）："
                    + "；".join(mismatches)
                    + "。拒绝写入：同维不同模型不得共用同一 collection。"
                )
            prof = profile
    owns_client = client is None
    qdrant = client or build_qdrant_client()
    try:
        # 已存在的点会被 upsert 覆盖成同一份内容，因此重试是安全的
        done = 0
        used_adapter = False
        batch_size = int(getattr(settings, "KB_EMBED_BATCH_SIZE", 16))
        already = file.embedded_chunk_count or 0
        pending = chunks if rebuilding else [chunk for chunk in chunks if chunk.ordinal >= already]
        progress.report('embedding', '正在建立向量索引', 0 if rebuilding else already, len(chunks), '段')
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            # embed_for_indexing 会在显式打开离线适配器时返回确定性假向量，
            # 并用 collection 名（必须以 __offline_test 结尾）挡住"假向量进正式索引"。
            vectors, used_adapter = embed_for_indexing(
                [chunk.text for chunk in batch],
                collection=collection,
                profile=prof,
                client=http_client,
                # 入库是后台/批处理，允许更长的超时（与"检索要快速降级"分开口径）
                timeout=float(
                    getattr(settings, "KB_EMBED_INDEX_TIMEOUT_SECONDS", 60)
                ),
            )
            points = [
                VectorPoint(
                    point_id=vectorindex.point_id_for(file, chunk.ordinal),
                    vector=vector,
                    payload={
                        "file_id": file.pk,
                        "library_id": file.library_id,
                        "library_code": file.library.code,
                        "ordinal": chunk.ordinal,
                        "page": chunk.page,
                        "text": chunk.text,
                        "permission_code": chunk.permission_code,
                        "original_name": file.original_name,
                        "directory_path": file.directory_path,
                        "classification": file.classification,
                        "customer": file.customer,
                        "department": file.department,
                        "content_sha256": file.content_sha256,
                        # 记录**登记时的模型名**：激活校验会拿它跟登记行比对，
                        # 防止旧模型的点混进新一代。"这是测试适配器算出来的"由
                        # is_test_adapter 标记区分，不混进模型名（否则真/假名字
                        # 不一致会把自己挡住）。
                        "model": prof.model,
                        "model_version": prof.model_version,
                        "generation": generation,
                        # 这条标记保证"假向量算出来的结果"在检索层能被一眼认出
                        "is_test_adapter": used_adapter,
                    },
                )
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            if not rebuilding:
                # **远端写入之前的许可复核**（审查要求③）：模型 IO 已经在锁外跑完，
                # 但"我手里这把许可还有效吗、活指针还是我准备写的那一代吗"必须在
                # upsert **之前**确认。冻结与解除都会**旋转**令牌，所以
                # "冻结又解除、collection 恰好没变"这种旧 job 复活场景也会在这里被挡住
                # （只比较 collection 名字是看不出来的）。
                with transaction.atomic():
                    assert_writable_locked(
                        file.library_id,
                        fence_token=write_token,
                        action=f"资料 #{file.pk} 写向量点",
                    )
                    live = (
                        KnowledgeLibrary.objects.filter(pk=file.library_id)
                        .values("vector_collection")
                        .first()
                        or {}
                    )
                    if (live.get("vector_collection") or "") != collection:
                        raise StaleGeneration(
                            f"资料 #{file.pk} 嵌入期间信息库 #{file.library_id} 的向量指针"
                            f"已从 {collection!r} 变为 {live.get('vector_collection')!r}："
                            "拒绝把向量点写进旧代次"
                        )
            vectorindex.upsert_points(qdrant, collection, points, dimension=prof.dimension)
            done += len(points)
            progress.report('embedding', '正在建立向量索引', done if rebuilding else already + done, len(chunks), '段')
            if on_progress is not None:
                on_progress(done, len(pending))
        if rebuilding:
            # 重建到指定代次：点已写进目标 collection，但**绝不能在这里改这份资料的
            # 当前指针**（``vector_collection_name`` / ``vector_generation``）。
            #
            # 为什么：目标代次还没被激活。若此时把指针改过去，等价于"提前宣布
            # 这份资料的向量已经在目标代次里"——一旦后续 coverage 校验失败
            # （需要回退/重跑），线上指针就指向了一个未激活的代次，
            # 检索会读错索引，而且失败重建会**污染所有当前指针**（审查指出）。
            #
            # 正确口径：重建期间只写点 + 记录"本次重建已完成多少段"这一类进度信息；
            # 指针的统一切换发生在 ``activate_generation`` 的锁库事务里
            # （校验通过后一次性切换库指针与资料指针）。
            #
            # 进度不落在这里的指针字段上：重建进度由返回值/重建报告承载，
            # 需要持久化时写独立的 build 记录，不改"当前生效"的语义字段。
            return StepResult(
                ok=True,
                detail=f"已为 {collection} 写入 {done} 个向量点（等待 coverage 校验后统一切换指针）",
                metrics={
                    "collection": collection,
                    "generation": generation,
                    "dimension": prof.dimension,
                    "model": prof.model,
                    "model_version": prof.model_version,
                    "points": done,
                    "chunks": len(chunks),
                    "rebuild": True,
                    "pointer_switched": False,
                },
            )
        # **写回前复核**（审查要求④）：模型 IO 期间库指针可能已经被切换，
        # 闸门也可能已经冻结/换令牌。所以在**写任何状态之前**再开一个短事务：
        #
        #   1. 重新过闸门（冻结/接管 → 拒绝，绝不覆盖、绝不回退指针）；
        #   2. 在同一行锁下读**活指针**，并且**只写活指针**（不再用 IO 之前读到的
        #      缓存值）。这样即使发生了切换，指针也只会等于当前生效的那一代。
        #
        # 为什么"活指针 ≠ 我写入的那一代"时必须**拒绝**而不是照写：
        # 本次向量已经 upsert 到旧代次的 collection 里，把档案指针写回旧代
        # 等于把这份资料从刚切换上线的新索引上摘下来（检索立刻漏掉它），
        # 而且失败重建/切换后这种回退是**静默**的——只有用户发现"搜不到了"才会暴露。
        with transaction.atomic():
            assert_writable_locked(
                file.library_id,
                # **同一把许可**：从取许可到现在，中间隔着模型 IO；如果期间发生过
                # 冻结（或冻结+解除），令牌已经旋转，这里会直接拒绝写入，
                # 而不是"因为 collection 名字没变就照写"。
                fence_token=write_token,
                action=f"资料 #{file.pk} 嵌入结果写回",
            )
            live_row = (
                KnowledgeLibrary.objects.filter(pk=file.library_id)
                .values("vector_collection", "active_generation")
                .first()
                or {}
            )
            live_collection = live_row.get("vector_collection") or ""
            live_generation = int(live_row.get("active_generation") or 0)
            if live_collection != collection:
                raise StaleGeneration(
                    f"资料 #{file.pk} 嵌入期间信息库 #{file.library_id} 的向量指针已从 "
                    f"{collection!r} 变为 {live_collection!r}：本次向量写在旧代次上，"
                    "拒绝把档案指针写回旧代（请对该资料重跑一次增量索引）"
                )
            # **空 pending 也必须走这里**：一个 chunk 都不用写时，唯一会动的就是
            # 状态与指针字段；用活指针（持闸门行锁读到的那个）写，绝不写回缓存值。
            _guarded_status(
                file,
                lease_token,
                file.parse_status,
                embedded_chunk_count=len(chunks),
                vector_status="indexed",
                # 用**活指针**（持闸门行锁读到的那个），不是 IO 之前的缓存值
                vector_collection=live_collection,
                vector_collection_name=live_collection,
                vector_generation=live_generation or generation,
            )
        if write_permission is not None:
            # 把"本次写入所依据的许可"交给调用方，让紧随其后的完成写入
            # （``_process_file_locked`` 的 DONE 提交）能在**同一把许可**下复核。
            write_permission["fence_token"] = write_token
            write_permission["collection"] = live_collection
            write_permission["generation"] = live_generation or generation
        return StepResult(
            ok=True,
            detail=f"嵌入并写入 {done} 个向量点",
            metrics={
                "collection": live_collection,
                "generation": live_generation or generation,
                "dimension": prof.dimension,
                "model": prof.model,
                "model_version": prof.model_version,
                "points": done,
            },
        )
    except EmbeddingError as exc:
        # 重建路径**只更新目标代次的失败信息**，绝不写线上 file 状态：
        # 目标代次还没激活，把线上资料标成 partial 等于用一次"还没上线的重建失败"
        # 去污染当前可用资料（审查要求②）。目标代次的失败由 rebuild_library 汇总。
        if not rebuilding:
            _write_failure_under_library_permission(
                file,
                write_token=write_token,
                collection=collection,
                lease_token=lease_token,
                vector_status="partial",
                error=f"嵌入失败：{exc}",
            )
        else:
            logger.warning(
                "重建到 %s 失败（资料 #%s）：不改线上状态；错误：%s",
                collection,
                file.pk,
                exc,
            )
        raise PipelineError(f"嵌入失败：{exc}") from exc
    except ServiceUnavailable as exc:
        # 模型服务不可用（含离线模式）：明确标记"不可用"并抛出可重试的流水线错误。
        # 不能让它以原始异常冒出去——上传接口只认 PipelineError，
        # 否则用户看到的是 500，而不是"服务未启用/不可用"这种可操作的信息。
        if not rebuilding:
            _write_failure_under_library_permission(
                file,
                write_token=write_token,
                collection=collection,
                lease_token=lease_token,
                vector_status="unavailable",
                error=f"模型服务不可用：{exc.reason}",
            )
        else:
            logger.warning(
                "重建到 %s 时模型服务不可用（资料 #%s）：不改线上状态；原因：%s",
                collection,
                file.pk,
                exc.reason,
            )
        raise PipelineError(f"模型服务不可用：{exc.reason}") from exc
    finally:
        if owns_client:
            qdrant.close()


# ------------------------------------------------------------------ 全流程 ----


def _write_failure_under_library_permission(
    file: IngestedFile,
    *,
    write_token: str,
    collection: str,
    lease_token: str,
    vector_status: str,
    error: str,
) -> None:
    """在**同一把库级许可**下写失败状态（``partial`` / ``unavailable``）。

    ## 为什么失败写回也要许可（审查要求）

    "只有成功写入需要闸门"是错的。真实场景：一个 worker 的租约**已经过期但 token
    还没被别人清掉**（进程卡住、GC 停顿、模型调用超时很久），它在模型报错时醒来，
    走到失败分支直接写 ``vector_status="partial"`` + ``error=...``。此时：

    * 库可能刚被 freeze 又重新 unfreeze（换代窗口结束）；
    * 或者线上一代已经切到新 collection。

    无论哪种，它写的都是**陈旧状态**：把新 owner / 新一代的线上事实覆盖成
    "partial + 一条旧错误"。所以失败写回与成功写回走**同一套**校验：

    1. 同一个外层事务里先过闸门，并带上**当初取到的那把 ``write_token``**
       （冻结与解除都会旋转令牌 → 陈旧任务必然失败）；
    2. 在同一把行锁下读**活指针**，必须仍是**当初那一代** collection；
    3. ``_guarded_status`` 再做租约 CAS（租约不是我的 → ``LeaseLost``）。

    任何一条不满足 → 抛 :class:`~apps.knowledge.writegate.WriteFrozen` /
    :class:`StaleGeneration` / :class:`LeaseLost`（**陈旧任务冲突**），
    **一个字段都不写**；由调用方原样上抛并审计，绝不改写成 FAILED。
    """
    from .models import KnowledgeLibrary
    from .writegate import assert_writable_locked

    with transaction.atomic():
        assert_writable_locked(
            file.library_id,
            fence_token=write_token,
            action=f"资料 #{file.pk} 写失败状态（{vector_status}）",
        )
        live_row = (
            KnowledgeLibrary.objects.filter(pk=file.library_id)
            .values("vector_collection")
            .first()
            or {}
        )
        live_collection = live_row.get("vector_collection") or ""
        if live_collection != collection:
            raise StaleGeneration(
                f"资料 #{file.pk} 的向量代次已从 {collection!r} 变为 {live_collection!r}："
                f"旧任务的失败状态（{vector_status}）不得写回线上"
            )
        # 最后一步 CAS：租约已经不是我的 → LeaseLost（不写、也不吞）
        _guarded_status(
            file,
            lease_token,
            file.parse_status,
            vector_status=vector_status,
            error=error,
        )


def process_file(
    file: IngestedFile,
    *,
    actor=None,
    ocr=None,
    profile=None,
    force: bool = False,
    client=None,
    http_client=None,
    target_collection: str = "",
    target_generation: int = 0,
    reuse_chunks: bool = False,
    consume_budget: bool = True,
    allow_archived: bool = False,
    classify_client=None,
) -> StepResult:
    """跑完整条流水线。每一步的结果都写审计事件，失败即置 FAILED（可重试）。

    ``client`` / ``http_client`` 只用于测试注入：本地嵌入模式的 Qdrant 由文件锁
    串行化，同一个目录不能开第二个客户端，所以测试必须把同一个客户端传进来。

    并发口径（全部由 :func:`acquire_lease` 的**条件 CAS** 保证）：

    - 资料已是 ``DONE`` 且非 ``force`` → 抛 :class:`AlreadyProcessed`，
      我们把它翻译成"跳过"（**不改状态、不耗尝试号**）；
    - 尝试次数已用尽 → 抛 :class:`BudgetExhausted`，**claim 未生效**，
      资料不会卡在 RUNNING；
    - 被别的 worker 占着 → :class:`LeaseNotAcquired`。

    ``reuse_chunks=True``（重建/换模型）：**复用已入库的只读分段**，不重新解析，
    也不消耗普通重试预算（配合 ``force=True``）。

    ``allow_archived=True`` 是**恢复归档的专用能力**：只有受控服务
    ``planner.unarchive_file``（library:admin + 确认 + 审计）会传它。
    它**不是** HTTP 参数：普通 ``process`` 接口、``parse_file`` 与后台周期任务都不传，
    因此 writer 无法用 ``?force=true`` 绕过恢复入口（见 :func:`acquire_lease`）。
    """
    if file.parse_status == ParseStatus.DONE and not force:
        return StepResult(ok=True, detail="已处理，跳过", metrics={"skipped": True})
    if file.parse_status == ParseStatus.ARCHIVED and not allow_archived:
        # 早退也要挡：抢租约的 CAS 里同样有这道条件（见 acquire_lease），
        # 这里只是让调用方拿到更早、更清楚的错误。
        raise PipelineError(
            f"资料 #{file.pk} 已归档：只能通过受控「恢复归档」重新处理，"
            "普通解析/重试入口不会恢复它。"
        )

    # 条件 claim：状态/预算/租约三项一起判定，命中 0 行时不改动任何字段。
    try:
        with hold_lease(
            file,
            force=force,
            consume_budget=consume_budget,
            allow_archived=allow_archived,
        ) as token:
            with progress.track_file(file, token):
                return _process_file_locked(
                    file,
                    actor=actor,
                    ocr=ocr,
                    profile=profile,
                    client=client,
                    http_client=http_client,
                    classify_client=classify_client,
                    lease_token=token,
                    target_collection=target_collection,
                    target_generation=target_generation,
                    reuse_chunks=reuse_chunks,
                )
    except AlreadyProcessed as exc:
        # 旧实例读到 PENDING、但另一个 worker 已经跑完：这里如实"跳过"
        logger.info("%s", exc)
        file.refresh_from_db()
        return StepResult(ok=True, detail=str(exc), metrics={"skipped": True, "stale": True})


def _auto_classify_parsed(file: IngestedFile, blocks, *, actor=None, client=None) -> dict:
    """解析/OCR 拿到正文后**自动识别资料性质**（同一条流水线，见 classification.py）。

    三条边界（都有测试）：

    1. **主要依正文**：判定输入是刚解析出的段落文本拼起来的正文；文件名只作为
       辅助线索传给模型，规则判定完全不看扩展名（扫描件 OCR 后同样是正文）。
    2. **失败不影响流水线**：识别失败/模型不可用/正文不足只是"待识别 + 原因"，
       **不会**让解析、分段与嵌入失败，也不会把失败强填成「普通文档」；
    3. **人工优先**：`classification_source == "manual"` 时一行都不改
       （由 ``auto_classify_file`` 判定，这里只是把结果放进 metrics）。
    """
    from .classification import auto_classify_file

    text = "\n".join(block.text for block in blocks)
    try:
        outcome = auto_classify_file(
            file, text=text, actor=actor, client=client,
            source_event="file.parse_auto_classify",
        )
    except Exception as exc:  # noqa: BLE001 - 分类绝不阻断解析/分段/嵌入
        logger.warning("自动识别性质失败（不影响解析与索引）：%s", exc, exc_info=True)
        return {
            "status": "error",
            "classification": file.classification or "",
            "source": file.classification_source or "",
            "note": f"自动识别异常：{type(exc).__name__}: {exc}"[:300],
        }
    return {
        "status": outcome.status,
        "classification": outcome.value,
        "source": outcome.source,
        "note": outcome.note,
    }


def _process_file_locked(
    file: IngestedFile,
    *,
    actor=None,
    ocr=None,
    profile=None,
    client=None,
    http_client=None,
    classify_client=None,
    lease_token: str = "",
    target_collection: str = "",
    target_generation: int = 0,
    reuse_chunks: bool = False,
) -> StepResult:
    """真正干活的那段（调用方必须已持有租约）。

    所有状态写入都带**租约 token 校验**：旧 worker 的租约一旦过期被接管，
    它醒来后的完成/失败写入会被拒绝，不会覆盖新持有者的结果。

    **不在这里流转 RUNNING / 递增尝试号**：那两件事已经在 ``acquire_lease``
    的原子 claim 里做完了（同状态 bump 是被模型禁止的，接管场景会因此炸掉）。
    """
    append_event(
        action="file.parse_started",
        actor_id=getattr(actor, "pk", None),
        resource_kind="ingested_file",
        resource_key=str(file.pk),
        payload={"attempt": file.parse_attempts, "original_name": file.original_name},
    )
    try:
        # 默认必须真的创建 OCR 客户端：扫描 PDF / 图片走的就是这条生产默认路径。
        # 旧实现 ocr=None 会把"能识别但没传客户端"变成"解析失败"（审查意见）。
        active_ocr = ocr if ocr is not None else OcrClient()
        if lease_token:
            original_ocr = active_ocr
            class LeaseCheckedOcr:
                def __getattr__(self, name):
                    return getattr(original_ocr, name)

                def recognize_image(self, *args, **kwargs):
                    assert_lease(file, lease_token)
                    text = original_ocr.recognize_image(*args, **kwargs)
                    assert_lease(file, lease_token)
                    return text
            active_ocr = LeaseCheckedOcr()
        # 失败重试复用已经原子保存的分段，避免再次 OCR 的差异与只读分段冲突。
        if not reuse_chunks and file.chunks.exists():
            reuse_chunks = True
        if reuse_chunks:
            # 重建 / 换模型：**复用已入库的只读分段**，不重新解析。
            # 分段是不可变的派生数据，重建只是"换一套向量"，
            # 没有理由再解析一遍原文（也不该碰普通重试预算）。
            from .parsers import ParsedBlock as _ParsedBlock

            existing = list(file.chunks.order_by("ordinal"))
            if not existing:
                raise PipelineError(
                    f"资料 #{file.pk} 没有可复用的分段；重建前请先完成一次解析"
                )
            blocks = [
                _ParsedBlock(order=chunk.ordinal, text=chunk.text, page=chunk.page)
                for chunk in existing
            ]
            parse_step = StepResult(
                ok=True,
                detail=f"复用已入库的 {len(blocks)} 段（重建不重新解析）",
                metrics={"blocks": len(blocks), "reused": True},
            )
        else:
            blocks, parse_step = parse_stored_file(file, ocr=active_ocr)
        progress.report('chunks', '正在保存解析段落', 0, len(blocks), '段')
        if target_collection:
            # **重建：分段只读复用**。不再重写分段表（``write_chunks`` 会碰当前 file
            # 的分段），当前 file 的解析事实/计数在切换事务成功前一概不动。
            chunk_step = StepResult(
                ok=True,
                detail=f"重建复用既有 {len(blocks)} 段（不重写分段）",
                metrics={"chunks": len(blocks), "reused": True, "rewritten": False},
            )
        else:
            chunk_step = write_chunks(file, blocks)
        # **解析成功立刻把分段数落库**。
        #
        # 为什么必须在这里写：`chunk_count` 是"这份资料已解析出多少段"的事实，
        # 它跟"有没有建向量"是两件事。早先只在 embed 成功之后才写，
        # 于是"解析成功但模型离线（向量化不可用）"的资料会显示
        # `chunk_count=0` 而库里其实已经有分段——界面和统计都会骗人
        # （实测：id17 actual chunks=1 但 chunk_count=0）。
        #
        # 这里用受控状态写入（不改 parse_status），失败也不阻断后续向量化尝试。
        #
        # **重建路径不写当前 file 的计数**（审查方缺陷 1）：chunk_count 是"当前生效
        # 代次的解析事实"，重建到新代次失败时绝不能把线上可用资料改成失败/半成品。
        if not target_collection:
            try:
                _guarded_status(
                    file,
                    lease_token,
                    file.parse_status,
                    chunk_count=chunk_step.metrics.get("chunks", len(blocks)),
                )
            except (LeaseLost, ImmutableRecordError):
                logger.warning("写分段计数失败（不影响后续向量化尝试）", exc_info=True)
        else:
            logger.info(
                "重建到 %s：跳过当前 file 的 chunk_count 写入（保留线上解析事实）",
                target_collection,
            )
        # **正文已到手 → 自动识别资料性质**（用户 2026-10-16 要求）。
        #
        # 位置很关键：必须在 embed_chunks **之前**。向量 payload 里带着
        # ``classification``（见 embed_chunks），先判定再嵌入，索引里的性质与库里
        # 当场一致，不需要"补识别之后再重嵌入"。
        #
        # ``reuse_chunks=True``（换模型重建）**不重新判定**：那条路径连原文都不解析，
        # 分类信息的补写交给专门的补识别入口（``manage.py kbclassify``），
        # 重建只做"换一套向量"这一件事。
        classify_metrics: dict = {"status": "skipped", "reason": "重建复用分段，不重新判定"}
        if not reuse_chunks:
            progress.report('classification', '正在识别资料性质')
            classify_metrics = _auto_classify_parsed(
                file, blocks, actor=actor, client=classify_client
            )
        # ``write_permission``：embed_chunks 把"它写回时用的那把库级许可 + 活指针"
        # 交回来（见其 docstring）。完成写入必须在**同一把许可**下复核，
        # 否则"embed 复核通过 → 冻结并解除 → 这里再写 DONE"这条窗口会让旧 job 复活。
        permission: dict = {}
        index_step = embed_chunks(
            file,
            profile=profile,
            client=client,
            http_client=http_client,
            target_collection=target_collection,
            target_generation=target_generation,
            lease_token=lease_token,
            write_permission=permission,
        )
        # 长任务续租：解析 + 嵌入可能跑很久，期间要保持持有权
        renew_lease(file, lease_token)
        progress.report('finalizing', '正在核验并完成入库')
        # **重建路径绝不能改任何"当前生效指针"字段**。
        #
        # 这里是第二处遗漏（第一处在 embed_chunks 里已移除）：完成写入时如果把
        # ``vector_collection`` 指向目标代次，就等于提前宣布"这份资料的向量已经
        # 在目标代次里"。一旦后续 coverage 校验失败需要回退/重跑，线上指针就指向
        # 未激活的 collection，检索读错索引，且失败重建会污染所有当前指针。
        #
        # 重建只更新"产出事实"（分段数/已嵌入段数/向量状态）；
        # 三个指针字段（vector_collection / vector_collection_name /
        # vector_generation）**只在 activate_generation 成功后统一更新**。
        done_fields: dict = {
            "chunk_count": chunk_step.metrics.get("chunks", len(blocks)),
            "embedded_chunk_count": index_step.metrics.get("points", 0),
            "vector_status": "indexed",
        }
        # 是否"写向某个目标代次"（重建）——与 embed_chunks 里的口径保持一致
        rebuilding = bool(target_collection)
        if rebuilding:
            # **重建路径一个线上字段都不写**（审查要求②的收口）：连
            # ``parse_status`` / ``chunk_count`` / ``embedded_chunk_count`` 都不写。
            # 目标代次还没激活，这份资料"当前生效的解析事实"必须原样保留：
            # 一旦这里写成 DONE/FAILED 或改了计数，失败重建就会污染线上可用资料。
            # 产出事实由 ``index_step.metrics`` 与目标代次登记行承载。
            logger.info(
                "重建到 %s：不写资料 #%s 的任何线上状态（仅写目标代次向量点）",
                target_collection,
                file.pk,
            )
        else:
            # **完成写入也要复核活指针**（审查要求④，第二处遗漏）：
            # ``index_step.metrics['collection']`` 是 embed_chunks 复核过的值，
            # 但"复核"与"这次写 DONE"之间仍可能有切换/冻结。所以这里再开一个短事务：
            # 过闸门 + 读活指针 → 只写活指针。绝不把 IO 之前缓存的那一代写回去。
            from .models import KnowledgeLibrary
            from .writegate import assert_writable_locked

            with transaction.atomic():
                assert_writable_locked(
                    file.library_id,
                    # **与 embed_chunks 用的是同一把许可**：中间还隔着
                    # renew_lease 与分类等步骤，令牌一旦被旋转（冻结/解除）就拒绝。
                    fence_token=permission.get("fence_token", ""),
                    action=f"资料 #{file.pk} 标记完成",
                )
                live_row = (
                    KnowledgeLibrary.objects.filter(pk=file.library_id)
                    .values("vector_collection", "active_generation")
                    .first()
                    or {}
                )
                live_collection = live_row.get("vector_collection") or ""
                live_generation = int(live_row.get("active_generation") or 0)
                if live_collection != index_step.metrics.get("collection", ""):
                    raise StaleGeneration(
                        f"资料 #{file.pk} 完成写入期间信息库 #{file.library_id} 的向量指针"
                        f"已变为 {live_collection!r}（本次嵌入写的是 "
                        f"{index_step.metrics.get('collection')!r}）：拒绝写回旧代次"
                    )
                if live_collection != permission.get("collection", ""):
                    raise StaleGeneration(
                        f"资料 #{file.pk} 的库级许可已过期（许可指向 "
                        f"{permission.get('collection')!r}，活指针是 {live_collection!r}）："
                        "拒绝在失效许可下写回指针"
                    )
                done_fields["vector_collection"] = live_collection
                done_fields["vector_collection_name"] = live_collection
                done_fields["vector_generation"] = live_generation
                _guarded_status(file, lease_token, ParseStatus.DONE, **done_fields)
        append_event(
            action="file.indexed",
            actor_id=getattr(actor, "pk", None),
            resource_kind="ingested_file",
            resource_key=str(file.pk),
            payload={
                "original_name": file.original_name,
                "parse": parse_step.metrics,
                "chunks": chunk_step.metrics,
                "index": index_step.metrics,
                # 自动识别的结果也写进审计：性质是"怎么来的"必须可复盘
                "classification": classify_metrics,
                "parsed_at": timezone.now().isoformat(),
            },
        )
        return StepResult(
            ok=True,
            detail="解析并建立索引完成",
            metrics={
                "parse": parse_step.metrics,
                "chunks": chunk_step.metrics,
                "index": index_step.metrics,
                "classification": classify_metrics,
            },
        )
    except (WriteFrozen, StaleGeneration, LeaseLost) as exc:
        # **陈旧任务冲突**（审查要求）：不是"这份资料处理失败"，而是"它的许可/代次/
        # 租约已经作废"。处理方式必须是**停止写入 + 留痕 + 原样上抛**：
        #
        # * 绝不写 FAILED —— 那会把一份完全正常的资料标成失败，甚至覆盖新 owner
        #   已经写好的成功状态；
        # * 原样上抛（不裹成 PipelineError）—— 上层才能把它映射成 409/跳过，
        #   而不是 400 "资料处理失败"。
        append_event(
            action="file.write_conflict_skipped",
            actor_id=getattr(actor, "pk", None),
            resource_kind="ingested_file",
            resource_key=str(file.pk),
            payload={
                "original_name": file.original_name,
                "library_id": file.library_id,
                "conflict": type(exc).__name__,
                "reason": str(exc)[:300],
                "note": "陈旧任务冲突：未改写任何线上状态（含 FAILED）",
            },
        )
        logger.warning(
            "资料 #%s 的写入许可已失效（%s），跳过所有状态写入：%s",
            file.pk,
            type(exc).__name__,
            exc,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须落到 FAILED，不能卡 RUNNING
        # 这里刻意兜住**所有**异常（不只是我们自己定义的几类）：
        # Qdrant 客户端的网络异常、json 解析、第三方库的奇怪异常……
        # 只要没被前面处理掉，就必须把状态写成 FAILED 并留痕，
        # 否则资料会永久停在 RUNNING，既不会被重试也不会被发现（审查意见）。
        reason = f"{type(exc).__name__}: {exc}"
        try:
            # 失败也要带 token：租约被接管后，旧 owner 不该把新 owner 的
            # RUNNING 状态改成 FAILED（那会把新 owner 的成功结果抹掉）。
            _guarded_status(file, lease_token, ParseStatus.FAILED, error=reason)
        except LeaseLost:
            logger.warning("租约已失效，跳过 FAILED 写入（原始错误：%s）", reason)
        except Exception:  # noqa: BLE001 - 状态写不进去也不能掩盖原始错误
            logger.exception("写 FAILED 状态失败（原始错误：%s）", reason)
        append_event(
            action="file.parse_failed",
            actor_id=getattr(actor, "pk", None),
            resource_kind="ingested_file",
            resource_key=str(file.pk),
            payload={
                "original_name": file.original_name,
                "error": reason,
                "error_type": type(exc).__name__,
            },
        )
        raise PipelineError(reason) from exc


def ensure_library_index(library, *, profile=None, client=None) -> dict:
    """确保这个库有可用的 collection：没有就**自动初建**第一代。

    为什么必须有它（审查者指出的循环依赖）：
    新库 ``active_generation = 0``、没有任何登记行 → 首上传时"无处可写"；
    如果要求"上传前先重建空库"，就成了"没资料建不了索引、没索引不能上传"的死循环。

    正确顺序是：**先落盘 + 建记录，再确保库有 collection**。
    第一代本来就是空的，直接指过去没有"空索引上线"的风险——它马上就会被这份
    新资料写入；换模型的全量重建仍走 :func:`rebuild_library`（那边有严格的
    coverage 校验）。

    **并发修复（4 并发首上传实测暴露的真实竞态）**
    ------------------------------------------------
    旧实现是典型的 check-then-act，没有锁：

    1. 4 个并发请求各自 ``refresh_from_db()``，都看到 ``vector_collection == ""``；
    2. 于是各自 ``prepare_generation()`` → 各占一个号（实测造出 g1/g2/g3/g4）；
    3. 各自把**库指针**写成自己的代次（后写者覆盖前者），再
       ``update(is_active=True)``；偏唯一索引
       ``uniq_active_collection_per_library`` 只允许一行 active
       → 后三个抛 ``IntegrityError`` → **HTTP 500**；
    4. 而且指针与登记表**不一致**：指针指向 g4，登记表里却是 g1 active。

    现在改为**在库行锁下原子决定唯一 winner**：

    - 全程持有 ``select_for_update()`` 的库行锁，拿锁**后重新检查**——
      已经有可用 collection 就直接复用（``initialized=False``），
      不再 mint 新代次，也**绝不覆盖**已有的指针；
    - 激活时**显式把该库其它登记行降级**，不把唯一性交给约束报错兜底；
    - 指针（``active_generation``/``vector_collection``）与登记行
      ``is_active`` 在**同一个事务**里一起改，保证两者一致；
    - 万一仍有并发穿过来（例如锁被绕过），捕获 ``IntegrityError`` 后
      **重读 winner 并复用**，调用方看到的仍是 2xx + 一致的 active，
      而不是 500。

    编号口径不变：``prepare_generation`` 严格单调占号、编号**永不复用**，
    失败代次标 ``failed`` 留痕。
    """
    from .embedding import active_profile
    from .models import KnowledgeLibrary, VectorCollection

    prof = profile or active_profile()

    def _reuse(registered, *, initialized: bool = False, note: str = "") -> dict:
        payload = {
            "initialized": initialized,
            "collection": registered.name,
            "generation": _generation_from_name(registered.name),
            "dimension": registered.dimension,
            "build_status": registered.build_status,
        }
        if note:
            payload["note"] = note
        return payload

    # ---- 0) 快速路径：已经有可用 winner 就直接复用（不 mint 候选）----
    #
    # 这一段**只读 + 短事务**，不做任何远端 I/O。
    with transaction.atomic():
        locked = KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
        if locked.vector_collection:
            registered = VectorCollection.objects.filter(
                library=locked, name=locked.vector_collection
            ).first()
            if registered is not None:
                _reconcile_active_flag(locked, registered)
                return _reuse(registered)

    # ---- 1) 候选：**必须在事务之外**完成"占号 + 建远端"----
    #
    # 这是本函数最关键的顺序约束。我上一版把 ``prepare_generation`` 包在外层
    # ``transaction.atomic()`` 里，那是**真实的占号回退缺陷**：
    #
    # ``prepare_generation`` 内部用 ``transaction.atomic()`` 提交"占号 + 登记行"，
    # 然后才去建远端集合。被外层 atomic 包住后，那个"占号事务"退化成 **savepoint**；
    # 远端集合已经建好，而外层一旦回滚（异常 / IntegrityError / publish 失败），
    # ``next_generation`` 的推进与 ``failed`` 登记行会被**一起撤销**。
    # 后果：远端留下一个**没人登记的孤立集合**，下次分配**又拿到同一个编号**，
    # 新数据被写进那个孤立集合——正是 ``prepare_generation`` 文档里要避免的事。
    #
    # 所以占号与外部构建都在事务外完成：失败只影响本次（编号作废、留 failed 痕迹）。
    plan = vectorindex.prepare_generation(library, profile=prof, client=client)

    # ---- 2) 独立短事务：锁库重查，只有一个 winner 能 publish ----
    winner = None
    with transaction.atomic():
        # **同一锁序 gate → library**（审查 P1）：首建发布第一代也是"改线上指针"，
        # 不能闯进别人的重建窗口。以前这里只锁库行，冻结中的重建会被这一路
        # 悄悄插入一次发布。
        from .writegate import assert_writable_locked

        assert_writable_locked(library.pk, action="索引初建发布第一代")
        locked = KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
        if locked.vector_collection:
            registered = VectorCollection.objects.filter(
                library=locked, name=locked.vector_collection
            ).first()
            if registered is not None:
                # 别的并发已经发布了：让位，**不覆盖**它
                winner = registered
                _reconcile_active_flag(locked, registered)
        if winner is None:
            try:
                _publish_generation(locked, plan)
            except IntegrityError:
                # 极端情况仍有并发穿过：重读 winner 让位；读不到才认输并抛
                registered = VectorCollection.objects.filter(
                    library=locked, name=locked.vector_collection
                ).first()
                if registered is None:
                    raise
                winner = registered
                _reconcile_active_flag(locked, registered)

    if winner is not None:
        # 未采用的候选：**事务外**精确清理（标 failed 留痕 + 删自己那个远端集合）。
        # 编号保留作废、永不复用 —— "允许并发产生候选，但只能一个 active"。
        _discard_generation(plan, reason="lost_initialization_race", client=client)
        return _reuse(winner, note="并发首建：本请求未获胜，已复用已发布的那一代")

    return {
        "initialized": True,
        "collection": plan.collection,
        "generation": plan.generation,
        "dimension": plan.dimension,
        "build_status": "ready",
    }


def _generation_from_name(name: str) -> int:
    """从 collection 名解析代次（``...-g3`` → 3；解析不出返回 0）。"""
    tail = str(name or "").rsplit("-g", 1)
    if len(tail) == 2 and tail[1].split("__")[0].isdigit():
        return int(tail[1].split("__")[0])
    return 0


def _reconcile_active_flag(locked, registered) -> None:
    """把登记表的 ``is_active`` 对齐到库指针指向的那一代。

    为什么需要：并发竞态曾经留下"指针指向 g4，但 g1 才是 is_active=True"这种
    不一致（实测遇到）。读路径用指针、登记表用 is_active，两者不一致会让
    "当前生效的是哪一代"说不清。这里以**库指针为准**做一次对齐。
    """
    VectorCollection.objects.filter(library=locked).exclude(
        pk=registered.pk
    ).update(is_active=False)
    if not registered.is_active:
        VectorCollection.objects.filter(pk=registered.pk).update(is_active=True)


def _publish_generation(locked, plan) -> None:
    """发布一代：指针与 ``is_active`` 在**同一事务**里一起改。

    顺序要点：**先把别的降级，再把自己置为 active**。旧实现直接
    ``update(is_active=True)``，把唯一性交给偏唯一索引报错兜底——
    并发下就是 500。
    """
    from .models import KnowledgeLibrary, VectorCollection

    VectorCollection.objects.filter(
        library=locked, name=plan.collection
    ).update(build_status="ready")
    VectorCollection.objects.filter(library=locked).exclude(
        name=plan.collection
    ).update(is_active=False)
    KnowledgeLibrary.objects.filter(pk=locked.pk).update(
        active_generation=plan.generation,
        vector_collection=plan.collection,
        next_generation=max(int(locked.next_generation or 0), plan.generation),
    )
    VectorCollection.objects.filter(library=locked, name=plan.collection).update(
        is_active=True
    )


def _discard_generation(plan, *, reason: str, client=None) -> None:
    """把**未被采用**的一代标 failed 并**精确清理**它的远端 collection。

    只删我们自己刚建的那一个集合（名字来自 plan），不碰任何别的代次，
    也不动库指针。编号保留在登记表里作废，**永不复用**。
    """
    from .models import VectorCollection

    VectorCollection.objects.filter(
        library_id=plan.library_id, name=plan.collection
    ).update(build_status="failed", build_error=reason[:400], is_active=False)
    own_client = client is None
    qdrant = client
    try:
        if qdrant is None:
            qdrant = vectorindex.build_client()
        if vectorindex.collection_exists(qdrant, plan.collection):
            qdrant.delete_collection(plan.collection)
            logger.warning("清理未采用的代次 %s（原因：%s）", plan.collection, reason)
    except Exception as exc:  # noqa: BLE001 - 清理失败只记录，不影响调用方
        logger.warning("清理未采用代次 %s 失败（可后续重试）：%s", plan.collection, exc)
    finally:
        if own_client and qdrant is not None:
            try:
                qdrant.close()
            except Exception:  # noqa: BLE001
                pass



def _mark_generation_failed(plan, *, reason: str) -> None:
    """把**未被采用**的一代标成 ``failed`` 并留痕（**不删远端 collection**）。

    为什么与 :func:`_discard_generation` 区别对待：

    - ``_discard_generation`` 处理的是"远端建集合阶段就失败"的空壳，删掉零风险；
    - 重建中途失败时，目标 collection 里已经有**一部分真实向量**。删它属于破坏性
      动作，而这个失败很可能只是模型服务抖动；保留远端数据便于排查（也能让
      "点到底写进去多少"这件事可核对）。登记行标 failed 之后
      :func:`validate_activation` 会拒绝激活它，编号也**永不复用**，
      所以留着它既不会上线也不会被后续构建覆盖。
    """
    from .models import VectorCollection

    VectorCollection.objects.filter(
        library_id=plan.library_id, name=plan.collection
    ).update(build_status="failed", build_error=(reason or "")[:400], is_active=False)


def rebuild_library(
    library,
    *,
    profile=None,
    client=None,
    http_client=None,
    actor=None,
    ocr=None,
) -> dict:
    """初始化 / 重建一个库的向量索引：**首上传之前必须先跑这一步**。

    流程（每一步都失败安全，任何一步失败都保留旧代次不动）：

    1. **先取冻结**（闸门行锁 + 活跃租约检查在同一个事务里）——冻结之后才枚举
       清单、才 ``prepare_generation``。顺序很关键：先枚举再冻结的话，"枚举到
       prepare 之间"新上传进来的资料会**漏出这次重建**，而激活时的 coverage
       校验又会把它算进"未归档资料"，于是重建永远无法通过校验（活锁）。
       冻结在前，"这次重建要覆盖哪些资料"就是一个冻结期内不可变的快照。
    2. ``prepare_generation`` 建一条**新代次**的 collection 并登记（旧代次不动）；
    3. 把快照里的资料在**新代次**上复用只读分段重新嵌入（``target_collection``），
       期间检索仍读旧代次，线上状态一个字段都不动；
    4. 只有全部资料的向量在真实 Qdrant 里**逐段核对通过**，且**本 job 仍然是
       当前冻结的所有者**（令牌未被旋转），才做原子激活；
    5. 失败就返回失败详情，旧代次继续生效（可回退、可重跑）；目标代次标 failed，
       编号作废不复用。
    6. **无论走哪条返回路径**，``finally`` 只解除**自己的**冻结（令牌不匹配就
       不动别人的状态，并记一条 warning）；崩溃残留的冻结用
       ``manage.py kbunfreeze`` 精确解除。
    """
    from .embedding import active_profile
    from .writegate import (
        WriteFrozen,
        assert_fence,
        assert_freeze_owner,
        freeze,
        unfreeze,
    )

    prof = profile or active_profile()
    # **冻结必须在枚举/准备之前**（审查要求①）：见 docstring 第 1 条。
    # 冻结内部会在**持有闸门行锁的同一事务里**检查活跃租约，有活跃解析/嵌入就
    # 直接拒绝（WriteFrozen），保留别人的租约，绝不抢占。
    fence_token = freeze(
        library.pk,
        reason=f"重建（profile={prof.name}）",
        actor=getattr(actor, "get_username", lambda: "")() if actor else "",
    )
    report: dict = {
        "library": library.code,
        "generation": 0,
        "collection": "",
        "dimension": int(getattr(prof, "dimension", 0) or 0),
        "files_total": 0,
        "files_processed": 0,
        "files_failed": [],
        "activated": False,
    }
    try:
        files = list(
            IngestedFile._base_manager.filter(library=library)
            .exclude(parse_status=ParseStatus.ARCHIVED)
            .order_by("id")
        )
        report["files_total"] = len(files)
        plan = vectorindex.prepare_generation(library, profile=prof, client=client)
        report["generation"] = plan.generation
        report["collection"] = plan.collection
        report["dimension"] = plan.dimension

        for record in files:
            try:
                # **重建不进普通解析/租约/状态流程**（审查方定稿的最小可靠方案）：
                # ``process_file → hold_lease → acquire_lease`` 会在入口把
                # ``parse_status`` 改成 RUNNING、清 ``parse_error``，失败还会写 FAILED ——
                # 那是**污染当前可用资料**。重建只做一件事：复用**已入库的只读分段**
                # 直接写目标代次的向量；进度与失败状态记在**目标代次登记行**上。
                if not record.chunks.exists():
                    raise PipelineError(
                        f"资料 #{record.pk} 没有可复用的分段；重建前请先完成一次解析"
                    )
                embed_chunks(
                    record,
                    profile=prof,
                    client=client,
                    http_client=http_client,
                    target_collection=plan.collection,
                    target_generation=plan.generation,
                )
                # 远端 upsert 之后复核冻结令牌：本 job 被接管/解冻时，
                # 刚写进目标代次的点**不算数**（目标代次最后会标 failed）。
                assert_fence(library.pk, fence_token, action="重建写目标代次")
            except Exception as exc:  # noqa: BLE001 - 单份失败不阻断整体，最后统一判定
                record.refresh_from_db()
                report["files_failed"].append(
                    {"file_id": record.pk, "name": record.original_name, "error": str(exc)}
                )
                continue
            report["files_processed"] += 1

        # 只有"全部资料都在新代次里写全"才允许激活；否则保留旧代次
        if report["files_failed"]:
            report["activated"] = False
            report["reason"] = "有资料重建失败，保留旧代次不切换（修好后可重跑 kbrebuild）"
            # **目标代次标 failed、编号作废不复用**：这一代已知不完整，绝不能被
            # 激活（validate_activation 也会拒绝），留痕便于巡检。**线上状态不动**。
            _mark_generation_failed(
                plan,
                reason="重建未完成：" + "；".join(
                    f"#{item['file_id']} {item['error']}" for item in report["files_failed"][:3]
                ),
            )
            return report
        # **冻结所有权校验**：必须"仍处于冻结 **且** 令牌仍是我这一把"才允许发布切换。
        # 只比令牌不够——冻结被解除后即使令牌巧合未变，也不该在没有排他保护的情况下
        # 切换线上索引；只比"是否冻结"也不够——另一个 job 冻结后我手里的令牌已失效。
        assert_freeze_owner(library.pk, fence_token, action="发布代次切换")
        try:
            # **把本 job 的冻结令牌交给 activate_generation**：它会在**同一个
            # gate 行锁事务**里复核"我仍是当前冻结所有者"，然后才锁库/校验/发布。
            # 不传令牌 = 外部激活，冻结期会被拒（409）。
            vectorindex.activate_generation(
                library, plan, client=client, freeze_token=fence_token
            )
        except vectorindex.VectorIndexError as exc:
            report["activated"] = False
            report["reason"] = f"coverage 校验未通过，保留旧代次：{exc}"
            _mark_generation_failed(plan, reason=f"激活校验未通过：{exc}")
            return report
        report["activated"] = True
        return report
    finally:
        # **只解除自己的冻结**：令牌不匹配说明冻结已被别人解除/接管，
        # 此时动闸门就是覆盖别人的状态。这里的异常绝不能盖住原始错误。
        try:
            unfreeze(library.pk, fence_token=fence_token)
        except WriteFrozen as exc:
            logger.warning(
                "重建收尾时未能解除信息库 #%s 的冻结（不是本次冻结的所有者）：%s",
                library.pk,
                exc,
            )
        except Exception:  # noqa: BLE001 - 收尾失败只记录，不能掩盖真实错误
            logger.exception("重建收尾解除冻结失败：信息库 #%s", library.pk)


def retry_file(
    file: IngestedFile,
    *,
    actor=None,
    profile=None,
    client=None,
    allow_archived: bool = False,
) -> StepResult:
    """重试/重建一份资料：沿用同一条记录、同一份原始字节、同一批分段。

    ``ARCHIVED`` **默认不允许**：归档是"移出检索"，恢复必须走受控入口
    （``planner.unarchive_file``：library:admin + 确认 + 审计），那里会显式传
    ``allow_archived=True``。否则 writer 只要调 ``process?force=true``
    （或 ``parse_file``／后台任务）就能反归档，等于权限绕过 —— 独立审查发现并
    要求堵死。``force`` 只表示"允许重跑已完成的资料"，**不**表示"允许反归档"。
    """
    allowed = {ParseStatus.FAILED, ParseStatus.RUNNING, ParseStatus.DONE}
    if allow_archived:
        allowed.add(ParseStatus.ARCHIVED)
    if file.parse_status not in allowed:
        if file.parse_status == ParseStatus.ARCHIVED:
            raise PipelineError(
                f"资料 #{file.pk} 已归档：只能通过受控「恢复归档」重新处理"
                "（需要该库管理员权限 + 一次独立确认），普通的重试入口不会恢复它。"
            )
        raise PipelineError(f"当前状态 {file.parse_status} 不允许重试")
    from .permissions import is_system_admin
    known_service_failure = any(reason in (file.parse_error or "") for reason in (
        "超过单份上限", "maximum context length is 512", "与本次解析结果不一致", "OCR 服务不可用",
        "OCR 输出达到长度上限",
    ))
    manual_recovery = bool(
        actor is not None and is_system_admin(actor) and file.parse_status == ParseStatus.FAILED
        and file.parse_attempts >= IngestedFile.MAX_PARSE_ATTEMPTS and known_service_failure
    )
    append_event(
        action="file.retry_requested",
        actor_id=getattr(actor, "pk", None),
        resource_kind="ingested_file",
        resource_key=str(file.pk),
        payload={"from_status": file.parse_status, "manual_service_recovery": manual_recovery},
    )
    return process_file(
        file,
        actor=actor,
        profile=profile,
        force=True,
        client=client,
        allow_archived=allow_archived,
        consume_budget=not manual_recovery,
    )


# ------------------------------------------------------------------ 检索 ----


def _dense_globally_disabled() -> bool:
    """稠密检索是否被**全局**关掉（离线模式 / 显式配置关闭）。

    这与"某组熔断"是两件事（审查要求⑤）：全局关闭 = 整条路都不走；
    某组熔断 = 只降级那一组，健康组继续。合在一起判断就会变成
    "一个模型坏拖垮全局"。
    """
    return bool(getattr(settings, "KB_MODEL_CALLS_DISABLED", False)) or not bool(
        getattr(settings, "KB_SEARCH_DENSE_ENABLED", True)
    )


def _group_circuit_open(info, profile_name: str) -> bool:
    """**该 profile 分组**的嵌入服务是否处于熔断（冷却）中。

    只读查询，不作任何网络调用：这样"这组服务已经确认挂掉"时，检索可以**立刻**
    只降级这一组，而不是再为它等一次超时（实测一次超时 10s、连重试 35s）。

    **键必须与 :func:`apps.knowledge.embedding.embedding_service_name` 完全一致**
    （完整快照指纹，不是裸 profile 名）：同名 profile 换了端点/版本时，
    两套服务是不同的熔断器；否则"旧端点挂了"会把新端点那组一起降级，
    而"旧端点健康"又会掩盖新端点的持续故障。两侧共用同一个 helper，避免再次漂移。
    """
    from .embedding import active_profile, embedding_service_name, get_profile
    from .resilience import breaker_for

    prof = info
    if prof is None:
        # 没有登记 profile：按当前生效的 profile 判断，
        # 不要把"未知"当成"没问题"（那样降级判断会静默失效）。
        try:
            prof = get_profile(profile_name) if profile_name else active_profile()
        except Exception:  # noqa: BLE001 - 解析不出就用裸名字兜底
            prof = None
    key = embedding_service_name(prof if prof is not None else (profile_name or ""))
    try:
        return not breaker_for(key).allow_request()
    except Exception:  # noqa: BLE001 - 探测熔断状态本身失败就不拦
        return False


class _LibrarySearchFailed(RuntimeError):
    """单个信息库的检索失败（不致命：其它库与关键字兜底照常）。"""


def _note_failed_collection(report: dict, target: dict, reason: str) -> None:
    """把一个失败的信息库记进报告。

    界面与模型都需要知道"这次没有搜全"——否则一个有命中、一个有故障的检索
    会被当成"全库都搜过了"，这对使用者是误导。
    """
    report.setdefault("failed_collections", []).append(
        {
            "collection": target.get("collection"),
            "library_id": target.get("library_id"),
            "reason": reason,
        }
    )


def search(
    *,
    query: str,
    libraries: list,
    permission_codes: list[str] | None,
    limit: int | None = None,
    use_rerank: bool | None = None,
    rerank_client: RerankClient | None = None,
    client=None,
    keyword_library_ids: list[int] | None = None,
    eligible_file_ids: set[int] | None = None,
) -> dict:
    """混合检索：稠密 + 正文关键词 + 文件名 → RRF 融合 → 重排 → 名称命中优先。

    每一路的真实命中数都写进返回值，便于验收时核对"确实走了几路"。
    ``keyword_library_ids`` 是数据库关键词兜底允许命中的库（应为"用户可读 ∩ 本次筛选"）；
    为 ``None`` 时回落到 ``libraries``。

    ``eligible_file_ids`` 是结构化条件在服务端**全量**收敛出的候选文件集合：
    ``None`` 表示不限；非空时统一约束 dense/sparse/DB 兜底，并在**重排之前**再过滤一次，
    保证送进重排的每一条都满足条件。空集合表示无候选——直接返回空、不外呼模型。
    """
    from .embedding import (
        EmbeddingError,
        ProfileInfo,
        profile_fingerprint,
        profile_for_registration,
    )

    if eligible_file_ids is not None and not eligible_file_ids:
        # 结构化条件没有任何候选：立即空结果，**不发起任何外呼**（含嵌入/重排）
        return {
            "results": [],
            "report": {
                "query": query,
                "collections": [],
                "reason": "no_eligible_file",
                "eligible_files": 0,
                "dense_hits": 0,
                "sparse_hits": 0,
                "db_keyword_hits": 0,
                "fused_hits": 0,
                "rerank": {"enabled": False, "applied": False, "error": ""},
                "degraded": False,
                "degraded_reason": "",
            },
        }

    limit = int(limit or getattr(settings, "KB_SEARCH_RESULT_LIMIT", 10))
    if use_rerank is None:
        use_rerank = bool(getattr(settings, "KB_SEARCH_USE_RERANK", True))
    dense_top_k = int(getattr(settings, "KB_SEARCH_DENSE_TOP_K", 30))
    sparse_top_k = int(getattr(settings, "KB_SEARCH_SPARSE_TOP_K", 30))
    if keyword_library_ids is None:
        keyword_library_ids = [library.pk for library in libraries]

    # 查询目标必须来自**登记表/库上的真实指针**，不能自己按 generation 拼名字。
    #
    # 这是踩过的坑：`collection_name(library.pk, generation)` 会拼出 `kb-1-g1`，
    # 而真实登记/写入的可能是 `kb-1-g1__offline_test`（测试专用后缀）或任何
    # 历史命名。自己拼名字 = 查一个不存在的 collection，结果永远是 0 命中，
    # 而且看起来像"向量库不支持"，排查会绕远路。
    #
    # 同时按**每个 collection 登记时用的 model/dimension** 取查询向量：
    # 换模型期间不同库可能跑在不同维度的索引上，全用默认 profile 会维度不符。
    from .embedding import get_profile

    targets: list[dict] = []
    seen: set[str] = set()
    #: collection → 解析好的 ProfileInfo。**只在函数内使用**：
    #: 绝不放进 targets/report（那会被 DRF 序列化，ProfileInfo 不是 JSON 类型）。
    resolved_profiles: dict[str, ProfileInfo] = {}
    for library in libraries:
        # 目标 collection 的取法，按"可信度"排序（审查意见：不能自己拼名字）：
        # 1. 库指针**且**该名字有登记行 → 最可信；
        # 2. 否则用该库"已激活"的登记行（指针与登记不一致时，登记行才是事实）；
        # 3. 都不存在才退回指针上的名字（可能查不到，但至少不会拼错）。
        name = library.vector_collection
        registered = None
        if name:
            registered = VectorCollection.objects.filter(library=library, name=name).first()
        if registered is None:
            registered = VectorCollection.objects.filter(
                library=library, is_active=True
            ).first()
            if registered is not None:
                name = registered.name
        if not name or name in seen:
            continue
        seen.add(name)
        target: dict = {
            "library_id": library.pk,
            "collection": name,
            "dimension": int(registered.dimension) if registered else 0,
            "profile": "",
            "model": registered.embedding_model if registered else "",
            "model_version": registered.model_version if registered else "",
            "base_url": (registered.base_url if registered else "") or "",
            "profile_error": "",
        }
        # 该代用哪个模型：**只认登记快照**（profile + model/version/dim/端点）。
        # 没有登记行、或快照与当前配置冲突 → 显式记为失败，**不回落全局 active profile**：
        # 回落会把"用 1024 模型查 2048 索引"变成静默空命中（看起来像"向量库没命中"）。
        try:
            if registered is None:
                raise EmbeddingError(
                    f"collection {name!r} 没有登记行：无法确定该代模型/维度，"
                    "拒绝按全局 active profile 检索"
                )
            info = profile_for_registration(
                profile=registered.profile or "",
                model=registered.embedding_model or "",
                model_version=registered.model_version or "",
                dimension=int(registered.dimension or 0),
                base_url=registered.base_url or "",
                where=f"信息库 #{library.pk} 的 collection {name!r}",
            )
            target["profile"] = info.name
            resolved_profiles[name] = info
        except EmbeddingError as exc:
            target["profile_error"] = str(exc)
        targets.append(target)
    collections: list[str] = [item["collection"] for item in targets]
    report_targets = targets

    owns_client = client is None
    try:
        qdrant = client or build_qdrant_client()
    except Exception as exc:  # noqa: BLE001 - 向量库不可用也要能检索
        qdrant = None
        vector_unavailable = str(exc)
    else:
        vector_unavailable = ""
    report: dict = {
        "query": query,
        # 本次检索的授权库范围：返回前用来剔除"越权/已删除"的残留向量
        "_authorized_library_ids": [],
        "collections": collections,
        "collection_profiles": report_targets,
        "dense_hits": 0,
        "sparse_hits": 0,
        #: 检索失败/被隔离的信息库（有值时 degraded 也为真，界面必须提示"部分结果"）
        "failed_collections": [],
        "db_keyword_hits": 0,
        "fused_hits": 0,
        "rerank": {"enabled": bool(use_rerank), "applied": False, "error": ""},
        # 模型服务不可用时，这里如实写"降级了"，并把原因带给界面。
        # 界面必须显示"关键字检索可用 / 语义检索不可用"，而不是让用户以为
        # "库里没有相关内容"。
        "degraded": False,
        "degraded_reason": "",
        "test_adapter": False,
        # 本地嵌入模式的 Qdrant：payload 索引不生效（qdrant 自己会打警告），
        # 但 **MatchText 仍然可用**——实测它是 whole-token 匹配，短查询命中不到长
        # 中文 token 是"分词边界"问题，不是"模式不支持"。这里如实记录模式，
        # 真正的召回缺口由 _db_keyword_search 兜底。
        "local_mode_sparse_unsupported": qdrant is None
        or vectorindex.qdrant_mode() != "server",
        "match_text_token_boundary_note": (
            "MatchText 为 whole-token 匹配；中文长串需整串命中，"
            "短查询由数据库关键词兜底"
        ),
        "vector_store_available": qdrant is not None,
        "vector_store_error": vector_unavailable,
        "db_keyword_fallback": False,
        # 稠密检索是否被"熔断/离线开关"提前跳过。默认 False，
        # **默认就要有键**：审计/界面会读它，缺键会让调用方拿到 KeyError。
        "dense_skipped_by_breaker": False,
        "dense_skip_reason": "",
        #: 本次因熔断冷却被逐组跳过的嵌入分组（与"全局关闭"区分开）
        "dense_groups_skipped": [],
        #: 本次真正发起过 dense 查询的嵌入分组
        "dense_groups_queried": [],
    }
    if qdrant is None:
        report["degraded"] = True
        report["degraded_reason"] = f"向量库不可用：{vector_unavailable}"
    try:
        # 显式初始化：任何分支（含"服务不可用被捕获"）之后都要能引用它们
        dense: list = []
        sparse: list = []
        dense_by_collection: dict[str, list] = {}
        if qdrant is not None and collections and query.strip():
            from .testing_adapter import enabled as _adapter_enabled

            adapter_on = _adapter_enabled()
            report["test_adapter"] = adapter_on
            # 稠密这一路的目标：默认就是全部目标 collection。
            # "要不要跳"由下面集中判断（全局开关 / **逐组**熔断）。
            collections_for_dense = list(collections)
            # **全局开关与逐组熔断要分开处理**（审查要求⑤）。
            #
            # 早先这里是一次性判断：只要**任何一个**目标库的嵌入 profile 处于熔断，
            # 就把全部 dense 目标清空。后果是"2048 那套模型挂了"会连带把 1024 的
            # 健康库一起降级成纯关键字检索——一个模型坏拖垮全局，多模型并存时
            # 尤其不可接受（换模型期间两代索引同时在线上）。
            #
            # 现在分两层：
            # * **全局性**开关（离线模式 / 显式关闭 dense）本身就是"整条路都别走"，
            #   在这里一次性处理，并保持原有的可读原因；
            # * **某组熔断**只在下面那个逐 target 循环里跳过**该组**，
            #   健康组照常走 dense，报告里逐组说明（不伪称"全量可用"）。
            #
            # 为什么仍然要"先看熔断，别等超时"：AS711 断开时一次稠密检索要先等
            # 10 秒超时再等重试（实测一次 35 秒，浏览器上就是一直"检索中"）。
            # 逐组判断同样发生在**发起外呼之前**，所以这个优化没有丢。
            if not adapter_on and _dense_globally_disabled():
                report["degraded"] = True
                if bool(getattr(settings, "KB_MODEL_CALLS_DISABLED", False)):
                    report["degraded_reason"] = (
                        "当前离线模式（KB_MODEL_CALLS_DISABLED=1）：语义检索未启用，"
                        "结果来自关键字匹配"
                    )
                else:
                    report["degraded_reason"] = (
                        "稠密检索已被配置关闭（KB_SEARCH_DENSE_ENABLED=0），"
                        "结果来自关键字匹配"
                    )
                report["dense_skipped_by_breaker"] = True
                logger.info("嵌入服务不可用/已关闭，跳过稠密检索直接走关键字")
                collections_for_dense = []
            # 按"每个 collection 登记时用的 profile"分组取查询向量：
            # 换模型期间不同库可能跑在不同维度的索引上，全用默认 profile 会维度不符。
            vectors_by_profile: dict[str, list[float]] = {}
            #: 已经嵌入失败的 profile：同一组只失败一次，之后直接跳过，
            #: 不再为每个库重复等一次超时。
            failed_profiles: set[str] = set()
            #: 本次**因熔断冷却**被跳过的 profile 分组（逐组降级的事实，界面要能看到）
            breaker_skipped_groups: list[str] = []
            #: 本次真正发起了 dense 查询的 profile 分组
            dense_queried_groups: list[str] = []
            for target in (targets if collections_for_dense else []):
                if target.get("profile_error"):
                    # 这一代的模型无法确定（没有登记行 / 快照与配置冲突）：
                    # 记为该库失败并跳过，**绝不用别的 profile 去查它**。
                    _note_failed_collection(report, target, target["profile_error"])
                    continue
                info = resolved_profiles.get(target["collection"])
                profile_name = info.name if info is not None else (target["profile"] or "")
                # ---- 逐组熔断（不是全局）----
                if not adapter_on and _group_circuit_open(info, profile_name):
                    if profile_name not in breaker_skipped_groups:
                        breaker_skipped_groups.append(profile_name)
                    report["degraded"] = True
                    _note_failed_collection(
                        report,
                        target,
                        f"嵌入配置 {profile_name or '未知'} 处于熔断冷却中，"
                        "本组跳过（其它组继续）",
                    )
                    # 不要用 setdefault 以外的写法：这个键的语义是"**没有任何一组**
                    # 走了 dense"，只有全部组都被跳过时才为真。
                    if not report.get("degraded_reason"):
                        report["degraded_reason"] = (
                            f"嵌入服务 {profile_name or '未知'} 处于熔断冷却中；"
                            "该组改用关键字检索，其它组不受影响"
                        )
                    continue
                if profile_name and profile_name not in dense_queried_groups:
                    dense_queried_groups.append(profile_name)
                # 缓存键 = **完整快照指纹**（模型/版本/维度/端点全在内）：
                # 只按 profile 名做键，会让"同名但已漂移"的两代共用同一个 query vector。
                cache_key = (
                    profile_fingerprint(info)
                    if info is not None
                    else f"name-only|{profile_name}"
                )
                # ---- 每个 collection **单独**隔离，避免一库坏拖垮全部 ----
                try:
                    if cache_key not in vectors_by_profile:
                        if cache_key in failed_profiles:
                            raise _LibrarySearchFailed(
                                f"嵌入配置 {profile_name} 本次已失败，跳过该组"
                            )
                        if adapter_on:
                            from .testing_adapter import OfflineAdapter

                            dim = int(target["dimension"] or 1024)
                            vectors_by_profile[cache_key] = OfflineAdapter(
                                dimension=dim
                            ).embed([query])[0]
                        else:
                            profile = info if info is not None else get_profile(profile_name)
                            vectors_by_profile[cache_key] = embed_texts(
                                [query], profile=profile
                            )[0]
                    vector = vectors_by_profile[cache_key]
                    registered_dim = int(target.get("dimension") or 0)
                    if registered_dim and len(vector) != registered_dim:
                        # 该代登记的维度与查询向量不符：宁可明确失败，也不要用错模型查，
                        # 否则会得到"看起来正常但都是错"的命中。
                        raise _LibrarySearchFailed(
                            f"查询向量维度 {len(vector)} ≠ 该代登记维度 {registered_dim}："
                            "拒绝用不匹配的模型查询这一代索引（会串模型/串维度）"
                        )
                    hits = vectorindex.search_dense(
                        qdrant,
                        collections=[target["collection"]],
                        vector=vector,
                        limit=dense_top_k,
                        permission_codes=permission_codes,
                        file_ids=eligible_file_ids,
                    )
                except ServiceUnavailable as exc:
                    # 嵌入服务不可用：该 profile 组整体不可用，但**不影响**其它组，
                    # 也不丢弃任何已成功的命中，更不影响关键字兜底。
                    failed_profiles.add(cache_key)
                    _note_failed_collection(
                        report, target, f"嵌入服务不可用：{exc.reason}"
                    )
                    report["degraded"] = True
                    report["degraded_reason"] = f"语义检索不可用：{exc.reason}"
                    report["service_unavailable"] = exc.as_dict()
                    logger.warning("嵌入服务不可用，该组跳过：%s", exc)
                    # **必须 continue**：否则会落到下面的 `if hits:`，
                    # 而 hits 可能是上一轮循环的旧值（张冠李戴）或根本没绑定。
                    continue
                except Exception as exc:  # noqa: BLE001
                    # 单个 collection 失败（例如 Qdrant 内部 panic/断线）：
                    # **只**记这一库，继续查健康库，并保留已有命中。
                    _note_failed_collection(
                        report, target, f"{type(exc).__name__}: {exc}"
                    )
                    report["degraded"] = True
                    report["degraded_reason"] = (
                        f"部分信息库的向量检索不可用（{len(report['failed_collections'])} 个）；"
                        "结果可能不完整"
                    )
                    logger.warning(
                        "collection %s 向量检索失败，已隔离并继续：%s",
                        target.get("collection"),
                        exc,
                    )
                    continue
                if hits:
                    dense_by_collection[target["collection"]] = hits
            # **逐组降级的事实要如实带回**（审查要求⑤）：
            # 界面/审计据此可以说"某套模型的库这一路被跳过了"，而不是把
            # "只有一个模型坏"渲染成"整个语义检索都不可用"。
            report["dense_groups_skipped"] = list(breaker_skipped_groups)
            report["dense_groups_queried"] = list(dense_queried_groups)
            if collections_for_dense and not dense_queried_groups:
                # 所有目标组都熔断 → 语义检索这一路整条都没发出去。
                # 保持旧口径（dense_skipped_by_breaker）为真，调用方不必区分两种跳法。
                report["dense_skipped_by_breaker"] = True
                report["dense_skip_reason"] = "所有目标嵌入分组都在熔断冷却中"
            dense = [
                hit
                for hits in dense_by_collection.values()
                for hit in hits
            ]
            dense.sort(key=lambda hit: hit.score, reverse=True)
            dense = dense[:dense_top_k]
        if qdrant is not None:
            # 与 dense 同样的口径：**逐个 collection** 查。
            # 一个坏库不能让其余库的向量侧关键字一起丢（用户实测：整段被一个
            # 500 的集合拖垮后，连健康库的命中也没了，结果像是"库里没有"）。
            sparse = []
            for target in targets:
                try:
                    hits = vectorindex.search_sparse(
                        qdrant,
                        collections=[target["collection"]],
                        text=query,
                        limit=sparse_top_k,
                        permission_codes=permission_codes,
                        file_ids=eligible_file_ids,
                    )
                except Exception as exc:  # noqa: BLE001 - 向量侧关键字坏了不该让检索整体失败
                    _note_failed_collection(
                        report, target, f"稀疏检索失败 {type(exc).__name__}: {exc}"
                    )
                    report["degraded"] = True
                    # setdefault 遇到已存在的**空串**不会写入 → 用显式判断
                    if not report.get("degraded_reason"):
                        report["degraded_reason"] = (
                            "部分信息库的向量检索不可用；结果可能不完整"
                        )
                    logger.warning(
                        "collection %s 稀疏检索失败，已隔离并继续：%s",
                        target.get("collection"),
                        exc,
                    )
                    continue
                sparse.extend(hits)
            sparse.sort(key=lambda hit: hit.score, reverse=True)
            sparse = sparse[:sparse_top_k]

        # 数据库关键词兜底：**只要存在向量路可能漏掉的情况就补上**。
        #
        # 实测结论（本机 qdrant_client 本地模式）：MatchText 是 **whole-token 匹配**——
        # 文档 "预付款比例 30%" 只能被 "预付款比例" / "30" 命中，
        # 查 "预付款" 命中不到（中文没有空格分词，整串就是一个 token）。
        # 所以"向量侧没命中"不等于"库里没有"，必须用数据库 icontains 兜一层。
        db_hits: list = []
        if query.strip():
            # 覆盖这几种硬情况：向量库不可用 / 向量侧关键字为空 / 语义降级 /
            # 向量侧命中不足（可能只是 token 边界对不上）。
            need_fallback = (
                qdrant is None
                or bool(report.get("local_mode_sparse_unsupported"))
                or not sparse
                or bool(report.get("degraded"))
                or len(sparse) < limit
            )
            if need_fallback:
                db_hits = _db_keyword_search(
                    query=query,
                    library_ids=keyword_library_ids,
                    limit=sparse_top_k,
                    eligible_file_ids=eligible_file_ids,
                )
                report["db_keyword_fallback"] = True
                report["db_keyword_hits"] = len(db_hits)

        report["dense_hits"] = len(dense)
        report["sparse_hits"] = len(sparse)
        # 如实说明稀疏这一路是怎么走到 0 条的，别让"0 命中"看起来像"库里没有"：
        # 中文在 Qdrant 全文索引里会被当成整串（无空格分词），MatchText 基本命不中，
        # 真正的关键词保底由下面的 DB 关键词兜底承担。
        if sparse:
            report["sparse_mode"] = "match_text"
        elif query.strip() and re.search(r"[\u4e00-\u9fff]", query):
            report["sparse_mode"] = "cjk_whole_token"
            report["sparse_note"] = (
                "中文查询在 Qdrant 全文索引上是整串 token，MatchText 难以命中；"
                "关键词保底已由数据库二元组匹配承担"
            )
        else:
            report["sparse_mode"] = "empty"
        from . import filename_search
        filename_hits = filename_search.recall(
            query=query, library_ids=keyword_library_ids, limit=max(limit, sparse_top_k),
            eligible_file_ids=eligible_file_ids,
        )
        report["filename_hits"] = len(filename_hits)
        fused = vectorindex.rrf_fuse(
            [dense, sparse, db_hits, filename_hits], limit=max(limit * 3, dense_top_k)
        )
        # 独立名称命中不能被融合的候选截断吞掉；随后仍经过同一权限/删除校验。
        identities = {vectorindex.fusion_identity(item) for item in fused}
        fused.extend(item for item in filename_hits if vectorindex.fusion_identity(item) not in identities)
        # **在融合之后、送进重排之前**剔除"已删除/越权"的残留向量。
        #
        # 顺序很重要（审查指出过我把顺序写反了）：重排会把候选正文作为上下文
        # 发给外部模型服务——如果先重排再过滤，已删除资料的正文已经被送出去了，
        # 那是实际的数据外泄。所以过滤必须在 rerank 调用**之前**完成。
        report["_authorized_library_ids"] = keyword_library_ids or [
            library.pk for library in libraries
        ]
        # **重排之前**过滤：融合会把不同来源的候选混在一起，任何一个不合
        # 结构化条件的点都不该占用重排名额，更不该把正文发给重排模型。
        fused = _filter_by_eligible(fused, eligible_file_ids, report)
        fused = _drop_tombstoned(fused, report)
        # **同文件同正文兜底去重**（独立复现：搜索"质保期"每份资料出现两条一模一样的
        # 正文）。身份对齐依赖 payload 字段齐全（缺 `content_sha256`/`ordinal` 会退回
        # `collection:point_id`），任何一条召回路径字段缺失就会漏合并；这里按
        # (file_id, 规整后正文) 再兜一层，**但不同段号/不同正文一律保留** ——
        # 绝不把"同一份资料的多个真实段落"合并成一条（那会丢召回质量）。
        fused = _dedupe_same_text(fused, report)
        report["fused_hits"] = len(fused)
        filename_file_ids = {item.payload["file_id"] for item in filename_hits}
        protected_filename_hits = {}
        for item in fused:
            file_id = item.payload.get("file_id")
            if file_id in filename_file_ids:
                protected_filename_hits.setdefault(file_id, item)

        if use_rerank and fused:
            reranker = rerank_client or RerankClient()
            try:
                hits = reranker.rerank(
                    query,
                    ["文件名：" + str(candidate.payload.get("original_name", "")) + "\n" +
                     candidate.payload.get("text", "") for candidate in fused],
                    top_n=min(int(getattr(settings, "KB_RERANK_TOP_N", 20)), len(fused)),
                )
                # 重排返回的是"候选序号 + 分数"；按分数重排、并把分数写回 payload
                reordered: list = []
                for hit in hits:
                    if not (0 <= hit.index < len(fused)):
                        continue
                    candidate = fused[hit.index]
                    payload = dict(candidate.payload)
                    payload["rerank_score"] = hit.score
                    reordered.append(
                        vectorindex.SearchCandidate(
                            point_id=candidate.point_id,
                            score=hit.score,
                            collection=candidate.collection,
                            payload=payload,
                        )
                    )
                min_spread = float(getattr(settings, "KB_RERANK_MIN_SPREAD", 0.05))
                apply_rerank, spread = _should_apply_rerank(
                    [hit.score for hit in hits], min_spread
                )
                report["rerank"]["score_spread"] = round(spread, 6)
                report["rerank"]["min_spread"] = min_spread
                if not apply_rerank:
                    # 分数饱和：重排没有提供可用的区分度，保留 RRF 顺序。
                    # 见 _rerank_spread 的说明与 evals/BASELINE.md 的实测数字。
                    report["rerank"]["applied"] = False
                    report["rerank"]["skipped"] = "saturated"
                    report["rerank"]["model"] = reranker.profile.model
                    report["rerank"]["reason"] = (
                        f"重排分数饱和（max-min={spread:.4f} < {min_spread}），"
                        "已保留融合排序，未采用重排顺序"
                    )
                else:
                    if reordered:
                        fused = reordered
                    report["rerank"]["applied"] = bool(reordered)
                    report["rerank"]["model"] = reranker.profile.model
            except Exception as exc:  # noqa: BLE001 - 重排失败不该让检索整体失败
                # **任何**重排异常都必须标"降级"，不只是"服务不可用"。
                #
                # 为什么：降级 = 本次排序退化为 RRF 融合排序，这和"重排生效"是
                # 两回事。用户拿到的是融合排序结果，界面就必须说清楚，否则会被
                # 当成"重排按相关性排过的结果"。
                #
                # 修的是真实缺陷（独立复现见 artifacts/acceptance/
                # independent-rerank-degradation-20261016.json）：此前只有
                # ``ServiceUnavailable`` 或异常文本含"不可用"才置 degraded=True，
                # 于是"HTTP 200 但响应结构畸形"这类失败虽然写进了 rerank.error，
                # 却 degraded=false / degraded_reason="" —— 搜索页
                # （apps/webui/static/webui/kb-search.js 读 report.degraded）
                # 一条降级提示都不显示。
                #
                # 文案分两类，但**两类都要写进 degraded_reason**，并用"；"拼接追加：
                # 服务不可用（熔断/超时/断连）→"重排服务不可用"；
                # 其余（结构畸形、解析失败等）→"重排失败（已退回融合排序）"。
                # 保留原有字段语义：unavailable 仍只在服务不可用时为 True。
                from .resilience import ServiceUnavailable as _ServiceUnavailable

                rerank_error_text = str(exc)
                rerank_unavailable = isinstance(exc, _ServiceUnavailable) or (
                    "不可用" in rerank_error_text
                )
                report["rerank"]["error"] = rerank_error_text
                report["rerank"]["unavailable"] = rerank_unavailable
                if rerank_unavailable:
                    rerank_reason = f"重排服务不可用：{rerank_error_text}"
                else:
                    rerank_reason = f"重排失败（已退回融合排序）：{rerank_error_text}"
                # 结果必须保留：这里只记降级，绝不抛出去——下面照常返回
                # RRF 融合结果（fused），让用户至少拿到已回退的检索结果。
                report["degraded"] = True
                report["degraded_reason"] = (
                    (report["degraded_reason"] + "；") if report["degraded_reason"] else ""
                ) + rerank_reason
                logger.warning("重排失败，回退到 RRF 融合结果：%s", exc)
        # **返回前再校验一次**：重排阶段可能把候选顺序变了，而且这期间
        # 数据库可能刚好提交了删除（过滤与返回之间存在时间差）。
        # 二次过滤是幂等的（已经过滤掉的不会再出现），成本只是几次 SQL。
        returned_file_ids = {item.payload.get("file_id") for item in fused}
        fused.extend(item for file_id, item in protected_filename_hits.items() if file_id not in returned_file_ids)
        safe = _drop_tombstoned(fused, report)
        safe = filename_search.promote(safe, query)
        report["filename_promoted"] = sum(bool(item.payload.get("filename_match_rank")) for item in safe)
        # 向量 payload 里的性质是写入时的快照；补识别/人工纠正都不重做嵌入，
        # 所以返回前用数据库当前值覆盖展示字段（不改向量、不改库）。
        return {
            "results": refresh_classification_shaped(_shape(safe[:limit])),
            "report": report,
        }
    finally:
        if owns_client and qdrant is not None:
            qdrant.close()


#: 关键词检索的最短词长：单字（尤其中文单字）命中率极低但噪音极大
MIN_KEYWORD_LEN = 2


def _rerank_spread(scores: list[float]) -> float:
    """重排分数的区分度（max-min）。

    为什么需要它（实测基线 ``evals/BASELINE.md``）：合同模板高度重复时，
    ``bge-reranker-large`` 对"正确段 / 错误兄弟段"的分数会饱和在 0.98~0.99——
    实测 0.9844（正确）vs 0.9922（错误），只差 0.008 的噪声，却足以把 RRF 排好的
    顺序整个掀翻：Recall@5 0.90 → 0.50，P50 延迟 1543ms → 2992ms。

    分数挤在一起说明重排没有提供可用区分度，此时应当保留融合顺序，
    而不是让 0.008 的抖动决定用户看到什么。
    """
    if len(scores) < 2:
        return 0.0
    return max(scores) - min(scores)


def _should_apply_rerank(scores: list[float], min_spread: float) -> tuple[bool, float]:
    """重排是否值得采用：返回 ``(是否采用, 本次区分度)``。

    少于两条候选时没有"排序"可谈，直接采用（保持旧行为）；
    其余情况按 ``_rerank_spread`` 的门槛判断，饱和就保留融合顺序。
    """
    if len(scores) < 2:
        return True, 0.0
    spread = _rerank_spread(scores)
    return spread >= min_spread, spread


def _db_keyword_search(
    *,
    query: str,
    library_ids: list[int],
    limit: int,
    eligible_file_ids: set[int] | None = None,
) -> list:
    """**数据库**关键词检索：不依赖向量库，也不依赖模型服务。

    为什么必须有它（审查意见 6 + 审查方第二轮反馈）：
    - Qdrant 本地嵌入模式不支持 payload 全文索引，字号检索拿不到结果；
    - 向量索引可能还没建（资料解析完但没嵌入）；
    - AS711 掉线时语义那一路根本不可用。

    这些情况下"关键字能搜到"是系统的基本可用性，不能退化成空结果。
    做法是用 :mod:`apps.knowledge.textquery` 切词（拉丁词 + 中文二元组），
    对 ``DocumentChunk.text`` 做 icontains 匹配（SQLite 与 PostgreSQL 都支持，
    行为一致、可解释），**覆盖查询词越多**的片段排得越前。

    中文为什么必须走二元组：按空白切词时"预付款比例"整串只是一个词，去掉空格的
    连续中文查询几乎全灭（实测离线词法 Recall@5 88% → 12%，纯中文问句 0 命中）。
    权限用 ``permission_code`` 在数据库层过滤——与向量检索同一套权限码。
    """
    from django.db.models import Q

    from .textquery import coverage, terms as query_terms

    terms = query_terms(query)
    if not terms:
        return []
    if not library_ids:
        return []
    queryset = DocumentChunk.objects.filter(file__library_id__in=list(library_ids))
    if eligible_file_ids is not None:
        # 结构化条件收敛出的候选集合：DB 兜底这条路同样必须受限，
        # 否则"没有关键词命中却语义相关"的资料会绕过条件被检索出来。
        if not eligible_file_ids:
            return []
        queryset = queryset.filter(file_id__in=list(eligible_file_ids))
    condition = Q()
    for term in terms:
        condition |= Q(text__icontains=term)
    queryset = queryset.filter(condition).select_related("file", "file__library")

    scored: list[tuple[int, DocumentChunk]] = []
    for chunk in queryset.order_by("file_id", "ordinal")[: limit * 5]:
        hits = coverage(chunk.text, terms)
        if hits:
            scored.append((hits, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].file_id, item[1].ordinal))

    candidates = []
    for rank, (hits, chunk) in enumerate(scored[:limit], start=1):
        candidates.append(
            vectorindex.SearchCandidate(
                point_id=f"db:{chunk.pk}",
                # 用名次的倒数当分数：和 RRF 的量纲一致，且"命中词更多"排更前
                score=1.0 / rank + hits / 100.0,
                collection="db-keyword",
                payload={
                    "file_id": chunk.file_id,
                    "library_id": chunk.file.library_id,
                    "library_code": chunk.file.library.code,
                    "ordinal": chunk.ordinal,
                    "page": chunk.page,
                    "text": chunk.text,
                    "permission_code": chunk.permission_code,
                    "original_name": chunk.file.original_name,
                    "directory_path": chunk.file.directory_path,
                    "classification": chunk.file.classification,
                    "customer": chunk.file.customer,
                    "department": chunk.file.department,
                    "content_sha256": chunk.file.content_sha256,
                    "source_layer": "db_keyword",
                    "keyword_hits": hits,
                },
            )
        )
    return candidates


def _filter_by_eligible(
    candidates: list, eligible_file_ids: set[int] | None, report: dict
) -> list:
    """按结构化候选集合过滤（``None`` = 不限制）。

    必须在**重排之前**调用：重排会把候选正文发给外部模型服务。
    取不到 ``file_id`` 的候选在受限时一律丢弃（fail-closed，宁可少不可错）。
    """
    if eligible_file_ids is None:
        return candidates
    kept = [
        item for item in candidates if _candidate_file_id(item) in eligible_file_ids
    ]
    report["eligible_filtered_out"] = len(candidates) - len(kept)
    report["eligible_kept"] = len(kept)
    return kept


def _candidate_file_id(candidate) -> int | None:
    """从候选命中里取 file_id（向量命中的 file_id 放在 payload 里）。"""
    payload = getattr(candidate, "payload", None)
    value = payload.get("file_id") if isinstance(payload, dict) else getattr(
        candidate, "file_id", None
    )
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _dedupe_same_text(candidates: list, report: dict) -> list:
    """同一份资料 + **完全相同正文**只保留 RRF 名次最高的一条。

    为什么在"身份对齐"之外还要这一层（独立复现）：搜索"质保期"时每份资料返回两条
    一模一样的正文（分数 0.223/0.218）。身份对齐要求 payload 字段齐全，任何一条召回
    路径缺 `content_sha256`/`ordinal` 就会退回 `collection:point_id`，于是同一分段被
    算成两个身份。这里按 `(file_id, 规整后正文)` 再兜一层，专治"同 file 同正文"。

    **不会误伤**：不同 `file_id`（别的资料）不合并；同一 `file_id` 但正文不同的
    多个真实段落**一律保留**（那是真实召回，不是重复）。

    比较用 `" ".join(text.split())`：只把**连续空白**折叠成单个空格，
    **保留词界**。曾用 `"".join(text.split())`（删掉所有空白）——那样
    `"AB C"` 与 `"A BC"` 都会被折叠成 `"ABC"`，把两段**不同**正文误判成重复删掉
    （独立审查指出）。
    """
    kept: list = []
    seen: dict[tuple, int] = {}
    dropped = 0
    for candidate in candidates:
        payload = candidate.payload or {}
        file_id = payload.get("file_id")
        text = " ".join(str(payload.get("text") or "").split())
        key = (file_id, text) if file_id is not None and text else None
        if key is not None and key in seen:
            seen[key] += 1
            dropped += 1
            continue
        if key is not None:
            seen[key] = 1
        kept.append(candidate)
    if dropped:
        report["text_dedupe_dropped"] = dropped
        report["text_dedupe_kept"] = len(kept)
    return kept


def _drop_tombstoned(candidates: list, report: dict) -> list:
    """**把"已删除资料"的残留向量从结果里剔除**（融合/返回模型上下文之前）。

    为什么必须有它（审查指出的结果安全点）：
    受控删除是"先提交数据库删除，再清理 Qdrant"。中间任何失败（向量库宕机、
    网络抖动、清理任务还没跑）都会留下**孤儿向量点**。如果检索直接返回
    payload，用户仍然能搜到已删除资料的内容——删了却还能看见，等于没删。

    判定口径（逐条按当前数据库事实核对，**不信 payload 自称**）：

    - ``file_id`` 必须仍存在于数据库；
    - 该资料的 ``content_sha256`` 必须与 payload 里的一致
      （防止 file_id 被复用/重建后"张冠李戴"）；
    - 所属信息库必须仍然存在且 ``is_active``；
    - 该资料所属库必须在**本次检索的授权范围内**（越权残留一并丢弃）。

    被丢弃的条目记进 ``report`` 便于审计：说明"清理尚未收敛"而不是"没有结果"。
    """
    if not candidates:
        return candidates

    from .models import IngestedFile, KnowledgeLibrary

    allowed_library_ids = set(report.get("_authorized_library_ids") or [])
    file_ids = {
        payload_file_id
        for candidate in candidates
        if (payload_file_id := (candidate.payload or {}).get("file_id"))
    }
    rows = {
        row.pk: row
        for row in IngestedFile._base_manager.filter(pk__in=file_ids).only(
            "id", "content_sha256", "library_id"
        )
    }
    active_library_ids = set(
        KnowledgeLibrary.objects.filter(is_active=True).values_list("pk", flat=True)
    )

    kept: list = []
    dropped = 0
    dropped_reasons: dict[str, int] = {}
    for candidate in candidates:
        payload = candidate.payload or {}
        file_id = payload.get("file_id")
        row = rows.get(file_id)
        if row is None:
            dropped += 1
            dropped_reasons["file_not_found"] = dropped_reasons.get("file_not_found", 0) + 1
            continue
        # **fail-closed**：指纹与库归属**必须**存在且一致，缺失一律丢弃。
        #
        # 早期写成"payload 里有才比较"（``if payload.get(...) and ... != ...``），
        # 于是缺字段的老点会被当成合格数据放过去——那是 fail-open，
        # 正好给了"绕过校验"的口子。检索结果宁可少，也不能放出无法核验的内容。
        payload_sha = payload.get("content_sha256")
        if not payload_sha or payload_sha != row.content_sha256:
            dropped += 1
            dropped_reasons["hash_mismatch"] = dropped_reasons.get("hash_mismatch", 0) + 1
            continue
        payload_library_id = payload.get("library_id")
        if not payload_library_id or int(payload_library_id) != int(row.library_id):
            dropped += 1
            dropped_reasons["library_mismatch"] = (
                dropped_reasons.get("library_mismatch", 0) + 1
            )
            continue
        if row.library_id not in active_library_ids:
            dropped += 1
            dropped_reasons["library_inactive"] = (
                dropped_reasons.get("library_inactive", 0) + 1
            )
            continue
        if allowed_library_ids and row.library_id not in allowed_library_ids:
            dropped += 1
            dropped_reasons["out_of_scope"] = dropped_reasons.get("out_of_scope", 0) + 1
            continue
        kept.append(candidate)

    if dropped:
        report["tombstoned_dropped"] = dropped
        report["tombstoned_reasons"] = dropped_reasons
        report.setdefault("notes", []).append(
            f"已丢弃 {dropped} 条无法核验/已删除的残留向量"
            f"（{dropped_reasons}；向量清理尚未收敛）"
        )
        report["degraded"] = True
        previous_reason = report.get("degraded_reason")
        report["degraded_reason"] = (
            (previous_reason + "；") if previous_reason else ""
        ) + f"检测到 {dropped} 条无法核验或已删除的残留向量，已从结果中剔除"
        logger.warning("检索丢弃 %d 条残留向量：%s", dropped, dropped_reasons)
    return kept


def _shape(candidates: list) -> list[dict]:
    out: list[dict] = []
    for candidate in candidates:
        payload = candidate.payload or {}
        out.append(
            {
                "point_id": candidate.point_id,
                "collection": candidate.collection,
                "score": candidate.score,
                "filename_match": payload.get("filename_match", ""),
                "filename_match_rank": payload.get("filename_match_rank", 0),
                "file_id": payload.get("file_id"),
                "ordinal": payload.get("ordinal"),
                "page": payload.get("page"),
                "text": payload.get("text", ""),
                "original_name": payload.get("original_name", ""),
                "library_id": payload.get("library_id"),
                "library_code": payload.get("library_code", ""),
                "directory_path": payload.get("directory_path", ""),
                "classification": payload.get("classification", ""),
                "permission_code": payload.get("permission_code", ""),
            }
        )
    return out


def refresh_classification_shaped(results: list[dict]) -> list[dict]:
    """把搜索结果里的「性质」对齐到**数据库当前值**（向量 payload 可能落后）。

    为什么需要这一步：向量 payload 里的 ``classification`` 是**写入那一刻**的快照。
    自动补识别（``manage.py kbclassify``）与人工纠正（``classify_file``）都**刻意
    不重做嵌入**（重嵌入代价大，且不是"改内容"），于是 payload 会落后于数据库。
    若不修，用户会看到"库里是合同、检索结果卡上却空着"这种自相矛盾。

    做法：按命中里的 file_id 一次性回查数据库，用当前值覆盖展示字段
    （并补上 ``classification_source`` / ``classification_note``）。
    **不改向量、不改数据库**——只是让"看到的口径"与库里一致。
    """
    ids = {item.get("file_id") for item in results if item.get("file_id")}
    if not ids:
        return results
    current = {
        row["id"]: row
        for row in IngestedFile._base_manager.filter(pk__in=ids).values(
            "id", "classification", "classification_source", "classification_note"
        )
    }
    for item in results:
        row = current.get(item.get("file_id"))
        if row is None:  # 资料已删除：保留 payload 快照，由调用方的删除校验负责
            continue
        item["classification"] = row["classification"] or ""
        item["classification_source"] = row["classification_source"] or ""
        item["classification_note"] = row["classification_note"] or ""
    return results
