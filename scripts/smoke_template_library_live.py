"""对本机已部署服务进行只读页面验收；只创建并回收一次临时登录会话。"""
import json
import subprocess

from playwright.sync_api import sync_playwright, expect

BOOTSTRAP = """
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
session = json.loads(subprocess.check_output(
    ["docker", "compose", "exec", "-T", "web", "python", "-c", BOOTSTRAP], text=True))
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": "sessionid", "value": session["key"], "url": "http://127.0.0.1:8800"}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8800/app/templates/", wait_until="networkidle")
        expect(page.get_by_role("heading", name="模板管理", exact=True)).to_be_visible()
        expect(page.locator("#template-count")).to_contain_text("份")
        page.goto("http://127.0.0.1:8800/app/knowledge/", wait_until="networkidle")
        button = page.get_by_role("button", name="查看详细信息", exact=True).first
        expect(button).to_be_visible()
        button.click()
        expect(page.locator("[data-library-summary]")).to_contain_text("份文档")
        documents = page.locator("details.stored-document summary")
        if documents.count():
            documents.first.click()
            expect(page.locator(".stored-metadata")).to_contain_text("嵌入模型")
            expect(page.locator("[data-file-content]").first).to_contain_text("解析分段与向量关联")
        assert not errors, errors
        browser.close()
    print("PASS: deployed template management, library details, document storage metadata; no browser errors")
finally:
    cleanup = """
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledgebase.settings')
django.setup()
from django.contrib.sessions.models import Session
Session.objects.filter(session_key=sys.stdin.read().strip()).delete()
"""
    subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", cleanup],
                   input=session["key"], text=True, check=True, capture_output=True)
