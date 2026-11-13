"""在实际部署容器验证模板上传、下载及持久化，仅清理本脚本创建的模板和会话。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
import uuid

from docx import Document
from playwright.sync_api import sync_playwright, expect
from pptx import Presentation

BASE = "http://127.0.0.1:8800"
ROOT = Path(__file__).resolve().parents[1]


def container_python(source, stdin=""):
    result = subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", source],
                            cwd=ROOT, input=stdin, text=True, capture_output=True, check=True)
    return result.stdout


def main(recreate=False):
    setup = """
import os, json, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledgebase.settings')
django.setup()
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
user = get_user_model().objects.filter(is_staff=True, is_active=True).first()
assert user is not None
session = SessionStore()
session['_auth_user_id'] = str(user.pk)
session['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
session['_auth_user_hash'] = user.get_session_auth_hash()
session.set_expiry(300)
session.save()
print(json.dumps({'key': session.session_key}))
"""
    session = json.loads(container_python(setup))
    created = []
    marker = "上传修复验收-" + uuid.uuid4().hex[:8]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_cookies([{"name": "sessionid", "value": session["key"], "url": BASE}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(BASE + "/app/templates/", wait_until="networkidle")
            csrf = next(cookie["value"] for cookie in context.cookies() if cookie.get("name") == "csrftoken" and "value" in cookie)
            try:
                document = Document()
                document.add_paragraph("公文基本规范模板验收")
                document.add_paragraph("一、标题\n二、正文\n三、落款")
                docx = io.BytesIO()
                document.save(docx)
                presentation = Presentation()
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                from pptx.shapes.autoshape import Shape

                title, body = slide.shapes.title, slide.placeholders[1]
                assert title is not None and isinstance(body, Shape)
                title.text_frame.paragraphs[0].add_run().text = "项目汇报模板验收"
                body.text_frame.paragraphs[0].add_run().text = "项目概况\n进度安排"
                pptx = io.BytesIO()
                presentation.save(pptx)
                sources = [("docx", docx.getvalue(), "公文基本规范模板验收"),
                           ("pptx", pptx.getvalue(), "项目汇报模板验收")]
                for extension, raw, expected in sources:
                    page.locator('[name="name"]').fill(marker + "-" + extension)
                    page.locator('[name="file"]').set_input_files({"name": marker + "." + extension,
                        "mimeType": "application/octet-stream", "buffer": raw})
                    with page.expect_response(lambda response: response.url == BASE + "/api/v1/planner/templates/"
                                              and response.request.method == "POST") as pending:
                        page.get_by_role("button", name="添加模板", exact=True).click()
                    response = pending.value
                    assert response.status == 201, (response.status, response.text())
                    body = response.json()
                    created.append((body["id"], raw))
                    expect(page.locator("#template-notice")).to_contain_text("模板已添加")
                    page.locator(f'[data-preview="{body["id"]}"]').click()
                    expect(page.locator(".stored-text")).to_contain_text(expected)
                    page.get_by_role("button", name="关闭", exact=True).click()
                    original = page.request.get(BASE + f'/api/v1/planner/templates/{body["id"]}/download/')
                    assert original.status == 200 and hashlib.sha256(original.body()).digest() == hashlib.sha256(raw).digest()
                    print(f"PASS: deployed {extension.upper()} upload, preview and original download")
                if recreate:
                    subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "--no-build", "--force-recreate", "web"],
                                   cwd=ROOT, check=True, capture_output=True)
                    for attempt in range(100):
                        try:
                            if page.request.get(BASE + "/api/v1/health/ready/", timeout=1000).status == 200:
                                break
                        except Exception:
                            pass
                        time.sleep(.2)
                    else:
                        raise RuntimeError("重建后的服务未就绪")
                    for template_id, raw in created:
                        original = page.request.get(BASE + f"/api/v1/planner/templates/{template_id}/download/")
                        assert original.status == 200 and original.body() == raw
                    page.reload(wait_until="networkidle")
                    expect(page.locator("#template-list")).to_contain_text(marker)
                    print("PASS: template files and list survive web container recreation")
                assert not errors, errors
            finally:
                for template_id, _ in created:
                    response = page.request.delete(BASE + f"/api/v1/planner/templates/{template_id}/",
                        headers={"X-CSRFToken": csrf})
                    assert response.status == 200, "验收模板清理失败"
                browser.close()
    finally:
        cleanup = """
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledgebase.settings')
django.setup()
from django.contrib.sessions.models import Session
Session.objects.filter(session_key=sys.stdin.read().strip()).delete()
"""
        container_python(cleanup, session["key"])
    print("PASS: temporary templates and login session cleaned up")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recreate-web", action="store_true", help="同时验证容器重建后的原件持久化")
    main(parser.parse_args().recreate_web)
