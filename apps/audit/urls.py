from __future__ import annotations

from django.urls import path

from .views import AuditChainVerifyView, AuditEventListView

app_name = "audit"

urlpatterns = [
    path("audit/events/", AuditEventListView.as_view(), name="audit-event-list"),
    path("audit/verify/", AuditChainVerifyView.as_view(), name="audit-verify"),
]
