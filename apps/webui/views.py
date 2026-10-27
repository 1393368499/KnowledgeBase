"""浏览器界面视图：登录、文件管理、搜索。

这里**不做业务判断**，只渲染模板并把数据交给前端 JS 去调 REST API。
唯一例外是登录（Django 自带 auth），因为"浏览器不保存 Basic 口令"的要求
只能靠 session 登录满足。
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import render
from django.urls import reverse_lazy


class KbLoginView(LoginView):
    """session 登录页（不使用 HTTP Basic，浏览器里不缓存口令）。"""

    template_name = "webui/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        # 默认落到「首页」（AI 会话页 /app/）；只有带 ?next= 时才回到用户原本要去的页面。
        return self.get_redirect_url() or str(reverse_lazy("webui:home"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["next"] = self.request.GET.get("next", "")
        return context


class KbLogoutView(LogoutView):
    next_page = reverse_lazy("webui:login")


@login_required
def files_page(request):
    """文件管理：上传（文件/整目录）、状态、重试、来源只读。"""
    return render(request, "webui/files.html", {"page": "files"})


@login_required
def assistant_page(request):
    """首页：AI 会话 + 操作提案确认 + 右侧派生文档预览/编辑/下载。"""
    return render(request, "webui/assistant.html", {"page": "assistant"})


@login_required
def search_page(request):
    """检索：查询 + **信息库下拉**（只列当前用户可读的库）。"""
    return render(request, "webui/search.html", {"page": "search"})


# ---- 以下四页按**原型的导航结构**补齐，数据一律来自真实 API，不留演示数据 ----


@login_required
def agent_page(request):
    """Agent 控制台：真实健康检查、熔断快照、审计事件、清理任务。"""
    return render(request, "webui/agent.html", {"page": "agent"})


@login_required
def settings_page(request):
    """系统配置：模型服务配置状态（来自 /guide/）、索引与基础设施只读信息。"""
    return render(request, "webui/settings.html", {"page": "settings"})


@login_required
def knowledge_page(request):
    """信息库：真实信息库清单 + 各库索引代次状态。"""
    return render(request, "webui/knowledge.html", {"page": "knowledge"})


@login_required
def templates_page(request):
    return render(request, "webui/templates.html", {"page": "templates"})


@login_required
def tasks_page(request):
    """添加记录：真实的操作记录（OperationRecord），不是演示数据。"""
    return render(request, "webui/tasks.html", {"page": "tasks"})


@login_required
def docs_page(request):
    """使用说明：**独立整页**（原型里也是单独一页，首页不塞说明卡）。"""
    return render(request, "webui/docs.html", {"page": "docs"})


@login_required
def devices_page(request):
    return render(request, "webui/devices.html", {"page": "devices"})
