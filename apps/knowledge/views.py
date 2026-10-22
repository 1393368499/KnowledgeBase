"""知识库基础 API：信息库、资料（只读 + 追加）、分段。

设计约定：
- 资料与分段**没有任何写接口**（除了"追加一次上传"），改删由 governance 层与模型层双重拦截。
- 列表接口按用户可读范围过滤，不泄露无权信息库的数据。
"""

from __future__ import annotations

import math
import time

from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import append_event
from governance.immutable import immutable_endpoint

from . import pipeline
from .feedback import apply_feedback_weight, feedback_max_adjust
from .models import (
    FILE_TYPE_GROUP_LABELS,
    FILE_TYPE_GROUPS,
    DocumentChunk,
    FileNature,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    SearchFeedback,
    VectorCollection,
    file_type_group,
)
from .nature import (
    NATURE_VOCABULARY,
    is_unidentified_filter,
    nature_error_message,
)
from .permissions import CanContributeLibrary, is_system_admin, readable_library_ids
from .serializers import (
    DocumentChunkSerializer,
    IngestedFileSerializer,
    LibrarySerializer,
    SearchRequestSerializer,
    VectorCollectionSerializer,
)
from .services import ingest_file, require_valid_upload, sha256_of_stream, store_upload


def validate_nature_value(value: str) -> str:
    """性质**取值**校验（写入口）：非空时必须是五项词表之一，否则 400。

    用在哪：上传（调用方显式指定性质）与搜索页的结构化条件。这两处都是
    调用方**主动给出一个取值**的地方 —— 非法取值静默通过，用户会以为
    "库里没有这类资料"或"设置成功了"，那是看起来成功、实际说谎。

    **明确拒绝**列表页专用的「待识别」哨兵（``__unidentified__``）：它是只读筛选
    语义，不是合法性质；让写入口接受它等于往库里写一个筛不出来的野值。

    **不用在**列表页筛选（``nature=``）：那是**只读查询**，而历史库里确实存在
    词表外的值（早期接口直接写入的「技术文档」）。若筛选也一并拒绝，那些历史行
    就再也筛不出来 —— 用户会看到"资料明明在列表里，却怎么筛都筛不到"。
    列表筛选走精确匹配：查询词原样参与比较，不写任何字段。
    """
    text = str(value or "").strip()
    if text and text not in NATURE_VOCABULARY:
        raise ValidationError({"nature": nature_error_message(text)})
    return text


class KbPagination(PageNumberPagination):
    """统一分页：``page_size`` 可指定，且有**明确上限**。

    为什么必须显式提供它（实测踩到）：DRF 默认每页 20 条且**不接受** ``page_size``，
    而界面按每页 10 条计算分页与"第 X / Y 页"。两者不一致就会漏条、页码错，
    看起来像"资料少了"。这里让客户端能按自己的页大小取，同时用
    ``max_page_size`` 防止一次拉爆数据库。
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


@immutable_endpoint
class LibraryViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = KbPagination
    """信息库（P1 只读；创建/改配置在 P2 提供并走审计）。"""

    serializer_class = LibrarySerializer
    queryset = KnowledgeLibrary.objects.all()

    def get_queryset(self):
        queryset = KnowledgeLibrary.objects.annotate(
            file_count=Count("files", distinct=True),
            member_count=Count("members", distinct=True),
        )
        allowed = readable_library_ids(self.request.user)
        if allowed is not None:
            queryset = queryset.filter(id__in=allowed)
        if self.request.query_params.get("active", "1") != "0":
            queryset = queryset.filter(is_active=True)
        return queryset.order_by("id")

    @action(detail=True, methods=["get"], url_path="documents")
    def documents(self, request, pk=None):
        """按文档归组的存储清单，先核验库权限，再分页。"""
        library = self.get_object()
        files = library.files.all()
        totals = files.aggregate(documents=Count("id"), parsed_chunks=Sum("chunk_count"),
                                 embedded_chunks=Sum("embedded_chunk_count"))
        keyword = request.query_params.get("q", "").strip()
        if keyword:
            files = files.filter(original_name__icontains=keyword)
        files = files.order_by("-created_at", "-id")
        page = self.paginate_queryset(files)
        response = self.get_paginated_response(
            IngestedFileSerializer(page, many=True, context={"request": request}).data)
        response.data["library"] = self.get_serializer(library).data
        response.data["totals"] = {key: value or 0 for key, value in totals.items()}
        return response


class IngestedFileViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = KbPagination
    """已入库资料：只读 + 上传追加。"""

    serializer_class = IngestedFileSerializer
    # 上传要 multipart；而 `process` 动作是**JSON**（前端/调用方发
    # {"force": "1"}）。只声明 multipart 时，JSON 请求会被 DRF 判成
    # "Unsupported media type"（HTTP 415）——实测踩到，界面上的"重试处理"
    # 因此从来没能真正工作过。所以三种解析器都要有。
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        queryset = IngestedFile.objects.select_related("library").all()
        allowed = readable_library_ids(self.request.user)
        if allowed is not None:
            queryset = queryset.filter(library_id__in=allowed)
        params = self.request.query_params
        library = params.get("library")
        if library:
            if library.isdigit():
                queryset = queryset.filter(library_id=int(library))
            else:
                queryset = queryset.filter(library__code=library)
        parse_status = params.get("status")
        if parse_status:
            # 取值必须落在 ParseStatus 里：静默接受拼错的状态会让"筛选"看起来生效、
            # 实际返回空列表，排查时非常难定位。未知值直接 400 并列出合法取值。
            wanted = [item.strip() for item in parse_status.split(",") if item.strip()]
            unknown = [item for item in wanted if item not in ParseStatus.values]
            if unknown:
                raise ValidationError(
                    {
                        "detail": f"无效的解析状态：{', '.join(unknown)}"
                        f"（可选：{', '.join(ParseStatus.values)}）"
                    }
                )
            queryset = queryset.filter(parse_status__in=wanted)
        customer = params.get("customer")
        if customer:
            queryset = queryset.filter(customer=customer)
        department = params.get("department")
        if department:
            queryset = queryset.filter(department=department)
        keyword = params.get("q")
        nature = params.get("nature") or params.get("classification")
        if is_unidentified_filter(nature):
            # 「待识别」筛选（前端 `KB.NATURE_UNIDENTIFIED`）：列出性质为空的资料。
            # 这是**只读**语义，与"性质 = 某值"的精确匹配互斥；哨兵值不是合法性质，
            # 写入路径（上传/classify_file/搜索条件）一律拒绝它。
            queryset = queryset.filter(classification="")
        elif nature:
            # 其余取值走**精确匹配**，不在这里做词表校验：历史库里有词表外的值
            # （早期接口写入的「技术文档」），拒绝会让那些行永远筛不出来。
            # 写入口另有严格校验，见 `validate_nature_value`。
            queryset = queryset.filter(classification=nature)
        # 金额下限（万元）：原型搜索页有这一项，必须**服务端**生效——
        # 只在前端已取回的那一页里过滤，统计数字就会说谎。
        amount_min = parse_amount_min(params.get("amount_min"))
        if amount_min is not None:
            queryset = queryset.filter(amount_wan__gte=amount_min)
        file_type = params.get("file_type")
        if file_type and file_type != "all":
            # 「文件类型」下拉给的是**三类**（pdf / excel / word，用户 2026-10-16 要求），
            # 而库里 `file_type` 存的是扩展名。所以三类值在这里展开成扩展名集合；
            # 扩展名本身（老前端 / 老链接里的 `file_type=pdf`）继续按精确匹配走，
            # 不会因为这次改动而失效。
            extensions = FILE_TYPE_GROUPS.get(file_type.strip().lower())
            if extensions:
                queryset = queryset.filter(file_type__in=extensions)
            else:
                queryset = queryset.filter(file_type=file_type)
        if keyword:
            mode = (params.get("mode") or "hybrid").lower()
            if mode == "content":
                queryset = queryset.filter(
                    id__in=DocumentChunk.objects.filter(text__icontains=keyword).values("file_id")
                )
            elif mode == "filename":
                queryset = queryset.filter(original_name__icontains=keyword)
            else:
                queryset = queryset.filter(
                    Q(original_name__icontains=keyword)
                    | Q(directory_path__icontains=keyword)
                    | Q(
                        id__in=DocumentChunk.objects.filter(
                            text__icontains=keyword
                        ).values("file_id")
                    )
                )
        # 来源口径：原型把"会话来源"与"目录扫描/普通上传"分开。
        # 必须**精确**按 source 过滤——旧写法 `exclude(source="manual")` 会把任何
        # 将来新增的来源（目录扫描等）也算成"会话资料"，口径就错了。
        source = params.get("source")
        if source:
            queryset = queryset.filter(source__in=source.split(","))
        session_only = params.get("session")
        if session_only in {"1", "true"}:
            queryset = queryset.filter(source="session")
        return queryset.order_by("-created_at", "-id")

    @action(detail=False, methods=["get"], url_path="facets")
    def facets(self, request):
        """筛选选项 + **服务端统计**。

        为什么必须有它：搜索页要显示"找到 N 条"，下拉里也要显示部门/性质计数。
        如果在前端从"已取回的那一页"里统计，窗口一旦被分页截断，数字就是错的、
        却看起来像全量——那等于说谎。所以统计一律在服务端做。
        """
        queryset = self.get_queryset()

        def distinct_counts(field):
            rows = (
                queryset.exclude(**{field: ""})
                .values(field)
                .annotate(count=Count("id"))
                .order_by("-count", field)
            )
            return [{"value": row[field], "count": row["count"]} for row in rows]

        natures = distinct_counts("classification")
        # 性质词表（`FileNature` 的五个取值）**永远出现在选项里**：否则"已入库资料
        # 都还没填性质"时，搜索页的「资料性质」下拉会是空的 —— 用户既看不到有哪几类
        # 可选，也没法按它筛。库里真实存在的其他取值（历史数据）照旧保留，不删除。
        present = {row["value"] for row in natures}
        natures = natures + [
            {"value": choice.value, "count": 0}
            for choice in FileNature
            if choice.value not in present
        ]

        # 「文件类型」下拉：按 PDF / Excel / Word / 文本文件（+ 其他）汇总真实份数。
        # 计数来自全量 queryset（服务端统计），不是某一页窗口 —— 数字必须能对得上。
        extension_counts: dict[str, int] = {}
        for row in distinct_counts("file_type"):
            extension_counts[str(row["value"]).lower()] = row["count"]
        groups = [
            {
                "value": group,
                "label": FILE_TYPE_GROUP_LABELS[group],
                "count": sum(
                    extension_counts.get(extension, 0) for extension in extensions
                ),
            }
            for group, extensions in FILE_TYPE_GROUPS.items()
        ]
        other = sum(
            count
            for value, count in extension_counts.items()
            if file_type_group(value) == "other"
        )
        if other:
            groups.append(
                {"value": "other", "label": FILE_TYPE_GROUP_LABELS["other"], "count": other}
            )

        return Response(
            {
                "total": queryset.count(),
                "departments": distinct_counts("department"),
                "customers": distinct_counts("customer"),
                "natures": natures,
                "classifications": [choice.value for choice in FileNature],
                "file_types": distinct_counts("file_type"),
                "file_type_groups": groups,
                "sources": distinct_counts("source"),
            }
        )

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        queryset = self.get_queryset()
        by_status = {
            row["parse_status"]: row["count"]
            for row in queryset.values("parse_status").annotate(count=Count("id"))
        }
        total = queryset.count()
        done = by_status.get(ParseStatus.DONE, 0)
        return Response(
            {
                "total": total,
                "by_status": {choice.value: by_status.get(choice.value, 0) for choice in ParseStatus},
                "done": done,
                "session_source": queryset.filter(source="session").count(),
                "chunks": DocumentChunk.objects.filter(
                    file__in=queryset.values("id")
                ).count(),
            }
        )

    @action(detail=True, methods=["get"], url_path="chunks")
    def chunks(self, request, pk=None):
        record = self.get_object()
        page = DocumentChunk.objects.filter(file=record).order_by("ordinal")
        serializer = DocumentChunkSerializer(page, many=True)
        return Response({"count": page.count(), "results": serializer.data})

    @action(detail=True, methods=["get"], url_path="stored-info")
    def stored_info(self, request, pk=None):
        """同一份文档的解析正文、向量归属和稳定点标识放在同一响应内。"""
        from .vectorindex import point_id_for

        record = self.get_object()
        chunks = record.chunks.order_by("ordinal")
        page = self.paginate_queryset(chunks)
        rows = DocumentChunkSerializer(page, many=True).data
        for row, chunk in zip(rows, page):
            row["point_id"] = point_id_for(record, chunk.ordinal)
        response = self.get_paginated_response(rows)
        response.data["file"] = self.get_serializer(record).data
        collection_name = record.vector_collection_name or record.vector_collection
        collection = VectorCollection.objects.filter(name=collection_name).first()
        response.data["vector"] = {
            "collection": collection_name,
            "generation": record.vector_generation,
            "status": record.vector_status,
            "embedded_chunks": record.embedded_chunk_count,
            "dimension": collection.dimension if collection else None,
            "model": collection.embedding_model if collection else "",
            "storage_mode": collection.storage_mode if collection else "",
        }
        return response

    @action(detail=False, methods=["post"], url_path="upload")
    def upload(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "缺少 file 字段（multipart/form-data）"})

        library_ref = request.data.get("library") or request.query_params.get("library")
        if not library_ref:
            raise ValidationError({"library": "必须指定信息库"})
        library = (
            KnowledgeLibrary.objects.filter(pk=int(library_ref)).first()
            if str(library_ref).isdigit()
            else KnowledgeLibrary.objects.filter(code=str(library_ref)).first()
        )
        if library is None:
            raise NotFound("信息库不存在")
        if not library.is_active:
            raise ValidationError({"library": "信息库已停用"})

        # 对象级权限：必须是库管理员/可写入成员/系统管理员
        if not CanContributeLibrary().has_object_permission(request, self, library):
            raise PermissionDenied(CanContributeLibrary.message)

        digest, size = sha256_of_stream(upload)
        require_valid_upload(size, upload.name)
        upload.seek(0)

        # **先校验金额，再落盘**：金额非法时必须 400，且不产生任何 blob。
        # 否则每次输错都会在只读存储里留下一个没人引用的孤儿文件。
        amount_wan = parse_amount_wan(request.data.get("amount_wan"))

        # 性质：调用方显式给值时**必须是词表五项之一**，否则 400。
        #
        # 为什么这里不再"宽松放行任意字符串"：性质已经改成**按正文自动识别**
        # （见 `apps.knowledge.classification`），上传弹窗不再提供"手选性质"这一项。
        # 上传时仍允许传值（整目录扫描 / AI 会话上传的既有调用方），
        # 但只能是词表内的值，且会被记为**人工设置**（`classification_source=manual`），
        # 自动识别不会覆盖它。词表外的值一律拒绝，而不是静默写进一个筛不出来的野值。
        classification_raw = str(request.data.get("classification", "") or "").strip()
        if classification_raw and classification_raw not in NATURE_VOCABULARY:
            raise ValidationError(
                {
                    "classification": nature_error_message(classification_raw)
                    + "；不填则按正文自动识别"
                }
            )
        classification = classification_raw

        # 第一步就落盘原始字节：内容寻址 + 只读副本，重传同一内容不产生第二份。
        blob = store_upload(upload, expected_sha256=digest)
        if blob.sha256 != digest:
            raise ValidationError(
                {"file": f"落盘后指纹不一致（{blob.sha256[:12]} != {digest[:12]}），已拒绝入库"}
            )

        # **顺序很关键（审查指出）**：先写数据库入库记录，再确保索引。
        #
        # 旧实现是先 ``ensure_library_index`` 再 ``ingest_file``，而且只
        # ``except PipelineError``：Qdrant 建集合失败（比如并发撞唯一约束抛
        # ``IntegrityError``）会直接 500，**资料记录根本没落库**——等于原件在磁盘上、
        # 但系统里查不到它，也没有可重试的对象。那违反"保留原件、可重试"。
        #
        # 现在：`ingest_file` 先落库（记录一定存在），随后索引失败只是把这份资料
        # 标成"待处理 + 原因"，返回 202 让调用方知道"已受理、稍后/重试"，
        # 记录与原件都不会丢。
        result = ingest_file(
            library=library,
            original_name=upload.name,
            content_sha256=digest,
            size_bytes=size,
            actor=request.user,
            directory_path=request.data.get("directory_path", ""),
            classification=classification,
            classification_source="manual" if classification else "",
            amount_wan=amount_wan,
            customer=request.data.get("customer", ""),
            department=request.data.get("department", ""),
            source=request.data.get("source", "manual"),
            conversation_key=request.data.get("conversation_key", ""),
            blob=blob,
        )

        # 自动初建索引：全新库还没有任何 collection 时先建出第一代，
        # 否则紧随其后的处理会报"未初始化"。这样"建库 → 首上传 → 处理"不再需要
        # 用户先去跑一次空库重建（那会形成死循环）。
        #
        # 注意捕获范围：**不只是 PipelineError**。外部依赖（Qdrant/网络）的
        # 任何异常都不能让"已入库"这个事实变成 500 消失。
        index_state: dict = {}
        index_failed = False
        try:
            index_state = pipeline.ensure_library_index(library)
        except pipeline.PipelineError as exc:
            index_state = {"initialized": False, "error": str(exc)}
            index_failed = True
        except Exception as exc:  # noqa: BLE001 - Qdrant/网络等外部失败
            index_state = {
                "initialized": False,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
            index_failed = True

        # 第二步：处理（解析 → 分段 → 嵌入 → 写 Qdrant）。
        #
        # 处理失败不改变"已入库"这个事实（记录仍在，parse_error 记明原因，可重试）。
        # 处理**模式**由 KB_UPLOAD_PROCESS_MODE 决定：
        # - inline：请求内跑完（会阻塞，模型服务掉线时很久）
        # - queue / auto：先入队，立刻返回 202 + 待处理；派发不出去就如实标"待处理"
        # 无论哪种模式，都绝不"假装完成"。
        from config.celery import dispatch_processing
        from django.conf import settings as _settings

        mode = str(getattr(_settings, "KB_UPLOAD_PROCESS_MODE", "auto")).lower()
        process_report: dict = {"attempted": False, "mode": mode, "queued": False}
        deferred = False
        if not result.created and result.reason == "duplicate_content":
            process_report["skipped_reason"] = "duplicate_content"
            # `blob.reused=False` 说明这次真的写了字节：对"原件丢失"的资料而言，
            # 这就是**修复**（内容寻址，写回同一个只读路径）。让界面能如实说出来。
            process_report["blob_restored"] = not blob.reused
            blob_note = (
                "本次上传的字节是新写入的：若此前原件缺失，现已补齐"
                if not blob.reused
                else "原件本来就在，未重写"
            )
            # 只有**处理失败**的资料才在重传时重新排队：
            # PENDING 的行本来就由 sweep_pending 周期扫尾，这里再排一次是多余的；
            # 而 FAILED 的行此前除了删记录重传没有任何修复入口（独立审计 F2）。
            if result.file.parse_status == ParseStatus.ARCHIVED:
                # **归档资料重传绝不能静默**（独立复现：重传返回
                # created=false/duplicate_content、仍是同一条 archived 记录、
                # queued=false，界面却承诺"重新上传原件即可恢复" —— 承诺不成立）。
                # 恢复要走受控入口（library:admin + 确认 + 审计），普通 writer
                # 不能靠"重传同一份字节"绕过归档决定。
                process_report["archived"] = True
                process_report["restore_required"] = True
                process_report["queued"] = False
                process_report["detail"] = (
                    "内容已在库中（未重复入库），但这条资料处于**已归档**状态，"
                    "重传**不会**自动把它恢复检索。请在文件管理页对这份资料点「恢复」"
                    "（受控流程：需要该库管理员权限 + 一次独立确认，会按原件重新建索引）。"
                    + blob_note
                )
            elif result.file.parse_status == ParseStatus.FAILED:
                dispatch = dispatch_processing(result.file.pk)
                process_report.update(dispatch)
                process_report["queued"] = True
                process_report["attempted"] = True
                if not dispatch.get("dispatched"):
                    deferred = True
                    process_report["ok"] = False
                    process_report["detail"] = (
                        "内容已在库中（未重复入库），但这次重传要触发的重新处理"
                        "没能派发（无可用 worker / broker）；资料仍是"
                        f"{result.file.get_parse_status_display()}，可用 manage.py kbprocess "
                        "或「重新解析」推进"
                    )
                else:
                    process_report["detail"] = (
                        "内容已在库中（未重复入库）；这条资料此前处理失败，"
                        "本次重传已重新排队处理。" + blob_note
                    )
            else:
                process_report["detail"] = (
                    "内容已在库中（未重复入库）：同内容资料只保留一条记录，"
                    "原始字节按内容寻址复用。" + blob_note
                )
        elif index_failed:
            # 索引都没准备好，就别去跑处理（必然失败且浪费一次模型调用）。
            # 但**记录已经在库里**，如实告诉调用方"待处理、可重试"。
            deferred = True
            process_report["ok"] = False
            process_report["deferred_reason"] = "index_unavailable"
            process_report["detail"] = (
                "已落盘并入库，但索引初始化未成功（外部向量库问题）；"
                "资料处于待处理状态，可在索引恢复后重试处理："
                + str(index_state.get("error", ""))[:200]
            )
        elif mode == "inline":
            process_report["attempted"] = True
            try:
                step = pipeline.process_file(result.file, actor=request.user)
                process_report["ok"] = step.ok
                process_report["detail"] = step.detail
                process_report["metrics"] = step.metrics
            except pipeline.PipelineError as exc:
                process_report["ok"] = False
                process_report["detail"] = str(exc)
        else:
            dispatch = dispatch_processing(result.file.pk)
            process_report.update(dispatch)
            process_report["queued"] = True
            process_report["attempted"] = True
            if not dispatch.get("dispatched"):
                # 没有 worker / broker 不可用：明确"待处理"，绝不假装完成
                deferred = True
                process_report["ok"] = False
                process_report["detail"] = (
                    "已落盘并入库，但处理任务未能派发（无可用 worker / broker）；"
                    "资料处于待处理状态，可用 manage.py kbprocess 或重试接口推进"
                )

        result.file.refresh_from_db()
        serializer = self.get_serializer(result.file)
        # 状态码口径：**队列模式下一律 202**（已受理、处理未必完成），
        # 不能因为"派发成功"就返回 201 —— 那会让调用方以为已经处理完了。
        if process_report.get("queued"):
            status_code = status.HTTP_202_ACCEPTED
        elif deferred:
            status_code = status.HTTP_202_ACCEPTED
        else:
            status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return Response(
            {
                "created": result.created,
                "reason": result.reason,
                "stored": {
                    "sha256": blob.sha256,
                    "rel_path": blob.rel_path,
                    "size_bytes": blob.size_bytes,
                    "reused": blob.reused,
                    "read_only": blob.read_only,
                },
                "processing": process_report,
                "index_state": index_state,
                "file": serializer.data,
            },
            status=status_code,
        )

    @action(detail=True, methods=["post"], url_path="process")
    def process(self, request, pk=None):
        """对一条已入库资料跑/重跑流水线（解析 → 分段 → 嵌入 → 写索引）。"""
        record = self.get_object()
        if not CanContributeLibrary().has_object_permission(request, self, record.library):
            raise PermissionDenied(CanContributeLibrary.message)
        if not record.raw_path:
            raise ValidationError(
                {"file": "这条记录没有原始字节（P1 阶段只存了元数据），请重新上传同名文件补齐"}
            )
        force = str(request.data.get("force", "")).lower() in {"1", "true", "yes"}
        try:
            if force:
                step = pipeline.retry_file(record, actor=request.user)
            else:
                step = pipeline.process_file(record, actor=request.user)
        except pipeline.PipelineError as exc:
            # 失败原因**必须原样回给调用方**（`detail` 就是这次尝试的真实原因，
            # 例如 `PipelineError: 模型服务不可用：...`）。文件管理页的行内错误文案
            # 直接取这个字段显示 —— 换成"处理失败"这类通用文案，用户就再也分不清
            # 是原件坏了、模型掉线还是 PDF 本身有问题（已证实缺陷 3）。
            # 状态码仍是 409：失败不能被包装成"已受理"。
            record.refresh_from_db()
            return Response(
                {"ok": False, "detail": str(exc), "file": self.get_serializer(record).data},
                status=status.HTTP_409_CONFLICT,
            )
        record.refresh_from_db()
        return Response({"ok": step.ok, "detail": step.detail, "metrics": step.metrics, "file": self.get_serializer(record).data})

    def destroy(self, request, *args, **kwargs):  # pragma: no cover - 治理层已拦，双保险
        raise PermissionDenied("已入库资料不可删除；请走删除确认流程（P4）")

    def update(self, request, *args, **kwargs):  # pragma: no cover
        raise PermissionDenied("已入库资料不可修改")


@immutable_endpoint
class VectorCollectionViewSet(viewsets.ReadOnlyModelViewSet):
    """向量集合登记表（只读）。

    可见性口径与资料一致：只返回"用户可读信息库"名下的集合，以及不属于任何
    信息库的系统集合（``library`` 为空，仅管理员可见）。
    """

    serializer_class = VectorCollectionSerializer

    def get_queryset(self):
        queryset = VectorCollection.objects.select_related("library").all()
        if not is_system_admin(self.request.user):
            allowed = readable_library_ids(self.request.user)
            allowed = set() if allowed is None else allowed
            queryset = queryset.filter(library_id__in=allowed)
        return queryset.order_by("id")


def parse_amount_wan(raw) -> float | None:
    """把"金额（万元）"输入解析成有限非负数；非法就抛 400。

    为什么必须显式判断 ``isfinite``：``float("nan")`` / ``float("inf")``
    都能通过 ``float()``，但拿它们去比较或写库会得到无法解释的行为
    （NaN 比较永远为假、inf 会污染聚合）。这类输入属于用户错误，应当 400。
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError({"amount_wan": "金额必须是数字（万元）"})
    if not math.isfinite(value):
        raise ValidationError({"amount_wan": "金额必须是有限数字（万元）"})
    if value < 0:
        raise ValidationError({"amount_wan": "金额不能为负数"})
    return value


def parse_amount_min(raw) -> float | None:
    """列表/搜索的金额下限：同样有限非负。"""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError({"amount_min": "金额下限必须是数字（万元）"})
    if not math.isfinite(value):
        raise ValidationError({"amount_min": "金额下限必须是有限数字（万元）"})
    if value < 0:
        raise ValidationError({"amount_min": "金额下限不能为负数"})
    return value


def _readable_files(user):
    """用户可读的全部资料（**不分页**）。检索的候选口径必须与列表页一致。"""
    queryset = IngestedFile.objects.all()
    allowed = readable_library_ids(user)
    if allowed is not None:
        queryset = queryset.filter(library_id__in=allowed)
    return queryset


def apply_file_filters(
    queryset,
    *,
    department: str = "",
    customer: str = "",
    nature: str = "",
    amount_min=None,
):
    """把结构化条件应用到资料 QuerySet 上（与列表页同一套语义）。

    与 ``IngestedFileViewSet.get_queryset`` 保持一致：这里**不做任何分页**，
    也不按查询词预过滤——候选集合必须是"满足结构化条件的全部资料"。
    """
    if department:
        queryset = queryset.filter(department=department)
    if customer:
        queryset = queryset.filter(customer=customer)
    if nature:
        if is_unidentified_filter(nature):
            # 「待识别」哨兵：与文件列表**同一语义**（列出 `classification == ""`）。
            # 同一份前端会在列表与检索里用同一个下拉值，两端口径必须一致，
            # 否则用户会看到"列表筛得出、检索筛不出"（用户明确要求全链路一致）。
            queryset = queryset.filter(classification="")
        else:
            # 搜索页的"资料性质"是**主动给出的取值**：校验在
            # `SearchRequestSerializer.validate_nature` 里做完（非法即 400），
            # 到这里只剩精确匹配。
            queryset = queryset.filter(classification=nature)
    if amount_min is not None:
        queryset = queryset.filter(amount_wan__gte=amount_min)
    return queryset


def serializer_has_structured(data) -> bool:
    """是否带了任一结构化条件（决定要不要收紧候选集合）。"""
    return bool(
        data.get("department")
        or data.get("customer")
        or data.get("nature")
        or data.get("amount_min") is not None
    )


class SearchView(APIView):
    """混合检索（稠密 + 关键词 + AS711 重排）。

    信息库下拉筛选与权限在**同一个地方**收敛：``libraries`` 参数只能收窄范围，
    永远不可能越过 ``readable_library_ids``；传了无权库的 code 会被静默剔除，
    而不是报错——不泄露"这个库存在但你无权"。
    """

    def get(self, request):
        serializer = SearchRequestSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return self._respond(request, serializer.validated_data)

    def post(self, request):
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._respond(request, serializer.validated_data)

    def _respond(self, request, data):
        allowed = readable_library_ids(request.user)
        if allowed is not None and not allowed:
            return Response(
                {
                    "query": data["q"],
                    "results": [],
                    "report": {"collections": [], "reason": "no_readable_library"},
                }
            )

        libraries = KnowledgeLibrary.objects.filter(is_active=True)
        if allowed is not None:
            libraries = libraries.filter(id__in=allowed)
        requested = data.get("libraries") or []
        if requested:
            # 下拉传 code；为便于脚本化也接受纯数字 id
            by_code = {item.code: item for item in libraries}
            by_id = {str(item.pk): item for item in libraries}
            libraries = [
                item
                for item in (by_code.get(key) or by_id.get(key) for key in requested)
                if item is not None
            ]
        else:
            libraries = list(libraries)
        if not libraries:
            return Response(
                {
                    "query": data["q"],
                    "results": [],
                    "report": {"collections": [], "reason": "no_matching_library"},
                }
            )

        # 权限码以"用户可读的库"为口径，而不是"这次选的库"：检索时任何一条命中
        # 都必须同时通过库权限与 generation 两道校验，口径越大越不会漏算。
        if allowed is None:
            permission_codes = None
        else:
            permission_codes = [f"library:{pk}" for pk in sorted(allowed)]

        # 数据库关键词兜底走的是 DocumentChunk，需要"用户可读 ∩ 本次筛选"的库集合
        # ——否则用户选了 A 库、却会收到 B 库（他有权限但没勾选）的命中。
        if allowed is None:
            effective_libraries = [item.pk for item in libraries]
        else:
            effective_libraries = [item.pk for item in libraries if item.pk in set(allowed)]

        # 结构化条件在**服务端全量**收敛成一个文件 id 集合，交给检索链路当约束。
        # 关键点：这里不做任何分页、也不用 q 去提前过滤——否则会漏掉"语义相关但
        # 不含原词"的资料，超过一页还会被静默截断。
        eligible_file_ids = None
        if serializer_has_structured(data):
            candidate_qs = _readable_files(request.user).filter(
                library_id__in=effective_libraries
            )
            candidate_qs = apply_file_filters(
                candidate_qs,
                department=data.get("department") or "",
                customer=data.get("customer") or "",
                nature=data.get("nature") or "",
                amount_min=data.get("amount_min"),
            )
            eligible_file_ids = set(candidate_qs.values_list("id", flat=True))
            if not eligible_file_ids:
                # 有结构化条件却没有任何候选：**立即空结果，不外呼模型**
                return Response(
                    {
                        "query": data["q"],
                        "results": [],
                        "report": {
                            "collections": [],
                            "reason": "no_eligible_file",
                            "eligible_files": 0,
                        },
                    }
                )

        outcome = pipeline.search(
            query=data["q"],
            libraries=libraries,
            permission_codes=permission_codes,
            limit=data.get("limit"),
            use_rerank=data.get("rerank"),
            keyword_library_ids=effective_libraries,
            eligible_file_ids=eligible_file_ids,
        )
        report = outcome["report"]
        report["libraries"] = [
            {"id": item.pk, "code": item.code, "name": item.name} for item in libraries
        ]
        report["permission_scoped"] = allowed is not None
        report["eligible_files"] = (
            len(eligible_file_ids) if eligible_file_ids is not None else None
        )
        # 基于反馈的重排加权（目标 ③）：**有界**微调次序，不做过滤/删除，
        # 并把权重与计数写回每条命中（可审计）。无反馈时次序完全不变。
        results = apply_feedback_weight(list(outcome["results"]))
        report["feedback_weighting"] = {
            "applied": True,
            "adjusted": sum(1 for item in results if item.get("feedback_weight")),
            "max_adjust": feedback_max_adjust(),
        }
        return Response({"query": data["q"], "results": results, "report": report})


@immutable_endpoint
class KnowledgeModelServiceView(APIView):
    """模型服务状态：**只读探测**，不提供任何停/改接口。

    需求要求"使用 AS711 上既有服务、禁止停改其他服务"，所以这里只有 GET：返回每个
    profile 的配置、目标维度、是否就绪（可选真实探一次），以及当前 active profile。
    """

    def get(self, request):
        if not is_system_admin(request.user):
            raise PermissionDenied("只有系统管理员可以查看模型服务配置")

        from .embedding import active_profile_name, assert_profile_ready, profiles
        from .ocr import default_profile as ocr_profile
        from .rerank import default_profile as rerank_profile

        probe = (request.query_params.get("probe") or "").lower() in {"1", "true", "yes"}
        # **按被点击的目标探测**（页面 4 行按钮各自对应一个登记服务）。
        # 白名单，**不接受任意 URL/参数**；未指定时保持向后兼容（只探当前生效的嵌入 profile）。
        target = (request.query_params.get("target") or "").strip().lower()
        allowed_targets = {"embedding", "rerank", "ocr", "llm"}
        if target and target not in allowed_targets:
            return Response(
                {"detail": f"不支持的目标：{target}；允许 {sorted(allowed_targets)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        active_name = active_profile_name()
        table = profiles()
        # **指定 profile 预检**（白名单 = 已配置表里的名字；**不接受任意 URL/参数**）：
        # 用途是"切换前先验证预留模型是否已就绪"，因此允许探测**非当前**的已配置 profile；
        # 名字不在表里直接 400，绝不接受外部传入的端点或任意参数。
        requested_profile = (request.query_params.get("profile") or "").strip()
        if requested_profile and requested_profile not in table:
            return Response(
                {
                    "detail": f"未知嵌入 profile：{requested_profile}；"
                    f"可用 {sorted(table)}（不接受任意端点/参数）"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        items = []
        for name, profile in table.items():
            item = {
                "name": name,
                "model": profile.model,
                "model_version": profile.model_version,
                "dimension": profile.dimension,
                "endpoint": profile.base_url,
                "active": name == active_name,
                "configured": profile.configured,
                "note": profile.note,
            }
            # 只在"被点的那一行"上探测：
            #   * 目标是 embedding，或未指定目标（兼容旧行为）→ 只探**当前生效**的 profile；
            #   * 绝不因为别的行被点击而去探**未配置**的 profile
            #     （实测问题：点 BGE 时把未配置的 2048 空 model 也探了，报出一串"失败"）。
            want_this = (
                bool(requested_profile) and name == requested_profile
            ) or (
                not requested_profile and target == "embedding" and name == active_name
            ) or (not requested_profile and not target and probe and name == active_name)
            if probe and want_this:
                # **按需探测 + 真实延迟**：显式请求才探测（AS711 是共享环境）；
                # 延迟是**实测**值，失败时记 probe_error，绝不编造"看起来正常"的延迟。
                started = time.perf_counter()
                try:
                    item["probe"] = assert_profile_ready(profile)
                    item["probe_latency_ms"] = round(
                        (time.perf_counter() - started) * 1000, 1
                    )
                except Exception as exc:  # noqa: BLE001 - 探测失败就是失败，不隐藏
                    item["probe_error"] = str(exc)
                    item["probe_latency_ms"] = round(
                        (time.perf_counter() - started) * 1000, 1
                    )
                item["probe_target"] = "embedding"
            items.append(item)

        rerank = rerank_profile()
        ocr = ocr_profile()

        def _probe_rerank(profile_obj) -> dict:
            """**真实调用**：跑一次最小重排（2 条极短文本），验证协议解析。

            独立审查要求：不再用 ``GET /models`` 冒充"推理能力" —— 那条请求
            **不经过模型客户端**（不读 ``build_http_client`` 的 ``trust_env=False``、
            也不带鉴权头），端点"有 HTTP 服务但没有 /v1/models"时还会误报未连通。
            这里改用真实 ``RerankClient``（与生产同一条外呼与解析路径）。
            """
            started = time.perf_counter()
            result: dict = {"probe_target": "rerank", "connectivity_only": False}
            if not getattr(profile_obj, "base_url", ""):
                result["probe_error"] = "未配置端点"
                return result
            try:
                from .rerank import RerankClient

                hit = RerankClient(profile_obj).rerank(
                    "质保期", ["质保期 12 个月", "与之无关的一段文字"], top_n=2
                )
                result["connected"] = True
                result["top_index"] = hit[0].index if hit else None
                result["top_score"] = hit[0].score if hit else None
            except Exception as exc:  # noqa: BLE001 - 失败就是失败，不隐藏
                result["probe_error"] = f"{type(exc).__name__}: {exc}"
            result["probe_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        def _probe_ocr(profile_obj) -> dict:
            """**真实调用**：把仓库里固定的自检样图交给真实 OCR 客户端识别。

            判据是**识别出样图关键词**（固定样图已在真实服务上验证过可识别），
            而不是"HTTP 通了"。样图与判定见 ``apps/knowledge/ocr_selfcheck.py``。
            """
            from . import ocr_selfcheck

            started = time.perf_counter()
            result: dict = {"probe_target": "ocr", "connectivity_only": False}
            if not getattr(profile_obj, "base_url", ""):
                result["probe_error"] = "未配置端点"
                return result
            try:
                from .ocr import OcrClient

                text = OcrClient(profile_obj).recognize_image(
                    ocr_selfcheck.sample_bytes(), hint="自检"
                )
                missing = ocr_selfcheck.missing_needles(text)
                result["matched_tokens"] = [
                    token for token in ocr_selfcheck.EXPECTED_NEEDLES if token not in missing
                ]
                result["missing_tokens"] = missing
                result["sample_text"] = (text or "")[:200]
                if missing:
                    result["probe_error"] = "有响应但没识别出这些关键词：" + "、".join(missing)
                else:
                    result["connected"] = True
            except Exception as exc:  # noqa: BLE001 - 失败就是失败，不隐藏
                result["probe_error"] = f"{type(exc).__name__}: {exc}"
            result["probe_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result

        rerank_payload = {
            "model": rerank.model,
            "endpoint": rerank.base_url,
            "configured": bool(rerank.base_url),
        }
        ocr_payload = {
            "model": ocr.model,
            "endpoint": ocr.base_url,
            "configured": bool(ocr.base_url),
        }
        if probe and target == "rerank":
            rerank_payload.update(_probe_rerank(rerank))
        if probe and target == "ocr":
            ocr_payload.update(_probe_ocr(ocr))
        llm_payload: dict = {}
        if probe and target == "llm":
            # LLM 的真实**最小调用**探测（走真实 LlmClient，不是只 GET /models）。
            llm_payload = _probe_llm_endpoint()
        return Response(
            {
                "active_profile": active_profile_name(),
                "profiles": items,
                "probe_target": target or ("embedding" if probe else ""),
                "rerank": rerank_payload,
                "ocr": ocr_payload,
                "llm": llm_payload,
            }
        )


def _probe_llm_endpoint() -> dict:
    """LLM 的按需探测：发一次**最小** ``chat/completions``，记录实测延迟。

    为什么不是 GET /models：那只证明"端口后面有个 HTTP 服务"。对 LLM 来说要确认的是
    **能完成一次对话补全**，所以这里真的调一次（极小预算），并把结果如实标注为
    ``connectivity_only=True`` —— **只证明连通，不证明回答质量**。

    关于空正文：本环境的模型是**推理型**（先输出 reasoning），预算很小时
    ``content`` 可能为空、``finish_reason=length``。这**仍然算连通成功**
    （HTTP 200 且返回合法 JSON），并把 ``finish_reason``/``content_empty`` 一并返回，
    供诊断"为什么有时拿不到正文"；**不会**把它伪装成一次有效回答。

    离线开关（``KB_MODEL_CALLS_DISABLED``）打开时**不发任何请求**。
    """
    from apps.knowledge.resilience import model_calls_disabled
    from apps.planner.llm import LlmClient, default_profile

    result: dict = {
        "probe_target": "llm",
        "connectivity_only": False,
        "implemented": True,
    }
    profile = default_profile()
    result["model"] = profile.model
    result["endpoint"] = profile.base_url
    if not profile.configured:
        result["probe_error"] = "未配置 LLM 端点或模型（MODEL_SERVICES['llm']）"
        return result
    if model_calls_disabled():
        # 离线模式：连 http 客户端都不造，也绝不外呼
        result["skipped"] = True
        result["probe_error"] = (
            "模型调用已被禁用（KB_MODEL_CALLS_DISABLED），未发起任何请求"
        )
        return result

    # **走真实客户端**：与生产同一条外呼路径（`build_http_client` → `trust_env=False`
    # 不受系统代理影响、带鉴权头、熔断/重试口径一致），不再手搓 `httpx.post`。
    # 预算调到最小：探测只验证"能完成一次调用"，不验证生成质量。
    import dataclasses

    probe_profile = dataclasses.replace(profile, max_tokens=64, temperature=0.0)
    started = time.perf_counter()
    try:
        reply = LlmClient(probe_profile).chat([{"role": "user", "content": "ping"}])
        result["connected"] = True
        result["finish_reason"] = reply.finish_reason or ""
        result["content_empty"] = not str(reply.content or "").strip()
        result["usage"] = reply.usage or {}
        result["note"] = (
            "已能完成一次真实 chat/completions 调用（走 LlmClient，含鉴权与代理保护）；"
            "仅证明连通，不代表回答质量。"
            + (
                "本次预算很小，推理型模型可能先输出思考而正文为空"
                f"（finish_reason={result['finish_reason']}），属正常现象。"
                if result["content_empty"]
                else ""
            )
        )
    except Exception as exc:  # noqa: BLE001 - 失败就是失败，不隐藏
        text = str(exc)
        # **被探测预算截断 ≠ 服务不可用**：HTTP 与协议都通了，只是 8~64 token 的
        # 小预算被推理模型的思考过程吃光（finish_reason=length）。这要如实说成
        # "连通但被预算截断"，不能报成"不可用"（也不能悄悄当成成功）。
        if "token 上限截断" in text or "finish_reason=length" in text:
            result["connected"] = True
            result["finish_reason"] = "length"
            result["content_empty"] = True
            result["note"] = (
                "已能完成一次真实 chat/completions 调用；本次探测预算很小，"
                "推理型模型把预算用在思考上导致正文被截断（finish_reason=length），"
                "这**不影响**连通性判定，也不代表回答质量。"
            )
        else:
            result["probe_error"] = f"{type(exc).__name__}: {text}"
    result["probe_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@immutable_endpoint
class KnowledgeRebuildView(APIView):
    """索引初建 / 重建（**可队列执行**）：建新代 → 全量重建 → 校验 → 原子激活。

    为什么要有 API 而不只是管理命令：
    "新库首传前必须先重建"这种要求如果只能靠命令行，就等于把运维动作塞进了
    用户流程。这里提供 API，并且上传接口还会**自动初建**第一代，
    所以"建库 → 首上传 → 处理"完全不需要人工介入。
    """

    def post(self, request):
        if not is_system_admin(request.user):
            raise PermissionDenied("只有系统管理员可以重建向量索引")
        from .embedding import active_profile_name as _active_name, get_profile

        library = self._library(request.data.get("library"))
        profile_name = request.data.get("profile") or _active_name()
        profile = get_profile(profile_name)

        # 未配置 model/base_url 的 profile（例如未部署的 bge2048）直接拒绝：
        # 不允许伪造升维，也不允许在没有真实模型时预先声明维度。
        if not profile.configured:
            raise ValidationError(
                {"profile": f"{profile_name} 未配置（模型/端点为空），拒绝用它建索引"}
            )

        # 冻结被拒（有活跃租约 / 已被别的重建冻结）→ **409**，不是 500。
        # 全局处理器也会兜这一层，但这里显式处理能把"重建"这个上下文
        # 一起带回去（report 形状统一：前端不必为两种响应写两套解析）。
        from .writegate import WriteFrozen

        try:
            report = pipeline.rebuild_library(library, profile=profile)
        except WriteFrozen as exc:
            append_event(
                action="vector.library_rebuild_refused",
                actor_id=request.user.pk,
                resource_kind="knowledge_library",
                resource_key=library.code,
                payload={
                    "reason": getattr(exc, "reason", "") or str(exc),
                    "frozen_by": getattr(exc, "frozen_by", "") or "",
                    "profile": profile.name,
                },
            )
            return Response(
                {
                    "library": library.code,
                    "generation": 0,
                    "collection": "",
                    "dimension": profile.dimension,
                    "files_total": 0,
                    "files_processed": 0,
                    "files_failed": [],
                    "activated": False,
                    "reason": str(exc),
                    "frozen": True,
                    "frozen_by": getattr(exc, "frozen_by", "") or "",
                    "retryable": True,
                    "code": "write_frozen",
                },
                status=status.HTTP_409_CONFLICT,
            )
        append_event(
            action="vector.library_rebuilt",
            actor_id=request.user.pk,
            resource_kind="knowledge_library",
            resource_key=library.code,
            payload={
                "generation": report["generation"],
                "collection": report["collection"],
                "activated": report["activated"],
                "files_total": report["files_total"],
                "files_processed": report["files_processed"],
                "files_failed": len(report["files_failed"]),
                "profile": profile.name,
            },
        )
        status_code = (
            status.HTTP_201_CREATED if report["activated"] else status.HTTP_409_CONFLICT
        )
        return Response(report, status=status_code)

    def _library(self, value):
        """按 code 或 id 取信息库（两个视图各自独立，不互相继承）。"""
        if not value:
            raise ValidationError({"library": "必须指定信息库（code 或 id）"})
        text = str(value)
        queryset = KnowledgeLibrary.objects.all()
        library = (
            queryset.filter(pk=int(text)).first()
            if text.isdigit()
            else queryset.filter(code=text).first()
        )
        if library is None:
            raise NotFound(f"信息库不存在：{value}")
        return library


@immutable_endpoint
class KnowledgeGenerationView(APIView):
    """向量 generation：新建 collection（POST）与原子切换/回退。

    **注意（审计澄清，避免踩坑）**：类上的 ``@immutable_endpoint`` 会把
    ``put``/``patch``/``delete`` 换成只读拒绝（403 ``immutable_resource``），
    因此下面那个 ``put`` 在 HTTP 上**永远进不来** —— 这是**有意**的：
    切换/回退只能走受控意图 ``activate_generation`` / ``rollback_generation``
    （`apps/planner/services.py`，带权限与确认），
    `tests/test_agent_console_reachability.py` 也守着"前端不许直连 PUT"。
    这里保留 ``put`` 的实现是为了让"该有的校验长什么样"有单一参照（服务层复用同一套
    ``GenerationPlan`` + ``vectorindex.activate_generation``），**不要**以为它能被调用。
    """

    def post(self, request):
        """为某库新建一个 generation 的 collection——**不动线上索引**。"""
        if not is_system_admin(request.user):
            raise PermissionDenied("只有系统管理员可以重建向量索引")
        from .embedding import active_profile_name as _active_name, get_profile
        from .vectorindex import prepare_generation

        library = self._library(request.data.get("library"))
        profile_name = request.data.get("profile") or _active_name()
        profile = get_profile(profile_name)
        if not profile.configured:
            raise ValidationError(
                {
                    "profile": f"{profile_name} 尚未声明就绪（拒绝用它建索引：不允许伪造升维，"
                    "也不允许在没有真实模型的情况下预先声明维度）"
                }
            )
        plan = prepare_generation(library, profile=profile)
        append_event(
            action="vector.generation_prepared",
            actor_id=request.user.pk,
            resource_kind="knowledge_library",
            resource_key=library.code,
            payload={
                "generation": plan.generation,
                "collection": plan.collection,
                "dimension": plan.dimension,
                "model": plan.model,
                "model_version": plan.model_version,
                "previous_generation": plan.previous_generation,
                "profile": profile.name,
            },
        )
        return Response(
            {
                "library": library.code,
                "generation": plan.generation,
                "collection": plan.collection,
                "dimension": plan.dimension,
                "model": plan.model,
                "model_version": plan.model_version,
                "previous_generation": plan.previous_generation,
                "previous_collection": plan.previous_collection,
                "profile": profile.name,
                "active_profile": _active_name(),
            },
            status=status.HTTP_201_CREATED,
        )

    def put(self, request):
        """原子切换：库的 active_generation 与每个文件的索引指向在同一事务里改。"""
        if not is_system_admin(request.user):
            raise PermissionDenied("只有系统管理员可以切换向量索引")
        from .vectorindex import GenerationPlan, VectorIndexError, activate_generation

        library = self._library(request.data.get("library"))
        generation = int(request.data.get("generation", 0) or 0)
        collection = request.data.get("collection", "")
        if generation <= 0 or not collection:
            raise ValidationError({"generation": "generation 与 collection 都是必填"})
        registered = VectorCollection.objects.filter(library=library, name=collection).first()
        if registered is None:
            raise ValidationError(
                {"collection": f"collection {collection} 没有登记过，不能切换过去"}
            )
        # **代次必须以登记名里的事实为准，不能信调用方传的 generation**：
        # 否则 PUT {generation: 99, collection: kb-1-g1} 会把库指针写成 g99、
        # 而真实 collection 是 g1 —— 指针与事实分叉，后续回退/重建立刻出问题。
        from .vectorindex import _generation_of

        actual_generation = _generation_of(library, collection)
        if actual_generation <= 0:
            raise ValidationError(
                {"collection": f"collection 名 {collection} 解析不出代次，拒绝切换"}
            )
        if generation != actual_generation:
            raise ValidationError(
                {
                    "generation": f"请求的 generation={generation} 与 collection "
                    f"{collection} 真实代次 {actual_generation} 不一致，拒绝切换"
                }
            )
        plan = GenerationPlan(
            library_id=library.pk,
            generation=actual_generation,
            collection=collection,
            dimension=registered.dimension,
            model=registered.embedding_model,
            model_version=registered.model_version,
            previous_generation=library.active_generation,
            previous_collection="",
            created=False,
        )
        try:
            updated = activate_generation(library, plan)
        except VectorIndexError as exc:
            # 空索引 / 半成品索引：明确拒绝，并告诉调用方缺什么
            raise ValidationError({"generation": str(exc)}) from exc
        append_event(
            action="vector.generation_activated",
            actor_id=request.user.pk,
            resource_kind="knowledge_library",
            resource_key=library.code,
            payload={
                "generation": plan.generation,
                "collection": plan.collection,
                "dimension": plan.dimension,
                "previous_generation": plan.previous_generation,
            },
        )
        return Response(
            {
                "library": updated.code,
                "active_generation": updated.active_generation,
                "collection": plan.collection,
                "previous_generation": plan.previous_generation,
                "files_moved": IngestedFile.objects.filter(library=updated).count(),
            }
        )

    def _library(self, value):
        if not value:
            raise ValidationError({"library": "必须指定信息库（code 或 id）"})
        queryset = KnowledgeLibrary.objects.all()
        text = str(value)
        library = (
            queryset.filter(pk=int(text)).first()
            if text.isdigit()
            else queryset.filter(code=text).first()
        )
        if library is None:
            raise NotFound(f"信息库不存在：{value}")
        return library


@immutable_endpoint
class KnowledgeIndexStatsView(APIView):
    """索引自证数据：容器里的真实点数 vs 库里的登记数，用来核对"确实写进去了"。"""

    def get(self, request):
        if not is_system_admin(request.user):
            raise PermissionDenied("只有系统管理员可以查看索引统计")
        from .vectorindex import build_client, collection_exists, count_points

        client = build_client()
        try:
            rows = []
            for record in VectorCollection.objects.select_related("library").order_by("id"):
                exists = collection_exists(client, record.name)
                rows.append(
                    {
                        "name": record.name,
                        "library": record.library.code if record.library else "",
                        "dimension": record.dimension,
                        "model": record.embedding_model,
                        "model_version": record.model_version,
                        "profile": record.profile,
                        "is_active": record.is_active,
                        # 退役待删（持久删除状态）：**不是生效代次、也不可被激活/回退**，
                        # 只是远端还没删掉（或上次删失败，下次重建/清理会重试）。
                        # 界面据此如实提示，别让管理员以为它还能用。
                        "retiring": record.retiring_at is not None,
                        "retiring_at": (
                            record.retiring_at.isoformat() if record.retiring_at else ""
                        ),
                        "exists_in_qdrant": exists,
                        "points": count_points(client, record.name) if exists else 0,
                        "chunks": record.chunk_count,
                    }
                )
            return Response(
                {
                    "collections": rows,
                    "files": {
                        "total": IngestedFile.objects.count(),
                        "parsed": IngestedFile.objects.filter(
                            parse_status=ParseStatus.DONE
                        ).count(),
                        "failed": IngestedFile.objects.filter(
                            parse_status=ParseStatus.FAILED
                        ).count(),
                        "chunks": DocumentChunk.objects.count(),
                    },
                }
            )
        finally:
            client.close()


class KnowledgeStatsView(APIView):
    """首页统计卡数据源（数量口径与原型一致）。"""

    def get(self, request):
        allowed = readable_library_ids(request.user)
        files = IngestedFile.objects.all()
        libraries = KnowledgeLibrary.objects.filter(is_active=True)
        if allowed is not None:
            files = files.filter(library_id__in=allowed)
            libraries = libraries.filter(id__in=allowed)
        return Response(
            {
                "libraries": libraries.count(),
                "files": files.count(),
                "done": files.filter(parse_status=ParseStatus.DONE).count(),
                "pending": files.filter(parse_status=ParseStatus.PENDING).count(),
                # 处理中的数量：首页"待处理"标签与统计需要它，不能只算 pending。
                "processing": files.filter(parse_status=ParseStatus.RUNNING).count(),
                "failed": files.filter(parse_status=ParseStatus.FAILED).count(),
                # 会话来源必须**精确**等于 source="session"，不能把目录扫描也算进来。
                "session_files": files.filter(source="session").count(),
                "chunks": DocumentChunk.objects.filter(file__in=files.values("id")).count(),
            }
        )


def _preview_headers(response):
    """预览类响应统一的**安全缓存头**。

    为什么要 `no-store` 而不是 `max-age`：预览内容受**账号与授权**约束 ——
    切账号或撤权后，浏览器若沿用旧页会读到**别人/已失去权限**的内容。
    因此一律不缓存；并加 `nosniff` 防止内容被当作其它类型解释。
    """
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@immutable_endpoint
class FileProgressView(APIView):
    """仅返回当前用户可读资料的轻量进度，不返回正文或内部租约标识。"""
    permission_classes = [IsAuthenticated]
    def get(self, request):
        from .progress import file_snapshot, read_many, request_ids
        ids = request_ids(request.query_params.get('ids'))
        rows = list(_readable_files(request.user).filter(pk__in=ids))
        snapshots = read_many('file', [row.pk for row in rows if row.parse_status in {'running', 'failed'}])
        return Response({'results': [{
            'id': row.pk, 'parse_status': row.parse_status, 'status_label': row.get_parse_status_display(),
            'progress': file_snapshot(row, snapshots.get(row.pk, {})),
            'chunk_count': row.chunk_count, 'embedded_chunk_count': row.embedded_chunk_count,
            'parse_error': row.parse_error if request.user.is_staff else '',
            'parsed_at': row.parsed_at.isoformat() if row.parsed_at else None,
        } for row in rows]}, headers={'Cache-Control': 'private, no-store'})


@immutable_endpoint
class FilePreviewMetaView(APIView):
    """原件预览元数据：**说明这是什么文件、能不能按原件渲染、端点在哪**。

    权限：与列表/详情同一口径（`_readable_files`）；不可读一律 **404**，
    不泄露"这个 id 存在但不属于你"。

    **不暴露 MEDIA**：预览数据由本视图按记录解析原件后**流式**返回，
    客户端拿不到任何静态目录路径。
    """

    permission_classes = (IsAuthenticated,)

    def get(self, request, pk: int):
        from .preview import build_preview

        record = _readable_files(request.user).filter(pk=pk).select_related("library").first()
        if record is None:
            return Response({"detail": "资料不存在"}, status=status.HTTP_404_NOT_FOUND)

        payload = build_preview(record)
        # 预览只读本地原件并回给已授权用户；**不涉及任何模型外呼**
        payload["external_model_calls"] = False
        return _preview_headers(Response(payload))


class FilePreviewPageView(APIView):
    """PDF **按原页**渲染成 PNG（保留表格与水印位置，不重排）。"""

    permission_classes = (IsAuthenticated,)

    def get(self, request, pk: int, page: int):
        from django.http import HttpResponse

        from .preview import render_pdf_page, resolve_original

        record = _readable_files(request.user).filter(pk=pk).first()
        if record is None:
            return Response({"detail": "资料不存在"}, status=status.HTTP_404_NOT_FOUND)
        path, reason = resolve_original(record)
        if path is None:
            return Response({"detail": reason}, status=status.HTTP_404_NOT_FOUND)
        # 页码是 **1 基**（界面上给用户看的是第 N 页）；缩放有界
        if page < 1:
            return Response({"detail": "页码必须从 1 开始"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            scale = float(request.query_params.get("scale") or 1.5)
        except (TypeError, ValueError):
            return Response({"detail": "scale 必须是数字"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            png = render_pdf_page(path, page - 1, scale)
        except IndexError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - 坏 PDF 如实报错，不返回空图
            return Response(
                {"detail": f"该页无法渲染：{type(exc).__name__}: {exc}"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        response = HttpResponse(png, content_type="image/png")
        response["X-Preview-Page"] = str(page)
        # **不缓存**：切账号/撤权后不得沿用旧页
        return _preview_headers(response)


class FileOriginalDownloadView(APIView):
    """下载**原件字节**（受鉴权保护，不经静态目录）。"""

    permission_classes = (IsAuthenticated,)

    def get(self, request, pk: int):
        from django.http import FileResponse

        from .preview import content_type_for, resolve_original

        record = _readable_files(request.user).filter(pk=pk).first()
        if record is None:
            return Response({"detail": "资料不存在"}, status=status.HTTP_404_NOT_FOUND)
        path, reason = resolve_original(record)
        if path is None:
            return Response({"detail": reason}, status=status.HTTP_404_NOT_FOUND)
        # 以只读句柄流式返回；**不复制、不修改**原件字节
        handle = open(path, "rb")
        response = FileResponse(
            handle,
            content_type=content_type_for(record),
            as_attachment=True,
            filename=record.original_name or f"file-{record.pk}",
        )
        response["X-Original-Sha256"] = (record.content_sha256 or "")[:64]
        return _preview_headers(response)


class IsSystemAdminForMetrics(BasePermission):
    """运行指标（队列深度、熔断状态、巡检读数）只给系统管理员。

    **为什么做成权限类而不是在函数体里判断**（Codex 审计 OP-20）：原实现是
    ``permission_classes = ()`` + 函数体内判权，而 ``collect()`` 在判权**之前**执行 ——
    任何登录用户（乃至匿名请求）都能触发一次真实的指标采集，然后才拿到 403。
    DRF 在进入 handler 之前先跑权限，所以这样写才能真正"先拒绝、后采集"。
    """

    message = "需要系统管理员权限才能查看运行指标"

    def has_permission(self, request, view):
        return bool(is_system_admin(getattr(request, "user", None)))


class KnowledgeRuntimeMetricsView(APIView):
    """真实运行指标（REST）。实现与 AI 意图**共用** `metrics.collect()`。

    只报能真正测到的值；未实现的（看门狗/巡检）在 `unavailable` 里如实列出，
    不填假数字。业务队列（broker DB0/DB1）拒绝探测。
    """

    #: 权限在 handler 之前生效 → 未授权请求**不会**触发 collect()
    permission_classes = (IsSystemAdminForMetrics,)

    def get(self, request):
        from .metrics import collect

        # 权限已由 IsSystemAdminForMetrics 在进入本方法前判定（因此 collect() 只对
        # 授权用户执行）；这里不再重复判权，避免"先采集再拒绝"的老问题。
        return Response(collect())

class SearchFeedbackView(APIView):
    """检索命中的人工反馈（"准确 / 不准确"）持久化。

    为什么要落库而不是只在前端记一下：反馈是后续"基于反馈的学习规则"
    （重排加权）的**唯一输入**，必须可审计、可复盘、可回滚。
    它**不修改源资料**，也不重写向量 —— 只影响排序权重。
    """

    #: 允许随反馈一起留存的检索条件字段（与 SearchView 接受的保持一致）
    CONDITION_FIELDS = (
        "department", "customer", "nature",
        "amount_min", "amount_wan", "libraries",
    )

    def post(self, request):
        data = request.data or {}
        verdict = str(data.get("verdict") or "").strip()
        if verdict not in SearchFeedback.Verdict.values:
            raise ValidationError(
                {"verdict": f"必须是 {list(SearchFeedback.Verdict.values)} 之一"}
            )
        query = str(data.get("query") or "").strip()
        if not query:
            raise ValidationError({"query": "不能为空（反馈必须对应一次具体检索）"})
        query = query[:512]

        # **反馈粒度必须显式**（Codex 审查）：加权是按**资料**聚合的
        # （`feedback.feedback_weights` 用 ``values("file_id")``），所以结构化/列表类
        # 结果本来就没有"第几片"。旧实现把缺失的序号默认成 0，等于把资料级评价
        # 登记成"第 0 片"，数据语义是错的。现在：
        #   * 显式给了 chunk_ordinal → 分片级（scope=chunk），必须是非负整数；
        #   * 没给 chunk_ordinal → **资料级**（scope=file），显式在 conditions 里
        #     留下 ``scope=file`` 标记，复盘时能分清"资料级"与"第 0 片"；
        #   * 调用方也可以显式传 scope 覆盖推断（chunk 时必须给序号）。
        scope_raw = data.get("scope")
        raw_chunk = data.get("chunk_ordinal")
        if scope_raw not in (None, ""):
            scope = str(scope_raw).strip().lower()
            if scope not in {"chunk", "file"}:
                raise ValidationError({"scope": "必须是 chunk 或 file"})
        else:
            scope = "chunk" if raw_chunk not in (None, "") else "file"
        if scope == "file":
            chunk_ordinal = 0
        else:
            if raw_chunk in (None, ""):
                raise ValidationError(
                    {"chunk_ordinal": "分片级反馈必须给出 chunk_ordinal；资料级反馈请传 scope=file"}
                )
            try:
                chunk_ordinal = int(raw_chunk)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"chunk_ordinal": "必须是整数"}) from exc
            if chunk_ordinal < 0:
                raise ValidationError({"chunk_ordinal": "不能为负数"})

        target = None
        file_id = data.get("file_id")
        if file_id not in (None, ""):
            # 与列表/检索**同一口径**：只允许对自己可读的资料反馈。
            # 越权或不存在的 id 一律 404（不泄露"这份资料存在但你无权"）。
            target = _readable_files(request.user).filter(pk=file_id).first()
            if target is None:
                raise NotFound("资料不存在或无权访问")

        conditions = {
            key: data[key] for key in self.CONDITION_FIELDS if data.get(key) not in (None, "")
        }
        # 粒度写进 conditions（JSON 列，无需迁移）：复盘/加权时能分清资料级与分片级
        conditions["scope"] = scope
        feedback, created = SearchFeedback.objects.update_or_create(
            user=request.user,
            query=query,
            file=target,
            chunk_ordinal=chunk_ordinal,
            defaults={
                "verdict": verdict,
                "note": str(data.get("note") or "")[:2000],
                "conditions": conditions,
            },
        )
        # 审计：反馈会影响排序权重，必须留痕（源资料不受影响）
        append_event(
            action="search_feedback",
            actor_id=request.user.pk,
            actor_label=getattr(request.user, "username", "") or "",
            resource_kind="IngestedFile" if target else "SearchQuery",
            resource_key=str(target.pk) if target else "",
            payload={
                "verdict": verdict,
                "query": query,
                "chunk_ordinal": chunk_ordinal,
                "scope": scope,
                "created": created,
            },
        )
        return Response(
            {
                "id": feedback.pk,
                "verdict": feedback.verdict,
                "query": feedback.query,
                "file_id": feedback.file_id,
                "chunk_ordinal": feedback.chunk_ordinal,
                "scope": scope,
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def get(self, request):
        """只列**自己**的反馈（分页），并带上资料名与该资料当前的净分。

        净分用与 `list_feedback_targets` / `reparse_failed` **同一实现**
        （apps/knowledge/integrity.py），所以界面上的"待重扫"与指标里的
        「待处理反馈」永远一致；且只对**当前可读**的资料给出净分。
        """
        from .integrity import net_negative_feedback_rows

        queryset = (
            SearchFeedback.objects.filter(user=request.user)
            .select_related("file")
            .order_by("-created_at", "-id")
        )
        paginator = KbPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        net_by_file = {
            row["file_id"]: row["net"] for row in net_negative_feedback_rows()
        }
        readable = set(
            _readable_files(request.user)
            .filter(pk__in=[item.file_id for item in page if item.file_id])
            .values_list("pk", flat=True)
        )
        response = paginator.get_paginated_response(
            [
                {
                    "id": item.pk,
                    "verdict": item.verdict,
                    "verdict_label": item.get_verdict_display(),
                    "query": item.query,
                    "file_id": item.file_id,
                    "file_name": (
                        (item.file.original_name or item.file.name)
                        if item.file_id
                        else ""
                    ),
                    "chunk_ordinal": item.chunk_ordinal,
                    "note": item.note,
                    "conditions": item.conditions,
                    "created_at": item.created_at.isoformat(),
                    # 分诊留痕（原型那列「标记已处理」）：**只表示有人看过了**，
                    # 不改变 net / 待处理反馈 / 重扫口径。
                    "handled": item.handled_at is not None,
                    "handled_at": (
                        item.handled_at.isoformat() if item.handled_at else None
                    ),
                    "handled_by": (
                        getattr(item.handled_by, "username", "") or ""
                        if item.handled_by_id
                        else ""
                    ),
                    # 净分 < 0 → 这份资料会被「重扫未处理反馈」覆盖（同一口径）
                    "net": (
                        net_by_file.get(item.file_id)
                        if item.file_id in readable
                        else None
                    ),
                }
                for item in page
            ]
        )
        response.data["note"] = (
            "只显示我自己的反馈；净分与「重扫未处理反馈」同一口径"
        )
        return response


class SearchFeedbackHandleView(APIView):
    """把一条检索反馈标记为「已处理」（**分诊留痕**，原型那列「标记已处理」）。

    语义边界（刻意保持窄）：
    - **只**记录"谁在什么时候看过这条批注"，用于运营分诊；
    - **不改变**检索加权（加权只看 verdict 的净分），**不改变**
      「待处理反馈 / 净分 / 重扫未处理反馈」的口径，也不把该行排除出重扫；
    - 可处理**自己**的反馈；系统管理员可处理全部（其他人一律 404，不泄露存在性）；
    - 幂等：已处理过的再点一次不会刷新处理人与时间（保留第一次留痕）。
    """

    def post(self, request, pk: int):
        feedback = SearchFeedback.objects.select_related("file", "handled_by").filter(pk=pk).first()
        if feedback is None or (
            feedback.user_id != request.user.pk
            and not is_system_admin(request.user)
        ):
            raise NotFound("反馈不存在或无权处理")

        already_handled = feedback.handled_at is not None
        if not already_handled:
            feedback.handled_at = timezone.now()
            feedback.handled_by = request.user
            feedback.save(update_fields=["handled_at", "handled_by"])
            append_event(
                action="search_feedback_handled",
                actor_id=request.user.pk,
                actor_label=getattr(request.user, "username", "") or "",
                resource_kind="SearchFeedback",
                resource_key=str(feedback.pk),
                payload={
                    "verdict": feedback.verdict,
                    "query": feedback.query,
                    "file_id": feedback.file_id,
                    "chunk_ordinal": feedback.chunk_ordinal,
                    "handled_at": feedback.handled_at.isoformat(),
                },
            )

        return Response(
            {
                "id": feedback.pk,
                "handled": True,
                "already_handled": already_handled,
                "handled_at": (
                    feedback.handled_at.isoformat() if feedback.handled_at else None
                ),
                "handled_by": (
                    getattr(feedback.handled_by, "username", "") or ""
                    if feedback.handled_by_id
                    else ""
                ),
                "note": (
                    "分诊留痕：不改变检索加权，也不影响「待处理反馈 / 重扫未处理反馈」的口径。"
                ),
            }
        )
