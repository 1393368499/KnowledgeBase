from __future__ import annotations

from django.urls import path

from .views import (
    ChatView,
    ConversationListView,
    ConversationView,
    DeletionJobView,
    DerivedDocumentDownloadView,
    DerivedDocumentView,
    DocumentTemplateDownloadView,
    DocumentTemplateView,
    GuideView,
    OperationAbandonView,
    OperationCatalogView,
    OperationConfirmView,
    OperationExecuteView,
    OperationListView,
    OperationProgressView,
    OperationProposeView,
    OperationResumeView,
    OperationSourcesView,
    OperationTemplateView,
)

app_name = "planner"

urlpatterns = [
    path('planner/operations/progress/', OperationProgressView.as_view(), name='operation-progress'),
    # 能力目录（静态登记表，与"我的操作记录"是两件事，路径必须分开）
    path("planner/catalog/", OperationCatalogView.as_view(), name="operation-catalog"),
    path("planner/guide/", GuideView.as_view(), name="guide"),
    # P3 会话：AI 只提案
    path("planner/chat/", ChatView.as_view(), name="chat"),
    path("planner/conversations/", ConversationListView.as_view(), name="conversation-list"),
    path("planner/conversations/<int:pk>/", ConversationView.as_view(), name="conversation"),
    # 操作提案 → 确认 → 执行（三步分离，AI 无法跳过确认）
    path(
        "planner/operations/propose/",
        OperationProposeView.as_view(),
        name="operation-propose",
    ),
    path("planner/operations/", OperationListView.as_view(), name="operation-list"),
    # 刷新后恢复"当前未完成的确认入口"：旧凭证作废，重新签发当前步骤的凭证
    path(
        "planner/operations/<int:pk>/resume/",
        OperationResumeView.as_view(),
        name="operation-resume",
    ),
    path(
        "planner/operations/<int:pk>/confirm/",
        OperationConfirmView.as_view(),
        name="operation-confirm",
    ),
    # 放弃一条待办：状态推进到终态（rejected），**不删除**操作记录本身
    path(
        "planner/operations/<int:pk>/abandon/",
        OperationAbandonView.as_view(),
        name="operation-abandon",
    ),
    path(
        "planner/operations/<int:pk>/execute/",
        OperationExecuteView.as_view(),
        name="operation-execute",
    ),
    # P4 清理任务：受权限保护的查询 / 重试入口（页面"清理与重试"区使用）
    path(
        "planner/deletions/",
        DeletionJobView.as_view(),
        name="deletion-jobs",
    ),
    path(
        "planner/deletions/<int:pk>/",
        DeletionJobView.as_view(),
        name="deletion-job-detail",
    ),
    # P5 派生文档
    path("planner/documents/", DerivedDocumentView.as_view(), name="document-list"),
    path(
        "planner/documents/<int:pk>/",
        DerivedDocumentView.as_view(),
        name="document-detail",
    ),
    path(
        "planner/documents/<int:pk>/download/",
        DerivedDocumentDownloadView.as_view(),
        name="document-download",
    ),
    # 公文模板：用户上传的格式范本（不是入库资料，可删）
    # 列表与上传同路径（GET/POST），详情与删除同路径（GET/DELETE）。
    path("planner/templates/", DocumentTemplateView.as_view(), name="template-list"),
    path(
        "planner/templates/<int:pk>/",
        DocumentTemplateView.as_view(),
        name="template-detail",
    ),
    path(
        "planner/templates/<int:pk>/download/",
        DocumentTemplateDownloadView.as_view(),
        name="template-download",
    ),
    # 给「编写新文档」的待确认卡选/改公文模板（未选 = 不用模板）
    path(
        "planner/operations/<int:pk>/template/",
        OperationTemplateView.as_view(),
        name="operation-template",
    ),
    # 给「编写新文档」的待确认卡选参考文件（当前所在库里的资料；未选 = 按主题全库）
    path(
        "planner/operations/<int:pk>/sources/",
        OperationSourcesView.as_view(),
        name="operation-sources",
    ),
]
