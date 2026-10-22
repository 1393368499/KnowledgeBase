"""隔离测试库中的浏览器验收，不向用户资料库写入测试文件。"""
import io
from pathlib import Path


import os
import sys
import uuid
import socket
import subprocess
import time
import urllib.request
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN_ID = uuid.uuid4().hex[:12]
RUN_DIR = ROOT / "artifacts" / "test-runs" / RUN_ID
RUN_DIR.mkdir(parents=True)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
os.environ["KB_TEST_RUN_UUID"] = RUN_ID
os.environ["KB_SQLITE_TEST_PATH"] = str(RUN_DIR / "browser.sqlite3")
import django
django.setup()

from django.conf import settings
from playwright.sync_api import sync_playwright, expect
from pptx import Presentation

from apps.knowledge.models import DocumentChunk
from apps.planner.models import Conversation, Message, OperationRecord
from apps.planner import services


def run_check(live_server, client, user, library, make_file, settings):
    file = make_file(library, "浏览器验收资料.md", chunk_count=2, embedded_chunk_count=2, vector_status="indexed")
    DocumentChunk.objects.create(file=file, ordinal=0, text="第一段：项目已完成验收。", page=1)
    DocumentChunk.objects.create(file=file, ordinal=1, text="第二段：后续维护安排。", page=2)
    conversation = Conversation.objects.create(user=user, title="模板选择验收")
    Message.objects.create(conversation=conversation, role="user", content="请编写项目报告")
    proposal = services.propose_operation(user, intent="compose_document", conversation=conversation,
        params={"title": "项目报告", "topic": "验收情况"})
    client.force_login(user)
    cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "验收汇报范本"
    slide.placeholders[1].text = "项目概况\n验收结果"
    buffer = io.BytesIO()
    presentation.save(buffer)
    out = Path("artifacts/acceptance/template-library")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": cookie, "url": live_server.url}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(live_server.url + "/app/templates/", wait_until="networkidle")
        expect(page.get_by_role("heading", name="模板管理", exact=True)).to_be_visible()
        expect(page.locator("#sidebar").get_by_text("模板管理", exact=True)).to_be_visible()
        page.locator('[name="name"]').fill("验收演示模板")
        page.locator('[name="description"]').fill("项目验收汇报")
        page.locator('[name="file"]').set_input_files({"name": "验收.pptx", "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "buffer": buffer.getvalue()})
        expect(page.locator('[name="category"]')).to_have_value("presentation")
        page.get_by_role("button", name="添加模板", exact=True).click()
        expect(page.locator("#template-notice")).to_contain_text("模板已添加")
        expect(page.locator("#template-list")).to_contain_text("验收演示模板")
        page.get_by_role("button", name="查看详情", exact=True).click()
        expect(page.locator(".modal .stored-text")).to_contain_text("验收汇报范本")
        page.get_by_role("button", name="关闭", exact=True).click()
        page.screenshot(path=str(out / "templates.png"), full_page=True)
        page.goto(live_server.url + f"/app/?conversation={conversation.pk}", wait_until="networkidle")
        select = page.locator(f'[data-op-template="{proposal["operation_id"]}"]')
        expect(select).to_be_visible()
        template_id = select.locator("option").filter(has_text="验收演示模板").get_attribute("value")
        select.select_option(template_id)
        expect(select).to_have_attribute("data-selected-template", template_id)
        operation = SimpleNamespace(pk=proposal["operation_id"])
        page.reload(wait_until="networkidle")
        expect(page.locator(f'[data-op-template="{operation.pk}"]')).to_have_value(template_id)
        page.screenshot(path=str(out / "confirmation.png"), full_page=True)
        page.locator("#btn-docs").click()
        page.locator("#doc-format-block summary").click()
        page.locator("#doc-title").fill("公文字体验收")
        page.locator("#doc-body").fill("# 公文字体验收\n\n## 工作安排\n\n这是仿宋正文。")
        page.locator("#doc-format-title_font").select_option("xiaobiaosong")
        page.locator("#doc-format-body_font").select_option("fangsong")
        page.locator("#doc-format-title_color").select_option("red")
        page.locator("#doc-format-text_color").select_option("black")
        page.locator("#doc-tab-preview").click()
        expect(page.locator("#doc-render .doc-format-title")).to_have_css("color", "rgb(255, 0, 0)")
        expect(page.locator("#doc-render p").first).to_have_css("color", "rgb(0, 0, 0)")
        page.screenshot(path=str(out / "document-fonts.png"), full_page=True)
        # 留在草稿编辑器的测试改动由取消确认流程丢弃，再检查信息库页。
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#doc-tab-edit").click()
        page.locator("#doc-title").fill("")
        page.locator("#doc-body").fill("")
        page.evaluate("KB.docFormat.set({}); window.onbeforeunload = null")
        page.goto(live_server.url + "/app/knowledge/", wait_until="networkidle")
        page.get_by_role("button", name="查看详细信息", exact=True).click()
        expect(page.locator("[data-document-list]")).to_contain_text("浏览器验收资料.md")
        page.locator("details.stored-document summary").click()
        expect(page.locator(".stored-chunk").first).to_contain_text("第一段：项目已完成验收。")
        expect(page.locator(".stored-chunk").nth(1)).to_contain_text("第二段：后续维护安排。")
        expect(page.locator("[data-library-summary]")).to_contain_text("2 个向量记录")
        page.screenshot(path=str(out / "library-details.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator(".modal").bounding_box()["width"] < 391
        page.screenshot(path=str(out / "library-mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.db import connections
    from apps.knowledge.models import KnowledgeLibrary, IngestedFile, LibraryMember
    call_command("migrate", verbosity=0, interactive=False)
    user = get_user_model().objects.create_user(username="template-browser", password=uuid.uuid4().hex)
    library = KnowledgeLibrary.objects.create(code="browser-fixture", name="验收信息库")
    LibraryMember.objects.create(library=library, user=user, role="writer")
    def make_file(library, name, **extra):
        return IngestedFile.objects.create(library=library, original_name=name,
            content_sha256=uuid.uuid4().hex * 2, parse_status="done", size_bytes=100,
            file_type="md", **extra)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log = open(RUN_DIR / "server.log", "w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}",
        "--noreload", "--insecure"], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/app/login/", timeout=1).close()
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError("隔离验收服务启动失败")
                time.sleep(.2)
        else:
            raise RuntimeError("隔离验收服务启动超时")
        run_check(SimpleNamespace(url=url), Client(), user, library, make_file, settings)
        print("PASS: template upload, preview, persistent card selection, grouped details, mobile layout; no browser errors")
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()
        connections.close_all()
        # Only the exact database created by this run is removed; diagnostic logs remain.
        database = (RUN_DIR / "browser.sqlite3").resolve()
        assert database.parent == RUN_DIR.resolve() and RUN_DIR.parent == ROOT / "artifacts" / "test-runs"
        database.unlink(missing_ok=True)
