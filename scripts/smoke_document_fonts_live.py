"""实际服务上的字体/颜色保存、刷新、预览与导出验收，完成后清理测试草稿。"""
import io
import json
import uuid
from docx import Document
from playwright.sync_api import sync_playwright, expect
from smoke_template_upload_live import container_python

BASE = "http://127.0.0.1:8800"
setup = """
import os,json,django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','knowledgebase.settings');django.setup()
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
u=get_user_model().objects.filter(is_staff=True,is_active=True).first();s=SessionStore()
s['_auth_user_id']=str(u.pk);s['_auth_user_backend']='django.contrib.auth.backends.ModelBackend'
s['_auth_user_hash']=u.get_session_auth_hash();s.set_expiry(300);s.save()
print(json.dumps({'key':s.session_key}))
"""
session = json.loads(container_python(setup))
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1100})
        context.add_cookies([{"name": "sessionid", "value": session["key"], "url": BASE}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/app/", wait_until="networkidle")
        csrf = next(c["value"] for c in context.cookies() if c["name"] == "csrftoken")
        title = "字体颜色验收-" + uuid.uuid4().hex[:6]
        settings = {"title_font": "xiaobiaosong", "body_font": "fangsong", "heading_font": "heiti",
                    "subheading_font": "kaiti", "title_color": "red", "text_color": "black"}
        response = page.request.post(BASE + "/api/v1/planner/documents/", headers={"X-CSRFToken": csrf},
            data={"title": title, "content_markdown": "# " + title + "\n\n## 工作安排\n\n这是仿宋正文。\n\n### 注意事项\n\n这是第二段正文。", "format_options": settings})
        assert response.status == 201, response.text()
        pk = response.json()["document_id"]
        try:
            page.reload(wait_until="networkidle")
            page.locator("#btn-docs").click()
            expect(page.locator("#doc-title")).to_have_value(title)
            page.locator("#doc-format-block summary").click()
            expect(page.locator("#doc-format-body_font")).to_have_value("fangsong")
            page.locator("#doc-format-title_color").select_option("black")
            expect(page.locator("#doc-save")).to_be_enabled()
            page.locator("#doc-save").click()
            expect(page.locator("#doc-save")).to_be_disabled()
            page.locator("#doc-format-title_color").select_option("red")
            page.locator("#doc-save").click()
            expect(page.locator("#doc-save")).to_be_disabled()
            page.reload(wait_until="networkidle")
            if not page.locator("#doc-title").is_visible():
                page.locator("#btn-docs").click()
            expect(page.locator("#doc-format-title_color")).to_have_value("red")
            page.locator("#doc-tab-preview").click()
            expect(page.locator("#doc-render .doc-format-title")).to_have_css("color", "rgb(255, 0, 0)")
            expect(page.locator("#doc-render p").first).to_have_css("color", "rgb(0, 0, 0)")
            word = page.request.get(BASE + f"/api/v1/planner/documents/{pk}/download/?export_format=docx")
            assert word.status == 200
            doc = Document(io.BytesIO(word.body()))
            assert "方正小标宋简体" in doc.paragraphs[0]._p.xml
            assert str(doc.paragraphs[0].runs[0].font.color.rgb) == "FF0000"
            pdf = page.request.get(BASE + f"/api/v1/planner/documents/{pk}/download/?export_format=pdf", timeout=60000)
            assert pdf.status == 200 and pdf.body().startswith(b"%PDF-")
            assert not errors, errors
            print("PASS: live format changes, save, reload, preview, Word and PDF exports")
        finally:
            result = page.request.delete(BASE + f"/api/v1/planner/documents/{pk}/", headers={"X-CSRFToken": csrf})
            assert result.status == 200
            browser.close()
finally:
    cleanup = """
import os,sys,django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','knowledgebase.settings');django.setup()
from django.contrib.sessions.models import Session
Session.objects.filter(session_key=sys.stdin.read().strip()).delete()
"""
    container_python(cleanup, session["key"])
