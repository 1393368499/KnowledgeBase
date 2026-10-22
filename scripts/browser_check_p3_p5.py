r"""真实浏览器复验：助手首页 + 派生文档渲染/导出（不调用任何模型）。

只读 + 下载，不删除任何数据。**用项目虚拟环境跑**（playwright 已装在 `.venv` 里）：
  $env:KB_BC_PW='<browsercheck 账号口令>'; .\.venv\Scripts\python.exe scripts/browser_check_p3_p5.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:8800")
USER = "browsercheck"
PASSWORD = os.environ["KB_BC_PW"]
OUT = Path("artifacts/acceptance")
OUT.mkdir(parents=True, exist_ok=True)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    # ---- 登录 ----
    page.goto(f"{BASE}/app/login/", wait_until="networkidle")
    page.fill('input[name="username"]', USER)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    check("登录成功（离开登录页）", "/app/login" not in page.url, page.url)

    # ---- 助手首页 ----
    page.goto(f"{BASE}/app/assistant/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    check("助手首页标题存在", page.locator("text=助手").count() > 0)
    # 能力目录（/catalog/）是否真的加载出按钮
    catalog_buttons = page.locator("#op-catalog button").count()
    check("能力目录已加载", catalog_buttons > 0, f"{catalog_buttons} 个操作按钮")
    # 使用说明
    guide_text = page.locator("#guide").inner_text()
    check("使用说明已加载", "上传" in guide_text or "确认" in guide_text, guide_text[:60])
    # 清理任务区（/deletions/）
    jobs_text = page.locator("#job-list").inner_text()
    check("清理任务区已加载", "清理" in jobs_text or "暂无" in jobs_text, jobs_text[:40])
    page.screenshot(path=str(OUT / "p3-assistant-home.png"), full_page=True)

    # ---- 派生文档：打开 → 预览渲染 ----
    doc_link = page.locator("#doc-list a").first
    check("派生文档出现在列表", doc_link.count() > 0)
    if doc_link.count() > 0:
        doc_link.click()
        page.wait_for_timeout(900)
        rendered = page.locator("#doc-render")
        html = rendered.inner_html()
        check("预览渲染出标题", "<h2>" in html, html[:80])
        check("预览渲染出粗体", "<strong>" in html)
        check("预览渲染出表格", "<table>" in html and "<td>" in html)
        check("预览渲染出列表", "<ul>" in html and "<li>" in html)
        check("预览渲染出代码块", "<pre><code>" in html)
        # script 必须被转义，且 doc-render 里不能有真的 script 节点
        check("script 被转义", "&lt;script&gt;" in html and "<script>" not in html)
        check("doc-render 无 script 节点", rendered.locator("script").count() == 0)
        page.screenshot(path=str(OUT / "p5-doc-preview.png"), full_page=True)

        # ---- 导出：Markdown 与 HTML（验证 export_format 不再 404）----
        with page.expect_download(timeout=15000) as md_info:
            page.click("#doc-download")
        md = md_info.value
        md_path = OUT / "export-check.md"
        md.save_as(str(md_path))
        md_text = md_path.read_text(encoding="utf-8")
        check("Markdown 下载成功", md.suggested_filename.endswith(".md"), md.suggested_filename)
        check("Markdown 内容是原文", "# 复验标题" in md_text)

        with page.expect_download(timeout=15000) as html_info:
            page.click("#doc-download-html")
        html_file = html_info.value
        html_path = OUT / "export-check.html"
        html_file.save_as(str(html_path))
        html_text = html_path.read_text(encoding="utf-8")
        check("HTML 下载成功", html_file.suggested_filename.endswith(".html"),
              html_file.suggested_filename)
        check("HTML 是真实渲染（有 h2/table）",
              "<h2>" in html_text and "<table>" in html_text)
        check("HTML 中 script 被转义", "&lt;script&gt;" in html_text)
        # 关键：导出正文与右侧预览同构
        check("导出与预览一致（都有 <td>12 个月</td>）", "<td>12 个月</td>" in html_text)

    # ---- 会话可用性（不触发模型：只检查不可用时是否如实提示）----
    page.fill("#chat-input", "（浏览器复验）你好")
    page.click("#chat-send")
    page.wait_for_timeout(4000)
    chat_text = page.locator("#chat-log").inner_text()
    notice_text = page.locator("#assistant-notice").inner_text()
    answered = "助手" in chat_text and "不可用" not in chat_text
    check(
        "会话有真实回答或如实提示不可用（不伪造）",
        answered or "不可用" in notice_text or "不可用" in chat_text,
        (notice_text or chat_text)[:80].replace("\n", " "),
    )
    page.screenshot(path=str(OUT / "p3-chat.png"), full_page=True)

    real_errors = [e for e in console_errors if "favicon" not in e.lower()]
    check("无 JS 控制台错误", not real_errors, "; ".join(real_errors[:2]))
    browser.close()

print("\n截图:", OUT)
print("失败项:", failures if failures else "无")
sys.exit(1 if failures else 0)
