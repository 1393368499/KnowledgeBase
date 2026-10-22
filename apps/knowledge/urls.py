from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    FileProgressView,
    FileOriginalDownloadView,
    FilePreviewMetaView,
    FilePreviewPageView,
    IngestedFileViewSet,
    KnowledgeGenerationView,
    KnowledgeIndexStatsView,
    KnowledgeModelServiceView,
    KnowledgeRebuildView,
    KnowledgeStatsView,
    LibraryViewSet,
    KnowledgeRuntimeMetricsView,
    SearchFeedbackHandleView,
    SearchFeedbackView,
    SearchView,
    VectorCollectionViewSet,
)

app_name = "knowledge"

router = DefaultRouter()
router.register("knowledge/libraries", LibraryViewSet, basename="library")
router.register("knowledge/files", IngestedFileViewSet, basename="file")
router.register("knowledge/collections", VectorCollectionViewSet, basename="collection")

urlpatterns = [
    path('knowledge/files/progress/', FileProgressView.as_view(), name='file-progress'),
    path("knowledge/stats/", KnowledgeStatsView.as_view(), name="knowledge-stats"),
    path("knowledge/index-stats/", KnowledgeIndexStatsView.as_view(), name="index-stats"),
    path("knowledge/models/", KnowledgeModelServiceView.as_view(), name="model-services"),
    path("knowledge/generations/", KnowledgeGenerationView.as_view(), name="generations"),
    # 索引初建/重建（可队列）：POST /api/v1/knowledge/rebuild/
    path("knowledge/rebuild/", KnowledgeRebuildView.as_view(), name="rebuild"),
    path("knowledge/search/", SearchView.as_view(), name="search"),
    # 检索反馈（准确/不准确）持久化
    path("knowledge/runtime-metrics/", KnowledgeRuntimeMetricsView.as_view(),
         name="runtime-metrics"),
    path("knowledge/search-feedback/", SearchFeedbackView.as_view(),
         name="search-feedback"),
    # 反馈分诊（原型「标记已处理」）：只留痕，不改权重口径
    path("knowledge/search-feedback/<int:pk>/handle/",
         SearchFeedbackHandleView.as_view(), name="search-feedback-handle"),
    # 原件预览（真实呈现原件内容；不暴露 MEDIA，鉴权与列表同口径）
    path("knowledge/files/<int:pk>/preview/", FilePreviewMetaView.as_view(), name="file-preview"),
    path("knowledge/files/<int:pk>/preview/pages/<int:page>/", FilePreviewPageView.as_view(),
         name="file-preview-page"),
    path("knowledge/files/<int:pk>/download/", FileOriginalDownloadView.as_view(),
         name="file-original-download"),
    *router.urls,
]
