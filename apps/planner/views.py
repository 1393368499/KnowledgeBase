"""会话与操作 API：仅删除需要确认，其余参数完整且通过权限核验后直接执行。"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.resilience import ServiceUnavailable
from governance.immutable import immutable_endpoint

from . import services
from apps.voice.locking import ConversationBusy
from .document_templates import (
    TemplateError,
    TemplateStorageError,
    create_template,
    delete_template,
    template_dict,
    template_path,
)
from .llm import LLMError, LlmClient
from .models import (
    ActionTicket,
    Conversation,
    DeletionJob,
    DerivedDocument,
    DocumentTemplate,
    Message,
    OperationRecord,
)
from .operations import all_specs, get as get_spec, is_mutation

logger = logging.getLogger(__name__)

#: 一次对话最多限定多少个信息库。限定选择是"收窄范围"，不是"批量列举"；
#: 给个上限是为了不让一次请求带上几千个 code 去撞数据库。
MAX_LIBRARY_SCOPE = 20


def _llm_unavailable_response(exc: Exception) -> Response:
    """模型不可用时的统一响应：**如实说明**，不给假回答。"""
    return Response(
        {
            "code": "assistant_unavailable",
            "detail": "智能助手当前不可用；你的问题已记录，稍后可以重试。",
            "reason": str(exc),
            "hint": "可以在“文件管理/检索”里直接用关键词查找资料，不依赖助手。",
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _library_codes_from_request(request) -> list[str] | None:
    """解析"知识库调用选择"（``library_codes``）。

    取值口径（刻意把三种情况分开，不合并）：

    - **省略字段 / 显式 null** → ``None``：没选，检索用户有权查看的全部信息库（默认行为）；
    - **显式空数组 / 空字符串** → 400：这**不是**"全库"的同义词。把它当全库，就等于
      让"我清空了选择"与"我要全库"这两件事在协议上无法区分，也等于把一次误清空
      静默放大成全局检索；
    - 非法类型 → 400；重复 code 去重、保持用户给的顺序（界面按顺序回显）。
    """
    if "library_codes" not in request.data:
        return None
    raw = request.data.get("library_codes")
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)):
        items = str(raw).split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("library_codes 必须是信息库 code 的数组（或逗号分隔字符串）")
    codes = [str(item).strip() for item in items if str(item).strip()]
    if not codes:
        raise ValueError(
            "library_codes 不能为空数组/空字符串；要检索全部信息库请**省略该字段**"
        )
    if len(codes) > MAX_LIBRARY_SCOPE:
        raise ValueError(f"library_codes 最多 {MAX_LIBRARY_SCOPE} 个信息库")
    return list(dict.fromkeys(codes))


class OperationCatalogView(APIView):
    """操作登记表的只读目录：界面用它渲染"可点选的操作"。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "count": len(all_specs()),
                "results": [
                    {**spec.to_dict(), "is_mutation": is_mutation(spec.intent)}
                    for spec in all_specs()
                ],
            }
        )


@immutable_endpoint
class ChatView(APIView):
    """首页会话：发一句话，得到助手回复 + **待确认的操作提案**。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        content = str(request.data.get("message", "")).strip()
        if not content:
            return Response(
                {"message": "请输入内容"}, status=status.HTTP_400_BAD_REQUEST
            )
        # 「知识库调用选择」：用户在输入框旁勾选的信息库。省略 = 不限定（全部可读库）。
        try:
            library_codes = _library_codes_from_request(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        conversation_id = request.data.get("conversation_id")
        try:
            conversation = services.get_or_create_conversation(
                request.user, int(conversation_id) if conversation_id else None
            )
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = services.reply_to_message(
                request.user, conversation, content, library_codes=library_codes
            )
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ConversationBusy as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ServiceUnavailable as exc:
            # 模型服务不可用：**不伪造回答**
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.SYSTEM,
                content=f"助手不可用：{exc.reason}",
                payload={"unavailable": True, "reason": exc.reason},
            )
            return _llm_unavailable_response(exc)
        except LLMError as exc:
            # 与上面的 ServiceUnavailable 分支**保持对称**：模型报错（含"返回空答复"）
            # 也必须留痕。只给本次 HTTP 503 的话，用户刷新会话后只剩自己那句提问，
            # 看不到"为什么这次没有回答"（与审查条目 57 是同一类问题）。
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.SYSTEM,
                content=f"助手不可用：{exc}",
                payload={"unavailable": True, "reason": str(exc)},
            )
            return _llm_unavailable_response(exc)
        return Response(payload, status=status.HTTP_201_CREATED)


class ConversationView(APIView):
    """读取会话历史（只读）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        conversation = get_object_or_404(Conversation, pk=pk, user=request.user)
        return Response(
            {
                "id": conversation.pk,
                "title": conversation.title,
                "messages": [
                    {
                        "id": row.pk,
                        "role": row.role,
                        "content": row.content,
                        "payload": row.payload,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in conversation.messages.order_by("id")
                ],
                # **卡片只用于"待确认"的操作**（2026-10-19 用户要求）：确认并执行完
                # 之后卡片就消失，不再把"已完成/共 N 条"这种东西留在会话里当装饰。
                # 历史不会被删：审计记录在「我的操作记录」里照旧可查。
                "operations": [
                    _operation_dict(row)
                    for row in conversation.operations.filter(
                        intent__in=[spec.intent for spec in all_specs() if spec.requires_confirmation]
                    ).exclude(status__in=OperationRecord.TERMINAL_STATUSES).order_by("-id")[:50]
                ],
            }
        )


@immutable_endpoint
class OperationProposeView(APIView):
    """手动提案（页面上的"点选操作"也走这里，与 AI 提案同一套流程）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        intent = str(request.data.get("intent", "")).strip()
        from .operations import RETIRED_INTENTS
        if intent in RETIRED_INTENTS:
            return Response({"detail": "归档功能已移除"}, status=status.HTTP_410_GONE)
        params = request.data.get("params") or {}
        conversation_id = request.data.get("conversation_id")
        conversation = None
        if conversation_id:
            conversation = Conversation.objects.filter(
                pk=conversation_id, user=request.user
            ).first()
        try:
            payload = services.propose_operation(
                request.user, intent=intent, params=params, conversation=conversation
            )
        except services.PlannerError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(payload, status=status.HTTP_201_CREATED)


@immutable_endpoint
class DeletionJobView(APIView):
    """清理任务的**受权限保护的查询/重试入口**（不能只存在未调用的函数）。

    - 查询：只看自己发起的操作对应的清理任务；系统管理员可看全部；
    - 重试：只补做失败/未完成的步骤（向量清理），不重复删数据库行；
    - 返回每步结果，未全部完成时**如实**是 ``partial``，不谎报完成。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int | None = None):
        from apps.knowledge.permissions import is_system_admin

        queryset = DeletionJob.objects.all()
        if not is_system_admin(request.user):
            queryset = queryset.filter(operation__user=request.user)
        if pk is not None:
            job = queryset.filter(pk=pk).first()
            if job is None:
                return Response(
                    {"detail": "清理任务不存在或无权查看"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(_deletion_job_dict(job))
        return Response({"results": [_deletion_job_dict(row) for row in queryset[:100]]})

    def post(self, request, pk: int):
        """重试一次未完成的清理。"""
        from apps.knowledge.permissions import is_system_admin

        queryset = DeletionJob.objects.all()
        if not is_system_admin(request.user):
            queryset = queryset.filter(operation__user=request.user)
        job = queryset.filter(pk=pk).first()
        if job is None:
            return Response(
                {"detail": "清理任务不存在或无权操作"},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            payload = services.retry_deletion_job(request.user, job)
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(payload)


def _deletion_job_dict(job: DeletionJob) -> dict:
    return {
        "id": job.pk,
        "operation_id": job.operation_id,
        "intent": job.operation.intent if job.operation_id else "",
        "target_kind": job.target_kind,
        "target_ref": job.target_ref,
        "status": job.status,
        "complete": job.status == DeletionJob.Status.COMPLETED,
        "attempts": job.attempts,
        "steps": job.steps,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@immutable_endpoint
class OperationConfirmView(APIView):
    """确认一次。破坏性操作第三次必须带 ``typed_name``（服务端比对）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        from .operations import RETIRED_INTENTS
        if operation.intent in RETIRED_INTENTS:
            return Response({"detail": "归档功能已移除"}, status=status.HTTP_410_GONE)
        try:
            payload = services.confirm_operation(
                request.user,
                operation,
                token=str(request.data.get("token", "")),
                typed_name=str(request.data.get("typed_name", "")),
            )
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class OperationAbandonView(APIView):
    """**放弃**一个待办操作（工具栏"待办事项"的删除按钮走这里）。

    只接受 POST：放弃是一次**状态推进**，不是对操作记录的 REST 删除
    （记录要留在审计里）。因此没有 DELETE/PUT/PATCH 实现，DRF 对它们返回 405。
    只允许操作人自己放弃（查询条件就带 ``user=request.user``）。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        try:
            payload = services.abandon_operation(request.user, operation)
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(payload)


@immutable_endpoint
class OperationExecuteView(APIView):
    """执行**已完成确认**的操作。未确认的一律拒绝（AI 不能自己执行）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        from .operations import RETIRED_INTENTS
        if operation.intent in RETIRED_INTENTS:
            return Response({"detail": "归档功能已移除"}, status=status.HTTP_410_GONE)
        try:
            payload = services.execute_operation(request.user, operation)
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ServiceUnavailable as exc:
            return _llm_unavailable_response(exc)
        return Response(payload)


class ConversationListView(APIView):
    """**会话列表**（原型左侧栏需要真实数据，不能只在首页有个本地数组）。

    只有 GET：返回当前用户自己的会话 + 消息条数 + 最近时间，按最近活跃排序。
    其它页面（文件管理/搜索/…）也靠它渲染会话列表，所以必须是服务端数据源。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = (
            Conversation.objects.filter(user=request.user)
            .annotate(message_count=Count("messages"))
            .order_by("-updated_at")[:50]
        )
        return Response(
            {
                "results": [
                    {
                        "id": row.pk,
                        "title": row.title,
                        "count": row.message_count,
                        "created_at": row.created_at.isoformat(),
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in rows
                ]
            }
        )


@immutable_endpoint
class OperationProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.knowledge.progress import operation_snapshot, read_many, request_ids
        ids = request_ids(request.query_params.get('ids'))
        rows = list(OperationRecord.objects.filter(pk__in=ids, user=request.user))
        snapshots = read_many('operation', [row.pk for row in rows if row.status in {'executing', 'failed'}])
        return Response({'results': [{
            'id': row.pk, 'status': row.status, 'progress': operation_snapshot(row, snapshots.get(row.pk, {})),
            'result': row.result if row.status == OperationRecord.Status.EXECUTED else {},
            'error': row.error if row.status == OperationRecord.Status.FAILED else '',
        } for row in rows]}, headers={'Cache-Control': 'private, no-store'})


@immutable_endpoint
class OperationListView(APIView):
    """我的操作记录（只读，用于界面显示待确认卡片）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = OperationRecord.objects.filter(user=request.user)
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        else:
            # 默认返回"还需要用户动手"的 + 最近的历史，界面才能恢复未完成流程
            queryset = queryset.exclude(status=OperationRecord.Status.REJECTED)
        # 「重启次数」指标的真实来源：**已执行**的"重启停滞任务"操作条数，
        # 直接在审计记录里数出来（不是进程内计数器，重启服务也不会归零）。
        restarts = OperationRecord.objects.filter(
            user=request.user,
            intent="restart_stalled_task",
            status=OperationRecord.Status.EXECUTED,
        ).count()
        totals = OperationRecord.objects.filter(user=request.user).aggregate(
            total=Count("pk"),
            executed=Count("pk", filter=Q(status=OperationRecord.Status.EXECUTED)),
            settled=Count("pk", filter=Q(status__in=OperationRecord.TERMINAL_STATUSES)),
        )
        return Response(
            {
                "results": [_operation_dict(row) for row in queryset[:100]],
                "summary": {"restart_stalled_executed": restarts, **totals},
            }
        )


@immutable_endpoint
class OperationResumeView(APIView):
    """恢复"未完成的确认流程"：**重新签发当前步骤的凭证**，旧凭证作废。

    为什么需要它（审查指出的问题）：确认凭证是**一次性**的，且只在提案/上一步
    的响应里出现过一次。用户刷新页面后手里就没有可用凭证了，界面上那个"继续确认"
    按钮点了必然失败——等于"刷新即丢流程"。

    安全口径（不做无凭证放行）：
    - 只对**当前用户自己的、未结束的**操作生效；
    - 重新核验权限（可能已被收回）；
    - 把该操作下**所有未核销的旧凭证标记作废**，再签发当前步骤的新凭证——
      这样"旧凭证作废"是显式的，不会被别人捡去用；
    - 状态与确认次数严格对应（CAS），不对应就拒绝。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        from .operations import RETIRED_INTENTS
        if operation.intent in RETIRED_INTENTS:
            return Response({"detail": "归档功能已移除"}, status=status.HTTP_410_GONE)
        if operation.status in OperationRecord.TERMINAL_STATUSES:
            return Response(
                {"detail": f"操作已结束（{operation.status}），无法恢复"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # **ARMED 已过期**：不接受"续期"，要求重新提案。
        # 续期等于把旧确认当新确认用，那三次确认就失去意义了。
        if (
            operation.status == OperationRecord.Status.ARMED
            and operation.armed_is_expired()
        ):
            OperationRecord.objects.filter(
                pk=operation.pk, status=OperationRecord.Status.ARMED
            ).update(
                status=OperationRecord.Status.EXPIRED, updated_at=timezone.now()
            )
            return Response(
                {
                    "detail": (
                        "该操作的执行窗口已过期（确认不再有效）。"
                        "为避免用过期的确认执行已变化的操作，请重新发起提案并重新确认。"
                    ),
                    "code": "armed_expired",
                },
                status=status.HTTP_409_CONFLICT,
            )
        spec = get_spec(operation.intent)
        if spec is None:
            return Response(
                {"detail": f"未登记的操作：{operation.intent}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            services.check_permission(request.user, spec, operation.params)
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        # 当前步骤 → 需要的凭证用途
        if spec.requires_destructive_flow:
            scope_for_step = {
                0: ActionTicket.Scope.CONFIRM_STEP_1,
                1: ActionTicket.Scope.CONFIRM_STEP_2,
                2: ActionTicket.Scope.CONFIRM_FINAL,
            }.get(operation.confirmations)
        else:
            scope_for_step = (
                ActionTicket.Scope.CONFIRM_STEP_1 if operation.confirmations == 0 else None
            )
        if scope_for_step is None:
            return Response(
                {"detail": "该操作当前不需要确认（可能已可执行）"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            locked = (
                OperationRecord.objects.select_for_update().filter(pk=operation.pk).first()
            )
            # 旧凭证一律作废（显式，避免"到处是有效凭证"）
            ActionTicket.objects.filter(
                operation=locked, consumed_at__isnull=True
            ).update(consumed_at=timezone.now())
            token = services.issue_ticket(
                request.user,
                operation=locked,
                conversation=locked.conversation,
                scope=scope_for_step,
                object_ref=locked.target_ref,
            )
        payload = _operation_dict(locked)
        payload["confirm_token"] = token
        payload["next"] = (
            "confirm_final"
            if scope_for_step == ActionTicket.Scope.CONFIRM_FINAL
            else ("execute" if scope_for_step is None else "confirm")
        )
        payload["typed_name_required"] = (
            locked.target_label
            if scope_for_step == ActionTicket.Scope.CONFIRM_FINAL
            else ""
        )
        payload["note"] = "已重新签发当前步骤的凭证；此前未使用的凭证已作废。"
        return Response(payload)


def _operation_dict(row: OperationRecord) -> dict:
    from apps.knowledge.progress import operation_snapshot
    spec = get_spec(row.intent)
    # 「本次检索范围」在卡片上要显示**库名**（只给 code 时对不上界面上的库名）。
    # 库可能已被删除/停用：找不到就如实回落成 code，不编造名字。
    scope_codes = [str(code) for code in (row.library_scope or [])]
    scope_labels: list[str] = []
    if scope_codes:
        from apps.knowledge.models import KnowledgeLibrary

        names = {
            item.code: item.name
            for item in KnowledgeLibrary.objects.filter(code__in=scope_codes)
        }
        scope_labels = [names.get(code) or code for code in scope_codes]
    # 「编写新文档」待确认卡上的**参考文件**选择器（用户要求：选完模板还要能在
    # 当前所在库里至少选一份参考文件）。
    # 候选只在该操作仍可改参数时下发 —— 已确认/已执行再列候选，会让人以为还能改。
    source_file_ids: list[int] = []
    source_candidates: list[dict] = []
    if spec is not None and row.intent == "compose_document":
        for ref in (row.params or {}).get("sources") or []:
            try:
                pk = int(ref)
            except (TypeError, ValueError):
                continue
            if pk > 0:
                source_file_ids.append(pk)
        if row.status in services.PENDING_EDITABLE_STATUSES:
            try:
                source_candidates = services.compose_source_candidates(row.user, row)
            except services.PlannerError:
                # 范围里的库已被删除/停用：卡片上不给候选，确认时执行层会如实报错。
                source_candidates = []
    return {
        "id": row.pk,
        "conversation_id": row.conversation_id,
        "intent": row.intent,
        "label": spec.label if spec else row.intent,
        "risk": row.risk,
        "status": row.status,
        "params": row.params,
        # 这一问用户勾选的检索范围（空 = 不限）。执行层按它收敛检索集合，
        # 所以卡片上必须能看见 —— 否则"为什么只搜出这几个库"无从解释。
        "library_scope": scope_codes,
        "library_scope_labels": scope_labels,
        # 「编写新文档」：已勾选的参考文件 + 当前库里可选的文件（至少一份）。
        "source_file_ids": source_file_ids,
        "source_candidates": source_candidates,
        "target_ref": row.target_ref,
        "target_label": row.target_label,
        "confirmations": row.confirmations,
        "required_confirmations": row.required_confirmations,
        "requires_confirmation": bool(spec and spec.requires_confirmation),
        "requires_destructive_flow": bool(
            spec.requires_destructive_flow if spec else False
        ),
        "result": row.result,
        "progress": operation_snapshot(row),
        "error": row.error,
        # ARMED 的执行窗口：界面据此显示"可执行 / 已过期"，
        # 并让"刷新后仍能继续"的执行入口受 TTL 约束（不提供无限期入口）。
        "armed_at": row.armed_at.isoformat() if row.armed_at else "",
        "armed_expires_at": (
            row.armed_expires_at.isoformat() if row.armed_expires_at else ""
        ),
        "armed_expired": (
            row.status == OperationRecord.Status.ARMED and row.armed_is_expired()
        ),
        "created_at": row.created_at.isoformat(),
    }


# ------------------------------------------------------------ 派生文档 ----


# 注意：这里**不能**加 ``@immutable_endpoint``。
#
# 不可变层保护的是"已入库的源资料"；派生文档是 AI 写出来的**草稿**，
# 本来就该被作者编辑（P5 要求右侧可编辑 + 预览 + 下载）。
# 早期给它加了装饰器，导致 PUT 编辑被治理层直接拒绝——
# 那份草稿永远只能看不能改，与需求相反。
#
# 派生文档自身的写入约束由权限（只有 created_by 能改）与模型层保证，
# 并且它**永远不会回写源资料**（services.update_derived_document 只改
# DerivedDocument 自己的字段）。
class DerivedDocumentView(APIView):
    """派生文档：新建 / 列表 / 读取 / 编辑（**绝不动来源资料**）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int | None = None):
        if pk:
            document = get_object_or_404(
                DerivedDocument, pk=pk, created_by=request.user
            )
            return Response(_document_dict(document))
        return Response(
            {
                "results": [
                    _document_dict(row)
                    for row in DerivedDocument.objects.filter(
                        created_by=request.user
                    )[:100]
                ]
            }
        )

    def post(self, request):
        from .document_formatting import validate_format_options
        try:
            options = validate_format_options(request.data.get("format_options", {}))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        # **用户手写的草稿**：给了非空 `content_markdown` 就直接落一份派生文档，
        # **不调用模型**（返回体里如实标注"非 AI 生成"）。
        # 为什么需要它：文档面板原来只有"生成草稿"一条路 —— 面板提示写着可以手写，
        # 但空白态下「保存」永远禁用、只能让 AI 覆盖用户写的正文（用户：正文都写好了，
        # "生成草稿"没有合理语义）。现在手写内容有自己的、诚实的保存路径。
        handwritten = str(request.data.get("content_markdown", "") or "")
        if handwritten.strip():
            title = str(request.data.get("title", "") or "").strip()
            topic = str(request.data.get("topic", "") or "").strip()
            try:
                payload = services.create_authored_document(
                    request.user,
                    title=title,
                    topic=topic,
                    content_markdown=handwritten,
                )
            except services.PlannerError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            if "format_options" in request.data:
                DerivedDocument.objects.filter(pk=payload["document_id"]).update(format_options=options)
                payload["format_options"] = options
            return Response(payload, status=status.HTTP_201_CREATED)

        # **严格来源校验**（审查第 1 条）：显式勾选的来源**绝不悄悄扩大或缩小范围**。
        # 旧实现用 `if str(value).isdigit()` 逐项丢弃非法值 → `["bad"]` 会变成空列表，
        # 于是"指定来源"静默退化成"全库检索"（范围被扩大），且负数/标量/空值都被吞掉。
        # 现在的口径：只要显式给了 `source_file_ids`，就逐项校验，任何一项不合法即 400，
        # 且在**调用模型之前**就拒绝。
        # **必须区分"键不存在"与"显式 null"**：`request.data.get(..., None)` 会把两者
        # 混为一谈，于是 `{"source_file_ids": null}` 被当成"没传"而静默退回全库。
        has_sources_key = "source_file_ids" in request.data
        raw_sources = request.data.get("source_file_ids", None)
        source_file_ids: list[int] = []
        if has_sources_key:
            if raw_sources is None:
                return Response(
                    {"detail": "source_file_ids 不能为 null；不指定来源请**省略该字段**（按主题全库检索）"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if isinstance(raw_sources, (str, bytes)) or not isinstance(raw_sources, (list, tuple)):
                return Response(
                    {"detail": "source_file_ids 必须是数组"}, status=status.HTTP_400_BAD_REQUEST
                )
            if not raw_sources:
                return Response(
                    {"detail": "source_file_ids 不能为空数组；要么不传（全库），要么给具体资料"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for value in raw_sources:
                # 只接受真正的整数（bool 是 int 的子类，必须排除）
                if isinstance(value, bool) or not isinstance(value, int):
                    if isinstance(value, str) and value.strip().isdigit():
                        value = int(value.strip())
                    else:
                        return Response(
                            {"detail": f"source_file_ids 含非法取值：{value!r}"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                if value <= 0:
                    return Response(
                        {"detail": f"source_file_ids 必须是正整数：{value!r}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                source_file_ids.append(value)

        # 公文模板（**可选**）：不传 = 不用模板（用户要求："未选择就不用模板"）。
        # 只校验"格式/存在性"这一层，真正的存在性检查仍在 `compose_document` 里做
        # （那里是模板解析与提示词注入的唯一入口）。
        raw_template = request.data.get("template_id", None)
        template_id: int | None = None
        if raw_template not in (None, ""):
            if isinstance(raw_template, bool):
                return Response(
                    {"detail": f"template_id 必须是正整数：{raw_template!r}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                template_id = int(str(raw_template).strip())
            except (TypeError, ValueError):
                return Response(
                    {"detail": f"template_id 必须是正整数：{raw_template!r}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if template_id <= 0:
                return Response(
                    {"detail": f"template_id 必须是正整数：{raw_template!r}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 显式来源的**存在性/权限/可用性**校验放在 `compose_document` 内做：
        # 那里有唯一的权限助手（`_permitted_files`），避免在这里复制一套权限口径。
        # 本视图只负责"格式必须合法"，且**在调用模型之前**就拒绝。
        try:
            payload = services.compose_document(
                request.user,
                title=str(request.data.get("title", "")).strip() or "未命名文档",
                topic=str(request.data.get("topic", "")).strip(),
                library_codes=list(request.data.get("libraries", []) or []),
                # P5 来源选择器：用户勾选的具体资料（优先于按主题全库检索）
                source_file_ids=source_file_ids or None,
                # 公文模板：可选（None = 不用模板）
                template_id=template_id,
            )
            if "format_options" in request.data:
                DerivedDocument.objects.filter(pk=payload["document_id"]).update(format_options=options)
                payload["format_options"] = options
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ServiceUnavailable as exc:
            return _llm_unavailable_response(exc)
        except LLMError as exc:
            # 与 ChatView 统一：模型侧失败（含空答复、被截断）一律 503，
            # 并且**不会**落下一份空/截断的文档。
            return _llm_unavailable_response(exc)
        return Response(payload, status=status.HTTP_201_CREATED)

    def put(self, request, pk: int):
        # **越权与不存在返回同一个 404**：不能泄露"这个 id 存在但不属于你"
        # （审查指出：早期返回 400，与 GET 的 404 不一致，等于确认了存在性）。
        document = DerivedDocument.objects.filter(
            pk=pk, created_by=request.user
        ).first()
        if document is None:
            return Response(
                {"detail": "派生文档不存在"}, status=status.HTTP_404_NOT_FOUND
            )
        try:
            payload = services.update_derived_document(
                request.user,
                pk,
                content_markdown=str(request.data.get("content_markdown", "")),
                # 标题/主题是**可编辑输入框**，就必须真的能存下来（否则是假保存）。
                # 只用 `in` 判断"这次请求有没有带这个字段"，没带就不动它 ——
                # 这样旧调用方（只 PUT content_markdown）行为不变。
                title=request.data.get("title") if "title" in request.data else None,
                topic=request.data.get("topic") if "topic" in request.data else None,
                format_options=request.data.get("format_options") if "format_options" in request.data else None,
            )
        except services.PlannerError as exc:
            detail = str(exc)
            # 不存在/越权 → 404（不泄露存在性）；字段不合法 → 400（用户要能改回来）
            code = (
                status.HTTP_404_NOT_FOUND
                if detail == "派生文档不存在"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": detail}, status=code)
        return Response(payload)


    def delete(self, request, pk: int):
        """删除一份**自己的**派生草稿（不是入库资料、不走三次确认）。

        口径与 ``put`` 一致：越权与不存在都返回同一个 404，不确认存在性。
        删掉草稿不会影响任何来源资料；审计事件照留（见
        ``services.delete_derived_document``）。
        """
        try:
            payload = services.delete_derived_document(request.user, pk)
        except services.PlannerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(payload)


class DerivedDocumentDownloadView(APIView):
    """下载派生文档：**真实导出** Markdown / HTML / DOCX / PDF。

    为什么改成走 `document_export`：旧实现手写 md/html 两个分支，DOCX/PDF 完全
    没有；现在四种格式共用一套实现（`build_export`），由它统一负责中文编码、
    MIME、以及**对中文友好的文件名**（RFC 5987 `filename*` + ASCII 兜底）。
    响应头 `X-Document-Format` 写出**实际**格式，便于核对"说下载什么、实际是什么"。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        from urllib.parse import quote

        from django.http import HttpResponse

        from .document_export import (
            SUPPORTED_FORMATS,
            ExportError,
            ExportUnavailable,
            UnsupportedExportFormat,
            build_export,
            content_disposition,
        )

        document = get_object_or_404(DerivedDocument, pk=pk, created_by=request.user)
        # **不要用 `format`**：那是 DRF 的 renderer 格式参数，会被
        # content negotiation 在进入视图之前拦掉（实测返回 404）。
        # 因此改用 `export_format`，并兼容旧的 `fmt`/`format` 只在**已进入视图**后读取。
        fmt = str(
            request.query_params.get("export_format")
            or request.query_params.get("fmt")
            or "md"
        ).lower()

        try:
            payload = build_export(document, fmt)
        except UnsupportedExportFormat as exc:
            return Response(
                {"detail": str(exc), "supported": list(SUPPORTED_FORMATS)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ExportUnavailable as exc:
            # 例如镜像里没有 LibreOffice：**如实** 503，不返回假的 PDF
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ExportError as exc:  # pragma: no cover - 兜底：导出失败不冒充成功
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = HttpResponse(payload.content, content_type=payload.content_type)
        response["Content-Disposition"] = content_disposition(payload.filename)
        response["X-Document-Format"] = payload.format or fmt
        if payload.note:
            # 头只能是 latin-1：说明文字用百分号编码带出，绝不把中文塞进响应头
            response["X-Document-Note"] = quote(payload.note, safe="")
        return response


def _escape(text: str) -> str:
    from django.utils.html import escape

    return escape(text or "")


def _document_dict(document: DerivedDocument) -> dict:
    return {
        "id": document.pk,
        "title": document.title,
        "topic": document.topic,
        "content_markdown": document.content_markdown,
        "format_options": document.format_options,
        "status": document.status,
        "sources": [
            {"id": row.pk, "name": row.original_name}
            for row in document.sources.all()
        ],
        "libraries": [row.code for row in document.libraries.all()],
        # 这份文档撰写时套的是哪份模板。模板被删后外键会置空，但 ``template_name``
        # 是当时的**快照**，所以这里仍然能如实回答"当时套的是哪一份"。
        "template_id": document.template_id,
        "template_name": document.template_name,
        # 预览要按模板口径排版（公文：仿宋三号、首行缩进两字符、28 磅行距）：
        # 只给名字不够，界面得知道它是"公文模板"还是"PPT/其他模板"。
        "template_category": (
            document.template.category if document.template_id and document.template else ""
        ),
        "updated_at": document.updated_at.isoformat(),
        "download_url": f"/api/v1/planner/documents/{document.pk}/download/",
    }


# ---------------------------------------------------------------- 公文模板 ----


class DocumentTemplateView(APIView):
    """公文模板：列表 / 上传 / 详情 / 删除。

    可见范围：**所有登录用户都能看到全部模板**。理由：模板是单位的格式规范，
    同事上传的正是别人该复用的东西；只让上传者看见，只会让人重复上传同一份范本。
    删除权限单独收敛：上传者本人或系统管理员（传错的模板不该成为永久垃圾）。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int | None = None):
        if pk:
            template = get_object_or_404(DocumentTemplate, pk=pk)
            # 详情带上正文：界面要能让人**看到**这份模板长什么样，
            # 而不是只凭一个文件名去选。
            return Response(template_dict(template, include_text=True, user=request.user))
        return Response(
            {
                "results": [
                    template_dict(row, user=request.user)
                    for row in self.template_queryset(request)
                ]
            }
        )

    def template_queryset(self, request):
        rows = DocumentTemplate.objects.all()
        category = request.query_params.get("category")
        keyword = request.query_params.get("q", "").strip()
        if category:
            rows = rows.filter(category=category)
        if keyword:
            from django.db.models import Q
            rows = rows.filter(Q(name__icontains=keyword) | Q(description__icontains=keyword)
                               | Q(original_name__icontains=keyword))
        return rows

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {
                    "detail": "缺少 file 字段（用 multipart/form-data 上传 "
                    ".docx / .doc / .pdf / .txt / .md）"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        # 格式与大小先用**可信元数据**卡住，再读内容：超大文件不该先被读进内存才被拒。
        try:
            size = int(getattr(upload, "size", 0) or 0)
            from .document_templates import require_valid_upload

            require_valid_upload(upload.name or "", size)
        except TemplateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        raw = upload.read()
        try:
            template = create_template(
                raw=raw,
                original_name=upload.name or "template",
                user=request.user,
                name=str(request.data.get("name", "") or ""),
                category=str(request.data.get("category", "") or ""),
                description=str(request.data.get("description", "") or ""),
            )
        except TemplateStorageError as exc:
            return Response(
                {"code": "template_storage_unavailable", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except TemplateError as exc:
            # 提取不出文字 / 解析失败：**不落任何记录、不留孤儿文件**，如实回报原因。
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(template_dict(template, user=request.user), status=status.HTTP_201_CREATED)

    def delete(self, request, pk: int):
        template = get_object_or_404(DocumentTemplate, pk=pk)
        if template.uploaded_by_id != request.user.pk and not request.user.is_staff:
            return Response(
                {"detail": "只能删除自己上传的模板（系统管理员可以删除任意模板）"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(delete_template(template))


class DocumentTemplateDownloadView(APIView):
    """下载模板**原件**（用于核对"这次套的到底是哪一版范本"）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        template = get_object_or_404(DocumentTemplate, pk=pk)
        try:
            path = template_path(template)
        except Exception as exc:  # noqa: BLE001 - 路径/项目根校验失败要如实说，不能 500
            return Response(
                {"detail": f"模板原件不可用：{exc}"}, status=status.HTTP_409_CONFLICT
            )
        if not path.is_file():
            return Response(
                {
                    "detail": "模板原件不在磁盘上（可能已被清理）；"
                    "模板的文字内容仍在库里，可以据此重新上传一份。"
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        response = FileResponse(
            open(path, "rb"), as_attachment=True, filename=template.original_name
        )
        # 如实回传标识与指纹，便于事后核对"套用的是不是同一份原件"
        response["X-Template-Id"] = str(template.pk)
        response["X-Template-Sha256"] = template.content_sha256 or ""
        return response


class OperationTemplateView(APIView):
    """给「编写新文档」的**待确认卡**选择公文模板（也可以改成"不用模板"）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        try:
            payload = services.set_operation_template(
                request.user, operation, request.data.get("template_id", None)
            )
        except services.PlannerError as exc:
            detail = str(exc)
            # 与 DerivedDocumentView 同一口径：不存在 → 404，其余（状态/类型）→ 400，
            # 状态冲突（已经执行中/已确认完）→ 409，让调用方能区分"改不了"的原因。
            if detail == "模板不存在或已被删除":
                return Response({"detail": detail}, status=status.HTTP_404_NOT_FOUND)
            if detail.startswith("该待办已处于"):
                return Response({"detail": detail}, status=status.HTTP_409_CONFLICT)
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class OperationSourcesView(APIView):
    """给「编写新文档」的待确认卡选择至少一份当前库中的参考文件。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        operation = get_object_or_404(OperationRecord, pk=pk, user=request.user)
        try:
            payload = services.set_operation_sources(
                request.user, operation, request.data.get("file_ids", None)
            )
        except services.PlannerError as exc:
            detail = str(exc)
            # 与模板接口同一口径：不存在 → 404，状态冲突 → 409，其余 → 400。
            if detail.startswith("参考文件不存在"):
                return Response({"detail": detail}, status=status.HTTP_404_NOT_FOUND)
            if detail.startswith("该待办已处于"):
                return Response({"detail": detail}, status=status.HTTP_409_CONFLICT)
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


# ------------------------------------------------------------ 使用说明 ----


class GuideView(APIView):
    """首页使用说明（P6）：由服务端提供，保证"说明"与"能力"同源。

    **可见范围必须与声明一致**（Codex 审计 OP-12/OP-20）：系统配置页声明"远程模型
    服务/模型与基础设施配置只有系统管理员能看到"，但这里原先对**所有登录用户**都
    如实回传了真实 LLM ``base_url``（内网端点）与模型名 —— 于是"声明"和"实际可见"
    自相矛盾。修法：端点与模型名**只给系统管理员**；普通用户拿到的是
    ``configured`` 布尔与一句说明，用于判断"模型是否已配置"，不泄露内网地址。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "title": "知识库使用说明",
                # ---- 环境自述（供压测脚本 fail-closed 判定；都是**服务端事实**）----
                "environment": {
                    # 模型外呼是否被禁用。压测必须为 True（否则会打到 AS711）。
                    "model_calls_disabled": bool(
                        getattr(settings, "KB_MODEL_CALLS_DISABLED", False)
                    ),
                    # 是否是**显式声明**的负载测试实例。
                    # 默认 False → 压测脚本会拒绝在正式服务上发压。
                    "load_test_mode": bool(
                        getattr(settings, "KB_LOAD_TEST_MODE", False)
                    ),
                    "load_test_label": getattr(settings, "KB_LOAD_TEST_LABEL", ""),
                    # 处理任务是否禁止派发（压测实例默认禁止，防止任务流入业务队列）
                    "dispatch_disabled": bool(
                        getattr(settings, "KB_DISPATCH_DISABLED", False)
                    ),
                    # 隔离证据：向量命名空间前缀与是否用了测试适配器
                    "qdrant_collection_prefix": getattr(
                        settings, "KB_QDRANT_COLLECTION_PREFIX", ""
                    ),
                    "upload_process_mode": getattr(
                        settings, "KB_UPLOAD_PROCESS_MODE", ""
                    ),
                    "test_adapter": getattr(settings, "KB_TEST_ADAPTER", ""),
                    # 大模型服务的**只读配置**（不探测）：系统配置页要展示真实模型与端点。
                    # **只有系统管理员**能拿到端点与模型名；普通账号只拿到"是否已配置"。
                    "llm": self._llm_view(request),
                    # 真实数据库引擎（展示用；不含口令）
                    "database_engine": settings.DATABASES["default"]["ENGINE"].rsplit(
                        ".", 1
                    )[-1],
                    # 不要暴露数据库口令等敏感信息；只给一个"是不是同一个库"的指纹
                    "database_name": (
                        settings.DATABASES["default"]["NAME"]
                        if getattr(settings, "KB_LOAD_TEST_MODE", False)
                        else ""
                    ),
                },
                "steps": [
                    {
                        "title": "1. 上传资料",
                        "body": "在“文件管理”里选择信息库，上传单个文件或整个文件夹。"
                        "原始内容按内容寻址保存且只读，重复上传会自动复用。",
                    },
                    {
                        "title": "2. 等待处理",
                        "body": "资料会依次解析、分段、建立向量索引。状态列会显示"
                        "待处理/处理中/已完成/失败；失败的可以点“重新解析”。",
                    },
                    {
                        "title": "3. 检索资料",
                        # 搜索页已按原型收敛：没有信息库下拉，只有模式 + 结构化条件。
                        # 说明必须写**实际**用法，不能指引一个不存在的控件。
                        "body": "在“搜索”里选模式（文件名 + 内容 / 只搜文件名 / 只搜正文），"
                        "输入关键词；还可以叠加归属部门、资料性质、金额下限一起筛。"
                        "智能检索不可用时会自动改用关键字匹配，并如实标注“本次为降级结果”。",
                    },
                    {
                        "title": "4. 用 AI 助手（首页）",
                        "body": "可以直接提问，也可以让它提出操作提案。"
                        "仅删除操作需要确认；其它操作通过权限校验且参数完整后直接执行。",
                    },
                    {
                        "title": "5. 编写新文档",
                        "body": "助手可以基于你明确指定的参考文件"
                        "写一份**新的**派生文档，在右侧预览、编辑并下载。"
                        "参考文件支持全选，至少指定一份后直接生成；已入库的源资料不会被修改。",
                    },
                    {
                        "title": "6. 删除需要三次确认",
                        "body": "删除资料/信息库要连续三次确认，最后一次必须准确输入"
                        "目标名称，由服务端比对。确认凭证一次性、会过期、不可重放。",
                    },
                ],
                "catalog": [
                    {**spec.to_dict(), "is_mutation": is_mutation(spec.intent)}
                    for spec in all_specs()
                ],
                "assistant": {
                    "configured": LlmClient().configured,
                    "available": LlmClient().available,
                    "last_error": LlmClient().last_error,
                },
            }
        )

    @staticmethod
    def _llm_view(request) -> dict:
        """LLM 只读配置的**按权限呈现**（见类文档：声明与可见范围必须一致）。"""
        from apps.knowledge.permissions import is_system_admin

        services_config = getattr(settings, "MODEL_SERVICES", {}) or {}
        llm = services_config.get("llm", {}) or {}
        base_url = str(llm.get("base_url", "") or "")
        model = str(llm.get("model", "") or "")
        payload = {"configured": bool(base_url)}
        if is_system_admin(request.user):
            payload["model"] = model
            payload["base_url"] = base_url
            payload["restricted"] = False
        else:
            # 普通账号：不返回内网端点与模型名（那属于系统配置的可见范围），
            # 只保留"是否已配置"这个对使用说明书有意义的事实。
            payload["model"] = ""
            payload["base_url"] = ""
            payload["restricted"] = True
            payload["note"] = "端点与模型名只有系统管理员可见（系统配置页）。"
        return payload
